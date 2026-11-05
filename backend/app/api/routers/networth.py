# SPDX-License-Identifier: AGPL-3.0-or-later
"""Net worth history, latest figures and projections (API.md ``/networth``).

* ``GET /networth?range=&granularity=``: history, one point per day, week or
  month (``range`` ``1m`` | ``3m`` | ``6m`` | ``ytd`` | ``1y`` | ``3y`` |
  ``5y`` | ``max``, default ``1y``).
* ``GET /networth/latest``: today's figures with their breakdown by class,
  account type and account.
* ``GET /networth/projection?months=&basis_months=``: linear trend and
  savings-rate projections.

Any household role (token scope ``networth:read``). Owners, admins and
members/viewers who see every account get household figures (stored
snapshots); anyone else figures recomputed over the accounts they may see.
Rules in ``app.domain.networth.service``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.domain.investments.schemas import Range
from app.domain.networth import service
from app.domain.networth.schemas import (
    DEFAULT_BASIS_MONTHS,
    DEFAULT_PROJECTION_MONTHS,
    MAX_BASIS_MONTHS,
    MAX_PROJECTION_MONTHS,
    Granularity,
    NetWorthHistory,
    NetWorthLatest,
    NetWorthProjection,
)

router = APIRouter(tags=["networth"])

Reader = Annotated[Principal, Depends(scoped_role("networth:read", HouseholdRole.VIEWER))]


@router.get(
    "/networth",
    response_model=NetWorthHistory,
    responses=PROBLEMS,
    summary="Net worth history",
)
def get_networth(
    db: DbSession,
    principal: Reader,
    range: Annotated[Range, Query()] = "1y",
    granularity: Annotated[Granularity | None, Query()] = None,
) -> NetWorthHistory:
    return service.history(db, principal, range_=range, granularity=granularity)


@router.get(
    "/networth/latest",
    response_model=NetWorthLatest,
    responses=PROBLEMS,
    summary="Today's net worth and its breakdown",
)
def get_latest(db: DbSession, principal: Reader) -> NetWorthLatest:
    return service.latest(db, principal)


@router.get(
    "/networth/projection",
    response_model=NetWorthProjection,
    responses=PROBLEMS,
    summary="Projected net worth (linear trend and savings rate)",
)
def get_projection(
    db: DbSession,
    principal: Reader,
    months: Annotated[int, Query(ge=1, le=MAX_PROJECTION_MONTHS)] = DEFAULT_PROJECTION_MONTHS,
    basis_months: Annotated[int, Query(ge=1, le=MAX_BASIS_MONTHS)] = DEFAULT_BASIS_MONTHS,
) -> NetWorthProjection:
    return service.projection(db, principal, months=months, basis_months=basis_months)
