# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /api/v1/auth/session`` (the SPA's who-am-I and CSRF source, see
``frontend/src/lib/csrf.ts``) and the session list/revoke endpoints."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.auth.models import SessionStage, UserSession
from app.core import clock
from app.core.csrf import CSRF_HEADER, PRE_AUTH_COOKIE
from app.main import API_PREFIX
from tests.fixtures.factories import AuthedClient, Factories, LoggedIn, UserRecord

pytestmark = pytest.mark.integration

SESSION = f"{API_PREFIX}/auth/session"
SESSIONS = f"{API_PREFIX}/auth/sessions"
SAME_ORIGIN = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}


def test_anonymous_gets_401_with_pre_auth_token(client: TestClient) -> None:
    response = client.get(SESSION)
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["type"] == "urn:perfin:problem:unauthenticated"
    assert body["authenticated"] is False
    token = body["csrf_token"]
    assert isinstance(token, str)
    assert 20 < len(token) <= 256
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{PRE_AUTH_COOKIE}=")
    nonce = response.cookies.get(PRE_AUTH_COOKIE)
    assert nonce
    assert client.app.state.csrf.pre_auth_token(nonce) == token  # type: ignore[attr-defined]


def test_pre_auth_token_passes_csrf_for_login_forms(client: TestClient) -> None:
    response = client.get(SESSION)
    nonce = response.cookies[PRE_AUTH_COOKIE]
    token = response.json()["csrf_token"]
    headers = {**SAME_ORIGIN, CSRF_HEADER: token, "Cookie": f"{PRE_AUTH_COOKIE}={nonce}"}
    # Passes CSRF (403 would be csrf-failed); fails authentication instead.
    attempt = client.delete(SESSIONS, headers=headers)
    assert attempt.status_code == 401
    assert client.delete(SESSIONS, headers={**SAME_ORIGIN}).status_code == 403


def test_authenticated_session_shape(factories: Factories, client: TestClient) -> None:
    record = factories.user(role="admin", with_totp=True, display_name="Synthetic Admin")
    login = factories.login(client, record)
    response = client.get(SESSION)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert record.household is not None
    assert body == {
        "authenticated": True,
        "user": {
            "id": str(record.id),
            "username": record.user.username,
            "display_name": "Synthetic Admin",
            "is_instance_admin": False,
        },
        "household": {
            "id": str(record.household.id),
            "name": record.household.name,
            "role": "admin",
        },
        "csrf_token": login.csrf_token,
        "mfa": {
            "enrolled": True,
            "verified": True,
            "verified_at": body["mfa"]["verified_at"],
            "required": True,
            "enrollment_required": False,
        },
        "auth_method": "local",
    }
    assert body["mfa"]["verified_at"] is not None
    # The session-bound token works for unsafe requests; no pre-auth cookie is issued.
    assert PRE_AUTH_COOKIE not in response.headers.get("set-cookie", "")
    revoke = client.delete(SESSIONS, headers={**SAME_ORIGIN, CSRF_HEADER: body["csrf_token"]})
    assert revoke.status_code == 200


def test_session_token_is_hmac_of_cookie(factories: Factories, client: TestClient) -> None:
    record = factories.user(role="member", with_totp=True)
    login = factories.login(client, record)
    token = client.get(SESSION).json()["csrf_token"]
    assert token == client.app.state.csrf.token_for_session(login.raw_id)  # type: ignore[attr-defined]


def test_user_without_household(factories: Factories, client: TestClient) -> None:
    record = factories.user(role=None, with_totp=True, is_instance_admin=True)
    factories.login(client, record)
    body = client.get(SESSION).json()
    assert body["household"] is None
    assert body["user"]["is_instance_admin"] is True


def test_restricted_user_is_told_to_enrol(factories: Factories, client: TestClient) -> None:
    record = factories.user(role="member", with_totp=False)
    factories.login(client, record)
    response = client.get(SESSION)
    assert response.status_code == 200
    assert response.json()["mfa"] == {
        "enrolled": False,
        "verified": False,
        "verified_at": None,
        "required": True,
        "enrollment_required": True,
    }


def test_mfa_pending_session(factories: Factories, client: TestClient) -> None:
    record = factories.user(role="member", with_totp=True)
    login = factories.login(client, record, stage=SessionStage.MFA_PENDING, mfa_verified=False)
    response = client.get(SESSION)
    assert response.status_code == 401
    body = response.json()
    assert body["type"] == "urn:perfin:problem:mfa-required"
    assert body["authenticated"] is False
    assert body["mfa"] == {"pending": True, "methods": ["totp"]}
    # Bound to the pending session, which is what the CSRF layer expects next.
    assert body["csrf_token"] == login.csrf_token
    assert PRE_AUTH_COOKIE not in response.headers.get("set-cookie", "")


def test_dead_session_cookie_is_cleared(factories: Factories, client: TestClient) -> None:
    record = factories.user(role="member", with_totp=True)
    login = factories.login(client, record)
    with factories.transaction() as db:
        db.execute(
            update(UserSession)
            .where(UserSession.id == login.session_id)
            .values(revoked_at=clock.utcnow())
        )
    response = client.get(SESSION)
    assert response.status_code == 401
    cookies = response.headers.get_list("set-cookie")
    assert any(c.startswith(f"{PRE_AUTH_COOKIE}=") for c in cookies)
    cleared = [c for c in cookies if c.startswith("__Host-perfin-session=")]
    assert len(cleared) == 1
    assert "max-age=0" in cleared[0].lower()


