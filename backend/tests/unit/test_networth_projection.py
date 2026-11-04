# SPDX-License-Identifier: AGPL-3.0-or-later
"""Net worth projection arithmetic and history sampling (pure)."""

from __future__ import annotations

import itertools
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.domain.networth import projection
from app.domain.networth.schemas import Granularity
from app.domain.networth.service import auto_granularity, bucket_ends

D = Decimal
TODAY = date(2026, 6, 30)


def test_slope_of_an_exact_line() -> None:
    points = [(TODAY - timedelta(days=d), D(1000) - D(10) * d) for d in (0, 30, 61, 91)]
    slope = projection.slope_per_day(points)
    assert slope == D(10)
    assert projection.per_month(D(10)) == D("304.368750")
    assert projection.along(D(1000), D(10), TODAY, TODAY + timedelta(days=31)) == D("1310")


def test_slope_needs_two_days() -> None:
    assert projection.slope_per_day([]) is None
    assert projection.slope_per_day([(TODAY, D(5)), (TODAY, D(7))]) is None


def test_slope_of_noisy_points_is_least_squares() -> None:
    # x = 0, 1, 2; y = 0, 2, 1 -> slope 0.5
    points = [(TODAY, D(0)), (TODAY + timedelta(days=1), D(2)), (TODAY + timedelta(days=2), D(1))]
    assert projection.slope_per_day(points) == D("0.5")


def test_savings_helpers() -> None:
    assert projection.average(D("3000"), 3) == D("1000")
    assert projection.average(D("1"), 0) == D(0)
    assert projection.average(D("10"), 3) == D("3.333333")
    assert projection.savings_rate(D("250"), D("1000")) == D("0.2500")
    assert projection.savings_rate(D("-50"), D("0")) is None
    assert projection.accumulate(D("100"), D("25.5"), 4) == D("202")


def test_bucket_ends() -> None:
    start = date(2026, 4, 15)
    days = bucket_ends(start, TODAY, Granularity.DAY)
    assert days[0] == start
    assert days[-1] == TODAY
    assert len(days) == 77
    weeks = bucket_ends(start, TODAY, Granularity.WEEK)
    assert weeks[-1] == TODAY
    assert weeks[0] >= start
    assert all((b - a).days == 7 for a, b in itertools.pairwise(weeks))
    assert bucket_ends(start, TODAY, Granularity.MONTH) == [
        date(2026, 4, 30),
        date(2026, 5, 31),
        TODAY,
    ]
    assert bucket_ends(TODAY, start, Granularity.DAY) == []


@pytest.mark.parametrize(
    ("days", "expected"),
    [
        (30, Granularity.DAY),
        (400, Granularity.DAY),
        (401, Granularity.WEEK),
        (4000, Granularity.MONTH),
    ],
)
def test_auto_granularity(days: int, expected: Granularity) -> None:
    assert auto_granularity(TODAY - timedelta(days=days - 1), TODAY) is expected
