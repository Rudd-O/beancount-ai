import argparse
from pathlib import Path

from beancount_ai.client.config import Configuration
from beancount_ai.client.server import RemoteVM


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Fetches a receipt file from the server and saves it to the file specified in the arguments.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    gotten = RemoteVM.from_cfg(cfg).fetch_receipt(args.filename)
    Path(args.destination).write_bytes(gotten)


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    fetch_cmd = sp.add_parser("fetch", help="Fetch a receipt file from the server")
    fetch_cmd.add_argument("filename", help="Filename of the receipt")
    fetch_cmd.add_argument("destination", help="Local path to save the retrieved file")
    return sp
