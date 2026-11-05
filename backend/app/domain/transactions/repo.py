# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transaction queries. Every query filters ``household_id`` explicitly and
takes the caller's account-visibility predicate
(``app.domain.accounts.visibility.visible_account_filter``); RLS is the
backstop (SECURITY.md F2/F3). Bound parameters only: the ``q`` search is an
``ILIKE`` with ``%``, ``_`` and ``\\`` escaped (:func:`like_pattern`), never a
query language.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Final

from sqlalchemy import ColumnElement, and_, delete, exists, or_, select, tuple_
from sqlalchemy.orm import Session

from app.domain.transactions.models import (
    Category,
    Payee,
    ReviewState,
    Tag,
    Transaction,
    TransactionSplit,
    TransactionStatus,
    TransactionTag,
)

LIKE_ESCAPE: Final = "\\"


def like_pattern(text: str) -> str:
    """``%text%`` with the LIKE wildcards and the escape character escaped."""
    escaped = (
        text.replace(LIKE_ESCAPE, LIKE_ESCAPE * 2)
        .replace("%", f"{LIKE_ESCAPE}%")
        .replace("_", f"{LIKE_ESCAPE}_")
    )
    return f"%{escaped}%"


@dataclass(frozen=True)
class TransactionFilters:
    """``GET /transactions`` filters; empty collections mean "no filter"."""

    account_ids: Collection[uuid.UUID] = ()
    date_from: date | None = None
    date_to: date | None = None
    category_ids: Collection[uuid.UUID] = ()
    uncategorized: bool | None = None
    payee_ids: Collection[uuid.UUID] = ()
    tag_ids: Collection[uuid.UUID] = ()
    review_states: Collection[ReviewState] = ()
    statuses: Collection[TransactionStatus] = ()
    q: str | None = None
    #: ``False``: only transactions that are not part of a transfer; ``True``: only transfers.
    transfer: bool | None = None
    extra: Sequence[ColumnElement[bool]] = field(default_factory=tuple)


def _has_split() -> ColumnElement[bool]:
    return exists().where(
        TransactionSplit.household_id == Transaction.household_id,
        TransactionSplit.transaction_id == Transaction.id,
    )


def filter_clauses(household_id: uuid.UUID, f: TransactionFilters) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = [Transaction.household_id == household_id]
    if f.account_ids:
        clauses.append(Transaction.account_id.in_(list(f.account_ids)))
    if f.date_from is not None:
        clauses.append(Transaction.posted_date >= f.date_from)
    if f.date_to is not None:
        clauses.append(Transaction.posted_date <= f.date_to)
    if f.category_ids:
        wanted = list(f.category_ids)
        clauses.append(
            or_(
                Transaction.category_id.in_(wanted),
                exists().where(
                    TransactionSplit.household_id == Transaction.household_id,
                    TransactionSplit.transaction_id == Transaction.id,
                    TransactionSplit.category_id.in_(wanted),
                ),
            )
        )
    if f.uncategorized is True:
        clauses.append(and_(Transaction.category_id.is_(None), ~_has_split()))
    elif f.uncategorized is False:
        clauses.append(or_(Transaction.category_id.is_not(None), _has_split()))
    if f.payee_ids:
        clauses.append(Transaction.payee_id.in_(list(f.payee_ids)))
    if f.tag_ids:
        clauses.append(
            exists().where(
                TransactionTag.household_id == Transaction.household_id,
                TransactionTag.transaction_id == Transaction.id,
                TransactionTag.tag_id.in_(list(f.tag_ids)),
            )
        )
    if f.review_states:
        clauses.append(Transaction.review_state.in_(list(f.review_states)))
    if f.statuses:
        clauses.append(Transaction.status.in_(list(f.statuses)))
    if f.transfer is True:
        clauses.append(Transaction.transfer_group_id.is_not(None))
    elif f.transfer is False:
        clauses.append(Transaction.transfer_group_id.is_(None))
    if f.q:
        pattern = like_pattern(f.q)
        clauses.append(
            or_(
                Transaction.raw_payee.ilike(pattern, escape=LIKE_ESCAPE),
                Transaction.memo.ilike(pattern, escape=LIKE_ESCAPE),
                Transaction.notes.ilike(pattern, escape=LIKE_ESCAPE),
                exists().where(
                    Payee.household_id == Transaction.household_id,
                    Payee.id == Transaction.payee_id,
                    Payee.name.ilike(pattern, escape=LIKE_ESCAPE),
                ),
            )
        )
    clauses.extend(f.extra)
    return clauses


