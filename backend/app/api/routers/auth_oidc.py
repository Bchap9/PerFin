# SPDX-License-Identifier: AGPL-3.0-or-later
"""OIDC login, link confirmation and logout (AUTH.md "OIDC (Authentik)").

Browser navigations (redirects, never JSON to the user):

* ``GET /auth/oidc/start?return_to=/path``: sets the sealed pre-auth cookie
  (state, nonce, PKCE verifier, ``return_to``; 5 minutes) and redirects to
  the IdP. ``return_to`` must be a same-origin relative path, else ``/``
  (B4). ``reauth=true`` asks the IdP to authenticate again even with an SSO
  session (``prompt=login&max_age=0``): the step-up path for a user who
  relies on trusted IdP MFA (A6; only such a login, with an ID token
  ``auth_time`` not older than its start, is a step-up, and the window runs
  from that ``auth_time``; review P1-SR3-01). An unreachable or misconfigured IdP redirects to
  ``/login?oidc_error=idp-unavailable``.
* ``GET /auth/oidc/callback``: completes the login (``app.auth.oidc.flow``)
  and redirects to ``return_to`` (full session), to
  ``/login/mfa?return_to=...`` (``mfa_pending``: the user has a local second
  factor and the IdP's MFA is not trusted), to ``/login/link-oidc`` (an
  account with the same verified email may be linked) or to
  ``/login?oidc_error=<code>``. The pre-auth cookie is always cleared.

JSON endpoints:

* ``GET /auth/oidc/status``: whether OIDC login is offered.
* ``GET /auth/oidc/link``: the pending link offer (email, expiry) or ``404``.
* ``POST /auth/oidc/link``: confirm it after a fresh **local** login to that
  account (password, plus MFA when enrolled); ``204``. Browser sessions
  only: a bearer caller is ``403 session-required`` before anything else.
* ``DELETE /auth/oidc/link``: discard the offer; ``204``.
* ``POST /auth/oidc/logout``: RP-initiated logout: revokes this session and
  returns ``{"redirect_url"}`` (the IdP's end-session URL with
  ``id_token_hint`` and an allowlisted ``post_logout_redirect_uri``, or
  ``null`` for a non-OIDC session or an IdP without one).
* ``POST /auth/oidc/backchannel-logout``: the IdP posts a signed
  ``logout_token`` (form field); matching sessions are revoked. CSRF-exempt
  (server to server, authenticated by the token signature). A token needs a
  ``jti`` and works once (a replay is refused); ``200`` or a
  ``400 invalid-logout-token`` problem that also carries the OAuth member
  ``"error": "invalid_request"`` (Back-Channel Logout 1.0, section 2.8).

Every endpoint is ``404 oidc-disabled`` while ``PERFIN_OIDC_ISSUER`` is empty.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from app.auth.authz import require_authenticated
from app.auth.deps import DbSession, get_app_settings, get_keyring
from app.auth.local import audit_auth, audit_auth_committed, get_sessionmaker
from app.auth.oidc.client import (
    OidcClient,
    OidcDisabledError,
    clear_cookie,
    get_oidc_client,
    link_cookie_name,
    set_sealed_cookie,
    state_cookie_name,
)
from app.auth.oidc.discovery import DiscoveryError
from app.auth.oidc.flow import (
    CallbackParams,
    LinkRefusedError,
    OidcLoginError,
    PendingLink,
    complete_login,
    confirm_link,
    error_redirect,
)
from app.auth.oidc.logout import (
    allowed_post_logout_uri,
    consume_logout_jti,
    revoke_for_logout_token,
    rp_logout,
)
from app.auth.oidc.tokens import TokenValidationError, validate_logout_token
from app.auth.principal import Principal
from app.auth.ratelimit import OIDC_CALLBACK_PER_IP, Limit, RateLimiter, ip_key
from app.auth.sessions import (
    SessionPolicy,
    clear_session_cookie,
    load_session,
    raw_session_id,
    set_session_cookie,
)
from app.core.audit import RequestMeta
from app.core.csrf import get_csrf
from app.core.errors import ProblemError, problem_response
from app.core.logging import get_logger
from app.core.settings import Settings

router = APIRouter(tags=["auth"])
log = get_logger(__name__)

BACKCHANNEL_PER_IP = Limit("oidc_bcl:ip", 60, timedelta(minutes=5))
MAX_LOGOUT_TOKEN = 16 * 1024


def _problems(*statuses: int) -> dict[int | str, dict[str, Any]]:
    return {
        code: {"description": "Problem details", "content": {"application/problem+json": {}}}
        for code in statuses
    }


def oidc_disabled() -> ProblemError:
    return ProblemError(
        404, "oidc-disabled", "Single sign-on is not configured", "OIDC login is not enabled."
    )


def _client(request: Request) -> OidcClient:
    try:
        return get_oidc_client(request)
    except OidcDisabledError:
        raise oidc_disabled() from None


def _settings(request: Request) -> Settings:
    settings = get_app_settings(request)
    if not settings.oidc_enabled:
        raise oidc_disabled()
    return settings


def _callback_limit(request: Request) -> Limit:
    """``oidc:ip`` (A1); ``app.state.oidc_callback_limit`` overrides it in tests."""
    limit = getattr(request.app.state, "oidc_callback_limit", None)
    return limit if isinstance(limit, Limit) else OIDC_CALLBACK_PER_IP


def _redirect(location: str) -> RedirectResponse:
    return RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class OidcStatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool


class LinkOfferOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    email: str
    expires_at: datetime


class OidcLogoutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    post_logout_redirect_uri: Annotated[str | None, Field(max_length=2048)] = None


class OidcLogoutOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    redirect_url: str | None


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------


@router.get("/auth/oidc/status", response_model=OidcStatusOut, summary="Is OIDC login offered")
def get_status(request: Request) -> OidcStatusOut:
    return OidcStatusOut(enabled=get_app_settings(request).oidc_enabled)


@router.get(
    "/auth/oidc/start",
    status_code=status.HTTP_303_SEE_OTHER,
    responses={303: {"description": "Redirect to the identity provider"}, **_problems(404)},
    summary="Start an OIDC login (redirects to the identity provider)",
)
def get_start(
    request: Request,
    return_to: Annotated[str | None, Query(max_length=2048)] = None,
    reauth: Annotated[bool, Query()] = False,
) -> Response:
    settings = _settings(request)
    client = _client(request)
    try:
        auth = client.begin(return_to, reauth=reauth)
    except DiscoveryError as exc:
        log.warning("OIDC discovery failed", extra={"error": str(exc)})
        return _redirect(error_redirect("idp-unavailable"))
    response = _redirect(auth.url)
    set_sealed_cookie(
        response, settings, state_cookie_name(settings), auth.cookie_value, samesite="lax"
    )
    return response


@router.get(
    "/auth/oidc/callback",
    status_code=status.HTTP_303_SEE_OTHER,
    responses={303: {"description": "Redirect into the app"}, **_problems(404)},
    summary="OIDC redirect URI (completes the login)",
)
def get_callback(
    request: Request,
    code: Annotated[str | None, Query(max_length=4096)] = None,
    state: Annotated[str | None, Query(max_length=512)] = None,
    error: Annotated[str | None, Query(max_length=256)] = None,
) -> Response:
    settings = _settings(request)
    client = _client(request)
    try:
        result = complete_login(
            request,
            settings,
            client,
            get_keyring(request),
            CallbackParams(code=code, state=state, error=error),
            previous_session_id=raw_session_id(request, settings),
            rate_limit=_callback_limit(request),
        )
    except OidcLoginError as exc:
        response = _redirect(error_redirect(exc.public))
    else:
        response = _redirect(result.redirect)
        if result.issued is not None:
            set_session_cookie(response, settings, result.issued)
            get_csrf(request).clear_pre_auth(response)
        if result.link_cookie is not None:
            set_sealed_cookie(
                response,
                settings,
                link_cookie_name(settings),
                result.link_cookie,
                samesite="strict",
            )
    clear_cookie(response, settings, state_cookie_name(settings), samesite="lax")
    return response


# ---------------------------------------------------------------------------
# Link by verified email
# ---------------------------------------------------------------------------


def _pending_link(request: Request, client: OidcClient, settings: Settings) -> PendingLink | None:
    value = request.cookies.get(link_cookie_name(settings))
    payload = client.sealer.unseal("link", value)
    link = PendingLink.from_payload(payload)
    if link is None or payload is None:
        return None
    return link


def _no_link() -> ProblemError:
    return ProblemError(
        404,
        "no-pending-link",
        "Nothing to link",
        "Sign in with the identity provider again to link your account.",
    )


@router.get(
    "/auth/oidc/link",
    response_model=LinkOfferOut,
    responses=_problems(404),
    summary="The pending offer to link an identity-provider login to a local account",
)
def get_link(request: Request) -> LinkOfferOut:
    settings = _settings(request)
    client = _client(request)
    payload = client.sealer.unseal("link", request.cookies.get(link_cookie_name(settings)))
    link = PendingLink.from_payload(payload)
    if link is None or payload is None:
        raise _no_link()
    return LinkOfferOut(
        email=link.email, expires_at=datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
    )


def require_link_session(
    principal: Annotated[Principal, Depends(require_authenticated)],
) -> Principal:
    """A browser session (``confirm_link`` re-checks it; this refuses tokens up front)."""
    if principal.auth_method != "session" or principal.session is None:
        raise ProblemError(
            403, "session-required", "Browser session required", "Use the web app for this."
        )
    return principal


@router.post(
    "/auth/oidc/link",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_problems(401, 403, 404, 409),
    summary="Link the identity-provider login after a fresh local login",
)
def post_link(
    request: Request,
    db: DbSession,
    principal: Annotated[Principal, Depends(require_link_session)],
) -> Response:
    settings = _settings(request)
    client = _client(request)
    link = _pending_link(request, client, settings)
    if link is None:
        raise _no_link()
    meta = RequestMeta.from_request(request)
    try:
        confirm_link(db, principal, link, issuer=client.issuer, meta=meta)
    except LinkRefusedError as exc:
        audit_auth_committed(
            get_sessionmaker(request),
            action="user.oidc_link_refused",
            user_id=principal.user.id,
            meta=meta,
            after={"reason": exc.reason},
        )
        if exc.reason == "already-linked":
            raise ProblemError(
                409, "already-linked", "Already linked", "This login is linked already."
            ) from None
        raise ProblemError(
            403,
            "link-refused",
            "Link not allowed",
            "Sign in to this account with its password (and second factor) to link it.",
            extra={"reason": exc.reason},
        ) from None
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_cookie(response, settings, link_cookie_name(settings), samesite="strict")
    return response


@router.delete(
    "/auth/oidc/link",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_problems(404),
    summary="Discard the pending link offer",
)
def delete_link(request: Request) -> Response:
    settings = _settings(request)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_cookie(response, settings, link_cookie_name(settings), samesite="strict")
    return response


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


@router.post(
    "/auth/oidc/logout",
    response_model=OidcLogoutOut,
    responses=_problems(400),
    summary="Log out here and at the identity provider (RP-initiated logout)",
)
def post_oidc_logout(request: Request, db: DbSession, body: OidcLogoutIn | None = None) -> Response:
    settings = _settings(request)
    client = _client(request)
    if request.headers.get("authorization") is not None:
        raise ProblemError(
            400, "session-required", "Browser session required", "Log out from the web app."
        )
    requested = body.post_logout_redirect_uri if body is not None else None
    target = allowed_post_logout_uri(settings, requested)
    if target is None:
        raise ProblemError(
            400,
            "invalid-redirect",
            "Redirect not allowed",
            "post_logout_redirect_uri is not on the allowlist.",
        )
    row = load_session(
        db,
        raw_session_id(request, settings),
        policy=SessionPolicy.from_settings(settings),
        touch=False,
        for_update=True,
    )
    redirect_url: str | None = None
    if row is not None:
        result = rp_logout(db, get_keyring(request), row)
        audit_auth(
            db,
            action="user.logout",
            user_id=result.user_id,
            meta=RequestMeta.from_request(request),
            target_type="user_session",
            target_id=result.session_id,
            after={"method": "oidc_rp" if result.was_oidc else "local"},
        )
        if result.was_oidc:
            try:
                redirect_url = client.end_session_url(
                    id_token_hint=result.id_token_hint, post_logout_redirect_uri=target
                )
            except DiscoveryError:
                redirect_url = None
    response = JSONResponse(OidcLogoutOut(redirect_url=redirect_url).model_dump())
    clear_session_cookie(response, settings)
    return response


def _backchannel_error() -> JSONResponse:
    # Problem details like every API error; the OAuth ``error`` member is what
    # Back-Channel Logout 1.0 (section 2.8) lets the IdP read.
    return problem_response(
        status.HTTP_400_BAD_REQUEST,
        "invalid-logout-token",
        "Invalid logout token",
        extra={"error": "invalid_request"},
    )


@router.post(
    "/auth/oidc/backchannel-logout",
    status_code=status.HTTP_200_OK,
    responses={400: {"description": "Invalid logout token"}, 429: {"description": "Rate limited"}},
    summary="OIDC back-channel logout (posted by the identity provider)",
)
def post_backchannel_logout(
    request: Request,
    db: DbSession,
    logout_token: Annotated[str, Form(max_length=MAX_LOGOUT_TOKEN)],
) -> Response:
    _settings(request)
    client = _client(request)
    factory = get_sessionmaker(request)
    RateLimiter(factory).hit(BACKCHANNEL_PER_IP, ip_key(request)).raise_if_denied()
    meta = RequestMeta.from_request(request)
    try:
        token = validate_logout_token(
            logout_token, issuer=client.issuer, client_id=client.client_id, keys=client.keys
        )
    except TokenValidationError as exc:
        audit_auth_committed(
            factory,
            action="oidc.backchannel_logout_rejected",
            user_id=None,
            meta=meta,
            after={"reason": exc.reason},
        )
        return _backchannel_error()
    # Recorded in the request transaction together with the revocation
    # (review P1-SR2-03): a failed revocation rolls the jti back, so the IdP's
    # retry of the same token is not refused as a replay.
    if not consume_logout_jti(db, token):
        # A replayed token (review P1-SR-03): it would end sessions opened
        # after the logout it was issued for.
        audit_auth_committed(
            factory,
            action="oidc.backchannel_logout_rejected",
            user_id=None,
            meta=meta,
            after={"reason": "replayed-jti"},
        )
        return _backchannel_error()
    result = revoke_for_logout_token(db, token)
    for user_id in result.users:
        audit_auth(
            db,
            action="user.logout",
            user_id=user_id,
            meta=meta,
            after={"method": "oidc_backchannel", "sid": token.sid is not None},
        )
    return Response(status_code=status.HTTP_200_OK, headers={"Cache-Control": "no-store"})
