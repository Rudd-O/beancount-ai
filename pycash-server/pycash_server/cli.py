#!/usr/bin/env python3
"""pycash-server — qrexec RPC service for receipt processing (runs on pym).

Subcommands:
    list    List receipt filenames to import (JSON output)

Config is read from ~/.config/pycash.json unless overridden.
As a qrexec service, it reads nothing from stdin and only writes structured results to stdout.
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import cast, TypedDict
from webdav4.client import Client  # type:ignore

from .pdf import render_pdf_pages_to_png

CONF_DEFAULT = Path.home() / ".config" / "pycash.json"
PROMPT_PATH = Path(__file__).resolve().parent / "RECEIPT_CONVERSION_PROMPT.md"


# -- configuration ---------------------------------------------------------


class Configuration(TypedDict):
    openwebui_url: str
    openwebui_token: str
    openwebui_model: str

    # WebDAV data sources and credentials
    receipts_username: str
    receipts_password: str
    receipts_ingestion_url: str


_cfg: Configuration | None = None  # cached config loaded from resolved path
_cfg_path: Path | None = None  # which file was actually loaded

_cfg_override: str | None = None  # set by --config before get_cfg() is called


def _get_cfg_path(override: str | None) -> Path:
    """Return the config file path, resolving overrides in order of priority.

    Priority (highest → lowest):
        1. ``--config`` CLI argument
        2. ``PYCASH_CONFIG`` environment variable
        3. Default ``~/.config/pycash.json``
    """
    if override:
        return Path(override)
    env_cfg = os.environ.get("PYCASH_CONFIG")
    if env_cfg:
        return Path(env_cfg)
    return CONF_DEFAULT


def get_cfg() -> Configuration:
    """Load and cache the config from the resolved path.

    If called multiple times, only the *first* invocation's resolution is used;
    subsequent calls return the cached result (prevents a user from accidentally
    reloading with different paths within one process).
    """
    global _cfg, _cfg_path
    if _cfg is not None:
        return _cfg

    fp = _get_cfg_path(_cfg_override)
    with open(fp) as fh:
        _cfg = cast(Configuration, json.load(fh))
    _cfg_path = fp
    return _cfg


# -- subcommands -----------------------------------------------------------

_EXT = frozenset((".jpg", ".jpeg", ".png", ".pdf"))


def do_list(args: argparse.Namespace) -> None:
    cfg = get_cfg()

    url = cfg["receipts_ingestion_url"]
    username = cfg["receipts_username"]
    password = cfg["receipts_password"]

    client = Client(url, auth=(username, password))

    class ItemListing(TypedDict):
        name: str
        content_length: int | None
        modified: datetime.datetime

    try:
        items = cast(list[ItemListing], client.ls("/", detail=True))
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


def do_process(args: argparse.Namespace) -> None:
    import base64
    import ssl

    from openai._streaming import Stream

    from openai.types.chat import (
        ChatCompletionContentPartImageParam,
        ChatCompletionChunk,
        ChatCompletionContentPartTextParam,
        ChatCompletionMessageParam,
    )
    from httpx import Client as HttpxClient
    from openwebui_client import OpenWebUIClient

    cfg = get_cfg()

    url = cfg["receipts_ingestion_url"]
    username = cfg["receipts_username"]
    password = cfg["receipts_password"]

    argsfilename = bytes.fromhex(args.filename.encode("ascii")).decode("utf-8")
    fn = os.path.basename(argsfilename)
    webdav_client = Client(url, auth=(username, password))

    receipt_path = f"/{fn}"

    print(f"Reading {fn} from WebDAV receipts URL", file=sys.stderr)

    prompt_text = PROMPT_PATH.read_text()

    try:
        with webdav_client.open(receipt_path, "rb") as remote_file:
            raw: bytes = remote_file.read()  # type: ignore
    except Exception as e:
        print(f"error: cannot read {fn}: {e}", file=sys.stderr)
        sys.exit(1)

    suffix = Path(fn).suffix.lower()

    ssl_paths = ssl.get_default_verify_paths()
    if ssl_paths.cafile:
        verify = ssl_paths.cafile
    else:
        import certifi

        verify = certifi.where()

    client = OpenWebUIClient(
        api_key=cfg["openwebui_token"],
        base_url=cfg["openwebui_url"],
        http_client=HttpxClient(verify=verify),
    )

    text_part: ChatCompletionContentPartTextParam = {
        "type": "text",
        "text": prompt_text,
    }

    image_parts: list[ChatCompletionContentPartImageParam] = []

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
            print(json.dumps({"error": f"PDF rendering failed: {e}"}))
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

    messages: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": [text_part, *image_parts]}
    ]

    resp = client.chat.completions.create(
        model=cfg["openwebui_model"],
        messages=messages,
        stream=True,
    )

    flush_every = 0
    # This will emit one of three types of lines:
    # {"reasoning": "reasoning text chunk"}
    # {"output": "output text chunk"}
    # {"finish":" finish reason"} (usually "stop")
    for chunk in cast(Stream[ChatCompletionChunk], resp):
        choice = chunk.choices[0]
        delta = choice.delta
        if hasattr(delta, "reasoning_content") and delta.reasoning_content is not None:  # type:ignore
            json.dump({"reasoning": delta.reasoning_content}, sys.stdout)  # type:ignore
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


def do_fetch(args: argparse.Namespace) -> None:
    cfg = get_cfg()

    url = cfg["receipts_ingestion_url"]
    username = cfg["receipts_username"]
    password = cfg["receipts_password"]

    fn = os.path.basename(bytes.fromhex(args.filename).decode("utf-8"))
    webdav_client = Client(url, auth=(username, password))
    receipt_path = f"/{fn}"

    print(f"Fetching {fn} from WebDAV receipts URL", file=sys.stderr)

    try:
        with webdav_client.open(receipt_path, "rb") as remote_file:
            raw: bytes = remote_file.read()  # type: ignore
    except Exception as e:
        print(f"error: cannot read {fn}: {e}", file=sys.stderr)
        sys.exit(1)

    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def do_remove(args: argparse.Namespace) -> None:
    cfg = get_cfg()

    url = cfg["receipts_ingestion_url"]
    username = cfg["receipts_username"]
    password = cfg["receipts_password"]

    fn = os.path.basename(bytes.fromhex(args.filename).decode("utf-8"))
    webdav_client = Client(url, auth=(username, password))
    receipt_path = f"/{fn}"

    print(f"Removing {fn} from WebDAV receipts URL", file=sys.stderr)

    try:
        webdav_client.remove(receipt_path)
    except Exception as e:
        print(f"error: cannot remove {fn}: {e}", file=sys.stderr)
        sys.exit(1)


# -- CLI -------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="pycash-server",
        description="qrexec RPC service for pycash",
    )
    ap.add_argument(
        "--config",
        "-c",
        default=None,
        dest="conf_path",
        help="Path to the config file; overrides $PYCASH_CONFIG and the default",
    )

    sp = ap.add_subparsers(dest="command")

    sp.add_parser("pycash.List", help="List receipt filenames to import (JSON)")

    fetch_cmd = sp.add_parser(
        "pycash.Fetch", help="Write the raw contents of a receipt file to stdout"
    )
    fetch_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )

    remove_cmd = sp.add_parser(
        "pycash.Remove", help="Delete a receipt file from WebDAV"
    )
    remove_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )

    process_cmd = sp.add_parser(
        "pycash.Process", help="Process a receipt image via Open-WebUI"
    )
    process_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )

    return ap


def main() -> None:
    import sys

    global _cfg_override  # override resolved by --config before any get_cfg() call
    ap = build_parser()
    args = ap.parse_args()

    if not args.command:
        ap.print_help(sys.stderr)
        sys.exit(1)

    _cfg_override = args.conf_path  # set it once for downstream calls to get_cfg()
    _cfg = None  # ensure fresh start if already cached (reload from --config path)

    dispatch = {
        "pycash.List": do_list,
        "pycash.Fetch": do_fetch,
        "pycash.Process": do_process,
        "pycash.Remove": do_remove,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        ap.print_help(sys.stderr)
        sys.exit(1)

    handler(args)


if __name__ == "__main__":
    main()
