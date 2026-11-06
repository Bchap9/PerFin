# SPDX-License-Identifier: AGPL-3.0-or-later
"""Return arithmetic: the time-weighted chain and XIRR (pure, ``Decimal`` only).

**Time-weighted return** (:func:`twr`): a daily valuation chain. For each
day ``d`` with the portfolio value ``V[d]`` at the end of the day, external
inflows ``In[d]`` (money put into the securities: purchases, fees paid,
transfers in at market value) and outflows ``Out[d]`` (money taken out:
sale proceeds, dividends and interest paid out, transfers out at market
value), both non-negative::

    r[d] = (V[d] + Out[d]) / (V[d-1] + In[d]) - 1

Inflows count from the start of the day (a first purchase earns that day's
move), outflows at its end (a full sale ends the day at zero without a
division by zero). A day with nothing invested (denominator 0) has return 0.
The period return is ``prod(1 + r[d]) - 1``, evaluated telescoped
(:func:`cumulative_returns`): a run of days without flows between them
collapses to one quotient, ``V[last] / V[first-1]``.

**Money-weighted return** (:func:`xirr`): the annual rate ``x`` with
``sum(c[i] / (1 + x) ** (days[i] / 365)) = 0`` for the investor's cash flows
(paid in negative, received positive; the starting value is paid in on the
first day, the ending value received on the last). It is solved for
``g = ln(1 + x)`` (:func:`xirr_growth`; so the huge annual rates of short
periods and near-total losses neither overflow nor round to -100 %) by
Newton's method from 10 %, falling back to bisection over a widening
bracket when Newton jumps too far, stalls or does not converge; ``None``
when the flows do not change sign or no bracket is found.
:func:`period_return` turns ``g`` into the return over the period.

Arithmetic runs in a 34-digit half-even context (``calc_context``); results
are rounded **once**, half-even, to :data:`RATE_PLACES` decimals
(:func:`round_rate`). Rates are fractions (``0.0525`` = 5.25 %).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final

from app.core.money import calc_context

#: Decimals of every rate in API output.
RATE_PLACES: Final = 10
RATE_QUANTUM: Final = Decimal(1).scaleb(-RATE_PLACES)
DAYS_PER_YEAR: Final = Decimal(365)
#: Solver tolerances: |npv| relative to the largest flow, and the rate step.
NPV_TOLERANCE: Final = Decimal("1e-24")
STEP_TOLERANCE: Final = Decimal("1e-28")
NEWTON_ITERATIONS: Final = 60
BISECTION_ITERATIONS: Final = 400
#: Largest ``|ln(1 + rate)|`` tried (a doubling over one day is ``2 ** 365``,
#: about ``e ** 253``) and the largest Newton step taken before bisecting.
LOG_GROWTH_CEILING: Final = Decimal(100_000)
NEWTON_MAX_STEP: Final = Decimal(5)


def round_rate(value: Decimal) -> Decimal:
    result = value.quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN, context=calc_context())
    return result + 0 if result.is_zero() else result


@dataclass(frozen=True)
class Day:
    """One day of the valuation chain (all in one currency)."""

    value: Decimal
    inflow: Decimal = Decimal(0)
    outflow: Decimal = Decimal(0)


def cumulative_returns(start_value: Decimal, days: Sequence[Day]) -> list[Decimal]:
    """``prod(1 + r[0..d]) - 1`` after every day ``d`` (module doc), unrounded.

    Each day's factor ``1 + r[d]`` is ``N[d] / D[d]`` with ``N[d] = V[d] + Out[d]``
    and ``D[d] = V[d-1] + In[d]``. When ``N[d] == D[d+1]`` (no flow between the
    two days) the factors telescope to ``N[d+1] / D[d]``, so a run of days
    without flows costs a single division instead of one rounded product per
    day. Multiplying the rounded daily returns instead can push an exact
    half-way result across the :data:`RATE_PLACES` tie: ``20.48 -> 30524.04 ->
    0.01`` is exactly ``-0.99951171875`` but chains to ``-0.99951171874999...``.
    """
    cumulative: list[Decimal] = []
    settled = Decimal(1)  # product of the closed runs
    run: tuple[Decimal, Decimal] | None = None  # (numerator, denominator) of the open run
    previous = start_value
    with localcontext(calc_context()):
        for day in days:
            invested = previous + day.inflow
            ending = day.value + day.outflow
            if invested > 0 and run is not None and run[0] == invested:
                run = (ending, run[1])
            else:
                if run is not None:
                    settled *= run[0] / run[1]
                # a day with nothing invested has return 0: it closes the run
                run = (ending, invested) if invested > 0 else None
            growth = settled * run[0] / run[1] if run is not None else settled
            cumulative.append(growth - 1)
            previous = day.value
    return cumulative


def twr(start_value: Decimal, days: Sequence[Day]) -> Decimal:
    """Time-weighted return of the period, unrounded (module doc)."""
    cumulative = cumulative_returns(start_value, days)
    return cumulative[-1] if cumulative else Decimal(0)


def annualize(period_return: Decimal, days: int) -> Decimal | None:
    """``(1 + r) ** (365 / days) - 1``; ``None`` for periods under a year (not meaningful)."""
    if days < 365 or period_return <= -1:
        return None
    with localcontext(calc_context()):
        return (1 + period_return) ** (DAYS_PER_YEAR / Decimal(days)) - 1


def deannualize(rate: Decimal, days: int) -> Decimal:
    """The return over ``days`` at the annual ``rate``: ``(1 + rate) ** (days / 365) - 1``."""
    with localcontext(calc_context()):
        return (1 + rate) ** (Decimal(days) / DAYS_PER_YEAR) - 1


def _npv(growth: Decimal, flows: Sequence[tuple[int, Decimal]]) -> tuple[Decimal, Decimal]:
    """NPV and its derivative at the log growth rate ``growth = ln(1 + x)``.

    Working on ``ln(1 + x)`` keeps huge annual rates of short periods (a
    doubling in one day is ``2 ** 365``) and near-total losses exact.
    """
    npv = Decimal(0)
    slope = Decimal(0)
    for day, amount in flows:
        t = Decimal(day) / DAYS_PER_YEAR
        value = amount * (-growth * t).exp()
        npv += value
        slope -= t * value
    return npv, slope


def xirr_growth(flows: Sequence[tuple[int, Decimal]]) -> Decimal | None:
    """``ln(1 + x)`` for the XIRR ``x`` of ``(day offset, amount)`` flows (module doc)."""
    flows = [(day, amount) for day, amount in flows if amount != 0]
    if not any(a > 0 for _, a in flows) or not any(a < 0 for _, a in flows):
        return None
    with localcontext(calc_context()):
        tolerance = max(abs(a) for _, a in flows) * NPV_TOLERANCE
        growth = Decimal("1.1").ln()  # start at 10 %
        for _ in range(NEWTON_ITERATIONS):
            npv, slope = _npv(growth, flows)
            if abs(npv) <= tolerance:
                return growth
            if slope == 0:
                break
            step = npv / slope
            if abs(step) > NEWTON_MAX_STEP:
                break
            growth -= step
            if abs(growth) > LOG_GROWTH_CEILING:
                break
            if abs(step) <= STEP_TOLERANCE:
                return growth
        return _bisect(flows, tolerance)


def xirr(flows: Sequence[tuple[int, Decimal]]) -> Decimal | None:
    """The annual rate solving ``NPV = 0``, unrounded; ``None`` when there is none."""
    growth = xirr_growth(flows)
    if growth is None:
        return None
    with localcontext(calc_context()):
        return growth.exp() - 1


def period_return(growth: Decimal, days: int) -> Decimal:
    """The return over ``days`` at the log growth rate ``growth``: ``e ** (g * days / 365) - 1``."""
    with localcontext(calc_context()):
        return (growth * Decimal(days) / DAYS_PER_YEAR).exp() - 1


def _bisect(flows: Sequence[tuple[int, Decimal]], tolerance: Decimal) -> Decimal | None:
    """Bisection on ``ln(1 + x)``, widening ``[-1, 1]`` until the NPV changes sign."""
    low, high = Decimal(-1), Decimal(1)
    f_low, _ = _npv(low, flows)
    f_high, _ = _npv(high, flows)
    while (f_low < 0) == (f_high < 0) and f_low != 0 and f_high != 0:
        if high >= LOG_GROWTH_CEILING:
            return None
        low, high = low * 2 - 1, high * 2 + 1
        f_low, _ = _npv(low, flows)
        f_high, _ = _npv(high, flows)
    if f_low == 0:
        return low
    for _ in range(BISECTION_ITERATIONS):
        mid = (low + high) / 2
        f_mid, _ = _npv(mid, flows)
        if abs(f_mid) <= tolerance or (high - low) <= STEP_TOLERANCE:
            return mid
        if (f_mid < 0) == (f_low < 0):
            low, f_low = mid, f_mid
        else:
            high = mid
    return (low + high) / 2
