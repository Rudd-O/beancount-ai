import argparse
import subprocess
import sys

from beancount_ai.client.config import Configuration
from beancount_ai.client.server import (
    RemoteVM,
)


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Processes a receipt file and produces the output of the LLM to stdout.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    try:
        llm_output, account = RemoteVM.from_cfg(cfg).process_receipt(
            args.filename, cfg.beancount.account_list_file.read_text().splitlines()
        )
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)

    print(llm_output)
    print(f"Main account: {account}")


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    process_cmd = sp.add_parser(
        "process", help="Process a receipt image and produce Beancount output from it"
    )
    process_cmd.add_argument("filename", help="Filename of the receipt")
    return sp
