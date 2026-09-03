#!/usr/bin/env python3
"""Unit tests for the ImportResult class.

All LLM/VM interactions are mocked; real filesystem operations go through pytest tmp_path.
"""

import os
import pathlib
import sys
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.client.commands.importcmd import ImportResult
from beancount_ai.client.config import BeancountConfiguration

# ===========================================================================
# Sample data used by the majority of tests.
# ===========================================================================

SAMPLE_TX_TEXT: str = (
    '2025-03-18 * "Grocery Store" "Apples"\n'
    "  Assets:Checking    -5.00 USD\n"
    "  Expenses:Food        5.00 USD\n"
)

SAMPLE_RECEIPT_DATA: bytes = b"%PDF-1.4 fake receipt content\r\n"


# ===========================================================================
# Helpers
# ===========================================================================


def _make_config(
    folder: pathlib.Path, *, tx_in_ledger: str = ""
) -> BeancountConfiguration:
    """Create a minimal *BeancountConfiguration* backed by *folder*.

    Parameters
    ----------
    folder:
        A pre-created tmp directory used for all files.
    tx_in_ledger:
        Optional content to write into ``imported.bean`` (the ingestion file).
    """
    main = folder / "main.bean"
    with open(main, "w") as fh:
        if tx_in_ledger:
            fh.write(tx_in_ledger)
    (folder / "imported.bean").write_text("")
    (folder / "accounts.txt").write_text("Expenses:Food\nAssets:Checking\n")

    bc = BeancountConfiguration()
    bc.main_file = main
    bc.ingestion_destination_file = pathlib.Path("imported.bean")
    bc.account_list_file = folder / "accounts.txt"
    return bc


@pytest.fixture
def bc(tmp_path: pathlib.Path) -> BeancountConfiguration:
    return _make_config(tmp_path)


def _make_vm(
    receipt_content: bytes = SAMPLE_RECEIPT_DATA,
    tx: str | None = None,
) -> mock.MagicMock:
    """Return a mocked ``RemoteVM``.

    By default the LLM returns *SAMPLE_TX_TEXT* with **no** markdown fence, so
    tests get a clean transaction and do not need to re-implement comment-stripping.
    """
    vm = mock.MagicMock()
    vm.fetch_receipt.return_value = receipt_content
    if tx is None:
        tx = SAMPLE_TX_TEXT
    vm.process_receipt.return_value = (tx, "Expenses:Food")
    return vm


# ===========================================================================
# _formatted_transaction_text
# ===========================================================================


class TestFormattedTransactionText:
    @pytest.fixture(autouse=True)
    def _build_result(self, bc: BeancountConfiguration) -> None:
        result = ImportResult(_make_vm(), bc, "test-001.pdf")
        self.result: ImportResult = result

    def test_with_trailing_newline(self) -> None:
        output: str = self.result._formatted_transaction_text("\n")  # pyright: ignore[reportPrivateUsage]
        expected: str = "\n" + self.result.transaction_text.strip() + "\n"
        assert output == expected

    def test_without_trailing_newline(self) -> None:
        output: str = self.result._formatted_transaction_text("X")  # pyright: ignore[reportPrivateUsage]
        expected: str = "\n\n" + self.result.transaction_text.strip() + "\n"
        assert output == expected


# ===========================================================================
# __init__
# ===========================================================================


class TestInitCalls:
    def test_fetches_receipt(self, bc: BeancountConfiguration) -> None:
        vm: mock.MagicMock = _make_vm(receipt_content=SAMPLE_RECEIPT_DATA)

        ImportResult(vm, bc, "invoice-92.pdf")

        vm.fetch_receipt.assert_called_once_with("invoice-92.pdf")

    def test_passes_account_list(self, bc: BeancountConfiguration) -> None:
        vm: mock.MagicMock = _make_vm()

        ImportResult(vm, bc, "any.pdf")

        accounts_arg: list[str] = vm.process_receipt.call_args[0][1]
        assert "Expenses:Food" in accounts_arg


class TestInitStripping:
    def test_strips_leading_comment_lines(self, bc: BeancountConfiguration) -> None:
        """Leading ``;  ...`` reasoning lines are stripped before date parsing."""
        commented: str = "; reasoning\n\n   \n" + SAMPLE_TX_TEXT
        vm: mock.MagicMock = _make_vm(tx=commented)

        result: ImportResult = ImportResult(vm, bc, "test.pdf")
        assert result.transaction_text.lstrip().startswith("2025-03-18")


