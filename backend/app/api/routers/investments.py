# SPDX-License-Identifier: AGPL-3.0-or-later
"""Investment transactions, holdings and performance (API.md ``/investments``).

* ``GET /investments/transactions?account=&security=&type=&from=&to=&cursor=&limit=``,
  ``GET /investments/transactions/{id}``: any role, visible accounts only.
* ``POST /investments/transactions`` (``Idempotency-Key`` honoured),
  ``PATCH`` / ``DELETE /investments/transactions/{id}``: member or above, on
  accounts the caller may edit; lots and holdings are replayed on every
  change.
* ``GET /investments/holdings?account=``: any role; holdings, lots,
  unrealised gains and allocation of visible accounts.
* ``GET /investments/performance?range=&account=``: any role; TWR, MWR,
  realised and unrealised gains of visible accounts.
* ``GET /investments/accounts/{account_id}/cost-method`` (any role) and
  ``PUT`` (member or above, an account the caller may edit): ``fifo`` or
  ``average``; changing it replays the account's lots.

Tokens need ``investments:read`` / ``investments:write``. Rules:
``app.domain.investments.transactions``, ``holdings``, ``performance``.
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
from app.domain.investments import holdings as holdings_service
from app.domain.investments import performance as performance_service
from app.domain.investments import transactions as service
from app.domain.investments.models import InvestmentTxnType
from app.domain.investments.schemas import (
    DEFAULT_PAGE,
    MAX_PAGE,
    CostMethodOut,
    CostMethodSet,
    HoldingsOut,
    InvestmentTxnCreate,
    InvestmentTxnOut,
    InvestmentTxnPage,
    InvestmentTxnUpdate,
    PerformanceOut,
    Range,
)

router = APIRouter(tags=["investments"])

Reader = Annotated[Principal, Depends(scoped_role("investments:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("investments:write", HouseholdRole.MEMBER))]
Cursor = Annotated[uuid.UUID | None, Query()]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]
AccountFilter = Annotated[uuid.UUID | None, Query(alias="account")]


def require_txn_writer(txn_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the transaction must be editable (``404`` before the body is read)."""
    service.require_editable(db, principal, txn_id)
    return principal


def require_account_writer(account_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the account must be editable (``404`` before the body is read)."""
    service.require_editable_account(db, principal, account_id)
    return principal


TxnWriter = Annotated[Principal, Depends(require_txn_writer)]
AccountWriter = Annotated[Principal, Depends(require_account_writer)]


@router.get(
    "/investments/transactions",
    response_model=InvestmentTxnPage,
    responses=PROBLEMS,
    summary="Investment transactions of accounts I can see",
)
def get_txns(
    db: DbSession,
    principal: Reader,
    account: AccountFilter = None,
    security: Annotated[uuid.UUID | None, Query()] = None,
    type: Annotated[InvestmentTxnType | None, Query()] = None,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_PAGE,
) -> InvestmentTxnPage:
    return service.list_txns(
        db,
        principal,
        account_id=account,
        security_id=security,
        type_=type,
        date_from=date_from,
        date_to=date_to,
        cursor=cursor,
        limit=limit,
    )


@router.post(
    "/investments/transactions",
    response_model=InvestmentTxnOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Record an investment transaction (member or above)",
)
def post_txn(
    body: InvestmentTxnCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /investments/transactions",
        payload=payload_of(body),
        run=lambda: service.create_txn(db, principal, body, request=request),
    )


@router.get(
    "/investments/transactions/{txn_id}",
    response_model=InvestmentTxnOut,
    responses=PROBLEMS,
    summary="One investment transaction",
)
def get_txn(txn_id: uuid.UUID, db: DbSession, principal: Reader) -> InvestmentTxnOut:
    return service.get_txn(db, principal, txn_id)


@router.patch(
    "/investments/transactions/{txn_id}",
    response_model=InvestmentTxnOut,
    responses=PROBLEMS,
    summary="Change an investment transaction (member or above)",
)
def patch_txn(
    txn_id: uuid.UUID,
    body: InvestmentTxnUpdate,
    request: Request,
    db: DbSession,
    principal: TxnWriter,
) -> InvestmentTxnOut:
    return service.update_txn(db, principal, txn_id, body, request=request)


@router.delete(
    "/investments/transactions/{txn_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete an investment transaction (member or above)",
)
def delete_txn(txn_id: uuid.UUID, request: Request, db: DbSession, principal: Writer) -> Response:
    service.delete_txn(db, principal, txn_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/investments/holdings",
    response_model=HoldingsOut,
    responses=PROBLEMS,
    summary="Holdings, lots, unrealised gains and allocation",
)
def get_holdings(db: DbSession, principal: Reader, account: AccountFilter = None) -> HoldingsOut:
    return holdings_service.holdings(db, principal, account_id=account)


@router.get(
    "/investments/performance",
    response_model=PerformanceOut,
    responses=PROBLEMS,
    summary="Time- and money-weighted returns and gains",
)
def get_performance(
    db: DbSession,
    principal: Reader,
    range: Annotated[Range, Query()] = "1y",
    account: AccountFilter = None,
) -> PerformanceOut:
    return performance_service.performance(db, principal, range_=range, account_id=account)


@router.get(
    "/investments/accounts/{account_id}/cost-method",
    response_model=CostMethodOut,
    responses=PROBLEMS,
    summary="The account's cost method",
)
def get_cost_method(account_id: uuid.UUID, db: DbSession, principal: Reader) -> CostMethodOut:
    return service.get_cost_method(db, principal, account_id)


@router.put(
    "/investments/accounts/{account_id}/cost-method",
    response_model=CostMethodOut,
    responses=PROBLEMS,
    summary="Set the account's cost method and replay its lots (member or above)",
)
def put_cost_method(
    account_id: uuid.UUID,
    body: CostMethodSet,
    request: Request,
    db: DbSession,
    principal: AccountWriter,
) -> CostMethodOut:
    return service.set_cost_method(db, principal, account_id, body, request=request)
