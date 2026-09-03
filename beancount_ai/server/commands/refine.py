import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any, cast

from openai._streaming import Stream
from openai.types.chat import (
    ChatCompletionChunk,
    ChatCompletionContentPartImageParam,
    ChatCompletionContentPartTextParam,
    ChatCompletionMessageParam,
)

from beancount_ai.server.config import Configuration
from beancount_ai.server.llm import (
    VALID_EXTENSIONS,
    file_to_image_parts,
    ssl_verify_path,
    stream_reasoning_and_output,
)
from beancount_ai.structs import RefineRequest

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
    from openwebui_client import OpenWebUIClient

    request_data_pre = json.loads(sys.stdin.read())
    if (
        not isinstance(request_data_pre, dict)
        or "transaction_text" not in request_data_pre
        or not isinstance(request_data_pre["transaction_text"], str)
        or not request_data_pre["transaction_text"].strip()
    ):
        print("error: Invalid request: missing transaction_text", file=sys.stderr)
        sys.exit(1)

    request_data: dict[Any, Any] = request_data_pre  # pyright: ignore[reportUnknownVariableType]
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
        or not all(isinstance(acc, str) for acc in request_data["accounts"])  # pyright: ignore[reportUnknownVariableType]
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
        if suffix not in VALID_EXTENSIONS:
            print(
                f"warning: unsupported document format, skipping: {suffix}",
                file=sys.stderr,
            )
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
        model=cfg.ai.model_name,
        messages=messages,
        stream=True,
    )

    stream_reasoning_and_output(cast(Stream[ChatCompletionChunk], resp))


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    sp.add_parser(
        "beanai.Refine",
        help="Refine an existing transaction using linked documents (request via stdin)",
    )
    return sp
