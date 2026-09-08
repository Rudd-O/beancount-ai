#!/usr/bin/env python3
"""Tests for the AI server's beanhand.HelpAssociateReceipt two-line stdin protocol.

The receipt arrives on the first stdin line (inlined, base64-encoded); the
candidate transactions arrive on the second stdin line, after the first LLM
pass has completed.  The first LLM prompt (RECEIPT_INFO_PROMPT) must be sent
before reading the second line; the second (RECEIPT_MATCH_PROMPT) must embed
the candidates.
"""

import argparse
import base64
import io
import json
import pathlib
import sys
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beanhand.server.ai.commands import associate as assoc_mod


def _fake_server_cfg() -> mock.MagicMock:
    cfg = mock.MagicMock()
    cfg.ai.token = "x"
    cfg.ai.api_url = "http://example.com/api"
    cfg.ai.model_name = "model"
    return cfg


class _FakeStdin:
    """A stdin stand-in whose readline() serves lines one at a time."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def readline(self) -> str:
        if not self._lines:
            return ""
        return self._lines.pop(0)

    def close(self) -> None:
        pass


def _fake_stream(text: str) -> mock.MagicMock:
    """A minimal Stream[ChatCompletionChunk] stand-in for stream_reasoning_and_output."""
    chunk = mock.MagicMock()
    chunk.choices = [mock.MagicMock()]
    chunk.choices[0].delta.reasoning_content = None
    chunk.choices[0].delta.content = text
    chunk.choices[0].finish_reason = None
    chunk2 = mock.MagicMock()
    chunk2.choices = [mock.MagicMock()]
    chunk2.choices[0].delta.reasoning_content = None
    chunk2.choices[0].delta.content = None
    chunk2.choices[0].finish_reason = "stop"
    return mock.MagicMock(__iter__=lambda self: iter([chunk, chunk2]))


class TestAssociateProtocol:
    def test_receipt_then_candidates_two_stage(
        self, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        receipt_line = json.dumps(
            {
                "receipt": {
                    "filename": "r.jpg",
                    "content": base64.b64encode(b"img").decode("ascii"),
                }
            }
        )
        candidates = [{"line_no": 12, "source_file": "f.bean"}]
        candidates_line = json.dumps(candidates)

        prompts: list[str] = []

        def _fake_client(**_kw: Any) -> Any:
            fake = mock.MagicMock()

            def _create(**c: Any) -> Any:
                prompts.append(c["messages"][0]["content"][0]["text"])
                return _fake_stream("{}")

            fake.chat.completions.create.side_effect = _create
            return fake

        with (
            mock.patch.object(assoc_mod, "file_to_image_parts", return_value=[]),
            mock.patch.object(assoc_mod, "ssl_verify_path", return_value="/tmp/ca"),
            mock.patch.object(
                sys, "stdin", _FakeStdin([receipt_line, candidates_line])
            ),
            mock.patch("httpx.Client", mock.MagicMock),
            mock.patch("openai.OpenAI", _fake_client),
        ):
            assoc_mod.run(_fake_server_cfg(), argparse.Namespace())

        # Two LLM passes happened, in order.
        assert len(prompts) == 2
        # The match prompt embeds the candidate list (re-serialized).
        assert json.dumps(candidates) in prompts[1]
        # The info prompt ran first (it is formatted from the filename).
        assert "r.jpg" in prompts[0].replace("`", "")
        # Both passes streamed their (fake) outputs to stdout as JSONL.
        out = capsys.readouterr().out
        assert out.count('{"output": "{}"}') == 2
        assert out.count('{"finish": "stop"}') == 2

    def test_receipt_line_must_come_first(
        self, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        # Garbage on the first line fails before any LLM call.
        with (
            mock.patch.object(sys, "stdin", _FakeStdin(["not json at all", "[]"])),
            mock.patch("openai.OpenAI", side_effect=RuntimeError("reached LLM")),
        ):
            with pytest.raises(SystemExit) as exc:
                assoc_mod.run(_fake_server_cfg(), argparse.Namespace())
        assert exc.value.code == 1
        assert "error: invalid receipt input" in capsys.readouterr().err

    def test_missing_receipt_rejected(
        self, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        with (
            mock.patch.object(sys, "stdin", _FakeStdin([json.dumps({"x": 1}), "[]"])),
            mock.patch("openai.OpenAI", side_effect=RuntimeError("reached LLM")),
        ):
            with pytest.raises(SystemExit) as exc:
                assoc_mod.run(_fake_server_cfg(), argparse.Namespace())
        assert exc.value.code == 1
        assert "error: invalid receipt input" in capsys.readouterr().err

    def test_candidates_must_be_array(
        self, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        receipt_line = json.dumps(
            {
                "receipt": {
                    "filename": "r.jpg",
                    "content": base64.b64encode(b"img").decode("ascii"),
                }
            }
        )
        with (
            mock.patch.object(assoc_mod, "file_to_image_parts", return_value=[]),
            mock.patch.object(assoc_mod, "ssl_verify_path", return_value="/tmp/ca"),
            mock.patch.object(
                sys, "stdin", _FakeStdin([receipt_line, json.dumps({"not": "a list"})])
            ),
            mock.patch("httpx.Client", mock.MagicMock),
            mock.patch(
                "openai.OpenAI",
                lambda **_kw: mock.MagicMock(
                    chat=mock.MagicMock(
                        completions=mock.MagicMock(
                            create=lambda **_c: _fake_stream("{}")
                        )
                    )
                ),
            ),
        ):
            with pytest.raises(SystemExit) as exc:
                assoc_mod.run(_fake_server_cfg(), argparse.Namespace())
        assert exc.value.code == 1
        assert "candidates must be a JSON array" in capsys.readouterr().err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
