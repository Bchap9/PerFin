# SPDX-License-Identifier: AGPL-3.0-or-later
"""Savings goals and contributions (API.md ``/goals``).

Who may do what (routers check role and token scope first; every function
here checks the role again):

* **Read** (any household role, token scope ``budgets:read``): only goals
  whose every account, and every account of a transaction linked to one of
  its contributions, the caller may see (``app.domain.goals.repo``); any
  other goal, another household's or a missing one is ``404
  goal-not-found``. Owners and admins see every goal.
* **Create, change, delete, contribute** (member or above,
  ``budgets:write``): on visible goals only; the accounts named must be
  visible to the caller (``404 account-not-found``).

Rules (DATA_MODEL.md "Budgets and goals"):

* ``target`` is ``> 0`` (``422 invalid-goal-target``) and sets the currency;
  the currency changes only while the goal has no contributions (``409
  goal-currency-locked``).
* A contribution's amount is non-zero (negative: a withdrawal) in the goal's
  currency (``422 currency-mismatch``, ``422 invalid-contribution``), dated
  today or earlier (``422 contribution-in-future``). A linked transaction
  must be visible (``404 transaction-not-found``), on one of the goal's
  accounts (``422 contribution-transaction-invalid``) and linked to the goal
  once (``409 contribution-exists``); without an ``amount`` its amount is
  converted into the goal's currency on its posted date (``422
  fx-rate-missing``).
* ``achieved_at`` is set when the contributions first reach the target and
  cleared when they fall below it again (or the target is raised).

Progress and projected completion: ``app.domain.goals.progress``. Every
write is audited (``goal.create|update|delete``, ``goal.contribution_add``,
``goal.contribution_delete``; a memo's text never goes to the audit trail)
and emits ``goal.created|updated|deleted`` with ids only.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import Money, sum_amounts
from app.domain.accounts.balances import balances_as_of
from app.domain.accounts.models import Account
from app.domain.accounts.visibility import require_visible_accounts
from app.domain.fx.service import FxConverter, convert
from app.domain.goals import repo
from app.domain.goals.models import Goal, GoalContribution
from app.domain.goals.progress import compute
from app.domain.goals.schemas import (
    ContributionCreate,
    ContributionOut,
    ContributionPage,
    GoalCreate,
    GoalOut,
    GoalPage,
    GoalProgressOut,
    GoalUpdate,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection


# ---------------------------------------------------------------------------
# Problems and helpers
# ---------------------------------------------------------------------------


def goal_not_found() -> ProblemError:
    return ProblemError(404, "goal-not-found", "Goal not found")


def contribution_not_found() -> ProblemError:
    return ProblemError(404, "contribution-not-found", "Contribution not found")


def _currency_mismatch(currency: str) -> ProblemError:
    return ProblemError(
        422,
        "currency-mismatch",
        "Currency mismatch",
        f"Amounts of this goal are in {currency}.",
        extra={"currency": currency},
    )


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def today() -> date:
    return clock.utcnow().date()


def _check_target(amount: Decimal) -> None:
    if amount <= 0:
        raise ProblemError(
            422, "invalid-goal-target", "Invalid goal target", "The target must be above zero."
        )


def _state(goal: Goal, account_ids: Sequence[uuid.UUID]) -> dict[str, Any]:
    return {
        "name": goal.name,
        "target_amount": format(goal.target_amount, "f"),
        "currency": goal.currency,
        "target_date": goal.target_date.isoformat() if goal.target_date else None,
        "archived": goal.archived,
        "account_ids": sorted(str(a) for a in account_ids),
    }


def _record(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    goal_id: uuid.UUID,
    action: str,
    event: str,
    *,
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=f"goal.{action}",
        target_type="goal",
        target_id=goal_id,
        before=before,
        after=after,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    # Members and viewers hear only about goals they may read: every account
    # of the goal visible (SECURITY.md I2, events.deliverable).
    accounts = {str(a) for a in repo.account_ids_for(db, household_id, [goal_id])[goal_id]}
    for state in (before, after):
        if isinstance(state, dict) and isinstance(state.get("account_ids"), list):
            accounts.update(str(a) for a in state["account_ids"])
    emit(
        db,
        household_id,
        f"goal.{event}",
        {"goal_id": str(goal_id), "all_account_ids": sorted(accounts)},
    )


def _get(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    goal_id: uuid.UUID,
    *,
    lock: bool = False,
) -> Goal:
    goal = repo.get_goal(db, principal, household_id, goal_id, lock=lock)
    if goal is None:
        raise goal_not_found()
    return goal


def _contributed(db: Session, goal: Goal) -> Decimal:
    daily = repo.daily_contributions(db, goal.household_id, [goal.id])[goal.id]
    return sum_amounts(amount for _, amount in daily)


def _refresh_achieved(db: Session, goal: Goal) -> None:
    reached = _contributed(db, goal) >= goal.target_amount
    if reached and goal.achieved_at is None:
        goal.achieved_at = clock.utcnow()
    elif not reached and goal.achieved_at is not None:
        goal.achieved_at = None
    db.flush()


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _accounts_balances(
    db: Session,
    household_id: uuid.UUID,
    goals: Sequence[Goal],
    accounts: dict[uuid.UUID, list[uuid.UUID]],
    on: date,
) -> dict[uuid.UUID, Decimal | None]:
    """Per goal, its accounts' balance today in the goal's currency (``None``: n/a)."""
    wanted = {a for ids in accounts.values() for a in ids}
    rows = (
        list(
            db.execute(
                select(Account).where(Account.household_id == household_id, Account.id.in_(wanted))
            ).scalars()
        )
        if wanted
        else []
    )
    by_id = {a.id: a for a in rows}
    balances = balances_as_of(db, rows, on)
    converters: dict[str, FxConverter] = {}
    result: dict[uuid.UUID, Decimal | None] = {}
    for goal in goals:
        ids = [a for a in accounts.get(goal.id, []) if a in by_id]
        if not ids:
            result[goal.id] = None
            continue
        converter = converters.setdefault(
            goal.currency, FxConverter(db, household_id, goal.currency, until=on)
        )
        parts = [converter.try_convert(balances[a], by_id[a].currency, on) for a in ids]
        result[goal.id] = (
            None
            if any(p is None for p in parts)
            else sum_amounts(p for p in parts if p is not None)
        )
    return result


def goals_out(db: Session, goals: Sequence[Goal]) -> list[GoalOut]:
    if not goals:
        return []
    household_id = goals[0].household_id
    ids = [g.id for g in goals]
    accounts = repo.account_ids_for(db, household_id, ids)
    daily = repo.daily_contributions(db, household_id, ids)
    as_of = today()
    balances = _accounts_balances(db, household_id, goals, accounts, as_of)
    result: list[GoalOut] = []
    for goal in goals:
        progress = compute(
            goal.target_amount, daily[goal.id], as_of=as_of, target_date=goal.target_date
        )
        balance = balances.get(goal.id)
        result.append(
            GoalOut(
                id=goal.id,
                name=goal.name,
                target=Money.of(goal.target_amount, goal.currency),
                target_date=goal.target_date,
                account_ids=accounts[goal.id],
                archived=goal.archived,
                achieved_at=goal.achieved_at,
                progress=GoalProgressOut(
                    as_of=as_of,
                    contributed=Money.of(progress.contributed, goal.currency),
                    remaining=Money.of(progress.remaining, goal.currency),
                    percent=progress.percent,
                    accounts_balance=(
                        Money.of(balance, goal.currency) if balance is not None else None
                    ),
                    monthly_rate=(
                        Money.of(progress.monthly_rate, goal.currency)
                        if progress.monthly_rate is not None
                        else None
                    ),
                    projected_completion=progress.projected_completion,
                    on_track=progress.on_track,
                ),
                created_at=goal.created_at,
                updated_at=goal.updated_at,
            )
        )
    return result


def contribution_out(row: GoalContribution) -> ContributionOut:
    return ContributionOut(
        id=row.id,
        goal_id=row.goal_id,
        contributed_on=row.contributed_on,
        amount=Money.of(row.amount, row.currency),
        transaction_id=row.transaction_id,
        memo=row.memo,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# Goals
# ---------------------------------------------------------------------------


def list_goals(
    db: Session,
    principal: Principal,
    *,
    cursor: uuid.UUID | None,
    limit: int,
    archived: bool | None = None,
) -> GoalPage:
    household_id = _household(principal, HouseholdRole.VIEWER)
    rows = repo.list_goals(
        db, principal, household_id, after=cursor, limit=limit + 1, archived=archived
    )
    page = rows[:limit]
    return GoalPage(
        items=goals_out(db, page), next_cursor=page[-1].id if len(rows) > limit and page else None
    )


def require_goal(db: Session, principal: Principal, goal_id: uuid.UUID) -> Goal:
    """A goal the caller may see, or ``404 goal-not-found``."""
    return _get(db, principal, _household(principal, HouseholdRole.VIEWER), goal_id)


def get_goal(db: Session, principal: Principal, goal_id: uuid.UUID) -> GoalOut:
    return goals_out(db, [require_goal(db, principal, goal_id)])[0]


def create_goal(
    db: Session, principal: Principal, data: GoalCreate, *, request: HTTPConnection | None
) -> GoalOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    _check_target(data.target.amount)
    require_visible_accounts(db, principal, data.account_ids)
    goal = Goal(
        household_id=household_id,
        name=data.name,
        target_amount=data.target.amount,
        currency=data.target.currency,
        target_date=data.target_date,
    )
    db.add(goal)
    db.flush()
    repo.set_accounts(db, goal, data.account_ids)
    _record(
        db,
        principal,
        household_id,
        goal.id,
        "create",
        "created",
        after=_state(goal, data.account_ids),
        request=request,
    )
    return goals_out(db, [goal])[0]


def update_goal(
    db: Session,
    principal: Principal,
    goal_id: uuid.UUID,
    data: GoalUpdate,
    *,
    request: HTTPConnection | None,
) -> GoalOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    goal = _get(db, principal, household_id, goal_id, lock=True)
    current_accounts = repo.account_ids_for(db, household_id, [goal.id])[goal.id]
    before = _state(goal, current_accounts)
    if data.target is not None:
        _check_target(data.target.amount)
        if data.target.currency != goal.currency and repo.has_contributions(db, goal):
            raise ProblemError(
                409,
                "goal-currency-locked",
                "Currency cannot change",
                "The goal has contributions in its current currency.",
            )
        goal.target_amount = data.target.amount
        goal.currency = data.target.currency
    if data.account_ids is not None:
        require_visible_accounts(db, principal, data.account_ids)
        repo.set_accounts(db, goal, data.account_ids)
        current_accounts = list(data.account_ids)
    if data.name is not None:
        goal.name = data.name
    if "target_date" in data.model_fields_set:
        goal.target_date = data.target_date
    if data.archived is not None:
        goal.archived = data.archived
    db.flush()
    _refresh_achieved(db, goal)
    after = _state(goal, current_accounts)
    if after != before:
        _record(
            db,
            principal,
            household_id,
            goal.id,
            "update",
            "updated",
            before=before,
            after=after,
            request=request,
        )
    return goals_out(db, [goal])[0]


def delete_goal(
    db: Session, principal: Principal, goal_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    household_id = _household(principal, HouseholdRole.MEMBER)
    goal = _get(db, principal, household_id, goal_id, lock=True)
    before = _state(goal, repo.account_ids_for(db, household_id, [goal.id])[goal.id])
    db.delete(goal)
    db.flush()
    _record(
        db, principal, household_id, goal_id, "delete", "deleted", before=before, request=request
    )


# ---------------------------------------------------------------------------
# Contributions
# ---------------------------------------------------------------------------


def list_contributions(
    db: Session, principal: Principal, goal_id: uuid.UUID, *, cursor: uuid.UUID | None, limit: int
) -> ContributionPage:
    goal = require_goal(db, principal, goal_id)
    rows = repo.list_contributions(db, goal, after=cursor, limit=limit + 1)
    page = rows[:limit]
    return ContributionPage(
        items=[contribution_out(r) for r in page],
        next_cursor=page[-1].id if len(rows) > limit and page else None,
    )


def add_contribution(
    db: Session,
    principal: Principal,
    goal_id: uuid.UUID,
    data: ContributionCreate,
    *,
    request: HTTPConnection | None,
) -> ContributionOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    goal = _get(db, principal, household_id, goal_id, lock=True)
    amount = data.amount.amount if data.amount is not None else None
    if data.amount is not None and data.amount.currency != goal.currency:
        raise _currency_mismatch(goal.currency)
    on = data.contributed_on
    if data.transaction_id is not None:
        transaction = repo.visible_transaction(db, principal, household_id, data.transaction_id)
        if transaction is None:
            raise ProblemError(404, "transaction-not-found", "Transaction not found")
        goal_accounts = repo.account_ids_for(db, household_id, [goal.id])[goal.id]
        if transaction.account_id not in goal_accounts:
            raise ProblemError(
                422,
                "contribution-transaction-invalid",
                "Transaction not on the goal's accounts",
                "A linked transaction must be on one of the goal's accounts.",
            )
        if repo.transaction_linked(db, goal, transaction.id):
            raise ProblemError(
                409,
                "contribution-exists",
                "Already contributed",
                "This transaction is already a contribution to the goal.",
            )
        if amount is None:
            amount = convert(
                db,
                household_id,
                transaction.amount,
                transaction.currency,
                goal.currency,
                transaction.posted_date,
            )
        if on is None:
            on = transaction.posted_date
    if amount is None:
        raise ProblemError(
            422,
            "invalid-contribution",
            "Invalid contribution",
            "Give an amount or a transaction_id.",
        )
    if amount == 0:
        raise ProblemError(
            422, "invalid-contribution", "Invalid contribution", "The amount must not be zero."
        )
    on = on if on is not None else today()
    if on > today():
        raise ProblemError(
            422,
            "contribution-in-future",
            "Contribution in the future",
            "contributed_on must be today or earlier.",
        )
    row = GoalContribution(
        household_id=household_id,
        goal_id=goal.id,
        contributed_on=on,
        amount=amount,
        currency=goal.currency,
        transaction_id=data.transaction_id,
        memo=data.memo,
    )
    db.add(row)
    db.flush()
    _refresh_achieved(db, goal)
    _record(
        db,
        principal,
        household_id,
        goal.id,
        "contribution_add",
        "updated",
        after={
            "contribution_id": str(row.id),
            "amount": format(amount, "f"),
            "currency": goal.currency,
            "contributed_on": on.isoformat(),
            "transaction_id": str(row.transaction_id) if row.transaction_id else None,
            "has_memo": row.memo is not None,
        },
        request=request,
    )
    return contribution_out(row)


def delete_contribution(
    db: Session,
    principal: Principal,
    goal_id: uuid.UUID,
    contribution_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> None:
    household_id = _household(principal, HouseholdRole.MEMBER)
    goal = _get(db, principal, household_id, goal_id, lock=True)
    row = repo.get_contribution(db, goal, contribution_id)
    if row is None:
        raise contribution_not_found()
    before = {
        "contribution_id": str(row.id),
        "amount": format(row.amount, "f"),
        "currency": row.currency,
        "contributed_on": row.contributed_on.isoformat(),
        "transaction_id": str(row.transaction_id) if row.transaction_id else None,
    }
    db.delete(row)
    db.flush()
    _refresh_achieved(db, goal)
    _record(
        db,
        principal,
        household_id,
        goal.id,
        "contribution_delete",
        "updated",
        before=before,
        request=request,
    )
