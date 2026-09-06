#!/usr/bin/env python3
"""Unit tests for load_live_accounts / accounts_for_prompt (dynamic account list).

These follow the style of test_beancount_lock.py: a real BeancountConfiguration
backed by a tmp_path ledger, with accounts marked via bean-ai-include metadata
on their open directives (no static account file).
"""

import json
import pathlib
import sys
from datetime import date
from typing import Any, Generator
from unittest import mock

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent.parent))

from beancount_ai.client.beancount_loader import (  # type: ignore
    AccountRef,
    accounts_for_prompt,
    load_live_accounts,
)
from beancount_ai.client.config import Configuration

AS_OF = date(2021, 1, 1)


# ===========================================================================
# Helpers
# ===========================================================================


def _write_ledger(folder: pathlib.Path, text: str) -> pathlib.Path:
    main = folder / "main.bean"
    main.write_text(text, encoding="utf-8")
    return main


def _config_for(ledger_text: str, tmp_path: pathlib.Path) -> pathlib.Path:
    main = _write_ledger(tmp_path, ledger_text)
    # The ledger must parse as healthy for accounts_for_prompt; a lock file is
    # taken on construction (and released at the end of each test by GC).
    return main


def _names(refs: list[AccountRef]) -> list[str]:
    return [r["name"] for r in refs]


# ===========================================================================
# load_live_accounts
# ===========================================================================


class TestMarkerPlacement:
    def test_included_account_include_self(self, tmp_path: pathlib.Path) -> None:
        """An account with bean-ai-include: "yes" on its own open is included;
        but children are NOT pulled in (that requires "recursively")."""
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "yes"\n'
            "2020-01-01 open Expenses:Food:Groceries\n"
            "2020-01-01 open Expenses:Food:Restaurants\n"
            "2020-01-01 open Expenses:Unrelated\n",
            tmp_path,
        )
        refs = load_live_accounts(main, AS_OF)
        assert _names(refs) == ["Expenses:Food"]

    def test_account_closed_does_not_include_self_but_subaccounts_opened_are_included(
        self, tmp_path: pathlib.Path
    ) -> None:
        """An account with bean-ai-include: "recursively" then closed is not included;
        but children which are open are pulled in."""
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "recursively"\n'
            "2020-02-28 close Expenses:Food\n"
            "; from this point onwards we will use specific food expense accounts\n"
            "2020-03-01 open Expenses:Food:Groceries\n"
            "2020-03-01 open Expenses:Food:Restaurants\n"
            "2020-03-01 open Expenses:Unrelated\n",
            tmp_path,
        )
        refs = load_live_accounts(main, AS_OF)
        assert _names(refs) == ["Expenses:Food:Groceries", "Expenses:Food:Restaurants"]

    def test_marker_on_the_account_include_subtree(
        self, tmp_path: pathlib.Path
    ) -> None:
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "recursively"\n'
            "2020-01-01 open Expenses:Food:Groceries\n"
            "2020-01-01 open Expenses:Food:Restaurants\n"
            "2020-01-01 open Expenses:Unrelated\n",
            tmp_path,
        )
        refs = load_live_accounts(main, AS_OF)
        assert _names(refs) == [
            "Expenses:Food",
            "Expenses:Food:Groceries",
            "Expenses:Food:Restaurants",
        ]

    def test_marker_on_ancestor(self, tmp_path: pathlib.Path) -> None:
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "recursively"\n'
            "2020-01-01 open Expenses:Food:Groceries\n"
            "2020-01-01 open Expenses:Food:Groceries:Organic\n",
            tmp_path,
        )
        refs = load_live_accounts(main, AS_OF)
        # The grandchild is unmarked but is pulled in by the recursive marker
        # on Expenses:Food two levels up.
        assert _names(refs) == [
            "Expenses:Food",
            "Expenses:Food:Groceries",
            "Expenses:Food:Groceries:Organic",
        ]

    def test_marker_on_closed_ancestor_still_pulls_live_children(
        self, tmp_path: pathlib.Path
    ) -> None:
        """Closing a parent does not stop its policy from propagating: the
        closed ancestor's recursive marker still pulls in live children,
        while the closed ancestor itself is not included."""
        main = _config_for(
            "2020-01-01 open Expenses:Old\n"
            '  bean-ai-include: "recursively"\n'
            "2020-06-01 close Expenses:Old\n"
            "2020-01-01 open Expenses:Old:Leaf\n",
            tmp_path,
        )
        # Expenses:Old is closed before AS_OF, so it is not itself offered —
        # but its marker still reaches the live leaf.
        assert _names(load_live_accounts(main, AS_OF)) == ["Expenses:Old:Leaf"]


