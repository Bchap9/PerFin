# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input and output schemas for savings goals and contributions (API.md ``/goals``).

Inputs forbid unknown fields (SECURITY.md F4). Money is ``{"amount":
"12.34", "currency": "USD"}`` (``app.core.money``).
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Annotated, Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.money import Money, MoneyIn
from app.domain.accounts.schemas import Name
from app.domain.goals.models import MEMO_MAX

MAX_PAGE: Final = 500
DEFAULT_PAGE: Final = 100
#: Most accounts one goal may name.
MAX_GOAL_ACCOUNTS: Final = 50

AccountIds = Annotated[list[uuid.UUID], Field(max_length=MAX_GOAL_ACCOUNTS)]


class _Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


def _unique(ids: list[uuid.UUID] | None) -> None:
    if ids is not None and len(ids) != len(set(ids)):
        raise ValueError("account_ids must be unique")


class GoalCreate(_Input):
    """``POST /goals``: ``target`` (``> 0``) sets the goal's currency."""

    name: Name
    target: MoneyIn
    target_date: date | None = None
    account_ids: AccountIds = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        _unique(self.account_ids)
        return self


class GoalUpdate(_Input):
    """``PATCH /goals/{id}``: absent fields stay; ``account_ids`` replaces the set."""

    name: Name | None = None
    target: MoneyIn | None = None
    target_date: date | None = None
    account_ids: AccountIds | None = None
    archived: bool | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        _unique(self.account_ids)
        for name in ("name", "target", "account_ids", "archived"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} cannot be null")
        return self


class ContributionCreate(_Input):
    """``POST /goals/{id}/contributions``.

    Either ``amount`` (in the goal's currency), or ``transaction_id`` (a
    transaction on one of the goal's accounts; its amount converted into the
    goal's currency on its posted date is the default amount, its posted
    date the default ``contributed_on``), or both. ``contributed_on``
    defaults to today and may not be in the future.
    """

    amount: MoneyIn | None = None
    contributed_on: date | None = None
    transaction_id: uuid.UUID | None = None
    memo: Annotated[str | None, Field(max_length=MEMO_MAX)] = None


class GoalProgressOut(_Output):
    """Progress as of ``as_of`` (``app.domain.goals.progress``).

    ``accounts_balance`` is the balance of the goal's accounts today in the
    goal's currency (``null`` without accounts or when a rate is missing).
    """

    as_of: date
    contributed: Money
    remaining: Money
    percent: str
    accounts_balance: Money | None
    monthly_rate: Money | None
    projected_completion: date | None
    on_track: bool | None


class GoalOut(_Output):
    id: uuid.UUID
    name: str
    target: Money
    target_date: date | None
    account_ids: list[uuid.UUID]
    archived: bool
    achieved_at: datetime | None
    progress: GoalProgressOut
    created_at: datetime
    updated_at: datetime


class GoalPage(_Output):
    items: list[GoalOut]
    next_cursor: uuid.UUID | None


class ContributionOut(_Output):
    id: uuid.UUID
    goal_id: uuid.UUID
    contributed_on: date
    amount: Money
    transaction_id: uuid.UUID | None
    memo: str | None
    created_at: datetime


class ContributionPage(_Output):
    items: list[ContributionOut]
    next_cursor: uuid.UUID | None
