"""Raw Beancount file operations.  Query-related code is in beancount_loader.py."""

import itertools
import os
import re
from datetime import date
from pathlib import Path

_DOCUMENT_METADATA_REGEX = re.compile(r'^\s*document(\d*):\s*"([^"]+)"')


def shorten_fn(folder: str | Path, fn: str) -> str:
    """Reduce max path length without affecting the file name extension."""
    maxlen = os.pathconf(folder, "PC_NAME_MAX")
    # Sarn, we only handle UTF-8 file systems.  Maybe this would be good to fix in the future.
    while len(fn.encode("utf-8")) > maxlen:
        n, e = os.path.splitext(fn)
        n = n[:-1]
        fn = n + e
    return fn


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


def resolve_local_document_path(doc_path: str, tx_file: Path) -> Path:
    """Resolve a ``document:`` value to a client-local path.

    Non-absolute paths resolve relative to the document referencing them.
    """
    tx_dir = tx_file.parent
    p = Path(os.path.join(tx_dir, doc_path))
    return p


# --------------------------------
# Operations on raw Beancount text
# --------------------------------


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
