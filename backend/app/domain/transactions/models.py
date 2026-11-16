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
  engine (``app.domain.rules.schemas``); ``sort_order`` orders evaluation
  (``order`` is an SQL keyword) and ``stop_processing`` ends it after a match.
* **How a category was decided** is stored with it: ``category_source`` (one
  of :data:`CATEGORY_SOURCES`; NULL while nobody and nothing decided),
  ``category_confidence``, and ``category_rule_id`` for a rule's answer. A
  guess below the line is not a fact: it is kept in
  ``suggested_category_id`` only, and only while ``category_id`` is NULL
  (checked). ``merchant_key`` is the normalised identity of who was paid,
  derived from ``raw_payee`` (never shown to an AI client), and
  ``scheduled_transaction_id`` links a charge to its series. The system
  writes these six columns only through
  ``app.domain.categorisation.pinned.pinned_update``, which leaves
  ``updated_at`` where it was: the importer reads
  ``updated_at == created_at`` as "untouched since import".
* A schedule has a ``kind`` (:data:`SERIES_KINDS`) and a ``status``
  (:data:`SERIES_STATUSES`), each with who set it (:data:`DECIDERS`).
  ``active`` stays and is always ``status IN ('active', 'confirmed')``
  (checked), so code that reads the flag keeps working. A writer that sets
  only one of the two gets the other filled in (:func:`_status_default` on
  insert, :func:`_keep_status_and_active_together` on an ORM update): the
  flag switched off or on without a status is a person pausing or confirming
  the series; a status without the flag carries the flag with it.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

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
    event,
    inspect,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.engine.default import DefaultExecutionContext
from sqlalchemy.orm import Mapped, Mapper, mapped_column

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

MERCHANT_KEY_MAX = 64
#: A merchant key: lower-case ``a-z0-9`` tokens joined by single spaces.
MERCHANT_KEY_PATTERN: Final = "^[a-z0-9]+( [a-z0-9]+)*$"

#: How a transaction's category was decided, in the order they answer.
CATEGORY_SOURCES: Final = (
    "person",
    "file",
    "rule",
    "payee",
    "learned",
    "dictionary",
    "keyword",
    "legacy",
)
#: What a recurring series is.
SERIES_KINDS: Final = ("subscription", "bill", "income", "other")
#: Where a recurring series stands.
SERIES_STATUSES: Final = ("suggested", "active", "confirmed", "paused", "ended", "dismissed")
#: The statuses that count (``scheduled_transaction.active`` is true for exactly these).
ACTIVE_STATUSES: Final = ("active", "confirmed")
#: Who set a series' kind or status.
DECIDERS: Final = ("detector", "person")


