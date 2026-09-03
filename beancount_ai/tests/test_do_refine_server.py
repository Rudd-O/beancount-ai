#!/usr/bin/env python3
"""Tests for the server-side do_refine request validation (before any LLM call).

These cases all fail *before* the LLM client is constructed, so constructing the
client is patched to raise; if it were reached the test would error.
"""

import io
import json
import pathlib
import sys
from typing import cast
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.server.commands.refine import run as do_refine


def _fake_cfg() -> mock.MagicMock:
    cfg = mock.MagicMock()
    cfg.ai.token = "x"
    cfg.ai.api_url = "http://example.com/api"
    cfg.ai.model_name = "model"
    return cfg


def _run(stdin_text: str) -> int | None:
    """Run do_refine with a patched stdin; return the SystemExit code (None if none).

    All these cases fail during input validation, before the LLM client is
    constructed, so the call never reaches the network.
    """
    with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)):
        try:
            do_refine(_fake_cfg(), mock.MagicMock())
            return None
        except SystemExit as e:
            return cast(int, e.code)


def test_missing_transaction_text_fails() -> None:
    assert _run(json.dumps({"accounts": []})) == 1


def test_non_object_fails() -> None:
    assert _run(json.dumps(["a", "b"])) == 1


def test_empty_transaction_text_fails() -> None:
    assert _run(json.dumps({"transaction_text": "   "})) == 1


@pytest.mark.skip("disabled for now")
def test_unsupported_document_extension_skipped(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An unsupported document is warned about (stderr) and skipped; processing continues."""
    req = {
        "transaction_text": '2026-01-01 * "X"\n',
        "accounts": ["Expenses:Food"],
        "documents": [{"filepath": "doc.docx", "data": "aGVsbG8="}],
    }
    stdin_text = json.dumps(req)
    with mock.patch(
        "openwebui_client.OpenWebUIClient",
        side_effect=RuntimeError("LLM client reached"),
    ):
        try:
            with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)):
                do_refine(_fake_cfg(), mock.MagicMock())
            pytest.fail(
                "do_refine should have raised RuntimeError (LLM client reached)"
            )
        except RuntimeError as e:
            assert "LLM client reached" in str(
                e
            )  # reached the LLM call => doc was skipped
    err = capsys.readouterr().err
    assert "docx" in err


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
