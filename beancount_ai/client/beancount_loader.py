"""Thin wrapper around beancount.loader for loading and filtering Beancount transactions.

Useful in the associate flow to find candidate transactions within a date range
so they can be presented to an LLM for receipt matching.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TypedDict
from beancount import loader


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransactionInfo:
    """Parsed fields from a single Beancount transaction."""

    file_path: str
    line_no: int
    date: date
    payee: str | None
    narration: str | None
    paid_amount: float | None  # total amount that was paid
    paid_currency: str | None  # e.g. "CHF"
    crediting_account: str
    accounts: set[str]


@dataclass
class CandidateContext:
    """All context needed to pass a candidate to the LLM."""

    date_str: str
    payee: str | None
    narration: str | None
    paid_amount: float | None
    paid_currency: str | None
    crediting_account: str
    source_file: str
    line_no: int
    transaction_text: str  # full Beancount text, for comparison


class MatchResult(TypedDict):
    """
    Result from LLM attempting to match a receipt to a set of transactions.

    See RECEIPT_MATCH_PROMPT.md for more information.
    """

    line_no: int
    source_file: str
    score: float
    reason: str


class MatchResults(TypedDict):
    """
    Results from LLM attempting to match a receipt to a set of transactions.

    See RECEIPT_MATCH_PROMPT.md for more information.
    """

    ambiguous: bool
    matches: list[MatchResult]


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------


def _find_paying_posting(postings) -> tuple[float | None, str | None, str | None]:
    """Return (paid_amount, currency, account) from the credit posting.

    If multiple credit postings exist, pick the one with the largest
    absolute value — that's the primary funding source.
    """
    # First try: find all credited accounts (negative amounts).
    credit_candidates: list[tuple[float, str, str]] = []
    for posting in postings or []:
        units = getattr(posting, "units", None)
        if units is None:
            continue
        number = float(getattr(units, "number", 0))
        currency = getattr(units, "currency", None) or ""
        account = getattr(posting, "account", "") or ""
        if not (number < 0 and currency and account):
            continue
        credit_candidates.append((abs(number), number, currency, account))

    if credit_candidates:
        # Pick the one with largest absolute value.
        credit_candidates.sort(key=lambda x: x[0], reverse=True)
        return (
            round(credit_candidates[0][0], 2),
            credit_candidates[0][2],
            credit_candidates[0][3],
        )

    # Fallback: single positive expense leg.
    pos_amounts = []
    for posting in postings or []:
        units = getattr(posting, "units", None)
        if units is None:
            continue
        number = float(getattr(units, "number", 0))
        currency = getattr(units, "currency", None) or ""
        if number <= 0 or not currency:
            continue
        pos_amounts.append((round(number, 2), currency))

    if len(pos_amounts) == 1:
        return (*pos_amounts[0], None)

    sum_amount = round(sum(a for a, _ in pos_amounts), 2)
    currencies = set(c for _, c in pos_amounts)
    currency = (
        list(currencies)[0]
        if len(currencies) == 1
        else "?"
        if not currencies
        else "MULTI"
    )
    return (sum_amount, currency or "?", None)


def load_transactions(
    main_file: str,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[TransactionInfo]:
    """Load a Beancount file and filter transactions.

    Parameters
    ----------
    main_file : str
        Path to the main Beancount ledger (includes are resolved automatically).
    start_date : date, optional
        Inclusive lower bound.  Only ``date >= start_date`` kept.
    end_date : date, optional
        Inclusive upper bound.  Only ``date <= end_date`` kept.

    Returns
    -------
    list[TransactionInfo]
        Parsed transactions within the date range.  Includes all files loaded via `include`
        directives in *main_file*.
    """
    entries, _, _ = loader.load_file(main_file)

    if start_date is None:
        start_date = date.min
    if end_date is None:
        end_date = date.max

    results: list[TransactionInfo] = []
    for entry in entries:
        # Only Transaction-type entries have postings.
        if not hasattr(entry, "postings"):
            continue
        if not hasattr(entry, "date"):
            continue

        cur_date = entry.date
        if cur_date < start_date or cur_date > end_date:
            continue

        date_str_raw = getattr(entry, "date", None)  # noqa (unused local — intentional clarity)
        payee = getattr(entry, "payee", None) or None
        narration = getattr(entry, "narration", None) or None

        file_path = ""
        line_no = 0
        if hasattr(entry, "meta") and entry.meta:
            file_path = entry.meta.get("filename", "")
            line_no = int(entry.meta.get("lineno", 0))

        paid_amount, paid_currency, crediting_account = _find_paying_posting(
            entry.postings
        )

        accounts: set[str] = set()
        for posting in entry.postings or []:
            accounts.add(getattr(posting, "account", ""))

        results.append(
            TransactionInfo(
                file_path=file_path,
                line_no=line_no,
                date=cur_date,
                payee=payee,
                narration=narration,
                paid_amount=paid_amount,
                paid_currency=paid_currency,
                crediting_account=crediting_account,
                accounts=accounts,
            )
        )

    return results


def load_transaction_contexts(
    main_file: str,
    start_date: date,
    end_date: date,
) -> tuple[list[TransactionInfo], list[CandidateContext]]:
    """Load transactions and produce candidate-context data suitable for LLM scoring.

    Like :py:func:`load_transactions` but also extracts the original Beancount text of each
    transaction (preserving metadata).

    Returns
    -------
    tuple[list[TransactionInfo], list[CandidateContext]]
        First element is the raw info list (same as :py:func:`load_transactions`).
        Second element is a list of CandidateContext dicts, one per candidate, each containing
        a ``transaction_text`` field ready to be embedded in an LLM prompt.
    """
    from beancount.parser import printer

    info_list = load_transactions(main_file, start_date, end_date)

    # Re-load so we can print the entries.  The loader caches; this is cheap.
    entries, _, _ = loader.load_file(main_file)

    # Disable filename/lineno in the printed output so it looks clean for LLM input.
    original_ignore = copy.copy(printer.EntryPrinter.META_IGNORE)
    printer.EntryPrinter.META_IGNORE = copy.copy(original_ignore) - {"meta"}

    contexts: list[CandidateContext] = []
    for entry in entries:
        if not hasattr(entry, "postings"):
            continue
        if not hasattr(entry, "date"):
            continue

        text = ""
        try:
            text = printer.format_entry(entry)
        except Exception:
            pass

        # Only include candidates from the date range.
        if entry.date < start_date or entry.date > end_date:
            continue

        paid_amount, paid_currency, crediting_account = _find_paying_posting(
            entry.postings
        )

        file_path = ""
        line_no = 0
        if hasattr(entry, "meta") and entry.meta:
            file_path = str(entry.meta.get("filename", ""))
            line_no = int(entry.meta.get("lineno", 0))

        payee = getattr(entry, "payee", None) or None
        narration = getattr(entry, "narration", None) or None

        contexts.append(
            CandidateContext(
                date_str=entry.date.isoformat(),
                payee=payee,
                narration=narration,
                paid_amount=paid_amount,
                paid_currency=paid_currency,
                crediting_account=crediting_account,
                source_file=file_path,
                line_no=line_no,
                transaction_text=text,
            )
        )

    # Restore original ignore set.
    printer.EntryPrinter.META_IGNORE = original_ignore

    return info_list, contexts


# ---------------------------------------------------------------------------
# CLI smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import sys

    main = sys.argv[1] if len(sys.argv) > 1 else "00-beancount.bean"
    n_days: int = int(sys.argv[2]) if len(sys.argv) > 2 else 7
    today = (
        date.strptime(sys.argv[3], "%Y-%m-%d") if len(sys.argv) > 3 else date.today()
    )

    infos, contexts = load_transaction_contexts(
        main, today - timedelta(n_days), today + timedelta(n_days)
    )

    results = [ctx.__dict__ for ctx in contexts]
    print(json.dumps(results, indent=2))
