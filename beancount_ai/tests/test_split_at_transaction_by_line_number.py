#!/usr/bin/env python3
"""Unit tests for split_at_transaction_by_line_number and
split_into_transactions_by_range using example.beancount.

The fixture reads ``tests/example.beancount`` so you can always look up which
line number corresponds to what by opening the file in an editor.

File layout (lines are 1-based):
    20-24  TX "Annual fee credit card"
    30-37  TX "JOHN REIMBERG AARGAU"
    39-46  TX "SELECTA AG DUSSELDORF"
    48-54  TX "UBS SWITZERLAND ... PARKPLATZ 14"
    55-61  TX "KAUFFMANISCHE KANTONALBANK DOM" (last line of file)
"""

import sys
from pathlib import Path
from typing import Iterable

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from beancount_ai.client.beanfiles import (
    FileBlocks,
    classify_by_target_spans,
    split_at_transaction_by_line_number,
    split_into_transactions_by_range,
)

# ---------------------------------------------------------------------------
# Fixture: read example.beancount once, return (path, lines)
# ---------------------------------------------------------------------------

_EXAMPLE_PATH = Path(__file__).parent / "example.beancount"


@pytest.fixture(scope="module")
def beancount_lines() -> list[str]:
    """Return the full file as a list of lines with newlines preserved."""
    content = _EXAMPLE_PATH.read_text(encoding="utf-8")
    return content.splitlines(True)


# Helpers for readability. The numbers here are 0-based Python indices.
# Cross-reference against beancount_ai/tests/example.beancount (1-based).
TX_1_DATE = 19  # "Annual fee credit card" date line
TX_1_TX = 20  # first metadata of TX1
TX_1_ROOT = 23  # root-level metadata of TX1
TX_1_LEN = 5  # The transaction has five lines

TX_2_DATE = 29  # "JOHN REIMBERG AARGAU" date line
TX_2_TX = 30  # first tx account of TX2
TX_2_DEEP = 33  # deep-metadate ("mailer") of TX2 (6-space indent)

TX_3_DATE = 38  # "SELECTA AG ..." date line
TX_3_TX = 39  # first tx account of TX3

TX_4_DATE = 47  # "UBS ... PARKPLATZ" date line
TX_4_TX = 48  # first tx account of TX4

TX_5_DATE = 54  # "KAUFFMANISCHE DOM" date line
TX_5_DEEP = 57  # deep metadata ("raw_string") of TX5

TX_6_DATE = 62  # "SELECTA AG DUSSELDORF" date line
TX_6_DEEP = 65  # deep metadata ("raw_string") of TX6
TX_6_LAST = 70  # last line of the transaction


# ===========================================================================
# Happy-path: any line within a transaction returns that entire transaction
# ===========================================================================


class TestHappyPath:
    def test_date_line(self, beancount_lines: list[str]) -> None:
        _, middle, _ = split_at_transaction_by_line_number(TX_1_DATE, beancount_lines)
        assert "Annual fee credit card" in middle[0]

    def test_first_metadata_line(self, beancount_lines: list[str]) -> None:
        _, middle, _ = split_at_transaction_by_line_number(TX_1_TX, beancount_lines)
        assert "Liabilities:Credit-cards:ZKB" in "".join(middle)

    def test_deeply_indented_metadata(self, beancount_lines: list[str]) -> None:
        """Pointer at a 6-space indented line should walk back to date correctly."""
        _, middle, _ = split_at_transaction_by_line_number(TX_2_DEEP, beancount_lines)
        assert "mailer:" in "".join(middle)

    def test_returns_empty_before_for_tx_near_start(
        self, beancount_lines: list[str]
    ) -> None:
        # The balances before TX 1 count as pre-lines. So `before` is NOT empty,
        # but we verify the returned transaction is correct.
        _, middle, _ = split_at_transaction_by_line_number(TX_1_DATE, beancount_lines)
        assert any("Annual fee" in line for line in middle)

    def test_returns_empty_after_for_last_tx_in_doc(
        self, beancount_lines: list[str]
    ) -> None:
        """TX 6 (SELECTA AG DUSSELDORF) is the last line of the file."""
        _, _, after = split_at_transaction_by_line_number(TX_6_DATE, beancount_lines)
        assert after == []

    def test_middle_list_is_independent_of_pointer(
        self, beancount_lines: list[str]
    ) -> None:
        """Pointing at any line of TX 4 must produce the same middle list."""
        m1 = split_at_transaction_by_line_number(TX_4_DATE, beancount_lines)[1]
        m2 = split_at_transaction_by_line_number(50, beancount_lines)[1]  # deep in TX4
        assert m1 == m2

    def test_concatenation_restores_document(self, beancount_lines: list[str]) -> None:
        before, middle, after = split_at_transaction_by_line_number(30, beancount_lines)
        combined = "".join(before + middle + after)
        source = _EXAMPLE_PATH.read_text(encoding="utf-8")
        assert combined == source