class TestInitMetadata:
    def test_injects_document_directive(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        result: ImportResult = ImportResult(_make_vm(), bc, "test.pdf")

        assert "document:" in result.transaction_text
        expected_part: str = str(tmp_path / "Expenses" / "Food")
        assert expected_part in result.receipt_destination_path.as_posix()


class TestInitAttributes:
    def test_all_attributes_set(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        result: ImportResult = ImportResult(
            _make_vm(receipt_content=b"CUSTOM"), bc, "test.pdf"
        )

        assert result.receipt_data == b"CUSTOM"
        assert isinstance(result.transaction_text, str)
        assert len(result.transaction_text) > 0
        assert isinstance(result.receipt_destination_path, pathlib.Path)
        assert isinstance(result.ingestion_destination_path, pathlib.Path)

    def test_rollback_size_is_none(self, bc: BeancountConfiguration) -> None:
        result: ImportResult = ImportResult(_make_vm(), bc, "test.pdf")
        assert result.rollback_size is None


# ===========================================================================
# diff
# ===========================================================================


class TestDiff:
    def _build_result(
        self,
        tmp_path: pathlib.Path,
        bc: BeancountConfiguration,
        existing_file_content: str,
    ) -> ImportResult:
        # Pre-write the ingest file with *existing_file_content*.
        (tmp_path / "imported.bean").write_text(existing_file_content)
        return ImportResult(_make_vm(), bc, "test.pdf")

    def test_empty_file_produces_unified_diff(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        lines: list[str] = self._build_result(tmp_path, bc, "").diff()
        assert len(lines) > 0
        header_count: int = sum(
            1 for l in lines if l.startswith("---") or l.startswith("+++")
        )
        assert header_count >= 2

    def test_empty_file_all_added_no_removed(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        result: ImportResult = self._build_result(tmp_path, bc, "")
        lines: list[str] = result.diff()
        minus_lines: list[str] = [
            l for l in lines if l.startswith("-") and not l.startswith("---")
        ]
        assert len(minus_lines) == 0

    def test_empty_file_starts_with_header(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        result: ImportResult = self._build_result(tmp_path, bc, "")
        lines: list[str] = result.diff()
        assert "---" in lines[0] or "+++" in lines[0]

    def test_nonempty_shows_context_and_additions(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        original: str = "2025-01-15 commodity EUR\n\n"
        result: ImportResult = self._build_result(tmp_path, bc, original)
        lines: list[str] = result.diff()

        has_context: bool = any("commodity EUR" in l for l in lines)
        assert has_context
        has_additions: bool = any("+2025-03-18" in l for l in lines)
        assert has_additions

    def test_without_trailing_newline(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        (tmp_path / "imported.bean").write_text("no trailing newline")
        result: ImportResult = ImportResult(_make_vm(), bc, "test.pdf")
        lines: list[str] = result.diff()
        assert len(lines) > 0


# ===========================================================================
# commit
# ===========================================================================


class TestCommit:
    def test_writes_receipt_file(self, bc: BeancountConfiguration) -> None:
        result: ImportResult = ImportResult(
            _make_vm(receipt_content=b"DID-WRITE-THIS"), bc, "img.pdf"
        )
        result.commit()

        assert result.receipt_destination_path.exists()
        assert result.receipt_destination_path.read_bytes() == b"DID-WRITE-THIS"

    def test_appends_transaction(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        result: ImportResult = ImportResult(_make_vm(), bc, "test.pdf")
        result.commit()

        content: str = (tmp_path / "imported.bean").read_text(encoding="utf-8")
        assert "2025-03-18" in content
        assert "Grocery Store" in content

    def test_sets_rollback_size(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        initial_size: int = (tmp_path / "imported.bean").stat().st_size
        result: ImportResult = ImportResult(_make_vm(), bc, "test.pdf")
        result.commit()

        assert result.rollback_size is not None
        assert result.rollback_size == initial_size


class TestCommitRollbackOnFailure:
    def test_receipt_write_failure_preserves_ledger(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        result: ImportResult = ImportResult(
            _make_vm(receipt_content=b"DID-WRITE-THIS"), bc, "img.pdf"
        )

        with mock.patch("pathlib.Path.write_bytes", side_effect=IOError("disk full")):
            with pytest.raises(IOError, match="disk full"):
                result.commit()

        assert (tmp_path / "imported.bean").read_text(encoding="utf-8") == ""

    def test_ingest_write_failure_deletes_receipt(
        self, bc: BeancountConfiguration
    ) -> None:
        result: ImportResult = ImportResult(_make_vm(), bc, "test.pdf")

        # Make file unwritable.
        os.chmod(result.ingestion_destination_path, 000)
        with pytest.raises(IOError, match="Permission denied"):
            result.commit()

        assert not result.receipt_destination_path.exists()

    def test_rollback_size_reset(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        initial_size: int = (tmp_path / "imported.bean").stat().st_size

        result: ImportResult = ImportResult(_make_vm(), bc, "test.pdf")
        result.commit()
        assert result.rollback_size is not None

        result.rollback()

        assert result.rollback_size is None
        final_size: int = (tmp_path / "imported.bean").stat().st_size
        assert final_size == initial_size


# ===========================================================================
# rollback
# ===========================================================================


class TestRollback:
    def _build_with_state(
        self, folder: pathlib.Path
    ) -> tuple[ImportResult, BeancountConfiguration]:
        bc: BeancountConfiguration = _make_config(
            folder, tx_in_ledger="existing content\n"
        )
        vm: mock.MagicMock = _make_vm()

        result: ImportResult = ImportResult(vm, bc, "test.pdf")

        # Make receipt file exist so rollback attempts to delete it.
        result.receipt_destination_path.parent.mkdir(parents=True, exist_ok=True)
        (result.receipt_destination_path).write_text("receipt data")

        # Simulate successful commit: set rollback_size to the post-commit size.
        ingest: pathlib.Path = folder / "imported.bean"
        ingest.write_text("existing content\n")  # pre-roll state
        result.rollback_size = os.path.getsize(str(ingest))

        return result, bc

    def test_deletes_receipt_file(self, tmp_path: pathlib.Path) -> None:
        result: ImportResult
        result, _ = self._build_with_state(tmp_path)

        assert result.receipt_destination_path.exists()
        result.rollback()
        assert not result.receipt_destination_path.exists()

    def test_rollback_truncates_ingestion_file(self, tmp_path: pathlib.Path) -> None:
        content_before: str = "AAAA\n" * 50
        content_after: str = "AAAA\n" * 51

        ingest_path: pathlib.Path = tmp_path / "ingest.bean"
        ingest_path.write_text(content_before)

        bc: BeancountConfiguration = BeancountConfiguration()
        bc.main_file = tmp_path / "dummy.bean"
        bc.ingestion_destination_file = pathlib.Path("ingest.bean")
        bc.account_list_file = tmp_path / "acct.txt"
        (tmp_path / "acct.txt").write_text("")

        result: ImportResult = ImportResult(_make_vm(), bc, "test.pdf")
        result.ingestion_destination_path = ingest_path
        result.rollback_size = len(content_before.encode("utf-8"))

        # Also fake receipt path for the deletion branch.
        result.receipt_destination_path = tmp_path / "nonexistent-receipt"
        ingest_path2: pathlib.Path = tmp_path / "ingest.bean"
        ingest_path2.write_text(content_after)

        result.rollback()

        final_content: str = ingest_path.read_text(encoding="utf-8")
        assert len(final_content) == len(content_before)
        assert len(content_before) < len(content_after)

    def test_restores_file_to_pre_commit_state(
        self, tmp_path: pathlib.Path, bc: BeancountConfiguration
    ) -> None:
        original_text: str = "line one\nline two\n"
        ingest: pathlib.Path = tmp_path / "imported.bean"

        result: ImportResult = ImportResult(_make_vm(), bc, "test.pdf")
        ingest.write_text(original_text)
        result.rollback_size = len(original_text.encode("utf-8"))

        # Simulate post-commit append.
        extended: str = original_text + "more stuff appended here!!!\n"
        ingest.write_text(extended)

        result.rollback()

        assert ingest.read_text() == original_text

    def test_raises_when_receipt_unlink_fails(self, bc: BeancountConfiguration) -> None:
        result: ImportResult = ImportResult(_make_vm(), bc, "test.pdf")

        # Ensure the receipt exists so unlink is attempted.
        result.receipt_destination_path.parent.mkdir(parents=True, exist_ok=True)
        (result.receipt_destination_path).write_text("x")
        result.rollback_size = 0

        with mock.patch.object(
            pathlib.Path, "unlink", side_effect=PermissionError("denied")
        ):
            with pytest.raises(PermissionError):
                result.rollback()

    def test_raises_when_truncate_fails(self, tmp_path: pathlib.Path) -> None:
        ingest_content: str = "AAAA\n" * 50
        ingest_file: pathlib.Path = tmp_path / "ingest.bean"
        ingest_file.write_text(ingest_content)

        bc: BeancountConfiguration = BeancountConfiguration()
        bc.main_file = tmp_path / "dummy.bean"
        bc.ingestion_destination_file = pathlib.Path("ingest.bean")
        bc.account_list_file = tmp_path / "acct.txt"
        (tmp_path / "acct.txt").write_text("")

        result: ImportResult = ImportResult(_make_vm(), bc, "test.pdf")
        result.ingestion_destination_path = ingest_file
        result.rollback_size = len(ingest_content.encode("utf-8"))
        result.receipt_destination_path = tmp_path / "nope"

        with mock.patch("os.truncate", side_effect=OSError("read only fs")):
            with pytest.raises(OSError):
                result.rollback()  # receipt deletion succeeded; truncate failed
            assert result.rollback_size is not None  # size is not cleared!
