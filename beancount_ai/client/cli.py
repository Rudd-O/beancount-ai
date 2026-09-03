#!/usr/bin/env python3
"""bean-ai — CLI wrapper that talks to bean-ai-server on `pim` via qrexec.

Config is read from ~/.config/bean-ai.json.
"""

import argparse
import base64
import difflib
import itertools
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

from beancount_ai.client.beancount_loader import (  # type:ignore
    MatchResults,
    load_transaction_contexts,
)
from beancount_ai.client.config import BeancountConfiguration, Configuration
from beancount_ai.structs import RefineRequest, RefineRequestDocument


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


def stream_reasoning_and_capture_output(stdout: IO[bytes]) -> str:
    accumulated: list[str] = []

    reasoning_over = False
    for line in stdout:
        msg = load_json(line)

        if msg.get("finish"):
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


# -- qrexec transport ------------------------------------------------------

# From inside a VM, IPC to another VM uses:
#   qrexec-client-vm <target_vm> <action_name> [rpc_client] [args...]
# Only stdin / stdout are relayed between client and server — the RPC action name
# determines *which* program on the target VM is invoked (registered via dom0 policy).


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
        # FIXME these things should be context managers, actually.  Yield the useful stuff,
        # then when the context is exited, if the command failed, raise an error.
        return cmd, proc, stdin, stdout

    def refine(self) -> tuple[list[str], subprocess.Popen[bytes], IO[bytes], IO[bytes]]:
        """
        Calls upon the LLM on the server side to produce a Beancount transaction
        and the main payment account.
        """
        cmd, proc, stdin, stdout = self._call("beanai.Refine")
        # FIXME caller of this rawdogs it, but the comms logic should be encapsulated in a class later.
        # FIXME these things should be context managers, actually.  Yield the useful stuff,
        # then when the context is exited, if the command failed, raise an error.
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


# FIXME: consider simply using the other function below that does this, so we can delete it.
# If deleted, update spec docs.
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


def split_into_transactions_by_range(  # noqa: C901
    tx_lines: list[str],
    start_line: int,
    end_line: int | None = None,
) -> list[tuple[bool, list[str]]]:
    """Split at the given range the supplied list of lines (Beancount data) into transaction / non-transaction groups.

    Arguments:
      tx_lines:   the document contents as a list of lines (with line endings as present in the source file)
      start_line: the (zero-based index of the) first line to start looking for transactions;
                  the function will intelligently look backwards to the start of a transaction if
                  this index points into the middle of one.
      end_line:   the (zero-based index of the) last line at which a transaction may begin;
                  a transaction that starts at or before this line is included whole, even if
                  its lines extend past it, but a transaction beginning after this line is not.
                  If not specified, end_line defaults to start_line, which means that only the
                  transaction containing start_line (located by looking backwards) is returned;
                  later transactions are not flagged.

    Returns:
      A list of tuples where each item is:
        (True, lines): a list of lines corresponding to a transaction found
        (False, lines): a list of lines that do not belong to any transaction

    Comments above or below a transaction are not considered part of the transaction in this iteration of the code.

    >>> data = \"""
    ... 2026-01-01 * "Beans"
    ...   Expenses:Beans 1000 CHF
    ...   Assets:Bank
    ...
    ... 2026-01-02 * "More beans"
    ...   Expenses:Beans 500 CHF
    ...   Assets:Bank
    ...
    ... 2026-01-03 balance Assets:Bank 15400000 CHF
    ... \""".splitlines(True)
    >>> split_into_transactions_by_range(data, 1)[1][1]
    ['2026-01-01 * "Beans"\\n', '  Expenses:Beans 1000 CHF\\n', '  Assets:Bank\\n']
    >>> split_into_transactions_by_range(data, 7,)[1][1]
    ['2026-01-02 * "More beans"\\n', '  Expenses:Beans 500 CHF\\n', '  Assets:Bank\\n']
    >>> split_into_transactions_by_range(data, 8)[0][1][-1]
    '2026-01-03 balance Assets:Bank 15400000 CHF\\n'
    >>> split_into_transactions_by_range(data, 9)[0][1][-1]
    '2026-01-03 balance Assets:Bank 15400000 CHF\\n'
    """
    if end_line is None:
        end_line = start_line
    if start_line >= len(tx_lines):
        raise ValueError(
            f"starting line number {start_line} cannot be greater than the supplied number of lines {len(tx_lines)}"
        )
    if start_line < 0:
        raise ValueError(f"starting line number {start_line} cannot be less than zero")
    if end_line >= len(tx_lines):
        raise ValueError(
            f"ending line number {end_line} cannot be greater than the supplied number of lines {len(tx_lines)}"
        )
    if end_line < 0:
        raise ValueError(f"ending line number {end_line} cannot be less than zero")
    if end_line < start_line:
        raise ValueError(
            f"start_line={start_line} must be less than or equal than end_line={end_line}"
        )

    initial_number_regex = re.compile(r"^[1-9]")
    initial_whitespace_regex = re.compile(r"^(\s+)")

    in_middle_of_transaction = False
    # Start with the first lookbehind.  Look at the current line.
    curr_line = start_line
    for curr_line in range(start_line, -1, -1):
        ln = tx_lines[curr_line]
        fields = ln.split()
        if initial_number_regex.match(ln):
            if len(fields) > 2 and len(fields[1]) == 1:
                # Found the start of the transaction.
                in_middle_of_transaction = True
                break
            else:
                # We didn't start at a transaction, so we stop here.
                break
        elif initial_whitespace_regex.match(ln):
            if ln.strip():
                # We may be in the middle of a transaction, because there appears to
                # be text starting by whitespace.  Look one line back back.
                continue
            else:
                break
        break

    intermediate: list[tuple[bool, str]] = []
    intermediate.extend((False, ln) for ln in tx_lines[:curr_line])

    for curr_line in range(curr_line, len(tx_lines)):
        ln = tx_lines[curr_line]
        fields = ln.split()
        if initial_number_regex.match(ln):
            if len(fields) > 2 and len(fields[1]) == 1:
                # Found the start of a transaction.
                if curr_line <= end_line:
                    # This start of the transaction is within the range!
                    in_middle_of_transaction = True
            else:
                in_middle_of_transaction = False
        elif initial_whitespace_regex.match(ln) and ln.strip():
            # If we were before in a transaction, we continue to be in the middle
            # of a transaction.  Look forward.
            in_middle_of_transaction = in_middle_of_transaction
        else:
            in_middle_of_transaction = False
        intermediate.append((in_middle_of_transaction, ln))

    result: list[tuple[bool, list[str]]] = []

    for is_tran, lines in itertools.groupby(intermediate, lambda m: m[0]):
        result.append((is_tran, [ln[1] for ln in lines]))
    return result


