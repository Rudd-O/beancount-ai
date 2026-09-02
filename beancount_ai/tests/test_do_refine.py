#!/usr/bin/env python3
"""Integration-style tests for the client's do_refine (LLM/VM interactions mocked)."""

import io
import json
import pathlib
import sys
from typing import Any
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.client import cli as client_cli
from beancount_ai.client.cli import do_refine
from beancount_ai.client.config import BeancountConfiguration, Configuration

ORIGINAL_BLOCK = (
    '2026-03-15 * "Coop" "Groceries"\n'
    '  document: "receipts/2026-03-15.coop.pdf"\n'
    "  Expenses:Current:Food    45.00 CHF\n"
    "  Assets:Cash:CHF        -45.00 CHF\n"
)

REFINED_BLOCK = (
    '2026-03-15 ! "Coop Supermarket" "Groceries"\n'
    '  document: "receipts/2026-03-15.coop.pdf"\n'
    "  Expenses:Current:Food:Groceries    38.25 CHF\n"
    "  Expenses:Current:Food:Snacks       12.75 CHF\n"
    "  Assets:Cash:CHF                  -58.25 CHF\n"
)


def _make_config(folder: pathlib.Path) -> Configuration:
    main = folder / "main.bean"
    main.write_text(
        "; header comment above tx\n"
        + ORIGINAL_BLOCK
        + "\n"
        '2026-04-01 * "Other" "Unchanged"\n'
        "  Expenses:Other   1.00 CHF\n"
    )
    (folder / "accounts.txt").write_text("Expenses:Current:Food\nAssets:Cash:CHF\n")
    (folder / "receipts").mkdir()
    (folder / "receipts" / "2026-03-15.coop.pdf").write_bytes(b"%PDF-1.4 fake")

    bc = BeancountConfiguration()
    bc.main_file = main
    bc.ingestion_destination_file = None
    bc.account_list_file = folder / "accounts.txt"
    cfg = object.__new__(Configuration)
    cfg.target_vm = None
    cfg.beancount = bc
    return cfg


def _jsonl_stream(llm_output: str) -> str:
    """Emit the JSONL protocol for a single-output LLM response."""
    payload = (
        {"reasoning": "thinking..."},
        {"output": llm_output},
        {"finish": "stop"},
    )
    return "\n".join(json.dumps(p) for p in payload) + "\n"


class _StdinCapture(io.BytesIO):
    """A BytesIO that records the JSON payload written into it."""

    def __init__(self) -> None:
        super().__init__()
        self.payload: dict[str, Any] | None = None

    def write(self, b: bytes) -> int:  # type: ignore[override]
        self.payload = json.loads(b.decode("utf-8"))
        return super().write(b)


@pytest.fixture
def fake_call(monkeypatch: "pytest.MonkeyPatch") -> dict[str, Any]:
    """Patched RemoteVM._call emitting a canned LLM output. Returns a control dict."""
    state: dict[str, Any] = {
        "llm_output": "",
        "proc": mock.MagicMock(),
    }
    state["proc"].wait.return_value = 0

    def _fake_call(
        self: object, action: str, arg: str | None = None
    ) -> tuple[list[str], mock.MagicMock, _StdinCapture, io.BytesIO]:
        assert action == "beanai.Refine", action
        assert arg is None  # refine carries no CLI argument
        stdin = _StdinCapture()
        state["stdin"] = stdin  # do_refine writes the payload into it
        stdout = io.BytesIO(_jsonl_stream(state["llm_output"]).encode("utf-8"))
        return (["cmd"], state["proc"], stdin, stdout)

    monkeypatch.setattr(client_cli.RemoteVM, "_call", _fake_call)
    return state


