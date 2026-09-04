"""Raw Beancount file operations.  Query-related code is in beancount_loader.py."""

import hashlib
import os
import re
from datetime import date
from pathlib import Path

_DOCUMENT_METADATA_REGEX = re.compile(r'^\s*document(\d*):\s*"([^"]+)"')


class FileModifiedError(Exception):
    """A Beancount file was modified (or removed) after bean-ai read it.

    Raised by :meth:`FileGuard.verify` when the on-disk content no longer
    matches the snapshot taken when the file was read, so that bean-ai can
    refuse to clobber edits the user made since the file was loaded.
    """


def _content_hash(path: Path) -> int:
    """Return a content checksum of *path* (raises ``FileNotFoundError`` if absent)."""
    data = path.read_bytes()
    return int.from_bytes(hashlib.sha256(data).digest()[:16], "big")


class FileGuard:
    """A snapshot of a file's content, guarding against concurrent external edits.

    A :class:`FileGuard` binds a file path to the checksum of its content at the
    moment the guard was taken — right after bean-ai read the file.  Before
    writing back, the caller invokes :meth:`verify`; if the on-disk content has
    since changed (or the file was deleted), :class:`FileModifiedError` is
    raised so the caller can refuse to clobber the user's edits and re-read the
    file instead.  Comparison is by content, not by mtime, so merely touching a
    file's timestamp does not trip it, but any change to the bytes does.
    """

    _path: Path
    _hash: int

    def __init__(self, path: Path, hash_value: int) -> None:
        self._path = path
        self._hash = hash_value

    @classmethod
    def take(cls, path: Path) -> "FileGuard":
        """Snapshot the file's current content; return a guard over it.

        Raises ``FileNotFoundError`` if *path* does not exist.  A caller taking
        a guard has just read the file, so a missing file here is a genuine
        error the caller should surface — not a state to be quietly ignored —
        which is why the method returns a guard and never ``None``.
        """
        return cls(path, _content_hash(path))

    def verify(self) -> None:
        """Raise :class:`FileModifiedError` if the content changed since :meth:`take`."""
        try:
            current = _content_hash(self._path)
        except FileNotFoundError:
            raise FileModifiedError(
                f"{self._path} was deleted after bean-ai read it; refusing to write"
            ) from None
        if current != self._hash:
            raise FileModifiedError(
                f"{self._path} appears to have been modified since bean-ai read it; "
                "refusing to overwrite — re-run the command to re-read the file"
            )


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


