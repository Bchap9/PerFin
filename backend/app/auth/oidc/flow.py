# SPDX-License-Identifier: AGPL-3.0-or-later
"""The OIDC callback and the email-link confirmation (AUTH.md "OIDC" steps 2-6).

:func:`complete_login` runs the callback (``GET /auth/oidc/callback``):

1. per-IP rate limit (``oidc:ip``, A1);
2. the sealed pre-auth cookie must be present and unexpired, and the
   ``state`` query parameter must equal the one in it (constant time);
3. the state is **consumed** (``oidc_used_state``, committed in its own
   transaction before anything else happens), so the same state and cookie
   never work twice, even after a failed attempt (B1);
4. the code is exchanged with the PKCE verifier from the cookie and the ID
   token validated (``app.auth.oidc.tokens``: pinned algorithms, JWKS,
   ``iss``/``aud``/``azp``/``exp``/``iat``/``nonce``/``at_hash``; A9, B2);
5. the user is resolved by ``(iss, sub)`` and the group-mapped role applied
   (``app.auth.oidc.provisioning``; B3, B6);
6. a normal server-side session is created (ADR-0005), ``mfa_pending`` when
   the user has a local second factor the IdP did not satisfy, otherwise
   full; the IdP's ``amr`` counts as MFA only with
   ``PERFIN_OIDC_TRUST_IDP_MFA`` (``app.auth.mfa.policy``), and it is a
   fresh step-up only for a login started with ``reauth=true`` whose ID
   token ``auth_time`` is not older than that start minus
   :data:`CLOCK_SKEW` (A6, reviews P1-SR2-01 and P1-SR3-01): a plain or SSO
   re-login never is. A session the browser presented before is revoked.
   The ID and access tokens are dropped (the ID token is kept encrypted
   only as the logout ``id_token_hint``).

Every failure raises :class:`OidcLoginError` with a short public code (the
SPA shows a friendly message for it) and writes ``user.login_failed`` with the
precise reason; tokens never reach a log or audit row.

:func:`confirm_link` finishes "link by verified email" (B3): the browser
holds the sealed pending-link cookie from the callback and has since logged
in **locally** to that very account (a session started after the link was
offered, with MFA verified after it when the account has a second factor).
"""

from __future__ import annotations

import hmac
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from urllib.parse import quote

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import HTTPConnection

from app.auth.local import audit_auth, audit_auth_committed, get_sessionmaker, request_ip
from app.auth.mfa.policy import idp_asserted_mfa, login_stage, mark_mfa_from_idp
from app.auth.models import LoginMethod, SessionStage, User, UserSession
from app.auth.oidc.client import (
    PRE_AUTH_TTL,
    OidcClient,
    TokenEndpointError,
    state_hash,
)
from app.auth.oidc.discovery import DiscoveryError
from app.auth.oidc.logout import record_oidc_session
from app.auth.oidc.models import OidcUsedState
from app.auth.oidc.provisioning import (
    LinkRequired,
    OidcRejectedError,
    link_identity,
    resolve_user,
    sync_membership,
    target_household,
)
from app.auth.oidc.tokens import (
    CLOCK_SKEW,
    TokenValidationError,
    ValidatedToken,
    validate_id_token,
)
from app.auth.principal import Principal, memberships
from app.auth.ratelimit import OIDC_CALLBACK_PER_IP, Limit, RateLimiter, ip_key
from app.auth.sessions import IssuedSession, SessionPolicy, create_session, load_session, revoke
from app.core import clock
from app.core.audit import RequestMeta
from app.core.crypto import Keyring
from app.core.logging import get_logger
from app.core.settings import Settings

log = get_logger(__name__)

