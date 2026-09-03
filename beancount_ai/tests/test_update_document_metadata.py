#!/usr/bin/env python3
"""Tests for update_document_metadata."""

import pathlib
import sys

# Ensure parent dir is on path so we can import cli module directly.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.client.beanfiles import update_document_metadata


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


def text(s: str) -> str:
    return s


def test_realistic_transactions() -> None:
    tx = text("""\
2026-01-15 * "BOUNCE - USEBOUNCE.COM" #madrid-2026
  date: 2026-01-13
  document: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-01-13.BOUNCE - USEBOUNCE.COM.pdf"
  document2: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-08-17.export_credit_cards_overview_XXXXXXXXXXXXXXXX_20260405.csv"
  Expenses:Current:Travel:Luggage
  Liabilities:Credit-cards:ZKB   -16.72 CHF
    raw_string: "BOUNCE - USEBOUNCE.COM   LISBOA PT EUR 17.32 Rate 0.9491721576 of 14.01.2026 CHF 16.45 1.7% Processing surcharge CHF 0.27"
    processing_surcharge: "CHF 0.27"
    consumed_string: "BOUNCE - USEBOUNCE.COM   LISBOA PT EUR 17.32 Rate 0.9491721576 of 14.01.2026 CHF 16.45 1.7%"
  Expenses:Current:Financial:Bank-fees     0.27 CHF

2026-01-16 * "Coop LU"
  document: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-01-15.Coop LU, 34.80 CHF — Receipt 2026-01-15 11_41_55+00_00.pdf"
  date: 2026-01-15
  document2: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-08-17.export_credit_cards_overview_XXXXXXXXXXXXXXXX_20260405.csv"
  Expenses:Current:Food:Groceries
  Expenses:Current:Food:Snacks                            4.95 CHF
  Expenses:Current:Family:Baby:Food                       1.95 +1.30 + 0.55 + 2.80 CHF
  Expenses:Current:Family:Wife:Groceries                 1.55 CHF
  Expenses:Current:Family:Wife:Snacks                    3.95 CHF
  Income:Discounts-and-rebates                           -3.95 CHF
  Liabilities:Credit-cards:ZKB  -34.8 CHF
    raw_string: "Coop-5240 ZH PfingstweidsZurich"

2026-01-19 * "digitec Galaxus (Online) Luzern"
  document: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-01-16.digitec Galaxus (Online) Luzern, 36.30 CHF — galaxus 16.01.26.pdf"
  date: 2026-01-16
  document2: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-08-17.export_credit_cards_overview_XXXXXXXXXXXXXXXX_20260405.csv"
  Expenses:Current:Gifts:Fraga-family
    narration:"Gifts for aunt"
  Liabilities:Credit-cards:ZKB      -36.3 CHF
    raw_string: "digitec Galaxus (Online) Luzern"
""")
    want = text("""\
2026-01-15 * "BOUNCE - USEBOUNCE.COM" #madrid-2026
  date: 2026-01-13
  document: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-01-13.BOUNCE - USEBOUNCE.COM.pdf"
  document2: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-08-17.export_credit_cards_overview_XXXXXXXXXXXXXXXX_20260405.csv"
  Expenses:Current:Travel:Luggage
  Liabilities:Credit-cards:ZKB   -16.72 CHF
    raw_string: "BOUNCE - USEBOUNCE.COM   LISBOA PT EUR 17.32 Rate 0.9491721576 of 14.01.2026 CHF 16.45 1.7% Processing surcharge CHF 0.27"
    processing_surcharge: "CHF 0.27"
    consumed_string: "BOUNCE - USEBOUNCE.COM   LISBOA PT EUR 17.32 Rate 0.9491721576 of 14.01.2026 CHF 16.45 1.7%"
  Expenses:Current:Financial:Bank-fees     0.27 CHF

2026-01-16 * "Coop LU"
  document: "/bimbambum.jpg"
  document2: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-01-15.Coop LU, 34.80 CHF — Receipt 2026-01-15 11_41_55+00_00.pdf"
  date: 2026-01-15
  document3: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-08-17.export_credit_cards_overview_XXXXXXXXXXXXXXXX_20260405.csv"
  Expenses:Current:Food:Groceries
  Expenses:Current:Food:Snacks                            4.95 CHF
  Expenses:Current:Family:Baby:Food                       1.95 +1.30 + 0.55 + 2.80 CHF
  Expenses:Current:Family:Wife:Groceries                 1.55 CHF
  Expenses:Current:Family:Wife:Snacks                    3.95 CHF
  Income:Discounts-and-rebates                           -3.95 CHF
  Liabilities:Credit-cards:ZKB  -34.8 CHF
    raw_string: "Coop-5240 ZH PfingstweidsZurich"

2026-01-19 * "digitec Galaxus (Online) Luzern"
  document: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-01-16.digitec Galaxus (Online) Luzern, 36.30 CHF — galaxus 16.01.26.pdf"
  date: 2026-01-16
  document2: "/home/user/Documents/Accounting/Liabilities/Credit-cards/ZKB/2026-08-17.export_credit_cards_overview_XXXXXXXXXXXXXXXX_20260405.csv"
  Expenses:Current:Gifts:Fraga-family
    narration:"Gifts for aunt"
  Liabilities:Credit-cards:ZKB      -36.3 CHF
    raw_string: "digitec Galaxus (Online) Luzern"
""")
    tx_lines = tx.splitlines(True)
    result = update_document_metadata(
        line_no=12, tx_lines=tx_lines, new_doc="/bimbambum.jpg"
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
