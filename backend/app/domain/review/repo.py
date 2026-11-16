# SPDX-License-Identifier: AGPL-3.0-or-later
"""Queries for the review queue and hash aliases (household filter plus RLS)."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from sqlalchemy import ColumnElement, case, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.domain.documents.models import Document, ReviewItem, ReviewItemState, ReviewKind
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


#: Reasons of items that only tell a person something: the document they
#: are about was imported whole and nothing waits on an answer
#: (``investment_figures_differ``: two figures the statement itself prints
#: disagree, no row is to blame), or was left alone on purpose
#: (``same_content_skipped``: a byte-identical copy of a file already here).
#: Such an item stays in the review queue until it is seen and never keeps
#: its document in ``needs_review``.
NOTICE_REASONS: Final = frozenset({"investment_figures_differ", "same_content_skipped"})


def open_documents(
    db: Session,
    household_id: uuid.UUID,
    visible: ColumnElement[bool],
    *,
    cursor: uuid.UUID | None,
    limit: int,
) -> list[uuid.UUID]:
    """Ids of documents with an open item the caller may see, by id, after ``cursor``."""
    query = (
        select(ReviewItem.document_id)
        .where(
            ReviewItem.household_id == household_id,
            visible,
            ReviewItem.state == ReviewItemState.OPEN,
            ReviewItem.document_id.is_not(None),
        )
        .distinct()
    )
    if cursor is not None:
        query = query.where(ReviewItem.document_id > cursor)
    rows = db.execute(query.order_by(ReviewItem.document_id).limit(limit)).scalars()
    return [document_id for document_id in rows if document_id is not None]


def open_items(
    db: Session,
    household_id: uuid.UUID,
    visible: ColumnElement[bool],
    document_ids: Collection[uuid.UUID],
) -> list[ReviewItem]:
    """The open items of ``document_ids`` the caller may see, oldest first."""
    if not document_ids:
        return []
    return list(
        db.execute(
            select(ReviewItem)
            .where(
                ReviewItem.household_id == household_id,
                visible,
                ReviewItem.state == ReviewItemState.OPEN,
                ReviewItem.document_id.in_(sorted(document_ids)),
            )
            .order_by(ReviewItem.id)
        ).scalars()
    )


def loose_items(
    db: Session, household_id: uuid.UUID, visible: ColumnElement[bool], *, limit: int
) -> list[ReviewItem]:
    """The open items of no document the caller may see, oldest first."""
    return list(
        db.execute(
            select(ReviewItem)
            .where(
                ReviewItem.household_id == household_id,
                visible,
                ReviewItem.state == ReviewItemState.OPEN,
                ReviewItem.document_id.is_(None),
            )
            .order_by(ReviewItem.id)
            .limit(limit)
        ).scalars()
    )


def documents_with_statements(
    db: Session, household_id: uuid.UUID, document_ids: Collection[uuid.UUID]
) -> set[uuid.UUID]:
    """Those of ``document_ids`` whose stored extraction holds a bank statement.

    Read in the database, so no extraction is loaded to answer it. The
    ``CASE`` keeps ``jsonb_array_length`` away from anything but an array.
    """
    if not document_ids:
        return set()
    statements = Document.extraction["statements"]
    count = case(
        (func.jsonb_typeof(statements) == "array", func.jsonb_array_length(statements)),
        else_=0,
    )
    return set(
        db.execute(
            select(Document.id).where(
                Document.household_id == household_id,
                Document.id.in_(sorted(document_ids)),
                count > 0,
            )
        ).scalars()
    )


def open_items_of(db: Session, household_id: uuid.UUID, document_id: uuid.UUID) -> int:
    """The document's open items that wait on a person (notices are not counted)."""
    return int(
        db.execute(
            select(func.count())
            .select_from(ReviewItem)
            .where(
                ReviewItem.household_id == household_id,
                ReviewItem.document_id == document_id,
                ReviewItem.state == ReviewItemState.OPEN,
                func.coalesce(ReviewItem.payload["reason"].astext, "").notin_(NOTICE_REASONS),
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


def alias_transactions(
    db: Session, household_id: uuid.UUID, account_id: uuid.UUID, hashes: Collection[str]
) -> list[tuple[uuid.UUID, str]]:
    """``(transaction id, alias hash)`` for those of ``hashes`` that are aliases on the account.

    The reconcile view finds with it the ledger row a statement's row was
    confirmed to be a duplicate of, whatever date that row now has.
    """
    if not hashes:
        return []
    rows = db.execute(
        select(ImportHashAlias.transaction_id, ImportHashAlias.import_hash)
        .where(
            ImportHashAlias.household_id == household_id,
            ImportHashAlias.account_id == account_id,
            ImportHashAlias.import_hash.in_(sorted(set(hashes))),
        )
        .order_by(ImportHashAlias.import_hash)
    )
    return [(transaction_id, str(value)) for transaction_id, value in rows]
