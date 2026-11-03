# SPDX-License-Identifier: AGPL-3.0-or-later
"""OIDC bookkeeping tables (AUTH.md "OIDC", SECURITY.md B1/B8).

Neither table is household-scoped (no RLS), like ``user_session``; both are
reached only through ``app.auth.oidc``.

* ``oidc_session``: what an OIDC login keeps beside its ``user_session``:
  the IdP ``sid`` (back-channel logout), the ID token for ``id_token_hint``
  (RP-initiated logout) and, only with claim refresh enabled, the refresh
  token. Both tokens are ``app.core.crypto`` envelopes bound to the row
  (``column_ad("oidc_session", <column>, id)``). ``session_id`` follows
  session rotation (``app.auth.oidc.logout.follow_rotation``) and the row
  dies with its session (``ON DELETE CASCADE``); the hourly ``oidc_prune``
  job drops rows whose session ended.
* ``oidc_used_state``: SHA-256 of every ``state`` value a callback consumed,
  until the pre-auth cookie that carried it has expired, so a state (and the
  cookie) works once (B1); and the domain-separated SHA-256 of every
  back-channel ``logout_token``'s ``(iss, jti)``, until the token is too old
  to pass validation, so a logout token works once (B8,
  ``app.auth.oidc.logout.consume_logout_jti``).
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, LargeBinary, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, Timestamps, UUIDPk


class OidcSession(UUIDPk, Timestamps, Base):
    """The OIDC side of one browser session."""

    __tablename__ = "oidc_session"
    __table_args__ = (
        Index("ix_oidc_session_issuer_sid", "issuer", "sid"),
        Index("ix_oidc_session_issuer_subject", "issuer", "subject"),
    )

    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("user_session.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="CASCADE"), nullable=False
    )
    issuer: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    sid: Mapped[str | None] = mapped_column(String(255))
    id_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    refresh_token_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary)
    claims_refreshed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class OidcUsedState(Base):
    """A consumed ``state`` or logout-token ``jti`` (SHA-256), kept while still valid."""

    __tablename__ = "oidc_used_state"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