# ===========================================================================
# Negative and out-of-range indices
# ===========================================================================


class TestNegativeAndOobIndex:
    def test_negative_index_raises(self, beancount_lines: list[str]) -> None:
        with pytest.raises(ValueError, match="cannot be less than zero"):
            split_at_transaction_by_line_number(-1, beancount_lines)

    def test_exactly_len_raises(self, beancount_lines: list[str]) -> None:
        n = len(beancount_lines)
        with pytest.raises(ValueError, match="cannot be greater"):
            split_at_transaction_by_line_number(n, beancount_lines)

    def test_beyond_len_raises(self, beancount_lines: list[str]) -> None:
        n = len(beancount_lines) + 10
        with pytest.raises(ValueError, match="cannot be greater"):
            split_at_transaction_by_line_number(n, beancount_lines)


# ===========================================================================
# Empty document
# ===========================================================================


class TestEmptyDocument:
    def test_empty_list_raises(self) -> None:
        with pytest.raises((ValueError, IndexError)):
            split_at_transaction_by_line_number(0, [])


# ===========================================================================
# Blank line between transactions is not a valid pointer
# ===========================================================================


class TestBlankBetweenTransactions:
    def test_blank_before_tx1_raises(self, beancount_lines: list[str]) -> None:
        # index 18 is the blank line between TX header and TX date.
        with pytest.raises(ValueError, match="does not point to a transaction"):
            split_at_transaction_by_line_number(18, beancount_lines)

    def test_blank_in_gap_between_txs_raises(self, beancount_lines: list[str]) -> None:
        # index 25 is the second blank line of the gap between TX1 and TX2.
        with pytest.raises(ValueError, match="does not point to a transaction"):
            split_at_transaction_by_line_number(25, beancount_lines)

    def test_blank_after_tx3_raises(self, beancount_lines: list[str]) -> None:
        with pytest.raises(ValueError, match="does not point to a transaction"):
            split_at_transaction_by_line_number(46, beancount_lines)


# ===========================================================================
# Lines that start with a digit but aren't transactions
# ===========================================================================


class TestNumericNonTransactionLines:
    def test_balance_directive(self, beancount_lines: list[str]) -> None:
        # index 4 = "2030-01-01 balance ..."
        with pytest.raises(ValueError, match="does not point to a transaction"):
            split_at_transaction_by_line_number(4, beancount_lines)

    def test_open_directive(self, beancount_lines: list[str]) -> None:
        # index 8 = "2023-09-14 open ..."
        with pytest.raises(ValueError, match="does not point to a transaction"):
            split_at_transaction_by_line_number(8, beancount_lines)


# ===========================================================================
# Lines starting with semicolon (comment) aren't valid pointers
# ===========================================================================


class TestCommentLines:
    def test_section_header_comment(self, beancount_lines: list[str]) -> None:
        # index 16 = `;; -*- mode: beancount -*-`
        with pytest.raises(ValueError, match="does not point to a transaction"):
            split_at_transaction_by_line_number(16, beancount_lines)

    def test_inline_comment_between_txs(self, beancount_lines: list[str]) -> None:
        # index 37 = `; This red bull has been accounted for.` (between TX2 and TX3)
        with pytest.raises(ValueError, match="does not point to a transaction"):
            split_at_transaction_by_line_number(37, beancount_lines)


# ===========================================================================
# Whitespace-only lines raise
# ===========================================================================


class TestWhitespaceOnlyLines:
    def test_tab_only_inside_tx_raises(self) -> None:
        """A line that contains only whitespace inside a transaction is invalid."""
        doc: list[str] = [
            '2023-01-01 * "Test"\n',
            "  Item:1 CHF\n",
            "\t\n",
            "  End:1 CHF\n",
        ]
        with pytest.raises(ValueError, match="does not point to a transaction"):
            split_at_transaction_by_line_number(2, doc)

    def test_space_only_inside_tx_raises(self) -> None:
        doc: list[str] = [
            '2023-01-01 * "Test"\n',
            "  Item:1 CHF\n",
            " \n",
            "  End:1 CHF\n",
        ]
        with pytest.raises(ValueError, match="does not point to a transaction"):
            split_at_transaction_by_line_number(2, doc)


