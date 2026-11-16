# SPDX-License-Identifier: AGPL-3.0-or-later
"""``subscription_event``: one thing that changed about a recurring series.

The series itself is a ``scheduled_transaction``
(``app.domain.transactions.models``). An event records, once per series,
kind and day, that it is new, changed price, was missed, ended, was charged
after a person ended it, is about to renew, or turned from a trial into a
paid plan (:data:`EVENT_KINDS`). It is ``open`` until a person has seen it.

Household-scoped (forced RLS). The series and the account are
household-consistent parents (the event goes with either); the charge that
showed the change is an optional reference, cleared when that row is
deleted. An event carries amounts, a date and ids only: never a name, a
merchant key or statement text.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Final

from sqlalchemy import CheckConstraint, Date, ForeignKey, String, UniqueConstraint, Uuid, text
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever this model is used.
import app.domain.accounts.models
import app.domain.transactions.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import AMOUNT, household_fk, household_ref_fk

#: What can change about a series.
EVENT_KINDS: Final = (
    "new",
    "price_changed",
    "missed",
    "ended",
    "charged_after_end",
    "renewal_due",
    "trial_converted",
)
#: ``open`` until a person has seen the event.
EVENT_STATES: Final = ("open", "seen")


def _in(column: str, values: tuple[str, ...]) -> str:
    """``column IN ('a', 'b')`` for a check constraint (fixed values from this module)."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class SubscriptionEvent(UUIDPk, Timestamps, HouseholdScoped, Base):
    """One change of one recurring series (module doc)."""

    __tablename__ = "subscription_event"
    __table_args__ = (
        household_fk("subscription_event", "schedule_id", "scheduled_transaction"),
        household_fk("subscription_event", "account_id", "account"),
        household_ref_fk("subscription_event", "transaction_id", "transaction"),
        UniqueConstraint(
            "household_id",
            "schedule_id",
            "kind",
            "occurred_on",
            name="uq_subscription_event_once",
        ),
        CheckConstraint(_in("kind", EVENT_KINDS), name="kind_known"),
        CheckConstraint(_in("state", EVENT_STATES), name="state_known"),
    )

    schedule_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    occurred_on: Mapped[date] = mapped_column(Date, nullable=False)
    amount_before: Mapped[Decimal | None] = mapped_column(AMOUNT)
    amount_after: Mapped[Decimal | None] = mapped_column(AMOUNT)
    #: The charge that showed the change, when there is one.
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transaction.id", ondelete="SET NULL"), index=True
    )
    state: Mapped[str] = mapped_column(
        String(8), nullable=False, default="open", server_default=text("'open'")
    )

    def __repr__(self) -> str:
        return f"SubscriptionEvent(id={self.id!s}, kind={self.kind})"