def test_bearer_caller(factories: Factories, client: TestClient) -> None:
    record = factories.user(role="viewer", with_totp=True)
    raw = factories.token(record)
    response = client.get(SESSION, headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 200
    body = response.json()
    assert body["auth_method"] == "token"
    assert body["csrf_token"] is None
    assert body["household"]["role"] == "viewer"


def test_bad_bearer_with_cookie_is_401(factories: Factories, client: TestClient) -> None:
    record = factories.user(role="member", with_totp=True)
    factories.login(client, record)
    response = client.get(SESSION, headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401
    assert response.json()["type"] == "urn:perfin:problem:invalid-token"


# ---- session list / revoke -----------------------------------------------------------


def test_list_and_revoke_sessions(factories: Factories, client: TestClient) -> None:
    record = factories.user(role="member", with_totp=True)
    other_device = factories.session(record, mfa_verified=True)
    login = factories.login(client, record)
    listed = client.get(SESSIONS).json()
    assert {s["id"] for s in listed} == {str(login.session_id), str(other_device.row.id)}
    current = [s for s in listed if s["current"]]
    assert [s["id"] for s in current] == [str(login.session_id)]
    assert all("token_hash" not in s for s in listed)

    response = client.delete(f"{SESSIONS}/{other_device.row.id}", headers=login.headers())
    assert response.status_code == 204
    assert [s["id"] for s in client.get(SESSIONS).json()] == [str(login.session_id)]
    again = client.delete(f"{SESSIONS}/{other_device.row.id}", headers=login.headers())
    assert again.status_code == 404


def test_cannot_revoke_someone_elses_session(factories: Factories, client: TestClient) -> None:
    victim = factories.user(role="member", with_totp=True)
    victim_session = factories.session(victim, mfa_verified=True)
    attacker = factories.user(role="member", with_totp=True)
    login = factories.login(client, attacker)
    response = client.delete(f"{SESSIONS}/{victim_session.row.id}", headers=login.headers())
    assert response.status_code == 404
    with factories.transaction() as db:
        row = db.get(UserSession, victim_session.row.id)
        assert row is not None
        assert row.revoked_at is None


def test_revoking_current_session_clears_cookie(factories: Factories, client: TestClient) -> None:
    record = factories.user(role="member", with_totp=True)
    login = factories.login(client, record)
    response = client.delete(f"{SESSIONS}/{login.session_id}", headers=login.headers())
    assert response.status_code == 204
    assert "max-age=0" in response.headers["set-cookie"].lower()
    assert client.get(SESSION).status_code == 401


def test_revoke_all_others(factories: Factories, client: TestClient) -> None:
    record = factories.user(role="member", with_totp=True)
    for _ in range(2):
        factories.session(record, mfa_verified=True)
    login = factories.login(client, record)
    response = client.delete(SESSIONS, headers=login.headers())
    assert response.json() == {"revoked": 2}
    assert [s["id"] for s in client.get(SESSIONS).json()] == [str(login.session_id)]


def test_session_management_refuses_tokens(factories: Factories, client: TestClient) -> None:
    record = factories.user(role="member", with_totp=True)
    raw = factories.token(record)
    response = client.get(SESSIONS, headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 403
    assert response.json()["type"] == "urn:perfin:problem:session-required"


def test_revocation_is_audited(factories: Factories, client: TestClient) -> None:
    from sqlalchemy import select

    from app.db.models import AuditLog
    from app.db.rls import set_context

    record = factories.user(role="member", with_totp=True)
    other = factories.session(record, mfa_verified=True)
    login = factories.login(client, record)
    client.delete(f"{SESSIONS}/{other.row.id}", headers=login.headers())
    with factories.transaction() as db:
        set_context(db, record.household_id)
        actions = (
            db.execute(select(AuditLog.action).where(AuditLog.user_id == record.id)).scalars().all()
        )
    assert "session.revoke" in actions


# ---- the documented fixtures ---------------------------------------------------------


def test_documented_fixtures(
    client: TestClient,
    make_household: Callable[..., Any],
    make_user: Callable[..., UserRecord],
    login_as: Callable[..., LoggedIn],
    bearer_token: Callable[..., str],
    authed_client: Callable[..., AuthedClient],
) -> None:
    household = make_household(name="Fixture Check Household")
    owner = make_user(role="owner", household=household, with_totp=True)
    viewer = make_user(role="viewer", household=household)
    assert owner.household_id == viewer.household_id == household.id
    assert owner.totp_now().isdigit()

    login = login_as(client, owner)
    body = client.get(SESSION).json()
    assert body["household"] == {
        "id": str(household.id),
        "name": "Fixture Check Household",
        "role": "owner",
    }
    assert body["csrf_token"] == login.csrf_token

    raw = bearer_token(viewer, scopes=["transactions:read"])
    as_viewer = client.get(SESSION, headers={"Authorization": f"Bearer {raw}"}).json()
    assert as_viewer["user"]["id"] == str(viewer.id)

    admin = authed_client(role="admin")
    assert admin.get(SESSION).json()["household"]["role"] == "admin"
    assert admin.delete(SESSIONS).status_code == 200  # CSRF headers added automatically
