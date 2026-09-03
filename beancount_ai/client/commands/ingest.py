import argparse
import sys
import tempfile
from io import StringIO
from pathlib import Path
from textwrap import indent
from traceback import print_exception

from beancount_ai.client.commands import remove
from beancount_ai.client.commands.importcmd import ImportResult
from beancount_ai.client.config import Configuration
from beancount_ai.client.display import print_diff
from beancount_ai.client.server import (
    RemoteVM,
    preview_receipt,
)


def run(cfg: Configuration, args: argparse.Namespace) -> None:  # noqa: C901
    """
    Processes all known receipts using the following procedure for each receipt:

    Imports the receipt from the server then, on success, deletes the receipt from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    vm = RemoteVM.from_cfg(cfg)
    receipts = vm.list_receipts("uningested")

    if args.filename:
        for fn in args.filename:
            if fn not in receipts:
                print(f"Receipt {fn} does not exist on server.", file=sys.stderr)
                sys.exit(1)
        # All specified receipts exist on the server.  Let's override
        # the list with what the user sent us.
        receipts = args.filename

    if not receipts:
        print("No receipts to ingest.", file=sys.stderr)
        return

    def do_ingest_one(receipt: str, preview_dir: Path) -> None:  # noqa: C901
        # Attempt the import.
        try:
            imp = ImportResult(vm, cfg.beancount, receipt)
        except Exception as e:
            raise Exception(f"Import of {receipt} failed: {e}") from e

        # Show a diff.
        diff = imp.diff()
        if diff:
            print_diff(diff)
        else:
            print(f"No changes to {args.filename}", file=sys.stderr)
            return

        if args.yes:
            action = "import"
        elif args.no:
            action = "draft-import"
        else:
            action = "skip"
            while True:
                print(
                    f"\nImport proposed transaction based on '{receipt}'? [y]es / [n]o / [p]review receipt / [q]uit ",
                    file=sys.stderr,
                    end="",
                )
                try:
                    answer = input().strip().lower()
                except EOFError:
                    return

                if answer == "q":
                    sys.exit(0)

                if answer == "p":
                    preview_receipt(cfg, receipt, preview_dir)
                    continue  # re-prompt for the same receipt

                if answer == "y":
                    action = "import"

                break  # leave prompt loop after y or n

            if "import" not in action:
                return  # genuinely skip this receipt

        # Commit the successful import.
        if action != "import":
            print("No files were changed.", file=sys.stderr)
            return
        else:
            try:
                imp.commit()
            except Exception as e:
                raise Exception(f"Commit of imported {receipt} failed: {e}") from e

            # Remove from WebDAV only after successful import.
            try:
                remove.run(cfg, argparse.Namespace(filename=receipt))
            except Exception as e:
                try:
                    # At this point, we have the transaction written and the receipt
                    # saved locally, but the receipt could not be deleted remotely,
                    # so it is safe to roll back without data loss.  Since the receipt
                    # is still on the server side, we can retry reimporting the same
                    # receipt later.
                    imp.rollback()
                except Exception as ee:
                    raise Exception(
                        f"Could not roll back transaction of imported {receipt}: {ee}"
                    ) from ee
                raise Exception(f"Could not remove {receipt} from WebDAV: {e}") from e

    with tempfile.TemporaryDirectory() as tmpdir:
        preview_dir = Path(tmpdir)

        exceptions: list[tuple[str, Exception]] = []
        for receipt in receipts:
            try:
                do_ingest_one(receipt, preview_dir)
            except Exception as e:
                exceptions.append((receipt, e))
                if args.yes or args.no:
                    print(f"{e} — continuing to next receipt", file=sys.stderr)
                else:
                    raise
        if exceptions:
            # Can only get here when not in batch mode.
            print("Summary of errors encountered:", file=sys.stderr)
            for f, exc in exceptions:
                print(f"* {f}:", file=sys.stderr)
                capt = StringIO()
                print_exception(exc, file=capt)
                capt.seek(0)
                print(f"{indent(capt.read(), '    ')}", file=sys.stderr)
            sys.exit(1)


def subcommand_parser(
    sp: argparse._SubParsersAction[argparse.ArgumentParser],  # pyright: ignore[reportPrivateUsage]
) -> argparse._SubParsersAction[argparse.ArgumentParser]:  # pyright: ignore[reportPrivateUsage]
    ing_cmd = sp.add_parser(
        "ingest",
        help="Batch / interactive ingest of receipt: process → organize → append → remove",
    )
    ing_cmd.add_argument(
        "filename",
        help="One or more receipt filenames (if none are present, all are processed)",
        nargs="*",
    )
    yes_group = ing_cmd.add_mutually_exclusive_group()
    yes_group.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        dest="yes",
        help="Ingest receipts without confirmation (equivalent to answering 'yes' to every prompt)",
    )
    yes_group.add_argument(
        "--no",
        "-n",
        action="store_true",
        default=False,
        dest="no",
        help="Do all the work of ingesting a receipt but don't touch any files (equivalent to answering 'no' to every prompt)",
    )
    return sp
