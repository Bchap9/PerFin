# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exchange rates (API.md "FX rates").

* ``GET /fx-rates?base=&quote=&from=&to=&cursor=&limit=``: any role.
* ``POST /fx-rates`` (``Idempotency-Key`` honoured) and ``DELETE
  /fx-rates/{rate_id}``: owner or admin.

Tokens need ``accounts:read`` / ``accounts:write``. Rules:
``app.domain.fx.manage``; fetched rates: ``app.integrations.fx``.
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
from app.core.money import CurrencyCode
from app.domain.fx import manage as service
from app.domain.fx.schemas import DEFAULT_PAGE, MAX_PAGE, FxRateOut, FxRatePage, FxRateSet

router = APIRouter(tags=["fx"])

Reader = Annotated[Principal, Depends(scoped_role("accounts:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("accounts:write", HouseholdRole.ADMIN))]


@router.get("/fx-rates", response_model=FxRatePage, responses=PROBLEMS, summary="Rate history")
def get_fx_rates(
    db: DbSession,
    principal: Reader,
    base: Annotated[CurrencyCode | None, Query()] = None,
    quote: Annotated[CurrencyCode | None, Query()] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> FxRatePage:
    return service.list_rates(
        db,
        principal,
        base=base,
        quote=quote,
        date_from=date_from,
        date_to=date_to,
        cursor=cursor,
        limit=limit,
    )


@router.post(
    "/fx-rates",
    response_model=FxRateOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Set the manual rate of a pair and day (owner or admin)",
)
def post_fx_rate(
    body: FxRateSet,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /fx-rates",
        payload=payload_of(body),
        run=lambda: service.set_rate(db, principal, body, request=request),
    )


@router.delete(
    "/fx-rates/{rate_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a rate (owner or admin)",
)
def delete_fx_rate(
    rate_id: uuid.UUID, request: Request, db: DbSession, principal: Writer
) -> Response:
    service.delete_rate(db, principal, rate_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
