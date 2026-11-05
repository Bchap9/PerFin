# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers for tests that drive the login, setup and reset endpoints.

* :func:`random_ip` gives each test its own client address, so the per-IP
  rate limits of one test never affect another (the test database is shared
  by the whole session).
* :func:`browser` builds a ``TestClient`` with that address on
  ``https://testserver``: the session and pre-auth cookies carry ``Secure``,
  and a cookie jar only returns those over https.
* :func:`pre_auth_headers` fetches the pre-auth CSRF token the SPA would use
  for a login form (``GET /auth/session`` while anonymous).
"""

from __future__ import annotations

import secrets
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.local import AuthLimits
from app.core.csrf import CSRF_HEADER
from app.core.settings import Settings
from app.main import API_PREFIX, create_app

ORIGIN = "http://testserver"
SAME_ORIGIN = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
SESSION_URL = f"{API_PREFIX}/auth/session"


def random_ip() -> str:
    """A random address in 10.0.0.0/8 (never in the default break-glass CIDR)."""
    return f"10.{secrets.randbelow(256)}.{secrets.randbelow(256)}.{1 + secrets.randbelow(254)}"


def build_app(settings: Settings, *, limits: AuthLimits | None = None, **state: Any) -> FastAPI:
    app = create_app(settings)
    if limits is not None:
        app.state.auth_limits = limits
    for key, value in state.items():
        setattr(app.state, key, value)
    return app


@contextmanager
def browser(app: FastAPI, ip: str | None = None) -> Iterator[TestClient]:
    """A client for ``app`` from ``ip`` (random by default) over https."""
    with TestClient(
        app, base_url="https://testserver", client=(ip or random_ip(), 50000)
    ) as client:
        yield client


def pre_auth_headers(client: TestClient) -> dict[str, str]:
    """Headers for an unsafe request without a session (login form)."""
    response = client.get(SESSION_URL)
    assert response.status_code == 401, response.text
    return {**SAME_ORIGIN, CSRF_HEADER: response.json()["csrf_token"]}


def session_headers(csrf_token: str) -> dict[str, str]:
    return {**SAME_ORIGIN, CSRF_HEADER: csrf_token}
