#!/usr/bin/env python3
"""bean-ai-server — qrexec RPC service for receipt processing (runs on VM with receipts).

Subcommands:
    list    List receipt filenames to import (JSON output)

Config is read from ~/.config/bean-ai.json unless overridden.
As a qrexec service, it reads nothing from stdin and only writes structured results to stdout.
"""

import argparse
import base64
import datetime
import json
import os
import ssl
import sys
from functools import partial
from pathlib import Path
from typing import IO, Any, Literal, TypedDict, cast

from openai._streaming import Stream
from openai.types.chat import (
    ChatCompletionChunk,
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)
from webdav4.client import Client, ResourceNotFound  # type:ignore

from beancount_ai.structs import RefineRequest

from .config import Configuration, WebDAVDocumentSourcesConfiguration
from .pdf import render_pdf_pages_to_png

RECEIPT_CONVERSION_PROMPT_PATH = (
    Path(__file__).resolve().parent / "RECEIPT_CONVERSION_PROMPT.md"
)
RECEIPT_MATCH_PROMPT_PATH = Path(__file__).resolve().parent / "RECEIPT_MATCH_PROMPT.md"
RECEIPT_INFO_PROMPT_PATH = Path(__file__).resolve().parent / "RECEIPT_INFO_PROMPT.md"
TRANSACTION_REFINEMENT_PROMPT_PATH = (
    Path(__file__).resolve().parent / "TRANSACTION_REFINEMENT_PROMPT.md"
)


def _ssl_verify_path() -> str:
    """Resolve an SSL CA bundle path for use with ``httpx`` clients.

    Falls back to ``certifi.where()`` when no OpenSSL default is configured.

    This is important because, in some Linux distributions, by default
    the OS certificate bundle is not paid attention to, resulting in
    private CA certificates not being loaded, ultimately resulting in
    SSL unverified errors.
    """
    ssl_paths = ssl.get_default_verify_paths()
    if ssl_paths.cafile:
        return ssl_paths.cafile
    import certifi

    return certifi.where()


def file_to_image_parts(
    fn: str, raw: bytes
) -> list["ChatCompletionContentPartImageParam"]:
    image_parts: list["ChatCompletionContentPartImageParam"] = []

    suffix = Path(fn).suffix.lower()

    if suffix == ".pdf":
        print("PDF detected; converting pages to PNG...", file=sys.stderr)
        try:
            page_infos: list[tuple[int, float, int]] = []
            for page_num, dpi_used, png_bytes in render_pdf_pages_to_png(raw):
                b64data = base64.b64encode(png_bytes).decode("utf-8")
                image_parts.append(
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/png;base64,{b64data}",
                            "detail": "high",
                        },
                    }
                )
                page_infos.append((page_num, dpi_used, len(png_bytes)))
        except Exception as e:
            # Report fatal error as a stream message.  Then exit.
            print(f"error PDF rendering failed: {e}", file=sys.stderr)
            sys.exit(1)
        for pnum, dpi_used, nbytes in page_infos:
            print(
                f"  Page {pnum}: {nbytes} bytes @ {dpi_used:.0f} DPI", file=sys.stderr
            )
    else:
        b64data = base64.b64encode(raw).decode("utf-8")
        mime_map = {
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".png": "image/png",
        }
        mime_type = mime_map.get(suffix, "image/jpeg")
        image_parts.append(
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{mime_type};base64,{b64data}",
                    "detail": "high",
                },
            }
        )

    return image_parts


# -- subcommands -----------------------------------------------------------

_EXT = frozenset((".jpg", ".jpeg", ".png", ".pdf"))


class ItemListing(TypedDict):
    name: str
    content_length: int | None
    modified: datetime.datetime


class WebDAVClient:
    def __init__(
        self,
        cfg: WebDAVDocumentSourcesConfiguration,
        category: Literal["unassociated"] | Literal["uningested"],
    ):
        url = (
            cfg.receipts_ingestion_url()
            if category == "uningested"
            else cfg.receipts_association_url()
        )

        self.client = Client(url, auth=(cfg.username, cfg.password))

    def list(self) -> list[ItemListing]:
        return cast(list[ItemListing], self.client.ls("/", detail=True))

    def read(self, filename: str) -> bytes:
        with self.client.open(filename, "rb") as remote_file:
            return cast(bytes, remote_file.read())

    def remove(self, filename: str) -> None:
        self.client.remove(filename)


