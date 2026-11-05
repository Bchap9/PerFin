# SPDX-License-Identifier: AGPL-3.0-or-later
"""``mfa_challenge``: short-lived MFA ceremony state, bound to one session.

One row is an outstanding ceremony of a browser session:

* ``webauthn_register`` / ``webauthn_authenticate``: the random challenge the
  authenticator must sign (``challenge``). Consumed (deleted) before the
  response is verified, in its own transaction, so a challenge is usable
  exactly once even when the verification fails.
* ``totp_enroll``: a TOTP secret generated server-side that becomes an
  ``mfa_method`` only once the user proves it with a first code.
  ``secret_encrypted`` is an ``app.core.crypto`` envelope bound to this row
  (``column_ad("mfa_challenge", "secret_encrypted", id)``).

A session has at most one outstanding challenge per purpose (issuing a new
one replaces the old). Rows expire after a few minutes, die with their
session (``ON DELETE CASCADE``) and are pruned hourly
(``app/workers/handlers/mfa_maintenance.py``). Not household-scoped (no RLS),
reachable only through ``app.auth.mfa``.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, LargeBinary, String, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk


class ChallengePurpose(enum.StrEnum):
    WEBAUTHN_REGISTER = "webauthn_register"
    WEBAUTHN_AUTHENTICATE = "webauthn_authenticate"
    TOTP_ENROLL = "totp_enroll"


_PURPOSES = ", ".join(f"'{p.value}'" for p in ChallengePurpose)


class MfaChallenge(UUIDPk, Timestamps, Base):
    """An outstanding MFA ceremony of one session (see module doc)."""

    __tablename__ = "mfa_challenge"
    __table_args__ = (
        CheckConstraint(f"purpose IN ({_PURPOSES})", name="purpose_known"),
        CheckConstraint(
            "(purpose = 'totp_enroll') = (secret_encrypted IS NOT NULL)",
            name="secret_only_for_totp",
        ),
        CheckConstraint(
            "(purpose = 'totp_enroll') OR challenge IS NOT NULL", name="webauthn_has_challenge"
        ),
        Index("ix_mfa_challenge_session_purpose", "session_id", "purpose"),
        Index("ix_mfa_challenge_expires", "expires_at"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("user_session.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    challenge: Mapped[bytes | None] = mapped_column(LargeBinary)
    secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
