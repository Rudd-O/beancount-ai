#!/usr/bin/env python3
"""Command-level tests for the client-side receipts feature (Decision D4 cleanup).

These drive the real ``ingest`` / ``associate`` / ``organize`` / ``process``
commands against a temp Beancount file with the documents/AI clients faked, and
assert the post-success cleanup symmetry: a store receipt is ``Remove``d, a local
source file is moved into the account folder (original mtime preserved), and a
local receipt makes zero documents-server calls.

See ``docs/specs/Client-side receipts.md`` (W4).
"""

import argparse
import json
import os
import pathlib
import sys
from typing import IO, Any
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beanhand.client import commands
from beanhand.client.commands import associate, ingest, organize, process
from beanhand.client.config import BeancountConfiguration, Configuration
from beanhand.client.server import ai, documents
from beanhand.structs import FetchedReceipt, ProcessResponse

SAMPLE_TX = (
    '2025-03-18 * "Grocery Store" "Apples"\n'
    "  Assets:Checking    -5.00 USD\n"
    "  Expenses:Food        5.00 USD\n"
)

MARKED_LEDGER = (
    "2020-01-01 open Expenses:Food\n"
    '  beanhand-include: "recursively"\n'
    "2020-01-01 open Assets:Checking\n"
    '  beanhand-include: "recursively"\n'
)

ORIGINAL_MTIME = 1_650_000_000.25


def _make_config(folder: pathlib.Path) -> Configuration:
    main = folder / "main.bean"
    main.write_text(MARKED_LEDGER, encoding="utf-8")
    (folder / "imported.bean").write_text("", encoding="utf-8")
    bc = BeancountConfiguration(
        main_file=main, ingestion_destination_file=pathlib.Path("imported.bean")
    )
    cfg = Configuration.__new__(Configuration)
    cfg.documents_target_vm = None
    cfg.ai_target_vm = None
    cfg.beancount = bc
    return cfg


def _make_local_receipt(folder: pathlib.Path, name: str = "r.pdf") -> pathlib.Path:
    p = folder / name
    p.write_bytes(b"%PDF-1.4 local receipt body")
    os.utime(p, (ORIGINAL_MTIME, ORIGINAL_MTIME))
    return p


def _ai_mock() -> mock.MagicMock:
    return mock.MagicMock(
        process_receipt=mock.MagicMock(
            return_value=ProcessResponse(SAMPLE_TX, "Expenses:Food")
        )
    )


def _account_folder(cfg: Configuration) -> pathlib.Path:
    return cfg.beancount.main_folder / "Expenses" / "Food"


# ===========================================================================
# ingest: D4 cleanup
# ===========================================================================


def _ingest_args(filenames: list[str], *, yes: bool = True, no: bool = False) -> argparse.Namespace:
    return argparse.Namespace(filename=filenames, yes=yes, no=no)


