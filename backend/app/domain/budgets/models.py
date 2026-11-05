# SPDX-License-Identifier: AGPL-3.0-or-later
"""Budgets (DATA_MODEL.md "Budgets and goals").

* ``budget``: a named plan over ``period`` (monthly, annual, or a custom
  range ``start_date..end_date``), in ``currency`` (the household's base
  currency by default). ``rollover`` carries unspent (or overspent) amounts
  of a period into the next one (envelope style).
* ``budget_line``: the amount per period for one category, in the budget's
  currency. One line per category and budget; the line goes when its
  category is deleted.

Both are ``HouseholdScoped``; references include ``household_id``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Date, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.domain.transactions.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import AMOUNT, CURRENCY, currency_check, household_fk, household_key, pg_enum

NAME_MAX = 200


class BudgetPeriod(enum.StrEnum):
    MONTHLY = "monthly"
    ANNUAL = "annual"
    CUSTOM = "custom"


class Budget(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "budget"
    __table_args__ = (
        household_key("budget"),
        currency_check(),
        CheckConstraint("(period = 'custom') = (end_date IS NOT NULL)", name="end_date_for_custom"),
        CheckConstraint("end_date IS NULL OR end_date >= start_date", name="end_after_start"),
    )

    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    period: Mapped[BudgetPeriod] = mapped_column(
        pg_enum(BudgetPeriod, "budget_period"), nullable=False
    )
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    rollover: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class BudgetLine(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "budget_line"
    __table_args__ = (
        household_fk("budget_line", "budget_id", "budget"),
        household_fk("budget_line", "category_id", "category"),
        UniqueConstraint("budget_id", "category_id", name="uq_budget_line_budget_category"),
    )

    budget_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
