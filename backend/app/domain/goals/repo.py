# SPDX-License-Identifier: AGPL-3.0-or-later
"""Goal, goal account and contribution queries (household-filtered, bound parameters).

Visibility (DATA_MODEL.md "Budgets and goals"): a goal is visible to a
caller only if they may see **every** account it names and every account of
a transaction linked to one of its contributions
(:func:`visible_goal_filter`); owners and admins see all goals.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from datetime import date
from decimal import Decimal

from sqlalchemy import ColumnElement, and_, delete, exists, func, select, true
from sqlalchemy.orm import QueryableAttribute, Session

from app.auth.principal import Principal
from app.domain.accounts.visibility import sees_all_accounts, visible_account_filter
from app.domain.goals.models import Goal, GoalAccount, GoalContribution
from app.domain.transactions.models import Transaction


def visible_goal_filter(
    principal: Principal,
    household_id: uuid.UUID,
    goal_id_column: ColumnElement[uuid.UUID] | QueryableAttribute[uuid.UUID],
) -> ColumnElement[bool]:
    """SQL predicate: the principal may see the goal (module doc)."""
    if sees_all_accounts(principal):
        return true()
    hidden_account = exists(
        select(GoalAccount.id).where(
            GoalAccount.household_id == household_id,
            GoalAccount.goal_id == goal_id_column,
            ~visible_account_filter(principal, GoalAccount.account_id),
        )
    )
    hidden_transaction = exists(
        select(GoalContribution.id)
        .join(
            Transaction,
            and_(
                Transaction.household_id == GoalContribution.household_id,
                Transaction.id == GoalContribution.transaction_id,
            ),
        )
        .where(
            GoalContribution.household_id == household_id,
            GoalContribution.goal_id == goal_id_column,
            ~visible_account_filter(principal, Transaction.account_id),
        )
    )
    return and_(~hidden_account, ~hidden_transaction)


def get_goal(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    goal_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Goal | None:
    stmt = select(Goal).where(
        Goal.household_id == household_id,
        Goal.id == goal_id,
        visible_goal_filter(principal, household_id, Goal.id),
    )
    if lock:
        stmt = stmt.with_for_update(of=Goal)
    return db.execute(stmt).scalar_one_or_none()


def list_goals(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    *,
    after: uuid.UUID | None,
    limit: int,
    archived: bool | None,
) -> list[Goal]:
    stmt = select(Goal).where(
        Goal.household_id == household_id,
        visible_goal_filter(principal, household_id, Goal.id),
    )
    if archived is not None:
        stmt = stmt.where(Goal.archived.is_(archived))
    if after is not None:
        stmt = stmt.where(Goal.id > after)
    return list(db.execute(stmt.order_by(Goal.id).limit(limit)).scalars())


def account_ids_for(
    db: Session, household_id: uuid.UUID, goal_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    result: dict[uuid.UUID, list[uuid.UUID]] = {goal_id: [] for goal_id in goal_ids}
    if not goal_ids:
        return result
    rows = db.execute(
        select(GoalAccount.goal_id, GoalAccount.account_id)
        .where(GoalAccount.household_id == household_id, GoalAccount.goal_id.in_(goal_ids))
        .order_by(GoalAccount.id)
    )
    for goal_id, account_id in rows:
        result[goal_id].append(account_id)
    return result


def set_accounts(db: Session, goal: Goal, account_ids: Collection[uuid.UUID]) -> None:
    current = set(account_ids_for(db, goal.household_id, [goal.id])[goal.id])
    wanted = set(account_ids)
    if current - wanted:
        db.execute(
            delete(GoalAccount).where(
                GoalAccount.household_id == goal.household_id,
                GoalAccount.goal_id == goal.id,
                GoalAccount.account_id.in_(current - wanted),
            )
        )
    for account_id in account_ids:
        if account_id not in current:
            db.add(
                GoalAccount(household_id=goal.household_id, goal_id=goal.id, account_id=account_id)
            )
    db.flush()


def daily_contributions(
    db: Session, household_id: uuid.UUID, goal_ids: Collection[uuid.UUID]
) -> dict[uuid.UUID, list[tuple[date, Decimal]]]:
    """Per goal, the sum of its contributions per day (aggregated in SQL)."""
    result: dict[uuid.UUID, list[tuple[date, Decimal]]] = {goal_id: [] for goal_id in goal_ids}
    if not goal_ids:
        return result
    rows = db.execute(
        select(
            GoalContribution.goal_id,
            GoalContribution.contributed_on,
            func.sum(GoalContribution.amount),
        )
        .where(
            GoalContribution.household_id == household_id,
            GoalContribution.goal_id.in_(goal_ids),
        )
        .group_by(GoalContribution.goal_id, GoalContribution.contributed_on)
        .order_by(GoalContribution.goal_id, GoalContribution.contributed_on)
    )
    for goal_id, day, total in rows:
        result[goal_id].append((day, Decimal(total)))
    return result


def has_contributions(db: Session, goal: Goal) -> bool:
    return (
        db.execute(
            select(GoalContribution.id)
            .where(
                GoalContribution.household_id == goal.household_id,
                GoalContribution.goal_id == goal.id,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def list_contributions(
    db: Session, goal: Goal, *, after: uuid.UUID | None, limit: int
) -> list[GoalContribution]:
    stmt = select(GoalContribution).where(
        GoalContribution.household_id == goal.household_id,
        GoalContribution.goal_id == goal.id,
    )
    if after is not None:
        stmt = stmt.where(GoalContribution.id > after)
    return list(db.execute(stmt.order_by(GoalContribution.id).limit(limit)).scalars())


def get_contribution(
    db: Session, goal: Goal, contribution_id: uuid.UUID
) -> GoalContribution | None:
    return db.execute(
        select(GoalContribution).where(
            GoalContribution.household_id == goal.household_id,
            GoalContribution.goal_id == goal.id,
            GoalContribution.id == contribution_id,
        )
    ).scalar_one_or_none()


def transaction_linked(db: Session, goal: Goal, transaction_id: uuid.UUID) -> bool:
    return (
        db.execute(
            select(GoalContribution.id)
            .where(
                GoalContribution.household_id == goal.household_id,
                GoalContribution.goal_id == goal.id,
                GoalContribution.transaction_id == transaction_id,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


def visible_transaction(
    db: Session, principal: Principal, household_id: uuid.UUID, transaction_id: uuid.UUID
) -> Transaction | None:
    return db.execute(
        select(Transaction).where(
            Transaction.household_id == household_id,
            Transaction.id == transaction_id,
            visible_account_filter(principal, Transaction.account_id),
        )
    ).scalar_one_or_none()
