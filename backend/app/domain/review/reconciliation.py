# SPDX-License-Identifier: AGPL-3.0-or-later
"""A statement checked against the ledger rows its document produced.

``GET /documents/{id}/reconciliation`` and its four actions (confirm,
accept, restore, remove-earlier; ``app.api.routers.reconciliation``). The
shapes are in ``app.domain.review.schemas`` (:class:`DocumentReconciliation`,
:class:`ReconcileOutcome`).

Declared only: every call first settles who may ask (a hidden document is
``404``, a read-only one ``403`` for the actions) and then answers ``409
statement-unavailable``. Nothing is read from or written to the ledger here
yet.
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.domain.documents.manage import get_editable_document
from app.domain.documents.service import get_visible_document
from app.domain.review.schemas import (
    DocumentReconciliation,
    ReconcileAccept,
    ReconcileConfirm,
    ReconcileOutcome,
    ReconcileRemoveEarlier,
    ReconcileRestore,
)


def statement_unavailable() -> ProblemError:
    return ProblemError(
        409,
        "statement-unavailable",
        "No statement to check",
        "The document has no statement that could be checked against the ledger.",
    )


def get_reconciliation(
    db: Session, principal: Principal, document_id: uuid.UUID
) -> DocumentReconciliation:
    get_visible_document(db, principal, document_id)
    raise statement_unavailable()


def confirm(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileConfirm,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    get_editable_document(db, principal, document_id)
    raise statement_unavailable()


def accept(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileAccept,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    get_editable_document(db, principal, document_id)
    raise statement_unavailable()


def restore(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileRestore,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    get_editable_document(db, principal, document_id)
    raise statement_unavailable()


def remove_earlier(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: ReconcileRemoveEarlier,
    *,
    request: HTTPConnection | None,
) -> ReconcileOutcome:
    get_editable_document(db, principal, document_id)
    raise statement_unavailable()
