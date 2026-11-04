# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rule queries and the transaction reads the engine needs.

Every query names ``household_id`` explicitly; RLS is the backstop
(SECURITY.md F2/F3). Bound parameters only.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, func, select, tuple_, update
from sqlalchemy.orm import Session

from app.domain.accounts.models import Account
from app.domain.transactions.models import Category, Payee, Rule, Tag, Transaction


def list_rules(
    db: Session, household_id: uuid.UUID, *, enabled_only: bool = False, lock: bool = False
) -> list[Rule]:
    stmt = select(Rule).where(Rule.household_id == household_id)
    if enabled_only:
        stmt = stmt.where(Rule.enabled.is_(True))
    stmt = stmt.order_by(Rule.sort_order, Rule.id)
    if lock:
        stmt = stmt.with_for_update()
    return list(db.execute(stmt).scalars())


def get_rule(
    db: Session, household_id: uuid.UUID, rule_id: uuid.UUID, *, lock: bool = False
) -> Rule | None:
    stmt = select(Rule).where(Rule.household_id == household_id, Rule.id == rule_id)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def count_rules(db: Session, household_id: uuid.UUID) -> int:
    return int(
        db.execute(
            select(func.count()).select_from(Rule).where(Rule.household_id == household_id)
        ).scalar_one()
    )


def next_sort_order(db: Session, household_id: uuid.UUID) -> int:
    current = db.execute(
        select(func.max(Rule.sort_order)).where(Rule.household_id == household_id)
    ).scalar_one()
    return 0 if current is None else int(current) + 1


def delete_rule(db: Session, rule: Rule) -> None:
    db.execute(delete(Rule).where(Rule.household_id == rule.household_id, Rule.id == rule.id))


def delete_all(db: Session, household_id: uuid.UUID) -> int:
    result = db.execute(delete(Rule).where(Rule.household_id == household_id).returning(Rule.id))
    return len(result.all())


def add_hits(
    db: Session, household_id: uuid.UUID, hits: Mapping[uuid.UUID, int], at: datetime
) -> None:
    """``hit_count += n`` and ``last_hit_at = at`` per rule (in id order: no deadlocks)."""
    for rule_id in sorted(hits):
        count = hits[rule_id]
        if count <= 0:
            continue
        db.execute(
            update(Rule)
            .where(Rule.household_id == household_id, Rule.id == rule_id)
            .values(hit_count=Rule.hit_count + count, last_hit_at=at)
        )


# ---------------------------------------------------------------------------
# Referenced objects
# ---------------------------------------------------------------------------


def existing_account_ids(
    db: Session, household_id: uuid.UUID, ids: Collection[uuid.UUID]
) -> set[uuid.UUID]:
    if not ids:
        return set()
    return set(
        db.execute(
            select(Account.id).where(Account.household_id == household_id, Account.id.in_(ids))
        ).scalars()
    )


def payee_names(
    db: Session, household_id: uuid.UUID, ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = db.execute(
        select(Payee.id, Payee.name).where(Payee.household_id == household_id, Payee.id.in_(ids))
    )
    return {row[0]: row[1] for row in rows}


def all_payees(db: Session, household_id: uuid.UUID) -> list[tuple[uuid.UUID, str, str]]:
    """``(id, name, normalized_name)`` of every payee."""
    rows = db.execute(
        select(Payee.id, Payee.name, Payee.normalized_name).where(
            Payee.household_id == household_id
        )
    )
    return [(row[0], row[1], row[2]) for row in rows]


def all_categories(
    db: Session, household_id: uuid.UUID
) -> list[tuple[uuid.UUID, uuid.UUID | None, str]]:
    """``(id, parent_id, name)`` of every category."""
    rows = db.execute(
        select(Category.id, Category.parent_id, Category.name).where(
            Category.household_id == household_id
        )
    )
    return [(row[0], row[1], row[2]) for row in rows]


def all_tags(db: Session, household_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    rows = db.execute(select(Tag.id, Tag.name).where(Tag.household_id == household_id))
    return [(row[0], row[1]) for row in rows]


def all_accounts(db: Session, household_id: uuid.UUID) -> list[tuple[uuid.UUID, str]]:
    rows = db.execute(select(Account.id, Account.name).where(Account.household_id == household_id))
    return [(row[0], row[1]) for row in rows]


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


def transaction_scope(
    household_id: uuid.UUID,
    *,
    account_ids: Sequence[uuid.UUID] | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    extra: Sequence[ColumnElement[bool]] = (),
) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = [Transaction.household_id == household_id, *extra]
    if account_ids:
        clauses.append(Transaction.account_id.in_(list(account_ids)))
    if date_from is not None:
        clauses.append(Transaction.posted_date >= date_from)
    if date_to is not None:
        clauses.append(Transaction.posted_date <= date_to)
    return clauses


def transactions_after(
    db: Session,
    clauses: Sequence[ColumnElement[bool]],
    *,
    after: tuple[date, uuid.UUID] | None,
    limit: int,
    lock: bool = False,
) -> list[Transaction]:
    """Up to ``limit`` rows, newest first, keyset ``(posted_date, id)`` descending."""
    stmt: Any = select(Transaction).where(*clauses)
    if after is not None:
        stmt = stmt.where(
            tuple_(Transaction.posted_date, Transaction.id) < tuple_(after[0], after[1])
        )
    stmt = stmt.order_by(Transaction.posted_date.desc(), Transaction.id.desc()).limit(limit)
    if lock:
        stmt = stmt.with_for_update(of=Transaction)
    return list(db.execute(stmt).scalars())
