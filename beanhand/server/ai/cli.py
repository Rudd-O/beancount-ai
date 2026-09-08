#!/usr/bin/env python3
"""beanhand-ai-server — qrexec RPC service for LLM-driven operations
(receipt processing, receipt matching, transaction refinement).  Runs on the
VM that has access to the LLM API.

Config is read from ~/.config/beanhand.json unless overridden.
This program needs the ``ai`` section of the config; it does not touch the
receipt storage itself: when an operation needs a document, the client
fetches it from the documents server and relays it over stdin.
"""

import argparse
import sys

from beanhand.server.ai.commands import (
    associate,
    process,
    refine,
)

from .config import Configuration


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="beanhand-ai-server",
        description="Backend RPC service in charge of talking to the LLM.",
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
        process.subcommand_parser,
        associate.subcommand_parser,
        refine.subcommand_parser,
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
        "beanhand.Process": process.run,
        "beanhand.HelpAssociateReceipt": associate.run,
        "beanhand.Refine": refine.run,
    }
    handler = dispatch.get(args.command)
    if handler is None:
        ap.print_help(sys.stderr)
        sys.exit(1)

    handler(cfg, args)


if __name__ == "__main__":
    main()
