# SPDX-License-Identifier: AGPL-3.0-or-later
"""Password reset by email (AUTH.md "Brute force and enumeration", SECURITY.md A2/A11).

* Offered only when SMTP is configured (``PERFIN_SMTP_HOST`` and
  ``PERFIN_SMTP_FROM``) and local login is enabled (OIDC-only mode disables
  the local reset, AUTH.md). :func:`reset_available`.
* ``POST /auth/password-reset/request`` answers the same ``202`` whatever the
  identifier; the lookup, token creation and mail happen in
  :func:`process_reset_request` *after* the response is sent (a background
  task), so neither the body nor the timing says whether an account exists.
* The token is 256 random bits, valid for :data:`RESET_TTL` (15 minutes) and
  once; only its SHA-256 is stored (``password_reset_token``). A new request
  invalidates the user's earlier unused tokens.
* The emailed link is ``<PERFIN_PUBLIC_URL>/reset-password#token=<token>``:
  the token is in the URL **fragment**, which browsers never send to a
  server (no access log, no ``Referer``); the reset page posts it in the
  request body to ``POST /auth/password-reset/confirm``. Every response
  carries ``Referrer-Policy: no-referrer`` (``app.core.headers``).
* Confirming sets the new password (same policy as any new password),
  consumes the token, clears the lockout and revokes every session of the
  user. Break-glass accounts, inactive accounts and accounts without an email
  address are never sent a reset.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.auth.local import audit_auth, set_password, validate_new_password
from app.auth.models import Credential, PasswordResetToken, User, normalize_username
from app.auth.sessions import revoke_all_sessions
from app.core import clock
from app.core.audit import RequestMeta
from app.core.logging import get_logger
from app.core.settings import Settings
from app.integrations.notifications.smtp import Mailer, MailError

log = get_logger(__name__)

RESET_TTL: Final = timedelta(minutes=15)
TOKEN_BYTES: Final = 32
RESET_PATH: Final = "/reset-password"
#: At most this many accounts are mailed for one identifier (shared email).
MAX_RECIPIENTS: Final = 5
SUBJECT: Final = "PerFin password reset"


class InvalidResetToken(Exception):  # noqa: N818 - an outcome
    """Unknown, expired, used, or the account cannot be reset."""


def reset_available(settings: Settings) -> bool:
    return settings.smtp_configured and settings.local_login_enabled


def hash_reset_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def reset_link(settings: Settings, token: str) -> str:
    return f"{settings.public_url.rstrip('/')}{RESET_PATH}#token={token}"


def _message(settings: Settings, user: User, token: str) -> str:
    minutes = int(RESET_TTL.total_seconds() // 60)
    return (
        f"Hello {user.display_name},\n\n"
        "Someone asked to reset the password of your PerFin account "
        f"({user.username}).\n\n"
        f"To choose a new password, open this link within {minutes} minutes:\n\n"
        f"{reset_link(settings, token)}\n\n"
        "The link works once. If you did not ask for this, ignore this email; "
        "your password stays unchanged.\n"
    )


def _eligible_users(db: Session, identifier: str) -> list[User]:
    """Active local accounts (not break-glass) with an email, by username or email."""
    name = normalize_username(identifier)
    rows = db.execute(
        select(User)
        .join(Credential, Credential.user_id == User.id)
        .where(
            or_(User.username == name, func.lower(User.email) == name),
            User.is_active.is_(True),
            User.is_breakglass.is_(False),
            User.email.is_not(None),
        )
        .order_by(User.id)
        .limit(MAX_RECIPIENTS)
    ).scalars()
    return list(rows)


@dataclass(frozen=True)
class _Outgoing:
    to: str
    body: str


def process_reset_request(
    factory: sessionmaker[Session],
    settings: Settings,
    mailer: Mailer | None,
    *,
    identifier: str,
    ip: str | None,
    meta: RequestMeta,
    now: datetime | None = None,
) -> int:
    """Create tokens and mail them; return how many mails were attempted.

    Runs after the ``202`` was sent. Never raises (failures are logged
    without addresses or tokens).
    """
    if mailer is None or not reset_available(settings):
        return 0
    now = now or clock.utcnow()
    outgoing: list[_Outgoing] = []
    try:
        with factory() as db, db.begin():
            for user in _eligible_users(db, identifier):
                if user.email is None:  # pragma: no cover - filtered in the query
                    continue
                # One live token per user: earlier unused ones stop working.
                db.execute(
                    delete(PasswordResetToken).where(
                        PasswordResetToken.user_id == user.id,
                        PasswordResetToken.used_at.is_(None),
                    )
                )
                token = secrets.token_urlsafe(TOKEN_BYTES)
                db.add(
                    PasswordResetToken(
                        token_hash=hash_reset_token(token),
                        user_id=user.id,
                        expires_at=now + RESET_TTL,
                        requested_ip=ip,
                        created_at=now,
                        updated_at=now,
                    )
                )
                db.flush()
                audit_auth(db, action="user.password_reset_requested", user_id=user.id, meta=meta)
                outgoing.append(_Outgoing(user.email, _message(settings, user, token)))
    except Exception as exc:  # noqa: BLE001 - background task: log, never raise
        log.error("password reset request failed", extra={"error": type(exc).__name__})
        return 0
    for mail in outgoing:
        try:
            mailer.send(to=mail.to, subject=SUBJECT, body=mail.body)
        except MailError as exc:
            log.warning("password reset mail not delivered", extra={"error": str(exc)})
    return len(outgoing)


def confirm_reset(
    db: Session,
    settings: Settings,
    *,
    token: str,
    new_password: str,
    meta: RequestMeta,
    now: datetime | None = None,
) -> uuid.UUID:
    """Set a new password with a valid token; return the user id.

    Raises :class:`InvalidResetToken` (token not consumed) or
    ``PasswordPolicyError`` (token not consumed; the user may try another
    password). Runs in the caller's transaction.
    """
    now = now or clock.utcnow()
    if not reset_available(settings) or not token or len(token) > 256:
        raise InvalidResetToken
    row = db.execute(
        select(PasswordResetToken)
        .where(PasswordResetToken.token_hash == hash_reset_token(token))
        .with_for_update()
    ).scalar_one_or_none()
    if row is None or row.used_at is not None or row.expires_at <= now:
        raise InvalidResetToken
    user = db.get(User, row.user_id)
    if user is None or not user.is_active or user.is_breakglass:
        raise InvalidResetToken
    validate_new_password(new_password, settings, username=user.username)
    set_password(db, user.id, new_password, now=now)
    row.used_at = now
    row.updated_at = now
    db.execute(
        update(User)
        .where(User.id == user.id)
        .values(failed_login_count=0, locked_until=None, updated_at=now)
        .execution_options(synchronize_session="fetch")
    )
    revoked = revoke_all_sessions(db, user.id, reason="password_reset", now=now)
    audit_auth(
        db,
        action="user.password_reset",
        user_id=user.id,
        meta=meta,
        after={"sessions_revoked": revoked},
    )
    db.flush()
    return user.id


def prune_reset_tokens(db: Session, *, older_than: timedelta, now: datetime | None = None) -> int:
    """Delete tokens that expired or were used more than ``older_than`` ago."""
    now = now or clock.utcnow()
    cutoff = now - older_than
    result = db.execute(
        delete(PasswordResetToken).where(
            or_(PasswordResetToken.expires_at < cutoff, PasswordResetToken.used_at < cutoff)
        )
    )
    return int(result.rowcount)  # type: ignore[attr-defined]
