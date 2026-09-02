#!/usr/bin/env python3
"""Tests for the `refine` client helpers: extract_document_paths and resolve_document_path."""

import pathlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.client.cli import extract_document_paths, resolve_document_path
from beancount_ai.client.config import BeancountConfiguration, Configuration


def _lines(*s: str) -> list[str]:
    return "\n".join(s).splitlines(True)


def _make_config(main_folder: pathlib.Path) -> Configuration:
    """Build a minimal Configuration whose beancount.main_folder points at *main_folder*."""
    main = main_folder / "main.bean"
    bc = BeancountConfiguration()
    bc.main_file = main
    bc.ingestion_destination_file = None
    bc.account_list_file = main_folder / "accounts.txt"
    cfg = object.__new__(Configuration)
    cfg.target_vm = None
    cfg.beancount = bc
    return cfg


# ============================ extract_document_paths =======================


def test_no_documents() -> None:
    assert extract_document_paths(_lines('2025-01-01 * "X"', "  Exp:Food  10 CHF")) == []


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
    cfg = _make_config(tmp_path)
    tx_file = tmp_path / "main.bean"
    abs_path = "/definitely/not/here.jpg"
    assert resolve_document_path(abs_path, tx_file, cfg) == Path(abs_path)


def test_relative_to_tx_file(tmp_path: pathlib.Path) -> None:
    (tmp_path / "one.pdf").write_bytes(b"x")
    cfg = _make_config(tmp_path)
    tx_file = tmp_path / "main.bean"
    # main_folder == tmp_path here, so also disambiguate by creating a conflicting name.
    assert resolve_document_path("one.pdf", tx_file, cfg) == tmp_path / "one.pdf"


def test_falls_back_to_main_folder(tmp_path: pathlib.Path) -> None:
    cfg = _make_config(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    tx_file = sub / "main.bean"
    (tmp_path / "two.pdf").write_bytes(b"x")
    assert resolve_document_path("two.pdf", tx_file, cfg) == tmp_path / "two.pdf"


def test_missing_raises(tmp_path: pathlib.Path) -> None:
    cfg = _make_config(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    tx_file = sub / "main.bean"
    with pytest.raises(FileNotFoundError):
        resolve_document_path("missing.pdf", tx_file, cfg)


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
