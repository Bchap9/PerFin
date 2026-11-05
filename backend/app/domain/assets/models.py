# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manually valued assets and their valuations (DATA_MODEL.md "Manual assets and liabilities").

* ``manual_asset``: the details of an ``account`` of type ``manual_asset``
  (one row per account; the service checks the type). The asset's kind
  (property, vehicle, other) is ``account.asset_kind``, which already exists
  for every manual asset, so it is not repeated here. ``description`` is
  identity-sensitive free text (it may hold a property's address; column
  info ``classification: identity``, so it is on the AI denylist and never
  audited or put in events, AI_ACCESS.md); ``valuation_method`` says how the values are obtained.
* ``asset_valuation``: the value of a manual asset (or a manual liability)
  account on a date, in the account currency, from a manual entry or a
  provider. ``value`` is a non-negative magnitude; ``account.is_asset``
  decides the side of the sheet (a liability of 100 has ``value`` 100 and a
  balance of -100). At most one per account, date and source; on one date
  ``manual`` beats ``provider``. Net worth reads the latest valuation on or
  before the day for a ``manual_balance`` account that has any, else its
  ``balance_snapshot`` (``app.domain.networth.service``).

Manual liabilities need no extra table: they are ``manual_liability``
accounts valued by ``asset_valuation`` or ``balance_snapshot`` rows.
Both tables are ``HouseholdScoped`` (forced RLS) with household-consistent
references; they are visible exactly when their account is.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, Index, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.domain.accounts.models  # noqa: F401
from app.core.dates import min_date_check
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import AMOUNT, CURRENCY, currency_check, household_fk, pg_enum

DESCRIPTION_MAX = 1000


class ValuationMethod(enum.StrEnum):
    MANUAL = "manual"
    APPRAISAL = "appraisal"
    PURCHASE_PRICE = "purchase_price"
    INDEX = "index"
    PROVIDER = "provider"


class ValuationSource(enum.StrEnum):
    MANUAL = "manual"
    PROVIDER = "provider"


class ManualAsset(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "manual_asset"
    __table_args__ = (
        household_fk("manual_asset", "account_id", "account"),
        UniqueConstraint("account_id", name="uq_manual_asset_account"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    description: Mapped[str | None] = mapped_column(
        String(DESCRIPTION_MAX), info={"classification": "identity"}
    )
    valuation_method: Mapped[ValuationMethod] = mapped_column(
        pg_enum(ValuationMethod, "valuation_method"), nullable=False
    )


class AssetValuation(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "asset_valuation"
    __table_args__ = (
        household_fk("asset_valuation", "account_id", "account", extra=(("currency", "currency"),)),
        UniqueConstraint(
            "account_id", "valued_on", "source", name="uq_asset_valuation_account_date_source"
        ),
        Index("ix_asset_valuation_account_date", "account_id", "valued_on"),
        currency_check(),
        CheckConstraint("value >= 0", name="value_non_negative"),
        CheckConstraint(min_date_check("valued_on"), name="valued_on_min"),
    )

    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    valued_on: Mapped[date] = mapped_column(Date, nullable=False)
    value: Mapped[Decimal] = mapped_column(AMOUNT, nullable=False)
    currency: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    source: Mapped[ValuationSource] = mapped_column(
        pg_enum(ValuationSource, "valuation_source"), nullable=False
    )
