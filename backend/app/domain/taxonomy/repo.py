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

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app.db.base import Base
from app.domain.budgets.models import BudgetLine
from app.domain.categorisation.models import MerchantMemory
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
    the source line's amount to it (both are in the budget's currency). A
    suggestion for the source becomes one for the target without the row
    counting as touched; what the household taught about the source is
    folded into the target (:func:`_fold_memory`).
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
    result = db.execute(
        update(Transaction)
        .where(
            Transaction.household_id == household_id,
            Transaction.suggested_category_id == source_id,
        )
        # A suggestion is PerFin's guess, not the person's edit: ``updated_at``
        # stays (explicit, so the column's ``onupdate`` does not apply) and the
        # importer still reads the row as untouched.
        .values(suggested_category_id=target_id, updated_at=Transaction.updated_at)
        .execution_options(synchronize_session=False)
    )
    counts["suggestions"] = _rowcount(result)
    counts["memory"] = _fold_memory(db, household_id, source_id, target_id)
    return counts


def _fold_memory(
    db: Session, household_id: uuid.UUID, source_id: uuid.UUID, target_id: uuid.UUID
) -> int:
    """Count the decisions for ``source_id`` as decisions for ``target_id``.

    ``merchant_memory`` has one row per merchant key, direction and category.
    Where the target already has a row for the key and direction, the
    source's decisions are added to it and it keeps the later
    ``last_decided_at``; the source's other rows are re-pointed. Returns the
    source rows folded or moved.
    """
    scope = (MerchantMemory.household_id == household_id,)
    rows = list(
        db.execute(
            select(MerchantMemory)
            .where(*scope, MerchantMemory.category_id == source_id)
            .order_by(MerchantMemory.id)
            .with_for_update()
        ).scalars()
    )
    if not rows:
        return 0
    held = {
        (row.merchant_key, row.direction): row
        for row in db.execute(
            select(MerchantMemory)
            .where(*scope, MerchantMemory.category_id == target_id)
            .with_for_update()
        ).scalars()
    }
    for row in rows:
        existing = held.get((row.merchant_key, row.direction))
        if existing is None:
            row.category_id = target_id
        else:
            existing.decisions = existing.decisions + row.decisions
            existing.last_decided_at = max(existing.last_decided_at, row.last_decided_at)
            db.delete(row)
    db.flush()
    return len(rows)


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


#: Sources of a category nobody chose: the system's own guesses. A row that
#: got the deleted category from one of them is undecided again.
GUESSED_SOURCES = ("learned", "dictionary", "keyword")


def delete_category(db: Session, household_id: uuid.UUID, category_id: uuid.UUID) -> None:
    """Delete the category; the foreign keys clear it from the rows that had it.

    A foreign key clears only the id. A row whose category was a guess
    (:data:`GUESSED_SOURCES`), or that was only offered this category, would
    keep saying how a category it no longer has was decided, and would never
    be looked at again: its source and confidence are cleared first, so it is
    undecided and the next pass evaluates it. What a person, a file, a rule
    or a payee decided (and ``legacy``) keeps its source: a decided "no
    category" that no guess overwrites. ``updated_at`` is pinned: this is not
    a person's edit of the row.
    """
    table = Base.metadata.tables[Transaction.__tablename__]
    db.execute(
        update(table)
        .where(
            table.c.household_id == household_id,
            or_(
                and_(
                    table.c.category_id == category_id,
                    table.c.category_source.in_(GUESSED_SOURCES),
                ),
                table.c.suggested_category_id == category_id,
            ),
        )
        .values(
            category_source=None,
            category_confidence=None,
            category_rule_id=None,
            # Explicit, so the column's ``onupdate`` does not apply.
            updated_at=table.c.updated_at,
        )
    )
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
