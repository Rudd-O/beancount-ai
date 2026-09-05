import argparse
import json
import os
import sys

from beancount_ai.server.config import Configuration
from beancount_ai.server.storage import (
    ResourceNotFoundError,
    make_receipt_backend,
)


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    fn = os.path.basename(bytes.fromhex(args.filename).decode("utf-8"))

    location = cfg.documents.uningested_location_name()
    print(f"Fetching {fn} from {location}", file=sys.stderr)
    receipt_path = f"/{fn}"

    try:
        fetched = make_receipt_backend(cfg, "uningested").read(receipt_path)
    except ResourceNotFoundError:
        try:
            fetched = make_receipt_backend(cfg, "unassociated").read(receipt_path)
        except Exception as e:
            print(f"error: cannot read {fn}: {e}", file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print(f"error: cannot read {fn}: {e}", file=sys.stderr)
        sys.exit(1)

    # The wire format is a single JSONL metadata line, then the raw bytes of
    # the receipt.  The newline after the JSON line is the delimiter that the
    # client partitions on.
    meta = json.dumps({"timestamp": fetched.timestamp})
    sys.stdout.buffer.write(meta.encode("utf-8") + b"\n")
    sys.stdout.buffer.write(fetched.data)
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
