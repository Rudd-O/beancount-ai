import argparse
import os
import sys
from functools import partial

from webdav4.client import ResourceNotFound  # type:ignore

from beancount_ai.server.config import Configuration, WebDAVDocumentSourcesConfiguration
from beancount_ai.server.storage import WebDAVClient


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    fn = os.path.basename(bytes.fromhex(args.filename).decode("utf-8"))
    receipt_path = f"/{fn}"

    print(f"Removing {fn} from WebDAV receipts URL", file=sys.stderr)

    if isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration):
        client_factory = partial(WebDAVClient, cfg.documents)
    else:
        assert 0, "not reached"

    try:
        webdav_client = client_factory("uningested")
        webdav_client.remove(receipt_path)
    except ResourceNotFound:
        try:
            webdav_client = client_factory("unassociated")
            webdav_client.remove(receipt_path)
        except Exception as e:
            print(f"error: cannot remove {fn}: {e}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"error: cannot remove {fn}: {e}", file=sys.stderr)
        sys.exit(1)


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    remove_cmd = sp.add_parser(
        "beanai.Remove", help="Delete a receipt file from WebDAV"
    )
    remove_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )
    return sp
