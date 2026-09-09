"""Client-side resolution of a receipt argument to a local file or a store receipt.

A receipt-taking command argument names either a file that already exists on the
client's own filesystem (a *local* receipt) or a filename known to the documents
server (a *store* receipt).  ``ReceiptRef`` encodes that decision once so every
receipt-taking command (``process``, ``import``, ``ingest``, ``associate``,
``organize``) shares the same local-vs-store semantics, and a local receipt makes
zero documents-server calls.

See ``docs/specs/Client-side receipts.md`` (Decisions D1 and D2) for the rules.
"""

from __future__ import annotations

import errno
import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from beanhand.structs import VALID_EXTENSIONS, FetchedReceipt

if TYPE_CHECKING:
    from beanhand.client.server.documents import DocumentsClient


class Source(Enum):
    """Where a receipt's bytes come from."""

    LOCAL = "local"
    STORE = "store"


@dataclass(frozen=True)
class ReceiptRef:
    """A resolved receipt: where its bytes come from and how to address them.

    ``filename`` is the *basename* of the receipt in **both** sources; it drives
    everything downstream (naming, extension, and the AI payload), which keeps
    local and store receipts interchangeable and byte-identical downstream.
    """

    src: Source
    arg: str
    path: Path
    filename: str

    @classmethod
    def resolve(cls, args: "list[str]") -> "list[ReceiptRef]":
        """Resolve a batch of receipt arguments to a de-duplicated list.

        Each argument is classified as a local file or a store filename
        (Decision D1): a path-like argument that does not exist, or that names
        a directory or a broken symlink, is an error — a path the user typed is
        a path, and re-interpreting a broken path as a store name would hide
        typos.  A bare name (no path separator) is local only when it is an
        existing file with a receipt extension; any other bare name is a store
        filename, exactly as before, so existing store-based workflows are
        undisturbed.

        The result is de-duplicated (Decision D7): a batch must name a receipt
        once, so this returns one ``ReceiptRef`` per underlying receipt in
        first-seen order.  De-duplication lives here, not in the callers,
        because a batch that names a receipt twice would otherwise be read,
        then fail on the second pass (the first pass moved / removed it).
        A single-receipt command simply passes a one-element list.
        """
        return cls._dedupe([cls._resolve_one(a) for a in args])

    @classmethod
    def _resolve_one(cls, arg: str) -> "ReceiptRef":
        path = Path(arg)

        pathlike = os.sep in arg or arg in (".", "..")
        if pathlike:
            if not os.path.lexists(arg):
                raise FileNotFoundError(f"{arg} (local path does not exist)")
            if not path.is_file():
                if path.is_dir():
                    raise IsADirectoryError(f"{arg} (path is a directory)")
                if os.path.islink(arg):
                    raise FileNotFoundError(f"{arg} (broken symlink)")
                raise OSError(errno.EINVAL, f"not a regular file: {arg}")
            return cls(Source.LOCAL, arg, path, path.name)

        # Bare name: local only when it is an existing file with a receipt
        # extension; otherwise a store filename (today's behavior).
        if path.is_file() and path.suffix in VALID_EXTENSIONS:
            return cls(Source.LOCAL, arg, path, path.name)

        return cls(Source.STORE, arg, Path(path.name), path.name)

    @staticmethod
    def _dedupe(refs: "list[ReceiptRef]") -> "list[ReceiptRef]":
        """Collapse *refs* to one element per receipt, preserving first-seen order."""
        seen: set[tuple[Source, object]] = set()
        out: list[ReceiptRef] = []
        for ref in refs:
            key = ref.dedup_key()
            if key in seen:
                continue
            seen.add(key)
            out.append(ref)
        return out

    def load(self, documents: "DocumentsClient") -> FetchedReceipt:
        """Return the receipt's bytes and a timestamp to preserve when filing.

        The local branch reads the file (recording ``st_mtime``) and the store
        branch fetches through the documents server; afterwards the caller cannot
        tell where the bytes came from.  ``documents`` is always supplied: for a
        local ref it simply goes unused (a local receipt makes zero
        documents-server calls), so the client is constructed the same way no
        matter where the receipt came from.  Raises ``FileNotFoundError`` in the
        race case where a local file vanishes between resolution and read.
        """
        if self.src is Source.LOCAL:
            data = self.path.read_bytes()
            return FetchedReceipt(data, self.path.stat().st_mtime, self.filename)
        return documents.fetch_receipt(self.filename)

    def dedup_key(self) -> tuple[Source, object]:
        """Identity of the receipt, for batch de-duplication.

        A local receipt is identified by its **resolved path** (so the same file
        given under different spellings — relative/absolute, with or without a
        trailing ``/`` — collides) and a store receipt by its **filename**.  The
        ``Source`` prefix keeps a local path from being mistaken for a
        same-named store receipt (they live in different universes).
        """
        if self.src is Source.LOCAL:
            return (self.src, self.path.resolve())
        return (self.src, self.filename)


def maybe_warn_collision(
    documents: "DocumentsClient",
    category: Literal["unassociated"] | Literal["uningested"],
    arg: str,
    ref: ReceiptRef,
) -> None:
    """Surface a bare-name local/store collision (Decision D1), if one exists.

    Only a *bare-name* LOCAL receipt with a receipt extension is ambiguous — a
    same-named file may also live in the store.  In that (rare) case a single
    ``List`` round-trip learns whether the store holds the name, and, if so, a
    one-line notice is printed so the user can re-choose deliberately.  The
    path-like case is unambiguous and makes no store call at all.
    """
    if ref.src is not Source.LOCAL:
        return
    if os.sep in arg or arg in (".", ".."):
        return
    if Path(arg).suffix not in VALID_EXTENSIONS:
        return
    if ref.filename in documents.list_receipts(category):
        print(
            f"Using local file '{arg}'; the documents store also contains "
            f"'{ref.filename}'",
            file=sys.stderr,
        )
