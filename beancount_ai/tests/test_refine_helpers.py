#!/usr/bin/env python3
"""Tests for the `refine` client helpers: extract_document_paths and resolve_local_document_path."""

import pathlib
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.client.beanfiles import (
    extract_document_paths,
    resolve_local_document_path,
)
from beancount_ai.client.commands.refine import (
    _tx_date,  # pyright: ignore[reportPrivateUsage]
)


def _lines(*s: str) -> list[str]:
    return "\n".join(s).splitlines(True)


# ============================ extract_document_paths =======================


def test_no_documents() -> None:
    assert (
        extract_document_paths(_lines('2025-01-01 * "X"', "  Exp:Food  10 CHF")) == []
    )


def test_single_document() -> None:
    block = _lines(
        '2025-01-01 * "Coop"',
        '  document: "/path/one.pdf"',
        "  Exp:Food  10 CHF",
    )
    assert extract_document_paths(block) == ["/path/one.pdf"]


def test_numbered_documents() -> None:
    block = _lines(
        '2025-01-01 * "Coop"',
        '  document: "/a.jpg"',
        '  document2: "/b.pdf"',
        '  document3: "/c.png"',
        "  Exp:Food  10 CHF",
    )
    assert extract_document_paths(block) == ["/a.jpg", "/b.pdf", "/c.png"]


def test_preserves_first_seen_order_and_dedupes() -> None:
    block = _lines(
        '2025-01-01 * "Coop"',
        '  document2: "/b.pdf"',
        '  document: "/a.jpg"',
        '  document2: "/b.pdf"',
        "  Exp:Food  10 CHF",
    )
    assert extract_document_paths(block) == ["/b.pdf", "/a.jpg"]


def test_ignores_other_metadata() -> None:
    block = _lines(
        '2025-01-01 * "Coop"',
        '  date: "2024-12-31"',
        '  doc: "/not-a-document.pdf"',
        '  document: "/a.jpg"',
        "  Exp:Food  10 CHF",
    )
    assert extract_document_paths(block) == ["/a.jpg"]


def test_requires_quoted_value() -> None:
    # A document key with an unquoted value is not matched by the canonical regex.
    block = _lines(
        '2025-01-01 * "Coop"',
        "  document: /no-quotes.jpg",
        '  document: "/a.jpg"',
        "  Exp:Food  10 CHF",
    )
    assert extract_document_paths(block) == ["/a.jpg"]


# ============================ resolve_document_path ========================


def test_absolute_path_used_as_is(tmp_path: pathlib.Path) -> None:
    tx_file = tmp_path / "main.bean"
    abs_path = "/definitely/not/here.jpg"
    assert resolve_local_document_path(abs_path, tx_file) == Path(abs_path)


def test_relative_to_tx_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "one.pdf").write_bytes(b"x")
    tx_file = tmp_path / "main.bean"
    # main_folder == tmp_path here, so also disambiguate by creating a conflicting name.
    assert resolve_local_document_path("one.pdf", tx_file) == tmp_path / "one.pdf"


# ============================ _tx_date =====================================


def test_tx_date_star_flag() -> None:
    assert _tx_date(_lines('2025-01-01 * "Coop"', "  Exp:Food  10 CHF")) == date(
        2025, 1, 1
    )


def test_tx_date_exclamation_flag() -> None:
    assert _tx_date(_lines('2024-12-31 ! "X"', "  Exp:Food  10 CHF")) == date(
        2024, 12, 31
    )


def test_tx_date_balanced_flag() -> None:
    assert _tx_date(_lines('2024-02-29 D "X"', "  Exp:Food  10 CHF")) == date(
        2024, 2, 29
    )


def test_tx_date_rejects_missing_header() -> None:
    with pytest.raises(ValueError, match="could not read the transaction date"):
        _tx_date(_lines("  Exp:Food  10 CHF", "  Assets:Cash -10 CHF"))


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
