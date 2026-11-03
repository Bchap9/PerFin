# SPDX-License-Identifier: AGPL-3.0-or-later
"""OIDC session bookkeeping and logout (AUTH.md "Logout"; SECURITY.md B4, B8).

* :func:`record_oidc_session` stores, next to a new OIDC ``user_session``,
  only what logout and claim refresh need (``oidc_session``): the IdP ``sid``
  and ``sub``; the ID token, encrypted, as the future ``id_token_hint`` (only
  when the IdP has an ``end_session_endpoint``); the refresh token,
  encrypted, only with ``PERFIN_OIDC_CLAIM_REFRESH_MINUTES`` > 0. The access
  token is never stored.
* :func:`follow_rotation` is registered as a session rotation hook, so the
  row always points at the session's current id (MFA completion, step-up,
  household switch, password change).
* **RP-initiated logout** (:func:`rp_logout`): the session is revoked, its
  ``oidc_session`` row (and the tokens in it) deleted, and the browser is sent
  to the IdP's ``end_session_endpoint`` with ``id_token_hint``, ``client_id``
  and a ``post_logout_redirect_uri`` that must equal one entry of
  ``PERFIN_OIDC_POST_LOGOUT_REDIRECT_URIS`` (default ``<PERFIN_PUBLIC_URL>/``).
* **Back-channel logout** (:func:`revoke_for_logout_token`): a validated
  ``logout_token`` revokes every session of that issuer whose ``sid`` matches
  (when the token has a ``sid``) and whose subject matches (when it has a
  ``sub``). Each token works once (:func:`consume_logout_jti`, review
  P1-SR-03): the SHA-256 of ``(iss, jti)``, domain-separated from the
  login ``state`` hashes that share the ``oidc_used_state`` table, is
  recorded until the token could no longer pass the ``iat`` age check, so a
  captured token cannot be replayed to end sessions opened after the
  original logout. It is recorded in the **same transaction** as the
  revocation (review P1-SR2-03): if revoking fails, the ``jti`` is rolled
  back with it and the IdP's retry of the same token still works.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import delete, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.auth.models import UserSession
from app.auth.oidc.models import OidcSession, OidcUsedState
from app.auth.oidc.tokens import CLOCK_SKEW, MAX_TOKEN_AGE, ValidatedToken
from app.auth.sessions import is_live, register_rotation_hook, revoke
from app.core import clock
from app.core.crypto import DecryptionError, Keyring, column_ad
from app.core.ids import uuid7
from app.core.settings import Settings


def id_token_ad(row_id: uuid.UUID) -> bytes:
    return column_ad("oidc_session", "id_token_encrypted", row_id)


def refresh_token_ad(row_id: uuid.UUID) -> bytes:
    return column_ad("oidc_session", "refresh_token_encrypted", row_id)


def record_oidc_session(
    db: Session,
    keyring: Keyring,
    *,
    session_row: UserSession,
    token: ValidatedToken,
    id_token: str | None,
    refresh_token: str | None,
    now: datetime | None = None,
) -> OidcSession:
    """Store the OIDC side of a new session (module doc)."""
    now = now or clock.utcnow()
    if token.subject is None:  # pragma: no cover - ID tokens always have sub
        raise ValueError("ID token without sub")
    row_id = uuid7()
    row = OidcSession(
        id=row_id,
        session_id=session_row.id,
        user_id=session_row.user_id,
        issuer=token.issuer,
        subject=token.subject,
        sid=token.sid,
        id_token_encrypted=(
            keyring.encrypt_str(id_token, ad=id_token_ad(row_id)) if id_token else None
        ),
        refresh_token_encrypted=(
            keyring.encrypt_str(refresh_token, ad=refresh_token_ad(row_id))
            if refresh_token
            else None
        ),
        claims_refreshed_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    return row


def follow_rotation(db: Session, old: UserSession, new: UserSession) -> None:
    """Rotation hook: move the OIDC data to the session's new id."""
    db.execute(
        update(OidcSession)
        .where(OidcSession.session_id == old.id)
        .values(session_id=new.id, updated_at=clock.utcnow())
        .execution_options(synchronize_session="fetch")
    )


register_rotation_hook(follow_rotation)


def oidc_session_for(db: Session, session_id: uuid.UUID) -> OidcSession | None:
    return db.execute(
        select(OidcSession).where(OidcSession.session_id == session_id)
    ).scalar_one_or_none()


def decrypt_id_token(keyring: Keyring, row: OidcSession) -> str | None:
    if row.id_token_encrypted is None:
        return None
    try:
        return keyring.decrypt_str(row.id_token_encrypted, ad=id_token_ad(row.id))
    except DecryptionError:
        return None


def decrypt_refresh_token(keyring: Keyring, row: OidcSession) -> str | None:
    if row.refresh_token_encrypted is None:
        return None
    try:
        return keyring.decrypt_str(row.refresh_token_encrypted, ad=refresh_token_ad(row.id))
    except DecryptionError:
        return None


