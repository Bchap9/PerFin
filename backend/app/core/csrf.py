# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSRF protection (SECURITY.md A5, AUTH.md "CSRF").

Every state-changing request (``POST``/``PUT``/``PATCH``/``DELETE``) under
``/api/v1`` passes two independent checks in :class:`CSRFMiddleware`:

1. **Provenance.** ``Sec-Fetch-Site`` must be ``same-origin`` (or ``none``,
   a user-initiated navigation) when present, and ``Origin`` must equal the
   origin of ``PERFIN_PUBLIC_URL`` when present. ``same-site`` is rejected
   too: a sibling subdomain is not trusted.
2. **Token.** ``X-CSRF-Token`` must carry a token bound to the caller:

   * with a session: ``HMAC(key, session_id)``. The session id comes from a
     pluggable resolver (``get_session_id(request) -> str | None``) that the
     auth layer registers with :meth:`CSRFProtect.set_session_id_resolver`;
     until Phase 1 it always returns ``None``. The resolver must be cheap and
     must not touch the database (it reads the session cookie).
   * without a session (pre-auth endpoints such as ``/auth/login``): a
     *signed* double-submit. :meth:`CSRFProtect.issue_pre_auth` sets the
     ``__Host-perfin-pre`` cookie to a random nonce and returns
     ``HMAC(key, nonce)``, which the SPA echoes in ``X-CSRF-Token``. An
     attacker who can inject a cookie still cannot compute the token.

Requests that carry ``Authorization: Bearer`` are exempt from the token
check: they are not cookie-authenticated, and a cross-site page cannot set
that header without a CORS preflight, which PerFin never grants. **The auth
layer must therefore never fall back to the session cookie when an
``Authorization`` header is present.** Paths in ``exempt_paths`` (the OIDC
back-channel logout, which the IdP posts server-to-server and which is
authenticated by its signed logout token) skip both checks.

``SameSite=Lax`` on the session cookie is defence in depth, not the control.

Phase 1 wiring::

    csrf = get_csrf(request)
    csrf.set_session_id_resolver(app.auth.sessions.session_id_from_cookie)  # once
    token = csrf.token_for_session(session_id)  # GET /auth/session
    token = csrf.issue_pre_auth(response)       # GET /auth/session, no session
    csrf.clear_pre_auth(response)               # after login succeeds
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import secrets
from collections.abc import Callable, Collection
from urllib.parse import urlsplit

from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.requests import HTTPConnection, Request
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import problem_response

CSRF_HEADER = "X-CSRF-Token"
PRE_AUTH_COOKIE = "__Host-perfin-pre"
#: Methods that never change state; every other method (POST, PUT, PATCH,
#: DELETE and any extension method such as PROPFIND) is checked. An allowlist,
#: so a route registered with an unusual method cannot skip the check.
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
#: The common unsafe methods (kept for callers and tests; not used to decide).
UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DEFAULT_EXEMPT_PATHS = frozenset({"/api/v1/auth/oidc/backchannel-logout"})

_SESSION_CONTEXT = b"perfin-csrf-session-v1\x00"
_PRE_AUTH_CONTEXT = b"perfin-csrf-pre-auth-v1\x00"
_KEY_CONTEXT = b"perfin/csrf/v1"
_NONCE_BYTES = 32
_MAX_TOKEN_LENGTH = 256
_ALLOWED_FETCH_SITES = frozenset({"same-origin", "none"})
_DEFAULT_PORTS = {"http": 80, "https": 443}

SessionIdResolver = Callable[[HTTPConnection], str | None]

logger = logging.getLogger("perfin.csrf")