#: SPA page that asks for the local login before linking.
LINK_PAGE: Final = "/login/link-oidc"
#: SPA page for the second factor of an ``mfa_pending`` login.
MFA_PAGE: Final = "/login/mfa"
LOGIN_PAGE: Final = "/login"
MAX_CODE_LENGTH: Final = 2048
_IDP_ERROR_RE: Final = re.compile(r"^[a-z_]{1,40}$")
#: Longest rotation chain walked back when checking a link confirmation.
_MAX_CHAIN: Final = 32

#: Public error codes (``/login?oidc_error=<code>``).
PUBLIC_REASONS: Final = frozenset(
    {
        "rate-limited",
        "state-invalid",
        "idp-error",
        "idp-unavailable",
        "token-invalid",
        "not-in-group",
        "no-account",
        "account-exists",
        "account-disabled",
        "login-failed",
    }
)


class OidcLoginError(Exception):
    """The callback failed: ``public`` is shown to the SPA, ``reason`` is audited."""

    def __init__(self, public: str, reason: str, *, user_id: uuid.UUID | None = None) -> None:
        super().__init__(reason)
        self.public = public if public in PUBLIC_REASONS else "login-failed"
        self.reason = reason
        self.user_id = user_id


def error_redirect(public: str) -> str:
    return f"{LOGIN_PAGE}?oidc_error={quote(public, safe='')}"


@dataclass(frozen=True)
class CallbackResult:
    """Where to send the browser, plus the new session or pending link."""

    redirect: str
    issued: IssuedSession | None = field(default=None, repr=False)
    link_cookie: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class CallbackParams:
    code: str | None
    state: str | None
    error: str | None


def _consume_state(factory: sessionmaker[Session], state: str, now: datetime) -> bool:
    """Record ``state`` as used; ``False`` if it was used before (B1)."""
    try:
        with factory() as db, db.begin():
            db.add(
                OidcUsedState(
                    state_hash=state_hash(state),
                    expires_at=now + PRE_AUTH_TTL + CLOCK_SKEW,
                    created_at=now,
                )
            )
    except IntegrityError:
        return False
    return True


def _fail(
    factory: sessionmaker[Session],
    meta: RequestMeta,
    public: str,
    reason: str,
    *,
    user_id: uuid.UUID | None = None,
) -> OidcLoginError:
    audit_auth_committed(
        factory,
        action="user.login_failed",
        user_id=user_id,
        meta=meta,
        after={"method": "oidc", "reason": reason},
    )
    return OidcLoginError(public, reason, user_id=user_id)


