"""Thin wrapper around beancount.loader for loading and filtering Beancount transactions.

Useful in the associate flow to find candidate transactions within a date range
so they can be presented to an LLM for receipt matching.
"""

from __future__ import annotations

import copy
import sys
import warnings
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, TypedDict, cast

from beancount import loader
from beancount.core import data

from beanhand.structs import AccountRef

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


@dataclass
class _AccountState:
    """Per-account open/close history collected while walking the ledger."""

    # Sorted list of (date, entry index, meta) for every `open` directive.
    opens: list[tuple[date, int, dict[str, Any]]]
    # Sorted list of (date, entry index) for every `close` directive.
    closes: list[tuple[date, int]]


_USE_MARKERS = ("yes", "recursively")


def _validate_beancount_metadata(meta: dict[str, Any], account: str) -> None:
    """Fail-stop validation of the beanhand-* metadata keys on one directive.

    Raises ValueError naming the account and the offending value when:
      - ``beanhand-include`` or ``beanhand-exclude`` is present and is not
        ``"yes"`` or ``"recursively"``; or
      - ``beanhand-rules`` is present and is not a string.
    """
    for key in (
        "beanhand-include",
        "beanhand-exclude",
        "bean-ai-include",
        "bean-ai-exclude",
    ):
        if key in meta:
            value = meta[key]
            if not isinstance(value, str) or value not in _USE_MARKERS:
                raise ValueError(
                    f"invalid {key} value for {account}: {value!r} "
                    "(only 'yes' and 'recursively' are accepted)"
                )
            if "bean-ai" in key:
                warnings.warn(
                    f"You are using deprecated metadata key {key} in your ledger."
                    "  This will be removed in the future."
                )

    for key in ("beanhand-rules", "bean-ai-rules"):
        if key not in meta:
            continue
        if not isinstance(meta[key], str):
            raise ValueError(f"{key} for {account} is not a string: {meta[key]!r}")
        if "bean-ai" in key:
            warnings.warn(
                f"You are using deprecated metadata key {key} in your ledger."
                "  This will be removed in the future."
            )


def _collect_account_state(entries: data.Directives) -> dict[str, _AccountState]:
    """Walk parsed entries, collecting open/close history per account name.

    ``beanhand-*`` metadata is validated on *every* open/close entry, so a typo
    anywhere in the file surfaces immediately.
    """
    state: dict[str, _AccountState] = {}
    for idx, entry in enumerate(entries):
        if isinstance(entry, data.Open):
            _validate_beancount_metadata(entry.meta, entry.account)
            st = state.setdefault(entry.account, _AccountState([], []))
            st.opens.append((entry.date, idx, entry.meta))
        elif isinstance(entry, data.Close):
            _validate_beancount_metadata(entry.meta, entry.account)
            st = state.setdefault(entry.account, _AccountState([], []))
            st.closes.append((entry.date, idx))
    for st in state.values():
        st.opens.sort(key=lambda t: t[:2])
        st.closes.sort(key=lambda t: t[:2])
    return state


def _live_and_open(
    state: dict[str, _AccountState], as_of: date
) -> tuple[list[str], list[tuple[date, int, dict[str, Any]] | None]]:
    """Return (sorted live accounts, their latest ≤ as_of open entries)."""
    accounts: list[str] = []
    opens: list[tuple[date, int, dict[str, Any]] | None] = []
    for acct in sorted(state):
        st = state[acct]
        events = [(d, i, True) for d, i, _ in st.opens if d <= as_of] + [
            (d, i, False) for d, i in st.closes if d <= as_of
        ]
        if not events:
            continue
        if not max(events, key=lambda t: t[:2])[2]:
            continue  # The most recent event ≤ as_of is a close.
        cands = [t for t in st.opens if t[0] <= as_of]
        accounts.append(acct)
        opens.append(cands[-1])
    return accounts, opens


