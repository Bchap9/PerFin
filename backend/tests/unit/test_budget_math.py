# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure budget, goal and report arithmetic (no database).

Periods, schedule occurrences, the linear projection, rollover, goal
progress and projected completion, percentages and the category roll-up.
Every expectation is an exact ``Decimal`` or date.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.errors import ProblemError
from app.domain.budgets.models import BudgetPeriod
from app.domain.budgets.reports import (
    MAX_OCCURRENCES,
    linear_projection,
    midpoint,
    occurrences,
    period_at,
    period_containing,
    rollover_carry,
)
from app.domain.goals.progress import compute
from app.domain.reports.dates import (
    add_months,
    check_range,
    iter_months,
    month_end,
    parse_month,
    parse_period_date,
)
from app.domain.reports.ledger import CategoryNode, CategoryTree
from app.domain.reports.service import percent
from app.domain.transactions.models import Cadence, CategoryKind

D = Decimal


# ---- calendar ----------------------------------------------------------------------


def test_add_months_clamps_the_day() -> None:
    assert add_months(date(2026, 1, 31), 1) == date(2026, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert add_months(date(2026, 11, 15), 3) == date(2027, 2, 15)
    assert add_months(date(2026, 1, 1), -13) == date(2024, 12, 1)
    with pytest.raises(OverflowError):
        add_months(date(9999, 12, 1), 1)


def test_month_helpers() -> None:
    assert month_end(date(2028, 2, 3)) == date(2028, 2, 29)
    assert iter_months(date(2025, 11, 20), date(2026, 2, 1)) == [
        date(2025, 11, 1),
        date(2025, 12, 1),
        date(2026, 1, 1),
        date(2026, 2, 1),
    ]
    assert parse_month("2026-03") == date(2026, 3, 1)
    assert parse_period_date("2026-03-17") == date(2026, 3, 17)
    assert parse_period_date("2026-03") == date(2026, 3, 1)
    for bad in ("2026-3", "2026-00", "2026-13", "26-03", "2026-02-30", "2026-03-01T00", ""):
        with pytest.raises(ProblemError) as raised:
            parse_period_date(bad)
        assert raised.value.slug == "invalid-period"


def test_ranges_are_bounded() -> None:
    check_range(date(2016, 1, 1), date(2026, 1, 7))  # 3660 days
    with pytest.raises(ProblemError) as too_long:
        check_range(date(2016, 1, 1), date(2026, 1, 8))
    assert too_long.value.slug == "range-too-long"
    with pytest.raises(ProblemError) as inverted:
        check_range(date(2026, 1, 2), date(2026, 1, 1))
    assert inverted.value.slug == "invalid-range"


# ---- periods -----------------------------------------------------------------------


def test_monthly_and_annual_periods() -> None:
    start = date(2026, 1, 1)
    assert period_containing(BudgetPeriod.MONTHLY, start, None, date(2026, 3, 17)) == period_at(
        BudgetPeriod.MONTHLY, start, None, 2
    )
    march = period_at(BudgetPeriod.MONTHLY, start, None, 2)
    assert (march.start, march.end, march.days) == (date(2026, 3, 1), date(2026, 3, 31), 31)
    fiscal = period_containing(BudgetPeriod.ANNUAL, date(2025, 4, 1), None, date(2026, 3, 31))
    assert (fiscal.index, fiscal.start, fiscal.end) == (0, date(2025, 4, 1), date(2026, 3, 31))
    nxt = period_containing(BudgetPeriod.ANNUAL, date(2025, 4, 1), None, date(2026, 4, 1))
    assert (nxt.index, nxt.start, nxt.end) == (1, date(2026, 4, 1), date(2027, 3, 31))


def test_custom_period_and_out_of_range() -> None:
    period = period_containing(
        BudgetPeriod.CUSTOM, date(2026, 3, 10), date(2026, 3, 20), date(2026, 3, 20)
    )
    assert (period.start, period.end, period.days) == (date(2026, 3, 10), date(2026, 3, 20), 11)
    for day in (date(2026, 3, 9), date(2026, 3, 21)):
        with pytest.raises(ProblemError) as raised:
            period_containing(BudgetPeriod.CUSTOM, date(2026, 3, 10), date(2026, 3, 20), day)
        assert raised.value.slug == "period-out-of-range"
    with pytest.raises(ProblemError):
        period_containing(BudgetPeriod.MONTHLY, date(2026, 1, 1), None, date(2025, 12, 31))
    with pytest.raises(ProblemError):
        period_containing(BudgetPeriod.MONTHLY, date(2026, 1, 1), None, date(9500, 1, 1))


# ---- schedules, projection, rollover -------------------------------------------------


def test_occurrences() -> None:
    weekly = occurrences(date(2026, 3, 4), Cadence.WEEKLY, 1, date(2026, 3, 11), date(2026, 3, 31))
    assert weekly == [date(2026, 3, 11), date(2026, 3, 18), date(2026, 3, 25)]
    fortnightly = occurrences(
        date(2026, 1, 2), Cadence.WEEKLY, 2, date(2026, 3, 1), date(2026, 3, 31)
    )
    assert fortnightly == [date(2026, 3, 13), date(2026, 3, 27)]
    month_end_due = occurrences(
        date(2026, 1, 31), Cadence.MONTHLY, 1, date(2026, 2, 1), date(2026, 4, 30)
    )
    assert month_end_due == [date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]
    quarterly = occurrences(
        date(2025, 11, 15), Cadence.QUARTERLY, 1, date(2026, 1, 1), date(2026, 12, 31)
    )
    assert quarterly == [
        date(2026, 2, 15),
        date(2026, 5, 15),
        date(2026, 8, 15),
        date(2026, 11, 15),
    ]
    yearly = occurrences(date(2020, 2, 29), Cadence.YEARLY, 1, date(2026, 1, 1), date(2026, 12, 31))
    assert yearly == [date(2026, 2, 28)]
    assert (
        occurrences(date(2026, 5, 1), Cadence.DAILY, 1, date(2026, 3, 1), date(2026, 3, 31)) == []
    )
    daily = occurrences(date(2000, 1, 1), Cadence.DAILY, 1, date(2026, 1, 1), date(2027, 12, 31))
    assert len(daily) == MAX_OCCURRENCES
    assert daily[0] == date(2026, 1, 1)


def test_linear_projection_and_rollover() -> None:
    assert linear_projection(D("150"), 10, 21) == D("315.000000")
    assert linear_projection(D("12.345678"), 10, 21) == D("25.925924")  # 25.9259238
    assert linear_projection(D("1"), 3, 1) == D("0.333333")
    assert linear_projection(D("100"), 31, 0) == D(0)
    assert linear_projection(D("100"), 0, 5) == D(0)
    assert rollover_carry(D("300"), [D("0"), D("60")]) == D("540")
    assert rollover_carry(D("300"), [D("0"), D("60"), D("1188.05")]) == D("-348.05")
    assert rollover_carry(D("300"), []) == D(0)
    assert midpoint(D("-1100"), D("-1000")) == D("-1050.000000")
    assert midpoint(D("0.000001"), D("0.000002")) == D("0.000002")  # 0.0000015 half-even


# ---- goals ---------------------------------------------------------------------------


def test_goal_progress_and_projection() -> None:
    daily = [
        (date(2026, 1, 15), D("500")),
        (date(2026, 3, 1), D("250.50")),
        (date(2026, 6, 1), D("300")),
        (date(2026, 6, 15), D("200")),
    ]
    result = compute(D("5000"), daily, as_of=date(2026, 6, 30), target_date=date(2028, 1, 1))
    assert (result.contributed, result.remaining, result.percent) == (
        D("1250.50"),
        D("3749.50"),
        "25.01",
    )
    assert result.monthly_rate == D("227.916729")  # 1250.50 / 167 days * 30.4375
    assert result.projected_completion == date(2027, 11, 13)  # ceil(3749.50 / rate) = 501 days
    assert result.on_track is True
    late = compute(D("5000"), daily, as_of=date(2026, 6, 30), target_date=date(2027, 11, 12))
    assert late.on_track is False


def test_goal_rate_window_rules() -> None:
    # A single recent deposit is spread over the 30-day minimum window.
    one = compute(
        D("100"), [(date(2026, 6, 29), D("10"))], as_of=date(2026, 6, 30), target_date=None
    )
    assert one.monthly_rate == D("10.145833")
    assert one.projected_completion == date(2027, 3, 27)  # 90 / (1/3 per day) = 270 days
    # Only the last 180 days count.
    old = compute(
        D("1000"),
        [(date(2025, 1, 1), D("900")), (date(2026, 6, 1), D("18"))],
        as_of=date(2026, 6, 30),
        target_date=None,
    )
    assert old.monthly_rate == D("3.04375")  # 18 / 180 * 30.4375
    assert old.projected_completion == date(2028, 9, 27)  # 82 left at 0.1 a day: 820 days
    # Withdrawals that leave no positive rate give no projection.
    none = compute(
        D("100"),
        [(date(2026, 6, 1), D("50")), (date(2026, 6, 2), D("-50"))],
        as_of=date(2026, 6, 30),
        target_date=date(2027, 1, 1),
    )
    assert (none.projected_completion, none.on_track, none.percent) == (None, None, "0.00")
    empty = compute(D("100"), [], as_of=date(2026, 6, 30), target_date=None)
    assert (empty.monthly_rate, empty.projected_completion) == (None, None)
    reached = compute(
        D("100"),
        [(date(2026, 6, 10), D("60")), (date(2026, 6, 20), D("40")), (date(2026, 6, 25), D("5"))],
        as_of=date(2026, 6, 30),
        target_date=date(2026, 6, 15),
    )
    assert (reached.remaining, reached.percent) == (D(0), "105.00")
    assert reached.projected_completion == date(2026, 6, 20)
    assert reached.on_track is False  # reached after the target date


def test_percent() -> None:
    assert percent(D("1182.716666"), D("60")) == "1971.19"
    assert percent(D("-30"), D("60")) == "-50.00"
    assert percent(D("10"), D("-40")) == "25.00"  # relative to the size of the base
    assert percent(D("1"), D("0")) is None
    assert percent(D("0.125"), D("1")) == "12.50"


def test_category_roll_up() -> None:
    top, mid, leaf, other = (uuid.uuid4() for _ in range(4))
    tree = CategoryTree(
        [
            CategoryNode(top, None, "Food", CategoryKind.EXPENSE),
            CategoryNode(mid, top, "Groceries", CategoryKind.EXPENSE),
            CategoryNode(leaf, mid, "Organic", CategoryKind.EXPENSE),
            CategoryNode(other, None, "Rent", CategoryKind.EXPENSE),
        ]
    )
    assert [tree.at_depth(leaf, d) for d in (1, 2, 3)] == [top, mid, leaf]
    assert tree.at_depth(other, 3) == other
    assert tree.at_depth(None, 1) is None
    assert tree.nearest(leaf, {mid: 1, top: 1}) == mid
    assert tree.nearest(leaf, {top: 1}) == top
    assert tree.nearest(other, {top: 1}) is None
    unknown = uuid.uuid4()
    assert tree.at_depth(unknown, 1) == unknown
