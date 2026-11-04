# SPDX-License-Identifier: AGPL-3.0-or-later
"""Email intake state (INGESTION.md "Email intake specifics", SECURITY.md D1, D2).

* ``mail_intake``: one row per household, the receipts mailbox the worker
  polls. ``username_encrypted``, ``password_encrypted`` and
  ``alias_encrypted`` are envelopes under the master key bound to their
  column and row (``app.core.crypto.column_ad("mail_intake", <column>,
  id)``, D2), so a ciphertext copied into another row or column does not
  decrypt. Host, port and folder names are plain configuration.
  ``allowed_senders`` lists extra sender addresses, each mapped to the
  member whose documents its mail becomes (``[{"address", "user_id"}]``);
  with ``member_addresses`` the members' own addresses are allowed too.
* ``mail_intake_message``: the message ledger, one row per message the
  poller decided on, keyed by ``message_key`` (SHA-256 over the
  ``Message-ID`` and ``From`` header, or over the whole header block when
  there is no ``Message-ID``). It is the dedupe index (a second copy of a
  message is never imported again) and the commit point of the two-phase
  move: a message is moved out of the inbox only by a run that finds it
  already recorded (``app.ingestion.intake.email``).

Both tables are household-scoped (forced RLS).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

# Referenced tables must be in the metadata wherever these models are used.
import app.domain.documents.models  # noqa: F401
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import household_ref_fk

SECRET: dict[str, Any] = {"classification": "secret"}
FREE_TEXT: dict[str, Any] = {"classification": "free_text"}

HOST_MAX = 253
FOLDER_MAX = 200
DEFAULT_PORT = 993
DEFAULT_INBOX = "INBOX"
DEFAULT_PROCESSED = "PerFin/Processed"
DEFAULT_REJECTED = "PerFin/Rejected"

OUTCOMES = ("accepted", "rejected")


class MailIntake(UUIDPk, Timestamps, HouseholdScoped, Base):
    """The household's receipts mailbox and its acceptance policy."""

    __tablename__ = "mail_intake"
    __table_args__ = (
        UniqueConstraint("household_id", name="uq_mail_intake_household"),
        CheckConstraint("port BETWEEN 1 AND 65535", name="port_range"),
        CheckConstraint("jsonb_typeof(allowed_senders) = 'array'", name="allowed_senders_array"),
        CheckConstraint(
            "jsonb_typeof(last_poll_counts) = 'object'", name="last_poll_counts_object"
        ),
        CheckConstraint(
            "NOT enabled OR (host IS NOT NULL AND username_encrypted IS NOT NULL"
            " AND password_encrypted IS NOT NULL)",
            name="enabled_needs_credentials",
        ),
    )

    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    host: Mapped[str | None] = mapped_column(String(HOST_MAX))
    port: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_PORT, server_default=text(str(DEFAULT_PORT))
    )
    username_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, info=SECRET)
    password_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary, info=SECRET)
    inbox_folder: Mapped[str] = mapped_column(
        String(FOLDER_MAX), nullable=False, default=DEFAULT_INBOX
    )
    processed_folder: Mapped[str] = mapped_column(
        String(FOLDER_MAX), nullable=False, default=DEFAULT_PROCESSED
    )
    rejected_folder: Mapped[str] = mapped_column(
        String(FOLDER_MAX), nullable=False, default=DEFAULT_REJECTED
    )
    alias_encrypted: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, info=SECRET)
    alias_rotated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    allowed_senders: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    member_addresses: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    require_auth_results: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("true")
    )
    # Only Authentication-Results headers added by this server are read.
    authserv_id: Mapped[str | None] = mapped_column(String(HOST_MAX))
    last_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_poll_outcome: Mapped[str | None] = mapped_column(String(32))
    last_poll_counts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    def __repr__(self) -> str:
        return f"MailIntake(id={self.id!s}, enabled={self.enabled})"


class MailIntakeMessage(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A message the poller decided on (accepted or rejected)."""

    __tablename__ = "mail_intake_message"
    __table_args__ = (
        UniqueConstraint("household_id", "message_key", name="uq_mail_intake_message_key"),
        household_ref_fk("mail_intake_message", "document_id", "document"),
        CheckConstraint("message_key ~ '^[0-9a-f]{64}$'", name="message_key_hex"),
        CheckConstraint("outcome IN ('accepted', 'rejected')", name="outcome_known"),
    )

    message_key: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(32))
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("document.id", ondelete="SET NULL"), index=True
    )

    def __repr__(self) -> str:
        return f"MailIntakeMessage(id={self.id!s}, outcome={self.outcome})"