def _included_accounts(
    state: dict[str, _AccountState], as_of: date, live: set[str]
) -> set[str]:
    """Return the live accounts selected by beanhand-include / beanhand-exclude.

    * raw-selected: an account's own selected open carries ``beanhand-include``,
      **or** a *proper* ancestor's selected open carries
      ``beanhand-include: "recursively"`` — even if the ancestor is not itself
      live at ``as_of`` (a closed parent's policy still applies to its live
      children).
    * raw-excluded: an account's own selected open carries ``beanhand-exclude``
      with value ``"yes"``, **or** a *proper* ancestor's selected open carries
      ``beanhand-exclude: "recursively"``.
    * carved out: an account that is raw-excluded but whose *own* selected open
      carries ``beanhand-include`` (any value) is re-included — an explicit
      self-include beats an ancestor's recursive exclude.

    Liveness and policy are kept separate: only live accounts are returned,
    but the include/exclude read from an ancestor does not depend on that
    ancestor being live.
    """

    def meta_at(acct: str) -> dict[str, Any] | None:
        st = state.get(acct)
        if st is None:
            return None
        cands = [t for t in st.opens if t[0] <= as_of]
        if not cands:
            return None
        return cands[-1][2]

    def own_marker(acct: str, key: str) -> str | None:
        m = meta_at(acct)
        if m is None:
            return None
        v = m.get(key)
        return v if isinstance(v, str) else None

    def ancestor_recursive(acct: str, key: str) -> bool:
        parts = acct.split(":")
        return any(
            own_marker(":".join(parts[:i]), key) == "recursively"
            for i in range(1, len(parts))
        )

    included: set[str] = set()
    for acct in live:
        own_inc = own_marker(acct, "beanhand-include") or own_marker(
            acct, "bean-ai-include"
        )
        own_exc = own_marker(acct, "beanhand-exclude") or own_marker(
            acct, "bean-ai-exclude"
        )
        raw_selected = (
            own_inc is not None
            or ancestor_recursive(acct, "beanhand-include")
            or ancestor_recursive(acct, "bean-ai-include")
        )
        if not raw_selected:
            continue
        raw_excluded = (
            own_exc is not None
            or ancestor_recursive(acct, "beanhand-exclude")
            or ancestor_recursive(acct, "bean-ai-exclude")
        )
        if raw_excluded and own_inc is None:
            continue
        included.add(acct)
    return included


def load_live_accounts(main_file: str | Path, as_of: date) -> list[AccountRef]:
    """Return the list of live accounts from the ledger, for the LLM prompt.

    An account is live if it is open (or was closed after ``as_of``) at
    ``as_of``.  A live account is included if it is selected by
    ``beanhand-include`` metadata — either ``"yes"`` on its own selected open
    (just this account) or ``"recursively"`` on its own or a proper
    ancestor's selected open (the account and its descendants; the ancestor
    need not itself be live — closing a parent lets its policy continue to
    reach its live children) — and is not blocked by
    ``beanhand-exclude`` metadata, except that an account with its own explicit
    ``beanhand-include`` beats an ancestor's ``"recursively"`` exclude.
    Accounts are returned sorted by name.  Each account's ``rule`` is taken
    from its own most recent ``open`` (≤ ``as_of``) carrying ``beanhand-rules``;
    it does not inherit.

    Raises on:
      - a parse or validation error in the ledger (propagated);
      - a ``beanhand-include`` / ``beanhand-exclude`` value other than ``"yes"``
        / ``"recursively"`` (ValueError naming the account and the offending
        value);
      - a non-string ``beanhand-rules`` value (ValueError).
    """
    from beancount.parser import printer

    entries, errors, _ = loader.load_file(main_file)
    if errors:
        format_error = cast(
            "Callable[[data.BeancountError], str]", printer.format_error
        )
        warnings.warn(
            f"{main_file} contains {len(errors)} errors; account derivation may not work.  Errors follow:\n\n"
            + "\n".join(format_error(e) for e in errors)
        )

    state = _collect_account_state(entries)
    live_accounts, latest_opens = _live_and_open(state, as_of)
    live = set(live_accounts)
    included = _included_accounts(state, as_of, live)

    refs: list[AccountRef] = []
    for acct, lo in zip(live_accounts, latest_opens):
        if acct not in included or lo is None:
            continue
        n: AccountRef = {"name": acct}
        rule = lo[2].get("beanhand-rules", lo[2].get("bean-ai-rules"))
        if isinstance(rule, str) and rule.strip():
            n["rule"] = rule
        refs.append(n)
    return refs


