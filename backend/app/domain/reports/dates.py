# SPDX-License-Identifier: AGPL-3.0-or-later
"""Calendar helpers for reports, budgets and goals (pure functions, no I/O).

* :func:`add_months` moves a date by whole months and clamps the day to the
  target month's length (``2026-01-31 + 1 month = 2026-02-28``).
* :func:`month_start` / :func:`month_end` and :func:`month_index` (months
  since year 0) let callers bucket dates by calendar month.
* :func:`parse_month` reads ``YYYY-MM``; :func:`parse_period_date` reads
  ``YYYY-MM`` (the first of that month) or ``YYYY-MM-DD``.
* :func:`check_range` enforces the bounded report window (SECURITY.md F8):
  ``date_from <= date_to`` (``422 invalid-range``) and at most
  :data:`MAX_REPORT_DAYS` days (``422 range-too-long``), the same limits and
  problem types as ``GET /accounts/{id}/balances``.
"""

from __future__ import annotations

import calendar
import re
from datetime import date, timedelta
from typing import Final

from app.core.errors import ProblemError

#: Longest range of a report (days, inclusive): ten years, like balance history.
MAX_REPORT_DAYS: Final = 3660
_MONTH_RE: Final = re.compile(r"^([0-9]{4})-([0-9]{2})$")
_DATE_RE: Final = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})$")


def add_months(value: date, months: int) -> date:
    """``value`` moved by ``months`` calendar months, the day clamped to the month."""
    index = value.year * 12 + (value.month - 1) + months
    year, month0 = divmod(index, 12)
    if not 1 <= year <= 9999:
        raise OverflowError("date out of range")
    last = calendar.monthrange(year, month0 + 1)[1]
    return date(year, month0 + 1, min(value.day, last))


def month_start(value: date) -> date:
    return value.replace(day=1)


def month_end(value: date) -> date:
    return value.replace(day=calendar.monthrange(value.year, value.month)[1])


def month_index(value: date) -> int:
    """Months since January of year 0 (``month_index(b) - month_index(a)`` = months apart)."""
    return value.year * 12 + value.month - 1


def month_key(value: date) -> str:
    """``YYYY-MM`` of ``value``."""
    return f"{value.year:04d}-{value.month:02d}"


def invalid_period(detail: str) -> ProblemError:
    return ProblemError(422, "invalid-period", "Invalid period", detail)


def parse_month(text: str) -> date:
    """The first day of the month ``YYYY-MM`` (``422 invalid-period`` otherwise)."""
    match = _MONTH_RE.fullmatch(text.strip())
    if match is None:
        raise invalid_period("Use a month as YYYY-MM.")
    year, month = int(match.group(1)), int(match.group(2))
    if not (1 <= year <= 9999 and 1 <= month <= 12):
        raise invalid_period("Use a month as YYYY-MM.")
    return date(year, month, 1)


def parse_period_date(text: str) -> date:
    """``YYYY-MM`` (its first day) or ``YYYY-MM-DD`` (``422 invalid-period`` otherwise)."""
    cleaned = text.strip()
    match = _DATE_RE.fullmatch(cleaned)
    if match is None:
        return parse_month(cleaned)
    try:
        return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    except ValueError as exc:
        raise invalid_period("Use a date as YYYY-MM-DD or a month as YYYY-MM.") from exc


def check_range(date_from: date, date_to: date, *, max_days: int = MAX_REPORT_DAYS) -> None:
    """``422 invalid-range`` / ``422 range-too-long`` for a bad report window."""
    if date_from > date_to:
        raise ProblemError(
            422, "invalid-range", "Invalid range", "date_from must not be after date_to."
        )
    if (date_to - date_from).days + 1 > max_days:
        raise ProblemError(
            422,
            "range-too-long",
            "Range too long",
            f"A report covers at most {max_days} days.",
            extra={"max_days": max_days},
        )


def days_inclusive(start: date, end: date) -> int:
    return (end - start).days + 1


def iter_months(first: date, last: date) -> list[date]:
    """First days of every month from ``first``'s month to ``last``'s month."""
    current = month_start(first)
    months: list[date] = []
    while current <= last:
        months.append(current)
        current = add_months(current, 1)
    return months


def clamp(value: date, low: date, high: date) -> date:
    return max(low, min(high, value))


ONE_DAY: Final = timedelta(days=1)