def _in(column: str, values: tuple[str, ...]) -> str:
    """``column IN ('a', 'b')`` for a check constraint (fixed values from this module)."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


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
        household_ref_fk("transaction", "category_rule_id", "rule"),
        household_ref_fk("transaction", "suggested_category_id", "category"),
        household_ref_fk("transaction", "scheduled_transaction_id", "scheduled_transaction"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
        CheckConstraint(
            f"category_source IS NULL OR {_in('category_source', CATEGORY_SOURCES)}",
            name="category_source_known",
        ),
        CheckConstraint(
            "category_confidence IS NULL OR "
            "(category_confidence >= 0 AND category_confidence <= 1)",
            name="category_confidence_range",
        ),
        CheckConstraint(
            "suggested_category_id IS NULL OR category_id IS NULL",
            name="suggestion_only_without_category",
        ),
        Index("ix_transaction_account_posted", "account_id", "posted_date"),
        Index("ix_transaction_household_posted", "household_id", "posted_date"),
        Index("ix_transaction_account_import_hash", "account_id", "import_hash"),
        Index(
            "ix_transaction_transfer_group",
            "transfer_group_id",
            postgresql_where=text("transfer_group_id IS NOT NULL"),
        ),
        Index(
            "ix_transaction_household_merchant",
            "household_id",
            "merchant_key",
            postgresql_where=text("merchant_key IS NOT NULL"),
        ),
        Index(
            "ix_transaction_schedule",
            "scheduled_transaction_id",
            postgresql_where=text("scheduled_transaction_id IS NOT NULL"),
        ),
        Index(
            "ix_transaction_suggested",
            "household_id",
            "suggested_category_id",
            postgresql_where=text("suggested_category_id IS NOT NULL"),
        ),
        Index(
            "ix_transaction_category_rule",
            "category_rule_id",
            postgresql_where=text("category_rule_id IS NOT NULL"),
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
    # ---- how the category was decided (module doc) ------------------------
    #: Who was paid, normalised; derived from ``raw_payee``.
    merchant_key: Mapped[str | None] = mapped_column(String(MERCHANT_KEY_MAX))
    #: One of :data:`CATEGORY_SOURCES`; NULL while undecided.
    category_source: Mapped[str | None] = mapped_column(String(12))
    category_confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    #: The rule that set the category (``category_source = 'rule'``).
    category_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("rule.id", ondelete="SET NULL")
    )
    #: A guess below the line: shown as a suggestion, counted nowhere.
    suggested_category_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("category.id", ondelete="SET NULL")
    )
    #: The recurring series this charge belongs to.
    scheduled_transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("scheduled_transaction.id", ondelete="SET NULL")
    )


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
        household_key("rule"),
        CheckConstraint("hit_count >= 0", name="hit_count_non_negative"),
        Index("ix_rule_household_order", "household_id", "sort_order"),
    )

    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    #: Stop evaluating later rules once this one matches ("first match" for this rule).
    stop_processing: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    conditions: Mapped[Any] = mapped_column(JSONB, nullable=False)
    actions: Mapped[Any] = mapped_column(JSONB, nullable=False)
    hit_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def _is_manual(origin: object) -> bool:
    return getattr(origin, "value", origin) == ScheduleOrigin.MANUAL.value


def _status_for(active: object, origin: object) -> tuple[str, str]:
    """``(status, status_source)`` for a schedule written with only its ``active`` flag.

    The same reading revision ``0014`` gives the rows that existed before it:
    switched off is a person's pause, a schedule entered by hand is confirmed
    by the person who entered it, and a detected one is the detector's.
    """
    if active is False:
        return "paused", "person"
    if _is_manual(origin):
        return "confirmed", "person"
    return "active", "detector"


def _parameters(context: DefaultExecutionContext) -> dict[str, Any]:
    """The values of the row being inserted (given, or defaulted so far)."""
    params: dict[str, Any] = context.get_current_parameters()  # type: ignore[no-untyped-call]
    return params


def _status_default(context: DefaultExecutionContext) -> str:
    params = _parameters(context)
    return _status_for(params.get("active"), params.get("origin"))[0]


def _status_source_default(context: DefaultExecutionContext) -> str:
    params = _parameters(context)
    status, source = _status_for(params.get("active"), params.get("origin"))
    # ``status`` is in the parameters either way (given, or defaulted just
    # before this column): only the status this module would have chosen
    # carries its decider; any other was written by code that names its own.
    return source if params.get("status", status) == status else "detector"


def _active_default(context: DefaultExecutionContext) -> bool:
    status = _parameters(context).get("status")
    return status is None or status in ACTIVE_STATUSES


class ScheduledTransaction(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A recurring transaction: detected (subscriptions, salary) or entered by hand.

    The expected amount is ``amount_min..amount_max`` (equal for a fixed
    amount), in the account's currency. Due every ``interval_count`` ``cadence``
    units from ``next_due``.
    """

    __tablename__ = "scheduled_transaction"
    __table_args__ = (
        household_key("scheduled_transaction"),
        household_fk(
            "scheduled_transaction", "account_id", "account", extra=(("currency", "currency"),)
        ),
        household_ref_fk("scheduled_transaction", "payee_id", "payee"),
        household_ref_fk("scheduled_transaction", "category_id", "category"),
        currency_check(),
        CheckConstraint("amount_min <= amount_max", name="amount_range"),
        CheckConstraint("interval_count >= 1 AND interval_count <= 366", name="interval_range"),
        CheckConstraint(_in("kind", SERIES_KINDS), name="kind_known"),
        CheckConstraint(_in("status", SERIES_STATUSES), name="status_known"),
        CheckConstraint(
            f"{_in('kind_source', DECIDERS)} AND {_in('status_source', DECIDERS)}",
            name="deciders_known",
        ),
        CheckConstraint(
            f"active = ({_in('status', ACTIVE_STATUSES)})", name="active_matches_status"
        ),
        CheckConstraint("occurrences >= 0", name="occurrences_non_negative"),
        CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)", name="confidence_range"
        ),
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
    #: Always ``status IN ('active', 'confirmed')`` (checked; module doc).
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=_active_default, server_default=text("true")
    )
    last_seen_on: Mapped[date | None] = mapped_column(Date)
    # ---- what the series is and where it stands (module doc) --------------
    kind: Mapped[str] = mapped_column(
        String(12), nullable=False, default="other", server_default=text("'other'")
    )
    kind_source: Mapped[str] = mapped_column(
        String(8), nullable=False, default="detector", server_default=text("'detector'")
    )
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, default=_status_default, server_default=text("'active'")
    )
    status_source: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default=_status_source_default,
        server_default=text("'detector'"),
    )
    #: The merchant the series was found for (``transaction.merchant_key``).
    merchant_key: Mapped[str | None] = mapped_column(String(MERCHANT_KEY_MAX))
    first_seen_on: Mapped[date | None] = mapped_column(Date)
    #: Charges seen so far.
    occurrences: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    confidence: Mapped[Decimal | None] = mapped_column(CONFIDENCE)
    #: The latest charge, and the one before a price change.
    last_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    previous_amount: Mapped[Decimal | None] = mapped_column(AMOUNT)
    price_changed_on: Mapped[date | None] = mapped_column(Date)
    ended_on: Mapped[date | None] = mapped_column(Date)


@event.listens_for(ScheduledTransaction, "before_update")
def _keep_status_and_active_together(
    mapper: Mapper[ScheduledTransaction], connection: object, row: ScheduledTransaction
) -> None:
    """An ORM update of only ``active`` or only ``status`` carries the other (module doc)."""
    attrs = inspect(row).attrs
    active_changed = attrs.active.history.has_changes()
    status_changed = attrs.status.history.has_changes()
    if active_changed and not status_changed:
        if row.active != (row.status in ACTIVE_STATUSES):
            row.status = "confirmed" if row.active else "paused"
            row.status_source = "person"
    elif status_changed and not active_changed:
        row.active = row.status in ACTIVE_STATUSES
