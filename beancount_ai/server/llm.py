"""Helpers to interact with LLMs."""

import base64
import json
import ssl
import sys
from pathlib import Path

from openai._streaming import Stream
from openai.types.chat import (
    ChatCompletionChunk,
    ChatCompletionContentPartImageParam,
)

from beancount_ai.server.pdf import render_pdf_pages_to_png

VALID_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png", ".pdf"))


def ssl_verify_path() -> str:
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
