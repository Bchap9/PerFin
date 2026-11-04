# SPDX-License-Identifier: AGPL-3.0-or-later
"""Stored documents and review items for API tests (synthetic content only).

``make_document(db, store, keyring, household_id, ...)`` encrypts ``data``
into ``store`` exactly as an upload would (``EncryptingWriter``) and writes
the ``blob`` and ``document`` rows directly, without enqueuing an
``ingest_document`` job (so no test's worker picks it up). By default the
document is ``processed`` with its blob in the **permanent** area (it can be
downloaded); ``area=BlobArea.QUARANTINE`` keeps it in quarantine.
``make_review_item(db, document, kind, payload)`` adds an open item. Both
flush only and run in the household's RLS context.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any

from sqlalchemy.orm import Session

from app.core.crypto import Keyring
from app.core.ids import uuid7
from app.domain.documents.blobstore import BlobStore, EncryptingWriter
from app.domain.documents.models import (
    Blob,
    BlobArea,
    Document,
    DocumentKind,
    DocumentSource,
    DocumentStatus,
    ReviewItem,
    ReviewKind,
)
from tests.fixtures.ledger import household_context

#: A small synthetic CSV statement (no real names or numbers).
SAMPLE_CSV = b"Date,Payee,Amount\n2026-01-02,Synthetic Grocer,-12.34\n2026-01-03,Pay,100.00\n"


def make_document(
    db: Session,
    store: BlobStore,
    keyring: Keyring,
    household_id: uuid.UUID,
    *,
    data: bytes = SAMPLE_CSV,
    mime: str = "text/csv",
    filename: str | None = "statement.csv",
    account_id: uuid.UUID | None = None,
    uploaded_by: uuid.UUID | None = None,
    status: DocumentStatus = DocumentStatus.PROCESSED,
    area: BlobArea = BlobArea.PERMANENT,
    kind: DocumentKind = DocumentKind.STATEMENT,
    sha256: str | None = None,
    extraction: dict[str, Any] | None = None,
) -> Document:
    with EncryptingWriter(store, keyring, uuid7(), area=area) as writer:
        writer.write(data)
        sealed = writer.finish()
    with household_context(db, household_id):
        blob = Blob(
            id=sealed.blob_id,
            household_id=household_id,
            store=sealed.store,
            key=sealed.key,
            area=area,
            size=sealed.size,
            stored_size=sealed.stored_size,
            enc_alg=sealed.enc_alg,
            key_version=sealed.wrapped.key_version,
            wrap_nonce=sealed.wrapped.wrap_nonce,
            wrapped_dek=sealed.wrapped.wrapped_dek,
            nonce=sealed.header,
        )
        db.add(blob)
        db.flush()
        document = Document(
            household_id=household_id,
            blob_id=blob.id,
            kind=kind,
            status=status,
            source=DocumentSource.UPLOAD,
            source_meta={},
            sha256=sha256 or hashlib.sha256(data).hexdigest(),
            size=len(data),
            mime_detected=mime,
            original_filename=filename,
            uploaded_by=uploaded_by,
            account_id=account_id,
            extraction=extraction,
        )
        db.add(document)
        db.flush()
    return document


def make_review_item(
    db: Session,
    document: Document,
    kind: ReviewKind = ReviewKind.UNPARSED,
    payload: dict[str, Any] | None = None,
) -> ReviewItem:
    with household_context(db, document.household_id):
        item = ReviewItem(
            household_id=document.household_id,
            document_id=document.id,
            kind=kind,
            payload=dict(payload if payload is not None else {"reason": "import_pending"}),
        )
        db.add(item)
        db.flush()
    return item