class TestCaseDateRules:
    def test_open_in_future_is_excluded(self, tmp_path: pathlib.Path) -> None:
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "recursively"\n'
            "2025-01-01 open Expenses:Food:Later\n",
            tmp_path,
        )
        assert _names(load_live_accounts(main, AS_OF)) == ["Expenses:Food"]
        assert _names(load_live_accounts(main, date(2026, 1, 1))) == [
            "Expenses:Food",
            "Expenses:Food:Later",
        ]

    def test_close_after_as_of_is_included(self, tmp_path: pathlib.Path) -> None:
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "recursively"\n'
            "2099-01-01 close Expenses:Food\n",
            tmp_path,
        )
        assert _names(load_live_accounts(main, AS_OF)) == ["Expenses:Food"]

    def test_close_before_as_of_is_excluded(self, tmp_path: pathlib.Path) -> None:
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "recursively"\n'
            "2020-06-01 close Expenses:Food\n",
            tmp_path,
        )
        assert load_live_accounts(main, AS_OF) == []

    def test_marker_rules_comes_from_latest_open_le_as_of(
        self, tmp_path: pathlib.Path
    ) -> None:
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "recursively"\n'
            '  bean-ai-rules: "supermarket runs"\n',
            tmp_path,
        )
        refs = load_live_accounts(main, AS_OF)
        assert refs == [AccountRef(name="Expenses:Food", rule="supermarket runs")]


class TestRules:
    def test_rules_are_per_account_and_do_not_inherit(
        self, tmp_path: pathlib.Path
    ) -> None:
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "recursively"\n'
            '  bean-ai-rules: "parent rule"\n'
            "2020-01-01 open Expenses:Food:Groceries\n"
            '  bean-ai-rules: "child rule"\n'
            "2020-01-01 open Expenses:Food:Snacks\n",
            tmp_path,
        )
        refs = load_live_accounts(main, AS_OF)
        byname = {r["name"]: r.get("rule") for r in refs}
        assert byname == {
            "Expenses:Food": "parent rule",
            "Expenses:Food:Groceries": "child rule",
            "Expenses:Food:Snacks": None,
        }

    def test_no_rules_metadata_means_none(self, tmp_path: pathlib.Path) -> None:
        main = _config_for(
            '2020-01-01 open Assets:Cash:CHF\n  bean-ai-include: "recursively"\n',
            tmp_path,
        )
        refs = load_live_accounts(main, AS_OF)
        assert refs == [AccountRef(name="Assets:Cash:CHF")]

    def test_account_closed_does_not_impact_future_subaccount_rules(
        self, tmp_path: pathlib.Path
    ) -> None:
        """An account with bean-ai-include: "recursively" then closed is not included;
        but children which are open are pulled in, and their bean-ai-rules are respected."""
        main = _config_for(
            "2018-12-01 open Expenses:Current:Family:Wife\n"
            '  bean-ai-include: "recursively"\n'
            "2019-01-01 close Expenses:Current:Family:Wife\n"
            "2019-01-01 open Expenses:Current:Family:Wife:Clothing\n"
            '  bean-ai-rules: "Female clothing goes here."\n'
            "2019-01-01 open Expenses:Current:Family:Wife:Cosmetics\n"
            '  bean-ai-rules: "Cosmetics go here, including hyaluron."\n'
            "2019-01-01 open Expenses:Current:Family:Wife:Groceries\n"
            '  bean-ai-rules: "Always assign to this when you see line items mentioning Living salad trio, other leafy vegetables, halbrahm (skim) milk / milchdrink / m-drink, creme fraiche, Lamate, Candida or Meridol toothpaste, Toast Vollkorn, salads / salad dressings, or Gruyere / Tilsiter cheese slices and packaged too."\n'
            "2019-01-01 open Expenses:Current:Family:Wife:Hygiene\n"
            '  bean-ai-rules: "Tampons, pads, linsenmittel (contact lens fluid), and other hygiene products used by women."\n'
            "2019-01-01 open Expenses:Current:Family:Wife:Snacks\n"
            '  bean-ai-rules: "Use this when you spot coffee shop / confiserie receipts as the wife loves coffee shops / confiseries. Also Brezelkönig. Also use when buying various types of coffee in grocery stores, or mint chocolate ice cream, or Signature La-Pistachio stuff, or Eve bottled drinks (e.g. lychee-flavored), flavored or plain (non-sparkling) water, mate drinks, or anything matcha-related."\n',
            tmp_path,
        )
        refs = load_live_accounts(main, AS_OF)
        byname = {r["name"]: r.get("rule") for r in refs}
        assert byname == {
            "Expenses:Current:Family:Wife:Clothing": "Female clothing goes here.",
            "Expenses:Current:Family:Wife:Cosmetics": "Cosmetics go here, including hyaluron.",
            "Expenses:Current:Family:Wife:Groceries": "Always assign to this when you see line items mentioning Living salad trio, other leafy vegetables, halbrahm (skim) milk / milchdrink / m-drink, creme fraiche, Lamate, Candida or Meridol toothpaste, Toast Vollkorn, salads / salad dressings, or Gruyere / Tilsiter cheese slices and packaged too.",
            "Expenses:Current:Family:Wife:Hygiene": "Tampons, pads, linsenmittel (contact lens fluid), and other hygiene products used by women.",
            "Expenses:Current:Family:Wife:Snacks": "Use this when you spot coffee shop / confiserie receipts as the wife loves coffee shops / confiseries. Also Brezelkönig. Also use when buying various types of coffee in grocery stores, or mint chocolate ice cream, or Signature La-Pistachio stuff, or Eve bottled drinks (e.g. lychee-flavored), flavored or plain (non-sparkling) water, mate drinks, or anything matcha-related.",
        }