def split_at_transaction_by_line_number(  # noqa: C901
    line_no: int, tx_lines: list[str]
) -> tuple[list[str], list[str], list[str]]:
    """Take a transaction file and a line number pointer, and split the file.

    Arguments:
      line_no: the (zero-based index of the) any line part of a transaction
      tx_lines: the document contents as a list of lines (with line endings if present in the file)

    Returns:
      A tuple where each item is:
        0: all the lines belonging to transactions before the identified one
        1: all the lines belonging to the transaction pointed to by the line number
        2: all the lines belonging to the transactions after that

    Comments above or below the transaction are not considered part of the transaction in this iteration of the code.

    >>> transes = \"""
    ... 2026-01-01 * "Beans"
    ...   Expenses:Beans 1000 CHF
    ...   Assets:Bank
    ...
    ... 2026-01-02 * "More beans"
    ...   Expenses:Beans 500 CHF
    ...   Assets:Bank
    ...
    ... 2026-01-03 balance Assets:Bank 15400000 CHF
    ... \""".splitlines(True)
    >>> split_at_transaction_by_line_number(1, transes)[1]
    ['2026-01-01 * "Beans"\\n', '  Expenses:Beans 1000 CHF\\n', '  Assets:Bank\\n']
    >>> split_at_transaction_by_line_number(7, transes)[1]
    ['2026-01-02 * "More beans"\\n', '  Expenses:Beans 500 CHF\\n', '  Assets:Bank\\n']
    >>> try: split_at_transaction_by_line_number(8, transes)
    ... except ValueError: print("nope")
    nope
    >>> try: split_at_transaction_by_line_number(9, transes)
    ... except ValueError: print("nope")
    nope
    """
    res = split_into_transactions_by_range(tx_lines, line_no)
    before: list[str] = []
    transaction: list[str] = []
    after: list[str] = []
    for istran, lines in res:
        if istran:
            if transaction:
                after.extend(lines)
            else:
                transaction.extend(lines)
        else:
            if transaction:
                after.extend(lines)
            else:
                before.extend(lines)

    if not transaction:
        raise ValueError(f"line number {line_no} does not point to a transaction")

    return before, transaction, after


