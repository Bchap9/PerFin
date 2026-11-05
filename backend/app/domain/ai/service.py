# SPDX-License-Identifier: AGPL-3.0-or-later
"""The AI-client flow, request context and call audit (AI_ACCESS.md, M1, M4, M6, E4).

**Clients.** :func:`create_client` is the **only** code that mints an
``ai:read`` token (``app.auth.scopes`` reserves the scope, so a personal
access token can never carry it). The caller must be the household owner on
a browser session with a fresh step-up (the settings router also guards it).
The token is a normal hashed, expiring ``api_token`` (``pfn_...``, SHA-256
at rest, I3) bound to the owner's active household with exactly the scope
``ai:read``; the raw value is returned once. :func:`revoke_client` revokes the
client and its token together. Creation and revocation are audited
(``ai_client.create`` / ``ai_client.revoke``) without the token or the
denylist terms.

**Confinement.** A token carrying ``ai:read`` is an AI token
(``Principal.ai_only``): ``app.auth.deps`` refuses it on every route outside
``/api/v1/ai/v1`` (``403 ai-token-confined``), and the AI routes accept
nothing else (:func:`resolve_client`: ``403 ai-client-required`` for
sessions, personal tokens and tokens whose client is revoked or gone).

**Rate limits (M6).** Per client, :data:`DEFAULT_AI_LIMITS` (a sliding
window per minute and per ten minutes in ``rate_limit_event``), on top of
the per-token use limit every bearer token has; ``429 rate-limited`` with
``Retry-After``. Tests set ``app.state.ai_limits``.

**Audit (M4).** :func:`record_call` writes one ``ai_call`` row per call with
the tool, the validated parameters, the status, the byte count and the
response body exactly as sent, in the request's transaction (a refused call
is recorded too: the router answers it as a problem response instead of
raising, so the row commits). :func:`search_calls` lets the owner search the
stored responses for a string.

**Scrubber.** :func:`scrubber_for` builds the per-request
:class:`~app.core.scrubber.Scrubber` from the household members' display
names, usernames and e-mail addresses, the family-name words of the
household's name, plus the client's aliases (``[MEMBER]``) and custom
denylist (``[REDACTED]``), and every stored ``account.number_last4`` of the
household (``[ACCT]`` wherever it stands alone: "Sapphire 6611"). Former
members are not known to it (a removed membership leaves no name behind); the
owner adds them as aliases (AI_ACCESS.md "Free-text scrubbing").
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection, Request

from app.auth.authz import role_at_least, step_up_is_fresh
from app.auth.models import ApiToken, HouseholdRole
from app.auth.principal import Principal, generate_api_token
from app.auth.ratelimit import Limit, get_rate_limiter
from app.auth.tokens import TOKEN_MAX_DAYS_CAP
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.scrubber import Scrubber, member_terms
from app.core.settings import Settings
from app.db.models import Household
from app.domain.accounts.models import Account
from app.domain.ai.models import (
    DATE_RANGE_CAP_MAX,
    NAME_MAX,
    ROUNDING_STEPS,
    TERM_MAX,
    TERMS_MAX,
    AiCall,
    AiClient,
    AiMode,
    AiTransport,
)
from app.domain.ai.policy import ClientPolicy
from app.domain.households.repo import list_members
from app.domain.notifications.routing import notify

AI_SCOPE: Final = "ai:read"
#: The path prefix of the AI views (``app.auth.deps`` confines AI tokens to it).
AI_PATH_PREFIX: Final = "/api/v1/ai/v1"
#: Most live AI clients per household.
MAX_ACTIVE_CLIENTS: Final = 20


PER_MINUTE: Final = Limit("ai:client:minute", 60, timedelta(minutes=1))
PER_TEN_MINUTES: Final = Limit("ai:client:10min", 300, timedelta(minutes=10))


@dataclass(frozen=True)
class AiLimits:
    """Per-client rate limits of the AI views (``app.state.ai_limits``)."""

    per_minute: Limit = PER_MINUTE
    per_ten_minutes: Limit = PER_TEN_MINUTES


DEFAULT_AI_LIMITS: Final = AiLimits()


def get_ai_limits(conn: HTTPConnection) -> AiLimits:
    limits = getattr(conn.app.state, "ai_limits", None)
    return limits if isinstance(limits, AiLimits) else DEFAULT_AI_LIMITS


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


def client_not_found() -> ProblemError:
    return ProblemError(404, "ai-client-not-found", "AI client not found")


def ai_client_required() -> ProblemError:
    return ProblemError(
        403,
        "ai-client-required",
        "AI client token required",
        "These views answer only the token of an active AI client.",
    )


def _owner_household(principal: Principal) -> uuid.UUID:
    if principal.household_id is None or not role_at_least(principal.role, HouseholdRole.OWNER):
        raise ProblemError(
            403, "forbidden", "Forbidden", "Only the household owner manages AI clients."
        )
    return principal.household_id


# ---------------------------------------------------------------------------
# Client lifecycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClientOptions:
    """What the owner chooses for a new client (validated by :func:`check_options`)."""

    name: str
    mode: AiMode = AiMode.DETAIL
    transport: AiTransport = AiTransport.STDIO
    rounding: int | None = None
    hide_nicknames: bool = False
    hide_merchant_location: bool = False
    date_range_cap: int | None = None
    custom_denylist: Sequence[str] = field(default_factory=tuple)
    member_aliases: Sequence[str] = field(default_factory=tuple)
    expires_in_days: int = 90


def _clean_terms(terms: Sequence[str], what: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = " ".join(term.split())
        if not clean:
            continue
        if len(clean) > TERM_MAX or any(ord(ch) < 32 for ch in clean):
            raise ProblemError(
                422,
                "invalid-ai-options",
                "Invalid AI client options",
                f"{what}: entry too long or not printable.",
            )
        if clean.casefold() not in seen:
            seen.add(clean.casefold())
            out.append(clean)
    if len(out) > TERMS_MAX:
        raise ProblemError(
            422,
            "invalid-ai-options",
            "Invalid AI client options",
            f"{what}: at most {TERMS_MAX} entries.",
        )
    return out


def check_options(options: ClientOptions, settings: Settings) -> ClientOptions:
    """Validate (and normalise) client options; ``422 invalid-ai-options``."""

    def bad(detail: str) -> ProblemError:
        return ProblemError(422, "invalid-ai-options", "Invalid AI client options", detail)

    name = " ".join(options.name.split())
    if not name or len(name) > NAME_MAX:
        raise bad(f"The name must be 1-{NAME_MAX} characters.")
    if options.rounding is not None and options.rounding not in ROUNDING_STEPS:
        raise bad("Rounding is 1, 10 or 100.")
    if options.date_range_cap is not None and not 1 <= options.date_range_cap <= DATE_RANGE_CAP_MAX:
        raise bad(f"The date range cap is 1-{DATE_RANGE_CAP_MAX} days.")
    max_days = min(settings.token_max_days, TOKEN_MAX_DAYS_CAP)
    if not 1 <= options.expires_in_days <= max_days:
        raise ProblemError(
            422,
            "expiry-too-long",
            "Expiry too long",
            f"A client token may live at most {max_days} days.",
            {"max_days": max_days},
        )
    return ClientOptions(
        name=name,
        mode=options.mode,
        transport=options.transport,
        rounding=options.rounding,
        hide_nicknames=options.hide_nicknames,
        hide_merchant_location=options.hide_merchant_location,
        date_range_cap=options.date_range_cap,
        custom_denylist=tuple(_clean_terms(options.custom_denylist, "Custom denylist")),
        member_aliases=tuple(_clean_terms(options.member_aliases, "Member aliases")),
        expires_in_days=options.expires_in_days,
    )


@dataclass(frozen=True)
class CreatedClient:
    """A new client, its token row and the raw token (shown once)."""

    client: AiClient
    token: ApiToken
    raw_token: str = field(repr=False)


def _client_state(client: AiClient) -> dict[str, Any]:
    # Never the token or the denylist/alias terms (identity data); counts only.
    return {
        "name": client.name,
        "mode": client.mode.value,
        "transport": client.transport.value,
        "rounding": client.rounding,
        "hide_nicknames": client.hide_nicknames,
        "hide_merchant_location": client.hide_merchant_location,
        "date_range_cap": client.date_range_cap,
        "custom_denylist_count": len(client.custom_denylist or ()),
        "member_aliases_count": len(client.member_aliases or ()),
        "expires_at": client.expires_at,
    }


def create_client(
    db: Session,
    principal: Principal,
    options: ClientOptions,
    *,
    settings: Settings,
    request: Request | None = None,
    now: datetime | None = None,
) -> CreatedClient:
    """Create an AI client and mint its ``ai:read`` token (module doc)."""
    now = now or clock.utcnow()
    if principal.auth_method != "session":
        raise ProblemError(
            403, "session-required", "Browser session required", "Use the web app for this."
        )
    household_id = _owner_household(principal)
    if not step_up_is_fresh(principal.mfa_verified_at, now):
        raise ProblemError(
            403,
            "step-up-required",
            "Recent verification required",
            "Verify your second factor again to continue.",
        )
    if principal.user.is_breakglass:
        raise ProblemError(403, "breakglass-account", "Not available for break-glass accounts")
    clean = check_options(options, settings)
    live = db.execute(
        select(func.count())
        .select_from(AiClient)
        .where(
            AiClient.household_id == household_id,
            AiClient.revoked_at.is_(None),
            AiClient.expires_at > now,
        )
    ).scalar_one()
    if int(live) >= MAX_ACTIVE_CLIENTS:
        raise ProblemError(
            409,
            "too-many-ai-clients",
            "Too many AI clients",
            f"Revoke a client first; at most {MAX_ACTIVE_CLIENTS} may be active.",
        )
    expires_at = now + timedelta(days=clean.expires_in_days)
    material = generate_api_token()
    token = ApiToken(
        household_id=household_id,
        user_id=principal.user_id,
        prefix=material.prefix,
        token_hash=material.token_hash,
        name=f"AI client: {clean.name}"[:100],
        scopes=[AI_SCOPE],
        expires_at=expires_at,
    )
    db.add(token)
    db.flush()
    client = AiClient(
        household_id=household_id,
        name=clean.name,
        mode=clean.mode,
        transport=clean.transport,
        rounding=clean.rounding,
        hide_nicknames=clean.hide_nicknames,
        hide_merchant_location=clean.hide_merchant_location,
        date_range_cap=clean.date_range_cap,
        custom_denylist=list(clean.custom_denylist),
        member_aliases=list(clean.member_aliases),
        token_id=token.id,
        created_by=principal.user_id,
        expires_at=expires_at,
    )
    db.add(client)
    db.flush()
    audit(
        db,
        action="ai_client.create",
        target_type="ai_client",
        target_id=client.id,
        after={**_client_state(client), "token_prefix": token.prefix},
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    notify(db, household_id, "ai_client.created", source_id=client.id)
    return CreatedClient(client=client, token=token, raw_token=material.raw)


def list_clients(db: Session, principal: Principal) -> list[AiClient]:
    household_id = _owner_household(principal)
    rows = db.execute(
        select(AiClient)
        .where(AiClient.household_id == household_id)
        .order_by(AiClient.created_at.desc(), AiClient.id.desc())
    ).scalars()
    return list(rows)


def get_client(db: Session, principal: Principal, client_id: uuid.UUID) -> AiClient:
    household_id = _owner_household(principal)
    row = db.execute(
        select(AiClient).where(AiClient.id == client_id, AiClient.household_id == household_id)
    ).scalar_one_or_none()
    if row is None:
        raise client_not_found()
    return row


def revoke_client(
    db: Session,
    principal: Principal,
    client_id: uuid.UUID,
    *,
    request: Request | None = None,
    now: datetime | None = None,
) -> AiClient:
    """Revoke the client and its token (idempotent)."""
    now = now or clock.utcnow()
    client = get_client(db, principal, client_id)
    if client.revoked_at is None:
        client.revoked_at = now
        if client.token_id is not None:
            token = db.get(ApiToken, client.token_id)
            if token is not None and token.revoked_at is None:
                token.revoked_at = now
        db.flush()
        audit(
            db,
            action="ai_client.revoke",
            target_type="ai_client",
            target_id=client.id,
            before={"name": client.name},
            household_id=client.household_id,
            user_id=principal.user_id,
            request=request,
        )
    return client


# ---------------------------------------------------------------------------
# Request context
# ---------------------------------------------------------------------------


def resolve_client(db: Session, principal: Principal, *, now: datetime | None = None) -> AiClient:
    """The active client behind an AI token principal (module doc), else ``403``."""
    now = now or clock.utcnow()
    if principal.auth_method != "token" or principal.token_id is None or not principal.ai_only:
        raise ai_client_required()
    if principal.household_id is None:
        raise ai_client_required()
    if not principal.has_scope(AI_SCOPE):
        # The owner who created the client is no longer an owner.
        raise ProblemError(
            403,
            "insufficient-scope",
            "Insufficient scope",
            f"This token lacks the {AI_SCOPE!r} scope.",
        )
    client = db.execute(
        select(AiClient).where(
            AiClient.token_id == principal.token_id,
            AiClient.household_id == principal.household_id,
        )
    ).scalar_one_or_none()
    if client is None or client.revoked_at is not None or client.expires_at <= now:
        raise ai_client_required()
    if client.last_used_at is None or (now - client.last_used_at).total_seconds() >= 60:
        client.last_used_at = now
        db.flush()
    return client


def check_rate(request: HTTPConnection, client: AiClient) -> None:
    """Count one call of ``client``; ``429 rate-limited`` beyond its limits (M6)."""
    limits = get_ai_limits(request)
    limiter = get_rate_limiter(request)
    key = str(client.id)
    for limit in (limits.per_minute, limits.per_ten_minutes):
        limiter.peek(limit, key).raise_if_denied()
    for limit in (limits.per_minute, limits.per_ten_minutes):
        limiter.hit(limit, key).raise_if_denied()


def scrubber_for(db: Session, household_id: uuid.UUID, policy: ClientPolicy) -> Scrubber:
    """The scrubber for one request: member identity, aliases, the denylist and
    the household's stored account-number fragments."""
    users = [user for _, user in list_members(db, household_id)]
    household_name = db.execute(
        select(Household.name).where(Household.id == household_id)
    ).scalar_one_or_none()
    terms = member_terms(
        names=[u.display_name for u in users],
        usernames=[u.username for u in users],
        emails=[u.email for u in users],
        aliases=policy.member_aliases,
        # The household's name is usually a family name (P6B-SR-R1-04).
        household_names=[household_name],
    )
    # Every stored account-number fragment of the household (last four
    # digits): "Sapphire 6611" in a name or memo is the account's own number
    # (P6B-SR-R2-01). Hidden and closed accounts too: more is safer.
    numbers = db.execute(
        select(Account.number_last4).where(
            Account.household_id == household_id, Account.number_last4.is_not(None)
        )
    ).scalars()
    return Scrubber(
        member_terms=terms, denylist=policy.custom_denylist, account_numbers=list(numbers)
    )


