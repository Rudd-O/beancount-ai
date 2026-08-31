#!/usr/bin/env python3
"""Tests for update_document_metadata."""

import pathlib
import sys

# Ensure parent dir is on path so we can import cli module directly.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.client.cli import update_document_metadata


def _lines(*s: str) -> str:
    return "\n".join(s) + "\n"  # trailing newline so splitlines works symmetrically


# =====================================================================


def test_no_existing_docs() -> None:
    """No document metadata present — insert `document:` after date line."""
    tx_lines = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        "  Exp:Food  10 CHF",
    )
    want = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        '  document: "/path/receipt.jpg"',
        "  Exp:Food  10 CHF",
    )
    result = update_document_metadata(
        line_no=1, tx_lines=tx_lines.splitlines(True), new_doc="/path/receipt.jpg"
    )
    assert result == want


def test_one_existing_document_tag() -> None:
    """One `document:` present — it becomes `document2:`, new one is `document:`."""
    tx_lines = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        '  document: "/old/doc.pdf"',
        "  Exp:Food  10 CHF",
    )
    want = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        '  document: "/new/receipt.jpg"',
        '  document2: "/old/doc.pdf"',
        "  Exp:Food  10 CHF",
    )
    result = update_document_metadata(
        line_no=1, tx_lines=tx_lines.splitlines(True), new_doc="/new/receipt.jpg"
    )
    assert result == want


def test_multiple_numbered_docs() -> None:
    """Two numbered docs — all bumped up by one."""
    tx_lines = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        '  document2: "/first.pdf"',
        '  document3: "/second.pdf"',
        "  Exp:Food  10 CHF",
    )
    want = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        '  document: "/new.jpg"',
        '  document2: "/first.pdf"',
        '  document3: "/second.pdf"',
        "  Exp:Food  10 CHF",
    )
    result = update_document_metadata(
        line_no=1, tx_lines=tx_lines.splitlines(True), new_doc="/new.jpg"
    )
    assert result == want


def test_mixed_document_and_numbered() -> None:
    """One unnumbered `document:` and one numbered — both bumped correctly."""
    tx_lines = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        '  document: "/base.pdf"',
        '  document3: "/third.pdf"',
        "  Exp:Food  10 CHF",
    )
    want = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        '  document: "/newer.jpg"',
        '  document2: "/base.pdf"',
        '  document3: "/third.pdf"',
        "  Exp:Food  10 CHF",
    )
    result = update_document_metadata(
        line_no=1, tx_lines=tx_lines.splitlines(True), new_doc="/newer.jpg"
    )
    assert result == want


def test_document_and_other_tag() -> None:
    """One unnumbered `document:` and one numbered — both bumped correctly."""
    tx_lines = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        '  date: "2024-12-31"',
        '  document: "/third.pdf"',
        "  Exp:Food  10 CHF",
    )
    want = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        '  date: "2024-12-31"',
        '  document: "/newer.jpg"',
        '  document2: "/third.pdf"',
        "  Exp:Food  10 CHF",
    )
    result = update_document_metadata(
        line_no=1, tx_lines=tx_lines.splitlines(True), new_doc="/newer.jpg"
    )
    assert result == want


def test_metadata_replacement_at_end_works() -> None:
    """One unnumbered `document:` and one numbered — both bumped correctly."""
    tx_lines = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        '  date: "2024-12-31"',
        "  Exp:Food  10 CHF",
        '  document: "/old.pdf"',
    )
    want = _lines(
        '2025-01-01 * "Grocery" "Milk"',
        '  date: "2024-12-31"',
        "  Exp:Food  10 CHF",
        '  document: "/newer.jpg"',
        '  document2: "/old.pdf"',
    )
    result = update_document_metadata(
        line_no=1, tx_lines=tx_lines.splitlines(True), new_doc="/newer.jpg"
    )
    assert result == want


def test_other_transactions_are_not_fucked() -> None:
    """One unnumbered `document:` and one numbered — both bumped correctly."""
    tx_lines = _lines(
        '2025-01-01 * "Grocery" "Donuts"',
        "  Exp:Food  10 CHF",
        "  ",
        '2025-01-01 * "Grocery" "Milk"',
        '  document: "/base.pdf"',
        '  document3: "/third.pdf"',
        "  Exp:Food  10 CHF",
        '2025-01-01 * "Grocery" "Red Bull"',
        "  Exp:Food  10 CHF",
        "  ",
    )
    want = _lines(
        '2025-01-01 * "Grocery" "Donuts"',
        "  Exp:Food  10 CHF",
        "  ",
        '2025-01-01 * "Grocery" "Milk"',
        '  document: "/newer.jpg"',
        '  document2: "/base.pdf"',
        '  document3: "/third.pdf"',
        "  Exp:Food  10 CHF",
        '2025-01-01 * "Grocery" "Red Bull"',
        "  Exp:Food  10 CHF",
        "  ",
    )
    result = update_document_metadata(
        line_no=4, tx_lines=tx_lines.splitlines(True), new_doc="/newer.jpg"
    )
    assert result == want


# =====================================================================


def run_all() -> int:
    funcs = [v for k, v in list(globals().items()) if k.startswith("test_")]
    fail = False
    for fn in funcs:
        try:
            fn()
            print(f"  OK  {fn.__name__}")
        except Exception as exc:
            print(f"FAIL  {fn.__name__}: {exc}", file=sys.stderr)
            fail = True
    return 1 if fail else 0


if __name__ == "__main__":
    raise SystemExit(run_all())
