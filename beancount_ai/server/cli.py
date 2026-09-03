#!/usr/bin/env python3
"""bean-ai-server — qrexec RPC service for receipt processing (runs on VM with receipts).

Config is read from ~/.config/bean-ai.json unless overridden.
As a qrexec service, it reads nothing from stdin and only writes structured results to stdout.
"""

import argparse

from beancount_ai.server.commands import (
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
        prog="bean-ai-server",
        description="qrexec RPC service for bean-ai",
    )
    ap.add_argument(
        "--config",
        "-c",
        default=None,
        dest="conf_path",
        help="Path to the config file; overrides $BEAN_AI_CONFIG and the default",
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
        "beanai.ListUningested": listcmds.do_list_uningested,
        "beanai.ListUnassociated": listcmds.do_list_unassociated,
        "beanai.Fetch": fetch.run,
        "beanai.Process": process.run,
        "beanai.HelpAssociateReceipt": associate.run,
        "beanai.Remove": remove.run,
        "beanai.Refine": refine.run,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        ap.print_help(sys.stderr)
        sys.exit(1)

    handler(cfg, args)


if __name__ == "__main__":
    main()
