# SPDX-License-Identifier: AGPL-3.0-or-later
"""Response schemas of ``/networth`` (API.md "Net worth notes").

Money is ``{"amount": "12.34", "currency": "USD"}`` (``app.core.money``).
``scope`` says what the figures cover: ``household`` (every account; the
caller sees them all) or ``visible_accounts`` (recomputed over the accounts
a member or viewer may see).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict

from app.core.money import Money

#: Most points in one history response.
MAX_POINTS: Final = 1000
#: Longest projection in months, and the longest basis window.
MAX_PROJECTION_MONTHS: Final = 360
MAX_BASIS_MONTHS: Final = 120
DEFAULT_PROJECTION_MONTHS: Final = 12
DEFAULT_BASIS_MONTHS: Final = 12

Scope = Literal["household", "visible_accounts"]
PointSource = Literal["snapshot", "computed"]


class Granularity(enum.StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


class NetWorthPointOut(_Output):
    as_of: date
    assets: Money
    liabilities: Money
    net: Money
    source: PointSource


class NetWorthHistory(_Output):
    """``GET /networth?range=``: one point per bucket end, oldest first."""

    currency: str
    range: str
    granularity: Granularity
    scope: Scope
    date_from: date
    date_to: date
    points: list[NetWorthPointOut]
    change: Money | None


class AccountValueOut(_Output):
    account_id: uuid.UUID
    name: str
    type: str
    account_class: str
    side: Literal["asset", "liability"]
    amount: Money
    amount_base: Money | None


class ClassTotals(_Output):
    assets: dict[str, Money]
    liabilities: dict[str, Money]


class NetWorthLatest(_Output):
    """``GET /networth/latest``: today's figures with their breakdown."""

    as_of: date
    currency: str
    scope: Scope
    assets: Money
    liabilities: Money
    net: Money
    by_class: ClassTotals
    by_type: dict[str, Money]
    accounts: list[AccountValueOut]
    unconverted: list[uuid.UUID]
    unpriced_security_ids: list[uuid.UUID]
    last_snapshot_date: date | None


class ProjectionPoint(_Output):
    on: date
    net: Money


class TrendModel(_Output):
    slope_per_month: Money
    points: list[ProjectionPoint]


class SavingsModel(_Output):
    average_monthly_savings: Money
    savings_rate: str | None
    points: list[ProjectionPoint]


class NetWorthProjection(_Output):
    """``GET /networth/projection``: linear trend and savings-rate projections."""

    as_of: date
    currency: str
    scope: Scope
    months: int
    basis_months: int
    current: Money
    trend: TrendModel | None
    savings: SavingsModel
    unconverted: list[Money]
