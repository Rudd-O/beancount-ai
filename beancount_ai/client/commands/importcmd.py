import argparse
import difflib
import os
import sys
from datetime import datetime
from pathlib import Path

from beancount_ai.client.beanfiles import (
    FileGuard,
    FileModifiedError,
    insert_document_metadata,
    predict_receipt_destination_path,
)
from beancount_ai.client.config import BeancountConfiguration, Configuration
from beancount_ai.client.display import print_diff
from beancount_ai.client.server import (
    RemoteVM,
)


class ImportResult:
    receipt_data: bytes
    transaction_text: str
    receipt_destination_path: Path
    ingestion_destination_path: Path
    rollback_size: int | None = None
    # Guard over the ingestion file, snapshotted the moment this result is
    # constructed.  The file must already exist: this is a Beancount file
    # operation, and proceeding without it is an error, so taking the guard
    # fails loudly if it is missing.  :meth:`commit` refuses to write if the
    # on-disk content has since changed, so we never clobber an edit the user
    # made while we were processing the receipt.
    _ingestion_guard: FileGuard

    def __init__(
        self,
        vm: RemoteVM,
        beancount: BeancountConfiguration,
        filename: str,
    ) -> None:
        # Snapshot the ingestion file up front, before the (slow) LLM call, so
        # that edits made while we process the receipt are detected at commit.
        # The file must exist (it is a Beancount file); ``take`` raises if it
        # is missing, surfacing that error instead of proceeding.
        dest = beancount.ingestion_destination_path
        self._ingestion_guard = FileGuard.take(dest)

        receipt_data = vm.fetch_receipt(filename)

        beancount_transaction, account = vm.process_receipt(
            filename, beancount.account_list_file.read_text().splitlines()
        )
        # Strip headline comments and newlines from the transaction.
        while beancount_transaction.lstrip().startswith(";"):
            beancount_transaction = "".join(
                beancount_transaction.splitlines(True)[1:]
            ).lstrip()

        datestr, reststr = beancount_transaction.split(" ", 1)
        # Take the text after the date, remove the transaction flag and the space next to it,
        # then use the payee and narration to construct a description for the receipt file name.
        # If there is a comment at the end of the line, strip it too.
        reststr = (
            reststr.splitlines()[0][2:]
            .replace('" "', " — ")
            .replace('"', "")
            .split(";")[0]
            .strip()
        )
        # Take the text containing the date, and make a date for the receipt file name.
        transdate = datetime.strptime(datestr, "%Y-%m-%d").date()

        receipt_path = predict_receipt_destination_path(
            beancount.main_folder, transdate, filename, account, reststr
        )
        formatted_tx = insert_document_metadata(
            beancount_transaction, str(receipt_path)
        )

        self.receipt_data = receipt_data
        self.transaction_text = formatted_tx
        self.receipt_destination_path = receipt_path
        self.ingestion_destination_path = beancount.ingestion_destination_path

    def _formatted_transaction_text(self, lastchar: str) -> str:
        """
        Format a transaction based on the last character of the file it will be added to.

        Caller supplies said last character(s).
        """
        return (
            ("\n" if lastchar.endswith("\n") else "\n\n")
            + self.transaction_text.strip()
            + "\n"
        )

    def diff(self) -> list[str]:
        """Print a unified diff of what would be appended to the ingestion file."""
        dest = self.ingestion_destination_path
        current = dest.read_text(encoding="utf-8")
        old_lines = current.splitlines(True) if current else []
        appended = self._formatted_transaction_text(current)
        new_lines = (current + appended).splitlines(True)
        diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=str(dest),
                tofile=str(dest),
                n=5,
            )
        )
        return diff

    def commit(self) -> None:
        dest = self.ingestion_destination_path
        receipt_path = self.receipt_destination_path

        # The ingestion file is shared across transactions, so it is the most
        # likely place for the user (or another bean-ai run) to edit while we
        # were away talking to the LLM.  If it changed since we snapshotted it
        # at construction, refuse to write rather than corrupting the file.
        try:
            self._ingestion_guard.verify()
        except FileModifiedError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)

        try:
            # Write the receipt data.
            receipt_path.write_bytes(self.receipt_data)
            print(
                f"The receipt has been filed under {receipt_path}",
                file=sys.stderr,
            )

            with open(self.ingestion_destination_path, "a+") as f:
                self.rollback_size = self.ingestion_destination_path.stat().st_size
                # no marker line is necessary, the transaction has a link to the document in it.
                # f.write("\n; {} imported by bean-ai.\n".format(args.filename))
                if f.tell() > 0:
                    f.seek(f.tell() - 1)
                lastchar = f.read()
                f.write(self._formatted_transaction_text(lastchar))
                f.flush()
                os.fsync(f.fileno())

                print(
                    f"The transaction has been imported to {dest}",
                    file=sys.stderr,
                )
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        eee: Exception | None = None

        if self.receipt_destination_path.exists():
            try:
                self.receipt_destination_path.unlink()
            except Exception as e:
                eee = e
                print(
                    f"The receipt {self.receipt_destination_path} could not be deleted as part of the transaction rollback",
                    file=sys.stderr,
                )

        if self.rollback_size is not None:
            try:
                os.truncate(self.ingestion_destination_path, self.rollback_size)
                self.rollback_size = None
            except Exception as e:
                eee = e
                print(
                    f"The transaction written to {self.ingestion_destination_path} could not be rolled back",
                    file=sys.stderr,
                )

        if eee is not None:
            raise eee


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Imports a receipt by creating a Beancount transaction for it, copying the document to
    the appropriate Beancount document folder, then writing the Beancount transaction to
    the designated transactions file while associating the transaction with the document.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    result = ImportResult(RemoteVM.from_cfg(cfg), cfg.beancount, args.filename)

    diff = result.diff()
    if diff:
        print_diff(diff)
    else:
        print(f"No changes to {args.filename}", file=sys.stderr)
        return

    result.commit()


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    imp_cmd = sp.add_parser(
        "import",
        help="Like ingest, but receipt in server is left alone instead of deleted",
    )
    imp_cmd.add_argument("filename", help="Filename of the receipt")
    return sp
