# SPDX-License-Identifier: AGPL-3.0-or-later
"""Budget and budget line queries (always filtered by household, bound parameters)."""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from decimal import Decimal

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.domain.budgets.models import Budget, BudgetLine
from app.domain.transactions.models import Category


def get_budget(
    db: Session, household_id: uuid.UUID, budget_id: uuid.UUID, *, lock: bool = False
) -> Budget | None:
    stmt = select(Budget).where(Budget.household_id == household_id, Budget.id == budget_id)
    if lock:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def list_budgets(
    db: Session, household_id: uuid.UUID, *, after: uuid.UUID | None, limit: int
) -> list[Budget]:
    stmt = select(Budget).where(Budget.household_id == household_id)
    if after is not None:
        stmt = stmt.where(Budget.id > after)
    return list(db.execute(stmt.order_by(Budget.id).limit(limit)).scalars())


def lines_for(
    db: Session, household_id: uuid.UUID, budget_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[BudgetLine]]:
    """Lines of each budget, in creation order."""
    result: dict[uuid.UUID, list[BudgetLine]] = {budget_id: [] for budget_id in budget_ids}
    if not budget_ids:
        return result
    rows = db.execute(
        select(BudgetLine)
        .where(BudgetLine.household_id == household_id, BudgetLine.budget_id.in_(budget_ids))
        .order_by(BudgetLine.id)
    ).scalars()
    for line in rows:
        result[line.budget_id].append(line)
    return result


def categories(
    db: Session, household_id: uuid.UUID, category_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, Category]:
    if not category_ids:
        return {}
    rows = db.execute(
        select(Category).where(Category.household_id == household_id, Category.id.in_(category_ids))
    ).scalars()
    return {c.id: c for c in rows}


def replace_lines(
    db: Session, budget: Budget, wanted: Mapping[uuid.UUID, Decimal]
) -> Sequence[BudgetLine]:
    """Make the budget's lines exactly ``{category_id: amount}`` (update, delete, add)."""
    current = {
        line.category_id: line
        for line in lines_for(db, budget.household_id, [budget.id])[budget.id]
    }
    gone = [line.id for cid, line in current.items() if cid not in wanted]
    if gone:
        db.execute(
            delete(BudgetLine).where(
                BudgetLine.household_id == budget.household_id, BudgetLine.id.in_(gone)
            )
        )
    for category_id, amount in wanted.items():
        line = current.get(category_id)
        if line is None:
            db.add(
                BudgetLine(
                    household_id=budget.household_id,
                    budget_id=budget.id,
                    category_id=category_id,
                    amount=amount,
                )
            )
        elif line.amount != amount:
            line.amount = amount
    db.flush()
    return lines_for(db, budget.household_id, [budget.id])[budget.id]


def delete_budget(db: Session, budget: Budget) -> None:
    db.delete(budget)
    db.flush()