# ===========================================================================
# bean-ai-exclude tests
# ===========================================================================


class TestExclude:
    def test_exclude_yes_blocks_self(self, tmp_path: pathlib.Path) -> None:
        """Under a recursive include, an account with exclude:"yes" is dropped;
        its children (via the recursive include from the ancestor) remain."""
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "recursively"\n'
            "2020-01-01 open Expenses:Food:Groceries\n"
            '  bean-ai-exclude: "yes"\n'
            "2020-01-01 open Expenses:Food:Restaurants\n",
            tmp_path,
        )
        assert _names(load_live_accounts(main, AS_OF)) == [
            "Expenses:Food",
            "Expenses:Food:Restaurants",
        ]

    def test_exclude_recursively_blocks_subtree(self, tmp_path: pathlib.Path) -> None:
        """Under a recursive include, an account with exclude:"recursively"
        drops itself and all live children beneath it."""
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "recursively"\n'
            "2020-01-01 open Expenses:Food:Bakery\n"
            '  bean-ai-exclude: "recursively"\n'
            "2020-01-01 open Expenses:Food:Bakery:Fancy\n"
            "2020-01-01 open Expenses:Food:Restaurants\n",
            tmp_path,
        )
        assert _names(load_live_accounts(main, AS_OF)) == [
            "Expenses:Food",
            "Expenses:Food:Restaurants",
        ]

    def test_explicit_include_overrides_recursive_exclude(
        self, tmp_path: pathlib.Path
    ) -> None:
        """An account with its own explicit bean-ai-include under an
        ancestor's bean-ai-exclude:"recursively" is re-included."""
        main = _config_for(
            "2020-01-01 open Assets:Banks\n"
            '  bean-ai-include: "recursively"\n'
            "2020-01-01 open Assets:Banks:Internal\n"
            '  bean-ai-exclude: "recursively"\n'
            "2020-01-01 open Assets:Banks:Internal:Approved\n"
            '  bean-ai-include: "yes"\n'
            "2020-01-01 open Assets:Banks:Internal:Blocked\n",
            tmp_path,
        )
        assert _names(load_live_accounts(main, AS_OF)) == [
            "Assets:Banks",
            "Assets:Banks:Internal:Approved",
        ]


# ===========================================================================
# Validation
# ===========================================================================


class TestValidation:
    @pytest.mark.parametrize(
        "key, bad_value, match",
        [
            ("bean-ai-include", '"yeppers"', "'yeppers'"),
            ("bean-ai-include", '"all"', "'all'"),
            ("bean-ai-include", '"recursive "', "'recursive '"),
            ("bean-ai-include", "42", "42"),
            ("bean-ai-exclude", '"nope"', "'nope'"),
            ("bean-ai-exclude", "42", "42"),
        ],
    )
    def test_invalid_metadata_value(
        self,
        tmp_path: pathlib.Path,
        key: str,
        bad_value: str,
        match: str,
    ) -> None:
        main = _config_for(
            f"2020-01-01 open Expenses:Food\n  {key}: {bad_value}\n", tmp_path
        )
        with pytest.raises(ValueError, match=match):
            load_live_accounts(main, AS_OF)

    def test_numeric_bean_ai_include(self, tmp_path: pathlib.Path) -> None:
        main = _config_for(
            "2020-01-01 open Expenses:Food\n  bean-ai-include: 42\n", tmp_path
        )
        with pytest.raises(ValueError, match="invalid bean-ai-include"):
            load_live_accounts(main, AS_OF)

    def test_numeric_bean_ai_rules(self, tmp_path: pathlib.Path) -> None:
        main = _config_for(
            "2020-01-01 open Expenses:Food\n  bean-ai-rules: 42\n", tmp_path
        )
        with pytest.raises(ValueError, match="not a string"):
            load_live_accounts(main, AS_OF)


