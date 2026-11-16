# SPDX-License-Identifier: AGPL-3.0-or-later
"""``merchant_memory``: what a household's own corrections say about a merchant.

One row per household, merchant key, direction (money ``out`` or ``in``) and
category, with the number of person-decided transactions behind it
(``decisions``) and when the latest was decided. The answer for a key is
worked out from its rows (:mod:`app.domain.categorisation.memory`).

It is a table and not a query over the ledger so that it survives deleting
and re-importing a statement. Household-scoped (forced RLS); the category
reference is household-consistent and goes with the category. The key is
derived from raw statement text: it is never logged, audited or shown to an
AI client.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Integer, String, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever this model is used.
import app.domain.transactions.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import household_fk
from app.domain.transactions.models import MERCHANT_KEY_MAX, MERCHANT_KEY_PATTERN


class MerchantMemory(UUIDPk, Timestamps, HouseholdScoped, Base):
    """How often this household filed a merchant under one category (module doc)."""

    __tablename__ = "merchant_memory"
    __table_args__ = (
        household_fk("merchant_memory", "category_id", "category"),
        UniqueConstraint(
            "household_id",
            "merchant_key",
            "direction",
            "category_id",
            name="uq_merchant_memory_choice",
        ),
        CheckConstraint(f"merchant_key ~ '{MERCHANT_KEY_PATTERN}'", name="merchant_key_form"),
        CheckConstraint("direction IN ('out', 'in')", name="direction_known"),
        CheckConstraint("decisions >= 1", name="decisions_positive"),
    )

    merchant_key: Mapped[str] = mapped_column(String(MERCHANT_KEY_MAX), nullable=False)
    #: ``out`` for money leaving the account, ``in`` for money arriving.
    direction: Mapped[str] = mapped_column(String(3), nullable=False)
    category_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    decisions: Mapped[int] = mapped_column(Integer, nullable=False)
    last_decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __repr__(self) -> str:
        # Never the merchant key (derived from statement text).
        return f"MerchantMemory(id={self.id!s}, decisions={self.decisions})"
