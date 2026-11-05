# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manual assets (property, vehicle, other) and their valuations (API.md ``/assets``).

* ``GET /assets?cursor=&limit=``, ``GET /assets/{id}``: any role; assets of
  visible accounts only, with the current value.
* ``POST /assets`` (``Idempotency-Key`` honoured), ``PATCH`` / ``DELETE
  /assets/{id}``: member or above, on an account the caller may edit.
* ``GET /assets/{id}/valuations?from=&to=&limit=``: any role, newest first;
  ``POST`` (``Idempotency-Key`` honoured) and ``DELETE
  /assets/{id}/valuations/{valuation_id}``: member or above.

Tokens need ``accounts:read`` / ``accounts:write`` (a manual asset is an
account). Rules: ``app.domain.assets.service``.
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
from app.domain.assets import service
from app.domain.assets.schemas import (
    DEFAULT_PAGE,
    MAX_PAGE,
    MAX_VALUATIONS,
    AssetCreate,
    AssetOut,
    AssetPage,
    AssetUpdate,
    ValuationCreate,
    ValuationList,
    ValuationOut,
)

router = APIRouter(tags=["assets"])

Reader = Annotated[Principal, Depends(scoped_role("accounts:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("accounts:write", HouseholdRole.MEMBER))]
Cursor = Annotated[uuid.UUID | None, Query()]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]


def require_asset_writer(asset_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the asset must be editable (``404`` before the body is read)."""
    service.require_editable(db, principal, asset_id)
    return principal


AssetWriter = Annotated[Principal, Depends(require_asset_writer)]


@router.get("/assets", response_model=AssetPage, summary="Manual assets I can see")
def get_assets(
    db: DbSession, principal: Reader, cursor: Cursor = None, limit: Limit = DEFAULT_PAGE
) -> AssetPage:
    return service.list_assets(db, principal, cursor=cursor, limit=limit)


@router.post(
    "/assets",
    response_model=AssetOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Describe a manual asset account (member or above)",
)
def post_asset(
    body: AssetCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /assets",
        payload=payload_of(body),
        run=lambda: service.create_asset(db, principal, body, request=request),
    )


@router.get("/assets/{asset_id}", response_model=AssetOut, responses=PROBLEMS, summary="One asset")
def get_asset(asset_id: uuid.UUID, db: DbSession, principal: Reader) -> AssetOut:
    return service.get_asset(db, principal, asset_id)


@router.patch(
    "/assets/{asset_id}",
    response_model=AssetOut,
    responses=PROBLEMS,
    summary="Change an asset's kind, description or valuation method (member or above)",
)
def patch_asset(
    asset_id: uuid.UUID, body: AssetUpdate, request: Request, db: DbSession, principal: AssetWriter
) -> AssetOut:
    return service.update_asset(db, principal, asset_id, body, request=request)


@router.delete(
    "/assets/{asset_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete an asset's details (member or above)",
)
def delete_asset(
    asset_id: uuid.UUID, request: Request, db: DbSession, principal: Writer
) -> Response:
    service.delete_asset(db, principal, asset_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/assets/{asset_id}/valuations",
    response_model=ValuationList,
    responses=PROBLEMS,
    summary="Valuation history, newest first",
)
def get_valuations(
    asset_id: uuid.UUID,
    db: DbSession,
    principal: Reader,
    start: Annotated[date | None, Query(alias="from")] = None,
    end: Annotated[date | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_VALUATIONS)] = MAX_VALUATIONS,
) -> ValuationList:
    return service.list_valuations(db, principal, asset_id, start=start, end=end, limit=limit)


@router.post(
    "/assets/{asset_id}/valuations",
    response_model=ValuationOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Enter a manual valuation (member or above)",
)
def post_valuation(
    asset_id: uuid.UUID,
    body: ValuationCreate,
    request: Request,
    db: DbSession,
    principal: AssetWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /assets/{asset_id}/valuations",
        payload=payload_of(body, asset_id=asset_id),
        run=lambda: service.add_valuation(db, principal, asset_id, body, request=request),
    )


@router.delete(
    "/assets/{asset_id}/valuations/{valuation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Remove a valuation (member or above)",
)
def delete_valuation(
    asset_id: uuid.UUID,
    valuation_id: uuid.UUID,
    request: Request,
    db: DbSession,
    principal: Writer,
) -> Response:
    service.delete_valuation(db, principal, asset_id, valuation_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
