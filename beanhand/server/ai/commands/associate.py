import argparse
import json
import sys
from pathlib import Path
from typing import Any

from openai.types.chat import (
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)

from beanhand.server.ai.config import Configuration
from beanhand.server.llm import (
    file_to_image_parts,
    ssl_verify_path,
    stream_reasoning_and_output,
)
from beanhand.structs import AssociateRequest

RECEIPT_INFO_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "RECEIPT_INFO_PROMPT.md"
)
RECEIPT_MATCH_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "RECEIPT_MATCH_PROMPT.md"
)


def _read_candidates_line() -> list[Any]:
    """Read the candidate-transaction list sent by the client on the second
    stdin line, after the first LLM pass has completed."""
    try:
        candidates_line = sys.stdin.readline()
        sys.stdin.close()
        candidates = json.loads(candidates_line)
    except Exception as e:
        print(f"error: invalid candidate input: {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(candidates, list):
        print(
            "error: invalid candidate input: candidates must be a JSON array",
            file=sys.stderr,
        )
        sys.exit(1)

    # Round-trip via a string so every escape is normalized (LLM-injection
    # defense): the re-serialized form is what the LLM will ever see.
    candidates_text = json.dumps(candidates)
    normalized: list[Any] = json.loads(candidates_text)
    return normalized


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    """Match a receipt against candidate transactions (both via stdin).

    The command carries no CLI argument.  The receipt arrives on the first stdin
    line (inline, base64-encoded bytes, relayed by the client from the documents
    server); its date and amount are extracted by the LLM and streamed to the
    client as JSONL.  The client then queries Beancount and writes the candidate
    transactions on the second stdin line; the LLM ranks the candidates and the
    structured match results are streamed to the client.
    """
    from httpx import Client as HttpxClient
    from openai import OpenAI

    try:
        req = AssociateRequest.deserialize(sys.stdin.readline())
    except Exception as e:
        print(
            f"error: invalid receipt input: {e}",
            file=sys.stderr,
        )
        sys.exit(1)
    fn, raw = req.receipt.filename, req.receipt.content

    client = OpenAI(
        api_key=cfg.token,
        base_url=cfg.api_url,
        http_client=HttpxClient(verify=ssl_verify_path()),
    )

    # Build the image part(s); PDFs are rendered to PNG page-by-page.
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
        model=cfg.model_name,
        messages=messages,
        stream=True,
    )

    stream_reasoning_and_output(resp)

    candidates = _read_candidates_line()
    candidates_text = json.dumps(candidates)

    prompt_text = RECEIPT_MATCH_PROMPT_PATH.read_text().format(
        candidates_json=candidates_text
    )

    text_part = {
        "type": "text",
        "text": prompt_text,
    }

    messages = [{"role": "user", "content": [text_part, *image_parts]}]

    resp = client.chat.completions.create(
        model=cfg.model_name,
        messages=messages,
        stream=True,
    )

    stream_reasoning_and_output(resp)


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    sp.add_parser(
        "beanhand.HelpAssociateReceipt",
        help="Match a receipt against candidate transactions (both via stdin)",
    )
    return sp
