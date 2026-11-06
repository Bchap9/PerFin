# SPDX-License-Identifier: AGPL-3.0-or-later
"""Return arithmetic (``app.domain.investments.returns``): the daily TWR chain and XIRR."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from app.domain.investments.returns import (
    Day,
    annualize,
    deannualize,
    period_return,
    round_rate,
    twr,
    xirr,
    xirr_growth,
)


def test_twr_ignores_the_timing_of_flows() -> None:
    # +10 %, flat, then -10 %, with a large deposit in between: TWR is 1.1 * 0.9 - 1.
    days = [Day(Decimal(110)), Day(Decimal(1110), inflow=Decimal(1000)), Day(Decimal(999))]
    assert round_rate(twr(Decimal(100), days)) == Decimal("-0.0100000000")


def test_twr_first_purchase_and_full_sale() -> None:
    days = [
        Day(Decimal(102), inflow=Decimal(100)),  # bought at 100, closes at 102
        Day(Decimal(0), outflow=Decimal(105)),  # sold everything for 105
        Day(Decimal(0)),  # nothing invested: no return
    ]
    assert round_rate(twr(Decimal(0), days)) == Decimal("0.0500000000")


def test_xirr_known_values() -> None:
    # 1000 paid in, 1100 back a year (365 days) later: 10 %.
    assert round_rate(xirr([(0, Decimal(-1000)), (365, Decimal(1100))]) or Decimal(0)) == Decimal(
        "0.1000000000"
    )
    # Two deposits; the rate satisfies NPV = 0 to the precision kept.
    flows = [(0, Decimal(-1000)), (182, Decimal(-500)), (365, Decimal(1600))]
    rate = xirr(flows)
    assert rate is not None
    npv = sum(
        (a / (1 + rate) ** (Decimal(d) / 365) for d, a in flows),
        Decimal(0),
    )
    assert abs(npv) < Decimal("1e-15")
    assert Decimal("0.08") < rate < Decimal("0.09")


def test_xirr_needs_both_signs_and_handles_large_losses() -> None:
    assert xirr([]) is None
    assert xirr([(0, Decimal(-5)), (10, Decimal(-1))]) is None
    loss = xirr([(0, Decimal(-1000)), (365, Decimal(1))])
    assert loss is not None
    assert round_rate(loss) == Decimal("-0.9990000000")
    gain = xirr([(0, Decimal(-1)), (30, Decimal(1000))])  # Newton overshoots: bisection
    assert gain is not None
    assert abs((1 + gain) ** (Decimal(30) / 365) - 1000) < Decimal("1e-12")


def test_annualize_only_full_years() -> None:
    assert annualize(Decimal("0.1"), 200) is None
    assert annualize(Decimal("0.21"), 730) == pytest.approx(Decimal("0.1"))
    assert round_rate(deannualize(Decimal("0.1"), 365)) == Decimal("0.1000000000")


_values = st.integers(min_value=1, max_value=10**9).map(lambda n: Decimal(n) / 100)


@settings(max_examples=200, deadline=None)
@given(_values, st.lists(_values, min_size=1, max_size=60))
# end/start - 1 is exactly halfway between two rounded rates here; the chained
# daily returns land a hair below it, so the unrounded values are compared.
@example(Decimal("20.48"), [Decimal("30524.04"), Decimal("0.01")])
def test_property_twr_equals_mwr_without_flows(start: Decimal, values: list[Decimal]) -> None:
    """With no flows inside the period, TWR and the period MWR are both end/start - 1."""
    days = [Day(v) for v in values]
    time_weighted = twr(start, days)
    growth = xirr_growth([(0, -start), (len(days), values[-1])])
    assert growth is not None
    money_weighted = period_return(growth, len(days))
    expected = values[-1] / start - 1
    assert abs(time_weighted - expected) <= Decimal("1e-20") * max(Decimal(1), abs(expected))
    assert abs(round_rate(time_weighted) - round_rate(expected)) <= Decimal("1e-10")
    assert abs(money_weighted - time_weighted) <= Decimal("1e-12") * max(
        Decimal(1), abs(time_weighted)
    )
