# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exchange rates (DATA_MODEL.md "Ledger": ``fx_rate``).

One row says: on ``rate_date``, one unit of ``base`` was worth ``rate``
units of ``quote``. Rates are ``HouseholdScoped`` (entered by hand or fetched
by a provider integration for that household), at most one per household,
pair and date. ``source`` is ``manual`` or the provider's name. Conversion
rules are in ``app.domain.fx.service``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.core.dates import min_date_check
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import CURRENCY, RATE, currency_check

SOURCE_MAX = 64
MANUAL_SOURCE = "manual"


class FxRate(UUIDPk, Timestamps, HouseholdScoped, Base):
    __tablename__ = "fx_rate"
    __table_args__ = (
        # Also the index for "latest rate on or before a date".
        UniqueConstraint("household_id", "base", "quote", "rate_date", name="uq_fx_rate_pair_date"),
        currency_check("base"),
        currency_check("quote"),
        CheckConstraint("base <> quote", name="distinct_pair"),
        CheckConstraint("rate > 0", name="rate_positive"),
        CheckConstraint(min_date_check("rate_date"), name="rate_date_min"),
    )

    base: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    quote: Mapped[str] = mapped_column(CURRENCY, nullable=False)
    rate_date: Mapped[date] = mapped_column(Date, nullable=False)
    rate: Mapped[Decimal] = mapped_column(RATE, nullable=False)
    source: Mapped[str] = mapped_column(String(SOURCE_MAX), nullable=False, default=MANUAL_SOURCE)
