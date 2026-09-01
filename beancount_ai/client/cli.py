#!/usr/bin/env python3
"""bean-ai — CLI wrapper that talks to bean-ai-server on `pim` via qrexec.

Config is read from ~/.config/bean-ai.json.
"""

import argparse
import difflib
import json
import os
import pprint
import re
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from io import StringIO
from pathlib import Path
from textwrap import indent
from traceback import print_exception
from typing import IO, Any, Literal, cast

from colorama import Fore, Style  # type: ignore

from .beancount_loader import MatchResults, load_transaction_contexts  # type:ignore
from .config import BeancountConfiguration, Configuration


def demarkdownify(llm_output: str) -> str:
    llm_output_lines = llm_output.splitlines(True)
    if llm_output_lines[0].startswith("```"):
        llm_output_lines = llm_output_lines[1:]
    if llm_output_lines[-1].startswith("```"):
        llm_output_lines = llm_output_lines[:-1]
    return "".join(llm_output_lines)


def shorten_fn(folder: str | Path, fn: str) -> str:
    """Reduce max path length without affecting the file name extension."""
    maxlen = os.pathconf(folder, "PC_NAME_MAX")
    # Sarn, we only handle UTF-8 file systems.  Maybe this would be good to fix in the future.
    while len(fn.encode("utf-8")) > maxlen:
        n, e = os.path.splitext(fn)
        n = n[:-1]
        fn = n + e
    return fn


class BadJSON(json.decoder.JSONDecodeError):
    def __str__(self) -> str:
        return json.decoder.JSONDecodeError.__str__(self) + "\nText:\n" + (self.doc)


def load_json(s: str | bytes) -> Any:
    try:
        return json.loads(s)
    except json.decoder.JSONDecodeError as e:
        raise BadJSON(e.msg, s if isinstance(s, str) else s.decode("utf-8"), e.pos)


# -- qrexec transport ------------------------------------------------------

# From inside a VM, IPC to another VM uses:
#   qrexec-client-vm <target_vm> <action_name> [rpc_client] [args...]
# Only stdin / stdout are relayed between client and server — the RPC action name
# determines *which* program on the target VM is invoked (registered via dom0 policy).


def stream_reasoning_and_capture_output(stdout: IO[bytes]) -> str:
    accumulated: list[str] = []

    reasoning_over = False
    for line in stdout:
        msg = load_json(line)

        if err := msg.get("error"):
            print(err, file=sys.stderr)
            break
        elif msg.get("finish"):
            break
        elif msg.get("reasoning"):
            sys.stderr.write(Fore.CYAN)
            sys.stderr.write(msg["reasoning"])
            sys.stderr.write(Style.RESET_ALL)
            sys.stderr.flush()
        elif msg.get("output"):
            if not reasoning_over:
                sys.stderr.write("\n")
                sys.stderr.flush()
                reasoning_over = True
            accumulated.append(msg["output"])
        else:
            assert 0, msg

    return "".join(accumulated).strip()


