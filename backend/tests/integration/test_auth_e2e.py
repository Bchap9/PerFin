# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 1 end to end through the real app, the way the SPA and a script use it.

Local: anonymous ``GET /auth/session`` (pre-auth CSRF token) -> ``POST
/auth/login`` -> ``mfa-required`` -> ``POST /auth/mfa/verify`` (session id
rotates) -> ``GET /auth/session`` -> stale MFA refuses token creation ->
``POST /auth/step-up`` -> ``POST /auth/tokens`` -> a bearer call from a
cookieless client -> revoke -> logout.

OIDC (fake IdP): sign in with IdP-asserted MFA -> ``GET /auth/session`` ->
token creation (the fresh IdP MFA counts as step-up) -> bearer call ->
back-channel logout ends the browser session.

Every request goes through the full middleware stack (CSRF, problem details,
headers) and the per-request RLS context; nothing is created behind the
API's back except the starting user.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.local import AuthLimits
from app.auth.ratelimit import Limit, LockoutPolicy
from app.core.csrf import CSRF_HEADER
from app.core.errors import PROBLEM_MEDIA_TYPE
from app.core.settings import Settings
from app.main import API_PREFIX
from tests.fixtures.auth_clients import (
    SESSION_URL,
    browser,
    build_app,
    pre_auth_headers,
    session_headers,
)
from tests.fixtures.factories import Factories
from tests.fixtures.fake_idp import (
    BACKCHANNEL,
    CLIENT_ID,
    GROUP_MEMBER,
    ISSUER,
    FakeIdP,
    clear_proxy_env,
    login_via_idp,
    mocked_idp,
    oidc_error,
    oidc_settings,
    resolver,
)
from tests.fixtures.mfa import FrozenClock, freezer, totp_code

pytestmark = pytest.mark.integration

LOGIN = f"{API_PREFIX}/auth/login"
LOGOUT = f"{API_PREFIX}/auth/logout"
VERIFY = f"{API_PREFIX}/auth/mfa/verify"
STEP_UP = f"{API_PREFIX}/auth/step-up"
TOKENS = f"{API_PREFIX}/auth/tokens"
SESSION_COOKIE = "__Host-perfin-session"

ROOMY = AuthLimits(
    login_per_ip=Limit("login:ip", 1000, timedelta(minutes=5)),
    login_per_account=Limit("login:account", 1000, timedelta(minutes=15)),
    lockout=LockoutPolicy(delay_after=1000, lock_after=1001),
)


def problem(response: Any) -> str:
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE, response.text
    return str(response.json()["type"]).removeprefix("urn:perfin:problem:")


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def freeze(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FrozenClock]:
    return freezer(monkeypatch)


