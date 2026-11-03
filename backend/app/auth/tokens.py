# SPDX-License-Identifier: AGPL-3.0-or-later
"""Personal access tokens: create, list, revoke, and bearer authentication (SECURITY.md I3).

Format and verification live in ``app.auth.principal``
(``generate_api_token``, ``authenticate_token``): ``pfn_<12 hex id>_<43
base64url chars = 256-bit secret>``; only the SHA-256 of the whole token is
stored, the id (``prefix``) is the public lookup key, and the hash is
compared in constant time. This module adds the lifecycle and the limits:

* **Create** (:func:`create_token`): a browser session with a fresh step-up
  (the router enforces ``require_step_up``), a name, at least one scope from
  ``app.auth.scopes`` that the caller's role may grant (``ai:read`` never),
  and a mandatory expiry of at most ``PERFIN_TOKEN_MAX_DAYS`` (default 90,
  hard cap 365). The token is bound to the session's active household and
  acts as the user there: role re-read per request, scopes narrowed to the
  role's ceiling, account grants applied by the endpoints. The raw token is
  returned once and never stored. At most :attr:`TokenLimits.max_active`
  live tokens per user and household. A break-glass account (SECURITY.md
  B7) gets none (``403 breakglass-account``): its access is bound to
  ``PERFIN_BREAKGLASS_CIDRS``, which a token would escape. While ``PERFIN_REQUIRE_MFA`` is on the
  account needs a PerFin second factor (``403 mfa-enrollment-required``):
  bearer requests are refused without one, and a session verified only by
  the IdP's MFA would otherwise mint a token that never works.
* **List** (:func:`list_tokens`): the caller's tokens in the active household,
  never the secret or its hash.
* **Revoke** (:func:`revoke_token`): sets ``revoked_at``; idempotent.
* **Use** (:func:`authenticate_bearer`, called by ``app.auth.deps`` for every
  request with an ``Authorization`` header): failed bearer attempts are
  limited per client IP (checked *before* the lookup, so a flood of bad
  tokens is turned away cheaply) and successful use per token. Each limit is
  a sliding window in ``rate_limit_event`` (``app.auth.ratelimit``), counted
  in its own transaction so a rejected request still counts.
  ``last_used_at`` is written at most once a minute
  (``principal.TOKEN_TOUCH_SECONDS``).

Deactivating a user invalidates their tokens at once (``authenticate_token``
refuses inactive users; ``/admin/users`` also revokes them), and so does
losing the membership of the token's household. Flagging an account as
break-glass does too (``authenticate_token`` refuses break-glass accounts;
``set_breakglass`` also revokes their tokens).

Create and revoke are audited in the household (``token.create``,
``token.revoke``) with the name, scopes, expiry and public id, never the
secret.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final, Literal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection, Request

from app.auth.models import ApiToken
from app.auth.principal import Principal, authenticate_token, generate_api_token
from app.auth.ratelimit import Limit, get_rate_limiter, ip_key
from app.auth.scopes import ScopeError, check_requested
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.settings import Settings

TOKEN_NAME_MAX: Final = 100
#: The hard cap on ``PERFIN_TOKEN_MAX_DAYS`` (the setting is validated to it).
TOKEN_MAX_DAYS_CAP: Final = 365

BEARER_CHALLENGE: Final = {"WWW-Authenticate": 'Bearer realm="perfin"'}

TokenStatus = Literal["active", "expired", "revoked"]

_LOCK_USER_TOKENS = text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 7306))")


#: Failed bearer authentications per client IP.
TOKEN_FAILURES_PER_IP: Final = Limit("token:fail:ip", 60, timedelta(minutes=5))
#: Authenticated requests per token.
TOKEN_USE_PER_TOKEN: Final = Limit("token:use", 600, timedelta(minutes=1))


@dataclass(frozen=True)
class TokenLimits:
    """Rate limits and caps for personal access tokens (``app.state.token_limits``)."""

    failures_per_ip: Limit = TOKEN_FAILURES_PER_IP
    use_per_token: Limit = TOKEN_USE_PER_TOKEN
    #: Live (unrevoked, unexpired) tokens per user and household.
    max_active: int = 50


DEFAULT_TOKEN_LIMITS: Final = TokenLimits()


def get_token_limits(conn: HTTPConnection) -> TokenLimits:
    limits = getattr(conn.app.state, "token_limits", None)
    return limits if isinstance(limits, TokenLimits) else DEFAULT_TOKEN_LIMITS


# ---------------------------------------------------------------------------
# Bearer authentication
# ---------------------------------------------------------------------------


def invalid_token() -> ProblemError:
    return ProblemError(
        401,
        "invalid-token",
        "Not authenticated",
        "The bearer token is missing, malformed, expired or revoked.",
        headers=BEARER_CHALLENGE,
    )


def bearer_credentials(header: str) -> str | None:
    """The credentials of an ``Authorization: Bearer <credentials>`` header."""
    scheme, _, credentials = header.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    value = credentials.strip()
    return value or None


def authenticate_bearer(
    request: Request, db: Session, header: str, *, settings: Settings
) -> Principal:
    """The principal for an ``Authorization`` header, else ``401``/``429``.

    Never falls back to a session cookie (``app.auth.deps``, review SR-8).
    """
    limits = get_token_limits(request)
    limiter = get_rate_limiter(request)
    client = ip_key(request)
    limiter.peek(limits.failures_per_ip, client).raise_if_denied()
    raw = bearer_credentials(header)
    principal = (
        authenticate_token(db, raw, require_mfa=settings.require_mfa) if raw is not None else None
    )
    if principal is None or principal.token_id is None:
        limiter.hit(limits.failures_per_ip, client)
        raise invalid_token()
    limiter.hit(limits.use_per_token, str(principal.token_id)).raise_if_denied()
    return principal


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def token_status(row: ApiToken, now: datetime | None = None) -> TokenStatus:
    now = now or clock.utcnow()
    if row.revoked_at is not None:
        return "revoked"
    if row.expires_at <= now:
        return "expired"
    return "active"


@dataclass(frozen=True)
class CreatedToken:
    """A new token row and its raw value (returned to the user once)."""

    row: ApiToken
    raw: str = field(repr=False)


def _household_of(principal: Principal) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise ProblemError(
            403, "forbidden", "Forbidden", "A token belongs to a household; join one first."
        )
    return principal.household_id


def _scope_problem(exc: ScopeError) -> ProblemError:
    titles = {
        "unknown-scope": "Unknown scope",
        "scope-reserved": "Reserved scope",
        "scope-not-allowed": "Scope not allowed",
    }
    status = 403 if exc.code == "scope-not-allowed" else 422
    return ProblemError(
        status, exc.code, titles.get(exc.code, "Invalid scope"), str(exc), {"scopes": exc.scopes}
    )


def active_token_count(
    db: Session, user_id: uuid.UUID, household_id: uuid.UUID, now: datetime
) -> int:
    count = db.execute(
        select(func.count())
        .select_from(ApiToken)
        .where(
            ApiToken.user_id == user_id,
            ApiToken.household_id == household_id,
            ApiToken.revoked_at.is_(None),
            ApiToken.expires_at > now,
        )
    ).scalar_one()
    return int(count)


def create_token(
    db: Session,
    principal: Principal,
    *,
    name: str,
    scopes: list[str],
    expires_in_days: int,
    settings: Settings,
    limits: TokenLimits = DEFAULT_TOKEN_LIMITS,
    request: Request | None = None,
    now: datetime | None = None,
) -> CreatedToken:
    """Mint a token for ``principal`` in its active household (see module doc).

    The caller (router) has already required a browser session with a fresh
    step-up; this function re-checks everything that does not depend on the
    transport.
    """
    now = now or clock.utcnow()
    if principal.auth_method != "session":
        raise ProblemError(
            403, "session-required", "Browser session required", "Use the web app for this."
        )
    if principal.user.is_breakglass:
        # B7: a break-glass account is usable only from PERFIN_BREAKGLASS_CIDRS;
        # a bearer token carries no such check (``authenticate_token`` refuses
        # them too, review P1-SR-01).
        raise ProblemError(
            403,
            "breakglass-account",
            "Not available for break-glass accounts",
            "A break-glass account cannot create personal access tokens.",
        )
    if principal.mfa_required and not principal.mfa_enrolled:
        # A bearer request carries no MFA of its own, so while MFA is required
        # a token works only for an account with a PerFin second factor
        # (``authenticate_token``). A session verified by the IdP's MFA alone
        # would otherwise mint a token that every request refuses.
        raise ProblemError(
            403,
            "mfa-enrollment-required",
            "Multi-factor authentication required",
            "Set up a second factor in PerFin before creating a token.",
        )
    household_id = _household_of(principal)
    max_days = min(settings.token_max_days, TOKEN_MAX_DAYS_CAP)
    if not 1 <= expires_in_days <= max_days:
        raise ProblemError(
            422,
            "expiry-too-long",
            "Expiry too long",
            f"A token may live at most {max_days} days.",
            {"max_days": max_days},
        )
    clean_name = name.strip()
    if not clean_name or len(clean_name) > TOKEN_NAME_MAX:
        raise ProblemError(422, "invalid-token-name", "Invalid token name")
    if not scopes:
        raise ProblemError(422, "unknown-scope", "Unknown scope", "Choose at least one scope.")
    try:
        wanted = check_requested(scopes, principal.role)
    except ScopeError as exc:
        raise _scope_problem(exc) from exc

    # Serialise creation per user so the cap cannot be overshot concurrently.
    db.execute(_LOCK_USER_TOKENS, {"k": f"api_token:{principal.user_id}:{household_id}"})
    if active_token_count(db, principal.user_id, household_id, now) >= limits.max_active:
        raise ProblemError(
            409,
            "too-many-tokens",
            "Too many tokens",
            f"Revoke a token first; at most {limits.max_active} may be active.",
        )

    material = generate_api_token()
    row = ApiToken(
        household_id=household_id,
        user_id=principal.user_id,
        prefix=material.prefix,
        token_hash=material.token_hash,
        name=clean_name,
        scopes=sorted(wanted),
        expires_at=now + timedelta(days=expires_in_days),
    )
    db.add(row)
    db.flush()
    audit(
        db,
        action="token.create",
        target_type="api_token",
        target_id=row.id,
        after={
            "name": row.name,
            "scopes": row.scopes,
            "expires_at": row.expires_at,
            "prefix": row.prefix,
        },
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    return CreatedToken(row=row, raw=material.raw)


def list_tokens(db: Session, principal: Principal) -> list[ApiToken]:
    """The caller's tokens in the active household, newest first."""
    household_id = _household_of(principal)
    rows = db.execute(
        select(ApiToken)
        .where(ApiToken.user_id == principal.user_id, ApiToken.household_id == household_id)
        .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
    ).scalars()
    return list(rows)


def revoke_token(
    db: Session,
    principal: Principal,
    token_id: uuid.UUID,
    *,
    request: Request | None = None,
    now: datetime | None = None,
) -> ApiToken:
    """Revoke one of the caller's tokens in the active household (idempotent).

    ``404 token-not-found`` for anything else, so other users' token ids are
    not revealed.
    """
    now = now or clock.utcnow()
    household_id = _household_of(principal)
    row = db.execute(
        select(ApiToken)
        .where(
            ApiToken.id == token_id,
            ApiToken.user_id == principal.user_id,
            ApiToken.household_id == household_id,
        )
        .with_for_update()
    ).scalar_one_or_none()
    if row is None:
        raise ProblemError(404, "token-not-found", "Token not found")
    if row.revoked_at is None:
        row.revoked_at = now
        db.flush()
        audit(
            db,
            action="token.revoke",
            target_type="api_token",
            target_id=row.id,
            before={"name": row.name, "prefix": row.prefix},
            household_id=household_id,
            user_id=principal.user_id,
            request=request,
        )
    return row
