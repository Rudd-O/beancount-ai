import argparse
import sys
from datetime import date

from beanhand.client.beancount_loader import (
    account_refs_or_die,
)
from beanhand.client.config import Configuration
from beanhand.client.receipts import ReceiptRef
from beanhand.client.server.ai import AIClient
from beanhand.client.server.documents import DocumentsClient


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Processes a receipt file and produces the output of the LLM to stdout.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    try:
        ref = ReceiptRef.resolve([args.filename])[0]
        # A local receipt makes zero documents-server calls: the client is
        # constructed anyway but never contacted when the receipt is local.
        documents_vm = DocumentsClient.from_cfg(cfg)
        fetched = ref.load(documents_vm)
        ai_vm = AIClient.from_cfg(cfg)
        resp = ai_vm.process_receipt(
            ref.filename,
            fetched,
            account_refs_or_die(cfg.beancount.main_file, date.today()),
        )
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(1)

    print(resp.transaction)
    print(f"Main account: {resp.payment_account}")


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    process_cmd = sp.add_parser(
        "process", help="Process a receipt image and produce Beancount output from it"
    )
    process_cmd.add_argument(
        "filename", help="Receipt store filename or path to a local file"
    )
    return sp
