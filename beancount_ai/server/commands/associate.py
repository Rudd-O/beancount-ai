import argparse
import json
import os
import sys
from pathlib import Path
from typing import cast

from openai._streaming import Stream
from openai.types.chat import (
    ChatCompletionChunk,
    ChatCompletionContentPartImageParam,
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

RECEIPT_INFO_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "RECEIPT_INFO_PROMPT.md"
)
RECEIPT_MATCH_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "RECEIPT_MATCH_PROMPT.md"
)


# FIXME split this function into two, no need to keep stdin open while
# the other side does things with the first return.
def run(cfg: Configuration, args: argparse.Namespace) -> None:
    """Process a receipt against a list of candidate transactions (passed via stdin).

    Reads candidates JSON from stdin; the filename arg comes via hex-encoded CLI.
    The function loads the receipt image from the configured storage
    backend, feeds it to LLM together with candidate text, and writes
    structured match results to stdout as plain JSON.
    """
    from httpx import Client as HttpxClient
    from openwebui_client import OpenWebUIClient

    argsfilename = bytes.fromhex(args.filename.encode("ascii")).decode("utf-8")
    fn = os.path.basename(argsfilename)

    storage = make_receipt_backend(cfg, "unassociated")

    receipt_path = f"/{fn}"

    print(
        f"Reading {fn} from {cfg.documents.unassociated_location_name()}",
        file=sys.stderr,
    )

    try:
        raw = storage.read(receipt_path)
    except Exception as e:
        print(f"error: cannot read {fn}: {e}", file=sys.stderr)
        sys.exit(1)

    verify = ssl_verify_path()

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


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    match_cmd = sp.add_parser(
        "beanai.HelpAssociateReceipt",
        help="Match a receipt against candidate transactions (candidates via stdin)",
    )
    match_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )
    return sp
