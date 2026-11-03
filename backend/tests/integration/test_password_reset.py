# SPDX-License-Identifier: AGPL-3.0-or-later
"""Password reset by email (``app.auth.reset``; SECURITY.md A2, A11)."""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text, update

from app.auth.local import AuthLimits
from app.auth.models import PasswordResetToken, User, UserSession
from app.auth.ratelimit import Limit, LockoutPolicy
from app.auth.reset import hash_reset_token
from app.core import clock
from app.core.settings import Settings
from app.main import API_PREFIX
from tests.fixtures.auth_clients import browser, build_app, pre_auth_headers
from tests.fixtures.factories import Factories, UserRecord

pytestmark = pytest.mark.integration

REQUEST = f"{API_PREFIX}/auth/password-reset/request"
CONFIRM = f"{API_PREFIX}/auth/password-reset/confirm"
LOGIN = f"{API_PREFIX}/auth/login"
NEW_PASSWORD = "lantern meadow quietly hums"
LINK_RE = re.compile(r"https?://\S+/reset-password#token=([A-Za-z0-9_-]+)")

ROOMY = AuthLimits(
    login_per_ip=Limit("login:ip", 1000, timedelta(minutes=5)),
    login_per_account=Limit("login:account", 1000, timedelta(minutes=15)),
    reset_request_per_ip=Limit("reset:ip", 1000, timedelta(minutes=15)),
    reset_request_per_account=Limit("reset:account", 1000, timedelta(minutes=15)),
    reset_confirm_per_ip=Limit("reset_confirm:ip", 1000, timedelta(minutes=15)),
    lockout=LockoutPolicy(delay_after=1000, lock_after=1001),
)


@dataclass
class FakeMailer:
    sent: list[dict[str, str]] = field(default_factory=list)

    def send(self, *, to: str, subject: str, body: str) -> None:
        self.sent.append({"to": to, "subject": subject, "body": body})

    def token(self, index: int = -1) -> str:
        match = LINK_RE.search(self.sent[index]["body"])
        assert match is not None, self.sent[index]["body"]
        return match.group(1)


@pytest.fixture
def smtp_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"smtp_host": "smtp.example.test", "smtp_from": "perfin@example.test"}
    )


@pytest.fixture
def mailer() -> FakeMailer:
    return FakeMailer()


@pytest.fixture
def app(smtp_settings: Settings, mailer: FakeMailer) -> FastAPI:
    return build_app(smtp_settings, limits=ROOMY, mailer=mailer)


@pytest.fixture
def web(app: FastAPI) -> Iterator[TestClient]:
    with browser(app) as client:
        yield client


def emailed_user(factories: Factories) -> UserRecord:
    name = f"reset-{uuid.uuid4().hex[:8]}"
    return factories.user(role="member", username=name, email=f"{name}@example.test")


def request_reset(client: TestClient, identifier: str) -> Any:
    return client.post(REQUEST, json={"identifier": identifier}, headers=pre_auth_headers(client))


def confirm(client: TestClient, token: str, password: str = NEW_PASSWORD) -> Any:
    return client.post(
        CONFIRM, json={"token": token, "new_password": password}, headers=pre_auth_headers(client)
    )


def test_full_reset_flow(
    factories: Factories, web: TestClient, mailer: FakeMailer, admin_engine: Engine
) -> None:
    record = emailed_user(factories)
    live = factories.session(record)
    response = request_reset(web, record.user.username)
    assert response.status_code == 202
    assert response.json() == {"status": "accepted"}
    assert len(mailer.sent) == 1
    mail = mailer.sent[0]
    assert mail["to"] == record.user.email
    # The token travels in the URL fragment, never in a path or query string.
    assert "#token=" in mail["body"]
    assert "?token" not in mail["body"]
    token = mailer.token()
    with factories.transaction() as db:
        row = db.execute(
            select(PasswordResetToken).where(PasswordResetToken.user_id == record.id)
        ).scalar_one()
        assert row.token_hash == hash_reset_token(token)
        assert token not in row.token_hash
        assert row.expires_at - row.created_at == timedelta(minutes=15)

    done = confirm(web, token)
    assert done.status_code == 204, done.text
    assert done.headers["referrer-policy"] == "no-referrer"
    # Every session of the user is gone; the new password works.
    with factories.transaction() as db:
        old = db.get(UserSession, live.row.id)
        assert old is not None
        assert old.revoked_reason == "password_reset"
    headers = pre_auth_headers(web)
    assert (
        web.post(
            LOGIN,
            json={"username": record.user.username, "password": record.password},
            headers=headers,
        ).status_code
        == 401
    )
    ok = web.post(
        LOGIN, json={"username": record.user.username, "password": NEW_PASSWORD}, headers=headers
    )
    assert ok.status_code == 200
    with admin_engine.connect() as conn:
        actions = [
            r.action
            for r in conn.execute(
                text("SELECT action, after FROM audit_log WHERE user_id = :u ORDER BY seq"),
                {"u": record.id},
            )
        ]
        dump = conn.execute(
            text(
                "SELECT string_agg(coalesce(after::text,''), ' ') FROM audit_log WHERE user_id=:u"
            ),
            {"u": record.id},
        ).scalar_one()
    assert "user.password_reset_requested" in actions
    assert "user.password_reset" in actions
    assert token not in dump
    assert NEW_PASSWORD not in dump


