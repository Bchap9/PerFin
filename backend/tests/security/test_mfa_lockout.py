# SPDX-License-Identifier: AGPL-3.0-or-later
"""A correct password does not wipe the lockout that MFA failures built up (A1/A8).

Review P1-SR-02: second-factor failures feed the account's progressive
lockout (``failed_login_count`` / ``locked_until``), but a correct password
used to reset both while the new session was still ``mfa_pending``. Someone
holding the password could then interleave password logins with code
guesses and never reach the growing delay or the 15 minute lock. Now the
counter is reset only by a verified second factor (or by a password login
of an account that has none).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.auth.local import AuthLimits
from app.auth.models import User
from app.auth.ratelimit import Limit
from app.core.settings import Settings
from app.main import API_PREFIX
from tests.fixtures.auth_clients import browser, build_app, pre_auth_headers, session_headers
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.mfa import FrozenClock, freezer, totp_code

pytestmark = [pytest.mark.integration, pytest.mark.security]

LOGIN = f"{API_PREFIX}/auth/login"
VERIFY = f"{API_PREFIX}/auth/mfa/verify"

#: Roomy request-rate limits, the real progressive lockout (DEFAULT_LOCKOUT).
LIMITS = AuthLimits(
    login_per_ip=Limit("login:ip", 1000, timedelta(minutes=5)),
    login_per_account=Limit("login:account", 1000, timedelta(minutes=15)),
)


@pytest.fixture
def freeze(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FrozenClock]:
    return freezer(monkeypatch)


def _state(factories: Factories, user_id: uuid.UUID) -> tuple[int, datetime | None]:
    with factories.transaction() as db:
        user = db.get(User, user_id)
        assert user is not None
        return user.failed_login_count, user.locked_until


def _password(client: TestClient, record: UserRecord) -> Any:
    return client.post(
        LOGIN,
        json={"username": record.user.username, "password": record.password},
        headers=pre_auth_headers(client),
    )


def _right(record: UserRecord, clk: FrozenClock) -> str:
    return totp_code(record.totp_secret or "", clk.now)


def _wrong(record: UserRecord, clk: FrozenClock) -> str:
    return f"{(int(_right(record, clk)) + 500_000) % 1_000_000:06d}"


def test_correct_password_keeps_the_mfa_failure_count(
    factories: Factories, settings: Settings, freeze: Callable[..., FrozenClock]
) -> None:
    clk = freeze()
    app = build_app(settings, limits=LIMITS)
    record = factories.user(role="member", with_totp=True)
    with browser(app) as client:
        first = _password(client, record)
        assert first.status_code == 200, first.text
        assert first.json()["status"] == "mfa_required"
        csrf = first.json()["csrf_token"]
        for _ in range(3):
            clk.advance(seconds=10)  # past any delay, within the MFA windows
            bad = client.post(
                VERIFY,
                json={"method": "totp", "code": _wrong(record, clk)},
                headers=session_headers(csrf),
            )
            assert bad.status_code == 400, bad.text
    count, locked_until = _state(factories, record.id)
    assert count == 3
    assert locked_until is not None  # the progressive delay has started

    clk.advance(seconds=10)  # the delay is over, so the password is accepted ...
    with browser(app) as client:
        again = _password(client, record)
        assert again.status_code == 200, again.text
        assert again.json()["status"] == "mfa_required"
        # ... but the session is only mfa_pending: nothing is forgiven yet.
        assert _state(factories, record.id) == (count, locked_until)

        # One more wrong code continues the same count (and a longer delay).
        clk.advance(seconds=10)
        csrf = again.json()["csrf_token"]
        bad = client.post(
            VERIFY,
            json={"method": "totp", "code": _wrong(record, clk)},
            headers=session_headers(csrf),
        )
        assert bad.status_code == 400, bad.text
        assert _state(factories, record.id)[0] == count + 1

        # Only a verified second factor resets it.
        clk.advance(seconds=10)
        good = client.post(
            VERIFY,
            json={"method": "totp", "code": _right(record, clk)},
            headers=session_headers(csrf),
        )
        assert good.status_code == 200, good.text
    assert _state(factories, record.id) == (0, None)


def test_password_alone_resets_an_account_without_a_factor(
    factories: Factories, settings: Settings, freeze: Callable[..., FrozenClock]
) -> None:
    clk = freeze()
    app = build_app(settings.model_copy(update={"require_mfa": False}), limits=LIMITS)
    record = factories.user(role="member", with_totp=False)
    with browser(app) as client:
        for _ in range(3):
            clk.advance(seconds=10)
            bad = client.post(
                LOGIN,
                json={"username": record.user.username, "password": "not-the-password-123"},
                headers=pre_auth_headers(client),
            )
            assert bad.status_code == 401
    assert _state(factories, record.id)[0] == 3
    clk.advance(seconds=10)
    with browser(app) as client:
        ok = _password(client, record)
        assert ok.status_code == 200, ok.text
        assert ok.json()["status"] == "authenticated"
    assert _state(factories, record.id) == (0, None)
