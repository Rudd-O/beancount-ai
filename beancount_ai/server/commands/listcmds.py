import argparse
import json
import sys
from typing import Literal

from beancount_ai.server.config import Configuration, WebDAVDocumentSourcesConfiguration
from beancount_ai.server.llm import (
    VALID_EXTENSIONS,
)
from beancount_ai.server.storage import WebDAVClient
from beancount_ai.structs import ItemListing


def do_list(
    cfg: Configuration,
    category: Literal["unassociated"] | Literal["uningested"],
    args: argparse.Namespace,
) -> None:
    if isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration):
        client = WebDAVClient(cfg.documents, category)
    else:
        assert 0, "not reached"

    try:
        items = client.list()
    except Exception as e:
        print(f"error: list failed: {e}", file=sys.stderr)
        sys.exit(1)

    files: list[ItemListing] = []
    for item in items:
        name = item["name"]
        if not any(name.lower().endswith(ext) for ext in VALID_EXTENSIONS):
            continue
        files.append(
            {
                "name": name,
                "content_length": item["content_length"],
                "modified": item["modified"],
            }
        )

    files.sort(key=lambda f: f["modified"])

    print(
        json.dumps(
            {
                "receipts": [f["name"] for f in files],
                "count": len(files),
            }
        )
    )


def do_list_unassociated(cfg: Configuration, args: argparse.Namespace) -> None:
    do_list(cfg, "unassociated", args)


def do_list_uningested(cfg: Configuration, args: argparse.Namespace) -> None:
    do_list(cfg, "uningested", args)


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    sp.add_parser(
        "beanai.ListUningested",
        help="List receipt filenames to import as transactions (JSON)",
    )
    sp.add_parser(
        "beanai.ListUnassociated",
        help="List receipt filenames to associate with transactions (JSON)",
    )
    return sp
