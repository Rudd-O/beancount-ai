#!/usr/bin/env python3
"""Tests for the server-side account-list handling (dynamic wire format).

  * the beanai.Process stdin validator accepts a JSON array of {name, rule?}
    objects and rejects the legacy bare-string / malformed shapes (fail-stop,
    exit 1, `error:`-prefixed message on stderr);
  * the beanai.Refine ``accounts`` field is validated to the same shape and
    fails the same way;
  * the prompt filler renders the indent=2 JSON plus the field-explanation
    note, without losing or re-escaping account text beyond json.dumps.

All cases stop before (or capture at) the LLM client boundary, so no network
is touched; the LLM client is patched to raise a sentinel when reached, which
proves the input passed validation.
"""

import argparse
import io
import json
import pathlib
import sys
from typing import Any, cast
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

import beancount_ai.server.commands.process as proc_mod
import beancount_ai.server.commands.refine as refine_mod

_LLM_SENTINEL = "__llm_reached__"


def _fake_server_cfg() -> mock.MagicMock:
    cfg = mock.MagicMock()
    cfg.ai.token = "x"
    cfg.ai.api_url = "http://example.com/api"
    cfg.ai.model_name = "model"
    return cfg


# ===========================================================================
# beanai.Process stdin validator
# ===========================================================================


def _run_process(stdin_text: str) -> int | None:
    args = argparse.Namespace(filename="x.pdf".encode("utf-8").hex())
    with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)):
        try:
            proc_mod.run(_fake_server_cfg(), args)
            return None
        except SystemExit as e:
            return cast(int, e.code)


class TestProcessValidator:
    def test_accepts_array_of_objects(
        self, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        # A valid shape passes validation and proceeds to the receipt backend
        # (mocked to raise a sentinel — proof we got past the account check).
        good = json.dumps([{"name": "Expenses:Food"}, {"name": "Assets:Cash"}])
        with (
            mock.patch.object(
                proc_mod,
                "make_receipt_backend",
                side_effect=Exception("__backend__"),
            ),
            mock.patch.object(sys, "stdin", io.StringIO(good)),
        ):
            with pytest.raises(Exception, match="__backend__"):
                proc_mod.run(
                    _fake_server_cfg(),
                    argparse.Namespace(filename="x.pdf".encode("utf-8").hex()),
                )
        assert "invalid account list input" not in capsys.readouterr().err

    @pytest.mark.parametrize(
        "stdin",
        [
            json.dumps(["Expenses:Food"]),  # legacy bare-string array
            json.dumps({"name": "Expenses:Food"}),  # non-array top level
            json.dumps([{"rule": "x"}]),  # missing name
            json.dumps([{"name": 42}]),  # non-string name
            json.dumps([{"name": ""}]),  # empty name
            json.dumps([{"name": "A", "rule": 42}]),  # non-string rule
            json.dumps(["not-an-object"]),  # element is a bare string
            "this is not json",  # not JSON
        ],
    )
    def test_rejects_bad_shape(
        self, stdin: str, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        assert _run_process(stdin) == 1
        assert "error: invalid account list input" in capsys.readouterr().err


# ===========================================================================
# beanai.Refine accounts validator
# ===========================================================================

_TX = '2026-01-01 * "X"\n  Expenses:Food 1.00 CHF\n  Assets:Cash -1.00 CHF\n'


def _run_refine(stdin_text: str) -> int | None:
    with mock.patch.object(sys, "stdin", io.StringIO(stdin_text)):
        try:
            refine_mod.run(_fake_server_cfg(), mock.MagicMock())
            return None
        except SystemExit as e:
            return cast(int, e.code)


class TestRefineAccountsValidator:
    def test_accepts_array_of_objects(
        self, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        req: dict[str, Any] = {
            "transaction_text": _TX,
            "accounts": [{"name": "Expenses:Food"}],
        }
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(req))):
            try:
                # Validation passes; anything failing past the account check is
                # fine — we only assert the rejection path is NOT the one taken.
                refine_mod.run(_fake_server_cfg(), mock.MagicMock())
            except Exception:
                pass
        err = capsys.readouterr().err
        assert "account list missing or invalid" not in err

    @pytest.mark.parametrize(
        "accounts",
        [
            ["Expenses:Food"],  # legacy string element
            42,  # non-list
            [{"rule": "x"}],  # missing name
            [{"name": 42}],  # non-string name
            [{"name": "A", "rule": 42}],  # non-string rule
            ["a", {"name": "b"}],  # mixed
        ],
    )
    def test_rejects_bad_shape(
        self, accounts: Any, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        req: dict[str, Any] = {"transaction_text": _TX, "accounts": accounts}
        assert _run_refine(json.dumps(req)) == 1
        assert "account list missing or invalid" in capsys.readouterr().err

    def test_missing_accounts_rejected(
        self, capsys: "pytest.CaptureFixture[str]"
    ) -> None:
        assert _run_refine(json.dumps({"transaction_text": _TX})) == 1
        assert "account list missing or invalid" in capsys.readouterr().err


# ===========================================================================
# prompt filler: indent=2 JSON + field-explanation note
# ===========================================================================

_EXPLANATION_SNIPPET = "an object with a `name` (the account to use"


class TestPromptFiller:
    def test_process_prompt_renders_indent2_json_and_note(self) -> None:
        accounts = [
            {"name": "Expenses:Food:Groceries", "rule": 'say "hi"'},
            {"name": "Assets:Cash:CHF"},
        ]
        stdin_text = json.dumps(accounts)
        args = argparse.Namespace(filename="x.pdf".encode("utf-8").hex())
        captured: dict[str, str] = {}

        def _fake_client(**_kw: Any) -> Any:
            fake = mock.MagicMock()

            def _create(**c: Any) -> Any:
                captured["prompt"] = c["messages"][0]["content"][0]["text"]
                raise Exception(_LLM_SENTINEL)

            fake.chat.completions.create.side_effect = _create
            return fake

        backend = mock.MagicMock()
        backend.read.return_value = mock.MagicMock(data=b"img")
        with (
            mock.patch.object(proc_mod, "make_receipt_backend", return_value=backend),
            mock.patch.object(proc_mod, "file_to_image_parts", return_value=[]),
            mock.patch.object(proc_mod, "ssl_verify_path", return_value="/tmp/ca"),
            mock.patch.object(sys, "stdin", io.StringIO(stdin_text)),
            mock.patch("httpx.Client", mock.MagicMock),
        ):
            with mock.patch("openwebui_client.OpenWebUIClient", _fake_client):
                with pytest.raises(Exception, match=_LLM_SENTINEL):
                    proc_mod.run(_fake_server_cfg(), args)

        prompt = captured["prompt"]
        # indent=2 JSON of the object array is present verbatim.
        assert json.dumps(accounts, indent=2) in prompt
        # The field-explanation note is present above the fence.
        assert _EXPLANATION_SNIPPET in prompt
        # A quoted rule is preserved exactly as json.dumps renders it (no
        # further escaping or stripping server-side).
        assert '"rule": "say \\"hi\\""' in prompt
        # The unlisted-accounts guard remains.
        assert "Do not imagine accounts not listed" in prompt


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
