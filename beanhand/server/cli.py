#!/usr/bin/env python3
"""beanhand-server — qrexec RPC service for receipt processing (runs on VM with receipts).

Config is read from ~/.config/beanhand.json unless overridden.
As a qrexec service, it reads nothing from stdin and only writes structured results to stdout.
"""

import argparse

from beanhand.server.commands import (
    associate,
    fetch,
    listcmds,
    process,
    refine,
    remove,
)

from .config import Configuration


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="beanhand-server",
        description="Backend RPC service for the beanhand CLI."
        "  This is the program in charge of fetching documents and talking to the LLM.",
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
        process.subcommand_parser,
        fetch.subcommand_parser,
        associate.subcommand_parser,
        remove.subcommand_parser,
        refine.subcommand_parser,
    ]:
        sp = p(sp)

    return ap


def main() -> None:
    import sys

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
        "beanhand.Process": process.run,
        "beanhand.HelpAssociateReceipt": associate.run,
        "beanhand.Remove": remove.run,
        "beanhand.Refine": refine.run,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        ap.print_help(sys.stderr)
        sys.exit(1)

    handler(cfg, args)


if __name__ == "__main__":
    main()
