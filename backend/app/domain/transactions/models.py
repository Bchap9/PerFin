# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transactions, splits, payees, categories, tags, rules and schedules.

DATA_MODEL.md "Ledger". Every table is ``HouseholdScoped`` (forced RLS) and
every reference between them includes ``household_id``
(``app.db.types.household_fk`` / ``household_ref_fk``), so a row can never
point at another household's account, payee, category, tag or transaction.

Conventions the services rely on:

* ``transaction.amount`` is signed from the account's point of view:
  positive increases the account balance (a deposit, a refund, a payment
  *to* a credit card), negative decreases it (a purchase, a withdrawal).
  A liability's balance is therefore normally negative.
* ``transaction.currency`` always equals the account's currency (composite
  foreign key ``(household_id, account_id, currency)``); a foreign-currency
  purchase is recorded in the account currency.
* ``transaction.category_id`` categorises an unsplit transaction. A split
  transaction has ``category_id`` NULL and ``transaction_split`` rows whose
  amounts add up to the transaction amount (checked by the service that
  writes splits). Reports read "category lines": the splits when there are
  any, else the transaction itself.
* ``transfer_group_id`` pairs the two legs of a transfer.
* ``source_document_id`` is the ``document`` the row was imported from
  (``SET NULL`` when the document is deleted, household-consistent through
  ``household_ref_fk``).
* ``import_hash`` is :func:`app.domain.transactions.import_hash.import_hash`
  (indexed with the account, not unique: a duplicate goes to the review
  queue, it is not refused).
* ``raw_payee``, ``memo`` and ``notes`` are free text from statements and
  users (column info ``classification: free_text``): rendered escaped, never
  logged, scrubbed before any AI-facing view.
* Categories form a tree through ``parent_id``. A parent with children
  cannot be deleted (composite ``NO ACTION`` key); the service re-parents or
  deletes the children first. ``system_key`` identifies the default set
  (``app.domain.transactions.categories``).
* ``rule.conditions``/``rule.actions`` are JSON documents owned by the rules
  engine; ``sort_order`` orders evaluation (``order`` is an SQL keyword).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.domain.accounts.models
import app.domain.documents.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import (
    AMOUNT,
    CONFIDENCE,
    CURRENCY,
    currency_check,
    household_fk,
    household_key,
    household_ref_fk,
    pg_enum,
)

NAME_MAX = 200
PAYEE_MAX = 500
MEMO_MAX = 1000
NOTES_MAX = 4000
ALIASES_MAX = 100

FREE_TEXT: dict[str, Any] = {"classification": "free_text"}


class CategoryKind(enum.StrEnum):
    EXPENSE = "expense"
    INCOME = "income"
    TRANSFER = "transfer"
    IGNORE = "ignore"


class TaxTreatment(enum.StrEnum):
    """Hint for the tax vault; NULL means not tax relevant / unknown."""

    TAXABLE_INCOME = "taxable_income"
    TAX_EXEMPT_INCOME = "tax_exempt_income"
    DEDUCTIBLE = "deductible"
    TAX_PAYMENT = "tax_payment"


class TransactionStatus(enum.StrEnum):
    PENDING = "pending"
    CLEARED = "cleared"
    RECONCILED = "reconciled"


class ReviewState(enum.StrEnum):
    NONE = "none"
    PENDING = "pending"
    CONFIRMED = "confirmed"