def list_page(
    db: Session,
    household_id: uuid.UUID,
    visible: ColumnElement[bool],
    filters: TransactionFilters,
    *,
    after: tuple[date, uuid.UUID] | None,
    limit: int,
) -> list[Transaction]:
    """Up to ``limit + 1`` rows, newest first, keyset ``(posted_date, id)`` descending."""
    stmt = select(Transaction).where(visible, *filter_clauses(household_id, filters))
    if after is not None:
        stmt = stmt.where(
            tuple_(Transaction.posted_date, Transaction.id) < tuple_(after[0], after[1])
        )
    stmt = stmt.order_by(Transaction.posted_date.desc(), Transaction.id.desc()).limit(limit + 1)
    return list(db.execute(stmt).scalars())


def get(
    db: Session,
    household_id: uuid.UUID,
    transaction_id: uuid.UUID,
    visible: ColumnElement[bool],
    *,
    lock: bool = False,
) -> Transaction | None:
    stmt = select(Transaction).where(
        Transaction.household_id == household_id, Transaction.id == transaction_id, visible
    )
    if lock:
        stmt = stmt.with_for_update(of=Transaction)
    return db.execute(stmt).scalar_one_or_none()


def get_many(
    db: Session,
    household_id: uuid.UUID,
    ids: Collection[uuid.UUID],
    visible: ColumnElement[bool],
    *,
    lock: bool = False,
) -> dict[uuid.UUID, Transaction]:
    if not ids:
        return {}
    stmt = (
        select(Transaction)
        .where(Transaction.household_id == household_id, Transaction.id.in_(list(ids)), visible)
        .order_by(Transaction.id)
    )
    if lock:
        stmt = stmt.with_for_update(of=Transaction)
    return {row.id: row for row in db.execute(stmt).scalars()}