def do_list(
    cfg: Configuration,
    category: Literal["unassociated"] | Literal["uningested"],
    args: argparse.Namespace,
) -> None:
    if isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration):
        client = WebDAVClient(cfg.documents, category)
    else:
        assert 0, "not reached"

    try:
        items = client.list()
    except Exception as e:
        print(f"error: WebDAV list failed: {e}", file=sys.stderr)
        sys.exit(1)

    files: list[ItemListing] = []
    for item in items:
        name = item["name"]
        if not any(name.lower().endswith(ext) for ext in _EXT):
            continue
        files.append(
            {
                "name": name,
                "content_length": item["content_length"],
                "modified": item["modified"],
            }
        )

    files.sort(key=lambda f: f["modified"])

    print(
        json.dumps(
            {
                "receipts": [f["name"] for f in files],
                "count": len(files),
            }
        )
    )


def do_list_unassociated(cfg: Configuration, args: argparse.Namespace) -> None:
    do_list(cfg, "unassociated", args)


def do_list_uningested(cfg: Configuration, args: argparse.Namespace) -> None:
    do_list(cfg, "uningested", args)


def stream_reasoning_and_output(resp: Stream[ChatCompletionChunk]) -> None:
    flush_every = 10
    # This will emit one of three types of lines:
    # {"reasoning": "reasoning text chunk"}
    # {"output": "output text chunk"}
    # {"finish":" finish reason"} (usually "stop")
    for chunk in resp:
        choice = chunk.choices[0]
        delta = choice.delta
        if hasattr(delta, "reasoning_content") and delta.reasoning_content is not None:  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            json.dump({"reasoning": delta.reasoning_content}, sys.stdout)  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            sys.stdout.write("\n")
        elif delta.content is not None:
            json.dump({"output": delta.content}, sys.stdout)
            sys.stdout.write("\n")
        elif choice.finish_reason is not None:
            json.dump({"finish": choice.finish_reason}, sys.stdout)
            sys.stdout.write("\n")
        flush_every += 1
        if flush_every % 10 == 0:
            sys.stdout.flush()
    sys.stdout.flush()


def read_accounts_and_close_stdin(stdin: IO[str]) -> list[str]:
    # Read accounts list from stdin (the client).
    try:
        account_lines = cast(list[str], json.loads(stdin.read()))
        assert isinstance(account_lines, list)
        assert all(isinstance(ln, str) for ln in account_lines)
        for n, acc in enumerate(account_lines):
            acc = acc.splitlines()[0].strip()
            account_lines[n] = acc
        stdin.close()
    except Exception as e:
        print(f"error: invalid account list input: {e}", file=sys.stderr)
        sys.exit(1)

    return account_lines


def do_process(cfg: Configuration, args: argparse.Namespace) -> None:
    from httpx import Client as HttpxClient
    from openwebui_client import OpenWebUIClient

    account_lines = read_accounts_and_close_stdin(sys.stdin)

    argsfilename = bytes.fromhex(args.filename.encode("ascii")).decode("utf-8")
    fn = os.path.basename(argsfilename)

    if isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration):
        webdav_client = WebDAVClient(cfg.documents, "uningested")
    else:
        assert 0, "not reached"

    receipt_path = f"/{fn}"

    print(f"Reading {fn} from WebDAV receipts URL", file=sys.stderr)

    account_text = json.dumps(account_lines)
    prompt_text = RECEIPT_CONVERSION_PROMPT_PATH.read_text()
    prompt_text = prompt_text.format(accounts=account_text)

    try:
        raw = webdav_client.read(receipt_path)
    except Exception as e:
        print(f"error: cannot read {fn}: {e}", file=sys.stderr)
        sys.exit(1)

    verify = _ssl_verify_path()

    client = OpenWebUIClient(
        api_key=cfg.ai.token,
        base_url=cfg.ai.api_url,
        http_client=HttpxClient(verify=verify),
    )

    text_part: ChatCompletionContentPartTextParam = {
        "type": "text",
        "text": prompt_text,
    }

    image_parts = file_to_image_parts(fn, raw)

    messages: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": [text_part, *image_parts]}
    ]

    resp = client.chat.completions.create(
        model=cfg.ai.model_name,
        messages=messages,
        stream=True,
    )

    stream_reasoning_and_output(cast(Stream[ChatCompletionChunk], resp))