class TestIngestLocal:
    def test_commit_moves_source(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)  # bare store names resolve to the store
        scan = tmp_path / "scan"
        scan.mkdir(exist_ok=True)
        src = _make_local_receipt(scan)

        fake_docs = mock.MagicMock()
        fake_ai = _ai_mock()
        with (
            mock.patch.object(documents.DocumentsClient, "from_cfg", return_value=fake_docs),
            mock.patch.object(ai.AIClient, "from_cfg", return_value=fake_ai),
        ):
            ingest.run(cfg, _ingest_args([str(src)]))

        # The source was moved: gone from its origin, present (with mtime) in the
        # account folder.
        assert not src.exists()
        filed = [p for p in _account_folder(cfg).iterdir() if p.is_file()]
        assert len(filed) == 1
        assert filed[0].stat().st_mtime == ORIGINAL_MTIME
        assert filed[0].read_bytes() == b"%PDF-1.4 local receipt body"

        # A local receipt makes zero documents-server calls of any kind.
        assert fake_docs.fetch_receipt.call_count == 0
        assert fake_docs.list_receipts.call_count == 0
        assert fake_docs.remove_receipt.call_count == 0

        # The transaction was appended to the ledger.
        assert "2025-03-18" in (tmp_path / "imported.bean").read_text(encoding="utf-8")

    def test_no_flag_leaves_local_source(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        src = _make_local_receipt(tmp_path, "local-no.pdf")
        original_bytes = src.read_bytes()

        fake_docs = mock.MagicMock()
        fake_ai = _ai_mock()
        with (
            mock.patch.object(documents.DocumentsClient, "from_cfg", return_value=fake_docs),
            mock.patch.object(ai.AIClient, "from_cfg", return_value=fake_ai),
        ):
            ingest.run(cfg, _ingest_args([str(src)], yes=False, no=True))

        # --no: no beancount write, local source untouched, nothing removed.
        assert (tmp_path / "imported.bean").read_text(encoding="utf-8") == ""
        assert src.exists()
        assert src.read_bytes() == original_bytes
        assert fake_docs.remove_receipt.call_count == 0
        assert fake_docs.fetch_receipt.call_count == 0

    def test_failed_unlink_rolls_back(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        src = _make_local_receipt(tmp_path, "locked.pdf")
        ledger_original = (tmp_path / "imported.bean").read_text(encoding="utf-8")

        real_unlink = pathlib.Path.unlink

        def flaky(self: pathlib.Path) -> None:
            if self == src:
                raise PermissionError("source locked")
            return real_unlink(self)  # pyright: ignore[reportUnknownVariableType]

        fake_docs = mock.MagicMock()
        fake_ai = _ai_mock()
        with (
            mock.patch.object(pathlib.Path, "unlink", flaky),
            mock.patch.object(documents.DocumentsClient, "from_cfg", return_value=fake_docs),
            mock.patch.object(ai.AIClient, "from_cfg", return_value=fake_ai),
        ):
            with pytest.raises(SystemExit) as exc:
                ingest.run(cfg, _ingest_args([str(src)]))

        assert exc.value.code == 1
        # Source survived the failed unlink, and the rollback removed the newly
        # filed copy and restored the ledger.
        assert src.exists()
        assert (tmp_path / "imported.bean").read_text(encoding="utf-8") == ledger_original
        assert list(_account_folder(cfg).iterdir()) == []
        assert fake_docs.remove_receipt.call_count == 0

    def test_source_inside_account_folder(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        acct = _account_folder(cfg)
        acct.mkdir(parents=True, exist_ok=True)
        src = acct / "raw.pdf"
        src.write_bytes(b"%PDF-1.4 in-folder receipt")
        os.utime(src, (ORIGINAL_MTIME, ORIGINAL_MTIME))

        fake_docs = mock.MagicMock()
        fake_ai = _ai_mock()
        with (
            mock.patch.object(documents.DocumentsClient, "from_cfg", return_value=fake_docs),
            mock.patch.object(ai.AIClient, "from_cfg", return_value=fake_ai),
        ):
            ingest.run(cfg, _ingest_args([str(src)]))

        # The source was unlinked; the copy got the date-prefixed name (strictly
        # longer than the source basename, so it can never coincide with source).
        assert not src.exists()
        filed = list(acct.iterdir())
        assert len(filed) == 1
        assert filed[0].name != "raw.pdf"
        assert filed[0].name.startswith("2025-03-18.")
        assert filed[0].name.endswith("raw.pdf")

    def test_same_file_twice_is_deduplicated(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        src = _make_local_receipt(tmp_path, "dup.pdf")

        fake_docs = mock.MagicMock()
        fake_ai = _ai_mock()
        with (
            mock.patch.object(documents.DocumentsClient, "from_cfg", return_value=fake_docs),
            mock.patch.object(ai.AIClient, "from_cfg", return_value=fake_ai),
        ):
            # The same file passed twice (bare name, then absolute path)
            # collapses to a single receipt.
            ingest.run(cfg, _ingest_args([src.name, str(src)]))

        # The file was moved once; exactly one copy filed; the transaction once.
        assert not src.exists()
        assert len([p for p in _account_folder(cfg).iterdir() if p.is_file()]) == 1
        ledger = (tmp_path / "imported.bean").read_text(encoding="utf-8")
        assert ledger.count('2025-03-18 * "Grocery Store"') == 1

    def test_store_name_twice_is_deduplicated(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        store_name = "dupstore.pdf"

        fake_docs = mock.MagicMock()
        fake_docs.fetch_receipt.return_value = FetchedReceipt(
            b"STOREBYTES", 123.0, store_name
        )
        fake_ai = _ai_mock()
        with (
            mock.patch.object(documents.DocumentsClient, "from_cfg", return_value=fake_docs),
            mock.patch.object(ai.AIClient, "from_cfg", return_value=fake_ai),
        ):
            ingest.run(cfg, _ingest_args([store_name, store_name]))

        # The duplicate store name was dropped: fetched and removed only once.
        assert fake_docs.fetch_receipt.call_count == 1
        assert fake_docs.remove_receipt.call_count == 1
        assert len([p for p in _account_folder(cfg).iterdir() if p.is_file()]) == 1

    def test_mixed_local_and_store_batch(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        src = _make_local_receipt(tmp_path, "mixlocal.pdf")
        store_name = "mixstore.pdf"

        fake_docs = mock.MagicMock()
        fake_docs.fetch_receipt.return_value = FetchedReceipt(
            b"STOREBYTES", 123.0, store_name
        )
        fake_ai = _ai_mock()
        with (
            mock.patch.object(documents.DocumentsClient, "from_cfg", return_value=fake_docs),
            mock.patch.object(ai.AIClient, "from_cfg", return_value=fake_ai),
        ):
            ingest.run(cfg, _ingest_args([str(src), store_name]))

        # Each receipt cleaned up from its own source: local moved, store removed.
        assert not src.exists()
        fake_docs.remove_receipt.assert_called_once_with(store_name)
        # Only the store receipt was fetched (the local one is read directly).
        assert fake_docs.fetch_receipt.call_count == 1
        assert fake_docs.fetch_receipt.call_args[0][0] == store_name
        # Two transactions, two filed copies.
        ledger = (tmp_path / "imported.bean").read_text(encoding="utf-8")
        assert ledger.count('2025-03-18 * "Grocery Store"') == 2
        assert len([p for p in _account_folder(cfg).iterdir() if p.is_file()]) == 2


class TestIngestStore:
    def test_store_commit_removes_and_files(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)  # ensure a bare store name is not a local file
        store_name = "storeonly.pdf"

        fake_docs = mock.MagicMock()
        fake_docs.fetch_receipt.return_value = FetchedReceipt(
            b"STOREBYTES", 123.0, store_name
        )
        fake_ai = _ai_mock()
        with (
            mock.patch.object(documents.DocumentsClient, "from_cfg", return_value=fake_docs),
            mock.patch.object(ai.AIClient, "from_cfg", return_value=fake_ai),
        ):
            ingest.run(cfg, _ingest_args([store_name]))

        fake_docs.fetch_receipt.assert_called_once_with(store_name)
        fake_docs.remove_receipt.assert_called_once_with(store_name)
        filed = [p for p in _account_folder(cfg).iterdir() if p.is_file()]
        assert len(filed) == 1
        assert filed[0].read_bytes() == b"STOREBYTES"

    def test_no_flag_store_no_remove_no_write(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        store_name = "storeonly.pdf"

        fake_docs = mock.MagicMock()
        fake_docs.fetch_receipt.return_value = FetchedReceipt(
            b"STOREBYTES", 123.0, store_name
        )
        fake_ai = _ai_mock()
        with (
            mock.patch.object(documents.DocumentsClient, "from_cfg", return_value=fake_docs),
            mock.patch.object(ai.AIClient, "from_cfg", return_value=fake_ai),
        ):
            ingest.run(cfg, _ingest_args([store_name], yes=False, no=True))

        fake_docs.fetch_receipt.assert_called_once_with(store_name)
        fake_docs.remove_receipt.assert_not_called()
        assert (tmp_path / "imported.bean").read_text(encoding="utf-8") == ""


# ===========================================================================
# organize / process: local touches no documents client; store does one fetch
# ===========================================================================


class TestOrganize:
    def test_local_makes_no_documents_server_calls(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        src = _make_local_receipt(tmp_path, "org.pdf")

        with mock.patch.object(
            documents.DocumentsClient, "from_cfg"
        ) as from_cfg_mock:
            organize.run(
                cfg,
                argparse.Namespace(
                    filename=str(src), date="2025-03-18", account="Expenses:Food"
                ),
            )

        # The client is constructed (shared code path) but a local receipt makes
        # no documents-server calls of any kind.
        from_cfg_mock.assert_called_once()
        from_cfg_mock.return_value.fetch_receipt.assert_not_called()
        from_cfg_mock.return_value.list_receipts.assert_not_called()
        from_cfg_mock.return_value.remove_receipt.assert_not_called()
        filed = list(_account_folder(cfg).iterdir())
        assert len(filed) == 1
        assert filed[0].read_bytes() == src.read_bytes()

    def test_store_fetches_once(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        store_name = "orgstore.pdf"
        fake_docs = mock.MagicMock()
        fake_docs.fetch_receipt.return_value = FetchedReceipt(b"x", 1.0, store_name)

        with mock.patch.object(
            documents.DocumentsClient, "from_cfg", return_value=fake_docs
        ) as from_cfg_mock:
            organize.run(
                cfg,
                argparse.Namespace(
                    filename=store_name, date="2025-03-18", account="Expenses:Food"
                ),
            )

        from_cfg_mock.assert_called_once()
        fake_docs.fetch_receipt.assert_called_once_with(store_name)
        fake_docs.remove_receipt.assert_not_called()


class TestProcess:
    def _args(self, filename: str) -> argparse.Namespace:
        return argparse.Namespace(filename=filename)

    def test_local_makes_no_documents_server_calls(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        src = _make_local_receipt(tmp_path, "proc.pdf")

        with (
            mock.patch.object(documents.DocumentsClient, "from_cfg") as from_cfg_mock,
            mock.patch.object(ai.AIClient, "from_cfg", return_value=_ai_mock()),
        ):
            process.run(cfg, self._args(str(src)))

        # The client is constructed (shared code path) but a local receipt makes
        # no documents-server calls of any kind.
        from_cfg_mock.assert_called_once()
        from_cfg_mock.return_value.fetch_receipt.assert_not_called()
        from_cfg_mock.return_value.list_receipts.assert_not_called()
        from_cfg_mock.return_value.remove_receipt.assert_not_called()

    def test_store_fetches_once(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _make_config(tmp_path)
        monkeypatch.chdir(tmp_path)
        store_name = "procstore.pdf"
        fake_docs = mock.MagicMock()
        fake_docs.fetch_receipt.return_value = FetchedReceipt(b"x", 1.0, store_name)

        with (
            mock.patch.object(
                documents.DocumentsClient, "from_cfg", return_value=fake_docs
            ) as from_cfg_mock,
            mock.patch.object(ai.AIClient, "from_cfg", return_value=_ai_mock()),
        ):
            process.run(cfg, self._args(store_name))

        from_cfg_mock.assert_called_once()
        fake_docs.fetch_receipt.assert_called_once_with(store_name)
        fake_docs.remove_receipt.assert_not_called()


# ===========================================================================
# associate: D4 cleanup for confirmed matches
# ===========================================================================


def _associate_jsonl(receipt_info: str, match_info: str) -> bytes:
    """Two JSONL passes (receipt-info, then match-results), each terminated."""
    def stream(lines: list[str]) -> str:
        out: list[str] = []
        for chunk in lines:
            out.append(json.dumps({"output": chunk}))
        out.append(json.dumps({"finish": "stop"}))
        return "\n".join(out) + "\n"

    return (stream([receipt_info]) + stream([match_info])).encode("utf-8")


def _ledger_with_tx(folder: pathlib.Path) -> None:
    # MARKED_LEDGER is four lines; the transaction header therefore lands on
    # line 5 (matching the candidate's line_no below).
    (folder / "main.bean").write_text(
        MARKED_LEDGER
        + '2025-03-18 * "Grocery Store" "Apples"\n'
        "  Assets:Checking    -5.00 USD\n"
        "  Expenses:Food        5.00 USD\n",
        encoding="utf-8",
    )


def _associate_cfg(folder: pathlib.Path) -> Configuration:
    cfg = _make_config(folder)
    # Overwrite the ledger with one that carries a transaction (header on line 5).
    _ledger_with_tx(folder)
    return cfg


_MAIN_FILE: str | None = None


class TestAssociate:
    def _patch_ai(self, monkeypatch: "pytest.MonkeyPatch") -> mock.MagicMock:
        from beanhand.client.beancount_loader import CandidateContext

        main_file = _MAIN_FILE
        assert main_file is not None

        # A single, known candidate.  The source file must be the config's main
        # ledger (already written by _associate_cfg with the transaction on line
        # 5) so the real associate flow can read it and update its metadata.
        ctx = CandidateContext(
            date_str="2025-03-18",
            payee="Grocery Store",
            narration="Apples",
            paid_amount=5.0,
            paid_currency="USD",
            crediting_account="Expenses:Food",
            source_file=main_file,
            line_no=5,
            transaction_text="tx",
        )
        monkeypatch.setattr(
            associate,
            "load_transaction_contexts",
            lambda *a, **k: ([None], [ctx]),
        )

        def _fake_help_associate(
            filename: str, fetched: FetchedReceipt
        ) -> tuple[list[str], mock.MagicMock, IO[bytes], IO[bytes]]:
            import io

            match = json.dumps(
                {
                    "matches": [
                        {
                            "line_no": 5,
                            "source_file": main_file,
                            "score": 0.99,
                            "reason": "x",
                        }
                    ],
                    "ambiguous": False,
                }
            )
            receipt_info = json.dumps({"date": "2025-03-18", "amount": "5.00 USD"})
            stdout = io.BytesIO(_associate_jsonl(receipt_info, match))
            proc = mock.MagicMock()
            proc.wait.return_value = 0
            return (["cmd"], proc, io.BytesIO(), stdout)

        fake_ai = mock.MagicMock()
        # fake_ai is a mock (not a real AIClient), so set the method on the
        # instance itself.
        fake_ai.help_associate_receipt.side_effect = _fake_help_associate
        fake_ai.process_receipt.return_value = ProcessResponse(SAMPLE_TX, "Expenses:Food")
        return fake_ai

    def test_local_match_moves_source_no_remove(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _associate_cfg(tmp_path)
        global _MAIN_FILE
        _MAIN_FILE = str(tmp_path / "main.bean")
        monkeypatch.chdir(tmp_path)
        src = _make_local_receipt(tmp_path, "assoc-local.pdf")

        fake_ai = self._patch_ai(monkeypatch)
        fake_docs = mock.MagicMock()
        with (
            mock.patch.object(documents.DocumentsClient, "from_cfg", return_value=fake_docs),
            mock.patch.object(ai.AIClient, "from_cfg", return_value=fake_ai),
        ):
            associate.run(
                cfg, argparse.Namespace(filename=[str(src)], yes=True, no=False)
            )

        assert not src.exists()
        fake_docs.remove_receipt.assert_not_called()
        filed = [p for p in _account_folder(cfg).iterdir() if p.is_file()]
        assert len(filed) == 1

    def test_store_match_removes_store(
        self, tmp_path: pathlib.Path, monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        cfg = _associate_cfg(tmp_path)
        global _MAIN_FILE
        _MAIN_FILE = str(tmp_path / "main.bean")
        monkeypatch.chdir(tmp_path)
        store_name = "assoc-store.pdf"

        fake_ai = self._patch_ai(monkeypatch)
        fake_docs = mock.MagicMock()
        fake_docs.fetch_receipt.return_value = FetchedReceipt(b"S", 1.0, store_name)
        with (
            mock.patch.object(documents.DocumentsClient, "from_cfg", return_value=fake_docs),
            mock.patch.object(ai.AIClient, "from_cfg", return_value=fake_ai),
        ):
            associate.run(
                cfg, argparse.Namespace(filename=[store_name], yes=True, no=False)
            )

        fake_docs.fetch_receipt.assert_called_once_with(store_name)
        fake_docs.remove_receipt.assert_called_once_with(store_name)
        filed = [p for p in _account_folder(cfg).iterdir() if p.is_file()]
        assert len(filed) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
