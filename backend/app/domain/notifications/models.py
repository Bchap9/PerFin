# SPDX-License-Identifier: AGPL-3.0-or-later
"""``notification_channel``: where a household's notifications go (DATA_MODEL.md).

* ``config_encrypted`` is one envelope under the master key bound to its
  column and row (``app.core.crypto.column_ad("notification_channel",
  "config_encrypted", id)``, SECURITY.md D2): the whole channel
  configuration as JSON, destination and credentials alike (SMTP password,
  webhook signing secret, ntfy topic and token, Gotify application token,
  Matrix access token). A ciphertext copied into another row does not
  decrypt.
* ``events`` lists the notification kinds the channel receives
  (``app.domain.notifications.routing.KINDS``).
* ``last_outcome``/``last_sent_at`` record the last delivery attempt that
  finished (an outcome code from ``app.integrations.notifications.base``).

Household-scoped (forced RLS). Never exported (secrets, per-instance
destinations).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Final

from sqlalchemy import Boolean, CheckConstraint, DateTime, LargeBinary, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import pg_enum
from app.integrations.notifications.base import ChannelType

SECRET: Final[dict[str, Any]] = {"classification": "secret"}
NAME_MAX: Final = 100
OUTCOME_MAX: Final = 32
#: Most channels per household.
CHANNELS_MAX: Final = 20


class NotificationChannel(UUIDPk, Timestamps, HouseholdScoped, Base):
    """One delivery channel of a household."""

    __tablename__ = "notification_channel"
    __table_args__ = (
        CheckConstraint("jsonb_typeof(events) = 'array'", name="events_array"),
        CheckConstraint("char_length(name) >= 1", name="name_not_empty"),
    )

    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    type: Mapped[ChannelType] = mapped_column(
        pg_enum(ChannelType, "notification_channel_type"), nullable=False
    )
    config_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, info=SECRET)
    events: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outcome: Mapped[str | None] = mapped_column(String(OUTCOME_MAX))

    def __repr__(self) -> str:
        return f"NotificationChannel(id={self.id!s}, type={self.type}, enabled={self.enabled})"