def test_local_login_mfa_step_up_token_and_bearer_call(
    settings: Settings, factories: Factories, freeze: Callable[..., FrozenClock]
) -> None:
    clk = freeze()
    record = factories.user(role="owner", with_totp=True)
    assert record.totp_secret is not None
    household = str(record.household_id)
    app = build_app(settings, limits=ROOMY)
    with browser(app) as web:
        # 1. Anonymous: a 401 problem carrying the pre-auth CSRF token.
        anonymous = web.get(SESSION_URL)
        assert anonymous.status_code == 401
        assert problem(anonymous) == "unauthenticated"
        # Login without that token is refused by CSRF, with it accepted.
        refused = web.post(LOGIN, json={"username": record.user.username, "password": "x" * 12})
        assert (refused.status_code, problem(refused)) == (403, "csrf-failed")
        login = web.post(
            LOGIN,
            json={"username": record.user.username, "password": record.password},
            headers=pre_auth_headers(web),
        )
        assert login.status_code == 200, login.text
        assert login.json()["status"] == "mfa_required"
        pending_cookie = web.cookies[SESSION_COOKIE]

        # 2. Password accepted, second factor pending: nothing else works yet.
        pending = web.get(SESSION_URL)
        assert (pending.status_code, problem(pending)) == (401, "mfa-required")
        assert web.get(f"{API_PREFIX}/households").status_code == 401

        # 3. TOTP completes the login; the session id rotates (A3).
        verify = web.post(
            VERIFY,
            json={"method": "totp", "code": totp_code(record.totp_secret, clk.now)},
            headers=session_headers(login.json()["csrf_token"]),
        )
        assert verify.status_code == 200, verify.text
        assert web.cookies[SESSION_COOKIE] != pending_cookie
        me = web.get(SESSION_URL)
        assert me.status_code == 200, me.text
        body = me.json()
        assert body["auth_method"] == "local"
        assert body["mfa"]["verified"] is True
        assert body["household"]["id"] == household
        assert body["household"]["role"] == "owner"
        csrf = body["csrf_token"]
        assert verify.json()["csrf_token"] == csrf
        assert web.get(f"{API_PREFIX}/households/{household}").status_code == 200

        # 4. Eleven minutes later the MFA is too old to mint a token (A6).
        clk.advance(minutes=11)
        create = {"name": "Synthetic script", "scopes": ["households:read"], "expires_in_days": 7}
        stale = web.post(TOKENS, json=create, headers=session_headers(csrf))
        assert (stale.status_code, problem(stale)) == (403, "step-up-required")

        # 5. Step-up with a new code; it rotates the session and its token.
        before = web.cookies[SESSION_COOKIE]
        up = web.post(
            STEP_UP,
            json={"method": "totp", "code": totp_code(record.totp_secret, clk.now)},
            headers=session_headers(csrf),
        )
        assert up.status_code == 200, up.text
        assert web.cookies[SESSION_COOKIE] != before
        old_csrf, csrf = csrf, up.json()["csrf_token"]
        assert csrf != old_csrf
        # The old session's CSRF token no longer works.
        assert problem(web.post(TOKENS, json=create, headers=session_headers(old_csrf))) == (
            "csrf-failed"
        )

        # 6. Token creation, shown once.
        created = web.post(TOKENS, json=create, headers=session_headers(csrf))
        assert created.status_code == 201, created.text
        token = created.json()["token"]
        token_id = created.json()["id"]
        assert token.startswith("pfn_")
        listed = web.get(TOKENS).json()
        assert [t["id"] for t in listed] == [token_id]
        assert "token" not in listed[0]

        # 7. A script: no cookies, no CSRF token, just the bearer token.
        with TestClient(app, base_url="https://testserver") as script:
            read = script.get(f"{API_PREFIX}/households/{household}", headers=bearer(token))
            assert read.status_code == 200, read.text
            assert read.json()["id"] == household
            whoami = script.get(SESSION_URL, headers=bearer(token))
            assert whoami.status_code == 200
            assert whoami.json()["auth_method"] == "token"
            # Bearer requests skip the CSRF token but not the scope check.
            rename = script.patch(
                f"{API_PREFIX}/households/{household}",
                json={"name": "Renamed"},
                headers=bearer(token),
            )
            assert (rename.status_code, problem(rename)) == (403, "insufficient-scope")
            # Token management needs a browser session.
            assert problem(script.get(TOKENS, headers=bearer(token))) == "session-required"
            # A malformed bearer is refused; it never falls back to anything.
            assert script.get(SESSION_URL, headers=bearer("pfn_bogus")).status_code == 401

            # 8. Revoke in the browser: the script's next call fails.
            revoked = web.delete(f"{TOKENS}/{token_id}", headers=session_headers(csrf))
            assert revoked.status_code == 204
            gone = script.get(f"{API_PREFIX}/households/{household}", headers=bearer(token))
            assert (gone.status_code, problem(gone)) == (401, "invalid-token")

        # 9. Logout ends the browser session.
        out = web.post(LOGOUT, headers=session_headers(csrf))
        assert out.status_code == 204
        after = web.get(SESSION_URL)
        assert (after.status_code, problem(after)) == (401, "unauthenticated")


@pytest.fixture
def idp(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeIdP]:
    clear_proxy_env(monkeypatch)
    with mocked_idp() as mock:
        yield mock.idp


