# SPDX-License-Identifier: AGPL-3.0-or-later
"""Local login, logout and password change (``app.auth.local``, ``auth_local`` router).

SECURITY.md A1 (rate limits, progressive lockout, breached list), A2 (uniform
answers and timing), A3 (fresh session id), B7 (break-glass CIDR).
"""

from __future__ import annotations

import json
import statistics
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text, update

from app.auth import local
from app.auth.local import AuthLimits, LockNotice, set_password
from app.auth.models import Credential, LoginMethod, SessionStage, User, UserSession
from app.auth.passwords import DEFAULT_PARAMS, dummy_verify
from app.auth.ratelimit import Limit, LockoutPolicy
from app.auth.sessions import hash_session_id
from app.core import clock
from app.core.settings import Settings
from app.main import API_PREFIX
from tests.fixtures.auth_clients import (
    SESSION_URL,
    browser,
    build_app,
    pre_auth_headers,
    random_ip,
    session_headers,
)
from tests.fixtures.factories import Factories, UserRecord

pytestmark = pytest.mark.integration

LOGIN = f"{API_PREFIX}/auth/login"
LOGOUT = f"{API_PREFIX}/auth/logout"
PASSWORD = f"{API_PREFIX}/auth/password"
OPTIONS = f"{API_PREFIX}/auth/login/options"
SESSIONS = f"{API_PREFIX}/auth/sessions"

#: Generous limits so a test only meets the limit it is about.
ROOMY = AuthLimits(
    login_per_ip=Limit("login:ip", 1000, timedelta(minutes=5)),
    login_per_account=Limit("login:account", 1000, timedelta(minutes=15)),
    lockout=LockoutPolicy(delay_after=1000, lock_after=1001),
)


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return build_app(settings, limits=ROOMY)


@pytest.fixture
def web(app: FastAPI) -> Iterator[TestClient]:
    with browser(app) as client:
        yield client


def login(client: TestClient, username: str, password: str, **extra: Any) -> Any:  # httpx.Response
    headers = pre_auth_headers(client)
    return client.post(
        LOGIN, json={"username": username, "password": password, **extra}, headers=headers
    )


def audit_rows(admin_engine: Engine, user_id: uuid.UUID | None, action: str) -> list[Any]:
    with admin_engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT action, user_id, after, ip FROM audit_log "
                    "WHERE action = :a AND (user_id = :u OR (:u IS NULL AND user_id IS NULL)) "
                    "ORDER BY seq"
                ),
                {"a": action, "u": user_id},
            )
        )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


def test_login_without_mfa_creates_a_full_session(
    factories: Factories, web: TestClient, admin_engine: Engine
) -> None:
    record = factories.user(role="member")
    response = login(web, record.user.username, record.password)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "authenticated"
    assert body["mfa"] == {"required": False, "methods": []}
    cookie = response.cookies.get("__Host-perfin-session")
    assert cookie
    set_cookie = response.headers.get_list("set-cookie")
    assert any(c.startswith("__Host-perfin-session=") and "HttpOnly" in c for c in set_cookie)
    # The pre-auth cookie is cleared once a session exists.
    assert any(c.startswith("__Host-perfin-pre=") and "Max-Age=0" in c for c in set_cookie)
    session = web.get(SESSION_URL)
    assert session.status_code == 200
    assert session.json()["auth_method"] == "local"
    assert session.json()["csrf_token"] == body["csrf_token"]
    rows = audit_rows(admin_engine, record.id, "user.login")
    assert rows
    assert rows[-1].after["method"] == "local"
    assert rows[-1].ip is not None


