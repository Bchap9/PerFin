# SPDX-License-Identifier: AGPL-3.0-or-later
"""Break-glass sessions stay on the allowlisted network (SECURITY.md B7).

The CIDR is checked at login (``tests/integration/test_local_login.py``) and
again on every request (``app.auth.deps.session_acceptor``): a break-glass
session cookie presented from outside ``PERFIN_BREAKGLASS_CIDRS`` counts as
no session at all, for full and MFA-pending sessions alike, and does not
slide the idle expiry. Ordinary sessions are not affected by the address.
"""

from __future__ import annotations

from datetime import timedelta
from ipaddress import ip_network
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.local import AuthLimits
from app.auth.models import LoginMethod, SessionStage, UserSession
from app.auth.ratelimit import Limit, LockoutPolicy
from app.auth.sessions import hash_session_id
from app.core.settings import Settings
from app.main import API_PREFIX
from tests.fixtures.auth_clients import (
    SESSION_URL,
    browser,
    build_app,
    pre_auth_headers,
    session_headers,
)
from tests.fixtures.factories import Factories, UserRecord

pytestmark = pytest.mark.integration

LOGIN = f"{API_PREFIX}/auth/login"
VERIFY = f"{API_PREFIX}/auth/mfa/verify"
SESSIONS = f"{API_PREFIX}/auth/sessions"
COOKIE = "__Host-perfin-session"
INSIDE = "192.0.2.10"
OUTSIDE = "198.51.100.20"

ROOMY = AuthLimits(
    login_per_ip=Limit("login:ip", 1000, timedelta(minutes=5)),
    login_per_account=Limit("login:account", 1000, timedelta(minutes=15)),
    lockout=LockoutPolicy(delay_after=1000, lock_after=1001),
)


@pytest.fixture
def oidc_only(settings: Settings) -> FastAPI:
    custom = settings.model_copy(
        update={"local_login_enabled": False, "breakglass_cidrs": [ip_network("192.0.2.0/24")]}
    )
    return build_app(custom, limits=ROOMY)


def _login(client: TestClient, record: UserRecord) -> Any:
    response = client.post(
        LOGIN,
        json={"username": record.user.username, "password": record.password},
        headers=pre_auth_headers(client),
    )
    assert response.status_code == 200, response.text
    return response


def _row(factories: Factories, raw: str) -> UserSession:
    with factories.transaction() as db:
        row = db.execute(
            select(UserSession).where(UserSession.token_hash == hash_session_id(raw))
        ).scalar_one()
        db.expunge(row)
        return row


def _problem(response: Any) -> str:
    assert response.headers["content-type"] == "application/problem+json", response.text
    return str(response.json()["type"]).removeprefix("urn:perfin:problem:")


def test_breakglass_session_is_refused_off_the_allowlisted_network(
    factories: Factories, oidc_only: FastAPI
) -> None:
    glass = factories.user(role="owner", is_instance_admin=True, is_breakglass=True)
    with browser(oidc_only, ip=INSIDE) as inside:
        raw = _login(inside, glass).cookies[COOKIE]
        assert inside.get(SESSION_URL).status_code == 200
    row = _row(factories, raw)
    assert row.login_method is LoginMethod.BREAKGLASS
    with browser(oidc_only, ip=OUTSIDE) as outside:
        outside.cookies.set(COOKIE, raw)
        session = outside.get(SESSION_URL)
        assert (session.status_code, _problem(session)) == (401, "unauthenticated")
        listed = outside.get(SESSIONS)
        assert (listed.status_code, _problem(listed)) == (401, "unauthenticated")
    # Not touched from outside: the idle expiry did not slide.
    after = _row(factories, raw)
    assert after.last_seen_at == row.last_seen_at
    assert after.idle_expires_at == row.idle_expires_at
    assert after.revoked_at is None
    # Back on the allowlisted network the same session works.
    with browser(oidc_only, ip=INSIDE) as inside:
        inside.cookies.set(COOKIE, raw)
        assert inside.get(SESSION_URL).status_code == 200


def test_pending_breakglass_login_cannot_be_finished_off_network(
    factories: Factories, oidc_only: FastAPI
) -> None:
    glass = factories.user(role="owner", is_instance_admin=True, is_breakglass=True, with_totp=True)
    with browser(oidc_only, ip=INSIDE) as inside:
        first = _login(inside, glass)
        assert first.json()["status"] == "mfa_required"
        raw = first.cookies[COOKIE]
    assert _row(factories, raw).stage is SessionStage.MFA_PENDING
    with browser(oidc_only, ip=OUTSIDE) as outside:
        outside.cookies.set(COOKIE, raw)
        verify = outside.post(
            VERIFY,
            json={"method": "totp", "code": glass.totp_now()},
            headers=session_headers(first.json()["csrf_token"]),
        )
        # CSRF passes (the token is bound to the cookie); the session does not.
        assert (verify.status_code, _problem(verify)) == (401, "unauthenticated")
    assert _row(factories, raw).stage is SessionStage.MFA_PENDING


def test_ordinary_sessions_do_not_depend_on_the_address(
    factories: Factories, settings: Settings
) -> None:
    app = build_app(settings, limits=ROOMY)
    member = factories.user(role="member", with_totp=False)
    no_mfa = build_app(settings.model_copy(update={"require_mfa": False}), limits=ROOMY)
    for target in (app, no_mfa):
        with browser(target, ip=INSIDE) as inside:
            raw = _login(inside, member).cookies[COOKIE]
        with browser(target, ip=OUTSIDE) as outside:
            outside.cookies.set(COOKIE, raw)
            assert outside.get(SESSION_URL).status_code == 200