def complete_login(
    request: HTTPConnection,
    settings: Settings,
    client: OidcClient,
    keyring: Keyring,
    params: CallbackParams,
    *,
    previous_session_id: str | None,
    rate_limit: Limit = OIDC_CALLBACK_PER_IP,
) -> CallbackResult:
    """Steps 1-6 of the module doc. Raises :class:`OidcLoginError`."""
    factory = get_sessionmaker(request)
    meta = RequestMeta.from_request(request)
    if not RateLimiter(factory).hit(rate_limit, ip_key(request)).allowed:
        raise OidcLoginError("rate-limited", "rate-limited")
    pre = client.read_state(request)
    if pre is None:
        raise _fail(factory, meta, "state-invalid", "missing-state-cookie")
    if params.error is not None:
        code = params.error if _IDP_ERROR_RE.fullmatch(params.error) else "other"
        raise _fail(factory, meta, "idp-error", f"idp:{code}")
    if not params.state or not hmac.compare_digest(
        params.state.encode("utf-8"), pre.state.encode("utf-8")
    ):
        raise _fail(factory, meta, "state-invalid", "state-mismatch")
    now = clock.utcnow()
    if not _consume_state(factory, pre.state, now):
        raise _fail(factory, meta, "state-invalid", "state-reused")
    if not params.code or len(params.code) > MAX_CODE_LENGTH:
        raise _fail(factory, meta, "idp-error", "missing-code")

    try:
        tokens = client.exchange_code(params.code, pre.verifier)
        metadata = client.metadata()
    except DiscoveryError:
        raise _fail(factory, meta, "idp-unavailable", "discovery-failed") from None
    except TokenEndpointError:
        raise _fail(factory, meta, "idp-error", "token-exchange-failed") from None
    if tokens.id_token is None:  # pragma: no cover - exchange_code requires it
        raise _fail(factory, meta, "token-invalid", "no-id-token")
    try:
        validated = validate_id_token(
            tokens.id_token,
            issuer=client.issuer,
            client_id=client.client_id,
            keys=client.keys,
            nonce=pre.nonce,
            access_token=tokens.access_token,
        )
    except TokenValidationError as exc:
        raise _fail(factory, meta, "token-invalid", exc.reason) from None

    now = clock.utcnow()
    policy = SessionPolicy.from_settings(settings)
    try:
        with factory() as db, db.begin():
            resolved = resolve_user(db, validated, settings, now=now)
            if isinstance(resolved, LinkRequired):
                audit_auth(
                    db,
                    action="user.oidc_link_offered",
                    user_id=resolved.user_id,
                    meta=meta,
                    after={"issuer": validated.issuer},
                )
                cookie = client.sealer.seal(
                    "link",
                    {
                        "iss": validated.issuer,
                        "sub": validated.subject,
                        "email": resolved.email,
                        "uid": str(resolved.user_id),
                        "iat_ms": int(now.timestamp() * 1000),
                    },
                    ttl=PRE_AUTH_TTL,
                )
                return CallbackResult(redirect=LINK_PAGE, link_cookie=cookie)
            user = resolved.user
            if resolved.provisioned:
                audit_auth(
                    db,
                    action="user.provisioned",
                    user_id=user.id,
                    meta=meta,
                    after={"method": "oidc", "issuer": validated.issuer},
                )
            change = sync_membership(db, user.id, resolved.role, settings)
            if change is not None:
                audit_auth(
                    db,
                    action="household.member_role_synced",
                    user_id=user.id,
                    meta=meta,
                    target_type="household",
                    target_id=change.household_id,
                    after={
                        "source": "oidc_groups",
                        "before": change.before.value if change.before else None,
                        "after": change.after.value,
                    },
                )
            issued = _start_session(
                db,
                request,
                settings,
                keyring,
                user=user,
                validated=validated,
                id_token=tokens.id_token if metadata.end_session_endpoint else None,
                refresh_token=(
                    tokens.refresh_token if settings.oidc_claim_refresh_minutes > 0 else None
                ),
                previous_session_id=previous_session_id,
                policy=policy,
                now=now,
                step_up_from=(
                    pre.started_at - CLOCK_SKEW
                    if pre.reauth and pre.started_at is not None
                    else None
                ),
            )
            audit_auth(
                db,
                action="user.login",
                user_id=user.id,
                meta=meta,
                after={
                    "method": "oidc",
                    "stage": issued.row.stage.value,
                    "idp_mfa": issued.row.mfa_verified_at is not None,
                    "reauth": pre.reauth,
                    "provisioned": resolved.provisioned,
                },
            )
    except OidcRejectedError as exc:
        raise _fail(factory, meta, exc.reason, exc.reason, user_id=exc.user_id) from None

    if issued.row.stage is SessionStage.MFA_PENDING:
        redirect = f"{MFA_PAGE}?return_to={quote(pre.return_to, safe='/')}"
    else:
        redirect = pre.return_to
    return CallbackResult(redirect=redirect, issued=issued)


