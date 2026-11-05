# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input and output schemas for budgets and their report (API.md ``/budgets``).

Inputs forbid unknown fields (SECURITY.md F4). Money is ``{"amount":
"12.34", "currency": "USD"}`` with the amount as a decimal string
(``app.core.money``); a line's money must be in the budget's currency.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.money import CurrencyCode, Money, MoneyIn
from app.domain.accounts.schemas import Name
from app.domain.budgets.models import BudgetPeriod
from app.domain.transactions.models import CategoryKind

MAX_PAGE: Final = 500
DEFAULT_PAGE: Final = 100
#: Most lines of one budget (one per category).
MAX_LINES: Final = 500


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


class BudgetLineIn(_Input):
    """The amount per period for one category (``>= 0``, the budget's currency)."""

    category_id: uuid.UUID
    amount: MoneyIn


Lines = Annotated[list[BudgetLineIn], Field(max_length=MAX_LINES)]


def _unique_categories(lines: list[BudgetLineIn] | None) -> None:
    if lines is None:
        return
    ids = [line.category_id for line in lines]
    if len(ids) != len(set(ids)):
        raise ValueError("each category may have only one line")


class BudgetCreate(_Input):
    """``POST /budgets``.

    ``period`` monthly or annual: ``start_date`` is the first day of a month
    and there is no ``end_date``; periods are calendar months, or twelve
    months from ``start_date``. ``custom``: one period ``start_date ..
    end_date`` (at most 3660 days). ``currency`` defaults to the household's
    base currency.
    """

    name: Name
    period: BudgetPeriod
    start_date: date
    end_date: date | None = None
    currency: CurrencyCode | None = None
    rollover: bool = False
    lines: Lines = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        _unique_categories(self.lines)
        return self


class BudgetUpdate(_Input):
    """``PATCH /budgets/{id}``: absent fields stay; ``lines`` replaces the whole set.

    Changing ``currency`` needs ``lines`` in the same request (their amounts
    are in the new currency).
    """

    name: Name | None = None
    period: BudgetPeriod | None = None
    start_date: date | None = None
    end_date: date | None = None
    currency: CurrencyCode | None = None
    rollover: bool | None = None
    lines: Lines | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _unique_categories(self.lines)
        for name in ("name", "period", "start_date", "currency", "rollover", "lines"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class BudgetLineOut(_Output):
    id: uuid.UUID
    category_id: uuid.UUID
    amount: Money


class BudgetOut(_Output):
    id: uuid.UUID
    name: str
    period: BudgetPeriod
    start_date: date
    end_date: date | None
    currency: str
    rollover: bool
    lines: list[BudgetLineOut]
    created_at: datetime
    updated_at: datetime


class BudgetPage(_Output):
    items: list[BudgetOut]
    next_cursor: uuid.UUID | None


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class PeriodOut(_Output):
    start: date
    end: date
    #: ``past``, ``current`` or ``future`` relative to ``as_of``.
    state: str


class BudgetReportLine(_Output):
    """One budget line in one period.

    ``budgeted`` is the line amount; ``carried_over`` what rollover brings
    from earlier periods (expense lines of a ``rollover`` budget only);
    ``available = budgeted + carried_over``; ``actual`` the money spent
    (expense) or received (income) in the period; ``remaining = available -
    actual``; ``forecast`` the expected actual at the end of the period.
    """

    category_id: uuid.UUID
    name: str | None
    kind: CategoryKind | None
    budgeted: Money
    carried_over: Money
    available: Money
    actual: Money
    remaining: Money
    forecast: Money


class UnbudgetedOut(_Output):
    """Spending in a category without a line (``null``: uncategorised)."""

    category_id: uuid.UUID | None
    name: str | None
    actual: Money


class BudgetTotals(_Output):
    budgeted: Money
    carried_over: Money
    available: Money
    actual: Money
    remaining: Money
    forecast: Money


class BudgetReportOut(_Output):
    budget_id: uuid.UUID
    name: str
    currency: str
    as_of: date
    period: PeriodOut
    include_pending: bool
    lines: list[BudgetReportLine]
    unbudgeted: list[UnbudgetedOut]
    unbudgeted_total: Money
    expense_totals: BudgetTotals
    income_totals: BudgetTotals
    unconverted: list[Money]