# ===========================================================================
# Orphan indented line (no preceding date) — walks back past index 0
# ===========================================================================


class TestMetadataOnlyLine:
    def test_orphaned_indented_line_raises(self) -> None:
        """An indented line not belonging to any transaction should raise."""
        doc: list[str] = [
            "  Expenses:Foobar 1 CHF\n",
            '2023-01-01 * "X"\n',
            "  End:1 CHF\n",
        ]
        with pytest.raises(ValueError):
            split_at_transaction_by_line_number(0, doc)


# ===========================================================================
# Comment after a transaction — sits in 'after' not 'middle'
# ===========================================================================


class TestCommentAfterTransaction:
    def test_comment_after_last_tx_in_doc(self, beancount_lines: list[str]) -> None:
        _, _, after = split_at_transaction_by_line_number(TX_2_DATE, beancount_lines)
        # The inline comment on line 38 sits in `after`, not in `middle`.
        assert any(";" in ln for ln in after)


class TestCommentInTransaction:
    def test_transaction_with_comment(self, beancount_lines: list[str]) -> None:
        for n in range(TX_6_DATE, TX_6_LAST + 1):
            _, middle, _ = split_at_transaction_by_line_number(n, beancount_lines)
            assert "SELECTA" in middle[0]
            assert "Snacks" in middle[-1]


# ===========================================================================
# Reversibility — split and reassemble must reproduce the original
# ===========================================================================


class TestReversibility:
    def test_recompose_tx1(self, beancount_lines: list[str]) -> None:
        before: list[str]
        middle: list[str]
        after: list[str]
        before, middle, after = split_at_transaction_by_line_number(
            TX_1_DATE, beancount_lines
        )
        assert "".join(before + middle + after) == _EXAMPLE_PATH.read_text(
            encoding="utf-8"
        )

    def test_recompose_tx3(self, beancount_lines: list[str]) -> None:
        before: list[str]
        middle: list[str]
        after: list[str]
        before, middle, after = split_at_transaction_by_line_number(
            TX_3_DATE, beancount_lines
        )
        assert "".join(before + middle + after) == _EXAMPLE_PATH.read_text(
            encoding="utf-8"
        )

    def test_recompose_tx5(self, beancount_lines: list[str]) -> None:
        before: list[str]
        middle: list[str]
        after: list[str]
        before, middle, after = split_at_transaction_by_line_number(
            TX_5_DATE, beancount_lines
        )
        assert "".join(before + middle + after) == _EXAMPLE_PATH.read_text(
            encoding="utf-8"
        )


# ===========================================================================
# Transaction flags (* and D are standard Beancount transaction flags)
# ===========================================================================


class TestFlags:
    def test_d_flag(self) -> None:
        doc: list[str] = [
            '2026-01-01 D "Froogs"\n',
            "  End:1 CHF\n",
            "  Inc:Fg:-1 CHF\n",
        ]
        _, middle, _ = split_at_transaction_by_line_number(0, doc)
        assert len(middle) == 3

    def test_star_flag(self) -> None:
        doc: list[str] = [
            '2026-01-01 * "Froogs"\n',
            "  End:1 CHF\n",
            "  Inc:Fg:-1 CHF\n",
        ]
        _, middle, _ = split_at_transaction_by_line_number(0, doc)
        assert len(middle) == 3

    def test_two_char_flag_not_recognized(self) -> None:
        doc: list[str] = ['2026-01-01 AB "Froogs"\n', "  End:1 CHF\n"]
        with pytest.raises(ValueError, match="does not point to a transaction"):
            split_at_transaction_by_line_number(0, doc)


# ===========================================================================
# Metadata-heavy transaction
# Use TX2 ("JOHN REIMBERG AARGAU") — has 6-space indent lines.
# ===========================================================================


class TestMetadataHeavy:
    def test_pointer_inner_metadata(self, beancount_lines: list[str]) -> None:
        # index 33 is raw_string at deep indent in TX2
        _, middle, _ = split_at_transaction_by_line_number(33, beancount_lines)
        assert "JOHN REIMBERG AARGAU" in "".join(middle)
        assert "    raw_string:" in "".join(middle)

    def test_pointer_first_metadata_of_tx2(self, beancount_lines: list[str]) -> None:
        _, middle, _ = split_at_transaction_by_line_number(30, beancount_lines)
        assert "Assets:Bank:ZKB:CHF" in "".join(middle)


