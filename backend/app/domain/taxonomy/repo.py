# SPDX-License-Identifier: AGPL-3.0-or-later
"""Queries for categories, payees and tags.

Every statement filters ``household_id`` explicitly (RLS is the backstop,
SECURITY.md F3); bound parameters only. Bulk reassignments touch every row of
the household, whichever account it is on: only owners and admins reach them
(``app.domain.taxonomy.service``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.domain.budgets.models import BudgetLine
from app.domain.transactions.models import (
    Category,
    CategoryKind,
    Payee,
    ScheduledTransaction,
    Tag,
    Transaction,
    TransactionSplit,
)


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryNode:
    id: uuid.UUID
    parent_id: uuid.UUID | None
    kind: CategoryKind


def list_categories(
    db: Session,
    household_id: uuid.UUID,
    *,
    after: uuid.UUID | None,
    limit: int,
    kind: CategoryKind | None = None,
) -> list[Category]:
    stmt = select(Category).where(Category.household_id == household_id)
    if after is not None:
        stmt = stmt.where(Category.id > after)
    if kind is not None:
        stmt = stmt.where(Category.kind == kind)
    return list(db.execute(stmt.order_by(Category.id).limit(limit)).scalars())


def get_category(
    db: Session, household_id: uuid.UUID, category_id: uuid.UUID, *, lock: bool = False
) -> Category | None:
    stmt = select(Category).where(Category.household_id == household_id, Category.id == category_id)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def category_tree(db: Session, household_id: uuid.UUID) -> dict[uuid.UUID, CategoryNode]:
    """Every category of the household as ``id -> (parent, kind)``."""
    rows = db.execute(
        select(Category.id, Category.parent_id, Category.kind).where(
            Category.household_id == household_id
        )
    )
    return {cid: CategoryNode(cid, parent, kind) for cid, parent, kind in rows}


def sibling_names(
    db: Session,
    household_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    *,
    exclude: Sequence[uuid.UUID] = (),
) -> set[str]:
    """Case-folded names of the categories directly under ``parent_id``."""
    stmt = select(func.lower(Category.name)).where(Category.household_id == household_id)
    stmt = stmt.where(
        Category.parent_id.is_(None) if parent_id is None else Category.parent_id == parent_id
    )
    if exclude:
        stmt = stmt.where(Category.id.not_in(list(exclude)))
    return set(db.execute(stmt).scalars())


def children_of(db: Session, household_id: uuid.UUID, parent_id: uuid.UUID) -> list[Category]:
    return list(
        db.execute(
            select(Category)
            .where(Category.household_id == household_id, Category.parent_id == parent_id)
            .order_by(Category.id)
        ).scalars()
    )


def set_kind(
    db: Session, household_id: uuid.UUID, ids: Sequence[uuid.UUID], kind: CategoryKind
) -> None:
    if not ids:
        return
    db.execute(
        update(Category)
        .where(Category.household_id == household_id, Category.id.in_(list(ids)))
        .values(kind=kind, updated_at=func.now())
        .execution_options(synchronize_session="fetch")
    )


def reparent_children(
    db: Session, household_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
) -> int:
    result = db.execute(
        update(Category)
        .where(Category.household_id == household_id, Category.parent_id == source_id)
        .values(parent_id=target_id, updated_at=func.now())
        .execution_options(synchronize_session="fetch")
    )
    return _rowcount(result)


def reassign_category(
    db: Session, household_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
) -> dict[str, int]:
    """Point every reference to ``source_id`` at ``target_id``; return counts per table.

    Budget lines merge: a budget that already has a line for the target adds
    the source line's amount to it (both are in the budget's currency).
    """
    counts: dict[str, int] = {}
    for name, model in (
        ("transactions", Transaction),
        ("splits", TransactionSplit),
        ("schedules", ScheduledTransaction),
    ):
        result = db.execute(
            update(model)
            .where(model.household_id == household_id, model.category_id == source_id)
            .values(category_id=target_id, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        counts[name] = _rowcount(result)
    result = db.execute(
        update(Payee)
        .where(Payee.household_id == household_id, Payee.default_category_id == source_id)
        .values(default_category_id=target_id, updated_at=func.now())
        .execution_options(synchronize_session=False)
    )
    counts["payees"] = _rowcount(result)
    counts["budget_lines"] = _merge_budget_lines(db, household_id, source_id, target_id)
    return counts


def _merge_budget_lines(
    db: Session, household_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
) -> int:
    lines = list(
        db.execute(
            select(BudgetLine).where(
                BudgetLine.household_id == household_id, BudgetLine.category_id == source_id
            )
        ).scalars()
    )
    for line in lines:
        existing = db.execute(
            select(BudgetLine).where(
                BudgetLine.household_id == household_id,
                BudgetLine.budget_id == line.budget_id,
                BudgetLine.category_id == target_id,
            )
        ).scalar_one_or_none()
        if existing is None:
            line.category_id = target_id
        else:
            existing.amount = existing.amount + line.amount
            db.delete(line)
    db.flush()
    return len(lines)


def delete_category(db: Session, household_id: uuid.UUID, category_id: uuid.UUID) -> None:
    db.execute(
        delete(Category).where(Category.household_id == household_id, Category.id == category_id)
    )


# ---------------------------------------------------------------------------
# Payees
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PayeeKeys:
    id: uuid.UUID
    normalized_name: str
    aliases: tuple[str, ...]


def list_payees(
    db: Session,
    household_id: uuid.UUID,
    *,
    after: uuid.UUID | None,
    limit: int,
    search: str | None = None,
) -> list[Payee]:
    """Payees in id order; ``search`` (already normalised) matches inside the name."""
    stmt = select(Payee).where(Payee.household_id == household_id)
    if after is not None:
        stmt = stmt.where(Payee.id > after)
    if search:
        stmt = stmt.where(Payee.normalized_name.contains(search, autoescape=True))
    return list(db.execute(stmt.order_by(Payee.id).limit(limit)).scalars())


def get_payee(
    db: Session, household_id: uuid.UUID, payee_id: uuid.UUID, *, lock: bool = False
) -> Payee | None:
    stmt = select(Payee).where(Payee.household_id == household_id, Payee.id == payee_id)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def payee_keys(db: Session, household_id: uuid.UUID) -> list[PayeeKeys]:
    rows = db.execute(
        select(Payee.id, Payee.normalized_name, Payee.aliases).where(
            Payee.household_id == household_id
        )
    )
    return [PayeeKeys(pid, name, tuple(aliases or ())) for pid, name, aliases in rows]


def reassign_payee(
    db: Session, household_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for name, model in (("transactions", Transaction), ("schedules", ScheduledTransaction)):
        result = db.execute(
            update(model)
            .where(model.household_id == household_id, model.payee_id == source_id)
            .values(payee_id=target_id, updated_at=func.now())
            .execution_options(synchronize_session=False)
        )
        counts[name] = _rowcount(result)
    return counts


def delete_payee(db: Session, household_id: uuid.UUID, payee_id: uuid.UUID) -> None:
    db.execute(delete(Payee).where(Payee.household_id == household_id, Payee.id == payee_id))


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def list_tags(
    db: Session, household_id: uuid.UUID, *, after: uuid.UUID | None, limit: int
) -> list[Tag]:
    stmt = select(Tag).where(Tag.household_id == household_id)
    if after is not None:
        stmt = stmt.where(Tag.id > after)
    return list(db.execute(stmt.order_by(Tag.id).limit(limit)).scalars())


def get_tag(
    db: Session, household_id: uuid.UUID, tag_id: uuid.UUID, *, lock: bool = False
) -> Tag | None:
    stmt = select(Tag).where(Tag.household_id == household_id, Tag.id == tag_id)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def tag_name_taken(
    db: Session, household_id: uuid.UUID, name: str, *, exclude: uuid.UUID | None = None
) -> bool:
    stmt = select(Tag.id).where(
        Tag.household_id == household_id, func.lower(Tag.name) == name.lower()
    )
    if exclude is not None:
        stmt = stmt.where(Tag.id != exclude)
    return db.execute(stmt.limit(1)).first() is not None


def delete_tag(db: Session, household_id: uuid.UUID, tag_id: uuid.UUID) -> None:
    """Delete a tag; its ``transaction_tag`` links cascade."""
    db.execute(delete(Tag).where(Tag.household_id == household_id, Tag.id == tag_id))