def tag_ids_for(
    db: Session, household_id: uuid.UUID, ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    out: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    if not ids:
        return out
    rows = db.execute(
        select(TransactionTag.transaction_id, TransactionTag.tag_id)
        .where(
            TransactionTag.household_id == household_id,
            TransactionTag.transaction_id.in_(list(ids)),
        )
        .order_by(TransactionTag.transaction_id, TransactionTag.tag_id)
    )
    for transaction_id, tag_id in rows:
        out[transaction_id].append(tag_id)
    return out


def splits_for(
    db: Session, household_id: uuid.UUID, ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[TransactionSplit]]:
    out: dict[uuid.UUID, list[TransactionSplit]] = defaultdict(list)
    if not ids:
        return out
    rows = db.execute(
        select(TransactionSplit)
        .where(
            TransactionSplit.household_id == household_id,
            TransactionSplit.transaction_id.in_(list(ids)),
        )
        .order_by(TransactionSplit.transaction_id, TransactionSplit.id)
    ).scalars()
    for split in rows:
        out[split.transaction_id].append(split)
    return out


def has_splits(db: Session, household_id: uuid.UUID, transaction_id: uuid.UUID) -> bool:
    return (
        db.execute(
            select(TransactionSplit.id)
            .where(
                TransactionSplit.household_id == household_id,
                TransactionSplit.transaction_id == transaction_id,
            )
            .limit(1)
        ).first()
        is not None
    )


def split_transaction_ids(
    db: Session, household_id: uuid.UUID, ids: Collection[uuid.UUID]
) -> set[uuid.UUID]:
    if not ids:
        return set()
    return set(
        db.execute(
            select(TransactionSplit.transaction_id)
            .where(
                TransactionSplit.household_id == household_id,
                TransactionSplit.transaction_id.in_(list(ids)),
            )
            .distinct()
        ).scalars()
    )


def replace_splits(
    db: Session, transaction: Transaction, lines: Iterable[dict[str, Any]]
) -> list[TransactionSplit]:
    db.execute(
        delete(TransactionSplit).where(
            TransactionSplit.household_id == transaction.household_id,
            TransactionSplit.transaction_id == transaction.id,
        )
    )
    created = [
        TransactionSplit(
            household_id=transaction.household_id, transaction_id=transaction.id, **line
        )
        for line in lines
    ]
    db.add_all(created)
    db.flush()
    return created


def set_tags(db: Session, transaction: Transaction, tag_ids: Collection[uuid.UUID]) -> bool:
    """Make the transaction's tag set exactly ``tag_ids``; True when it changed."""
    current = set(tag_ids_for(db, transaction.household_id, [transaction.id])[transaction.id])
    wanted = set(tag_ids)
    if current == wanted:
        return False
    add_tags(db, transaction.household_id, [transaction.id], wanted - current)
    remove_tags(db, transaction.household_id, [transaction.id], current - wanted)
    return True


def add_tags(
    db: Session,
    household_id: uuid.UUID,
    transaction_ids: Collection[uuid.UUID],
    tag_ids: Collection[uuid.UUID],
) -> set[uuid.UUID]:
    """Add every tag to every transaction; return the transactions that gained one."""
    if not transaction_ids or not tag_ids:
        return set()
    existing: set[tuple[uuid.UUID, uuid.UUID]] = {
        (row[0], row[1])
        for row in db.execute(
            select(TransactionTag.transaction_id, TransactionTag.tag_id).where(
                TransactionTag.household_id == household_id,
                TransactionTag.transaction_id.in_(list(transaction_ids)),
                TransactionTag.tag_id.in_(list(tag_ids)),
            )
        )
    }
    changed: set[uuid.UUID] = set()
    for transaction_id in sorted(transaction_ids):
        for tag_id in sorted(tag_ids):
            if (transaction_id, tag_id) not in existing:
                db.add(
                    TransactionTag(
                        household_id=household_id, transaction_id=transaction_id, tag_id=tag_id
                    )
                )
                changed.add(transaction_id)
    db.flush()
    return changed


def remove_tags(
    db: Session,
    household_id: uuid.UUID,
    transaction_ids: Collection[uuid.UUID],
    tag_ids: Collection[uuid.UUID],
) -> set[uuid.UUID]:
    """Remove the tags; return the transactions that lost one."""
    if not transaction_ids or not tag_ids:
        return set()
    removed = db.execute(
        delete(TransactionTag)
        .where(
            TransactionTag.household_id == household_id,
            TransactionTag.transaction_id.in_(list(transaction_ids)),
            TransactionTag.tag_id.in_(list(tag_ids)),
        )
        .returning(TransactionTag.transaction_id)
    ).scalars()
    return set(removed)


def existing_ids(
    db: Session,
    model: type[Category] | type[Payee] | type[Tag],
    household_id: uuid.UUID,
    ids: Collection[uuid.UUID],
) -> set[uuid.UUID]:
    """The subset of ``ids`` that are rows of ``model`` in ``household_id``."""
    if not ids:
        return set()
    return set(
        db.execute(
            select(model.id).where(model.household_id == household_id, model.id.in_(list(ids)))
        ).scalars()
    )


def group_members(
    db: Session, household_id: uuid.UUID, group_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[uuid.UUID, uuid.UUID]]]:
    """``{group_id: [(transaction_id, account_id), ...]}`` (not filtered by visibility)."""
    out: dict[uuid.UUID, list[tuple[uuid.UUID, uuid.UUID]]] = defaultdict(list)
    if not group_ids:
        return out
    rows = db.execute(
        select(Transaction.transfer_group_id, Transaction.id, Transaction.account_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.transfer_group_id.in_(list(group_ids)),
        )
        .order_by(Transaction.id)
    )
    for group_id, transaction_id, account_id in rows:
        if group_id is not None:
            out[group_id].append((transaction_id, account_id))
    return out


def delete_transaction(db: Session, transaction: Transaction) -> None:
    db.execute(
        delete(Transaction).where(
            Transaction.household_id == transaction.household_id,
            Transaction.id == transaction.id,
        )
    )
