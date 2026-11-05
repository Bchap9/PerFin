# SPDX-License-Identifier: AGPL-3.0-or-later
"""Payees (API.md ``/payees``; rules in ``app.domain.taxonomy.service``).

* ``GET /payees?cursor=&limit=&q=`` (``q`` matches inside the normalised
  name), ``GET /payees/{id}``: any role.
* ``POST /payees`` (``Idempotency-Key`` honoured), ``PATCH /payees/{id}``
  (name, aliases, default category): member or above.
* ``DELETE /payees/{id}``, ``POST /payees/{id}/merge`` (``{"into": id}``):
  owner or admin, because they rewrite transactions on every account.

Tokens need ``transactions:read`` / ``transactions:write``.
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
from app.domain.taxonomy import service
from app.domain.taxonomy.schemas import (
    DEFAULT_PAGE,
    MAX_PAGE,
    SEARCH_MAX,
    MergeInto,
    PayeeCreate,
    PayeeOut,
    PayeePage,
    PayeeUpdate,
)

router = APIRouter(tags=["payees"])

Reader = Annotated[Principal, Depends(scoped_role("transactions:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("transactions:write", HouseholdRole.MEMBER))]
Manager = Annotated[Principal, Depends(scoped_role("transactions:write", HouseholdRole.ADMIN))]


def require_payee_writer(payee_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the payee must exist here (``404`` before the body is read)."""
    service.get_payee(db, principal, payee_id)
    return principal


PayeeWriter = Annotated[Principal, Depends(require_payee_writer)]


def require_payee_manager(payee_id: uuid.UUID, db: DbSession, principal: Manager) -> Principal:
    """:data:`Manager`, then the payee must exist here (``404`` before the body is read)."""
    service.get_payee(db, principal, payee_id)
    return principal


PayeeManager = Annotated[Principal, Depends(require_payee_manager)]


@router.get("/payees", response_model=PayeePage, summary="Payees of the household")
def get_payees(
    db: DbSession,
    principal: Reader,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
    q: Annotated[str | None, Query(max_length=SEARCH_MAX)] = None,
) -> PayeePage:
    return service.list_payees(db, principal, cursor=cursor, limit=limit, q=q)


@router.post(
    "/payees",
    response_model=PayeeOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Create a payee (member or above)",
)
def post_payee(
    body: PayeeCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /payees",
        payload=payload_of(body),
        run=lambda: service.create_payee(db, principal, body, request=request),
    )


@router.get("/payees/{payee_id}", response_model=PayeeOut, responses=PROBLEMS, summary="One payee")
def get_payee(payee_id: uuid.UUID, db: DbSession, principal: Reader) -> PayeeOut:
    return service.get_payee(db, principal, payee_id)


@router.patch(
    "/payees/{payee_id}",
    response_model=PayeeOut,
    responses=PROBLEMS,
    summary="Change a payee (member or above)",
)
def patch_payee(
    payee_id: uuid.UUID, body: PayeeUpdate, request: Request, db: DbSession, principal: PayeeWriter
) -> PayeeOut:
    return service.update_payee(db, principal, payee_id, body, request=request)


@router.delete(
    "/payees/{payee_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a payee (owner/admin)",
)
def delete_payee(
    payee_id: uuid.UUID, request: Request, db: DbSession, principal: Manager
) -> Response:
    service.delete_payee(db, principal, payee_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/payees/{payee_id}/merge",
    response_model=PayeeOut,
    responses=PROBLEMS,
    summary="Merge a payee into another (owner/admin)",
)
def post_payee_merge(
    payee_id: uuid.UUID, body: MergeInto, request: Request, db: DbSession, principal: PayeeManager
) -> PayeeOut:
    return service.merge_payee(db, principal, payee_id, body, request=request)
