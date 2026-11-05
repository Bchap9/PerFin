# SPDX-License-Identifier: AGPL-3.0-or-later
"""Queries for the review queue and hash aliases (household filter plus RLS)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.domain.documents.models import ReviewItem, ReviewItemState, ReviewKind
from app.domain.review.models import ImportHashAlias
from app.domain.transactions.models import Transaction


def get_item(
    db: Session,
    household_id: uuid.UUID,
    item_id: uuid.UUID,
    visible: ColumnElement[bool],
    *,
    lock: bool = False,
) -> ReviewItem | None:
    query = select(ReviewItem).where(
        ReviewItem.household_id == household_id, ReviewItem.id == item_id, visible
    )
    if lock:
        query = query.with_for_update()
    return db.execute(query).scalar_one_or_none()


def list_items(
    db: Session,
    household_id: uuid.UUID,
    visible: ColumnElement[bool],
    *,
    state: ReviewItemState | None,
    kind: ReviewKind | None,
    document_id: uuid.UUID | None,
    cursor: uuid.UUID | None,
    limit: int,
) -> list[ReviewItem]:
    query = select(ReviewItem).where(ReviewItem.household_id == household_id, visible)
    if state is not None:
        query = query.where(ReviewItem.state == state)
    if kind is not None:
        query = query.where(ReviewItem.kind == kind)
    if document_id is not None:
        query = query.where(ReviewItem.document_id == document_id)
    if cursor is not None:
        query = query.where(ReviewItem.id > cursor)
    return list(db.execute(query.order_by(ReviewItem.id).limit(limit)).scalars())


def open_items_of(db: Session, household_id: uuid.UUID, document_id: uuid.UUID) -> int:
    return int(
        db.execute(
            select(func.count())
            .select_from(ReviewItem)
            .where(
                ReviewItem.household_id == household_id,
                ReviewItem.document_id == document_id,
                ReviewItem.state == ReviewItemState.OPEN,
            )
        ).scalar_one()
    )


def add_alias(
    db: Session,
    household_id: uuid.UUID,
    account_id: uuid.UUID,
    transaction_id: uuid.UUID,
    import_hash: str,
) -> None:
    """Record ``import_hash`` as another hash of ``transaction_id`` (idempotent)."""
    db.execute(
        insert(ImportHashAlias)
        .values(
            household_id=household_id,
            account_id=account_id,
            transaction_id=transaction_id,
            import_hash=import_hash,
        )
        .on_conflict_do_nothing(constraint="uq_import_hash_alias_hash")
    )


def alias_rows(
    db: Session,
    household_id: uuid.UUID,
    account_id: uuid.UUID,
    first: date,
    last: date,
) -> Sequence[tuple[uuid.UUID, date, Decimal, str]]:
    """``(transaction id, posted date, amount, alias hash)`` of the account in a date range.

    The deduplicator adds these to the account's own ``import_hash`` values,
    so a row confirmed as a duplicate matches its kept transaction exactly.
    """
    rows = db.execute(
        select(
            Transaction.id,
            Transaction.posted_date,
            Transaction.amount,
            ImportHashAlias.import_hash,
        )
        .join(
            ImportHashAlias,
            (ImportHashAlias.household_id == Transaction.household_id)
            & (ImportHashAlias.transaction_id == Transaction.id),
        )
        .where(
            Transaction.household_id == household_id,
            Transaction.account_id == account_id,
            ImportHashAlias.account_id == account_id,
            Transaction.posted_date >= first,
            Transaction.posted_date <= last,
        )
        .order_by(Transaction.posted_date, Transaction.id)
    )
    return [(i, d, a, h) for i, d, a, h in rows]