# ===========================================================================
# Single-line transaction edge case (tx with only a date line).
# ===========================================================================


class TestSingleElementTransaction:
    def test_date_only_line(self) -> None:
        doc: list[str] = ['2026-01-01 * "One-liner"\n']
        before, middle, after = split_at_transaction_by_line_number(0, doc)
        assert middle == ['2026-01-01 * "One-liner"\n']
        assert before == []
        assert after == []


# ===========================================================================
# Transactions with 9-series dates (digit-start regex must include them).
# ===========================================================================


class TestDateFormats:
    def test_nine_series_date(self) -> None:
        doc: list[str] = ['9202-01-01 * "Far-future"\n', "  End:1 CHF\n"]
        _, middle, _ = split_at_transaction_by_line_number(0, doc)
        assert len(middle) == 2

    def test_date_with_quoted_payee(self) -> None:
        doc: list[str] = ['2026-01-01 * "A & B" "C D"\n', "  End:1 CHF\n"]
        _, middle, _ = split_at_transaction_by_line_number(0, doc)
        assert len(middle) == 2


# ===========================================================================
# Pointer at non-transaction that happens to start with a digit.
# ===========================================================================


class TestNumericButNotTx:
    def test_fava_directive_in_balances(self, beancount_lines: list[str]) -> None:
        # Index 1 is indented fava metadata under the balance block. The backward walk hits
        # line 0 (an `open` directive), which isn't a tx. Raises.
        with pytest.raises(ValueError, match="does not point to a transaction"):
            split_at_transaction_by_line_number(1, beancount_lines)


def _debug_split_into_transactions(res: Iterable[tuple[bool, list[str]]]) -> None:  # pyright: ignore[reportUnusedFunction]
    for istran, lines in res:
        sys.stderr.write("T  " if istran else "F  ")
        for n, line in enumerate(lines):
            if n > 0:
                sys.stderr.write("   ")
            sys.stderr.write(line)


def test_split_into_transactions_by_range_start_in_middle_of_transaction(
    beancount_lines: list[str],
) -> None:
    res = split_into_transactions_by_range(beancount_lines, start_line=TX_1_DATE + 1)
    assert res[0][0] is False
    assert len(res[0][2]) == TX_1_DATE
    assert res[1][0] is True
    assert len(res[1][2]) == TX_1_LEN


# ===========================================================================
# split_into_transactions_by_range — argument validation
# ===========================================================================


class TestSplitByRangeValidation:
    """The helper rejects malformed ranges with ValueError before splitting."""

    def test_negative_start(self, beancount_lines: list[str]) -> None:
        with pytest.raises(
            ValueError, match="starting line number .* cannot be less than zero"
        ):
            split_into_transactions_by_range(beancount_lines, start_line=-1)

    def test_negative_end(self, beancount_lines: list[str]) -> None:
        with pytest.raises(
            ValueError, match="ending line number .* cannot be less than zero"
        ):
            split_into_transactions_by_range(beancount_lines, 0, -1)

    def test_start_equal_to_len(self, beancount_lines: list[str]) -> None:
        with pytest.raises(
            ValueError, match="starting line number .* cannot be greater"
        ):
            split_into_transactions_by_range(
                beancount_lines, start_line=len(beancount_lines)
            )

    def test_end_equal_to_len(self, beancount_lines: list[str]) -> None:
        with pytest.raises(ValueError, match="ending line number .* cannot be greater"):
            split_into_transactions_by_range(beancount_lines, 0, len(beancount_lines))

    def test_end_beyond_len(self, beancount_lines: list[str]) -> None:
        with pytest.raises(ValueError, match="ending line number .* cannot be greater"):
            split_into_transactions_by_range(
                beancount_lines, 0, len(beancount_lines) + 5
            )

    def test_end_before_start(self, beancount_lines: list[str]) -> None:
        with pytest.raises(
            ValueError, match="must be less than or equal than end_line"
        ):
            split_into_transactions_by_range(beancount_lines, 5, 3)

    def test_empty_document(
        self,
    ) -> None:
        with pytest.raises(ValueError):
            split_into_transactions_by_range([], 0)


# ===========================================================================
# split_into_transactions_by_range — single-transaction behaviour
# ===========================================================================