def write_beancount_file(path: Path, text: str) -> None:
    """Write *text* to a Beancount file, flushing and fsyncing to disk.

    The explicit flush-to-disk improves data reliability: if the process
    crashes in the middle of a write (or the power fails), the ledger is less
    likely to be left half-written.
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


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


type FileBlock = tuple[bool, int, list[str]]
type FileBlocks = list[FileBlock]


def split_into_transactions_by_range(  # noqa: C901
    tx_lines: list[str],
    start_line: int,
    end_line: int | None = None,
) -> FileBlocks:
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
        (True, lineno, lines): a list of consecutive lines corresponding to a single transaction;
        each transaction is its own group, even when consecutive transactions have no
        blank line between them
        (False, lineno, lines): a list of consecutive lines that do not belong to any transaction
      lineno is the start line number of the list of lines in the block

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
    >>> split_into_transactions_by_range(data, 1)[1][2]
    ['2026-01-01 * "Beans"\\n', '  Expenses:Beans 1000 CHF\\n', '  Assets:Bank\\n']
    >>> split_into_transactions_by_range(data, 7,)[1][2]
    ['2026-01-02 * "More beans"\\n', '  Expenses:Beans 500 CHF\\n', '  Assets:Bank\\n']
    >>> split_into_transactions_by_range(data, 8)[0][2][-1]
    '2026-01-03 balance Assets:Bank 15400000 CHF\\n'
    >>> split_into_transactions_by_range(data, 9)[0][2][-1]
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

    def is_transaction_header(ln: str) -> bool:
        fields = ln.split()
        return (
            initial_number_regex.match(ln) is not None
            and len(fields) > 2
            and len(fields[1]) == 1
        )

    # Start with the first lookbehind.  Look at the current line.
    curr_line = start_line
    for curr_line in range(start_line, -1, -1):
        ln = tx_lines[curr_line]
        if is_transaction_header(ln):
            # Found the start of the transaction.
            break
        if initial_whitespace_regex.match(ln) and ln.strip():
            # We may be in the middle of a transaction, because there appears to
            # be text starting by whitespace.  Look one line back.
            continue
        # We didn't start at a transaction, so we stop here.
        break

    in_middle_of_transaction = False
    intermediate: list[tuple[bool, str, int]] = []
    intermediate.extend((False, ln, 0) for ln in tx_lines[:curr_line])

    for cln in range(curr_line, len(tx_lines)):
        ln = tx_lines[cln]
        if is_transaction_header(ln):
            if cln <= end_line:
                # Found the start of a transaction within the range.
                in_middle_of_transaction = True
            else:
                in_middle_of_transaction = False
        elif initial_whitespace_regex.match(ln) and ln.strip():
            # An indented, non-blank line: if we were in a transaction, we
            # continue to be in the middle of one.  Look forward.
            in_middle_of_transaction = in_middle_of_transaction
        else:
            in_middle_of_transaction = False
        intermediate.append((in_middle_of_transaction, ln, cln))

    # Fold the per-line flags into blocks.  As with a plain run-based
    # grouping, consecutive lines sharing a flag coalesce; the one difference
    # is that a run of transaction lines is further split at every transaction
    # header, so that two adjacent transactions (no blank line between them)
    # are emitted as separate blocks rather than merged into one.
    result: FileBlocks = []
    for is_tran, ln, cln in intermediate:
        # A new block starts when the flag changes, or when, inside a
        # transaction run, we hit a further transaction header (so adjacent
        # transactions each become their own block).
        same_block = (
            result
            and result[-1][0] == is_tran
            and not (is_tran and is_transaction_header(ln))
        )
        if same_block:
            result[-1][2].append(ln)
        else:
            result.append((is_tran, cln, [ln]))
    return result


def classify_by_target_spans(
    tx_lines: list[str], spans: list[tuple[int, int]]
) -> list[tuple[bool, int, list[str]]]:
    """Classify an entire Beancount document, flagging the transactions selected by *spans*.

    Each span is a (zero-based, inclusive) ``(start, end)`` pair built from one
    user-supplied target token:

    - a single-line token ``N`` becomes span ``(N-1, N-1)`` and selects the
      transaction *containing* line ``N`` (walk-back semantics: pointing into
      the middle of a transaction selects the whole transaction);
    - a range token ``A-B`` becomes span ``(A-1, B-1)`` and selects every
      transaction that *begins* on a line between ``A`` and ``B`` (inclusive),
      plus the transaction containing line ``A`` itself when ``A`` points into
      the middle of one (that transaction is included whole, even if its body
      extends past ``B``).

    A transaction selected by more than one span is flagged exactly once.  The
    returned groups cover the whole document: flattening them back together
    (``"".join`` of every line) reproduces the input byte-for-byte.  Every
    group that is not a selected transaction is returned as a non-transaction
    group, so callers can rebuild the file by substitution.

    Since the base classifier emits at most one transaction per group, every
    ``True`` group in the output corresponds to exactly one transaction, even
    when two transactions have no blank line between them.

    >>> doc = (
    ...     '2026-01-01 * "Beans"\\n'
    ...     "  Expenses:Beans 1000 CHF\\n"
    ...     "  Assets:Bank\\n"
    ...     "\\n"
    ...     '2026-01-02 * "More beans"\\n'
    ...     "  Expenses:Beans 500 CHF\\n"
    ...     "  Assets:Bank\\n"
    ... ).splitlines(True)
    >>> [t for t, _, _ in classify_by_target_spans(doc, [(1, 1)])]
    [True, False, False]
    >>> [t for t, _, _ in classify_by_target_spans(doc, [(0, 4)])]
    [True, False, True]
    >>> [t for t, _, _ in classify_by_target_spans(doc, [(3, 3)])]
    [False, False, False]
    >>> "".join(ln for _, _, ls in classify_by_target_spans(doc, [(0, 4)]) for ln in ls) == "".join(doc)
    True
    """
    if not tx_lines:
        return []
    return [
        (
            is_tx
            and any(
                s <= start_lineno <= e
                or start_lineno <= s <= start_lineno + len(lines) - 1
                for s, e in spans
            ),
            start_lineno,
            lines,
        )
        for is_tx, start_lineno, lines in split_into_transactions_by_range(
            tx_lines, 0, len(tx_lines) - 1
        )
    ]


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
    for istran, _lineno, lines in res:
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
