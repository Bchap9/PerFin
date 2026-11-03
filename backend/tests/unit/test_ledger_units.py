# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure ledger helpers: balance computation, FX quotes, the default category set."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.domain.accounts.balances import (
    Anchor,
    DailyBalance,
    Movement,
    balance_on,
    best_anchors,
    daily_balances,
    pick_anchor,
    running_balance,
)
from app.domain.accounts.models import (
    AccountType,
    BalanceSource,
    default_is_asset,
    default_manual_balance,
)
from app.domain.fx.service import RateQuote, _choose
from app.domain.transactions.categories import DEFAULT_CATEGORIES, default_category_keys
from app.domain.transactions.models import NAME_MAX

D = Decimal
JAN = date(2026, 1, 1)


def day(n: int) -> date:
    return JAN + timedelta(days=n - 1)


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


def test_anchor_priority_and_latest_date() -> None:
    snaps = [
        Anchor(day(5), D("100"), BalanceSource.COMPUTED),
        Anchor(day(5), D("90"), BalanceSource.STATEMENT),
        Anchor(day(5), D("95"), BalanceSource.MANUAL),
        Anchor(day(3), D("10"), BalanceSource.STATEMENT),
        Anchor(day(9), D("1"), BalanceSource.COMPUTED),
    ]
    assert [a.balance for a in best_anchors(snaps)] == [D("10"), D("95"), D("1")]
    assert pick_anchor(snaps, day(4)) == snaps[3]
    assert pick_anchor(snaps, day(5)) == snaps[2]
    assert pick_anchor(snaps, day(8)) == snaps[2]
    assert pick_anchor(snaps, day(2)) is None
    without_manual = snaps[:2]
    assert pick_anchor(without_manual, day(6)) == snaps[1]


def test_balance_on_transactions_only() -> None:
    moves = [Movement(day(1), D("100")), Movement(day(2), D("-30.25")), Movement(day(4), D("5"))]
    assert balance_on([], moves, day(1)) == D("100")
    assert balance_on([], moves, day(3)) == D("69.75")
    assert balance_on([], moves, day(10)) == D("74.75")
    assert balance_on([], moves, date(2025, 12, 31)) == 0


def test_snapshot_includes_its_own_day() -> None:
    moves = [Movement(day(1), D("100")), Movement(day(2), D("-30")), Movement(day(3), D("-5"))]
    snaps = [Anchor(day(2), D("1000"), BalanceSource.STATEMENT)]
    assert balance_on(snaps, moves, day(1)) == D("100")
    assert balance_on(snaps, moves, day(2)) == D("1000")  # day 2's -30 is inside it
    assert balance_on(snaps, moves, day(3)) == D("995")


def test_manual_accounts_ignore_transactions() -> None:
    moves = [Movement(day(2), D("-50000"))]
    snaps = [Anchor(day(1), D("300000")), Anchor(day(10), D("310000"))]
    assert balance_on(snaps, moves, day(5), manual=True) == D("300000")
    assert balance_on(snaps, moves, day(10), manual=True) == D("310000")
    assert balance_on([], moves, day(5), manual=True) == 0


def test_daily_balances_reanchor_and_match_balance_on() -> None:
    moves = [
        Movement(day(1), D("10")),
        Movement(day(3), D("5")),
        Movement(day(3), D("-2.5")),
        Movement(day(6), D("1")),
        Movement(day(8), D("100")),
    ]
    snaps = [Anchor(day(4), D("50"), BalanceSource.STATEMENT)]
    series = daily_balances(snaps, moves, day(2), day(8))
    assert series == [
        DailyBalance(day(2), D("10")),
        DailyBalance(day(3), D("12.5")),
        DailyBalance(day(4), D("50")),
        DailyBalance(day(5), D("50")),
        DailyBalance(day(6), D("51")),
        DailyBalance(day(7), D("51")),
        DailyBalance(day(8), D("151")),
    ]
    for point in series:
        assert point.balance == balance_on(snaps, moves, point.on)
    assert daily_balances(snaps, moves, day(5), day(4)) == []