class TestSplitByRangeSingle:
    """When end_line is omitted (defaults to start_line) only the transaction
    containing start_line is flagged; everything else is non-transaction."""

    def test_default_end_returns_containing_tx(
        self, beancount_lines: list[str]
    ) -> None:
        # start at TX1's date line.
        res = split_into_transactions_by_range(beancount_lines, TX_1_DATE)
        assert res == [
            (False, 0, beancount_lines[:TX_1_DATE]),
            (True, TX_1_DATE, beancount_lines[TX_1_DATE : TX_1_DATE + TX_1_LEN]),
            (False, TX_1_DATE + TX_1_LEN, beancount_lines[TX_1_DATE + TX_1_LEN :]),
        ]

    def test_explicit_end_equal_to_start_matches_default(
        self, beancount_lines: list[str]
    ) -> None:
        assert split_into_transactions_by_range(beancount_lines, TX_1_DATE) == (
            split_into_transactions_by_range(beancount_lines, TX_1_DATE, TX_1_DATE)
        )

    def test_start_on_last_line_of_tx_walks_back(
        self, beancount_lines: list[str]
    ) -> None:
        # Pointing at TX1's final posting still returns the whole TX1.
        res = split_into_transactions_by_range(
            beancount_lines, TX_1_DATE + TX_1_LEN - 1
        )
        tx = next(l for t, _, l in res if t)
        assert tx == beancount_lines[TX_1_DATE : TX_1_DATE + TX_1_LEN]

    def test_start_deep_in_transaction_returns_same_tx(
        self, beancount_lines: list[str]
    ) -> None:
        # Deeply indented line of TX2 walks back to TX2's date line.
        mid_res = split_into_transactions_by_range(beancount_lines, TX_2_DEEP)
        date_res = split_into_transactions_by_range(beancount_lines, TX_2_DATE)
        assert next(l for t, _, l in mid_res if t) == next(
            l for t, _, l in date_res if t
        )

    def test_start_on_indented_comment_within_tx(
        self, beancount_lines: list[str]
    ) -> None:
        # An indented comment line counts as part of its enclosing transaction.
        res = split_into_transactions_by_range(beancount_lines, 69)
        tx = next(l for t, _, l in res if t)
        assert tx[0] == beancount_lines[TX_6_DATE]
        assert tx[-1] == beancount_lines[TX_6_LAST]


# ===========================================================================
# split_into_transactions_by_range — no-transaction starting points
# ===========================================================================


class TestSplitByRangeNoTransaction:
    """A start line that is not part of any transaction yields a single
    non-transaction group spanning the whole document."""

    def test_start_on_directive_first_line(self, beancount_lines: list[str]) -> None:
        res = split_into_transactions_by_range(beancount_lines, 0)
        assert res == [(False, 0, list(beancount_lines))]

    def test_start_on_section_comment(self, beancount_lines: list[str]) -> None:
        # index 37 = "; This red bull has been accounted for." (unindented comment)
        res = split_into_transactions_by_range(beancount_lines, 37)
        assert res == [(False, 0, list(beancount_lines))]

    def test_start_on_blank_gap_line(self, beancount_lines: list[str]) -> None:
        # index 25 is the second blank line of the gap between TX1 and TX2.
        res = split_into_transactions_by_range(beancount_lines, 25)
        assert res == [(False, 0, list(beancount_lines))]

    def test_balance_directive_inline_doc(
        self,
    ) -> None:
        doc = [
            "2030-01-01 balance Assets:Bank 0 CHF\n",
            "2024-01-01 open Assets:Bank CHF\n",
        ]
        assert split_into_transactions_by_range(doc, 0) == [(False, 0, list(doc))]

    def test_directives_only_inline_doc(
        self,
    ) -> None:
        doc = [
            "2023-01-01 open Assets:Bank CHF\n",
            "2030-01-01 balance Assets:Bank 0 CHF\n",
        ]
        assert split_into_transactions_by_range(doc, 0) == [(False, 0, list(doc))]

    def test_orphaned_indented_line_at_top(
        self,
    ) -> None:
        # An indented line with no preceding transaction is not part of one.
        doc = [
            "  Expenses:Foo 1 CHF\n",
            '2023-01-01 * "X"\n',
            "  End:1 CHF\n",
        ]
        res = split_into_transactions_by_range(doc, 0, 0)
        assert res == [(False, 0, list(doc))]


# ===========================================================================
# split_into_transactions_by_range — range end semantics
# ===========================================================================


