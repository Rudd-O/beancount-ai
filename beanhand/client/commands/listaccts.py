import argparse
from datetime import date

from beanhand.client.beancount_loader import (
    account_refs_or_die,
)
from beanhand.client.config import Configuration


def run(cfg: Configuration, args: argparse.Namespace) -> None:  # noqa: C901
    for acct in account_refs_or_die(cfg.beancount.main_file, args.date):
        print(acct["name"])
        if "rule" in acct:
            print(f"  rule: {acct['rule']}")


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    cmd = sp.add_parser(
        "list-accounts",
        help="List accounts that will be used in Beancount file editing functionality",
    )
    cmd.add_argument(
        "date",
        help="Date to consider when listing included accounts for the purposes of distinguishing between open and close accounts (defaults to today)",
        default=date.today(),
        nargs="?",
    )
    return sp