def do_refine(cfg: Configuration, args: argparse.Namespace) -> None:
    """Refine an existing Beancount transaction using its linked documents.

    The command carries no CLI argument.  The whole request arrives on stdin as a
    single plain-JSON object (``transaction_text``, ``accounts``, ``documents``);
    each document's raw bytes are base64-encoded.  The LLM produces a rewritten
    Beancount transaction, streamed back as JSONL like the other handlers.  The
    original transaction block is preserved verbatim in the prompt and only
    posting-level content may be refined.
    """
    from httpx import Client as HttpxClient
    from openwebui_client import OpenWebUIClient

    request_data = json.loads(sys.stdin.read())
    if (
        not isinstance(request_data, dict)
        or "transaction_text" not in request_data
        or not isinstance(request_data["transaction_text"], str)
        or not request_data["transaction_text"].strip()
    ):
        print("error: Invalid request: missing transaction_text", file=sys.stderr)
        sys.exit(1)

    request_data = cast(dict[Any, Any], request_data)
    if "documents" not in request_data:
        request_data["documents"] = []
    for dn, d in enumerate(cast(list[Any], request_data["documents"])):
        if "filepath" not in d or not isinstance(d["filepath"], str):
            print(
                f"error: Invalid request: document {dn} missing or invalid file path",
                file=sys.stderr,
            )
            sys.exit(1)
        if "data" not in d or not isinstance(d["data"], str):
            print(
                f"error: Invalid request: document {dn} missing or invalid data",
                file=sys.stderr,
            )
            sys.exit(1)
    if (
        "accounts" not in request_data
        or not isinstance(request_data["accounts"], list)
        or not all(
            isinstance(acc, str) for acc in cast(list[Any], request_data["accounts"])
        )
    ):
        print(
            f"error: Invalid request: account list missing or invalid", file=sys.stderr
        )
        sys.exit(1)

    request = cast(RefineRequest, request_data)
    transaction_text = request["transaction_text"]
    accounts = request["accounts"]
    documents = request.get("documents", [])

    image_parts: list[ChatCompletionContentPartImageParam] = []
    for doc in documents:
        fn = Path(doc["filepath"])
        suffix = fn.suffix.lower()
        if suffix not in _EXT:
            print(f"warning: unsupported document format, skipping: {suffix}", file=sys.stderr)
            continue
        raw = base64.b64decode(doc["data"])
        image_parts.extend(file_to_image_parts(doc["filepath"], raw))

    account_text = json.dumps(accounts)
    prompt_text = TRANSACTION_REFINEMENT_PROMPT_PATH.read_text()
    prompt_text = prompt_text.format(
        transaction_text=transaction_text, accounts=account_text
    )

    client = OpenWebUIClient(
        api_key=cfg.ai.token,
        base_url=cfg.ai.api_url,
        http_client=HttpxClient(verify=_ssl_verify_path()),
    )

    text_part: ChatCompletionContentPartTextParam = {
        "type": "text",
        "text": prompt_text,
    }

    messages: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": [text_part, *image_parts]}
    ]

    resp = client.chat.completions.create(
        model=cfg.ai.model_name,
        messages=messages,
        stream=True,
    )

    stream_reasoning_and_output(cast(Stream[ChatCompletionChunk], resp))