# Use numbered document keys (document, document2, document3, ...) so each associated
# receipt gets its own metadata key and the newest one is always just `document:`.
def update_document_metadata(line_no: int, tx_lines: list[str], new_doc: str) -> str:
    """Add or replace a document metadata entry after the date line.

    Arguments:
      line_no: the (zero-based index of the) line containing the first non-date line of the transaction
      tx_lines: the document contents as a list of lines (keeping line endings)
      new_doc: which document to add as a document: tag

    The newest doc is always ``document:`` (first in the block).  Any existing
    ``document:`` / ``documentN:`` entries are preserved but renumbered to
    ``document2:``, ``document3:``, … -- old numbering is ignored.
    """
    before, transaction, after = split_at_transaction_by_line_number(line_no, tx_lines)

    initial_whitespace_regex = re.compile(r"^(\s+)")

    pre_indent = initial_whitespace_regex.match(tx_lines[1])
    assert pre_indent, transaction[1]
    pre_str = pre_indent.group(1)

    transaction_document_metadata_regex = re.compile(
        "^" + pre_str + r"document(\d*):(.+)"
    )

    # (original_line_index, document number (or empty), metadata value with quotes)
    doc_entries: list[tuple[int, int, str]] = []
    insert_position = None
    for x, ln in enumerate(transaction):
        if x == 0:
            # Ignore the first line
            continue
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
        transaction.insert(1, pre_str + "document: " + f'"{new_doc}"\n')
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
    open_document(dest_path)


_DOCUMENT_METADATA_REGEX = re.compile(r'^\s*document(\d*):\s*"([^"]+)"')


def extract_document_paths(tx_block: list[str]) -> list[str]:
    """Return the paths of all ``document`` / ``documentN:`` entries in a transaction block.

    Scans each line for the canonical ``document`` metadata form (a ``document``
    or ``documentN`` key followed by a quoted path) and returns the extracted
    paths as a deduplicated list preserving first-seen order.
    """
    paths: list[str] = []
    seen: set[str] = set()
    for ln in tx_block:
        m = _DOCUMENT_METADATA_REGEX.match(ln)
        if m:
            path = m.group(2)
            if path not in seen:
                seen.add(path)
                paths.append(path)
    return paths


def resolve_local_document_path(doc_path: str, tx_file: Path) -> Path:
    """Resolve a ``document:`` value to a client-local path.

    Non-absolute paths resolve relative to the document referencing them.
    """
    tx_dir = tx_file.parent
    p = Path(os.path.join(tx_dir, doc_path))
    return p


