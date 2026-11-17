# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional periodic claim refresh with the refresh token (SECURITY.md B8).

Enabled by ``PERFIN_OIDC_CLAIM_REFRESH_MINUTES`` > 0 (only then is a refresh
token kept, encrypted, in ``oidc_session``). Every live OIDC session whose
claims are older than that interval is refreshed at the token endpoint:

* the IdP refuses the grant (``invalid_grant``: the user was disabled, the
  IdP session or consent was revoked) -> the PerFin session is revoked;
* a new ID token (validated like a login one, without nonce; same ``iss`` and
  ``sub``) re-applies the group mapping: no mapped group with
  ``PERFIN_OIDC_REQUIRE_GROUP`` revokes the session, a changed group updates
  the household role (never instance admin, B6);
* a rotated refresh token replaces the stored one;
* the IdP being unreachable changes nothing (retried on the next run); a
  JWKS that cannot be fetched counts as unreachable: the token call already
  happened, so a rotated refresh token is still stored, the claims are not
  stamped as refreshed, and the IdP is not asked again in that run.

**No row locks across the IdP.** A run first reads the due sessions without
locking them and makes every token call (and validates every ID token)
with nothing locked, so a request touching one of those sessions, or a
logout, never waits for the IdP. Then each outcome is applied under a
short row lock, and only if the session is still live and its stored
refresh token is still the one the IdP was given (otherwise the row is
skipped: a newer token or a logout wins). One run at a time
(:func:`take_run_lock`, a transaction-level advisory lock) so a refresh
token is never sent twice. Each row is applied in a savepoint: a lock
conflict there skips that row, not the run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import or_, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth.local import audit_auth
from app.auth.models import User, UserSession
from app.auth.oidc.client import OidcClient, TokenEndpointError, TokenResponse
from app.auth.oidc.discovery import DiscoveryError
from app.auth.oidc.logout import decrypt_refresh_token, refresh_token_ad
from app.auth.oidc.models import OidcSession
from app.auth.oidc.provisioning import map_groups, sync_membership
from app.auth.oidc.tokens import TokenValidationError, ValidatedToken, validate_id_token
from app.auth.sessions import revoke
from app.core import clock
from app.core.audit import RequestMeta
from app.core.crypto import Keyring
from app.core.logging import get_logger
from app.core.settings import Settings

log = get_logger(__name__)

BATCH_SIZE = 100
# Namespace and key for pg_try_advisory_xact_lock(int4, int4): one run at a time.
_RUN_LOCK: Final = text("SELECT pg_try_advisory_xact_lock(1347766866, 1)")  # "PFOR", 1


@dataclass
class RefreshStats:
    refreshed: int = 0
    revoked: int = 0
    skipped: int = 0


@dataclass
class _Outcome:
    """What the IdP said about one session, decided without any row lock."""

    oidc_id: uuid.UUID
    session_id: uuid.UUID
    #: The stored ciphertext the decision was made for (applied only if unchanged).
    token_seen: bytes | None
    #: ``keep``: only store a rotated refresh token (the ID token could not be checked).
    action: Literal["revoke", "apply", "keep"]
    reason: str = ""
    response: TokenResponse | None = None
    validated: ValidatedToken | None = None


def take_run_lock(db: Session) -> bool:
    """Claim the run for this transaction; ``False`` while another run holds it."""
    return bool(db.execute(_RUN_LOCK).scalar_one())


def _revoke(db: Session, oidc: OidcSession, session_row: UserSession, reason: str) -> None:
    revoke(session_row, reason=reason)
    audit_auth(
        db,
        action="user.session_revoked_by_idp",
        user_id=session_row.user_id,
        meta=RequestMeta(),
        target_type="user_session",
        target_id=session_row.id,
        after={"reason": reason},
    )
    db.delete(oidc)


def _ask_idp(
    oidc: OidcSession, client: OidcClient, keyring: Keyring
) -> _Outcome | Literal["skip", "unavailable"]:
    """The token call and ID token checks for one session (no database writes)."""
    seen = oidc.refresh_token_encrypted

    def outcome(
        action: Literal["revoke", "apply", "keep"],
        reason: str = "",
        response: TokenResponse | None = None,
        validated: ValidatedToken | None = None,
    ) -> _Outcome:
        return _Outcome(oidc.id, oidc.session_id, seen, action, reason, response, validated)

    refresh_token = decrypt_refresh_token(keyring, oidc)
    if refresh_token is None:
        return outcome("revoke", "idp_refresh_unavailable")
    try:
        response = client.refresh(refresh_token)
    except DiscoveryError:
        return "unavailable"
    except TokenEndpointError as exc:
        return outcome("revoke", "idp_refresh_denied") if exc.invalid_grant else "skip"
    validated: ValidatedToken | None = None
    if response.id_token is not None:
        try:
            validated = validate_id_token(
                response.id_token,
                issuer=client.issuer,
                client_id=client.client_id,
                keys=client.keys,
                nonce=None,
                access_token=response.access_token,
            )
        except TokenValidationError as exc:
            if exc.reason == "jwks-unavailable":
                return outcome("keep", response=response)
            return outcome("revoke", f"idp_refresh_invalid:{exc.reason}")
        if validated.subject != oidc.subject:
            return outcome("revoke", "idp_refresh_subject_changed")
    return outcome("apply", response=response, validated=validated)