def test_login_with_totp_returns_a_pending_session(factories: Factories, web: TestClient) -> None:
    record = factories.user(role="member", with_totp=True)
    response = login(web, record.user.username.upper(), record.password)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "mfa_required"
    assert body["mfa"] == {"required": True, "methods": ["totp"]}
    raw = response.cookies["__Host-perfin-session"]
    with factories.transaction() as db:
        row = db.execute(
            select(UserSession).where(UserSession.token_hash == hash_session_id(raw))
        ).scalar_one()
        assert row.stage is SessionStage.MFA_PENDING
        assert row.absolute_expires_at - row.created_at <= timedelta(minutes=10)
    # GET /auth/session reports the pending login with the same CSRF token.
    pending = web.get(SESSION_URL)
    assert pending.status_code == 401
    assert pending.json()["type"] == "urn:perfin:problem:mfa-required"
    assert pending.json()["csrf_token"] == body["csrf_token"]


def test_remember_device_sets_a_persistent_cookie(factories: Factories, web: TestClient) -> None:
    record = factories.user(role="member")
    response = login(web, record.user.username, record.password, remember_device=True)
    assert response.status_code == 200
    cookie = next(
        c for c in response.headers.get_list("set-cookie") if c.startswith("__Host-perfin-session")
    )
    assert "Max-Age=" in cookie


def test_login_rotates_away_a_presented_session(factories: Factories, web: TestClient) -> None:
    record = factories.user(role="member")
    first = login(web, record.user.username, record.password)
    old_raw = first.cookies["__Host-perfin-session"]
    headers = session_headers(first.json()["csrf_token"])
    second = web.post(
        LOGIN,
        json={"username": record.user.username, "password": record.password},
        headers=headers,
    )
    assert second.status_code == 200
    new_raw = second.cookies["__Host-perfin-session"]
    assert new_raw != old_raw
    with factories.transaction() as db:
        old = db.execute(
            select(UserSession).where(UserSession.token_hash == hash_session_id(old_raw))
        ).scalar_one()
        assert old.revoked_at is not None
        assert old.revoked_reason == "replaced_by_login"


def test_rehash_on_parameter_change(factories: Factories, web: TestClient) -> None:
    record = factories.user(role="member")  # hashed with cheap test parameters
    with factories.transaction() as db:
        before = db.execute(select(Credential).where(Credential.user_id == record.id)).scalar_one()
        assert before.hash_params["memory_kib"] != DEFAULT_PARAMS.memory_kib
        old_hash = before.password_hash
    assert login(web, record.user.username, record.password).status_code == 200
    with factories.transaction() as db:
        after = db.execute(select(Credential).where(Credential.user_id == record.id)).scalar_one()
    assert after.password_hash != old_hash
    assert after.hash_params == DEFAULT_PARAMS.as_dict()
    assert f"m={DEFAULT_PARAMS.memory_kib},t={DEFAULT_PARAMS.time_cost}" in after.password_hash
    # The upgraded hash still verifies.
    web.cookies.clear()
    assert login(web, record.user.username, record.password).status_code == 200


def test_login_options(settings: Settings) -> None:
    app = build_app(settings, limits=ROOMY)
    with browser(app) as client:
        assert client.get(OPTIONS).json() == {"local_login": True, "password_reset": False}
    oidc_only = build_app(settings.model_copy(update={"local_login_enabled": False}))
    with browser(oidc_only) as client:
        assert client.get(OPTIONS).json()["local_login"] is False
    with browser(oidc_only, ip="127.0.0.1") as client:
        assert client.get(OPTIONS).json()["local_login"] is True


# ---------------------------------------------------------------------------
# Enumeration (A2)
# ---------------------------------------------------------------------------


def _fingerprint(response: Any) -> tuple[int, str, frozenset[str]]:
    headers = frozenset(k.lower() for k in response.headers)
    return response.status_code, json.dumps(response.json(), sort_keys=True), headers


