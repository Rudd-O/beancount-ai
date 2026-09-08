#!/usr/bin/env python3
"""beanhand-documents-server — qrexec RPC service for receipt document operations
(listing, fetching, removing).  Runs on the VM that has the receipts.

Config is read from ~/.config/beanhand.json unless overridden.
"""

import argparse
import sys

from beanhand.server.documents.commands import (
    fetch,
    listcmds,
    remove,
)

from .config import Configuration


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="beanhand-documents-server",
        description="Backend RPC service in charge of fetching and erasing documents.",
    )
    ap.add_argument(
        "--config",
        "-c",
        default=None,
        dest="conf_path",
        help="Path to the config file; overrides $BEANHAND_CONFIG and the default",
    )
    sp = ap.add_subparsers(dest="command")
    for p in [
        listcmds.subcommand_parser,
        fetch.subcommand_parser,
        remove.subcommand_parser,
    ]:
        sp = p(sp)

    return ap


def main() -> None:
    ap = build_parser()
    args = ap.parse_args()

    if not args.command:
        ap.print_help(sys.stderr)
        sys.exit(1)

    cfg = Configuration.load(args.conf_path)

    dispatch = {
        "beanhand.ListUningested": listcmds.do_list_uningested,
        "beanhand.ListUnassociated": listcmds.do_list_unassociated,
        "beanhand.Fetch": fetch.run,
        "beanhand.Remove": remove.run,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        ap.print_help(sys.stderr)
        sys.exit(1)

    handler(cfg, args)


if __name__ == "__main__":
    main()