def _lock_current(
    db: Session, outcome: _Outcome, now: datetime
) -> tuple[OidcSession, UserSession] | None:
    """The rows under lock, if the session is live and its token is the one sent."""
    row = db.execute(
        select(OidcSession, UserSession)
        .join(UserSession, UserSession.id == OidcSession.session_id)
        .where(OidcSession.id == outcome.oidc_id, UserSession.id == outcome.session_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        return None
    oidc, session_row = row
    if (
        session_row.revoked_at is not None
        or session_row.idle_expires_at <= now
        or session_row.absolute_expires_at <= now
        or oidc.refresh_token_encrypted != outcome.token_seen
    ):
        return None
    return oidc, session_row


def _apply(
    db: Session,
    outcome: _Outcome,
    oidc: OidcSession,
    session_row: UserSession,
    keyring: Keyring,
    settings: Settings,
    now: datetime,
) -> Literal["revoked", "refreshed", "kept"]:
    """Apply one outcome to the locked rows."""
    if outcome.action == "revoke":
        _revoke(db, oidc, session_row, outcome.reason)
        return "revoked"
    if outcome.action == "keep":
        _store_refresh_token(oidc, outcome.response, keyring)
        oidc.updated_at = now
        return "kept"
    validated = outcome.validated
    if validated is not None:
        user = db.get(User, oidc.user_id)
        role = map_groups(validated.groups, settings)
        if user is None or not user.is_active:
            _revoke(db, oidc, session_row, "account_disabled")
            return "revoked"
        if role is None and settings.oidc_require_group:
            _revoke(db, oidc, session_row, "not_in_group")
            return "revoked"
        change = sync_membership(db, oidc.user_id, role, settings)
        if change is not None:
            audit_auth(
                db,
                action="household.member_role_synced",
                user_id=oidc.user_id,
                meta=RequestMeta(),
                target_type="household",
                target_id=change.household_id,
                after={
                    "source": "oidc_claim_refresh",
                    "before": change.before.value if change.before else None,
                    "after": change.after.value,
                },
            )
        if validated.sid is not None:
            oidc.sid = validated.sid
    _store_refresh_token(oidc, outcome.response, keyring)
    oidc.claims_refreshed_at = now
    oidc.updated_at = now
    return "refreshed"


def _store_refresh_token(
    oidc: OidcSession, response: TokenResponse | None, keyring: Keyring
) -> None:
    if response is not None and response.refresh_token is not None:
        oidc.refresh_token_encrypted = keyring.encrypt_str(
            response.refresh_token, ad=refresh_token_ad(oidc.id)
        )


def refresh_claims(
    db: Session,
    client: OidcClient,
    keyring: Keyring,
    settings: Settings,
    *,
    now: datetime | None = None,
    batch_size: int = BATCH_SIZE,
) -> RefreshStats:
    """Refresh due sessions (module doc); the caller commits."""
    stats = RefreshStats()
    if settings.oidc_claim_refresh_minutes <= 0:
        return stats
    if not take_run_lock(db):
        log.info("OIDC claim refresh skipped: another run is active")
        return stats
    now = now or clock.utcnow()
    due = now - timedelta(minutes=settings.oidc_claim_refresh_minutes)
    candidates = list(
        db.execute(
            select(OidcSession)
            .join(UserSession, UserSession.id == OidcSession.session_id)
            .where(
                OidcSession.refresh_token_encrypted.is_not(None),
                or_(
                    OidcSession.claims_refreshed_at.is_(None),
                    OidcSession.claims_refreshed_at <= due,
                ),
                UserSession.revoked_at.is_(None),
                UserSession.idle_expires_at > now,
                UserSession.absolute_expires_at > now,
            )
            .order_by(OidcSession.claims_refreshed_at.nulls_first())
            .limit(batch_size)
        ).scalars()
    )
    # Phase 1: every IdP call with no row locked.
    outcomes: list[_Outcome] = []
    for index, oidc in enumerate(candidates):
        result = _ask_idp(oidc, client, keyring)
        if result == "unavailable":
            log.warning("OIDC claim refresh skipped: provider unavailable")
            stats.skipped += len(candidates) - index
            break
        if result == "skip":
            stats.skipped += 1
            continue
        outcomes.append(result)
        if result.action == "keep":
            log.warning("OIDC claim refresh skipped: JWKS unavailable")
            stats.skipped += len(candidates) - index - 1
            break
    # Phase 2: apply each under a short lock, if nothing changed meanwhile.
    for outcome in outcomes:
        try:
            with db.begin_nested():
                locked = _lock_current(db, outcome, now)
                if locked is None:
                    stats.skipped += 1
                    continue
                applied = _apply(db, outcome, *locked, keyring, settings, now)
                if applied == "revoked":
                    stats.revoked += 1
                elif applied == "refreshed":
                    stats.refreshed += 1
                else:
                    stats.skipped += 1
                db.flush()
        except DBAPIError as exc:
            log.warning(
                "OIDC claim refresh row skipped", extra={"error_class": type(exc.orig).__name__}
            )
            stats.skipped += 1
    db.flush()
    return stats
