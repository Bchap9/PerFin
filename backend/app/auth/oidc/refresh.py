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
* the IdP being unreachable changes nothing (retried on the next run).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.auth.local import audit_auth
from app.auth.models import User, UserSession
from app.auth.oidc.client import OidcClient, TokenEndpointError
from app.auth.oidc.discovery import DiscoveryError
from app.auth.oidc.logout import decrypt_refresh_token, refresh_token_ad
from app.auth.oidc.models import OidcSession
from app.auth.oidc.provisioning import map_groups, sync_membership
from app.auth.oidc.tokens import TokenValidationError, validate_id_token
from app.auth.sessions import revoke
from app.core import clock
from app.core.audit import RequestMeta
from app.core.crypto import Keyring
from app.core.logging import get_logger
from app.core.settings import Settings

log = get_logger(__name__)

BATCH_SIZE = 100


@dataclass
class RefreshStats:
    refreshed: int = 0
    revoked: int = 0
    skipped: int = 0


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
    now = now or clock.utcnow()
    due = now - timedelta(minutes=settings.oidc_claim_refresh_minutes)
    rows = db.execute(
        select(OidcSession, UserSession)
        .join(UserSession, UserSession.id == OidcSession.session_id)
        .where(
            OidcSession.refresh_token_encrypted.is_not(None),
            or_(OidcSession.claims_refreshed_at.is_(None), OidcSession.claims_refreshed_at <= due),
            UserSession.revoked_at.is_(None),
            UserSession.idle_expires_at > now,
            UserSession.absolute_expires_at > now,
        )
        .order_by(OidcSession.claims_refreshed_at.nulls_first())
        .limit(batch_size)
        .with_for_update(skip_locked=True)
    ).all()
    for oidc, session_row in rows:
        refresh_token = decrypt_refresh_token(keyring, oidc)
        if refresh_token is None:
            _revoke(db, oidc, session_row, "idp_refresh_unavailable")
            stats.revoked += 1
            continue
        try:
            response = client.refresh(refresh_token)
        except DiscoveryError:
            log.warning("OIDC claim refresh skipped: provider unavailable")
            stats.skipped += len(rows) - stats.refreshed - stats.revoked
            break
        except TokenEndpointError as exc:
            if exc.invalid_grant:
                _revoke(db, oidc, session_row, "idp_refresh_denied")
                stats.revoked += 1
            else:
                stats.skipped += 1
            continue
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
                _revoke(db, oidc, session_row, f"idp_refresh_invalid:{exc.reason}")
                stats.revoked += 1
                continue
            if validated.subject != oidc.subject:
                _revoke(db, oidc, session_row, "idp_refresh_subject_changed")
                stats.revoked += 1
                continue
            user = db.get(User, oidc.user_id)
            role = map_groups(validated.groups, settings)
            if user is None or not user.is_active:
                _revoke(db, oidc, session_row, "account_disabled")
                stats.revoked += 1
                continue
            if role is None and settings.oidc_require_group:
                _revoke(db, oidc, session_row, "not_in_group")
                stats.revoked += 1
                continue
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
        if response.refresh_token is not None:
            oidc.refresh_token_encrypted = keyring.encrypt_str(
                response.refresh_token, ad=refresh_token_ad(oidc.id)
            )
        oidc.claims_refreshed_at = now
        oidc.updated_at = now
        stats.refreshed += 1
    db.flush()
    return stats
