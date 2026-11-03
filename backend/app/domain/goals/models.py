# SPDX-License-Identifier: AGPL-3.0-or-later
"""Savings goals (DATA_MODEL.md "Budgets and goals").

* ``goal``: a target amount in ``currency``, optionally by ``target_date``.
  ``achieved_at`` is set when the goal is reached; ``archived`` hides it.
* ``goal_account``: the goal's accounts (``accounts[]`` in DATA_MODEL.md,
  many-to-many). A goal's progress may only be shown to a member or viewer
  who can see every one of its accounts (``app.domain.accounts.visibility``).
* ``goal_contribution``: a dated amount towards the goal, optionally linked
  to the transaction that moved the money (cleared if that transaction is
  deleted).

All ``HouseholdScoped``; references include ``household_id``.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.domain.transactions.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import (
    AMOUNT,
    CURRENCY,
    currency_check,
    household_fk,
    household_key,
    household_ref_fk,
)

NAME_MAX = 200
MEMO_MAX = 1000


class Goal(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "goal"
    __table_args__ = (
        household_key("goal"),
        currency_check(),
        CheckConstraint("target_amount > 0", name="target_positive"),
    )

    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    target_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    target_date: Mapped[date | None] = mapped_column(Date)
    achieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    archived: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class GoalAccount(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "goal_account"
    __table_args__ = (
        household_fk("goal_account", "goal_id", "goal"),
        household_fk("goal_account", "account_id", "account"),
        UniqueConstraint("goal_id", "account_id", name="uq_goal_account_pair"),
    )

    goal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)


class GoalContribution(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "goal_contribution"
    __table_args__ = (
        household_fk("goal_contribution", "goal_id", "goal"),
        household_ref_fk("goal_contribution", "transaction_id", "transaction"),
        currency_check(),
    )

    goal_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    contributed_on: Mapped[date] = mapped_column(Date, nullable=False)
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transaction.id", ondelete="SET NULL"), index=True
    )
    memo: Mapped[str | None] = mapped_column(String(MEMO_MAX), info={"classification": "free_text"})
