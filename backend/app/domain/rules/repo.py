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

from sqlalchemy import ColumnElement, and_, case, delete, exists, func, or_, select, tuple_, update
from sqlalchemy.orm import Session

from app.domain.accounts.models import Account
from app.domain.transactions.models import (
    Category,
    Payee,
    Rule,
    Tag,
    Transaction,
    TransactionSplit,
)


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


# ---------------------------------------------------------------------------
# Merchant rules: what one would change, and what one changed
# ---------------------------------------------------------------------------

#: States of a merchant's row towards a merchant rule (:func:`merchant_state`).
UNCATEGORISED = "uncategorised"
GUESSED = "guessed"
DECIDED = "decided"
SAME_CATEGORY = "same_category"
#: The states a merchant rule applied to existing rows changes.
CHANGING = (UNCATEGORISED, GUESSED)


def merchant_state(
    category_id: uuid.UUID, own_rule_id: uuid.UUID | None, guess_sources: Collection[str]
) -> ColumnElement[str]:
    """Where a transaction stands towards a merchant rule that sets ``category_id``.

    ``decided``: split, or a category (or a cleared one) from a person, the
    file, a payee default, an earlier release or another rule: never changed.
    ``same_category``: already there. ``uncategorised``: no category and
    nobody decided (a suggestion included). ``guessed``: another category
    from a guess, or from this rule itself (``own_rule_id``) before its
    category was changed. The Python form is ``service._is_decided``.
    """
    split = exists().where(
        TransactionSplit.household_id == Transaction.household_id,
        TransactionSplit.transaction_id == Transaction.id,
    )
    guess = Transaction.category_source.in_(sorted(guess_sources))
    replaceable: ColumnElement[bool] = guess
    if own_rule_id is not None:
        replaceable = or_(
            guess,
            and_(
                Transaction.category_source == "rule", Transaction.category_rule_id == own_rule_id
            ),
        )
    return case(
        (split, DECIDED),
        (Transaction.category_id == category_id, SAME_CATEGORY),
        (
            and_(
                Transaction.category_id.is_(None),
                or_(Transaction.category_source.is_(None), guess),
            ),
            UNCATEGORISED,
        ),
        (and_(Transaction.category_id.is_not(None), replaceable), GUESSED),
        else_=DECIDED,
    )


def merchant_counts(
    db: Session, clauses: Sequence[ColumnElement[bool]], state: ColumnElement[str]
) -> dict[str, int]:
    """Rows per :func:`merchant_state` (counted in SQL, never capped)."""
    rows = db.execute(select(state, func.count()).where(*clauses).group_by(state))
    return {str(row[0]): int(row[1]) for row in rows}


def merchant_sample(
    db: Session, clauses: Sequence[ColumnElement[bool]], state: ColumnElement[str], limit: int
) -> list[uuid.UUID]:
    """The newest ``limit`` rows a merchant rule would change."""
    stmt = (
        select(Transaction.id)
        .where(*clauses, state.in_(CHANGING))
        .order_by(Transaction.posted_date.desc(), Transaction.id.desc())
        .limit(limit)
    )
    return list(db.execute(stmt).scalars())


def merchant_accounts(db: Session, clauses: Sequence[ColumnElement[bool]]) -> list[uuid.UUID]:
    """The accounts holding the rows of ``clauses``, in id order."""
    stmt = select(Transaction.account_id).where(*clauses).distinct()
    return sorted(db.execute(stmt).scalars())


def rows_set_by_rule(
    db: Session, household_id: uuid.UUID, rule_id: uuid.UUID
) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """``(id, account_id)`` of the rows whose categorisation ``rule_id`` set, locked.

    On every account: the rule was the household's. A row a person
    re-categorised since no longer names the rule and is not among them.
    """
    rows = db.execute(
        select(Transaction.id, Transaction.account_id)
        .where(
            Transaction.household_id == household_id,
            Transaction.category_rule_id == rule_id,
            Transaction.category_source == "rule",
        )
        .order_by(Transaction.id)
        .with_for_update(of=Transaction)
    )
    return [(row[0], row[1]) for row in rows]


def delete_splits_of(db: Session, household_id: uuid.UUID, ids: Collection[uuid.UUID]) -> None:
    """Remove the splits of ``ids`` (the transaction rows themselves are not written)."""
    if not ids:
        return
    db.execute(
        delete(TransactionSplit).where(
            TransactionSplit.household_id == household_id,
            TransactionSplit.transaction_id.in_(list(ids)),
        )
    )
