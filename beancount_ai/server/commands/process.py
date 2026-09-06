import argparse
import json
import os
import sys
from pathlib import Path
from typing import IO, cast

from openai._streaming import Stream
from openai.types.chat import (
    ChatCompletionChunk,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)

from beancount_ai.server.config import Configuration
from beancount_ai.server.llm import (
    file_to_image_parts,
    ssl_verify_path,
    stream_reasoning_and_output,
)
from beancount_ai.server.storage import make_receipt_backend
from beancount_ai.structs import AccountRef, check_account_refs

RECEIPT_CONVERSION_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "RECEIPT_CONVERSION_PROMPT.md"
)


def _read_account_refs_and_close_stdin(stdin: IO[str]) -> list[AccountRef]:
    # Read the account list (a JSON array of {name, rule?} objects) from stdin.
    try:
        data = json.loads(stdin.read())
        stdin.close()
    except Exception as e:
        print(f"error: invalid account list input: {e}", file=sys.stderr)
        sys.exit(1)

    try:
        return check_account_refs(data)
    except ValueError as reason:
        print(f"error: invalid account list input: {reason}", file=sys.stderr)
        sys.exit(1)


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    from httpx import Client as HttpxClient
    from openwebui_client import OpenWebUIClient

    account_refs = _read_account_refs_and_close_stdin(sys.stdin)

    argsfilename = bytes.fromhex(args.filename.encode("ascii")).decode("utf-8")
    fn = os.path.basename(argsfilename)

    storage = make_receipt_backend(cfg, "uningested")

    receipt_path = f"/{fn}"

    print(
        f"Reading {fn} from {cfg.documents.uningested_location_name()}",
        file=sys.stderr,
    )

    account_text = json.dumps(account_refs, indent=2)
    prompt_text = RECEIPT_CONVERSION_PROMPT_PATH.read_text()
    prompt_text = prompt_text.format(accounts=account_text)

    try:
        raw = storage.read(receipt_path).data
    except Exception as e:
        print(f"error: cannot read {fn}: {e}", file=sys.stderr)
        sys.exit(1)

    verify = ssl_verify_path()

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


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    process_cmd = sp.add_parser(
        "beanai.Process", help="Process a receipt image via LLM"
    )
    process_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )
    return sp
