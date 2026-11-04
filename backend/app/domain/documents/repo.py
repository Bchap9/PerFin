# SPDX-License-Identifier: AGPL-3.0-or-later
"""Queries for documents and blobs (always filtered by household as well as RLS)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core import clock
from app.domain.documents.models import (
    Blob,
    Document,
    DocumentStatus,
    ReviewItem,
    ReviewKind,
)


def get_document(
    db: Session, household_id: uuid.UUID, document_id: uuid.UUID, *, lock: bool = False
) -> Document | None:
    query = select(Document).where(
        Document.household_id == household_id, Document.id == document_id
    )
    if lock:
        query = query.with_for_update()
    return db.execute(query).scalar_one_or_none()


def get_blob(db: Session, household_id: uuid.UUID, blob_id: uuid.UUID) -> Blob | None:
    return db.execute(
        select(Blob).where(Blob.household_id == household_id, Blob.id == blob_id)
    ).scalar_one_or_none()


def find_duplicate(
    db: Session, household_id: uuid.UUID, sha256: str, *, exclude: uuid.UUID | None = None
) -> uuid.UUID | None:
    """The oldest live document of the household with the same content hash."""
    query = (
        select(Document.id)
        .where(
            Document.household_id == household_id,
            Document.sha256 == sha256,
            Document.status.not_in((DocumentStatus.REJECTED, DocumentStatus.FAILED)),
        )
        .order_by(Document.created_at, Document.id)
        .limit(1)
    )
    if exclude is not None:
        query = query.where(Document.id != exclude)
    return db.execute(query).scalar_one_or_none()


def add_review_item(
    db: Session,
    household_id: uuid.UUID,
    kind: ReviewKind,
    payload: dict[str, Any],
    *,
    document_id: uuid.UUID | None = None,
) -> ReviewItem:
    item = ReviewItem(
        household_id=household_id, document_id=document_id, kind=kind, payload=payload
    )
    db.add(item)
    db.flush()
    return item


def shred_blob_key(db: Session, household_id: uuid.UUID, blob_id: uuid.UUID) -> None:
    """Destroy the wrapped data key of a blob (crypto-shredding, SECURITY.md G5)."""
    db.execute(
        update(Blob)
        .where(Blob.household_id == household_id, Blob.id == blob_id)
        .values(wrapped_dek=None, wrap_nonce=None, shredded_at=clock.utcnow())
        .execution_options(synchronize_session="fetch")
    )
