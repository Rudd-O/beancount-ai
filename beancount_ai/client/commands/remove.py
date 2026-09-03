import argparse

from beancount_ai.client.config import Configuration
from beancount_ai.client.server import RemoteVM


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Removes a receipt file from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    RemoteVM.from_cfg(cfg).remove_receipt(args.filename)


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    rm_cmd = sp.add_parser("remove", help="Delete a receipt file")
    rm_cmd.add_argument("filename", help="Filename of the receipt file")
    return sp