def test_unknown_wrong_and_locked_look_identical(
    factories: Factories, settings: Settings, admin_engine: Engine
) -> None:
    app = build_app(settings, limits=ROOMY)
    known = factories.user(role="member")
    locked = factories.user(role="member")
    with factories.transaction() as db:
        db.execute(
            update(User)
            .where(User.id == locked.id)
            .values(locked_until=clock.utcnow() + timedelta(minutes=15))
        )
    with browser(app) as client:
        unknown = login(client, f"nobody-{uuid.uuid4().hex[:8]}", "Wrong-password-1234")
        wrong = login(client, known.user.username, "Wrong-password-1234")
        # The right password while locked is refused the same way.
        locked_response = login(client, locked.user.username, locked.password)
        inactive_user = factories.user(role="member", is_active=False)
        inactive = login(client, inactive_user.user.username, inactive_user.password)
    prints = {_fingerprint(r) for r in (unknown, wrong, locked_response, inactive)}
    assert len(prints) == 1, prints
    status_code, body, headers = prints.pop()
    assert status_code == 401
    assert json.loads(body) == {
        "type": "urn:perfin:problem:invalid-credentials",
        "title": "Login failed",
        "status": 401,
        "detail": "The username or password is incorrect.",
    }
    assert "set-cookie" not in headers
    assert "retry-after" not in headers
    # Only the audit trail knows the reason (and never the password).
    reasons = {
        row.after["reason"] for row in audit_rows(admin_engine, known.id, "user.login_failed")
    }
    assert reasons == {"bad_password"}
    reasons = {
        row.after["reason"] for row in audit_rows(admin_engine, locked.id, "user.login_failed")
    }
    assert reasons == {"locked"}


def _interleaved_medians(fns: dict[str, Callable[[], Any]], rounds: int) -> dict[str, float]:
    """Median wall time per function, sampled round-robin.

    Interleaving means a burst of load on the machine (other test processes)
    slows every path alike instead of whichever was being measured then.
    """
    samples: dict[str, list[float]] = {name: [] for name in fns}
    for _ in range(rounds):
        for name, fn in fns.items():
            start = time.perf_counter()
            fn()
            samples[name].append(time.perf_counter() - start)
    return {name: statistics.median(values) for name, values in samples.items()}


def test_timing_is_uniform_thanks_to_the_dummy_hash(
    factories: Factories, settings: Settings
) -> None:
    app = build_app(settings, limits=ROOMY)
    record = factories.user(role="member")
    locked = factories.user(role="member")
    with factories.transaction() as db:
        # Production-cost hashes, as real accounts have.
        set_password(db, record.id, record.password)
        set_password(db, locked.id, locked.password)
        db.execute(
            update(User)
            .where(User.id == locked.id)
            .values(locked_until=clock.utcnow() + timedelta(hours=1))
        )
    with browser(app) as client:
        headers = pre_auth_headers(client)

        def attempt(username: str, password: str) -> Callable[[], Any]:
            def run() -> None:
                response = client.post(
                    LOGIN, json={"username": username, "password": password}, headers=headers
                )
                assert response.status_code == 401

            return run

        unknown = attempt(f"ghost-{uuid.uuid4().hex[:8]}", "Wrong-password-1234")
        wrong = attempt(record.user.username, "Wrong-password-1234")
        is_locked = attempt(locked.user.username, locked.password)
        for fn in (unknown, wrong, is_locked):  # warm up connections and the dummy hash
            fn()
        medians = _interleaved_medians(
            {
                "unknown": unknown,
                "wrong": wrong,
                "locked": is_locked,
                "one_hash": lambda: dummy_verify("x" * 16),
            },
            rounds=9,
        )
    one_hash = medians.pop("one_hash")
    # Every path pays at least (most of) one full Argon2id verification ...
    for elapsed in medians.values():
        assert elapsed >= 0.6 * one_hash, (medians, one_hash)
    # ... and the three are within a margin that no extra or missing hash fits in.
    fastest, slowest = min(medians.values()), max(medians.values())
    assert slowest - fastest <= 0.5 * one_hash + 0.02, (medians, one_hash)


# ---------------------------------------------------------------------------
# Rate limits and lockout (A1)
# ---------------------------------------------------------------------------


