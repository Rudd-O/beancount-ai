import argparse
import os
import sys

from beancount_ai.server.config import Configuration
from beancount_ai.server.storage import (
    ResourceNotFoundError,
    make_receipt_backend,
)


def run(cfg: Configuration, args: argparse.Namespace) -> None:
    fn = os.path.basename(bytes.fromhex(args.filename).decode("utf-8"))
    receipt_path = f"/{fn}"

    location = cfg.documents.uningested_location_name()
    print(f"Removing {fn} from {location}", file=sys.stderr)

    try:
        make_receipt_backend(cfg, "uningested").remove(receipt_path)
    except ResourceNotFoundError:
        try:
            make_receipt_backend(cfg, "unassociated").remove(receipt_path)
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
        "beanai.Remove", help="Delete a receipt file from the configured backend"
    )
    remove_cmd.add_argument(
        "filename",
        help="Filename of the receipt (encoded as hex)",
    )
    return sp
