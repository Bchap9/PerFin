# SPDX-License-Identifier: AGPL-3.0-or-later
"""Spending, income, trend and cash-flow reports (API.md ``/reports``).

* ``GET /reports/spending`` and ``GET /reports/income``:
  ``date_from``/``date_to`` (default the current month), ``group_by``
  (``category`` | ``payee`` | ``month`` | ``account``), ``depth`` (1-3,
  category roll-up level), ``account`` (repeatable, at most 50),
  ``include_pending`` (default: the household setting
  ``reports.include_pending_review``, itself ``false`` by default).
* ``GET /reports/trends``: ``flow`` (``spending`` | ``income``), ``month``
  (``YYYY-MM``, default the current month), ``months`` (series length,
  1-60, default 12), ``depth``, ``account``, ``include_pending``.
* ``GET /reports/cashflow``: ``date_from``/``date_to`` (default this month
  and the eleven before), ``account``, ``include_pending``.

Any household role (token scope ``transactions:read``); every amount comes
only from accounts the caller may see. Rules in
``app.domain.reports.service``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.domain.reports import service
from app.domain.reports.dates import parse_month
from app.domain.reports.schemas import (
    DEFAULT_TREND_MONTHS,
    MAX_DEPTH,
    MAX_FILTER_VALUES,
    MAX_TREND_MONTHS,
    CashflowReport,
    Flow,
    FlowReport,
    GroupBy,
    TrendReport,
)

router = APIRouter(tags=["reports"])

Reader = Annotated[Principal, Depends(scoped_role("transactions:read", HouseholdRole.VIEWER))]
Accounts = Annotated[list[uuid.UUID] | None, Query(alias="account", max_length=MAX_FILTER_VALUES)]
Depth = Annotated[int, Query(ge=1, le=MAX_DEPTH)]
Pending = Annotated[bool | None, Query()]
From = Annotated[date | None, Query()]
To = Annotated[date | None, Query()]


@router.get(
    "/reports/spending",
    response_model=FlowReport,
    responses=PROBLEMS,
    summary="Spending by category, payee, month or account",
)
def get_spending(
    db: DbSession,
    principal: Reader,
    date_from: From = None,
    date_to: To = None,
    group_by: Annotated[GroupBy, Query()] = GroupBy.CATEGORY,
    depth: Depth = 1,
    account: Accounts = None,
    include_pending: Pending = None,
) -> FlowReport:
    return service.flow_report(
        db,
        principal,
        flow=Flow.SPENDING,
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        depth=depth,
        account_ids=account,
        include_pending=include_pending,
    )


@router.get(
    "/reports/income",
    response_model=FlowReport,
    responses=PROBLEMS,
    summary="Income by category, payee, month or account",
)
def get_income(
    db: DbSession,
    principal: Reader,
    date_from: From = None,
    date_to: To = None,
    group_by: Annotated[GroupBy, Query()] = GroupBy.CATEGORY,
    depth: Depth = 1,
    account: Accounts = None,
    include_pending: Pending = None,
) -> FlowReport:
    return service.flow_report(
        db,
        principal,
        flow=Flow.INCOME,
        date_from=date_from,
        date_to=date_to,
        group_by=group_by,
        depth=depth,
        account_ids=account,
        include_pending=include_pending,
    )


@router.get(
    "/reports/trends",
    response_model=TrendReport,
    responses=PROBLEMS,
    summary="Month-on-month and year-on-year trends",
)
def get_trends(
    db: DbSession,
    principal: Reader,
    flow: Annotated[Flow, Query()] = Flow.SPENDING,
    month: Annotated[str | None, Query(max_length=7)] = None,
    months: Annotated[int, Query(ge=1, le=MAX_TREND_MONTHS)] = DEFAULT_TREND_MONTHS,
    depth: Depth = 1,
    account: Accounts = None,
    include_pending: Pending = None,
) -> TrendReport:
    return service.trends(
        db,
        principal,
        flow=flow,
        month=parse_month(month) if month is not None else None,
        months=months,
        depth=depth,
        account_ids=account,
        include_pending=include_pending,
    )


@router.get(
    "/reports/cashflow",
    response_model=CashflowReport,
    responses=PROBLEMS,
    summary="Money in, money out and net per month",
)
def get_cashflow(
    db: DbSession,
    principal: Reader,
    date_from: From = None,
    date_to: To = None,
    account: Accounts = None,
    include_pending: Pending = None,
) -> CashflowReport:
    return service.cashflow(
        db,
        principal,
        date_from=date_from,
        date_to=date_to,
        account_ids=account,
        include_pending=include_pending,
    )