class RemoteVM:
    def __init__(self, target_vm: str | None):
        self.target_vm = target_vm

    @classmethod
    def from_cfg(cls, cfg: Configuration) -> "RemoteVM":
        return cls(cfg.target_vm)

    def _call(
        self,
        action: str,
        arg: str | None = None,
    ) -> tuple[list[str], subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        """Start a remote process and return its Popen handle (with all streams already connected)."""
        if arg is not None:
            # arguments must be hex
            arg = arg.encode("utf-8").hex()

        # Local fallback for testing: when target_vm is None, invoke bean-ai-server directly.
        if self.target_vm is None:
            cmd = ["bean-ai-server", "--config", str(Configuration.cfg_path)]
            if arg is not None:
                cmd.extend([action, arg])
            else:
                cmd.append(action)
        else:
            if arg is not None:
                action = f"{action}+{arg}"
            cmd = ["qrexec-client-vm", str(self.target_vm), action]

        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        assert proc.stdin is not None
        assert proc.stdout is not None
        return cmd, proc, proc.stdin, proc.stdout

    def list_receipts(
        self, category: Literal["unassociated"] | Literal["uningested"]
    ) -> list[str]:
        """Return receipt filenames from the server.

        Raises on qrexec transport error; prints to stderr and returns ``[]``
        when the JSON cannot be decoded.
        """

        cmd, proc, stdin, stdout = self._call(
            "beanai.List"
            + ("Uningested" if category == "uningested" else "Unassociated")
        )
        stdin.close()

        read_data = stdout.read()
        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        data = load_json(read_data)
        receipts = cast(list[str], data["receipts"])
        mm = [os.path.basename(x) for x in receipts]
        if mm != receipts:
            raise Exception(
                f"The document store returned non-base paths when listing receipts: {data['receipts']}"
            )
        return receipts

    def help_associate_receipt(
        self, filename: str
    ) -> tuple[list[str], subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        """
        Calls upon the LLM on the server side to produce a Beancount transaction
        and the main payment account.
        """
        cmd, proc, stdin, stdout = self._call(
            "beanai.HelpAssociateReceipt", arg=filename
        )
        # FIXME caller of this rawdogs it, but the comms logic should be encapsulated in a class later.
        return cmd, proc, stdin, stdout

    def process_receipt(
        self, filename: str, account_list: list[str]
    ) -> tuple[str, str]:
        """
        Calls upon the LLM on the server side to produce a Beancount transaction
        and the main payment account.
        """
        cmd, proc, stdin, stdout = self._call("beanai.Process", arg=filename)
        acctlist = json.dumps(account_list).encode("utf-8")
        stdin.write(acctlist)
        stdin.close()

        llm_output = stream_reasoning_and_capture_output(stdout)
        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        llm_output_original = llm_output

        # Remove Markdown quote formatting from JSON output.
        llm_output = demarkdownify(llm_output)

        # Fish out first account in the payment accounts list.
        data = load_json(llm_output)

        try:
            payment_account = data["payment_accounts"][0]
        except Exception as e:
            raise Exception(
                f"Could not retrieve expense account from LLM output: {e}\n{llm_output_original}"
            )

        try:
            transaction = data["transaction"]
        except Exception as e:
            raise Exception(
                f"Could not retrieve Beancount transaction from LLM output: {e}\n{llm_output_original}"
            )

        return transaction, payment_account

    def fetch_receipt(self, filename: str) -> bytes:
        cmd, proc, stdin, stdout = self._call("beanai.Fetch", arg=filename)
        stdin.close()

        raw = stdout.read()
        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        return raw

    def remove_receipt(self, filename: str) -> None:
        cmd, proc, stdin, _ = self._call("beanai.Remove", arg=filename)
        stdin.close()

        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)


def predict_receipt_destination_path(
    beancount_folder: Path,
    transaction_date: date,
    filename: str,
    account: str,
    description: str | None = None,
) -> Path:
    """
    Organize a receipt file into an account folder.

    For receipts to be recognized as documents in Beancount, their filename has
    the requirement that it must begin with a date in Y-m-d format.  Hence
    the requisite transaction date at the beginning of the file name.
    """
    # Construct the final destination folder.  Account folders reside directly under `beancount_folder`.
    receipt_dir = beancount_folder / account.replace(":", "/")
    receipt_dir.mkdir(parents=True, exist_ok=True)
    if description:
        fn = transaction_date.strftime("%Y-%m-%d.") + description + " — " + filename
    else:
        fn = transaction_date.strftime("%Y-%m-%d.") + filename

    # No slashes in the file name, please.
    fn = fn.replace("/", "_")

    fn = shorten_fn(receipt_dir, fn)

    receipt_path = receipt_dir / fn

    return receipt_path


def organize_receipt(
    beancount_folder: Path,
    vm: RemoteVM,
    transaction_date: date,
    filename: str,
    account: str,
    description: str | None = None,
) -> Path:
    """
    Organize a receipt file into an account folder.

    For receipts to be recognized as documents in Beancount, their filename has
    the requirement that it must begin with a date in Y-m-d format.  Hence
    the requisite transaction date at the beginning of the file name.
    """
    receipt_path = predict_receipt_destination_path(
        beancount_folder, transaction_date, filename, account, description
    )
    raw = vm.fetch_receipt(filename)
    receipt_path.write_bytes(raw)
    return receipt_path


def insert_document_metadata(transaction_text: str, file_path: str) -> str:
    """
    Takes a Beancount transaction and inserts the `file_path` as a document metadata entry.
    """
    lines = transaction_text.splitlines(True)
    if not lines or lines[0].strip().startswith("#"):
        return transaction_text
    stripped = lines[1].lstrip()
    indent = lines[1][: len(lines[1]) - len(stripped)]
    lines.insert(1, '{}document: "{}"\n'.format(indent, file_path.replace('"', '\\"')))
    return "".join(lines)


# Use numbered document keys (document, document2, document3, ...) so each associated
# receipt gets its own metadata key and the newest one is always just `document:`.
def update_document_metadata(line_no: int, tx_lines: list[str], new_doc: str) -> str:  # noqa: C901
    """Add or replace a document metadata entry after the date line.

    Arguments:
      line_no: the (zero-based index of the) line containing the first non-date line of the transaction
      tx_lines: the document contents as a list of lines (keeping line endings)
      new_doc: which document to add as a document: tag

    The newest doc is always ``document:`` (first in the block).  Any existing
    ``document:`` / ``documentN:`` entries are preserved but renumbered to
    ``document2:``, ``document3:``, … -- old numbering is ignored.
    """
    metadata_start = line_no  # first index AFTER the date/payee line
    assert metadata_start < len(tx_lines)

    initial_whitespace_regex = re.compile(r"^(\s+)")

    # 2. Scan from metadata_start collecting doc entries; stop at empty line or non-doc.
    pre_indent = initial_whitespace_regex.match(tx_lines[metadata_start])
    if not pre_indent:
        raise ValueError(
            f"supplied line {metadata_start} corresponding to line {tx_lines[metadata_start]} is not part of a transaction"
        )
    pre_str = pre_indent.group(1)

    transaction_document_metadata_regex = re.compile(
        "^" + pre_str + r"document(\d*):(.+)"
    )
    transaction_metadata_key_regex = re.compile(
        "^" + pre_str + r"([a-z][a-zA-Z0-9_-]*):"
    )

    # Locate transaction contents.
    j = metadata_start
    while j < len(tx_lines):
        ln = tx_lines[j]
        if not ln.strip():  # blank line → stop scanning
            break
        if ln.strip().startswith(";"):  # comment line → stop scanning
            break
        # Accept any valid Beancount metadata key.
        m_meta = transaction_metadata_key_regex.match(ln)
        if m_meta:
            j += 1
            continue
        # Line doesn't match a known metadata key. If it starts with whitespace,
        # it may be a posting — scan past it. If not, we've left the transaction.
        if ln.strip() and len(ln) > 0 and ln[0].isspace():
            j += 1
        else:
            break

    # 1. Keep lines before metadata_start unchanged.
    before: list[str] = list(tx_lines[:metadata_start])
    # 2. Pick out the transaction we will process.
    transaction = tx_lines[metadata_start:j]
    # 3. Keep everything after the transaction intact.
    after = tx_lines[j:]

    # (original_line_index, document number (or empty), metadata value with quotes)
    doc_entries: list[tuple[int, int, str]] = []
    insert_position = None
    for x, ln in enumerate(transaction):
        # Verify if this line matches the Beancount document metadata key.
        if m_doc := transaction_document_metadata_regex.match(ln):
            doc_entries.append(
                (
                    x,
                    1 if "" == m_doc.group(1) else int(m_doc.group(1)),
                    m_doc.group(2),
                )
            )
            if insert_position is None:
                insert_position = x

    if insert_position is None:
        # First document of the transaction!  Simple insert.
        transaction.insert(0, pre_str + "document: " + f'"{new_doc}"\n')
    else:
        # reserve the "document 1" number for the one I will be inserting soon
        numbers_taken = {1}
        for lineno, docnumber, docdata in sorted(
            doc_entries, key=lambda entry: entry[1]
        ):
            # Look for the next highest number available.
            while docnumber in numbers_taken:
                docnumber = docnumber + 1
            transaction[lineno] = (
                pre_str
                + f"document{docnumber}:"
                + docdata
                + ("" if docdata.endswith("\n") else "\n")
            )
            numbers_taken.add(docnumber)
        transaction.insert(insert_position, pre_str + "document: " + f'"{new_doc}"\n')

    return "".join(before + transaction + after)


def _preview_receipt(cfg: Configuration, filename: str, preview_dir: Path) -> None:
    dest_path = preview_dir / filename
    dest_path.write_bytes(RemoteVM.from_cfg(cfg).fetch_receipt(filename))
    subprocess.Popen(
        ["xdg-open", str(dest_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


class ImportResult:
    receipt_data: bytes
    transaction_text: str
    receipt_destination_path: Path
    ingestion_destination_path: Path
    rollback_size: int | None = None

    def __init__(
        self,
        vm: RemoteVM,
        beancount: BeancountConfiguration,
        filename: str,
    ) -> None:
        receipt_data = vm.fetch_receipt(filename)

        beancount_transaction, account = vm.process_receipt(
            filename, beancount.account_list_file.read_text().splitlines()
        )
        # Strip headline comments and newlines from the transaction.
        while beancount_transaction.lstrip().startswith(";"):
            beancount_transaction = "".join(
                beancount_transaction.splitlines(True)[1:]
            ).lstrip()

        datestr, reststr = beancount_transaction.split(" ", 1)
        # Take the text after the date, remove the transaction flag and the space next to it,
        # then use the payee and narration to construct a description for the receipt file name.
        # If there is a comment at the end of the line, strip it too.
        reststr = (
            reststr.splitlines()[0][2:]
            .replace('" "', " — ")
            .replace('"', "")
            .split(";")[0]
            .strip()
        )
        # Take the text containing the date, and make a date for the receipt file name.
        transdate = datetime.strptime(datestr, "%Y-%m-%d").date()

        receipt_path = predict_receipt_destination_path(
            beancount.main_folder, transdate, filename, account, reststr
        )
        formatted_tx = insert_document_metadata(
            beancount_transaction, str(receipt_path)
        )

        self.receipt_data = receipt_data
        self.transaction_text = formatted_tx
        self.receipt_destination_path = receipt_path
        self.ingestion_destination_path = beancount.ingestion_destination_path

    def _formatted_transaction_text(self, lastchar: str) -> str:
        """
        Format a transaction based on the last character of the file it will be added to.

        Caller supplies said last character(s).
        """
        return (
            ("\n" if lastchar.endswith("\n") else "\n\n")
            + self.transaction_text.strip()
            + "\n"
        )

    def diff(self) -> list[str]:
        """Print a unified diff of what would be appended to the ingestion file."""
        dest = self.ingestion_destination_path
        current = dest.read_text(encoding="utf-8") if dest.exists() else ""
        old_lines = current.splitlines(True) if current else []
        appended = self._formatted_transaction_text(current)
        new_lines = (current + appended).splitlines(True)
        diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=str(dest),
                tofile=str(dest),
                n=5,
            )
        )
        return diff

    def commit(self) -> None:
        dest = self.ingestion_destination_path
        receipt_path = self.receipt_destination_path

        try:
            # Write the receipt data.
            receipt_path.write_bytes(self.receipt_data)
            print(
                f"The receipt has been filed under {receipt_path}",
                file=sys.stderr,
            )

            with open(self.ingestion_destination_path, "a+") as f:
                self.rollback_size = self.ingestion_destination_path.stat().st_size
                # no marker line is necessary, the transaction has a link to the document in it.
                # f.write("\n; {} imported by bean-ai.\n".format(args.filename))
                if f.tell() > 0:
                    f.seek(f.tell() - 1)
                lastchar = f.read()
                f.write(self._formatted_transaction_text(lastchar))
                f.flush()

                print(
                    f"The transaction has been imported to {dest}",
                    file=sys.stderr,
                )
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        eee: Exception | None = None

        if self.receipt_destination_path.exists():
            try:
                self.receipt_destination_path.unlink()
            except Exception as e:
                eee = e
                print(
                    f"The receipt {self.receipt_destination_path} could not be deleted as part of the transaction rollback",
                    file=sys.stderr,
                )

        if self.rollback_size is not None:
            try:
                os.truncate(self.ingestion_destination_path, self.rollback_size)
                self.rollback_size = None
            except Exception as e:
                eee = e
                print(
                    f"The transaction written to {self.ingestion_destination_path} could not be rolled back",
                    file=sys.stderr,
                )

        if eee is not None:
            raise eee


def do_list_uningested(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Lists uningested receipt files from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    for fname in RemoteVM.from_cfg(cfg).list_receipts("uningested"):
        print(fname)


def do_list_unassociated(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Lists receipt files yet to be associated to transactions from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    for fname in RemoteVM.from_cfg(cfg).list_receipts("unassociated"):
        print(fname)


def do_fetch(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Fetches a receipt file from the server and saves it to the file specified in the arguments.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    gotten = RemoteVM.from_cfg(cfg).fetch_receipt(args.filename)
    Path(args.destination).write_bytes(gotten)


def do_remove(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Removes a receipt file from the server.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    RemoteVM.from_cfg(cfg).remove_receipt(args.filename)


def do_process(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Processes a receipt file and produces the output of the LLM to stdout.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    try:
        llm_output, account = RemoteVM.from_cfg(cfg).process_receipt(
            args.filename, cfg.beancount.account_list_file.read_text().splitlines()
        )
    except subprocess.CalledProcessError as e:
        sys.exit(e.returncode)

    print(llm_output)
    print(f"Main account: {account}")


def print_diff(diff: list[str]) -> None:
    print("--- Changes ---", file=sys.stdout)
    for line in diff:
        if line.startswith("-"):
            line = Fore.RED + line + Style.RESET_ALL
        elif line.startswith("+"):
            line = Fore.GREEN + line + Style.RESET_ALL
        sys.stdout.write(line)


def do_import(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Imports a receipt by creating a Beancount transaction for it, copying the document to
    the appropriate Beancount document folder, then writing the Beancount transaction to
    the designated transactions file while associating the transaction with the document.

    Exits on success, and if errors are encountered, exits with a non-zero error code.
    """
    result = ImportResult(RemoteVM.from_cfg(cfg), cfg.beancount, args.filename)

    diff = result.diff()
    if diff:
        print_diff(diff)

    result.commit()


def do_ingest(cfg: Configuration, args: argparse.Namespace) -> None:  # noqa: C901
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
                    _preview_receipt(cfg, receipt, preview_dir)
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
                do_remove(cfg, argparse.Namespace(filename=receipt))
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


def do_organize(cfg: Configuration, args: argparse.Namespace) -> None:
    """
    Copies a receipt to the designated folder for the account under the Beancount folder.

    See `predict_receipt_destination_path` for requirements imposed on Beancount document
    file naming.
    """
    tdate = datetime.strptime(args.date, "%Y-%m-%d").date()
    receipt_path = organize_receipt(
        cfg.beancount.main_folder,
        RemoteVM.from_cfg(cfg),
        tdate,
        args.filename,
        args.account,
    )
    print("The file has been organized into", str(receipt_path), file=sys.stderr)


def find_transaction_in_file(file_path: Path, transaction_text: str) -> int | None:
    """Find the line number of *transaction_text* in *file_path*.

    Returns the 1-based line number where the transaction starts, or ``None``.
    """
    content = file_path.read_text(encoding="utf-8")
    lines = content.splitlines(True)
    target_lines = [ln for ln in transaction_text.splitlines(True) if ln.strip()]
    if not target_lines:
        return None

    first_line_stripped = target_lines[0].strip()
    for i, _ in enumerate(lines):
        if i + len(target_lines) > len(lines):
            break
        if lines[i].strip() == first_line_stripped:
            # Verify the rest of the transaction matches.
            match = True
            for j, target_line in enumerate(target_lines[1:]):
                stripped_target = target_line.strip()
                if not stripped_target:
                    continue
                if i + 1 + j >= len(lines):
                    match = False
                    break
                if lines[i + 1 + j].strip() != stripped_target:
                    match = False
                    break
            if match:
                return i + 1  # 1-based
    return None


def do_associate(cfg: Configuration, args: argparse.Namespace) -> None:  # noqa: C901
    """Associate a receipt with an existing Beancount transaction.

    Flow:
      1. Process the receipt → get date + amount from LLM.
      2. Query Beancount for candidates within -1/+45 days.
      3. Send candidates to beanai.MatchCandidates on server.
      4. If unambiguous, auto-select + update document metadata.
      5. If ambiguous, present ranked list to user.
      6. Organize the receipt file.
    """
    vm = RemoteVM.from_cfg(cfg)
    receipts = vm.list_receipts("unassociated")

    if args.filename:
        for fn in args.filename:
            if fn not in receipts:
                print(f"Receipt {fn} does not exist on server.", file=sys.stderr)
                sys.exit(1)
        # All specified receipts exist on the server.  Let's override
        # the list with what the user sent us.
        receipts = args.filename

    if not receipts:
        print("No receipts to associate.", file=sys.stderr)
        return

    def do_associate_one(receipt: str, preview_dir: Path) -> None:  # noqa: C901
        # Step 1: Process the receipt via LLM (existing flow).
        try:
            cmd, proc, stdin, stdout = vm.help_associate_receipt(receipt)
        except subprocess.CalledProcessError as e:
            raise Exception(f"Error processing receipt: {e}") from e

        llm_output = demarkdownify(stream_reasoning_and_capture_output(stdout))

        receipt_info = load_json(llm_output)

        try:
            receipt_date = datetime.strptime(receipt_info["date"], "%Y-%m-%d").date()
        except KeyError:
            receipt_date = None
        try:
            amt_cur = cast(str, receipt_info["amount"])
        except KeyError:
            amt_cur = None

        if receipt_date:
            print(f"Receipt date: {receipt_date.isoformat()}", file=sys.stderr)
        else:
            print("No date in receipt", file=sys.stderr)
        if amt_cur:
            print(f"Receipt amount: {amt_cur}", file=sys.stderr)

        # Step 2: Get candidates from Beancount.
        if receipt_date:
            start_date = receipt_date - timedelta(days=1)
            end_date = receipt_date + timedelta(
                days=45
            )  # 45 days ought to be good for receipts paid up to a month later

            try:
                _, contexts = load_transaction_contexts(
                    str(cfg.beancount.main_file), start_date, end_date
                )
            except Exception as e:
                raise Exception(f"Error loading candidates from Beancount: {e}") from e
        else:
            assert 0, "Date for receipt could not be deduced."

        print(f"Found {len(contexts)} candidate transactions.", file=sys.stderr)

        candidates_data = [
            ctx.__dict__ if hasattr(ctx, "__dict__") else ctx for ctx in contexts
        ]
        # Write candidates JSON to the server.
        candidates_raw = json.dumps(candidates_data).encode("utf-8")
        stdin.write(candidates_raw)
        stdin.flush()
        stdin.close()

        llm_output = demarkdownify(stream_reasoning_and_capture_output(stdout))
        stdout.close()

        ret = proc.wait()
        if ret != 0:
            raise subprocess.CalledProcessError(ret, cmd)

        resp = cast(MatchResults, load_json(llm_output))

        matches = resp.get("matches", [])
        if not matches:
            print(f"No valid matches found for receipt {receipt}.", file=sys.stderr)
            return

        # Step 4 & 5: Interpret results.
        is_ambiguous = resp.get("ambiguous", False) or (
            len(matches) > 0 and matches[0].get("score", 0) < 0.8
        )

        if is_ambiguous:
            raise Exception(
                f"sorry, matches are ambiguous, cannot proceed; list of matches:\n{pprint.pformat(matches)}"
            )

            # Present ranked list to user.  This is dead code for now, but we will enable it in the future
            # when more testing has taken place.
            print("\nRanked candidates (select by index):", file=sys.stderr)
            for candidate_match in matches[:5]:  # show top 5
                print("candidate:", file=sys.stderr)
                print(pprint.pformat(candidate_match), file=sys.stderr)
            #     idx = candidate_match["index"]
            #     score = candidate_match.get("score", 0)
            #     ctx = contexts[idx]

            #     amount_str = (
            #         f"{ctx['paid_amount']} {ctx['paid_currency']}"
            #         if ctx.get("paid_amount")
            #         else "N/A"
            #     )
            #     payee = ctx.get("payee") or "N/A"
            #     narration = (ctx.get("narration") or "N/A").ljust(30)

            #     print(
            #         f"  [{idx}] score={score:.2f} {ctx['date_str']}  {str(payee).ljust(25)}  {narration}{amount_str:>15}",
            #         file=sys.stderr,
            #     )

            # while True:
            #     try:
            #         choice = input("\nSelect candidate (index, or 'r' to retry): ")
            #         if choice.strip().lower() == "r":
            #             print(
            #                 "Retrying...", file=sys.stderr
            #             )  # TODO: actually reload and re-match
            #             pass  # stay in loop

            #         selected_idx = int(choice.strip())
            #         if 0 <= selected_idx < len(contexts):
            #             is_ambiguous = False
            #             break
            #         else:
            #             print(
            #                 f"Invalid index. Use [0-{len(contexts) - 1}].", file=sys.stderr
            #             )
            #     except (ValueError, EOFError):
            #         print("Please enter a valid number.", file=sys.stderr)
            return

        selected_match_index = 0
        selected_match = matches[selected_match_index]

        selected_txes = [
            tx
            for tx in contexts
            if tx.line_no == selected_match["line_no"]
            and tx.source_file == selected_match["source_file"]
        ]

        if len(selected_txes) > 1:
            raise Exception(
                f"Multiple transactions fit selected transaction match produced by LLM: {selected_match}"
            )
        elif not selected_txes:
            raise Exception(
                f"No transactions fit selected transaction match produced by LLM: {selected_match}"
            )

        selected_tx = selected_txes[0]

        # # We won't be generating descriptions for now.
        # description: str | None = None
        if selected_tx.narration and selected_tx.narration not in ["EFT payment"]:
            description = selected_tx.narration
        elif selected_tx.payee:
            description = selected_tx.payee
        else:
            description = None

        if amt_cur:
            if description is None:
                description = amt_cur
            else:
                description = description + f", {amt_cur}"

        # Step 6: Download + organize receipt.
        receipt_path = predict_receipt_destination_path(
            cfg.beancount.main_folder,
            receipt_date,
            receipt,
            selected_tx.crediting_account,
            description=description,
        )

        # Step 7: Update document metadata.
        tx_file = Path(selected_tx.source_file)
        line_no = selected_tx.line_no

        if not tx_file.exists():
            raise Exception(
                f"Warning: Transaction source file '{tx_file}' does not exist. Cannot update metadata."
            )

        # Read the transaction text and update it.
        all_lines = tx_file.read_text(encoding="utf-8").splitlines(True)

        if line_no > len(all_lines):
            raise Exception(
                f"Error: line number {line_no} exceeds file length ({len(all_lines)})."
            )

        new_content = update_document_metadata(
            line_no,
            all_lines,
            str(receipt_path),
        )

        old_lines = all_lines
        new_lines_txt = (
            new_content.rstrip("\n") + "\n"
            if not new_content.endswith("\n")
            else new_content
        )
        new_lines = new_lines_txt.splitlines(True)

        diff = list(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=str(tx_file),
                tofile=str(tx_file),
                n=5,
            )
        )
        if diff:
            print_diff(diff)

        if not args.no and not args.yes:
            while True:
                print(
                    f"\nSave proposed changes to '{tx_file}' and import {receipt}? [y]es / [n]o / [p]review receipt / [q]uit ",
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
                    _preview_receipt(cfg, receipt, preview_dir)
                    continue  # re-prompt for the same receipt

                if answer != "y":
                    return

                break  # leave prompt loop after y or n

        if args.no:
            print(f"Skipping changes to {tx_file} (--no requested)", file=sys.stderr)
            return

        # Download the receipt and save it organized.
        raw_bytes = vm.fetch_receipt(receipt)
        receipt_path.write_bytes(raw_bytes)

        print(f"Receipt saved to {receipt_path}", file=sys.stderr)

        tx_file.write_text(new_content, encoding="utf-8")
        print(
            f"Updated document metadata on line {line_no} of {tx_file}", file=sys.stderr
        )

        vm.remove_receipt(receipt)

    with tempfile.TemporaryDirectory() as tmpdir:
        preview_dir = Path(tmpdir)

        exceptions: list[tuple[str, Exception]] = []
        for receipt in receipts:
            try:
                do_associate_one(receipt, preview_dir)
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

    sp.add_parser(
        "list-unassociated",
        help="Get receipt filenames to associate with transactions (plain text)",
    )
    sp.add_parser(
        "list-uningested",
        help="Get receipt filenames to import as transactions (plain text)",
    )

    process_cmd = sp.add_parser(
        "process", help="Process a receipt image and produce Beancount output from it"
    )
    process_cmd.add_argument("filename", help="Filename of the receipt")

    fetch_cmd = sp.add_parser("fetch", help="Fetch a receipt file from the server")
    fetch_cmd.add_argument("filename", help="Filename of the receipt")
    fetch_cmd.add_argument("destination", help="Local path to save the retrieved file")

    rm_cmd = sp.add_parser("remove", help="Delete a receipt file")
    rm_cmd.add_argument("filename", help="Filename of the receipt file")

    org_cmd = sp.add_parser(
        "organize", help="File a copy of a receipt under a payment account"
    )
    org_cmd.add_argument("filename", help="Filename of the receipt")
    org_cmd.add_argument("date", help="Date to impute to receipt file", type=str)
    org_cmd.add_argument("account", help="Payment account (e.g. Assets:Cash:CHF)")

    imp_cmd = sp.add_parser(
        "import",
        help="Like ingest, but receipt in server is left alone instead of deleted",
    )
    imp_cmd.add_argument("filename", help="Filename of the receipt")

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

    assoc_cmd = sp.add_parser(
        "associate", help="Associate a receipt with an existing Beancount transaction"
    )
    assoc_cmd.add_argument(
        "filename",
        help="One or more receipt filename (if none are present, all are processed)",
        nargs="*",
    )
    yes_group = assoc_cmd.add_mutually_exclusive_group()
    yes_group.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        dest="yes",
        help="Make changes without confirmation (equivalent to answering 'yes' to every prompt)",
    )
    yes_group.add_argument(
        "--no",
        "-n",
        action="store_true",
        default=False,
        dest="no",
        help="Show the changes that would be made but make none (equivalent to answering 'no' to every prompt)",
    )

    return ap


def main() -> None:
    ap = build_parser()
    args = ap.parse_args()

    cfg = Configuration.load(args.conf_path)

    if not args.command:
        ap.print_help(sys.stderr)
        sys.exit(1)

    dispatch = {
        "list-unassociated": do_list_unassociated,
        "list-uningested": do_list_uningested,
        "fetch": do_fetch,
        "import": do_import,
        "ingest": do_ingest,
        "organize": do_organize,
        "process": do_process,
        "remove": do_remove,
        "associate": do_associate,
    }
    dispatch[args.command](cfg, args)


if __name__ == "__main__":
    main()
