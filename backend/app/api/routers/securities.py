# SPDX-License-Identifier: AGPL-3.0-or-later
"""The security catalogue and manual prices (API.md ``/securities``).

* ``GET /securities?q=&asset_class=&cursor=&limit=``, ``GET /securities/{id}``:
  any role; securities with activity on hidden accounts are ``404``.
* ``POST /securities`` (``Idempotency-Key`` honoured), ``PATCH`` and
  ``DELETE /securities/{id}``: member or above.
* ``GET /securities/{id}/prices?from=&to=``: any role; ``POST
  /securities/{id}/prices`` (manual close of a day) and ``DELETE
  /securities/{id}/prices/{price_id}``: member or above.

Tokens need ``investments:read`` / ``investments:write``. Rules:
``app.domain.investments.securities``.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.idempotency import IdempotencyKeyHeader, idempotent, payload_of
from app.domain.investments import securities as service
from app.domain.investments.models import AssetClass
from app.domain.investments.schemas import (
    DEFAULT_PAGE,
    MAX_PAGE,
    PriceOut,
    PricePage,
    PriceSet,
    SecurityCreate,
    SecurityOut,
    SecurityPage,
    SecurityUpdate,
)
from app.domain.investments.service import get_visible_security

router = APIRouter(tags=["investments"])

Reader = Annotated[Principal, Depends(scoped_role("investments:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("investments:write", HouseholdRole.MEMBER))]
Cursor = Annotated[uuid.UUID | None, Query()]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]


def require_security_writer(security_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the security must be visible (``404`` before the body is read)."""
    get_visible_security(db, principal, security_id)
    return principal


SecurityWriter = Annotated[Principal, Depends(require_security_writer)]


@router.get("/securities", response_model=SecurityPage, summary="Securities I can see")
def get_securities(
    db: DbSession,
    principal: Reader,
    q: Annotated[str | None, Query(max_length=100)] = None,
    asset_class: Annotated[AssetClass | None, Query()] = None,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_PAGE,
) -> SecurityPage:
    return service.list_securities(
        db, principal, q=q, asset_class=asset_class, cursor=cursor, limit=limit
    )


@router.post(
    "/securities",
    response_model=SecurityOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Add a security to the catalogue (member or above)",
)
def post_security(
    body: SecurityCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /securities",
        payload=payload_of(body),
        run=lambda: service.create_security(db, principal, body, request=request),
    )


@router.get(
    "/securities/{security_id}",
    response_model=SecurityOut,
    responses=PROBLEMS,
    summary="One security",
)
def get_security(security_id: uuid.UUID, db: DbSession, principal: Reader) -> SecurityOut:
    return service.get_security(db, principal, security_id)


@router.patch(
    "/securities/{security_id}",
    response_model=SecurityOut,
    responses=PROBLEMS,
    summary="Change a security (member or above)",
)
def patch_security(
    security_id: uuid.UUID,
    body: SecurityUpdate,
    request: Request,
    db: DbSession,
    principal: SecurityWriter,
) -> SecurityOut:
    return service.update_security(db, principal, security_id, body, request=request)


@router.delete(
    "/securities/{security_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete an unused security and its prices (member or above)",
)
def delete_security(
    security_id: uuid.UUID, request: Request, db: DbSession, principal: Writer
) -> Response:
    service.delete_security(db, principal, security_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/securities/{security_id}/prices",
    response_model=PricePage,
    responses=PROBLEMS,
    summary="Closing prices of a security, newest first",
)
def get_prices(
    security_id: uuid.UUID,
    db: DbSession,
    principal: Reader,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_PAGE,
) -> PricePage:
    return service.list_prices(
        db,
        principal,
        security_id,
        date_from=date_from,
        date_to=date_to,
        cursor=cursor,
        limit=limit,
    )


@router.post(
    "/securities/{security_id}/prices",
    response_model=PriceOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Set the manual close of a day (member or above)",
)
def post_price(
    security_id: uuid.UUID,
    body: PriceSet,
    request: Request,
    db: DbSession,
    principal: SecurityWriter,
) -> PriceOut:
    return service.set_price(db, principal, security_id, body, request=request)


@router.delete(
    "/securities/{security_id}/prices/{price_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a price (member or above)",
)
def delete_price(
    security_id: uuid.UUID,
    price_id: uuid.UUID,
    request: Request,
    db: DbSession,
    principal: Writer,
) -> Response:
    service.delete_price(db, principal, security_id, price_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
