# SPDX-License-Identifier: AGPL-3.0-or-later
"""Budgets: list, read, create, change, delete and report (API.md ``/budgets``).

Who may do what (routers check role and token scope first; every function
here checks the role again):

* **Read** (``GET /budgets``, ``/budgets/{id}``, ``/budgets/{id}/report``):
  any household role (token scope ``budgets:read``). Budgets and their lines
  are household-wide plans; the report's *actuals*, however, only come from
  the accounts the caller may see (``app.domain.accounts.visibility``), so
  a member or viewer sees the plan with their own share of the spending.
* **Create, change, delete**: member or above (``budgets:write``). A budget
  only holds amounts per category, never ledger data, so deleting one loses
  no financial records.

Rules (DATA_MODEL.md "Budgets and goals"; periods in
``app.domain.budgets.reports``):

* ``monthly`` and ``annual`` budgets start on the first day of a month and
  have no ``end_date``; a ``custom`` budget has ``end_date >= start_date``,
  at most 3660 days later (``422 invalid-budget-dates``).
* ``currency`` is an active ISO 4217 code, default the household's base
  currency; every line's money is in it (``422 currency-mismatch``). A
  currency change needs the lines in the same request (``422
  budget-lines-required``).
* One line per category, of an ``expense`` or ``income`` category of the
  household (``404 category-not-found``, ``422 budget-category-kind``); an
  amount is ``>= 0`` (``422 invalid-budget-amount``).
* A budget of another household or a missing one is ``404
  budget-not-found``.

Every write is audited (``budget.create|update|delete``, with names, dates,
currency and line amounts) and emits ``budget.created|updated|deleted``
with the budget id only.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import Money
from app.domain.budgets import repo
from app.domain.budgets.models import Budget, BudgetLine, BudgetPeriod
from app.domain.budgets.reports import build_report
from app.domain.budgets.schemas import (
    BudgetCreate,
    BudgetLineIn,
    BudgetLineOut,
    BudgetOut,
    BudgetPage,
    BudgetReportOut,
    BudgetUpdate,
)
from app.domain.fx.service import base_currency
from app.domain.reports.dates import MAX_REPORT_DAYS, days_inclusive, parse_period_date
from app.domain.reports.service import resolve_include_pending, today
from app.domain.transactions.models import CategoryKind

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: Kinds a budget line may be on.
LINE_KINDS: Final = frozenset({CategoryKind.EXPENSE, CategoryKind.INCOME})


# ---------------------------------------------------------------------------
# Problems and helpers
# ---------------------------------------------------------------------------


def budget_not_found() -> ProblemError:
    return ProblemError(404, "budget-not-found", "Budget not found")


def _category_not_found() -> ProblemError:
    return ProblemError(404, "category-not-found", "Category not found")


def _invalid_dates(detail: str) -> ProblemError:
    return ProblemError(422, "invalid-budget-dates", "Invalid budget dates", detail)


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def check_dates(period: BudgetPeriod, start_date: date, end_date: date | None) -> None:
    """``422 invalid-budget-dates`` unless the dates fit the period (module doc)."""
    if period is BudgetPeriod.CUSTOM:
        if end_date is None:
            raise _invalid_dates("A custom budget needs an end_date.")
        if end_date < start_date:
            raise _invalid_dates("end_date must not be before start_date.")
        if days_inclusive(start_date, end_date) > MAX_REPORT_DAYS:
            raise _invalid_dates(f"A custom budget covers at most {MAX_REPORT_DAYS} days.")
        return
    if end_date is not None:
        raise _invalid_dates("Only a custom budget has an end_date.")
    if start_date.day != 1:
        raise _invalid_dates("A monthly or annual budget starts on the first day of a month.")


def _check_lines(
    db: Session, household_id: uuid.UUID, currency: str, lines: Sequence[BudgetLineIn]
) -> dict[uuid.UUID, Decimal]:
    found = repo.categories(db, household_id, {line.category_id for line in lines})
    wanted: dict[uuid.UUID, Decimal] = {}
    for line in lines:
        category = found.get(line.category_id)
        if category is None:
            raise _category_not_found()
        if category.kind not in LINE_KINDS:
            raise ProblemError(
                422,
                "budget-category-kind",
                "Category cannot be budgeted",
                "Budget lines are for expense and income categories.",
            )
        if line.amount.currency != currency:
            raise ProblemError(
                422,
                "currency-mismatch",
                "Currency mismatch",
                f"Amounts of this budget are in {currency}.",
                extra={"currency": currency},
            )
        if line.amount.amount < 0:
            raise ProblemError(
                422,
                "invalid-budget-amount",
                "Invalid budget amount",
                "A budget line amount is zero or more.",
            )
        wanted[line.category_id] = line.amount.amount
    return wanted


def _state(budget: Budget, lines: Sequence[BudgetLine]) -> dict[str, Any]:
    return {
        "name": budget.name,
        "period": budget.period.value,
        "start_date": budget.start_date.isoformat(),
        "end_date": budget.end_date.isoformat() if budget.end_date else None,
        "currency": budget.currency,
        "rollover": budget.rollover,
        "lines": {str(line.category_id): format(line.amount, "f") for line in lines},
    }


def budget_out(budget: Budget, lines: Sequence[BudgetLine]) -> BudgetOut:
    return BudgetOut(
        id=budget.id,
        name=budget.name,
        period=budget.period,
        start_date=budget.start_date,
        end_date=budget.end_date,
        currency=budget.currency,
        rollover=budget.rollover,
        lines=[
            BudgetLineOut(
                id=line.id,
                category_id=line.category_id,
                amount=Money.of(line.amount, budget.currency),
            )
            for line in lines
        ],
        created_at=budget.created_at,
        updated_at=budget.updated_at,
    )


def _record(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    budget_id: uuid.UUID,
    verb: str,
    *,
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=f"budget.{verb}",
        target_type="budget",
        target_id=budget_id,
        before=before,
        after=after,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(db, household_id, f"budget.{verb}d", {"budget_id": str(budget_id)})


def _get(
    db: Session, household_id: uuid.UUID, budget_id: uuid.UUID, *, lock: bool = False
) -> Budget:
    budget = repo.get_budget(db, household_id, budget_id, lock=lock)
    if budget is None:
        raise budget_not_found()
    return budget


def _lines(db: Session, budget: Budget) -> list[BudgetLine]:
    return repo.lines_for(db, budget.household_id, [budget.id])[budget.id]


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------


def list_budgets(
    db: Session, principal: Principal, *, cursor: uuid.UUID | None, limit: int
) -> BudgetPage:
    household_id = _household(principal, HouseholdRole.VIEWER)
    rows = repo.list_budgets(db, household_id, after=cursor, limit=limit + 1)
    page = rows[:limit]
    lines = repo.lines_for(db, household_id, [b.id for b in page])
    return BudgetPage(
        items=[budget_out(b, lines[b.id]) for b in page],
        next_cursor=page[-1].id if len(rows) > limit and page else None,
    )


def require_budget(db: Session, principal: Principal, budget_id: uuid.UUID) -> Budget:
    """The budget of the active household, or ``404 budget-not-found``."""
    return _get(db, _household(principal, HouseholdRole.VIEWER), budget_id)


def get_budget(db: Session, principal: Principal, budget_id: uuid.UUID) -> BudgetOut:
    budget = require_budget(db, principal, budget_id)
    return budget_out(budget, _lines(db, budget))


def budget_report(
    db: Session,
    principal: Principal,
    budget_id: uuid.UUID,
    *,
    period: str | None = None,
    include_pending: bool | None = None,
) -> BudgetReportOut:
    """``GET /budgets/{id}/report?period=`` (``app.domain.budgets.reports``)."""
    budget = require_budget(db, principal, budget_id)
    now = today()
    day = parse_period_date(period) if period is not None else now
    pending = resolve_include_pending(db, budget.household_id, include_pending)
    return build_report(
        db, principal, budget, _lines(db, budget), day=day, today=now, include_pending=pending
    )


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------


def create_budget(
    db: Session, principal: Principal, data: BudgetCreate, *, request: HTTPConnection | None
) -> BudgetOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    currency = data.currency or base_currency(db, household_id)
    check_dates(data.period, data.start_date, data.end_date)
    wanted = _check_lines(db, household_id, currency, data.lines)
    budget = Budget(
        household_id=household_id,
        name=data.name,
        period=data.period,
        start_date=data.start_date,
        end_date=data.end_date,
        currency=currency,
        rollover=data.rollover,
    )
    db.add(budget)
    db.flush()
    lines = repo.replace_lines(db, budget, wanted)
    _record(
        db,
        principal,
        household_id,
        budget.id,
        "create",
        after=_state(budget, lines),
        request=request,
    )
    return budget_out(budget, lines)


def update_budget(
    db: Session,
    principal: Principal,
    budget_id: uuid.UUID,
    data: BudgetUpdate,
    *,
    request: HTTPConnection | None,
) -> BudgetOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    budget = _get(db, household_id, budget_id, lock=True)
    current_lines = _lines(db, budget)
    before = _state(budget, current_lines)
    fields = data.model_fields_set
    period = data.period if data.period is not None else budget.period
    start_date = data.start_date if data.start_date is not None else budget.start_date
    end_date = data.end_date if "end_date" in fields else budget.end_date
    if "period" in fields and period is not BudgetPeriod.CUSTOM and "end_date" not in fields:
        end_date = None  # switching away from custom drops the end
    currency = data.currency if data.currency is not None else budget.currency
    check_dates(period, start_date, end_date)
    if currency != budget.currency and data.lines is None and current_lines:
        raise ProblemError(
            422,
            "budget-lines-required",
            "Budget lines required",
            "Changing the currency needs the lines in the new currency in the same request.",
        )
    wanted = (
        _check_lines(db, household_id, currency, data.lines) if data.lines is not None else None
    )
    if data.name is not None:
        budget.name = data.name
    budget.period = period
    budget.start_date = start_date
    budget.end_date = end_date
    budget.currency = currency
    if data.rollover is not None:
        budget.rollover = data.rollover
    db.flush()
    lines = repo.replace_lines(db, budget, wanted) if wanted is not None else current_lines
    after = _state(budget, lines)
    if after != before:
        _record(
            db,
            principal,
            household_id,
            budget.id,
            "update",
            before=before,
            after=after,
            request=request,
        )
    return budget_out(budget, lines)


def delete_budget(
    db: Session, principal: Principal, budget_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    household_id = _household(principal, HouseholdRole.MEMBER)
    budget = _get(db, household_id, budget_id, lock=True)
    before = _state(budget, _lines(db, budget))
    repo.delete_budget(db, budget)
    _record(db, principal, household_id, budget_id, "delete", before=before, request=request)
