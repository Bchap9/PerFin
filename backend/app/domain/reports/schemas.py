# SPDX-License-Identifier: AGPL-3.0-or-later
"""Response schemas of ``/reports`` (API.md "Reports notes").

Money is ``{"amount": "12.34", "currency": "USD"}`` (``app.core.money``);
percentages are decimal strings with two decimals (``"12.50"``) or ``null``
when the comparison base is zero. Reports have no request bodies: every
parameter is an explicit query parameter.
"""

from __future__ import annotations

import enum
from datetime import date
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.core.money import Money

#: Deepest category level (DATA_MODEL.md: at most three levels).
MAX_DEPTH: Final = 3
#: Longest trend series in months (and the largest ``months`` value).
MAX_TREND_MONTHS: Final = 60
DEFAULT_TREND_MONTHS: Final = 12
#: Default cash-flow window: this month and the 11 before it.
DEFAULT_CASHFLOW_MONTHS: Final = 12
#: Most values of a repeatable filter (as ``GET /transactions``).
MAX_FILTER_VALUES: Final = 50


class GroupBy(enum.StrEnum):
    CATEGORY = "category"
    PAYEE = "payee"
    MONTH = "month"
    ACCOUNT = "account"


class Flow(enum.StrEnum):
    SPENDING = "spending"
    INCOME = "income"


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


class ReportItem(_Output):
    """One group: ``key`` is a category, payee or account id, or ``YYYY-MM``.

    ``key`` is ``null`` for uncategorised lines (``group_by=category``) and
    lines without a payee (``group_by=payee``); ``name`` is the category,
    payee or account name (``null`` for a month or a missing one).
    """

    key: str | None
    name: str | None
    amount: Money


class FlowReport(_Output):
    """``GET /reports/spending`` and ``GET /reports/income``.

    Spending amounts are positive money out (refunds in an expense category
    reduce them); income amounts are positive money in.
    """

    flow: Flow
    currency: str
    date_from: date
    date_to: date
    group_by: GroupBy
    depth: int
    include_pending: bool
    items: list[ReportItem]
    total: Money
    unconverted: list[Money]


class TrendPoint(_Output):
    month: str
    amount: Money


class TrendRow(_Output):
    """One category (or the total): this month against last month and a year ago."""

    key: str | None
    name: str | None
    amount: Money
    previous: Money
    mom_change: Money
    mom_percent: str | None
    year_ago: Money
    yoy_change: Money
    yoy_percent: str | None


class TrendReport(_Output):
    flow: Flow
    currency: str
    month: str
    months: int
    depth: int
    include_pending: bool
    series: list[TrendPoint]
    total: TrendRow
    categories: list[TrendRow]
    unconverted: list[Money]


class CashflowMonth(_Output):
    month: str
    inflow: Money
    outflow: Money
    net: Money


class CashflowReport(_Output):
    """``GET /reports/cashflow``: money in, money out (positive) and net per month."""

    currency: str
    date_from: date
    date_to: date
    include_pending: bool
    months: list[CashflowMonth]
    inflow: Money
    outflow: Money
    net: Money
    unconverted: list[Money]
