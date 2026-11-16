# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reconcile view of a document (rules in ``app.domain.review.reconciliation``).

* ``GET /documents/{id}/reconciliation``: each statement of the document on
  an account the caller can see, with its balances as printed, the ledger
  rows the document produced, their sum, the difference and its likely
  causes (any role that can see the document).
* ``POST /documents/{id}/reconciliation/confirm``: the statement balances
  (balances may be typed as printed): its rows are confirmed and the
  balance is recorded (``409 statement-does-not-reconcile`` otherwise).
* ``POST /documents/{id}/reconciliation/accept``: accept the statement as
  read without balancing it; no balance is recorded (``409
  statement-balanced`` for one that balances: confirm it instead).
* ``POST /documents/{id}/reconciliation/restore``: put a removed row back
  (``404 tombstone-not-found``).
* ``POST /documents/{id}/reconciliation/remove-earlier``: remove the rows an
  earlier reading of the statement left behind.

The four actions need member or above who can change the document and
honour ``Idempotency-Key``; the statement's account must be one the caller
may edit (``404 account-not-found`` otherwise). A document with no statement
to check is ``409 statement-unavailable``; a statement index the document
does not have is ``422 statement-not-found``. Tokens need ``documents:read`` /
``documents:write``.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Request

from app.api.routers.accounts import PROBLEMS
from app.api.routers.documents import DocumentWriter, Reader
from app.auth.deps import DbSession
from app.core.idempotency import IdempotencyKeyHeader, idempotent, payload_of
from app.domain.review import reconciliation
from app.domain.review.schemas import (
    DocumentReconciliation,
    ReconcileAccept,
    ReconcileConfirm,
    ReconcileOutcome,
    ReconcileRemoveEarlier,
    ReconcileRestore,
)

router = APIRouter(tags=["documents"])


@router.get(
    "/documents/{document_id}/reconciliation",
    response_model=DocumentReconciliation,
    responses=PROBLEMS,
    summary="A document's statements against the ledger rows it produced",
)
def get_reconciliation(
    document_id: uuid.UUID, db: DbSession, principal: Reader
) -> DocumentReconciliation:
    return reconciliation.get_reconciliation(db, principal, document_id)


@router.post(
    "/documents/{document_id}/reconciliation/confirm",
    response_model=ReconcileOutcome,
    responses=PROBLEMS,
    summary="Confirm a statement that balances",
)
def post_confirm(
    document_id: uuid.UUID,
    body: ReconcileConfirm,
    request: Request,
    db: DbSession,
    principal: DocumentWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    def run() -> ReconcileOutcome:
        return reconciliation.confirm(db, principal, document_id, body, request=request)

    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /documents/{document_id}/reconciliation/confirm",
        payload=payload_of(body, document_id=document_id),
        run=run,
        status_code=200,
    )


@router.post(
    "/documents/{document_id}/reconciliation/accept",
    response_model=ReconcileOutcome,
    responses=PROBLEMS,
    summary="Accept a statement as read, without balancing it",
)
def post_accept(
    document_id: uuid.UUID,
    body: ReconcileAccept,
    request: Request,
    db: DbSession,
    principal: DocumentWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    def run() -> ReconcileOutcome:
        return reconciliation.accept(db, principal, document_id, body, request=request)

    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /documents/{document_id}/reconciliation/accept",
        payload=payload_of(body, document_id=document_id),
        run=run,
        status_code=200,
    )


@router.post(
    "/documents/{document_id}/reconciliation/restore",
    response_model=ReconcileOutcome,
    responses=PROBLEMS,
    summary="Put a removed row of the document back",
)
def post_restore(
    document_id: uuid.UUID,
    body: ReconcileRestore,
    request: Request,
    db: DbSession,
    principal: DocumentWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    def run() -> ReconcileOutcome:
        return reconciliation.restore(db, principal, document_id, body, request=request)

    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /documents/{document_id}/reconciliation/restore",
        payload=payload_of(body, document_id=document_id),
        run=run,
        status_code=200,
    )


@router.post(
    "/documents/{document_id}/reconciliation/remove-earlier",
    response_model=ReconcileOutcome,
    responses=PROBLEMS,
    summary="Remove the rows an earlier reading left behind",
)
def post_remove_earlier(
    document_id: uuid.UUID,
    body: ReconcileRemoveEarlier,
    request: Request,
    db: DbSession,
    principal: DocumentWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    def run() -> ReconcileOutcome:
        return reconciliation.remove_earlier(db, principal, document_id, body, request=request)

    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /documents/{document_id}/reconciliation/remove-earlier",
        payload=payload_of(body, document_id=document_id),
        run=run,
        status_code=200,
    )
