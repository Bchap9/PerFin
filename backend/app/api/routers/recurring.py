# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recurring transactions and the cash-flow calendar (API.md ``/recurring``, ``/cashflow``).

* ``GET /recurring?origin=&active=&account=``: any role; schedules of visible
  accounts (detected and manual), soonest first, at most 500.
* ``GET /recurring/candidates?account=``: any role; detection over the
  visible accounts, nothing written.
* ``POST /recurring/detect``: member or above; writes detected schedules for
  the accounts the caller may edit.
* ``POST /recurring`` (``Idempotency-Key`` honoured), ``GET`` / ``PATCH`` /
  ``DELETE /recurring/{id}``: reads any role, writes member or above on an
  account the caller may edit.
* ``GET /cashflow/calendar?days=&account=``: any role; expected income and
  bills for the next ``days`` days (1-366, default 30).

Tokens need ``transactions:read`` / ``transactions:write``. Rules:
``app.domain.recurring.service``.
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
from app.domain.recurring import service
from app.domain.recurring.schemas import (
    DEFAULT_CALENDAR_DAYS,
    MAX_CALENDAR_DAYS,
    MAX_FILTER_VALUES,
    CandidateList,
    CashflowCalendar,
    DetectIn,
    DetectResult,
    ScheduleCreate,
    ScheduleList,
    ScheduleOut,
    ScheduleUpdate,
)
from app.domain.transactions.models import ScheduleOrigin

router = APIRouter(tags=["recurring"])

Reader = Annotated[Principal, Depends(scoped_role("transactions:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("transactions:write", HouseholdRole.MEMBER))]
Accounts = Annotated[list[uuid.UUID] | None, Query(alias="account", max_length=MAX_FILTER_VALUES)]


def require_schedule_reader(schedule_id: uuid.UUID, db: DbSession, principal: Reader) -> Principal:
    """:data:`Reader`, then the schedule must be visible (``404`` before the body is read)."""
    service.get_row(db, principal, schedule_id)
    return principal


def require_schedule_writer(schedule_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the schedule must be editable (``404``/``403`` before the body)."""
    service.require_editable(db, principal, schedule_id)
    return principal


ScheduleReader = Annotated[Principal, Depends(require_schedule_reader)]
ScheduleWriter = Annotated[Principal, Depends(require_schedule_writer)]


@router.get(
    "/recurring",
    response_model=ScheduleList,
    responses=PROBLEMS,
    summary="Recurring transactions I can see",
)
def get_recurring(
    db: DbSession,
    principal: Reader,
    origin: Annotated[ScheduleOrigin | None, Query()] = None,
    active: Annotated[bool | None, Query()] = None,
    account: Accounts = None,
) -> ScheduleList:
    return service.list_schedules(db, principal, origin=origin, active=active, account_ids=account)


@router.get(
    "/recurring/candidates",
    response_model=CandidateList,
    responses=PROBLEMS,
    summary="Detected recurring patterns (dry run, nothing stored)",
)
def get_candidates(db: DbSession, principal: Reader, account: Accounts = None) -> CandidateList:
    return service.candidates(db, principal, account_ids=account)


@router.post(
    "/recurring/detect",
    response_model=DetectResult,
    responses=PROBLEMS,
    summary="Detect recurring transactions and store them (member or above)",
)
def post_detect(body: DetectIn, request: Request, db: DbSession, principal: Writer) -> DetectResult:
    return service.detect(db, principal, body, request=request)


@router.post(
    "/recurring",
    response_model=ScheduleOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Add a recurring transaction by hand (member or above)",
)
def post_recurring(
    body: ScheduleCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /recurring",
        payload=payload_of(body),
        run=lambda: service.create_schedule(db, principal, body, request=request),
    )


@router.get(
    "/recurring/{schedule_id}",
    response_model=ScheduleOut,
    responses=PROBLEMS,
    summary="One recurring transaction",
)
def get_schedule(schedule_id: uuid.UUID, db: DbSession, principal: ScheduleReader) -> ScheduleOut:
    return service.get_schedule(db, principal, schedule_id)


@router.patch(
    "/recurring/{schedule_id}",
    response_model=ScheduleOut,
    responses=PROBLEMS,
    summary="Change a recurring transaction (member or above)",
)
def patch_schedule(
    schedule_id: uuid.UUID,
    body: ScheduleUpdate,
    request: Request,
    db: DbSession,
    principal: ScheduleWriter,
) -> ScheduleOut:
    return service.update_schedule(db, principal, schedule_id, body, request=request)


@router.delete(
    "/recurring/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a recurring transaction (member or above)",
)
def delete_schedule(
    schedule_id: uuid.UUID, request: Request, db: DbSession, principal: ScheduleWriter
) -> Response:
    service.delete_schedule(db, principal, schedule_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/cashflow/calendar",
    response_model=CashflowCalendar,
    responses=PROBLEMS,
    summary="Expected income and bills, day by day",
)
def get_calendar(
    db: DbSession,
    principal: Reader,
    days: Annotated[int, Query(ge=1, le=MAX_CALENDAR_DAYS)] = DEFAULT_CALENDAR_DAYS,
    account: Accounts = None,
) -> CashflowCalendar:
    return service.calendar(db, principal, days=days, account_ids=account)
