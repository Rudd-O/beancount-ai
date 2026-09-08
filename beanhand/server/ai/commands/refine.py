import argparse
import json
import sys
from pathlib import Path

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
from beanhand.structs import (
    VALID_EXTENSIONS,
    RefineRequest,
)

TRANSACTION_REFINEMENT_PROMPT_PATH = (
    Path(__file__).resolve().parent.parent / "TRANSACTION_REFINEMENT_PROMPT.md"
)


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    """Refine an existing Beancount transaction using its linked documents.

    The command carries no CLI argument.  The whole request arrives on stdin as a
    single plain-JSON object (``transaction_text``, ``accounts``, ``documents``);
    each document's raw bytes are base64-encoded.  The LLM produces a rewritten
    Beancount transaction, streamed back as JSONL like the other handlers.  The
    original transaction block is preserved verbatim in the prompt and only
    posting-level content may be refined.
    """
    from httpx import Client as HttpxClient
    from openai import OpenAI

    try:
        request = RefineRequest.deserialize(sys.stdin.read())
    except Exception as e:
        print(f"error while reading request from client: {e}", file=sys.stderr)
        sys.exit(1)

    image_parts: list[ChatCompletionContentPartImageParam] = []
    for doc in request.documents:
        fn = Path(doc.filename)
        suffix = fn.suffix.lower()
        if suffix not in VALID_EXTENSIONS:
            print(
                f"warning: unsupported document format, skipping: {suffix}",
                file=sys.stderr,
            )
            continue
        image_parts.extend(file_to_image_parts(doc.filename, doc.content))

    account_text = json.dumps(request.accounts, indent=2)
    prompt_text = TRANSACTION_REFINEMENT_PROMPT_PATH.read_text()
    prompt_text = prompt_text.format(
        transaction_text=request.transaction_text, accounts=account_text
    )

    client = OpenAI(
        api_key=cfg.token,
        base_url=cfg.api_url,
        http_client=HttpxClient(verify=ssl_verify_path()),
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


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    sp.add_parser(
        "beanhand.Refine",
        help="Refine an existing transaction using linked documents (request via stdin)",
    )
    return sp