def test_per_ip_rate_limit(factories: Factories, settings: Settings) -> None:
    limits = AuthLimits(
        login_per_ip=Limit("login:ip", 3, timedelta(minutes=5)),
        login_per_account=Limit("login:account", 1000, timedelta(minutes=15)),
        lockout=LockoutPolicy(delay_after=1000, lock_after=1001),
    )
    app = build_app(settings, limits=limits)
    record = factories.user(role="member")
    ip = random_ip()
    with browser(app, ip=ip) as client:
        for _ in range(3):
            assert login(client, f"x-{uuid.uuid4().hex[:6]}", "Wrong-password-1").status_code == 401
        blocked = login(client, record.user.username, record.password)
        assert blocked.status_code == 429
        assert blocked.json()["type"] == "urn:perfin:problem:rate-limited"
        assert int(blocked.headers["retry-after"]) >= 1
    # Another address is not affected.
    with browser(app) as other:
        assert login(other, record.user.username, record.password).status_code == 200


def test_per_account_rate_limit_is_the_same_for_unknown_accounts(
    factories: Factories, settings: Settings
) -> None:
    limits = AuthLimits(
        login_per_ip=Limit("login:ip", 1000, timedelta(minutes=5)),
        login_per_account=Limit("login:account", 2, timedelta(minutes=15)),
        lockout=LockoutPolicy(delay_after=1000, lock_after=1001),
    )
    app = build_app(settings, limits=limits)
    record = factories.user(role="member")
    ghost = f"ghost-{uuid.uuid4().hex[:8]}"
    outcomes: dict[str, list[int]] = {}
    for name in (record.user.username, ghost):
        codes = []
        for _ in range(3):
            # A new address each time: only the per-account limit applies.
            with browser(app) as client:
                codes.append(login(client, name, "Wrong-password-1234").status_code)
        outcomes[name] = codes
    assert outcomes[record.user.username] == [401, 401, 429]
    assert outcomes[ghost] == [401, 401, 429]


