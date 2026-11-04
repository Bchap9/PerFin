# SPDX-License-Identifier: AGPL-3.0-or-later
"""FastAPI dependencies resolving the caller (SECURITY.md A4/A5, review SR-8).

:func:`get_optional_principal` authenticates **either** a session cookie
**or** an ``Authorization: Bearer`` personal access token:

* If an ``Authorization`` header is present at all, only that header counts.
  A malformed, unknown, expired or revoked token, or any other scheme, is a
  ``401`` even when a valid session cookie is also sent. This matters because
  ``app.core.csrf`` exempts ``Bearer`` requests from the CSRF token: falling
  back to the cookie would let a cross-site form ride the session by adding a
  junk ``Authorization`` header (it cannot, without a CORS preflight, but the
  rule must not depend on that).
* Bearer authentication (``app.auth.tokens.authenticate_bearer``) is rate
  limited: failed attempts per client IP, use per token (``429``).
* Otherwise the session cookie (``app.auth.sessions``); no or a dead cookie
  means anonymous.
* A break-glass session (``login_method = breakglass``, SECURITY.md B7) is
  honoured only from ``PERFIN_BREAKGLASS_CIDRS`` on **every** request, not
  just at login (:func:`session_acceptor`): from anywhere else the cookie
  counts as absent and is not even touched, so a stolen break-glass cookie
  is useless off the allowlisted network.

Once resolved, the request's database transaction carries the RLS context
(household, user, instance-admin flag) and ``request.state.principal`` holds
the :class:`~app.auth.principal.Principal`. Endpoints normally depend on the
guards in ``app.auth.authz`` instead of using this module directly.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth.local import from_breakglass_network
from app.auth.models import LoginMethod, SessionStage, UserSession
from app.auth.principal import Principal, authenticate_session
from app.auth.sessions import SessionPolicy, load_session, raw_session_id
from app.auth.tokens import authenticate_bearer
from app.core.crypto import Keyring
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.core.request_context import client_ip
from app.core.settings import Settings
from app.db.session import get_db

log = get_logger("perfin.auth")


def get_app_settings(request: Request) -> Settings:
    """The settings the app was built with (``app.state.settings``)."""
    settings = request.app.state.settings
    if not isinstance(settings, Settings):
        raise RuntimeError("app.state.settings is not configured")
    return settings


def get_session_policy(request: Request) -> SessionPolicy:
    return SessionPolicy.from_settings(get_app_settings(request))


def get_keyring(request: Request) -> Keyring:
    """The master-key keyring, loaded once per app (``app.state.keyring``)."""
    keyring = getattr(request.app.state, "keyring", None)
    if not isinstance(keyring, Keyring):
        keyring = Keyring.from_settings(get_app_settings(request))
        request.app.state.keyring = keyring
    return keyring


def unauthenticated(detail: str = "Authentication is required.") -> ProblemError:
    return ProblemError(401, "unauthenticated", "Not authenticated", detail)


# scope="function": the transaction commits before the response is sent, so
# a client that got a 2xx reads its own write on the next request and a failed
# commit is a 500, not a success (FastAPI's default "request" scope ends the
# dependency after the response; tests/integration/test_commit_before_response.py).
DbSession = Annotated[Session, Depends(get_db, scope="function")]


def session_acceptor(request: Request) -> Callable[[UserSession], bool]:
    """Per-request conditions on a session row (``load_session(accept=...)``).

    A break-glass session is accepted only while the request comes from
    ``PERFIN_BREAKGLASS_CIDRS`` (trusted-proxy-aware client address, as at
    login). Every other session is accepted.
    """
    settings = get_app_settings(request)
    ip = client_ip(request)

    def accept(row: UserSession) -> bool:
        if row.login_method is not LoginMethod.BREAKGLASS:
            return True
        if from_breakglass_network(settings, ip):
            return True
        log.warning(
            "break-glass session presented from outside PERFIN_BREAKGLASS_CIDRS",
            extra={"session_id": str(row.id)},
        )
        return False

    return accept


def get_optional_principal(request: Request, db: DbSession) -> Principal | None:
    """The authenticated caller, ``None`` when anonymous (see module doc)."""
    cached = getattr(request.state, "principal", None)
    if isinstance(cached, Principal):
        return cached
    settings = get_app_settings(request)
    header = request.headers.get("authorization")
    principal: Principal | None
    if header is not None:
        # Rate limited per IP (failures) and per token (use); 401 or 429.
        principal = authenticate_bearer(request, db, header, settings=settings)
    else:
        principal = authenticate_session(
            db,
            raw_session_id(request, settings),
            policy=SessionPolicy.from_settings(settings),
            require_mfa=settings.require_mfa,
            accept=session_acceptor(request),
        )
    if principal is not None:
        request.state.principal = principal
    return principal


OptionalPrincipal = Annotated[Principal | None, Depends(get_optional_principal)]


def require_principal(principal: OptionalPrincipal) -> Principal:
    """Any authenticated caller, including an MFA-restricted one.

    Only MFA enrolment, ``GET /auth/session`` and logout should use this
    directly; everything else uses ``app.auth.authz.require_authenticated``.
    """
    if principal is None:
        raise unauthenticated()
    return principal


def get_pending_mfa_session(request: Request, db: DbSession) -> UserSession:
    """The caller's ``mfa_pending`` session (for ``POST /auth/mfa/verify``).

    Refuses requests with an ``Authorization`` header (tokens never complete
    a login) and anything that is not a live pending session.
    """
    if request.headers.get("authorization") is not None:
        raise unauthenticated()
    settings = get_app_settings(request)
    row = load_session(
        db,
        raw_session_id(request, settings),
        policy=SessionPolicy.from_settings(settings),
        for_update=True,
        accept=session_acceptor(request),
    )
    if row is None or row.stage is not SessionStage.MFA_PENDING:
        raise unauthenticated("No login is waiting for a second factor.")
    return row


AuthenticatedPrincipal = Annotated[Principal, Depends(require_principal)]
