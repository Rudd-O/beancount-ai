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
            "12a",
            "abc",
            "",
            " 42",
            "42 ",
            "4.2",
            "+5",
            "1--5",
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
