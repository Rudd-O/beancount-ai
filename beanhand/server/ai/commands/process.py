import argparse
import json
import sys
from pathlib import Path

from openai.types.chat import (
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)

from beanhand.server.ai.config import Configuration
from beanhand.server.llm import (
    file_to_image_parts,
    ssl_verify_path,
    stream_reasoning_and_output,
)
from beanhand.structs import ProcessRequest

RECEIPT_CONVERSION_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "RECEIPT_CONVERSION_PROMPT.md"
)


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    from httpx import Client as HttpxClient
    from openai import OpenAI

    # The whole request arrives on stdin as a single plain-JSON object:
    # the account list plus the receipt's raw bytes, inline.
    try:
        preq = ProcessRequest.deserialize(sys.stdin.read())
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        sys.stdin.close()

    account_text = json.dumps(preq.accounts, indent=2)
    prompt_text = RECEIPT_CONVERSION_PROMPT_PATH.read_text()
    prompt_text = prompt_text.format(accounts=account_text)

    client = OpenAI(
        api_key=cfg.token,
        base_url=cfg.api_url,
        http_client=HttpxClient(verify=ssl_verify_path()),
    )

    text_part: ChatCompletionContentPartTextParam = {
        "type": "text",
        "text": prompt_text,
    }

    image_parts = file_to_image_parts(preq.receipt.filename, preq.receipt.content)

    messages: list[ChatCompletionMessageParam] = [
        {"role": "user", "content": [text_part, *image_parts]}
    ]

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
        "beanhand.Process",
        help="Process a receipt via LLM (receipt and accounts via stdin)",
    )
    return sp