class Cadence(enum.StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class ScheduleOrigin(enum.StrEnum):
    DETECTED = "detected"
    MANUAL = "manual"


# ---------------------------------------------------------------------------
# Categories, payees, tags
# ---------------------------------------------------------------------------


class Category(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "category"
    __table_args__ = (
        household_key("category"),
        household_fk("category", "parent_id", "category", ondelete=None),
        UniqueConstraint(
            "household_id",
            "parent_id",
            "name",
            name="uq_category_household_parent_name",
            postgresql_nulls_not_distinct=True,
        ),
        UniqueConstraint("household_id", "system_key", name="uq_category_household_system_key"),
        CheckConstraint("parent_id IS NULL OR parent_id <> id", name="not_own_parent"),
    )

    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), index=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    kind: Mapped[CategoryKind] = mapped_column(
        pg_enum(CategoryKind, "category_kind"), nullable=False
    )
    tax_treatment: Mapped[TaxTreatment | None] = mapped_column(
        pg_enum(TaxTreatment, "tax_treatment")
    )
    system_key: Mapped[str | None] = mapped_column(String(64))


class Payee(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A canonical payee; ``normalized_name`` is ``normalize_payee(name)``."""

    __tablename__ = "payee"
    __table_args__ = (
        household_key("payee"),
        household_ref_fk("payee", "default_category_id", "category"),
        UniqueConstraint("household_id", "normalized_name", name="uq_payee_household_normalized"),
        CheckConstraint(f"cardinality(aliases) <= {ALIASES_MAX}", name="aliases_bounded"),
    )

    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    normalized_name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    aliases: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, default=list, server_default=text("'{}'::text[]")
    )
    default_category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("category.id", ondelete="SET NULL"), index=True
    )


class Tag(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "tag"
    __table_args__ = (
        household_key("tag"),
        UniqueConstraint("household_id", "name", name="uq_tag_household_name"),
    )

    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)


# ---------------------------------------------------------------------------
# Transactions
# ---------------------------------------------------------------------------


class Transaction(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "transaction"
    __table_args__ = (
        household_key("transaction"),
        household_fk("transaction", "account_id", "account", extra=(("currency", "currency"),)),
        household_ref_fk("transaction", "payee_id", "payee"),
        household_ref_fk("transaction", "category_id", "category"),
        household_ref_fk("transaction", "source_document_id", "document"),
        currency_check(),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        Index("ix_transaction_account_posted", "account_id", "posted_date"),
        Index("ix_transaction_household_posted", "household_id", "posted_date"),
        Index("ix_transaction_account_import_hash", "account_id", "import_hash"),
        Index(
            "ix_transaction_transfer_group",
            "transfer_group_id",
            postgresql_where=text("transfer_group_id IS NOT NULL"),
        ),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    posted_date: Mapped[date] = mapped_column(Date, nullable=False)
    effective_date: Mapped[date | None] = mapped_column(Date)
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    payee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("payee.id", ondelete="SET NULL"), index=True
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("category.id", ondelete="SET NULL"), index=True
    )
    raw_payee: Mapped[str | None] = mapped_column(String(PAYEE_MAX), info=FREE_TEXT)
    memo: Mapped[str | None] = mapped_column(String(MEMO_MAX), info=FREE_TEXT)
    notes: Mapped[str | None] = mapped_column(String(NOTES_MAX), info=FREE_TEXT)
    status: Mapped[TransactionStatus] = mapped_column(
        pg_enum(TransactionStatus, "transaction_status"),
        nullable=False,
        default=TransactionStatus.CLEARED,
        server_default=text("'cleared'"),
    )
    transfer_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL"), index=True
    )
    import_hash: Mapped[str | None] = mapped_column(String(64))
    review_state: Mapped[ReviewState] = mapped_column(
        pg_enum(ReviewState, "review_state"),
        nullable=False,
        default=ReviewState.NONE,
        server_default=text("'none'"),
    )
    confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)


class TransactionSplit(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "transaction_split"
    __table_args__ = (
        household_fk("transaction_split", "transaction_id", "transaction"),
        household_ref_fk("transaction_split", "category_id", "category"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("category.id", ondelete="SET NULL"), index=True
    )
    amount: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    memo: Mapped[str | None] = mapped_column(String(MEMO_MAX), info=FREE_TEXT)
    tax_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )


class TransactionTag(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "transaction_tag"
    __table_args__ = (
        household_fk("transaction_tag", "transaction_id", "transaction"),
        household_fk("transaction_tag", "tag_id", "tag"),
        UniqueConstraint("transaction_id", "tag_id", name="uq_transaction_tag_pair"),
    )

    transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tag_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)


# ---------------------------------------------------------------------------
# Rules and scheduled transactions
# ---------------------------------------------------------------------------


class Rule(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "rule"
    __table_args__ = (
        CheckConstraint("hit_count >= 0", name="hit_count_non_negative"),
        Index("ix_rule_household_order", "household_id", "sort_order"),
    )

    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    conditions: Mapped[Any] = mapped_column(JSONB, nullable=False)
    actions: Mapped[Any] = mapped_column(JSONB, nullable=False)
    hit_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduledTransaction(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A recurring transaction: detected (subscriptions, salary) or entered by hand.

    The expected amount is ``amount_min..amount_max`` (equal for a fixed
    amount), in the account's currency. Due every ``interval_count`` ``cadence``
    units from ``next_due``.
    """

    __tablename__ = "scheduled_transaction"
    __table_args__ = (
        household_fk(
            "scheduled_transaction", "account_id", "account", extra=(("currency", "currency"),)
        ),
        household_ref_fk("scheduled_transaction", "payee_id", "payee"),
        household_ref_fk("scheduled_transaction", "category_id", "category"),
        currency_check(),
        CheckConstraint("amount_min <= amount_max", name="amount_range"),
        CheckConstraint("interval_count >= 1 AND interval_count <= 366", name="interval_range"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    cadence: Mapped[Cadence] = mapped_column(pg_enum(Cadence, "schedule_cadence"), nullable=False)
    interval_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    next_due: Mapped[date] = mapped_column(Date, nullable=False)
    amount_min: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    amount_max: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    payee_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("payee.id", ondelete="SET NULL"), index=True
    )
    category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("category.id", ondelete="SET NULL"), index=True
    )
    origin: Mapped[ScheduleOrigin] = mapped_column(
        pg_enum(ScheduleOrigin, "schedule_origin"), nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_seen_on: Mapped[date | None] = mapped_column(Date)