class TestSplitByRangeEndLine:
    """end_line is the last index at which a transaction may *begin*; a
    beginning transaction is always included whole (its body may run past it),
    but a transaction that starts after end_line is not flagged."""

    def test_end_inside_tx_body_includes_tx_whole(
        self, beancount_lines: list[str]
    ) -> None:
        # end lands mid-body of TX1; the whole TX1 is still returned intact.
        for end in range(TX_1_DATE, TX_1_DATE + TX_1_LEN):
            res = split_into_transactions_by_range(beancount_lines, TX_1_DATE, end)
            tx = next(l for t, _, l in res if t)
            assert tx == beancount_lines[TX_1_DATE : TX_1_DATE + TX_1_LEN]

    def test_end_excludes_later_tx_starting_after_it(
        self, beancount_lines: list[str]
    ) -> None:
        # end=19 lands on TX1's date line, so only TX1 is flagged; TX2 (date 29)
        # starts after end and is excluded even though it follows later in the file.
        res = split_into_transactions_by_range(beancount_lines, 0, TX_1_DATE)
        flagged = [l for t, _, l in res if t]
        assert len(flagged) == 1
        assert flagged[0] == beancount_lines[TX_1_DATE : TX_1_DATE + TX_1_LEN]

    def test_end_inclusive_at_later_tx_start(self, beancount_lines: list[str]) -> None:
        # With end exactly on TX2's date line, TX2 is now flagged.
        res = split_into_transactions_by_range(beancount_lines, 37, TX_3_DATE)
        tx = next(l for t, _, l in res if t)
        assert tx[0] == beancount_lines[TX_3_DATE]

    def test_default_end_does_not_flag_later_tx(
        self, beancount_lines: list[str]
    ) -> None:
        # Omitting end_line limits the result to the transaction containing
        # start_line; later transactions (e.g. TX6 at the end of the file) stay
        # non-transaction.
        res = split_into_transactions_by_range(beancount_lines, TX_2_DATE)
        assert sum(1 for t, _, _ in res if t) == 1


# ===========================================================================
# split_into_transactions_by_range — multiple transactions in a range
# ===========================================================================


class TestSplitByRangeMultiple:
    def test_range_spanning_three_tx_groups(self, beancount_lines: list[str]) -> None:
        res = split_into_transactions_by_range(beancount_lines, TX_1_DATE, TX_3_DATE)
        assert len(res) == 7
        assert [t for t, _, _ in res] == [False, True, False, True, False, True, False]
        # Each non-empty False group is exactly the gap / comment between the txs.
        assert res[2][2] == beancount_lines[TX_1_DATE + TX_1_LEN : TX_2_DATE]
        assert res[4][2] == [beancount_lines[37]]
        # The three transaction groups are the intact txs.
        assert res[1][2][0] == beancount_lines[TX_1_DATE]
        assert res[3][2][0] == beancount_lines[TX_2_DATE]
        assert res[5][2][0] == beancount_lines[TX_3_DATE]

    def test_range_start_and_end_on_tx_date_lines(
        self, beancount_lines: list[str]
    ) -> None:
        res = split_into_transactions_by_range(beancount_lines, TX_2_DATE, TX_4_DATE)
        assert [t for t, _, _ in res] == [False, True, False, True, False, True, False]
        dates = [l[0] for t, _, l in res if t]
        assert dates == [
            beancount_lines[TX_2_DATE],
            beancount_lines[TX_3_DATE],
            beancount_lines[TX_4_DATE],
        ]

    def test_adjacent_txs_no_separator_get_own_groups(
        self,
    ) -> None:
        # No blank line between the two transactions: even though the per-line
        # flags form a single run, each transaction is emitted as its own
        # (flagged) group, so targeting one does not flag the other.
        doc = [
            '2023-01-01 * "A"\n',
            "  A:1 CHF\n",
            '2023-01-02 * "B"\n',
            "  B:1 CHF\n",
        ]
        res = split_into_transactions_by_range(doc, 0, 3)
        assert res == [(True, 0, doc[:2]), (True, 2, doc[2:])]

    def test_adjacent_unflagged_txs_still_merge(
        self,
    ) -> None:
        # When the second transaction's header begins past end_line it is not
        # flagged, and unflagged lines coalesce into one group as before.
        doc = [
            '2023-01-01 * "A"\n',
            "  A:1 CHF\n",
            '2023-01-02 * "B"\n',
            "  B:1 CHF\n",
        ]
        res = split_into_transactions_by_range(doc, 0, 1)
        assert res == [(True, 0, doc[:2]), (False, 2, doc[2:])]


# ===========================================================================
# split_into_transactions_by_range — invariants
# ===========================================================================