def test_daily_balances_manual() -> None:
    snaps = [Anchor(day(2), D("7")), Anchor(day(4), D("9"))]
    series = daily_balances(snaps, [Movement(day(3), D("1"))], day(1), day(5), manual=True)
    assert [p.balance for p in series] == [D(0), D(7), D(7), D(9), D(9)]


def test_running_balance() -> None:
    assert running_balance(D("10"), [D("1"), D("-2.5"), D("0.000001")]) == [
        D("11"),
        D("8.5"),
        D("8.500001"),
    ]
    assert running_balance(D("3"), []) == []


def test_account_type_defaults() -> None:
    liabilities = {t for t in AccountType if not default_is_asset(t)}
    assert liabilities == {
        AccountType.CREDIT_CARD,
        AccountType.LOAN,
        AccountType.MORTGAGE,
        AccountType.LINE_OF_CREDIT,
        AccountType.MANUAL_LIABILITY,
    }
    manual = {t for t in AccountType if default_manual_balance(t)}
    assert manual == {AccountType.MANUAL_ASSET, AccountType.MANUAL_LIABILITY}
    assert {t.value for t in AccountType} >= {
        "checking",
        "savings",
        "credit_card",
        "cash",
        "brokerage",
        "retirement",
        "loan",
        "mortgage",
        "line_of_credit",
        "manual_asset",
        "manual_liability",
    }


# ---------------------------------------------------------------------------
# FX quotes
# ---------------------------------------------------------------------------


def test_rate_quote_direct_and_inverse_round_half_even() -> None:
    direct = RateQuote("EUR", "USD", day(1), D("1.0850"), inverted=False)
    assert direct.apply(D("100")) == D("108.500000")
    assert direct.rate == D("1.0850")
    inverse = RateQuote("USD", "EUR", day(1), D("1.0850"), inverted=True)
    # 100 / 1.085 = 92.1658986175...
    assert inverse.apply(D("100")) == D("92.165899")
    assert inverse.rate == D("0.9216589861751152073732718894009217")  # 34 digits
    # Half-even at the 6th decimal: 0.0000025 -> 0.000002, 0.0000035 -> 0.000004.
    half = RateQuote("AAA", "BBB", day(1), D("0.5"), inverted=False)
    assert half.apply(D("0.000005")) == D("0.000002")
    assert half.apply(D("0.000007")) == D("0.000004")
    assert half.apply(D("-0.000005")) == D("-0.000002")


def test_choose_prefers_the_later_rate_then_direct() -> None:
    from app.domain.fx.models import FxRate

    direct = FxRate(base="EUR", quote="USD", rate_date=day(3), rate=D("1.1"))
    newer_inverse = FxRate(base="USD", quote="EUR", rate_date=day(4), rate=D("0.8"))
    same_day_inverse = FxRate(base="USD", quote="EUR", rate_date=day(3), rate=D("0.8"))
    picked = _choose(direct, newer_inverse, "EUR", "USD")
    assert picked is not None
    assert (picked.inverted, picked.rate_date) == (True, day(4))
    tie = _choose(direct, same_day_inverse, "EUR", "USD")
    assert tie is not None
    assert not tie.inverted
    assert _choose(None, None, "EUR", "USD") is None


# ---------------------------------------------------------------------------
# Default categories
# ---------------------------------------------------------------------------


def test_default_category_set_is_well_formed() -> None:
    keys = default_category_keys()
    assert len(keys) == len(set(keys))
    assert all(len(k) <= 64 for k in keys)
    top_names = [g.name for g in DEFAULT_CATEGORIES]
    assert len(top_names) == len(set(top_names))
    kinds = {g.kind.value for g in DEFAULT_CATEGORIES}
    assert kinds == {"expense", "income", "transfer", "ignore"}
    for group in DEFAULT_CATEGORIES:
        assert "." not in group.key
        assert group.children, group.key
        names = [c.name for c in group.children]
        assert len(names) == len(set(names)), group.key
        for child in group.children:
            assert child.key.startswith(group.key + ".")
            assert child.kind is group.kind
            assert 0 < len(child.name) <= NAME_MAX
            assert not child.children


@pytest.mark.parametrize("key", ["income.salary", "food.groceries", "transfers.between_accounts"])
def test_default_set_has_the_basics(key: str) -> None:
    assert key in default_category_keys()
