# SPDX-License-Identifier: AGPL-3.0-or-later
"""Accounts and balances (API.md ``/accounts``).

* ``GET /accounts?cursor=&limit=&closed=&type=``: the accounts the caller can
  see (owner/admin all; members and viewers their granted ones), in creation
  order, ``{"items": [...], "next_cursor"}``; each with today's balance and
  ``can_edit``.
* ``POST /accounts``: member or above (``Idempotency-Key`` honoured). A
  member creating an account gets an ``edit`` grant on it.
* ``GET /accounts/{id}``: visible accounts only; a hidden, foreign or
  missing account is ``404 account-not-found``.
* ``PATCH /accounts/{id}``, ``POST /accounts/{id}/close``, ``POST
  /accounts/{id}/reopen``: member or above with ``edit`` on the account.
* ``DELETE /accounts/{id}``: owner or admin; transactions, balances,
  schedules and grants of the account go with it.
* ``GET /accounts/{id}/balances?start=&end=``: end-of-day balance history
  (default the last 90 days, at most 3660) plus the snapshots in the range.
* ``POST /accounts/{id}/balances``: record a manual balance (member or above
  with ``edit``; ``Idempotency-Key`` honoured).

Tokens need ``accounts:read`` / ``accounts:write``. Rules live in
``app.domain.accounts.service``; this module resolves the caller and maps
to responses. :func:`scoped_role` is shared by the other ledger routers.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.auth.authz import require_role, require_scope
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.idempotency import IdempotencyKeyHeader, idempotent, payload_of
from app.domain.accounts import service
from app.domain.accounts.models import AccountType
from app.domain.accounts.schemas import (
    DEFAULT_PAGE,
    MAX_PAGE,
    AccountClose,
    AccountCreate,
    AccountOut,
    AccountPage,
    AccountUpdate,
    BalanceHistoryOut,
    ManualBalanceIn,
    SnapshotOut,
)

router = APIRouter(tags=["accounts"])

PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: {"description": "Problem details", "content": {"application/problem+json": {}}}
    for code in (403, 404, 409, 422)
}


def scoped_role(scope: str, minimum: HouseholdRole) -> Callable[[Principal], Principal]:
    """Dependency: a household role of at least ``minimum`` and (for tokens) ``scope``."""
    scope_guard = require_scope(scope)
    role_guard = require_role(minimum)

    # A default instead of ``Annotated[...]``: with postponed annotations a
    # closure variable in an annotation could not be resolved by FastAPI.
    def guard(principal: Principal = Depends(role_guard)) -> Principal:  # noqa: B008
        return scope_guard(principal)

    guard.__name__ = f"require_{scope.replace(':', '_')}_{minimum.value}"
    return guard


require_reader = scoped_role("accounts:read", HouseholdRole.VIEWER)
require_writer = scoped_role("accounts:write", HouseholdRole.MEMBER)
Reader = Annotated[Principal, Depends(require_reader)]
Writer = Annotated[Principal, Depends(require_writer)]
Manager = Annotated[Principal, Depends(scoped_role("accounts:write", HouseholdRole.ADMIN))]


def require_account_writer(account_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the account must be visible (``404`` before the body is read).

    Object checks normally run in the service; routes with a request body
    check the path's account here too, so a foreign or hidden account is
    ``404 account-not-found`` whatever the body holds.
    """
    service.require_visible(db, principal, account_id)
    return principal


AccountWriter = Annotated[Principal, Depends(require_account_writer)]


@router.get("/accounts", response_model=AccountPage, summary="Accounts I can see")
def get_accounts(
    db: DbSession,
    principal: Reader,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    closed: Annotated[bool | None, Query()] = None,
    account_type: Annotated[AccountType | None, Query(alias="type")] = None,
) -> AccountPage:
    return service.list_accounts(
        db, principal, cursor=cursor, limit=limit, closed=closed, account_type=account_type
    )


@router.post(
    "/accounts",
    response_model=AccountOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Create an account (member or above)",
)
def post_account(
    body: AccountCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /accounts",
        payload=payload_of(body),
        run=lambda: service.create_account(db, principal, body, request=request),
    )


@router.get(
    "/accounts/{account_id}", response_model=AccountOut, responses=PROBLEMS, summary="One account"
)
def get_account(account_id: uuid.UUID, db: DbSession, principal: Reader) -> AccountOut:
    return service.get_account(db, principal, account_id)


@router.patch(
    "/accounts/{account_id}",
    response_model=AccountOut,
    responses=PROBLEMS,
    summary="Change an account (edit access)",
)
def patch_account(
    account_id: uuid.UUID,
    body: AccountUpdate,
    request: Request,
    db: DbSession,
    principal: AccountWriter,
) -> AccountOut:
    return service.update_account(db, principal, account_id, body, request=request)


@router.delete(
    "/accounts/{account_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete an account and everything on it (owner/admin)",
)
def delete_account(
    account_id: uuid.UUID, request: Request, db: DbSession, principal: Manager
) -> Response:
    service.delete_account(db, principal, account_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/accounts/{account_id}/close",
    response_model=AccountOut,
    responses=PROBLEMS,
    summary="Close an account (edit access)",
)
def post_account_close(
    account_id: uuid.UUID,
    request: Request,
    db: DbSession,
    principal: AccountWriter,
    body: AccountClose | None = None,
) -> AccountOut:
    data = body if body is not None else AccountClose()
    return service.close_account(db, principal, account_id, data, request=request)


@router.post(
    "/accounts/{account_id}/reopen",
    response_model=AccountOut,
    responses=PROBLEMS,
    summary="Reopen a closed account (edit access)",
)
def post_account_reopen(
    account_id: uuid.UUID, request: Request, db: DbSession, principal: Writer
) -> AccountOut:
    return service.reopen_account(db, principal, account_id, request=request)


@router.get(
    "/accounts/{account_id}/balances",
    response_model=BalanceHistoryOut,
    responses=PROBLEMS,
    summary="End-of-day balance history",
)
def get_account_balances(
    account_id: uuid.UUID,
    db: DbSession,
    principal: Reader,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
) -> BalanceHistoryOut:
    return service.balance_history(db, principal, account_id, start=start, end=end)


@router.post(
    "/accounts/{account_id}/balances",
    response_model=SnapshotOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Record a manual balance (edit access)",
)
def post_account_balance(
    account_id: uuid.UUID,
    body: ManualBalanceIn,
    request: Request,
    db: DbSession,
    principal: AccountWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /accounts/{account_id}/balances",
        payload=payload_of(body, account_id=account_id),
        run=lambda: service.record_manual_balance(db, principal, account_id, body, request=request),
    )