def _start_session(
    db: Session,
    request: HTTPConnection,
    settings: Settings,
    keyring: Keyring,
    *,
    user: User,
    validated: ValidatedToken,
    id_token: str | None,
    refresh_token: str | None,
    previous_session_id: str | None,
    policy: SessionPolicy,
    now: datetime,
    step_up_from: datetime | None = None,
) -> IssuedSession:
    previous = load_session(db, previous_session_id, policy=policy, now=now, touch=False)
    if previous is not None:
        revoke(previous, reason="replaced_by_login", now=now)
    idp_mfa = idp_asserted_mfa(validated.amr, trust_idp_mfa=settings.oidc_trust_idp_mfa)
    stage = login_stage(db, user.id, idp_mfa=idp_mfa)
    preferred = target_household(db, settings)
    found = memberships(db, user.id)
    household_id = None
    if found:
        household_id = next(
            (m.household_id for m in found if m.household_id == preferred),
            found[0].household_id,
        )
    issued = create_session(
        db,
        user_id=user.id,
        policy=policy,
        login_method=LoginMethod.OIDC,
        stage=stage,
        household_id=household_id,
        ip=request_ip(request),
        user_agent=request.headers.get("user-agent"),
        now=now,
    )
    if stage is SessionStage.AUTHENTICATED:
        mark_mfa_from_idp(
            db,
            issued.row,
            validated.amr,
            auth_time=validated.auth_time,
            step_up_from=step_up_from,
            settings=settings,
            now=now,
        )
    record_oidc_session(
        db,
        keyring,
        session_row=issued.row,
        token=validated,
        id_token=id_token,
        refresh_token=refresh_token,
        now=now,
    )
    user.last_login_at = now
    db.flush()
    return issued


# ---------------------------------------------------------------------------
# Link by verified email (B3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingLink:
    issuer: str
    subject: str
    email: str
    user_id: uuid.UUID
    offered_at: datetime

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> PendingLink | None:
        if payload is None:
            return None
        try:
            return cls(
                issuer=str(payload["iss"]),
                subject=str(payload["sub"]),
                email=str(payload["email"]),
                user_id=uuid.UUID(str(payload["uid"])),
                offered_at=datetime.fromtimestamp(int(payload["iat_ms"]) / 1000, tz=UTC),
            )
        except (KeyError, ValueError, TypeError):
            return None


class LinkRefusedError(Exception):
    """The link confirmation is refused; ``reason`` is a stable code."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _chain_root(db: Session, row: UserSession) -> UserSession:
    """The session a rotation chain started with (the login itself)."""
    current = row
    for _ in range(_MAX_CHAIN):
        if current.rotated_from_id is None:
            return current
        parent = db.get(UserSession, current.rotated_from_id)
        if parent is None:
            return current
        current = parent
    return current


def confirm_link(
    db: Session,
    principal: Principal,
    link: PendingLink,
    *,
    issuer: str,
    meta: RequestMeta,
) -> None:
    """Link the pending identity to the caller's account (module doc)."""
    session_row = principal.session
    if principal.auth_method != "session" or session_row is None:
        raise LinkRefusedError("session-required")
    if link.issuer != issuer:
        raise LinkRefusedError("issuer-changed")
    if session_row.login_method is not LoginMethod.LOCAL:
        raise LinkRefusedError("local-login-required")
    if principal.user.id != link.user_id:
        raise LinkRefusedError("account-mismatch")
    # Both times come from this server's clock (millisecond precision).
    offered = link.offered_at - timedelta(milliseconds=1)
    root = _chain_root(db, session_row)
    if root.created_at < offered:
        raise LinkRefusedError("reauthentication-required")
    if principal.mfa_enrolled and (
        principal.mfa_verified_at is None or principal.mfa_verified_at < offered
    ):
        raise LinkRefusedError("mfa-required")
    user = principal.user
    if (user.email or "").strip().lower() != link.email.strip().lower() or not user.email_verified:
        raise LinkRefusedError("email-changed")
    try:
        link_identity(db, user=user, issuer=link.issuer, subject=link.subject, email=link.email)
    except OidcRejectedError as exc:
        raise LinkRefusedError(exc.reason) from None
    audit_auth(
        db,
        action="user.oidc_linked",
        user_id=user.id,
        meta=meta,
        after={"issuer": link.issuer, "method": "verified_email"},
    )
