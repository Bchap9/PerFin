# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounds of user-supplied business dates (SECURITY.md F8).

Investment trade and settle dates, prices, loan origination, payment and
rate-change dates, asset valuations, FX rates and recurring due dates are
never before :data:`MIN_BUSINESS_DATE`. Several reads walk day by day from
the oldest such date (investment performance, net worth history), so an
unbounded past would let one planted row make every later read slow. The
bound is enforced three times: in the input schemas (:data:`BusinessDate`,
422 at the API), in the importers and feeds that write these rows (out of
range rows are held or dropped), and by a database CHECK
(:func:`min_date_check`).
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Final

from pydantic import AfterValidator

#: The earliest business date accepted anywhere (inclusive).
MIN_BUSINESS_DATE: Final = date(1900, 1, 1)


def in_business_range(value: date) -> bool:
    """Whether ``value`` is on or after :data:`MIN_BUSINESS_DATE`."""
    return value >= MIN_BUSINESS_DATE


def _not_too_old(value: date) -> date:
    if not in_business_range(value):
        raise ValueError(f"date must be on or after {MIN_BUSINESS_DATE.isoformat()}")
    return value


#: A date input that may not lie before :data:`MIN_BUSINESS_DATE`.
BusinessDate = Annotated[date, AfterValidator(_not_too_old)]


def min_date_check(column: str) -> str:
    """The SQL CHECK expression bounding ``column`` (nullable columns pass on NULL)."""
    return f"{column} >= '{MIN_BUSINESS_DATE.isoformat()}'::date"