class TestSplitByRangeInvariants:
    def test_groups_alternate_when_txs_are_separated(
        self, beancount_lines: list[str]
    ) -> None:
        # TX1, TX2 and TX3 are separated by blank lines / a comment, so
        # flags still alternate here.
        res = split_into_transactions_by_range(beancount_lines, TX_1_DATE, TX_3_DATE)
        flags = [t for t, _, _ in res]
        assert flags == [False, True, False, True, False, True, False]
        assert flags[0] is False
        assert all(a is not b for a, b in zip(flags, flags[1:]))

    def test_adjacent_fixture_transactions_are_separate_groups(
        self, beancount_lines: list[str]
    ) -> None:
        # TX4 and TX5 have no blank line between them (TX5's date line
        # immediately follows TX4's last line), yet they are not merged:
        # each transaction is its own True group.
        assert TX_5_DATE == TX_4_DATE + 7
        res = split_into_transactions_by_range(beancount_lines, TX_4_DATE, TX_5_DATE)
        flags = [t for t, _, _ in res]
        assert flags == [False, True, True, False]
        assert res[1][2] == beancount_lines[TX_4_DATE : TX_5_DATE]
        # TX5 runs from its date line to index 60 (line 61 in the editor);
        # index 61 is the blank line separating it from TX6.
        assert res[2][2] == beancount_lines[TX_5_DATE:61]

    def test_flattening_reconstructs_document(self, beancount_lines: list[str]) -> None:
        # Groups preserve the original lines verbatim and in order: rejoining
        # every line of every group reproduces the source byte-for-byte.
        source = "".join(beancount_lines)
        start: int
        end: int | None
        for start, end in [  # pyright: ignore[reportUnknownVariableType]
            (TX_1_DATE, None),
            (TX_1_DATE + 1, None),
            (TX_1_DATE, TX_6_LAST),
            (0, 0),
            (0, len(beancount_lines) - 1),
        ]:
            res = split_into_transactions_by_range(beancount_lines, start, end)  # pyright: ignore[reportUnknownArgumentType]
            assert "".join(ln for _, _, grp in res for ln in grp) == source


# ===========================================================================
# classify_by_target_spans
# ===========================================================================


def _flagged_groups(res: FileBlocks) -> list[list[str]]:
    return [lines for is_tx, _, lines in res if is_tx]


def _all_transactions(beancount_lines: list[str]) -> list[tuple[int, list[str]]]:
    """Enumerate every transaction of the document as (abs_start, lines), in
    file order, with consecutive unseparated transactions kept as separate
    items (each item is exactly one transaction, starting on its date line)."""
    n = len(beancount_lines)
    full = split_into_transactions_by_range(beancount_lines, 0, n - 1)
    out: list[tuple[int, list[str]]] = []
    for is_tx, lineno, lines in full:
        if is_tx:
            out.append((lineno, lines))
    return out