def open_document(dest_path: Path) -> None:
    subprocess.Popen(
        ["xdg-open", str(dest_path)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def preview_local_document(doc_path: str, tx_file: Path, cfg: Configuration) -> None:
    """Open a client-local, already-linked document in the user's default viewer."""
    resolved = resolve_local_document_path(doc_path, tx_file)
    open_document(resolved)


_TX_HEADER_REGEX = re.compile(r"^\d{4}-\d{2}-\d{2} [*!D]\s")


def validate_refined_transaction(rewritten: str) -> bool:
    """Check the structural invariants a refined transaction block must satisfy.

    A valid block has a Beancount date + flag header on its first line and at
    least two posting lines (so the transaction remains well-formed).  Posting
    lines are the two-space-indented account lines (not the four-space-indented
    metadata beneath them, nor comments).
    """
    lines = rewritten.splitlines()
    if not lines or not _TX_HEADER_REGEX.match(lines[0]):
        return False
    postings = 0
    for ln in lines[1:]:
        if re.match(r"^\s\s\S", ln) and not ln.lstrip().startswith(";"):
            postings += 1
    return postings >= 2


def do_refine(cfg: Configuration, args: argparse.Namespace) -> None:  # noqa: C901
    """Refine an existing Beancount transaction using its linked documents.

    The user points at a transaction by file path and 1-based line number.  The
    client extracts the transaction block, gathers the client-local documents
    linked in its metadata, sends them (base64-encoded) to the server over the
    standard transport, and asks the LLM for a rewritten transaction.  A colored
    diff is shown and, depending on the flags / the user's answer, the change is
    applied (replacing only the target transaction block) or discarded.
    """
    # 1. Read the file.
    tx_file = Path(args.file_path)
    if not tx_file.exists():
        print(f"Error: file not found: {tx_file}", file=sys.stderr)
        sys.exit(1)
    all_lines = tx_file.read_text(encoding="utf-8").splitlines(True)

    # 2. Extract the transaction block, preserving all formatting.
    #    The helper takes a zero-based index and accepts any line within a tx.
    try:
        before, tx_block, after = split_at_transaction_by_line_number(
            args.line_number - 1, all_lines
        )
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    tx_block_str = "".join(tx_block)  # exact original text

    # 3. Discover linked documents (document / documentN metadata).
    doc_paths = extract_document_paths(tx_block)

    # 4. Collect document contents (client-local, resolved near the tx file).
    documents_data: list[RefineRequestDocument] = []
    for doc_path in doc_paths:
        resolved = resolve_local_document_path(doc_path, tx_file)
        try:
            raw = resolved.read_bytes()
        except FileNotFoundError:
            print(f"Error: linked document not found: {doc_path}", file=sys.stderr)
            sys.exit(1)
        documents_data.append(
            RefineRequestDocument(
                filepath=doc_path,
                data=base64.b64encode(raw).decode("ascii"),
            )
        )

    # 5. Call the server — plain-JSON payload on stdin (no command argument).
    vm = RemoteVM.from_cfg(cfg)
    try:
        cmd, proc, stdin, stdout = vm.refine()
    except subprocess.CalledProcessError as e:
        raise Exception(f"Error refining receipt: {e}") from e

    accounts = cfg.beancount.account_list_file.read_text(encoding="utf-8").splitlines()

    request_payload: RefineRequest = {
        "transaction_text": tx_block_str,
        "accounts": accounts,
        "documents": documents_data,
    }
    stdin.write(json.dumps(request_payload).encode("utf-8"))
    stdin.flush()
    stdin.close()

    llm_output = stream_reasoning_and_capture_output(stdout)
    stdout.close()

    ret = proc.wait()
    if ret != 0:
        raise subprocess.CalledProcessError(ret, cmd)

    # 6. Parse the response — strip any markdown fences, then parse JSON.
    llm_output = demarkdownify(llm_output).strip()
    try:
        resp = load_json(llm_output)
        rewritten_tx_raw = cast(str, resp["transaction"])
    except Exception:
        print(
            "Error: could not parse LLM response as JSON. Raw output:",
            file=sys.stderr,
        )
        print(llm_output, file=sys.stderr)
        sys.exit(1)

    # We will not delete comments, either sent by the user in the original
    # transaction and returning to us, or inserted by the LLM.
    lines = rewritten_tx_raw.splitlines(True)
    rewritten_tx = "".join(lines).rstrip("\n") + "\n"

    if not validate_refined_transaction(rewritten_tx):
        print(
            "Error: LLM returned a malformed transaction (no header / "
            "fewer than two postings). Raw output:",
            file=sys.stderr,
        )
        print(llm_output, file=sys.stderr)
        sys.exit(1)

    # 7. Reassemble the file with only the target block replaced.
    new_lines = (
        before
        + [
            ln if ln.endswith("\n") else ln + "\n"
            for ln in rewritten_tx.splitlines(True)
        ]
        + after
    )
    new_content = "".join(new_lines)

    diff = list(
        difflib.unified_diff(
            all_lines,
            new_lines,
            fromfile=str(tx_file),
            tofile=str(tx_file),
            n=5,
        )
    )
    if diff:
        print_diff(diff)
    else:
        print(f"No changes to {tx_file}", file=sys.stderr)
        return

    # 8. Prompt for / perform the write.
    if args.no:
        print(f"Skipping changes to {tx_file} (--no requested)", file=sys.stderr)
        return

    if not args.yes:
        while True:
            print(
                f"\nApply refined transaction to '{tx_file}'? [y]es / [n]o / [p]review document / [q]uit ",
                file=sys.stderr,
                end="",
            )
            try:
                answer = input().strip().lower()
            except EOFError:
                return
            if answer == "q":
                sys.exit(0)
            if answer == "p" and documents_data:
                preview_local_document(documents_data[0]["filepath"], tx_file, cfg)
                continue
            if answer == "y":
                break
            return  # 'n' -> abort without writing

    tx_file.write_text(new_content, encoding="utf-8")
    print(f"Updated transaction in {tx_file}", file=sys.stderr)


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
    else:
        print(f"No changes to {args.filename}", file=sys.stderr)
        return

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
        else:
            print(f"No changes to {receipt_path}", file=sys.stderr)
            return

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

    refine_cmd = sp.add_parser(
        "refine", help="Refine an existing transaction using its linked documents"
    )
    refine_cmd.add_argument(
        "file_path",
        help="Path to the Beancount file containing the target transaction",
    )
    refine_cmd.add_argument(
        "line_number",
        help="1-based line number of any line within the target transaction",
        type=int,
    )
    yes_group = refine_cmd.add_mutually_exclusive_group()
    yes_group.add_argument(
        "--yes",
        "-y",
        action="store_true",
        default=False,
        dest="yes",
        help="Apply the modification without confirmation",
    )
    yes_group.add_argument(
        "--no",
        "-n",
        action="store_true",
        default=False,
        dest="no",
        help="Do all the work (fetch documents, call the LLM, show the diff) but don't touch any file",
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
        "refine": do_refine,
    }
    dispatch[args.command](cfg, args)


if __name__ == "__main__":
    main()
