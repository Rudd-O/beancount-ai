import argparse

from beancount_ai.client.config import Configuration
from beancount_ai.client.server import RemoteVM


def do_list_uningested(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Lists uningested receipt files from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    for fname in RemoteVM.from_cfg(cfg).list_receipts("uningested"):
        print(fname)


def do_list_unassociated(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Lists receipt files yet to be associated to transactions from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    for fname in RemoteVM.from_cfg(cfg).list_receipts("unassociated"):
        print(fname)


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    sp.add_parser(
        "list-unassociated",
        help="Get receipt filenames to associate with transactions (plain text)",
    )
    sp.add_parser(
        "list-uningested",
        help="Get receipt filenames to import as transactions (plain text)",
    )
    return sp
