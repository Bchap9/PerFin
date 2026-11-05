# SPDX-License-Identifier: AGPL-3.0-or-later
"""Institutions, accounts and balance snapshots (DATA_MODEL.md "Ledger").

All three are ``HouseholdScoped`` (forced RLS). References between
household-scoped tables include ``household_id`` (``app.db.types``), so no row
can point at another household's account even though Postgres checks foreign
keys without RLS.

* ``institution``: household-scoped. ``builtin_key`` marks a row created from
  a shipped default (the Phase 3 fingerprint catalogue); user-created rows
  leave it NULL.
* ``account``: every account type of VISION.md. ``number_last4`` is identity
  data (SECURITY.md E1/H8): stored for statement matching, never logged and
  never put in audit rows or events. ``is_asset`` decides the side of the
  net-worth sheet (:func:`default_is_asset` gives the default per type).
  ``manual_balance`` accounts take their balance from ``balance_snapshot``
  rows only (property, vehicles, manual liabilities); transaction-based
  accounts add transactions to the latest snapshot
  (``app.domain.accounts.balances``). Visibility for members and viewers is
  ``account_grant`` (``app.domain.accounts.visibility``); owners and admins
  see every account. ``(household_id, id, currency)`` is unique so a
  transaction's and a snapshot's currency must equal the account's (and an
  account's currency cannot change while it has any).
* ``balance_snapshot``: the balance at the **end** of ``as_of_date``, from a
  statement, a manual entry or a computation. At most one per account, date
  and source. ``source_document_id`` is the statement ``document`` it came
  from (``SET NULL`` on delete, plus the deferred household-consistent check of
  ADR-0008).
"""

from __future__ import annotations

import enum
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

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

NAME_MAX = 200


class AccountType(enum.StrEnum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CREDIT_CARD = "credit_card"
    CASH = "cash"
    BROKERAGE = "brokerage"
    RETIREMENT = "retirement"
    LOAN = "loan"
    MORTGAGE = "mortgage"
    LINE_OF_CREDIT = "line_of_credit"
    MANUAL_ASSET = "manual_asset"
    MANUAL_LIABILITY = "manual_liability"


class AssetKind(enum.StrEnum):
    """Kind of a ``manual_asset`` account (NULL for every other type)."""

    PROPERTY = "property"
    VEHICLE = "vehicle"
    OTHER = "other"


class BalanceSource(enum.StrEnum):
    STATEMENT = "statement"
    COMPUTED = "computed"
    MANUAL = "manual"


#: Account types on the liability side by default.
LIABILITY_TYPES = frozenset(
    {
        AccountType.CREDIT_CARD,
        AccountType.LOAN,
        AccountType.MORTGAGE,
        AccountType.LINE_OF_CREDIT,
        AccountType.MANUAL_LIABILITY,
    }
)
#: Account types whose balance is entered by hand by default.
MANUAL_BALANCE_TYPES = frozenset({AccountType.MANUAL_ASSET, AccountType.MANUAL_LIABILITY})


def default_is_asset(account_type: AccountType) -> bool:
    return account_type not in LIABILITY_TYPES


def default_manual_balance(account_type: AccountType) -> bool:
    return account_type in MANUAL_BALANCE_TYPES


class Institution(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A bank, card issuer, broker or lender."""

    __tablename__ = "institution"
    __table_args__ = (
        household_key("institution"),
        UniqueConstraint("household_id", "name", name="uq_institution_household_name"),
        UniqueConstraint("household_id", "builtin_key", name="uq_institution_household_builtin"),
        CheckConstraint("country IS NULL OR country ~ '^[A-Z]{2}$'", name="country_format"),
        CheckConstraint("jsonb_typeof(fingerprint_rules) = 'object'", name="rules_object"),
    )

    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    country: Mapped[str | None] = mapped_column(CHAR(2))
    fingerprint_rules: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    default_parser: Mapped[str | None] = mapped_column(String(64))
    builtin_key: Mapped[str | None] = mapped_column(String(64))


class Account(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A financial account or a manually valued asset/liability."""

    __tablename__ = "account"
    __table_args__ = (
        household_key("account"),
        UniqueConstraint(
            "household_id", "id", "currency", name="uq_account_household_id_id_currency"
        ),
        household_ref_fk("account", "institution_id", "institution"),
        currency_check(),
        CheckConstraint(
            "number_last4 IS NULL OR number_last4 ~ '^[0-9]{4}$'", name="number_last4_digits"
        ),
        CheckConstraint(
            "(type = 'manual_asset') = (asset_kind IS NOT NULL)", name="asset_kind_for_manual_asset"
        ),
        CheckConstraint(
            "closed_on IS NULL OR opened_on IS NULL OR closed_on >= opened_on",
            name="closed_after_opened",
        ),
        CheckConstraint("subtype IS NULL OR subtype ~ '^[a-z0-9_]{1,40}$'", name="subtype_code"),
    )

    institution_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("institution.id", ondelete="SET NULL"), index=True
    )
    type: Mapped[AccountType] = mapped_column(pg_enum(AccountType, "account_type"), nullable=False)
    asset_kind: Mapped[AssetKind | None] = mapped_column(pg_enum(AssetKind, "asset_kind"))
    #: What kind of account within ``type`` (``roth_ira``, ``education_529``...;
    #: ``app.domain.accounts.subtypes``). A code, validated by the application.
    subtype: Mapped[str | None] = mapped_column(String(40))
    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    nickname: Mapped[str | None] = mapped_column(String(NAME_MAX))
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    number_last4: Mapped[str | None] = mapped_column(CHAR(4), info={"classification": "identity"})
    is_asset: Mapped[bool] = mapped_column(Boolean, nullable=False)
    manual_balance: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    include_in_net_worth: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    opened_on: Mapped[date | None] = mapped_column(Date)
    closed_on: Mapped[date | None] = mapped_column(Date)

    def __repr__(self) -> str:
        # Never include number_last4 or names (identity data; SECURITY.md H8).
        return f"Account(id={self.id!s}, type={self.type!s})"


class BalanceSnapshot(UUIDPk, Timestamps, HouseholdScoped, Base):
    """An account balance at the end of ``as_of_date``."""

    __tablename__ = "balance_snapshot"
    __table_args__ = (
        household_fk(
            "balance_snapshot", "account_id", "account", extra=(("currency", "currency"),)
        ),
        UniqueConstraint(
            "account_id", "as_of_date", "source", name="uq_balance_snapshot_account_date_source"
        ),
        Index("ix_balance_snapshot_account_date", "account_id", "as_of_date"),
        household_ref_fk("balance_snapshot", "source_document_id", "document"),
        currency_check(),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    as_of_date: Mapped[date] = mapped_column(Date, nullable=False)
    balance: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    source: Mapped[BalanceSource] = mapped_column(
        pg_enum(BalanceSource, "balance_source"), nullable=False
    )
    #: The statement the balance came from (``SET NULL`` when it is deleted).
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL"), index=True
    )


# ``balance_snapshot.source_document_id`` references ``document``, whose module
# imports this one: import it last so the FK target is always in the metadata.
import app.domain.documents.models  # noqa: E402, F401
