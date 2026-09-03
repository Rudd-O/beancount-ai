#!/usr/bin/env python3
"""bean-ai — CLI wrapper that talks to bean-ai-server on `pim` via qrexec.

Config is read from ~/.config/bean-ai.json.
"""

import argparse
import sys

from beancount_ai.client.commands import (
    associate,
    fetch,
    importcmd,
    ingest,
    listcmds,
    organize,
    process,
    refine,
    remove,
)
from beancount_ai.client.config import Configuration


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="bean-ai",
        description="qrexec client for bean-ai",
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
        remove.subcommand_parser,
        organize.subcommand_parser,
        importcmd.subcommand_parser,
        ingest.subcommand_parser,
        associate.subcommand_parser,
        refine.subcommand_parser,
    ]:
        sp = p(sp)

    return ap


def main() -> None:
    ap = build_parser()
    args = ap.parse_args()

    cfg = Configuration.load(args.conf_path)

    if not args.command:
        ap.print_help(sys.stderr)
        sys.exit(1)

    dispatch = {
        "list-unassociated": listcmds.do_list_unassociated,
        "list-uningested": listcmds.do_list_uningested,
        "fetch": fetch.run,
        "import": importcmd.run,
        "ingest": ingest.run,
        "organize": organize.run,
        "process": process.run,
        "remove": remove.run,
        "associate": associate.run,
        "refine": refine.run,
    }
    # The BeancountConfiguration was locked when it was instantiated (see
    # Configuration.load); the lock is held for the rest of the process.
    dispatch[args.command](cfg, args)


if __name__ == "__main__":
    main()
