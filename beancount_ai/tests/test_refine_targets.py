#!/usr/bin/env python3
"""Unit tests for the refine subcommand's target-token parser and range
validation (see docs/specs/Refine multi-range target specification.md)."""

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from beancount_ai.client.commands.refine import parse_target, validate_target_ranges

# ===========================================================================
# parse_target
# ===========================================================================


class TestParseTarget:
    def test_single_line_number(self) -> None:
        assert parse_target("42") == (42, 42)

    def test_range(self) -> None:
        assert parse_target("12-45") == (12, 45)

    def test_degenerate_range_is_single(self) -> None:
        assert parse_target("42-42") == (42, 42)

    def test_open_range_to_end(self) -> None:
        assert parse_target("12-end") == (12, None)

    def test_open_range_single_to_end(self) -> None:
        assert parse_target("5-end") == (5, None)

    @pytest.mark.parametrize(
        "token",
        [
            "0",  # line numbers start at 1
            "01",  # no leading zeroes
            "10-5",  # inverted range
            "1-",  # missing range end
            "-1",  # missing range start / negative
            "-5",
            "1-2-3",  # two hyphens
            "1-end-5",  # two hyphens with the end keyword
            "12a",
            "abc",
            "",
            " 42",
            "42 ",
            "4.2",
            "+5",
            "1--5",
            "end",  # bare keyword, no start
            "end-5",  # keyword as start
            "1-end5",  # trailing junk after the end keyword
            "12-End",  # keyword is case sensitive
        ],
    )
    def test_rejects(self, token: str) -> None:
        with pytest.raises(argparse.ArgumentTypeError):
            parse_target(token)


# ===========================================================================
# validate_target_ranges
# ===========================================================================


class TestValidateTargetRanges:
    def test_single_target_ok(self) -> None:
        assert validate_target_ranges([(10, 10)], 99) == [(10, 10)]

    def test_several_disjoint_targets_ok(self) -> None:
        targets = [(10, 10), (20, 30), (31, 50)]
        assert validate_target_ranges(targets, 99) == targets

    def test_contiguous_targets_ok(self) -> None:
        # 1-500 and 501-1000 are disjoint even though they touch.
        targets = [(1, 500), (501, 1000)]
        assert validate_target_ranges(targets, 1000) == targets

    def test_out_of_bounds_past_end(self) -> None:
        with pytest.raises(ValueError, match="out of file bounds"):
            validate_target_ranges([(5, 12)], 9)

    def test_start_past_end(self) -> None:
        with pytest.raises(ValueError, match="out of file bounds"):
            validate_target_ranges([(9, 9)], 8)

    def test_empty_list_rejected(self) -> None:
        with pytest.raises(ValueError):
            validate_target_ranges([], 9)

    def test_open_range_resolves_to_file_end(self) -> None:
        assert validate_target_ranges([(10, None)], 99) == [(10, 99)]

    def test_open_range_mixed_with_concrete_targets_ok(self) -> None:
        assert validate_target_ranges([(10, 20), (30, None)], 100) == [
            (10, 20),
            (30, 100),
        ]

    def test_open_range_then_later_target_rejected(self) -> None:
        # Once a range runs to the end of the file, nothing may follow it.
        with pytest.raises(ValueError, match="must not overlap"):
            validate_target_ranges([(10, None), (50, 60)], 100)

    def test_open_range_start_out_of_bounds_rejected(self) -> None:
        with pytest.raises(ValueError, match="out of file bounds"):
            validate_target_ranges([(12, None)], 9)

    def test_open_range_in_error_message(self) -> None:
        # The open form is rendered back as "A-end" in diagnostic output.
        with pytest.raises(ValueError, match=r"^target range 12-end out of file"):
            validate_target_ranges([(12, None)], 6)

    def test_descending_starts_rejected(self) -> None:
        with pytest.raises(ValueError, match="not strictly ascending"):
            validate_target_ranges([(100, 200), (50, 60)], 999)

    def test_repeated_single_rejected(self) -> None:
        with pytest.raises(ValueError, match="not strictly ascending"):
            validate_target_ranges([(42, 42), (42, 42)], 999)

    def test_range_starting_inside_previous_range_rejected(self) -> None:
        with pytest.raises(ValueError, match="must not overlap"):
            validate_target_ranges([(1, 500), (400, 900)], 1000)

    def test_larger_range_swallowing_smaller_rejected(self) -> None:
        with pytest.raises(ValueError, match="not strictly ascending"):
            validate_target_ranges([(50, 100), (1, 200)], 1000)


# ===========================================================================
# Argparse wiring
# ===========================================================================


def _parse_refine(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="bean-ai")
    from beancount_ai.client.commands import refine

    sp = ap.add_subparsers(dest="command")
    sp = refine.subcommand_parser(sp)
    args = ap.parse_args(["refine", *argv])
    assert isinstance(args, argparse.Namespace)
    return args


class TestArgparseWiring:
    def test_multiple_single_and_range_targets(self) -> None:
        args = _parse_refine(["f.bean", "1234", "5678-9012", "20000"])
        assert args.targets == [(1234, 1234), (5678, 9012), (20000, 20000)]

    def test_range_only(self) -> None:
        args = _parse_refine(["f.bean", "123-456"])
        assert args.targets == [(123, 456)]

    def test_open_range_to_end(self) -> None:
        args = _parse_refine(["f.bean", "5678-end"])
        assert args.targets == [(5678, None)]

    def test_open_range_among_targets(self) -> None:
        args = _parse_refine(["f.bean", "1234", "5678-end"])
        assert args.targets == [(1234, 1234), (5678, None)]

    def test_no_targets_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_refine(["f.bean"])

    def test_malformed_token_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_refine(["f.bean", "5-"])

    def test_inverted_range_rejected(self) -> None:
        with pytest.raises(SystemExit):
            _parse_refine(["f.bean", "10-5"])


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