# ---------------------------------------------------------------------------
# Call audit
# ---------------------------------------------------------------------------


def record_call(
    db: Session,
    client: AiClient,
    *,
    tool: str,
    params: dict[str, Any],
    status: int,
    body: bytes,
    response: Any,
) -> AiCall:
    """One ``ai_call`` row: the response exactly as sent and its size (M4)."""
    row = AiCall(
        household_id=client.household_id,
        ai_client_id=client.id,
        tool=tool,
        params=params,
        status=status,
        bytes=len(body),
        response=response,
    )
    db.add(row)
    db.flush()
    return row


def search_calls(
    db: Session,
    principal: Principal,
    *,
    client_id: uuid.UUID | None = None,
    contains: str | None = None,
    limit: int = 50,
) -> list[AiCall]:
    """The household's AI calls, newest first; ``contains`` searches the responses.

    The search is a case-insensitive substring match on the stored response
    text (bound parameter, no pattern syntax: ``%`` and ``_`` are escaped).
    """
    household_id = _owner_household(principal)
    stmt = select(AiCall).where(AiCall.household_id == household_id)
    if client_id is not None:
        stmt = stmt.where(AiCall.ai_client_id == client_id)
    if contains:
        escaped = contains.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(cast(AiCall.response, String).ilike(f"%{escaped}%", escape="\\"))
    limit = max(1, min(limit, 500))
    rows = db.execute(stmt.order_by(AiCall.created_at.desc(), AiCall.id.desc()).limit(limit))
    return list(rows.scalars())