def test_do_refine_writes_refined_block(tmp_path: pathlib.Path, fake_call: dict[str, Any]) -> None:
    cfg = _make_config(tmp_path)
    fake_call["llm_output"] = json.dumps(
        {"transaction": REFINED_BLOCK, "changes_summary": "split expenses"}
    )
    args = mock.Mock(file_path=str(tmp_path / "main.bean"), line_number=2, yes=True, no=False)

    do_refine(cfg, args)

    new_content = (tmp_path / "main.bean").read_text(encoding="utf-8")
    assert '2026-03-15 ! "Coop Supermarket"' in new_content
    assert "Expenses:Current:Food:Snacks" in new_content
    # The other transaction must be untouched.
    assert '2026-04-01 * "Other"' in new_content
    # The header comment above the refined transaction is preserved.
    assert new_content.startswith("; header comment above tx\n")

    # The server received a well-formed refine request.
    payload = fake_call["stdin"].payload
    assert payload is not None
    assert payload["transaction_text"] == ORIGINAL_BLOCK
    assert payload["accounts"] == ["Expenses:Current:Food", "Assets:Cash:CHF"]
    assert len(payload["documents"]) == 1
    assert payload["documents"][0]["filepath"] == "receipts/2026-03-15.coop.pdf"
    assert payload["documents"][0]["data"]  # base64, non-empty


def test_do_refine_strips_leading_llm_comments(tmp_path: pathlib.Path, fake_call: dict[str, Any]) -> None:
    cfg = _make_config(tmp_path)
    fake_call["llm_output"] = json.dumps(
        {
            "transaction": (
                "; this is reasoning\n"
                "; more reasoning\n"
                + REFINED_BLOCK
            )
        }
    )
    args = mock.Mock(file_path=str(tmp_path / "main.bean"), line_number=2, yes=True, no=False)
    do_refine(cfg, args)
    new_content = (tmp_path / "main.bean").read_text(encoding="utf-8")
    assert "; this is reasoning" not in new_content
    assert "more reasoning" not in new_content


def test_do_refine_no_flag_does_not_write(
    tmp_path: pathlib.Path, fake_call: dict[str, Any], capsys: "pytest.CaptureFixture[str]"
) -> None:
    cfg = _make_config(tmp_path)
    fake_call["llm_output"] = json.dumps({"transaction": REFINED_BLOCK})
    original = (tmp_path / "main.bean").read_text(encoding="utf-8")
    args = mock.Mock(file_path=str(tmp_path / "main.bean"), line_number=2, yes=False, no=True)
    do_refine(cfg, args)
    assert (tmp_path / "main.bean").read_text(encoding="utf-8") == original
    assert "--no requested" in capsys.readouterr().err


def test_do_refine_missing_file(
    tmp_path: pathlib.Path, capsys: "pytest.CaptureFixture[str]"
) -> None:
    cfg = _make_config(tmp_path)
    args = mock.Mock(file_path=str(tmp_path / "nope.bean"), line_number=1, yes=True, no=False)
    with pytest.raises(SystemExit):
        do_refine(cfg, args)
    assert "file not found" in capsys.readouterr().err


def test_do_refine_line_not_in_transaction(
    tmp_path: pathlib.Path, capsys: "pytest.CaptureFixture[str]"
) -> None:
    cfg = _make_config(tmp_path)
    # Line 1 is the comment header, not part of any transaction.
    args = mock.Mock(file_path=str(tmp_path / "main.bean"), line_number=1, yes=True, no=False)
    with pytest.raises(SystemExit):
        do_refine(cfg, args)
    assert "Error" in capsys.readouterr().err


def test_do_refine_malformed_llm_output(
    tmp_path: pathlib.Path, fake_call: dict[str, Any], capsys: "pytest.CaptureFixture[str]"
) -> None:
    cfg = _make_config(tmp_path)
    fake_call["llm_output"] = "not json at all"
    args = mock.Mock(file_path=str(tmp_path / "main.bean"), line_number=2, yes=True, no=False)
    with pytest.raises(SystemExit):
        do_refine(cfg, args)
    assert "could not parse LLM response" in capsys.readouterr().err


def test_do_refine_interactive_no(tmp_path: pathlib.Path, fake_call: dict[str, Any]) -> None:
    """Answering 'n' at the prompt leaves the file untouched and returns normally."""
    cfg = _make_config(tmp_path)
    fake_call["llm_output"] = json.dumps({"transaction": REFINED_BLOCK})
    original = (tmp_path / "main.bean").read_text(encoding="utf-8")
    args = mock.Mock(file_path=str(tmp_path / "main.bean"), line_number=2, yes=False, no=False)
    with mock.patch.object(client_cli, "input", side_effect=["n"]):
        do_refine(cfg, args)
    assert (tmp_path / "main.bean").read_text(encoding="utf-8") == original


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