def oidc_claims(sid: str) -> dict[str, Any]:
    """A member of the PerFin group whose IdP login used MFA."""
    sub = f"sub-{uuid.uuid4().hex[:12]}"
    return {
        "sub": sub,
        "iss": ISSUER,
        "aud": CLIENT_ID,
        "sid": sid,
        "preferred_username": f"oidc-{sub[-8:]}",
        "name": "Synthetic Person",
        "email": f"{sub}@example.test",
        "email_verified": True,
        "groups": [GROUP_MEMBER],
        "amr": ["pwd", "mfa"],
        # The IdP authenticated the user (with MFA) just now.
        "auth_time": int(time.time()),
    }


def test_oidc_login_token_and_backchannel_logout(
    settings: Settings, factories: Factories, idp: FakeIdP
) -> None:
    household = factories.household()
    configured = oidc_settings(settings, oidc_household_id=household.id, oidc_trust_idp_mfa=True)
    app = build_app(configured, limits=ROOMY, oidc_resolver=resolver())
    sid = f"sid-{uuid.uuid4().hex[:8]}"
    claims = oidc_claims(sid)
    with browser(app) as web:
        # A forced IdP sign-in (``reauth=true``): only such a login can earn
        # a fresh step-up from the IdP's MFA (P1-SR3-01).
        callback = login_via_idp(web, idp, claims, return_to="/settings", reauth=True)
        assert callback.status_code == 303
        assert oidc_error(callback) is None
        assert callback.headers["location"] == "/settings"
        me = web.get(SESSION_URL)
        assert me.status_code == 200, me.text
        body = me.json()
        assert body["auth_method"] == "oidc"
        assert body["household"]["id"] == str(household.id)
        assert body["household"]["role"] == "member"
        assert body["mfa"]["verified"] is True
        csrf = body["csrf_token"]
        # The IdP's MFA (auth_time) was during this forced sign-in: a fresh step-up.
        created = web.post(
            TOKENS,
            json={"name": "OIDC script", "scopes": ["households:read"], "expires_in_days": 1},
            headers=session_headers(csrf),
        )
        assert created.status_code == 201, created.text
        token = created.json()["token"]
        with TestClient(app, base_url="https://testserver") as script:
            read = script.get(f"{API_PREFIX}/households/{household.id}", headers=bearer(token))
            assert read.status_code == 200, read.text

        # Back-channel logout: no cookies, no CSRF token, just the signed token.
        with TestClient(app, base_url="https://testserver") as idp_server:
            forged = idp_server.post(BACKCHANNEL, data={"logout_token": "not-a-jwt"})
            assert (forged.status_code, problem(forged)) == (400, "invalid-logout-token")
            assert forged.json()["error"] == "invalid_request"
            ok = idp_server.post(BACKCHANNEL, data={"logout_token": idp.logout_token(sid=sid)})
            assert ok.status_code == 200, ok.text
        ended = web.get(SESSION_URL)
        assert (ended.status_code, problem(ended)) == (401, "unauthenticated")
        # A state-changing request with the dead session's token is refused.
        assert web.post(TOKENS, headers={CSRF_HEADER: csrf}).status_code == 403


def test_idp_mfa_alone_cannot_mint_a_token_while_mfa_is_required(
    settings: Settings, factories: Factories, idp: FakeIdP
) -> None:
    """Bearer requests need a PerFin factor under ``PERFIN_REQUIRE_MFA``, so
    token creation refuses up front instead of minting a dead token."""
    household = factories.household()
    configured = oidc_settings(
        settings, oidc_household_id=household.id, require_mfa=True, oidc_trust_idp_mfa=True
    )
    app = build_app(configured, limits=ROOMY, oidc_resolver=resolver())
    with browser(app) as web:
        # A forced IdP sign-in, so the session has a fresh step-up and the
        # refusal below is about the missing PerFin factor alone.
        login_via_idp(web, idp, oidc_claims(f"sid-{uuid.uuid4().hex[:8]}"), reauth=True)
        body = web.get(SESSION_URL).json()
        assert body["mfa"]["verified"] is True  # the session itself is fine
        assert web.get(f"{API_PREFIX}/households/{household.id}").status_code == 200
        refused = web.post(
            TOKENS,
            json={"name": "Dead token", "scopes": ["households:read"], "expires_in_days": 1},
            headers=session_headers(body["csrf_token"]),
        )
        assert (refused.status_code, problem(refused)) == (403, "mfa-enrollment-required")
        assert web.get(TOKENS).json() == []
