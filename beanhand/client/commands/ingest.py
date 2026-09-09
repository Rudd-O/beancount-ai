import argparse
import sys
import tempfile
from io import StringIO
from pathlib import Path
from textwrap import indent
from traceback import print_exception

from beanhand.client.commands import remove
from beanhand.client.commands.importcmd import ImportResult
from beanhand.client.config import Configuration
from beanhand.client.display import print_diff
from beanhand.client.receipts import (
    ReceiptRef,
    Source,
    maybe_warn_collision,
)
from beanhand.client.server.ai import (
    AIClient,
)
from beanhand.client.server.documents import (
    DocumentsClient,
    preview_receipt,
)


def run(cfg: Configuration, args: argparse.Namespace) -> None:  # noqa: C901
    """
    Processes all known receipts using the following procedure for each receipt:

    Imports the receipt from the server then, on success, deletes the receipt from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    vm = DocumentsClient.from_cfg(cfg)
    ai_vm = AIClient.from_cfg(cfg)

    # With filenames, each argument resolves independently (a local path or a
    # store name); a local path that does not exists fails fast, before any LLM
    # or transport call.  A store name is no longer pre-checked against the
    # listing — the fetch during processing is the single source of truth.
    # Without filenames, the batch is the store's uningested queue (all STORE).
    # ``resolve`` of a list resolves each argument and de-duplicates (D7) in one
    # step, so a receipt named more than once is processed exactly once.
    if args.filename:
        refs = ReceiptRef.resolve(args.filename)
    else:
        refs = ReceiptRef.resolve(vm.list_receipts("uningested"))

    if not refs:
        print("No receipts to ingest.", file=sys.stderr)
        return

    def do_ingest_one(ref: ReceiptRef, preview_dir: Path) -> None:  # noqa: C901
        # Load the receipt's bytes (local read or store fetch).  A local file
        # that vanished between resolution and read surfaces here, before any
        # AI call (most commonly because an earlier receipt in this run moved it).
        try:
            fetched = ref.load(vm)
        except FileNotFoundError:
            raise Exception(
                f"Import of {ref.arg} failed: file not found "
                "(already processed earlier in this run?)"
            ) from None

        maybe_warn_collision(vm, "uningested", ref.arg, ref)

        # Attempt the import.
        try:
            imp = ImportResult(ai_vm, cfg.beancount, ref.filename, fetched)
        except Exception as e:
            raise Exception(f"Import of {ref.arg} failed: {e}") from e

        # Show a diff.
        diff = imp.diff()
        if diff:
            print_diff(diff)
        else:
            print(f"No changes to {ref.arg}", file=sys.stderr)
            return

        if args.yes:
            action = "import"
        elif args.no:
            action = "draft-import"
        else:
            action = "skip"
            while True:
                print(
                    f"\nImport proposed transaction based on '{ref.arg}'? [y]es / [n]o / [p]review receipt / [q]uit ",
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
                    preview_receipt(fetched, preview_dir)
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
                raise Exception(f"Commit of imported {ref.arg} failed: {e}") from e

            # Consume the receipt from wherever it came from, only after a
            # successful import: a store receipt is removed from the server, a
            # local source file is moved into the account folder (its organized
            # copy was already written by commit, with the source's mtime).
            if ref.src is Source.STORE:
                try:
                    remove.run(cfg, argparse.Namespace(filename=ref.filename))
                except Exception as e:
                    try:
                        # At this point, we have the transaction written and the
                        # receipt saved locally, but the receipt could not be
                        # deleted remotely, so it is safe to roll back without
                        # data loss.  Since the receipt is still on the server
                        # side, we can retry reimporting the same receipt later.
                        imp.rollback()
                    except Exception as ee:
                        raise Exception(
                            f"Could not roll back transaction of imported {ref.arg}: {ee}"
                        ) from ee
                    raise Exception(f"Could not remove {ref.arg} from folder: {e}") from e
            else:
                try:
                    ref.path.unlink()
                except Exception as e:
                    # The organized copy was written, but the source could not be
                    # removed; roll back (deleting the newly filed copy and
                    # restoring the ledger) so the user's original survives for a
                    # clean re-run of the same command.
                    try:
                        imp.rollback()
                    except Exception as ee:
                        raise Exception(
                            f"Could not roll back transaction of imported {ref.arg}: {ee}"
                        ) from ee
                    raise Exception(f"Could not remove {ref.arg}: {e}") from e

    with tempfile.TemporaryDirectory() as tmpdir:
        preview_dir = Path(tmpdir)

        exceptions: list[tuple[str, Exception]] = []
        for ref in refs:
            try:
                do_ingest_one(ref, preview_dir)
            except Exception as e:
                exceptions.append((ref.arg, e))
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
        help="One or more receipt filenames (documents store) or paths to local files (if none are present, all store receipts are processed)",
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
