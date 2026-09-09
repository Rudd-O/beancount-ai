import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from beanhand.client.beanfiles import predict_receipt_destination_path
from beanhand.client.config import Configuration
from beanhand.client.receipts import ReceiptRef
from beanhand.client.server.documents import (
    DocumentsClient,
    save_receipt,
)


def organize_receipt(
    beancount_folder: Path,
    ref: ReceiptRef,
    documents: DocumentsClient,
    transaction_date: date,
    account: str,
    description: str | None = None,
) -> Path:
    """
    Organize a receipt file into an account folder.

    For receipts to be recognized as documents in Beancount, their filename has
    the requirement that it must begin with a date in Y-m-d format.  Hence
    the requisite transaction date at the beginning of the file name.

    ``ref`` names the receipt (local file or store filename); its bytes are
    loaded from whichever source that is, and filed under the receipt's
    basename.  The documents server is contacted only when ``ref`` is a store
    receipt (``documents`` is simply unused for a local receipt).
    """
    receipt_path = predict_receipt_destination_path(
        beancount_folder, transaction_date, ref.filename, account, description
    )
    fetched = ref.load(documents)
    save_receipt(receipt_path, fetched)
    return receipt_path


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Copies a receipt to the designated folder for the account under the Beancount folder.

    See `predict_receipt_destination_path` for requirements imposed on Beancount document
    file naming.
    """
    tdate = datetime.strptime(args.date, "%Y-%m-%d").date()
    ref = ReceiptRef.resolve([args.filename])[0]
    # A local receipt makes zero documents-server calls: the client is
    # constructed anyway but never contacted when the receipt is local.
    documents = DocumentsClient.from_cfg(cfg)
    receipt_path = organize_receipt(
        cfg.beancount.main_folder,
        ref,
        documents,
        tdate,
        args.account,
    )
    print("The file has been organized into", str(receipt_path), file=sys.stderr)


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    org_cmd = sp.add_parser(
        "organize", help="File a copy of a receipt under a payment account"
    )
    org_cmd.add_argument(
        "filename",
        help="Receipt store filename or path to a local file",
    )
    org_cmd.add_argument("date", help="Date to impute to receipt file", type=str)
    org_cmd.add_argument("account", help="Payment account (e.g. Assets:Cash:CHF)")
    return sp
