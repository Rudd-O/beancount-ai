import argparse
import os
import sys
from functools import partial

from webdav4.client import ResourceNotFound  # type:ignore

from beancount_ai.server.config import Configuration, WebDAVDocumentSourcesConfiguration
from beancount_ai.server.storage import WebDAVClient


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    fn = os.path.basename(bytes.fromhex(args.filename).decode("utf-8"))

    print(f"Fetching {fn} from WebDAV receipts URL", file=sys.stderr)
    receipt_path = f"/{fn}"

    if isinstance(cfg.documents, WebDAVDocumentSourcesConfiguration):
        client_factory = partial(WebDAVClient, cfg.documents)
    else:
        assert 0, "not reached"

    try:
        webdav_client = client_factory("uningested")
        raw = webdav_client.read(receipt_path)
    except ResourceNotFound:
        try:
            webdav_client = client_factory("unassociated")
            raw = webdav_client.read(receipt_path)
        except Exception as e:
            print(f"error: cannot read {fn}: {e}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"error: cannot read {fn}: {e}", file=sys.stderr)
        sys.exit(1)

    sys.stdout.buffer.write(raw)
    sys.stdout.buffer.flush()


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    fetch_cmd = sp.add_parser(
        "beanai.Fetch", help="Write the raw contents of a receipt file to stdout"
    )
    fetch_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )
    return sp
