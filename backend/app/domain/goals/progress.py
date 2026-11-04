# SPDX-License-Identifier: AGPL-3.0-or-later
"""Goal progress and projected completion (pure functions, no I/O).

* ``contributed``: the sum of the goal's contributions (all in the goal's
  currency; withdrawals are negative contributions).
* ``remaining = max(0, target - contributed)``; ``percent = contributed /
  target * 100`` with two decimals, half-even (it may pass 100).
* **Observed rate**: the contributions of the last :data:`WINDOW_DAYS` days
  up to ``as_of``, but not before the first contribution, over a window of
  at least :data:`MIN_WINDOW_DAYS` days (one early deposit is not a daily
  habit): ``daily = sum / window_days``. ``monthly_rate = daily * 365.25 /
  12`` rounded half-even to 6 decimals.
* **Projected completion**: when reached, the date the running total first
  reached the target; otherwise ``as_of + ceil(remaining / daily)`` days
  when the rate is positive and the date is within :data:`HORIZON_YEARS`
  years; otherwise none.
* ``on_track``: with a ``target_date``, whether the projected completion is
  on or before it (``None`` without a target date or a projection).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_CEILING, Decimal, localcontext
from typing import Final

from app.core.money import calc_context, quantize, sum_amounts, to_storage

WINDOW_DAYS: Final = 180
MIN_WINDOW_DAYS: Final = 30
HORIZON_YEARS: Final = 100
#: Average days per month (365.25 / 12).
DAYS_PER_MONTH: Final = Decimal("30.4375")
ZERO: Final = Decimal(0)


@dataclass(frozen=True)
class Progress:
    contributed: Decimal
    remaining: Decimal
    percent: str
    monthly_rate: Decimal | None
    projected_completion: date | None
    on_track: bool | None


def _reached_on(target: Decimal, daily: Sequence[tuple[date, Decimal]]) -> date | None:
    running = ZERO
    for day, amount in sorted(daily):
        running = sum_amounts((running, amount))
        if running >= target:
            return day
    return None


def compute(
    target: Decimal,
    daily: Sequence[tuple[date, Decimal]],
    *,
    as_of: date,
    target_date: date | None,
) -> Progress:
    """Progress of a goal with ``target`` from its per-day contribution sums."""
    contributed = sum_amounts(amount for _, amount in daily)
    remaining = max(ZERO, target - contributed)
    with localcontext(calc_context()):
        share = contributed * 100 / target
    percent = f"{quantize(share, 2):f}"

    monthly: Decimal | None = None
    rate = ZERO
    if daily:
        first = min(day for day, _ in daily)
        window_start = max(first, as_of - timedelta(days=WINDOW_DAYS - 1))
        window_days = max(MIN_WINDOW_DAYS, (as_of - window_start).days + 1)
        since = as_of - timedelta(days=window_days - 1)
        in_window = sum_amounts(amount for day, amount in daily if since <= day <= as_of)
        with localcontext(calc_context()):
            rate = in_window / window_days
            monthly = to_storage(rate * DAYS_PER_MONTH)

    projected: date | None = None
    if remaining == 0:
        projected = _reached_on(target, daily)
    elif rate > 0:
        with localcontext(calc_context()):
            days = (remaining / rate).to_integral_value(rounding=ROUND_CEILING)
        if days <= HORIZON_YEARS * 366:
            projected = as_of + timedelta(days=int(days))

    on_track: bool | None = None
    if target_date is not None and projected is not None:
        on_track = projected <= target_date
    return Progress(
        contributed=contributed,
        remaining=remaining,
        percent=percent,
        monthly_rate=monthly,
        projected_completion=projected,
        on_track=on_track,
    )