def test_token_is_single_use(factories: Factories, web: TestClient, mailer: FakeMailer) -> None:
    record = emailed_user(factories)
    request_reset(web, record.user.username)
    token = mailer.token()
    assert confirm(web, token).status_code == 204
    again = confirm(web, token, "another long passphrase entirely")
    assert again.status_code == 400
    assert again.json()["type"] == "urn:perfin:problem:invalid-reset-token"


def test_token_expires_after_fifteen_minutes(
    factories: Factories, web: TestClient, mailer: FakeMailer
) -> None:
    record = emailed_user(factories)
    request_reset(web, record.user.username)
    token = mailer.token()
    with factories.transaction() as db:
        db.execute(
            update(PasswordResetToken)
            .where(PasswordResetToken.user_id == record.id)
            .values(expires_at=clock.utcnow() - timedelta(seconds=1))
        )
    response = confirm(web, token)
    assert response.status_code == 400
    assert response.json()["type"] == "urn:perfin:problem:invalid-reset-token"


def test_new_request_invalidates_the_previous_token(
    factories: Factories, web: TestClient, mailer: FakeMailer
) -> None:
    record = emailed_user(factories)
    request_reset(web, record.user.username)
    first = mailer.token()
    request_reset(web, record.user.email or "")  # by email this time
    second = mailer.token()
    assert first != second
    assert confirm(web, first).status_code == 400
    assert confirm(web, second).status_code == 204


def test_weak_password_does_not_consume_the_token(
    factories: Factories, web: TestClient, mailer: FakeMailer
) -> None:
    record = emailed_user(factories)
    request_reset(web, record.user.username)
    token = mailer.token()
    weak = confirm(web, token, "password1234")
    assert weak.status_code == 422
    assert weak.json()["reason"] == "breached"
    assert confirm(web, token).status_code == 204


def test_reset_clears_a_lockout(factories: Factories, web: TestClient, mailer: FakeMailer) -> None:
    record = emailed_user(factories)
    with factories.transaction() as db:
        db.execute(
            update(User)
            .where(User.id == record.id)
            .values(failed_login_count=12, locked_until=clock.utcnow() + timedelta(minutes=15))
        )
    request_reset(web, record.user.username)
    assert confirm(web, mailer.token()).status_code == 204
    with factories.transaction() as db:
        user = db.get(User, record.id)
        assert user is not None
        assert user.failed_login_count == 0
        assert user.locked_until is None


def test_response_is_identical_for_unknown_and_known_accounts(
    factories: Factories, web: TestClient, mailer: FakeMailer
) -> None:
    record = emailed_user(factories)
    no_email = factories.user(role="member")
    glass = factories.user(
        role=None, is_breakglass=True, email=f"glass-{uuid.uuid4().hex[:6]}@example.test"
    )
    responses = [
        request_reset(web, record.user.username),
        request_reset(web, f"nobody-{uuid.uuid4().hex[:8]}"),
        request_reset(web, f"nobody-{uuid.uuid4().hex[:8]}@example.test"),
        request_reset(web, no_email.user.username),
        request_reset(web, glass.user.username),
    ]
    shapes = {(r.status_code, r.text, frozenset(k.lower() for k in r.headers)) for r in responses}
    assert len(shapes) == 1
    assert responses[0].status_code == 202
    # Only the real, eligible account got mail (break-glass accounts never do).
    assert [m["to"] for m in mailer.sent] == [record.user.email]


def test_unavailable_without_smtp(settings: Settings, factories: Factories) -> None:
    app = build_app(settings, limits=ROOMY, mailer=FakeMailer())
    record = emailed_user(factories)
    with browser(app) as client:
        response = request_reset(client, record.user.username)
        assert response.status_code == 404
        assert response.json()["type"] == "urn:perfin:problem:password-reset-unavailable"
        assert confirm(client, "x" * 43).status_code == 404


def test_unavailable_in_oidc_only_mode(smtp_settings: Settings, factories: Factories) -> None:
    app = build_app(
        smtp_settings.model_copy(update={"local_login_enabled": False}),
        limits=ROOMY,
        mailer=FakeMailer(),
    )
    record = emailed_user(factories)
    with browser(app) as client:
        assert request_reset(client, record.user.username).status_code == 404


def test_request_is_rate_limited(smtp_settings: Settings, factories: Factories) -> None:
    limits = AuthLimits(
        reset_request_per_ip=Limit("reset:ip", 1000, timedelta(minutes=15)),
        reset_request_per_account=Limit("reset:account", 2, timedelta(minutes=15)),
    )
    mailer = FakeMailer()
    app = build_app(smtp_settings, limits=limits, mailer=mailer)
    record = emailed_user(factories)
    with browser(app) as client:
        codes = [request_reset(client, record.user.username).status_code for _ in range(3)]
    assert codes == [202, 202, 429]
    assert len(mailer.sent) == 2


def test_referrer_policy_on_reset_endpoints(web: TestClient) -> None:
    response = web.post(
        CONFIRM,
        json={"token": "x" * 43, "new_password": NEW_PASSWORD},
        headers=pre_auth_headers(web),
    )
    assert response.status_code == 400
    assert response.headers["referrer-policy"] == "no-referrer"
    # The SPA page that reads the fragment is served under the same policy.
    assert web.get("/reset-password").headers["referrer-policy"] == "no-referrer"
