# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transactions (API.md "Transactions").

* ``GET /transactions``: filters ``account``, ``date_from``/``date_to``,
  ``category``, ``uncategorized``, ``payee``, ``tag``, ``review_state``,
  ``status``, ``transfer`` and ``q`` (a case-insensitive substring of the
  payee text, payee name, memo or notes; no query language); keyset cursor
  pagination (``cursor``, ``limit`` at most 500), newest first.
* ``POST /transactions``: a manual transaction (``Idempotency-Key`` honoured).
* ``GET/PATCH/DELETE /transactions/{id}``, ``POST /transactions/{id}/split``.
* ``POST /transactions/bulk``: categorise / tag / untag / delete / confirm
  many; every id is authorised on its own.
* Transfers: ``GET /transactions/{id}/transfer-candidates``, ``POST`` and
  ``DELETE /transactions/{id}/transfer`` (link / unlink by hand) and
  ``POST /transactions/transfers/auto-pair`` (run the matcher over the
  caller's editable accounts).

Guards: reading needs any household role and the ``transactions:read``
token scope; writing needs at least ``member`` and ``transactions:write``.
Account visibility (grants) is applied by the service: a transaction the
caller cannot see (or edit, for writes) is ``404 transaction-not-found``,
loaded by a dependency so the answer comes before body validation.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.auth.authz import forbidden, require_scope, role_at_least
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.idempotency import IdempotencyKeyHeader, idempotent
from app.domain.transactions import repo, service, transfers
from app.domain.transactions.models import ReviewState, Transaction, TransactionStatus
from app.domain.transactions.schemas import (
    DEFAULT_LIMIT,
    FILTER_VALUES_MAX,
    MAX_LIMIT,
    Q_MAX,
    TRANSFER_WINDOW_DEFAULT,
    TRANSFER_WINDOW_MAX,
    AutoPairRequest,
    AutoPairResult,
    BulkAction,
    BulkResult,
    SplitSet,
    TransactionCreate,
    TransactionOut,
    TransactionPage,
    TransactionUpdate,
    TransferLink,
    TransferPair,
)

router = APIRouter(tags=["transactions"])

_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: {"description": "Problem details", "content": {"application/problem+json": {}}}
    for code in (403, 404, 409, 422)
}

SCOPE_READ = "transactions:read"
SCOPE_WRITE = "transactions:write"
_ReadScope = Annotated[Principal, Depends(require_scope(SCOPE_READ))]
_WriteScope = Annotated[Principal, Depends(require_scope(SCOPE_WRITE))]


def _in_household(principal: Principal, minimum: HouseholdRole) -> Principal:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal


def require_reader(principal: _ReadScope) -> Principal:
    """Any household role, ``transactions:read`` for tokens."""
    return _in_household(principal, HouseholdRole.VIEWER)


def require_member_reader(principal: _ReadScope) -> Principal:
    """Member or above, ``transactions:read`` for tokens (link suggestions)."""
    return _in_household(principal, HouseholdRole.MEMBER)


def require_writer(principal: _WriteScope) -> Principal:
    """Member or above, ``transactions:write`` for tokens."""
    return _in_household(principal, HouseholdRole.MEMBER)


Reader = Annotated[Principal, Depends(require_reader)]
MemberReader = Annotated[Principal, Depends(require_member_reader)]
Writer = Annotated[Principal, Depends(require_writer)]


def readable_transaction(
    transaction_id: uuid.UUID, db: DbSession, principal: Reader
) -> Transaction:
    return service.get_visible_transaction(db, principal, transaction_id)


def suggestable_transaction(
    transaction_id: uuid.UUID, db: DbSession, principal: MemberReader
) -> Transaction:
    return service.get_visible_transaction(db, principal, transaction_id, need="edit")


def editable_transaction(
    transaction_id: uuid.UUID, db: DbSession, principal: Writer
) -> Transaction:
    return service.get_visible_transaction(db, principal, transaction_id, need="edit", lock=True)


Readable = Annotated[Transaction, Depends(readable_transaction)]
Suggestable = Annotated[Transaction, Depends(suggestable_transaction)]
Editable = Annotated[Transaction, Depends(editable_transaction)]

_Ids = Query(max_length=FILTER_VALUES_MAX)


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


@router.get(
    "/transactions",
    response_model=TransactionPage,
    responses=_PROBLEMS,
    summary="List transactions (filters, cursor pagination)",
)
def list_transactions(
    db: DbSession,
    principal: Reader,
    account: Annotated[list[uuid.UUID] | None, _Ids] = None,
    date_from: date | None = None,
    date_to: date | None = None,
    category: Annotated[list[uuid.UUID] | None, _Ids] = None,
    uncategorized: bool | None = None,
    payee: Annotated[list[uuid.UUID] | None, _Ids] = None,
    tag: Annotated[list[uuid.UUID] | None, _Ids] = None,
    review_state: Annotated[list[ReviewState] | None, _Ids] = None,
    status_: Annotated[list[TransactionStatus] | None, Query(alias="status", max_length=3)] = None,
    transfer: bool | None = None,
    q: Annotated[str | None, Query(min_length=1, max_length=Q_MAX)] = None,
    cursor: Annotated[str | None, Query(max_length=128)] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
) -> TransactionPage:
    filters = repo.TransactionFilters(
        account_ids=account or (),
        date_from=date_from,
        date_to=date_to,
        category_ids=category or (),
        uncategorized=uncategorized,
        payee_ids=payee or (),
        tag_ids=tag or (),
        review_states=review_state or (),
        statuses=status_ or (),
        q=q.strip() or None if q is not None else None,
        transfer=transfer,
    )
    return service.list_transactions(db, principal, filters, cursor=cursor, limit=limit)