class TestClassifyByTargetSpans:
    def test_single_line_in_transaction_selects_that_tx(
        self, beancount_lines: list[str]
    ) -> None:
        res = classify_by_target_spans(beancount_lines, [(TX_2_DEEP, TX_2_DEEP)])
        flagged = _flagged_groups(res)
        expected = split_at_transaction_by_line_number(TX_2_DEEP, beancount_lines)[1]
        assert flagged == [expected]
        # The flagged group is the transaction whole, beginning at its date line.
        assert flagged[0][0] == beancount_lines[TX_2_DATE]

    def test_single_line_flags_exactly_that_transaction(
        self, beancount_lines: list[str]
    ) -> None:
        # A single-line target flags exactly the transaction containing that
        # line (walk-back semantics), never any other transaction.
        for idx in (TX_1_DATE, TX_2_DATE, TX_2_DEEP, TX_5_DEEP, TX_6_DEEP):
            expected = split_at_transaction_by_line_number(idx, beancount_lines)[1]
            flagged = _flagged_groups(
                classify_by_target_spans(beancount_lines, [(idx, idx)])
            )
            assert flagged == [expected]

    def test_range_selects_exactly_the_right_transactions(
        self, beancount_lines: list[str]
    ) -> None:
        # A range target selects every transaction beginning within [start,
        # end], plus the transaction containing the start line itself when it
        # points into the middle of one (walk-back).
        all_txs = _all_transactions(beancount_lines)
        for start, end in [
            (TX_1_DATE, TX_3_DATE),
            (TX_2_DATE, TX_5_DATE),
            (TX_3_DATE, TX_3_DATE),
            (TX_2_DEEP, TX_4_DATE),
        ]:
            expected = [(s, tx) for s, tx in all_txs if s <= end and start <= s]
            containing = split_at_transaction_by_line_number(start, beancount_lines)[1]
            if containing not in [tx for _, tx in expected]:
                expected.append(
                    (next(s for s, tx in all_txs if tx == containing), containing)
                )
            expected.sort(key=lambda pair: pair[0])
            flagged = _flagged_groups(
                classify_by_target_spans(beancount_lines, [(start, end)])
            )
            assert flagged == [tx for _, tx in expected]

    def test_multiple_disjoint_spans_flag_each(
        self, beancount_lines: list[str]
    ) -> None:
        res = classify_by_target_spans(
            beancount_lines, [(TX_1_DATE, TX_1_DATE), (TX_4_DATE, TX_4_DATE)]
        )
        flagged = _flagged_groups(res)
        dates = [g[0] for g in flagged]
        assert dates == [beancount_lines[TX_1_DATE], beancount_lines[TX_4_DATE]]

    def test_mixed_single_and_range_spans(self, beancount_lines: list[str]) -> None:
        res = classify_by_target_spans(
            beancount_lines,
            [(TX_1_DATE, TX_1_DATE), (TX_3_DATE, TX_4_DATE), (TX_6_DEEP, TX_6_DEEP)],
        )
        dates = [g[0] for g in _flagged_groups(res)]
        assert dates == [
            beancount_lines[TX_1_DATE],
            beancount_lines[TX_3_DATE],
            beancount_lines[TX_4_DATE],
            beancount_lines[TX_6_DATE],
        ]

    def test_single_line_outside_any_transaction_flags_nothing(
        self, beancount_lines: list[str]
    ) -> None:
        # Index 37 is the unindented comment between TX2 and TX3.
        res = classify_by_target_spans(beancount_lines, [(37, 37)])
        assert _flagged_groups(res) == []
        assert "".join(ln for _, _, grp in res for ln in grp) == "".join(
            beancount_lines
        )

    def test_full_file_span_selects_every_transaction(
        self, beancount_lines: list[str]
    ) -> None:
        res = classify_by_target_spans(beancount_lines, [(0, len(beancount_lines) - 1)])
        dates = [g[0] for g in _flagged_groups(res)]
        for d in (TX_1_DATE, TX_2_DATE, TX_3_DATE, TX_4_DATE, TX_5_DATE, TX_6_DATE):
            assert beancount_lines[d] in dates

    def test_overlapping_spans_flag_each_tx_once(
        self, beancount_lines: list[str]
    ) -> None:
        # Overlaps are rejected by the command-line validation, but the
        # classifier itself must flag a transaction at most once.
        res = classify_by_target_spans(
            beancount_lines, [(TX_2_DATE, TX_3_DATE), (TX_2_DATE, TX_5_DATE)]
        )
        dates = [g[0] for g in _flagged_groups(res)]
        assert dates == [
            beancount_lines[TX_2_DATE],
            beancount_lines[TX_3_DATE],
            beancount_lines[TX_4_DATE],
            beancount_lines[TX_5_DATE],
        ]

    def test_empty_document(self) -> None:
        assert classify_by_target_spans([], [(0, 0)]) == []

    def test_adjacent_txs_are_subsplit_into_own_groups(self) -> None:
        # No blank line between the transactions, so the base classifier merges
        # them; classify_by_target_spans must still split them back apart so
        # that targeting the second transaction does not flag the first.
        doc = [
            '2023-01-01 * "A"\n',
            "  A:1 CHF\n",
            '2023-01-02 * "B"\n',
            "  B:1 CHF\n",
        ]
        res = classify_by_target_spans(doc, [(0, 1)])
        assert res == [(True, 0, doc[:2]), (False, 2, doc[2:])]
        res = classify_by_target_spans(doc, [(2, 3)])
        assert res == [(False, 0, doc[:2]), (True, 2, doc[2:])]
        res = classify_by_target_spans(doc, [(0, 0)])
        assert res == [(True, 0, doc[:2]), (False, 2, doc[2:])]

    def test_flattening_reconstructs_document(self, beancount_lines: list[str]) -> None:
        source = "".join(beancount_lines)
        for spans in [
            [(TX_1_DATE, TX_1_DATE)],
            [(TX_1_DATE, TX_2_DEEP), (TX_5_DATE, TX_5_DEEP)],
            [(0, len(beancount_lines) - 1)],
            [(37, 37)],
        ]:
            res = classify_by_target_spans(beancount_lines, spans)
            assert "".join(ln for _, _, grp in res for ln in grp) == source