def test_progressive_lockout_then_lock_with_notification(
    factories: Factories,
    settings: Settings,
    admin_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = AuthLimits(
        login_per_ip=Limit("login:ip", 1000, timedelta(minutes=5)),
        login_per_account=Limit("login:account", 1000, timedelta(minutes=15)),
        lockout=LockoutPolicy(delay_after=2, base_delay=timedelta(seconds=30), lock_after=4),
    )
    app = build_app(settings, limits=limits)
    record = factories.user(role="member")
    notices: list[LockNotice] = []
    monkeypatch.setattr(local, "LOCK_NOTIFIERS", [notices.append])
    with browser(app) as client:
        assert login(client, record.user.username, "Wrong-password-0001").status_code == 401
        # One failure: no delay yet, the right password works and resets the count.
        assert login(client, record.user.username, record.password).status_code == 200
        client.cookies.clear()
        for _ in range(2):
            assert login(client, record.user.username, "Wrong-password-0002").status_code == 401
        with factories.transaction() as db:
            user = db.get(User, record.id)
            assert user is not None
            assert user.failed_login_count == 2
            assert user.locked_until is not None  # progressive delay began
        # During the delay even the right password gets the uniform 401.
        assert login(client, record.user.username, record.password).status_code == 401
        # Push past the delay and fail twice more: the long lock starts.
        for _ in range(2):
            with factories.transaction() as db:
                db.execute(update(User).where(User.id == record.id).values(locked_until=None))
            assert login(client, record.user.username, "Wrong-password-0003").status_code == 401
        assert login(client, record.user.username, record.password).status_code == 401
    with factories.transaction() as db:
        user = db.get(User, record.id)
        assert user is not None
        assert user.failed_login_count == 4
        assert user.locked_until is not None
        assert user.locked_until - clock.utcnow() > timedelta(minutes=14)
    assert [n.user_id for n in notices] == [record.id]
    assert audit_rows(admin_engine, record.id, "user.locked")
    with admin_engine.connect() as conn:
        events = conn.execute(
            text(
                "SELECT household_id FROM event WHERE type = 'user.locked' "
                "AND payload->>'user_id' = :u"
            ),
            {"u": str(record.id)},
        ).all()
    assert [row.household_id for row in events] == [record.household_id]


def test_failed_attempts_survive_the_rolled_back_request(
    factories: Factories, settings: Settings
) -> None:
    limits = AuthLimits(
        login_per_ip=Limit("login:ip", 1000, timedelta(minutes=5)),
        login_per_account=Limit("login:account", 1000, timedelta(minutes=15)),
        lockout=LockoutPolicy(delay_after=1000, lock_after=1001),
    )
    app = build_app(settings, limits=limits)
    record = factories.user(role="member")
    with browser(app) as client:
        for _ in range(3):
            login(client, record.user.username, "Wrong-password-0001")
    with factories.transaction() as db:
        user = db.get(User, record.id)
        assert user is not None
        assert user.failed_login_count == 3


# ---------------------------------------------------------------------------
# OIDC-only mode and break-glass (B7)
# ---------------------------------------------------------------------------


@pytest.fixture
def oidc_only(settings: Settings) -> FastAPI:
    return build_app(settings.model_copy(update={"local_login_enabled": False}), limits=ROOMY)


def test_oidc_only_refuses_local_login_outside_breakglass_cidr(
    factories: Factories, oidc_only: FastAPI
) -> None:
    normal = factories.user(role="member")
    glass = factories.user(role=None, is_instance_admin=True, is_breakglass=True)
    with browser(oidc_only) as client:
        for record in (normal, glass):
            response = login(client, record.user.username, record.password)
            assert response.status_code == 403
            assert response.json()["type"] == "urn:perfin:problem:local-login-disabled"
        ghost = login(client, "nobody-at-all", "Wrong-password-1234")
        assert ghost.status_code == 403  # the same for every username


def test_oidc_only_allows_only_breakglass_users_from_the_cidr(
    factories: Factories, oidc_only: FastAPI
) -> None:
    normal = factories.user(role="member")
    glass = factories.user(role=None, is_instance_admin=True, is_breakglass=True)
    with browser(oidc_only, ip="127.0.0.1") as client:
        refused = login(client, normal.user.username, normal.password)
        assert refused.status_code == 401
        assert refused.json()["type"] == "urn:perfin:problem:invalid-credentials"
        allowed = login(client, glass.user.username, glass.password)
        assert allowed.status_code == 200, allowed.text
        raw = allowed.cookies["__Host-perfin-session"]
    with factories.transaction() as db:
        row = db.execute(
            select(UserSession).where(UserSession.token_hash == hash_session_id(raw))
        ).scalar_one()
        assert row.login_method is LoginMethod.BREAKGLASS


def test_breakglass_account_is_refused_outside_its_cidr(
    factories: Factories, settings: Settings, admin_engine: Engine
) -> None:
    app = build_app(settings, limits=ROOMY)  # local login enabled
    glass = factories.user(role=None, is_instance_admin=True, is_breakglass=True)
    with browser(app) as client:  # 10.x, outside 127.0.0.1/32
        response = login(client, glass.user.username, glass.password)
        assert response.status_code == 401
        assert response.json()["type"] == "urn:perfin:problem:invalid-credentials"
    reasons = {
        row.after["reason"] for row in audit_rows(admin_engine, glass.id, "user.login_failed")
    }
    assert reasons == {"breakglass_outside_cidr"}
    with browser(app, ip="127.0.0.1") as client:
        assert login(client, glass.user.username, glass.password).status_code == 200


def test_custom_breakglass_cidr(factories: Factories, settings: Settings) -> None:
    from ipaddress import ip_network

    custom = settings.model_copy(
        update={"local_login_enabled": False, "breakglass_cidrs": [ip_network("192.0.2.0/24")]}
    )
    app = build_app(custom, limits=ROOMY)
    glass = factories.user(role=None, is_instance_admin=True, is_breakglass=True)
    with browser(app, ip="192.0.2.77") as client:
        assert login(client, glass.user.username, glass.password).status_code == 200
    with browser(app, ip="127.0.0.1") as client:
        assert login(client, glass.user.username, glass.password).status_code == 403


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_logout_revokes_the_session_and_clears_the_cookie(
    factories: Factories, web: TestClient, admin_engine: Engine
) -> None:
    record = factories.user(role="member")
    response = login(web, record.user.username, record.password)
    raw = response.cookies["__Host-perfin-session"]
    out = web.post(LOGOUT, headers=session_headers(response.json()["csrf_token"]))
    assert out.status_code == 204
    assert any(
        c.startswith("__Host-perfin-session=") and "Max-Age=0" in c
        for c in out.headers.get_list("set-cookie")
    )
    with factories.transaction() as db:
        row = db.execute(
            select(UserSession).where(UserSession.token_hash == hash_session_id(raw))
        ).scalar_one()
        assert row.revoked_reason == "logout"
    # The old cookie is dead even if replayed.
    web.cookies.set("__Host-perfin-session", raw, domain="testserver")
    assert web.get(SESSION_URL).status_code == 401
    assert audit_rows(admin_engine, record.id, "user.logout")


def test_logout_of_a_pending_login(factories: Factories, web: TestClient) -> None:
    record = factories.user(role="member", with_totp=True)
    response = login(web, record.user.username, record.password)
    assert response.json()["status"] == "mfa_required"
    out = web.post(LOGOUT, headers=session_headers(response.json()["csrf_token"]))
    assert out.status_code == 204
    assert web.get(SESSION_URL).json()["type"] == "urn:perfin:problem:unauthenticated"


def test_logout_without_session_is_harmless(web: TestClient) -> None:
    assert web.post(LOGOUT, headers=pre_auth_headers(web)).status_code == 204


def test_logout_needs_csrf(factories: Factories, web: TestClient) -> None:
    record = factories.user(role="member")
    login(web, record.user.username, record.password)
    assert web.post(LOGOUT).status_code == 403


# ---------------------------------------------------------------------------
# Password change
# ---------------------------------------------------------------------------


def _logged_in(web: TestClient, record: UserRecord) -> dict[str, str]:
    response = login(web, record.user.username, record.password)
    assert response.status_code == 200, response.text
    return session_headers(response.json()["csrf_token"])


@pytest.fixture
def no_mfa_app(settings: Settings) -> FastAPI:
    """MFA not required, so a password-only session may change its password."""
    return build_app(settings.model_copy(update={"require_mfa": False}), limits=ROOMY)


def test_password_change_rotates_and_revokes_other_sessions(
    factories: Factories, no_mfa_app: FastAPI, admin_engine: Engine
) -> None:
    record = factories.user(role="member")
    other = factories.session(record)  # another device
    with browser(no_mfa_app) as web:
        headers = _logged_in(web, record)
        before = web.cookies.get("__Host-perfin-session")
        new_password = "a quiet orchard in late november"
        response = web.post(
            PASSWORD,
            json={"current_password": record.password, "new_password": new_password},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["sessions_revoked"] >= 1
        after = response.cookies.get("__Host-perfin-session")
        assert after
        assert after != before
        # The new CSRF token is what the session needs now.
        assert web.get(SESSIONS).status_code == 200
        assert web.delete(SESSIONS, headers=session_headers(body["csrf_token"])).status_code == 200
        web.cookies.clear()
        assert login(web, record.user.username, record.password).status_code == 401
        web.cookies.clear()
        assert login(web, record.user.username, new_password).status_code == 200
    with factories.transaction() as db:
        row = db.get(UserSession, other.row.id)
        assert row is not None
        assert row.revoked_reason == "password_change"
    rows = audit_rows(admin_engine, record.id, "user.password_change")
    assert rows
    assert new_password not in str(rows[-1].after)


def test_password_change_needs_the_current_password(
    factories: Factories, no_mfa_app: FastAPI, admin_engine: Engine
) -> None:
    record = factories.user(role="member")
    with browser(no_mfa_app) as web:
        headers = _logged_in(web, record)
        response = web.post(
            PASSWORD,
            json={"current_password": "not-the-password", "new_password": "x" * 20 + "unique"},
            headers=headers,
        )
    assert response.status_code == 403
    assert response.json()["type"] == "urn:perfin:problem:invalid-credentials"
    assert audit_rows(admin_engine, record.id, "user.password_change_failed")


@pytest.mark.parametrize(
    ("new_password", "reason"),
    [
        ("short", "too-short"),
        ("x" * 129, "too-long"),
        ("correcthorsebatterystaple", "breached"),
        ("Password1234", "breached"),
    ],
)
def test_password_change_policy(
    factories: Factories, no_mfa_app: FastAPI, new_password: str, reason: str
) -> None:
    record = factories.user(role="member")
    with browser(no_mfa_app) as web:
        headers = _logged_in(web, record)
        response = web.post(
            PASSWORD,
            json={"current_password": record.password, "new_password": new_password},
            headers=headers,
        )
    assert response.status_code == 422
    assert response.json()["type"] == "urn:perfin:problem:weak-password"
    assert response.json()["reason"] == reason


def test_password_change_requires_fresh_step_up_with_mfa(
    factories: Factories, settings: Settings, client: TestClient
) -> None:
    record = factories.user(role="member", with_totp=True)
    stale = clock.utcnow() - timedelta(minutes=30)
    login_state = factories.login(client, record, mfa_verified=False)
    with factories.transaction() as db:
        db.execute(
            update(UserSession)
            .where(UserSession.id == login_state.session_id)
            .values(mfa_verified_at=stale)
        )
    body = {"current_password": record.password, "new_password": "a brand new long phrase"}
    response = client.post(PASSWORD, json=body, headers=login_state.headers())
    assert response.status_code == 403
    assert response.json()["type"] == "urn:perfin:problem:step-up-required"
    with factories.transaction() as db:
        db.execute(
            update(UserSession)
            .where(UserSession.id == login_state.session_id)
            .values(mfa_verified_at=clock.utcnow())
        )
    response = client.post(PASSWORD, json=body, headers=login_state.headers())
    assert response.status_code == 200, response.text


def test_password_change_refuses_tokens(
    factories: Factories, client: TestClient, bearer_token: Callable[..., str]
) -> None:
    record = factories.user(role="member", with_totp=True)
    token = bearer_token(record)
    response = client.post(
        PASSWORD,
        json={"current_password": record.password, "new_password": "another long phrase here"},
        headers={"Authorization": f"Bearer {token}", "Origin": "http://testserver"},
    )
    assert response.status_code == 403
    assert response.json()["type"] == "urn:perfin:problem:session-required"


def test_login_audit_never_contains_the_password(
    factories: Factories, web: TestClient, admin_engine: Engine
) -> None:
    record = factories.user(role="member")
    login(web, record.user.username, "Typo-" + record.password)
    login(web, record.user.username, record.password)
    with admin_engine.connect() as conn:
        dump = conn.execute(
            text(
                "SELECT string_agg(coalesce(after::text, ''), ' ') FROM audit_log "
                "WHERE user_id = :u"
            ),
            {"u": record.id},
        ).scalar_one()
    assert record.password not in dump
    assert "password_hash" not in dump


def test_password_then_totp_completes_the_login(factories: Factories, web: TestClient) -> None:
    """The pending-session contract with ``POST /auth/mfa/verify`` (MFA track)."""
    record = factories.user(role="member", with_totp=True)
    first = login(web, record.user.username, record.password)
    assert first.json()["status"] == "mfa_required"
    pending_raw = first.cookies["__Host-perfin-session"]
    verify = web.post(
        f"{API_PREFIX}/auth/mfa/verify",
        json={"method": "totp", "code": record.totp_now()},
        headers=session_headers(first.json()["csrf_token"]),
    )
    assert verify.status_code == 200, verify.text
    full_raw = verify.cookies["__Host-perfin-session"]
    assert full_raw != pending_raw  # rotated on MFA completion (A3)
    session = web.get(SESSION_URL)
    assert session.status_code == 200
    assert session.json()["mfa"]["verified"] is True