@router.post(
    "/transactions",
    response_model=TransactionOut,
    status_code=status.HTTP_201_CREATED,
    responses=_PROBLEMS,
    summary="Create a manual transaction",
)
def post_transaction(
    body: TransactionCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    def run() -> TransactionOut:
        row = service.create_transaction(db, principal, body, request=request)
        return service.one_out(db, principal, row)

    return idempotent(
        db, principal, idempotency_key, scope="POST /transactions", payload=body, run=run
    )


@router.post(
    "/transactions/bulk",
    response_model=BulkResult,
    responses=_PROBLEMS,
    summary="Categorise, tag, untag, delete or confirm many transactions",
)
def post_bulk(body: BulkAction, request: Request, db: DbSession, principal: Writer) -> BulkResult:
    return service.bulk(db, principal, body, request=request)


@router.post(
    "/transactions/transfers/auto-pair",
    response_model=AutoPairResult,
    responses=_PROBLEMS,
    summary="Pair unlinked transfers among the accounts I may edit",
)
def post_auto_pair(
    body: AutoPairRequest, request: Request, db: DbSession, principal: Writer
) -> AutoPairResult:
    household_id = principal.household_id
    if household_id is None:  # pragma: no cover - require_writer guarantees it
        raise forbidden()
    outcome = transfers.auto_pair(
        db,
        household_id,
        principal=principal,
        account_ids=body.account_ids,
        date_from=body.date_from,
        date_to=body.date_to,
        window_days=body.window_days,
        request=request,
    )
    return AutoPairResult(
        paired=[
            TransferPair(group_id=p.group_id, transaction_ids=[p.outflow_id, p.inflow_id])
            for p in outcome.pairs
        ],
        ambiguous=outcome.ambiguous,
    )


# ---------------------------------------------------------------------------
# One transaction
# ---------------------------------------------------------------------------


@router.get(
    "/transactions/{transaction_id}",
    response_model=TransactionOut,
    responses=_PROBLEMS,
    summary="One transaction",
)
def get_transaction(row: Readable, db: DbSession, principal: Reader) -> TransactionOut:
    return service.one_out(db, principal, row)


@router.patch(
    "/transactions/{transaction_id}",
    response_model=TransactionOut,
    responses=_PROBLEMS,
    summary="Change a transaction",
)
def patch_transaction(
    row: Editable, body: TransactionUpdate, request: Request, db: DbSession, principal: Writer
) -> TransactionOut:
    service.update_transaction(db, principal, row, body, request=request)
    return service.one_out(db, principal, row)


@router.delete(
    "/transactions/{transaction_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_PROBLEMS,
    summary="Delete a transaction (the audit trail keeps it)",
)
def delete_transaction(
    row: Editable, request: Request, db: DbSession, principal: Writer
) -> Response:
    service.delete_transaction(db, principal, row, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/transactions/{transaction_id}/split",
    response_model=TransactionOut,
    responses=_PROBLEMS,
    summary="Replace a transaction's splits (they must add up to its amount)",
)
def post_split(
    row: Editable, body: SplitSet, request: Request, db: DbSession, principal: Writer
) -> TransactionOut:
    service.set_splits(db, principal, row, body, request=request)
    return service.one_out(db, principal, row)


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------


@router.get(
    "/transactions/{transaction_id}/transfer-candidates",
    response_model=list[TransactionOut],
    responses=_PROBLEMS,
    summary="Transactions that could be the other leg of this transfer",
)
def get_transfer_candidates(
    row: Suggestable,
    db: DbSession,
    principal: MemberReader,
    window_days: Annotated[int, Query(ge=0, le=TRANSFER_WINDOW_MAX)] = TRANSFER_WINDOW_DEFAULT,
) -> list[TransactionOut]:
    return service.transfer_candidates(db, principal, row, window_days=window_days)


@router.post(
    "/transactions/{transaction_id}/transfer",
    response_model=TransactionOut,
    responses=_PROBLEMS,
    summary="Link this transaction and another as one transfer",
)
def post_transfer(
    row: Editable, body: TransferLink, request: Request, db: DbSession, principal: Writer
) -> TransactionOut:
    transfers.link(db, principal, row, body.counterpart_id, request=request)
    return service.one_out(db, principal, row)


@router.delete(
    "/transactions/{transaction_id}/transfer",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_PROBLEMS,
    summary="Unlink the transfer this transaction belongs to",
)
def delete_transfer(row: Editable, request: Request, db: DbSession, principal: Writer) -> Response:
    transfers.unlink(db, principal, row, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
