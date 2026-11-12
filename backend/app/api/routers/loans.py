# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loans, schedules, extra-payment simulation, payments and rate changes (API.md ``/loans``).

* ``GET /loans?cursor=&limit=``, ``GET /loans/{id}``: any role; loans of
  visible accounts only, with the balance owed and a payoff projection.
* ``POST /loans`` (``Idempotency-Key`` honoured), ``PATCH`` / ``DELETE
  /loans/{id}``: member or above, on an account the caller may edit;
  ``POST`` with ``account`` opens the loan's account too (a token then
  also needs ``accounts:write``).
* ``GET /loans/{id}/schedule?basis=original|projected``: any role.
* ``POST /loans/{id}/simulate``: any role (stores nothing): payoff date,
  interest saved and payments saved with one-off and recurring extras.
* ``GET /loans/{id}/payments?from=&to=&limit=``: any role, each payment
  checked against the schedule; ``POST`` (``Idempotency-Key`` honoured) and
  ``DELETE /loans/{id}/payments/{payment_id}``: member or above.
* ``GET /loans/{id}/rate-changes``: any role; ``POST`` and ``DELETE
  /loans/{id}/rate-changes/{change_id}``: member or above.

Tokens need ``loans:read`` / ``loans:write``. Rules: ``app.domain.loans.service``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.idempotency import IdempotencyKeyHeader, idempotent, payload_of
from app.domain.loans import service
from app.domain.loans.schemas import (
    DEFAULT_PAGE,
    MAX_PAGE,
    MAX_PAYMENTS,
    LoanCreate,
    LoanOut,
    LoanPage,
    LoanPaymentCreate,
    LoanPaymentList,
    LoanPaymentOut,
    LoanUpdate,
    RateChangeCreate,
    RateChangeList,
    RateChangeOut,
    ScheduleOut,
    SimulateIn,
    SimulationOut,
)

router = APIRouter(tags=["loans"])

Reader = Annotated[Principal, Depends(scoped_role("loans:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("loans:write", HouseholdRole.MEMBER))]
Cursor = Annotated[uuid.UUID | None, Query()]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]


def require_loan_reader(loan_id: uuid.UUID, db: DbSession, principal: Reader) -> Principal:
    """:data:`Reader`, then the loan must be visible (``404`` before the body is read)."""
    service.get_loan_row(db, principal, loan_id)
    return principal


def require_loan_writer(loan_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the loan must be editable (``404`` before the body is read)."""
    service.require_editable(db, principal, loan_id)
    return principal


LoanReader = Annotated[Principal, Depends(require_loan_reader)]
LoanWriter = Annotated[Principal, Depends(require_loan_writer)]


@router.get("/loans", response_model=LoanPage, summary="Loans I can see")
def get_loans(
    db: DbSession, principal: Reader, cursor: Cursor = None, limit: Limit = DEFAULT_PAGE
) -> LoanPage:
    return service.list_loans(db, principal, cursor=cursor, limit=limit)


@router.post(
    "/loans",
    response_model=LoanOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Add a loan: its terms, on a new or an existing account (member or above)",
)
def post_loan(
    body: LoanCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /loans",
        payload=payload_of(body),
        run=lambda: service.create_loan(db, principal, body, request=request),
    )


@router.get("/loans/{loan_id}", response_model=LoanOut, responses=PROBLEMS, summary="One loan")
def get_loan(loan_id: uuid.UUID, db: DbSession, principal: Reader) -> LoanOut:
    return service.get_loan(db, principal, loan_id)


@router.patch(
    "/loans/{loan_id}",
    response_model=LoanOut,
    responses=PROBLEMS,
    summary="Change a loan's terms (member or above)",
)
def patch_loan(
    loan_id: uuid.UUID, body: LoanUpdate, request: Request, db: DbSession, principal: LoanWriter
) -> LoanOut:
    return service.update_loan(db, principal, loan_id, body, request=request)


@router.delete(
    "/loans/{loan_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a loan's terms, payments and rate changes (member or above)",
)
def delete_loan(loan_id: uuid.UUID, request: Request, db: DbSession, principal: Writer) -> Response:
    service.delete_loan(db, principal, loan_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/loans/{loan_id}/schedule",
    response_model=ScheduleOut,
    responses=PROBLEMS,
    summary="Amortisation schedule (original or projected from today)",
)
def get_schedule(
    loan_id: uuid.UUID,
    db: DbSession,
    principal: Reader,
    basis: Annotated[Literal["original", "projected"], Query()] = "original",
) -> ScheduleOut:
    return service.get_schedule(db, principal, loan_id, basis=basis)


@router.post(
    "/loans/{loan_id}/simulate",
    response_model=SimulationOut,
    responses=PROBLEMS,
    summary="Extra-payment what-if (stores nothing)",
)
def post_simulate(
    loan_id: uuid.UUID, body: SimulateIn, db: DbSession, principal: LoanReader
) -> SimulationOut:
    return service.simulate(db, principal, loan_id, body)


@router.get(
    "/loans/{loan_id}/payments",
    response_model=LoanPaymentList,
    responses=PROBLEMS,
    summary="Recorded payments, checked against the schedule",
)
def get_payments(
    loan_id: uuid.UUID,
    db: DbSession,
    principal: Reader,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAYMENTS)] = MAX_PAYMENTS,
) -> LoanPaymentList:
    return service.list_payments(db, principal, loan_id, start=start, end=end, limit=limit)


@router.post(
    "/loans/{loan_id}/payments",
    response_model=LoanPaymentOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Record a payment, as its split or the amount paid (member or above)",
)
def post_payment(
    loan_id: uuid.UUID,
    body: LoanPaymentCreate,
    request: Request,
    db: DbSession,
    principal: LoanWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /loans/{loan_id}/payments",
        payload=payload_of(body, loan_id=loan_id),
        run=lambda: service.add_payment(db, principal, loan_id, body, request=request),
    )


@router.delete(
    "/loans/{loan_id}/payments/{payment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Remove a payment (member or above)",
)
def delete_payment(
    loan_id: uuid.UUID,
    payment_id: uuid.UUID,
    request: Request,
    db: DbSession,
    principal: Writer,
) -> Response:
    service.delete_payment(db, principal, loan_id, payment_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/loans/{loan_id}/rate-changes",
    response_model=RateChangeList,
    responses=PROBLEMS,
    summary="Rate changes of a loan",
)
def get_rate_changes(loan_id: uuid.UUID, db: DbSession, principal: Reader) -> RateChangeList:
    return service.list_rate_changes(db, principal, loan_id)


@router.post(
    "/loans/{loan_id}/rate-changes",
    response_model=RateChangeOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Add a rate change (member or above)",
)
def post_rate_change(
    loan_id: uuid.UUID,
    body: RateChangeCreate,
    request: Request,
    db: DbSession,
    principal: LoanWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /loans/{loan_id}/rate-changes",
        payload=payload_of(body, loan_id=loan_id),
        run=lambda: service.add_rate_change(db, principal, loan_id, body, request=request),
    )


@router.delete(
    "/loans/{loan_id}/rate-changes/{change_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Remove a rate change (member or above)",
)
def delete_rate_change(
    loan_id: uuid.UUID,
    change_id: uuid.UUID,
    request: Request,
    db: DbSession,
    principal: Writer,
) -> Response:
    service.delete_rate_change(db, principal, loan_id, change_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
