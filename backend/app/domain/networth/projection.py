# SPDX-License-Identifier: AGPL-3.0-or-later
"""Net worth projections (``GET /networth/projection``), pure arithmetic.

Two simple, explainable models; neither is advice and neither compounds
returns:

* **Linear trend**: an ordinary least-squares line through the net worth
  history of the basis window (month ends, plus today), ``x`` in days. The
  projection starts from today's net worth and adds ``slope_per_day *
  days``. The reported ``slope_per_month`` is ``slope_per_day * 30.436875``
  (an average Gregorian month). Fewer than two points: no trend.
* **Savings rate**: the average monthly net cash flow (money in minus money
  out, transfers excluded, as ``GET /reports/cashflow``) over the basis
  window's whole months, added once per month to today's net worth. The
  savings rate is ``net / money in`` over the window (``null`` without
  income). Market moves, loan amortisation and revaluations are not in it.

Arithmetic is ``Decimal`` in the 34-digit context; money is rounded once to
storage precision (6 decimals, half-even), rates to 4 decimals.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final

from app.core.money import calc_context, to_storage

#: Days in an average Gregorian month (365.2425 / 12).
DAYS_PER_MONTH: Final = Decimal("30.436875")
RATE_QUANTUM: Final = Decimal("0.0001")


def slope_per_day(points: Sequence[tuple[date, Decimal]]) -> Decimal | None:
    """Least-squares slope of ``points`` (value per day); ``None`` with fewer than 2 days."""
    if len({d for d, _ in points}) < 2:
        return None
    origin = min(d for d, _ in points)
    with localcontext(calc_context()):
        xs = [Decimal((d - origin).days) for d, _ in points]
        ys = [v for _, v in points]
        n = Decimal(len(points))
        mean_x = sum(xs, Decimal(0)) / n
        mean_y = sum(ys, Decimal(0)) / n
        sxx = sum(((x - mean_x) ** 2 for x in xs), Decimal(0))
        sxy = sum(((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys, strict=True)), Decimal(0))
        return sxy / sxx


def per_month(slope: Decimal) -> Decimal:
    with localcontext(calc_context()):
        return to_storage(slope * DAYS_PER_MONTH)


def along(start: Decimal, slope: Decimal, today: date, on: date) -> Decimal:
    """``start + slope * days(today -> on)``."""
    with localcontext(calc_context()):
        return to_storage(start + slope * Decimal((on - today).days))


def average(total: Decimal, count: int) -> Decimal:
    if count <= 0:
        return Decimal(0)
    with localcontext(calc_context()):
        return to_storage(total / Decimal(count))


def savings_rate(net: Decimal, inflow: Decimal) -> Decimal | None:
    """``net / inflow`` to 4 decimals; ``None`` without inflow."""
    if inflow <= 0:
        return None
    with localcontext(calc_context()):
        return (net / inflow).quantize(RATE_QUANTUM, rounding=ROUND_HALF_EVEN)


def accumulate(start: Decimal, monthly: Decimal, months: int) -> Decimal:
    """``start + monthly * months`` (linear; savings are not invested)."""
    with localcontext(calc_context()):
        return to_storage(start + monthly * Decimal(months))