def allowed_post_logout_uri(settings: Settings, requested: str | None) -> str | None:
    """``requested`` if it is on the allowlist (exact match), the default if
    nothing was requested, else ``None`` (refused)."""
    allowlist = settings.effective_post_logout_redirect_uris
    if requested is None:
        return allowlist[0]
    return requested if requested in allowlist else None


@dataclass(frozen=True)
class RpLogout:
    """What RP-initiated logout found: the revoked session's user and its id_token_hint."""

    user_id: uuid.UUID
    session_id: uuid.UUID
    id_token_hint: str | None
    was_oidc: bool


def rp_logout(
    db: Session, keyring: Keyring, row: UserSession, *, now: datetime | None = None
) -> RpLogout:
    """Revoke ``row`` and drop its OIDC data; return the hint for the IdP."""
    now = now or clock.utcnow()
    oidc = oidc_session_for(db, row.id)
    hint = decrypt_id_token(keyring, oidc) if oidc is not None else None
    revoke(row, reason="logout", now=now)
    if oidc is not None:
        db.delete(oidc)
    db.flush()
    return RpLogout(
        user_id=row.user_id, session_id=row.id, id_token_hint=hint, was_oidc=oidc is not None
    )


#: Domain separator for logout-token ids in ``oidc_used_state`` (whose other
#: rows are SHA-256 of a bare login ``state``).
_JTI_DOMAIN: Final = b"perfin:backchannel-logout:jti\x00"


def logout_jti_hash(issuer: str, jti: str) -> str:
    """The ``oidc_used_state`` key of a logout token: SHA-256 of ``(iss, jti)``."""
    digest = hashlib.sha256(_JTI_DOMAIN)
    digest.update(issuer.encode("utf-8"))
    digest.update(b"\x00")
    digest.update(jti.encode("utf-8"))
    return digest.hexdigest()


def consume_logout_jti(db: Session, token: ValidatedToken, *, now: datetime | None = None) -> bool:
    """Record the token's ``jti`` as used; ``False`` if it was seen before.

    Runs in the caller's transaction, which must also revoke the sessions
    (:func:`revoke_for_logout_token`): the ``jti`` only counts as used once
    the revocation commits. ``INSERT ... ON CONFLICT DO NOTHING`` on the
    primary key serialises concurrent deliveries of one token: the second
    waits for the first transaction and is a replay if it committed, or
    records the ``jti`` itself if it rolled back. The row lives until
    ``iat + MAX_TOKEN_AGE + CLOCK_SKEW``, after which
    ``validate_logout_token`` refuses the token as too old anyway;
    ``oidc_prune`` then drops it.
    """
    now = now or clock.utcnow()
    jti = token.jti
    if jti is None:  # pragma: no cover - validate_logout_token requires it
        return False
    issued_at = token.issued_at or now
    expires_at = max(issued_at, now) + MAX_TOKEN_AGE + CLOCK_SKEW
    inserted = db.execute(
        pg_insert(OidcUsedState)
        .values(
            state_hash=logout_jti_hash(token.issuer, jti),
            expires_at=expires_at,
            created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[OidcUsedState.state_hash])
        .returning(OidcUsedState.state_hash)
    ).scalar_one_or_none()
    return inserted is not None


@dataclass(frozen=True)
class BackchannelResult:
    users: tuple[uuid.UUID, ...]
    sessions_revoked: int


def revoke_for_logout_token(
    db: Session, token: ValidatedToken, *, now: datetime | None = None
) -> BackchannelResult:
    """Revoke the sessions a validated logout token names (module doc)."""
    now = now or clock.utcnow()
    stmt = select(OidcSession).where(OidcSession.issuer == token.issuer)
    if token.sid is not None:
        stmt = stmt.where(OidcSession.sid == token.sid)
    if token.subject is not None:
        stmt = stmt.where(OidcSession.subject == token.subject)
    rows = list(db.execute(stmt.with_for_update()).scalars())
    revoked = 0
    users: list[uuid.UUID] = []
    for oidc in rows:
        session_row = db.get(UserSession, oidc.session_id, with_for_update=True)
        if session_row is not None and is_live(session_row, now):
            revoke(session_row, reason="idp_logout", now=now)
            revoked += 1
            if session_row.user_id not in users:
                users.append(session_row.user_id)
        db.delete(oidc)
    db.flush()
    return BackchannelResult(users=tuple(users), sessions_revoked=revoked)


def prune(db: Session, *, now: datetime | None = None) -> tuple[int, int]:
    """Drop ``oidc_session`` rows of ended sessions and expired used states."""
    now = now or clock.utcnow()
    ended = select(UserSession.id).where(
        or_(
            UserSession.revoked_at.is_not(None),
            UserSession.idle_expires_at <= now,
            UserSession.absolute_expires_at <= now,
        )
    )
    sessions = db.execute(delete(OidcSession).where(OidcSession.session_id.in_(ended)))
    states = db.execute(delete(OidcUsedState).where(OidcUsedState.expires_at < now))
    return int(sessions.rowcount), int(states.rowcount)  # type: ignore[attr-defined]
