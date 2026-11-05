# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers for the MFA tests: endpoint paths, CSRF headers after rotation,
adopting a rotated session cookie, freezing the clock."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pyotp
import pytest
from fastapi.testclient import TestClient

from app.auth.mfa import totp
from app.auth.sessions import cookie_name
from app.core import clock
from app.core.csrf import CSRF_HEADER
from app.core.settings import Settings
from app.main import API_PREFIX
from tests.fixtures.factories import TEST_ORIGIN

VERIFY = f"{API_PREFIX}/auth/mfa/verify"
STEP_UP = f"{API_PREFIX}/auth/step-up"
MFA = f"{API_PREFIX}/auth/mfa"
TOTP = f"{API_PREFIX}/auth/mfa/totp"
TOTP_CONFIRM = f"{API_PREFIX}/auth/mfa/totp/confirm"
WEBAUTHN = f"{API_PREFIX}/auth/mfa/webauthn"
WEBAUTHN_REG_OPTIONS = f"{API_PREFIX}/auth/mfa/webauthn/registration-options"
WEBAUTHN_AUTH_OPTIONS = f"{API_PREFIX}/auth/mfa/webauthn/authentication-options"
RECOVERY = f"{API_PREFIX}/auth/mfa/recovery-codes"
SESSION = f"{API_PREFIX}/auth/session"
SESSIONS = f"{API_PREFIX}/auth/sessions"


def unsafe_headers(csrf_token: str) -> dict[str, str]:
    return {CSRF_HEADER: csrf_token, "Origin": TEST_ORIGIN, "Sec-Fetch-Site": "same-origin"}


def set_cookie_value(response: Any, name: str) -> str | None:
    """The value a ``Set-Cookie`` header gives ``name`` (the jar ignores
    ``Secure`` cookies over the TestClient's plain http)."""
    headers: list[str] = list(response.headers.get_list("set-cookie"))
    for header in headers:
        key, _, rest = header.partition("=")
        if key == name:
            return rest.split(";", 1)[0]
    return None


def adopt_session_cookie(client: TestClient, settings: Settings, response: Any) -> str:
    """Put the rotated session cookie from ``response`` on ``client``; return it."""
    name = cookie_name(settings)
    value = set_cookie_value(response, name)
    assert value, "response did not set a session cookie"
    client.cookies.clear()
    client.cookies.set(name, value)
    return value


def use_cookie(client: TestClient, settings: Settings, raw: str) -> None:
    client.cookies.clear()
    client.cookies.set(cookie_name(settings), raw)


def totp_code(secret: str, at: datetime | None = None, offset_steps: int = 0) -> str:
    step = totp.counter_at(at or clock.utcnow()) + offset_steps
    return pyotp.TOTP(secret).generate_otp(step)


class FrozenClock:
    """``app.core.clock.utcnow`` pinned to ``now``; ``advance`` moves it."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, now: datetime) -> None:
        self.now = now
        monkeypatch.setattr(clock, "utcnow", self._now)

    def _now(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> datetime:
        self.now = self.now + timedelta(**kwargs)
        return self.now


def freezer(monkeypatch: pytest.MonkeyPatch) -> Callable[[datetime | None], FrozenClock]:
    def freeze(now: datetime | None = None) -> FrozenClock:
        return FrozenClock(monkeypatch, now or clock.utcnow())

    return freeze
