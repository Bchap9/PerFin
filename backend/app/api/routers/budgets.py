# SPDX-License-Identifier: AGPL-3.0-or-later
"""Budgets (API.md ``/budgets``; rules in ``app.domain.budgets.service``).

* ``GET /budgets?cursor=&limit=``, ``GET /budgets/{id}``: any role.
* ``GET /budgets/{id}/report?period=&include_pending=``: budgeted vs actual
  vs forecast per line for the period containing ``period`` (``YYYY-MM`` or
  ``YYYY-MM-DD``, default today), in the budget's currency; actuals only
  from accounts the caller may see.
* ``POST /budgets`` (``Idempotency-Key`` honoured), ``PATCH /budgets/{id}``,
  ``DELETE /budgets/{id}``: member or above.

Tokens need ``budgets:read`` / ``budgets:write``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.idempotency import IdempotencyKeyHeader, idempotent, payload_of
from app.domain.budgets import service
from app.domain.budgets.schemas import (
    DEFAULT_PAGE,
    MAX_PAGE,
    BudgetCreate,
    BudgetOut,
    BudgetPage,
    BudgetReportOut,
    BudgetUpdate,
)

router = APIRouter(tags=["budgets"])

Reader = Annotated[Principal, Depends(scoped_role("budgets:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("budgets:write", HouseholdRole.MEMBER))]


def require_budget_writer(budget_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the budget must exist here (``404`` before the body is read)."""
    service.require_budget(db, principal, budget_id)
    return principal


BudgetWriter = Annotated[Principal, Depends(require_budget_writer)]


@router.get("/budgets", response_model=BudgetPage, summary="Budgets of the household")
def get_budgets(
    db: DbSession,
    principal: Reader,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> BudgetPage:
    return service.list_budgets(db, principal, cursor=cursor, limit=limit)


@router.post(
    "/budgets",
    response_model=BudgetOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Create a budget (member or above)",
)
def post_budget(
    body: BudgetCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /budgets",
        payload=payload_of(body),
        run=lambda: service.create_budget(db, principal, body, request=request),
    )


@router.get(
    "/budgets/{budget_id}", response_model=BudgetOut, responses=PROBLEMS, summary="One budget"
)
def get_budget(budget_id: uuid.UUID, db: DbSession, principal: Reader) -> BudgetOut:
    return service.get_budget(db, principal, budget_id)


@router.patch(
    "/budgets/{budget_id}",
    response_model=BudgetOut,
    responses=PROBLEMS,
    summary="Change a budget; lines replace the whole set (member or above)",
)
def patch_budget(
    budget_id: uuid.UUID,
    body: BudgetUpdate,
    request: Request,
    db: DbSession,
    principal: BudgetWriter,
) -> BudgetOut:
    return service.update_budget(db, principal, budget_id, body, request=request)


@router.delete(
    "/budgets/{budget_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a budget (member or above)",
)
def delete_budget(
    budget_id: uuid.UUID, request: Request, db: DbSession, principal: Writer
) -> Response:
    service.delete_budget(db, principal, budget_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/budgets/{budget_id}/report",
    response_model=BudgetReportOut,
    responses=PROBLEMS,
    summary="Budgeted vs actual vs forecast for one period",
)
def get_budget_report(
    budget_id: uuid.UUID,
    db: DbSession,
    principal: Reader,
    period: Annotated[str | None, Query(max_length=10)] = None,
    include_pending: Annotated[bool | None, Query()] = None,
) -> BudgetReportOut:
    return service.budget_report(
        db, principal, budget_id, period=period, include_pending=include_pending
    )