class TestUnopenedAndErrors:
    def test_unopened_account_in_postings_excluded(
        self, tmp_path: pathlib.Path
    ) -> None:
        main = _config_for(
            "2020-01-01 open Expenses:Food\n"
            '  bean-ai-include: "recursively"\n'
            "2020-01-01 open Expenses:Food:Groceries\n"
            "2020-01-01 open Assets:Cash:CHF\n"
            '2020-01-01 * "X" "Y"\n'
            "  Expenses:Food:Groceries  5.00 CHF\n"
            "  Assets:Cash:CHF         -5.00 CHF\n",
            tmp_path,
        )
        refs = load_live_accounts(main, AS_OF)
        assert _names(refs) == ["Expenses:Food", "Expenses:Food:Groceries"]


class TestOutputShape:
    def test_sorted_by_name(self, tmp_path: pathlib.Path) -> None:
        main = _config_for(
            "2020-01-01 open Income:Salaries\n"
            '  bean-ai-include: "yes"\n'
            "2020-01-01 open Assets:Cash:CHF\n"
            '  bean-ai-include: "yes"\n'
            "2020-01-01 open Liabilities:Credit\n"
            '  bean-ai-include: "yes"\n',
            tmp_path,
        )
        assert _names(load_live_accounts(main, AS_OF)) == [
            "Assets:Cash:CHF",
            "Income:Salaries",
            "Liabilities:Credit",
        ]

    def test_shallowest_account_include_itself_and_children(
        self, tmp_path: pathlib.Path
    ) -> None:
        main = _config_for(
            "2020-01-01 open Expenses:Travel\n"
            '  bean-ai-include: "recursively"\n'
            "2020-01-01 open Expenses:Travel:Airfare\n",
            tmp_path,
        )
        refs = load_live_accounts(main, AS_OF)
        assert _names(refs) == ["Expenses:Travel", "Expenses:Travel:Airfare"]


# ===========================================================================
# accounts_for_prompt
# ===========================================================================


class TestAccountsForPrompt:
    def test_returns_derived_accounts(self, tmp_path: pathlib.Path) -> None:
        main = _config_for(
            '2020-01-01 open Expenses:Food\n  bean-ai-include: "recursively"\n',
            tmp_path,
        )
        refs = accounts_for_prompt(main, run_date=AS_OF)
        assert refs == [AccountRef(name="Expenses:Food")]

    def test_uses_today_when_run_date_none(self, tmp_path: pathlib.Path) -> None:
        main = _config_for(
            '2020-01-01 open Expenses:Food\n  bean-ai-include: "recursively"\n',
            tmp_path,
        )
        # 2020 < today, so it resolves without pinning the date.
        refs = accounts_for_prompt(main)
        assert _names(refs) == ["Expenses:Food"]

    def test_empty_result_raises(self, tmp_path: pathlib.Path) -> None:
        main = _config_for("2020-01-01 open Expenses:Food\n", tmp_path)
        with pytest.raises(RuntimeError, match="no accounts"):
            accounts_for_prompt(main, run_date=AS_OF)


# ===========================================================================
# Configuration.load() legacy account_list_file error
# ===========================================================================


def _write_client_config(
    tmp_path: pathlib.Path, bean_section: dict[str, Any]
) -> pathlib.Path:
    cfg: dict[str, Any] = {"target_vm": None, "beancount": bean_section}
    fp = tmp_path / "bean-ai.json"
    fp.write_text(json.dumps(cfg))
    return fp


class TestConfigurationLoadLegacyKey:
    @pytest.fixture(autouse=True)
    def _fresh_instance(self) -> Generator[None, None, None]:
        with mock.patch.object(Configuration, "instance", None):
            yield

    def test_raises_when_key_present(self, tmp_path: pathlib.Path) -> None:
        main = _write_ledger(tmp_path, "2020-01-01 open Expenses:Food\n")
        fp = _write_client_config(
            tmp_path,
            {
                "main_file": str(main),
                "account_list_file": str(tmp_path / "bean-ai.accounts"),
            },
        )
        with pytest.raises(ValueError, match="account_list_file is no longer used"):
            Configuration.load(str(fp))

    def test_no_error_when_key_absent(self, tmp_path: pathlib.Path) -> None:
        main = _write_ledger(tmp_path, "2020-01-01 open Expenses:Food\n")
        fp = _write_client_config(tmp_path, {"main_file": str(main)})
        cfg = Configuration.load(str(fp))
        assert cfg.beancount.main_file == main
        cfg.beancount.unlock()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