def do_fetch(cfg: Configuration, args: argparse.Namespace) -> None:
    fn = os.path.basename(bytes.fromhex(args.filename).decode("utf-8"))

    print(f"Fetching {fn} from WebDAV receipts URL", file=sys.stderr)
    receipt_path = f"/{fn}"

    if isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration):
        client_factory = partial(WebDAVClient, cfg.documents)
    else:
        assert 0, "not reached"

    try:
        webdav_client = client_factory("uningested")
        raw = webdav_client.read(receipt_path)
    except ResourceNotFound:
        try:
            webdav_client = client_factory("unassociated")
            raw = webdav_client.read(receipt_path)
        except Exception as e:
            print(f"error: cannot read {fn}: {e}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"error: cannot read {fn}: {e}", file=sys.stderr)
        sys.exit(1)

    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def do_remove(cfg: Configuration, args: argparse.Namespace) -> None:
    fn = os.path.basename(bytes.fromhex(args.filename).decode("utf-8"))
    receipt_path = f"/{fn}"

    print(f"Removing {fn} from WebDAV receipts URL", file=sys.stderr)

    if isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration):
        client_factory = partial(WebDAVClient, cfg.documents)
    else:
        assert 0, "not reached"

    try:
        webdav_client = client_factory("uningested")
        webdav_client.remove(receipt_path)
    except ResourceNotFound:
        try:
            webdav_client = client_factory("unassociated")
            webdav_client.remove(receipt_path)
        except Exception as e:
            print(f"error: cannot remove {fn}: {e}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"error: cannot remove {fn}: {e}", file=sys.stderr)
        sys.exit(1)


# FIXME split this function into two, no need to keep stdin open while
# the other side does things with the first return.
def do_help_associate_receipt(cfg: Configuration, args: argparse.Namespace) -> None:
    """Process a receipt against a list of candidate transactions (passed via stdin).

    Reads candidates JSON from stdin; the filename arg comes via hex-encoded CLI.
    The function loads the receipt image from WebDAV, feeds it to LLM together
    with candidate text, and writes structured match results to stdout as plain JSON.
    """
    from httpx import Client as HttpxClient
    from openwebui_client import OpenWebUIClient

    argsfilename = bytes.fromhex(args.filename.encode("ascii")).decode("utf-8")
    fn = os.path.basename(argsfilename)

    if isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration):
        webdav_client = WebDAVClient(cfg.documents, "unassociated")
    else:
        assert 0, "not reached"

    receipt_path = f"/{fn}"

    print(f"Reading {fn} from WebDAV receipts URL", file=sys.stderr)

    try:
        raw = webdav_client.read(receipt_path)
    except Exception as e:
        print(f"error: cannot read {fn}: {e}", file=sys.stderr)
        sys.exit(1)

    verify = _ssl_verify_path()

    client = OpenWebUIClient(
        api_key=cfg.ai.token,
        base_url=cfg.ai.api_url,
        http_client=HttpxClient(verify=verify),
    )

    # Build the image part(s); reuse PDF → PNG logic from do_process.
    image_parts: list[ChatCompletionContentPartImageParam] = file_to_image_parts(
        fn, raw
    )

    prompt_text = RECEIPT_INFO_PROMPT_PATH.read_text().format(
        **{"fn": "`" + fn.replace("`", "\\`") + "`"}
    )

    text_part: ChatCompletionContentPartTextParam = {
        "type": "text",
        "text": prompt_text,
    }

    messages: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": [text_part, *image_parts]}
    ]

    resp = client.chat.completions.create(
        model=cfg.ai.model_name,
        messages=messages,
        stream=True,
    )

    stream_reasoning_and_output(cast(Stream[ChatCompletionChunk], resp))

    # Read candidates from stdin.
    try:
        candidates_raw = sys.stdin.read()
        # prevent LLM injection.
        candidates = json.loads(candidates_raw)
        candidates_text = json.dumps(candidates)
    except Exception as e:
        print(f"error: invalid candidate input: {e}", file=sys.stderr)
        sys.exit(1)

    prompt_text = RECEIPT_MATCH_PROMPT_PATH.read_text().format(
        candidates_json=candidates_text
    )

    text_part = {
        "type": "text",
        "text": prompt_text,
    }

    messages = [{"role": "user", "content": [text_part, *image_parts]}]

    resp = client.chat.completions.create(
        model=cfg.ai.model_name,
        messages=messages,
        stream=True,
    )

    stream_reasoning_and_output(cast(Stream[ChatCompletionChunk], resp))


# -- CLI -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bean-ai-server",
        description="qrexec RPC service for bean-ai",
    )
    ap.add_argument(
        "--config",
        "-c",
        default=None,
        dest="conf_path",
        help="Path to the config file; overrides $BEAN_AI_CONFIG and the default",
    )

    sp = ap.add_subparsers(dest="command")

    sp.add_parser(
        "beanai.ListUningested",
        help="List receipt filenames to import as transactions (JSON)",
    )
    sp.add_parser(
        "beanai.ListUnassociated",
        help="List receipt filenames to associate with transactions (JSON)",
    )

    fetch_cmd = sp.add_parser(
        "beanai.Fetch", help="Write the raw contents of a receipt file to stdout"
    )
    fetch_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )

    remove_cmd = sp.add_parser(
        "beanai.Remove", help="Delete a receipt file from WebDAV"
    )
    remove_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )

    process_cmd = sp.add_parser(
        "beanai.Process", help="Process a receipt image via LLM"
    )
    process_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )

    match_cmd = sp.add_parser(
        "beanai.HelpAssociateReceipt",
        help="Match a receipt against candidate transactions (candidates via stdin)",
    )
    match_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )

    sp.add_parser(
        "beanai.Refine",
        help="Refine an existing transaction using linked documents (request via stdin)",
    )

    return ap


def main() -> None:
    import sys

    ap = build_parser()
    args = ap.parse_args()

    if not args.command:
        ap.print_help(sys.stderr)
        sys.exit(1)

    cfg = Configuration.load(args.conf_path)

    dispatch = {
        "beanai.ListUningested": do_list_uningested,
        "beanai.ListUnassociated": do_list_unassociated,
        "beanai.Fetch": do_fetch,
        "beanai.Process": do_process,
        "beanai.HelpAssociateReceipt": do_help_associate_receipt,
        "beanai.Remove": do_remove,
        "beanai.Refine": do_refine,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        ap.print_help(sys.stderr)
        sys.exit(1)

    handler(cfg, args)


if __name__ == "__main__":
    main()