def no_session(_conn: HTTPConnection) -> str | None:
    """Default resolver until the auth layer registers one (Phase 1)."""
    return None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def normalize_origin(url: str) -> str | None:
    """``scheme://host[:port]`` in canonical form, or ``None`` if not an origin."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if scheme not in _DEFAULT_PORTS or not host:
        return None
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    if port is None or port == _DEFAULT_PORTS[scheme]:
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def _has_bearer(headers: Headers) -> bool:
    scheme, _, credentials = headers.get("authorization", "").partition(" ")
    return scheme.lower() == "bearer" and bool(credentials.strip())


class CSRFProtect:
    """Token derivation and request checking; one per app (``app.state.csrf``)."""

    def __init__(
        self,
        *,
        secret_key: bytes,
        public_origin: str,
        exempt_paths: Collection[str] = DEFAULT_EXEMPT_PATHS,
        get_session_id: SessionIdResolver = no_session,
    ) -> None:
        if len(secret_key) < 16:
            raise ValueError("CSRF secret key is too short")
        origin = normalize_origin(public_origin)
        if origin is None:
            raise ValueError("public origin must be an http(s) URL")
        self._key = hmac.new(secret_key, _KEY_CONTEXT, hashlib.sha256).digest()
        self.public_origin = origin
        self.exempt_paths = frozenset(exempt_paths)
        self._get_session_id = get_session_id

    # ---- plug-in point -------------------------------------------------------
    def set_session_id_resolver(self, resolver: SessionIdResolver) -> None:
        """Register the auth layer's ``get_session_id(request)``."""
        self._get_session_id = resolver

    def session_id(self, conn: HTTPConnection) -> str | None:
        return self._get_session_id(conn)

    # ---- tokens --------------------------------------------------------------
    def _mac(self, context: bytes, value: str) -> str:
        return _b64(hmac.new(self._key, context + value.encode("utf-8"), hashlib.sha256).digest())

    def token_for_session(self, session_id: str) -> str:
        """The CSRF token bound to ``session_id`` (returned by ``GET /auth/session``)."""
        return self._mac(_SESSION_CONTEXT, session_id)

    def pre_auth_token(self, nonce: str) -> str:
        return self._mac(_PRE_AUTH_CONTEXT, nonce)

    def issue_pre_auth(self, response: Response) -> str:
        """Set a fresh pre-auth nonce cookie on ``response``; return its token."""
        nonce = secrets.token_urlsafe(_NONCE_BYTES)
        response.set_cookie(
            PRE_AUTH_COOKIE,
            nonce,
            path="/",
            secure=True,  # required by the __Host- prefix (localhost counts as secure)
            httponly=True,
            samesite="strict",
        )
        return self.pre_auth_token(nonce)

    def clear_pre_auth(self, response: Response) -> None:
        response.delete_cookie(
            PRE_AUTH_COOKIE, path="/", secure=True, httponly=True, samesite="strict"
        )

    # ---- checks --------------------------------------------------------------
    def check_provenance(self, headers: Headers) -> str | None:
        """Return a failure reason if the request is not same-origin."""
        site = headers.get("sec-fetch-site")
        if site is not None and site.strip().lower() not in _ALLOWED_FETCH_SITES:
            return "cross-site request"
        origin = headers.get("origin")
        if origin is not None and normalize_origin(origin) != self.public_origin:
            return "origin mismatch"
        return None

    def check_token(self, conn: HTTPConnection) -> str | None:
        """Return a failure reason if ``X-CSRF-Token`` is missing or wrong."""
        supplied = conn.headers.get(CSRF_HEADER, "")
        if not supplied or len(supplied) > _MAX_TOKEN_LENGTH:
            return "missing token"
        session_id = self.session_id(conn)
        if session_id:
            expected = self.token_for_session(session_id)
        else:
            nonce = conn.cookies.get(PRE_AUTH_COOKIE)
            if not nonce or len(nonce) > _MAX_TOKEN_LENGTH:
                return "missing pre-auth cookie"
            expected = self.pre_auth_token(nonce)
        if not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("ascii")):
            return "token mismatch"
        return None

    def requires_check(self, method: str, path: str, api_prefix: str) -> bool:
        if method.upper() in SAFE_METHODS:
            return False
        if path != api_prefix and not path.startswith(api_prefix + "/"):
            return False
        return path.rstrip("/") not in self.exempt_paths

    def verify(self, conn: HTTPConnection) -> str | None:
        """Both checks for an unsafe request; ``None`` means allowed."""
        reason = self.check_provenance(conn.headers)
        if reason is not None:
            return reason
        if _has_bearer(conn.headers):
            return None
        return self.check_token(conn)


class CSRFMiddleware:
    """Apply :meth:`CSRFProtect.verify` to unsafe ``/api/v1`` requests."""

    def __init__(self, app: ASGIApp, *, protect: CSRFProtect, api_prefix: str) -> None:
        self.app = app
        self.protect = protect
        self.api_prefix = api_prefix.rstrip("/")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self.protect.requires_check(
            scope.get("method", "GET"), scope.get("path", ""), self.api_prefix
        ):
            reason = self.protect.verify(HTTPConnection(scope))
            if reason is not None:
                logger.info("csrf rejected", extra={"reason": reason})
                response = problem_response(
                    403,
                    "csrf-failed",
                    "CSRF check failed",
                    "The request is missing a valid CSRF token or did not come from this site.",
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def get_csrf(conn: HTTPConnection | Request) -> CSRFProtect:
    """The app's :class:`CSRFProtect` (``request.app.state.csrf``)."""
    protect = getattr(conn.app.state, "csrf", None)
    if not isinstance(protect, CSRFProtect):
        raise RuntimeError("CSRF protection is not installed on this app")
    return protect


def install_csrf(app: FastAPI, protect: CSRFProtect, *, api_prefix: str) -> None:
    """Expose ``protect`` on ``app.state.csrf`` and add the middleware."""
    app.state.csrf = protect
    app.add_middleware(CSRFMiddleware, protect=protect, api_prefix=api_prefix)
