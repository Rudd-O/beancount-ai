import argparse
import sys
from datetime import date, datetime
from pathlib import Path

from beancount_ai.client.beanfiles import predict_receipt_destination_path
from beancount_ai.client.config import Configuration
from beancount_ai.client.server import (
    RemoteVM,
    save_receipt,
)


def organize_receipt(
    beancount_folder: Path,
    vm: RemoteVM,
    transaction_date: date,
    filename: str,
    account: str,
    description: str | None = None,
) -> Path:
    """
    Organize a receipt file into an account folder.

    For receipts to be recognized as documents in Beancount, their filename has
    the requirement that it must begin with a date in Y-m-d format.  Hence
    the requisite transaction date at the beginning of the file name.
    """
    receipt_path = predict_receipt_destination_path(
        beancount_folder, transaction_date, filename, account, description
    )
    fetched = vm.fetch_receipt(filename)
    save_receipt(receipt_path, fetched)
    return receipt_path


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Copies a receipt to the designated folder for the account under the Beancount folder.

    See `predict_receipt_destination_path` for requirements imposed on Beancount document
    file naming.
    """
    tdate = datetime.strptime(args.date, "%Y-%m-%d").date()
    receipt_path = organize_receipt(
        cfg.beancount.main_folder,
        RemoteVM.from_cfg(cfg),
        tdate,
        args.filename,
        args.account,
    )
    print("The file has been organized into", str(receipt_path), file=sys.stderr)


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    org_cmd = sp.add_parser(
        "organize", help="File a copy of a receipt under a payment account"
    )
    org_cmd.add_argument("filename", help="Filename of the receipt")
    org_cmd.add_argument("date", help="Date to impute to receipt file", type=str)
    org_cmd.add_argument("account", help="Payment account (e.g. Assets:Cash:CHF)")
    return sp