def accounts_for_prompt(
    main_file: str | Path, run_date: date | None = None
) -> list[AccountRef]:
    """Return the account list to send to the LLM.

    Wraps :func:`load_live_accounts` with the run date (defaulting to today)
    and raises ``RuntimeError("the LLM would be offered no accounts")`` when
    the derived list is empty — see the "no live accounts" edge case: an empty
    or near-empty account list is almost always a misconfiguration (most
    commonly, an un-migrated install), and offering nothing to the LLM
    guarantees a garbage transaction.
    """
    accounts = load_live_accounts(main_file, run_date or date.today())
    if not accounts:
        raise RuntimeError("the LLM would be offered no accounts")
    return accounts


def account_refs_or_die(main_file: str | Path, run_date: date) -> list[AccountRef]:
    """Load the account list to send to the LLM, exiting 1 on failure.

    ``run_date`` is the date the account list is derived "as of": accounts are
    only offered if they are open (and not yet closed) on that date.  Callers
    that create a new transaction (process / import) pass ``date.today()``; the
    refine command passes the existing transaction's own date, so the LLM is
    offered exactly the accounts that were legal when the transaction was made.

    Fail-stop on purpose: an empty account list (an un-migrated ledger) or an
    invalid ``beanhand-*`` metadata value guarantees a garbage transaction, so
    the command must not proceed.  A ledger with parse or validation errors is
    equally fatal: the account graph is only meaningful for a consistent file.
    """
    try:
        return accounts_for_prompt(main_file, run_date)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
    except RuntimeError as e:
        if str(e) == "the LLM would be offered no accounts":
            print(
                f'Error: no accounts marked beanhand-include: "yes" or "recursively" in '
                f"{main_file}; the LLM would be offered no accounts to post to.  "
                "Mark the accounts or subtrees you want available (see docs) on their "
                "'open' directives.",
                file=sys.stderr,
            )
        else:
            print(f"Error: {e}", file=sys.stderr)
    sys.exit(1)


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


def _find_paying_posting(
    postings: list[data.Posting],
) -> tuple[float | None, str | None, str | None]:
    """Return (paid_amount, currency, account) from the credit posting.

    If multiple credit postings exist, pick the one with the largest
    absolute value — that's the primary funding source.
    """
    # First try: find all credited accounts (negative amounts).
    credit_candidates: list[tuple[float, float, str, str]] = []
    for posting in postings:
        units = posting.units
        if units is None or units.number is None:
            continue
        number = float(units.number)
        currency = units.currency
        account = posting.account
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
    pos_amounts: list[tuple[float, str]] = []
    for posting in postings:
        units = posting.units
        if units is None or units.number is None:
            continue
        number = float(units.number)
        currency = units.currency
        if number <= 0 or not currency:
            continue
        pos_amounts.append((round(number, 2), currency))

    if len(pos_amounts) == 1:
        return (*pos_amounts[0], None)

    sum_amount = round(sum((a for a, _ in pos_amounts), 0), 2)
    currencies = {c for _, c in pos_amounts}
    currency = (
        next(iter(currencies))
        if len(currencies) == 1
        else "?"
        if not currencies
        else "MULTI"
    )
    return (sum_amount, currency, None)


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
        # Only Transaction entries carry postings.
        if not isinstance(entry, data.Transaction):
            continue

        if entry.date < start_date or entry.date > end_date:
            continue

        payee = entry.payee or None
        narration = entry.narration or None

        file_path = ""
        line_no = 0
        if entry.meta:
            file_path = str(entry.meta.get("filename", ""))
            line_no = int(entry.meta.get("lineno", 0))

        paid_amount, paid_currency, crediting_account = _find_paying_posting(
            entry.postings
        )

        accounts: set[str] = {posting.account for posting in entry.postings}

        results.append(
            TransactionInfo(
                file_path=file_path,
                line_no=line_no,
                date=entry.date,
                payee=payee,
                narration=narration,
                paid_amount=paid_amount,
                paid_currency=paid_currency,
                crediting_account=crediting_account or "",
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
        if not isinstance(entry, data.Transaction):
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
        if entry.meta:
            file_path = str(entry.meta.get("filename", ""))
            line_no = int(entry.meta.get("lineno", 0))

        payee = entry.payee or None
        narration = entry.narration or None

        contexts.append(
            CandidateContext(
                date_str=entry.date.isoformat(),
                payee=payee,
                narration=narration,
                paid_amount=paid_amount,
                paid_currency=paid_currency,
                crediting_account=crediting_account or "",
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
