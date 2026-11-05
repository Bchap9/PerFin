# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loans, their payments and rate changes (DATA_MODEL.md "Loans").

A loan is the terms of an ``account`` of type ``loan``, ``mortgage`` or
``line_of_credit`` (one ``loan`` row per account; the service checks the
type), so it appears in net worth with that account's balance.

* ``loan``: ``rate`` is the annual nominal rate as a **fraction**
  (``0.06125`` is 6.125 %), ``rate_type`` fixed or variable (the current
  rate of a variable loan is the latest ``loan_rate_change`` on or before a
  date, else ``rate``). ``principal_original``, ``payment_amount`` and
  ``escrow_amount`` are in the account currency. ``payment_day`` is the day
  of the month a payment is due (29-31 fall back to the month's last day).
  ``lender`` is a display name; loan numbers are identity data and are not
  stored (AI_ACCESS.md).
* ``loan_payment``: the split of one payment into principal, interest,
  escrow and fees (all non-negative, in the loan currency), optionally linked
  to the ledger ``transaction`` that paid it and the statement ``document``
  it came from (both ``SET NULL`` on delete).
* ``loan_rate_change``: a new rate from ``effective_date``, at most one per
  loan and date.

All ``HouseholdScoped`` (forced RLS); references include ``household_id``.
A loan is visible to a member or viewer exactly when its account is.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date
from decimal import Decimal
from typing import Final

from sqlalchemy import (
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.domain.documents.models
import app.domain.transactions.models  # noqa: F401
from app.core.dates import min_date_check
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import (
    AMOUNT,
    CURRENCY,
    currency_check,
    household_fk,
    household_key,
    household_ref_fk,
    pg_enum,
)

#: Annual interest rate as a fraction, ``NUMERIC(10, 8)`` (``0.06125000``).
INTEREST_RATE: Final = Numeric(10, 8, asdecimal=True)
LENDER_MAX = 200
#: 100 years.
TERM_MONTHS_MAX = 1200


class RateType(enum.StrEnum):
    FIXED = "fixed"
    VARIABLE = "variable"


class Loan(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "loan"
    __table_args__ = (
        household_key("loan"),
        UniqueConstraint("household_id", "id", "currency", name="uq_loan_household_id_id_currency"),
        household_fk("loan", "account_id", "account", extra=(("currency", "currency"),)),
        UniqueConstraint("account_id", name="uq_loan_account"),
        currency_check(),
        CheckConstraint("principal_original > 0", name="principal_positive"),
        CheckConstraint("rate >= 0 AND rate < 1", name="rate_fraction"),
        CheckConstraint(f"term_months > 0 AND term_months <= {TERM_MONTHS_MAX}", name="term_range"),
        CheckConstraint("payment_amount >= 0", name="payment_non_negative"),
        CheckConstraint("payment_day BETWEEN 1 AND 31", name="payment_day_range"),
        CheckConstraint("escrow_amount IS NULL OR escrow_amount >= 0", name="escrow_non_negative"),
        CheckConstraint(min_date_check("origination_date"), name="origination_date_min"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    principal_original: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    rate: Mapped[Decimal] = mapped_column(INTEREST_RATE, nullable=False)
    rate_type: Mapped[RateType] = mapped_column(pg_enum(RateType, "loan_rate_type"), nullable=False)
    term_months: Mapped[int] = mapped_column(Integer, nullable=False)
    origination_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    payment_day: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    escrow_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    lender: Mapped[str | None] = mapped_column(String(LENDER_MAX))


class LoanPayment(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "loan_payment"
    __table_args__ = (
        household_fk("loan_payment", "loan_id", "loan", extra=(("currency", "currency"),)),
        household_ref_fk("loan_payment", "transaction_id", "transaction"),
        household_ref_fk("loan_payment", "source_document_id", "document"),
        Index("ix_loan_payment_loan_date", "loan_id", "paid_on"),
        currency_check(),
        CheckConstraint(
            "principal >= 0 AND interest >= 0 AND escrow >= 0 AND fees >= 0",
            name="parts_non_negative",
        ),
        CheckConstraint(min_date_check("paid_on"), name="paid_on_min"),
    )

    loan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    paid_on: Mapped[date] = mapped_column(Date, nullable=False)
    principal: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal(0), server_default=text("0")
    )
    interest: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal(0), server_default=text("0")
    )
    escrow: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal(0), server_default=text("0")
    )
    fees: Mapped[Decimal] = mapped_column(
        AMOUNT, nullable=False, default=Decimal(0), server_default=text("0")
    )
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transaction.id", ondelete="SET NULL"), index=True
    )
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL"), index=True
    )


class LoanRateChange(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "loan_rate_change"
    __table_args__ = (
        household_fk("loan_rate_change", "loan_id", "loan"),
        UniqueConstraint("loan_id", "effective_date", name="uq_loan_rate_change_loan_date"),
        CheckConstraint("new_rate >= 0 AND new_rate < 1", name="rate_fraction"),
        CheckConstraint(min_date_check("effective_date"), name="effective_date_min"),
    )

    loan_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    new_rate: Mapped[Decimal] = mapped_column(INTEREST_RATE, nullable=False)
