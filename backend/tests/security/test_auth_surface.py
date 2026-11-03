# SPDX-License-Identifier: AGPL-3.0-or-later
"""The whole API surface against CSRF and problem details (SECURITY.md A5, F5).

Every route is enumerated from the router modules ``create_app`` includes, so
a route added later is covered without editing this file:

* every state-changing ``/api/v1`` route refuses a request without a CSRF
  token, anonymous or with a valid session cookie (``403 csrf-failed``);
  the only exemption is the OIDC back-channel logout, which the IdP posts
  server to server and which is authenticated by the logout token's
  signature. Login, setup and password reset take the *pre-auth* token, so
  they are refused without one too.
* every error an auth, household, settings or admin route returns, anonymous
  or authenticated, with an empty or junk body, is
  ``application/problem+json`` with a ``urn:perfin:problem:`` type. The OIDC
  start and callback are browser navigations and answer with redirects
  (``303``), never with an error body.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app.api.routers as routers_package
from app.core.csrf import DEFAULT_EXEMPT_PATHS, SAFE_METHODS, CSRFProtect
from app.core.errors import PROBLEM_MEDIA_TYPE, PROBLEM_TYPE_PREFIX
from app.core.settings import Settings
from app.main import API_PREFIX
from tests.fixtures.auth_clients import SAME_ORIGIN, browser, build_app, pre_auth_headers
from tests.fixtures.factories import Factories, UserRecord

pytestmark = pytest.mark.integration

BACKCHANNEL = f"{API_PREFIX}/auth/oidc/backchannel-logout"
#: Route prefixes owned by Phase 1 (auth, households, user settings, admin).
PHASE1_PREFIXES = (
    f"{API_PREFIX}/auth",
    f"{API_PREFIX}/setup",
    f"{API_PREFIX}/households",
    f"{API_PREFIX}/settings/user",
    f"{API_PREFIX}/admin/users",
)
#: Browser navigations: answer with a redirect, never an error body.
NAVIGATIONS = frozenset({f"{API_PREFIX}/auth/oidc/start", f"{API_PREFIX}/auth/oidc/callback"})
_PARAM = re.compile(r"\{[^}]+\}")


@dataclass(frozen=True)
class Endpoint:
    method: str
    template: str

    @property
    def url(self) -> str:
        return _PARAM.sub(lambda _m: str(uuid.uuid4()), self.template)

    def __str__(self) -> str:
        return f"{self.method} {self.template}"


def _api_endpoints() -> list[Endpoint]:
    """Every (method, path) of the ``router`` objects ``create_app`` includes."""
    found: set[Endpoint] = set()
    for info in pkgutil.iter_modules(routers_package.__path__):
        module = importlib.import_module(f"{routers_package.__name__}.{info.name}")
        router = getattr(module, "router", None)
        if not isinstance(router, APIRouter):
            continue
        for route in router.routes:
            if isinstance(route, APIRoute):
                for method in route.methods or ():
                    found.add(Endpoint(method, API_PREFIX + route.path))
    return sorted(found, key=lambda e: (e.template, e.method))


ENDPOINTS = _api_endpoints()
UNSAFE = [e for e in ENDPOINTS if e.method not in SAFE_METHODS]
PHASE1 = [e for e in ENDPOINTS if e.template.startswith(PHASE1_PREFIXES)]


def test_the_enumeration_sees_the_auth_surface() -> None:
    """Guards against a vacuous pass: the state-changing auth routes are there."""
    unsafe = {str(e) for e in UNSAFE}
    for expected in (
        "POST /api/v1/auth/login",
        "POST /api/v1/auth/logout",
        "POST /api/v1/setup",
        "POST /api/v1/auth/password",
        "POST /api/v1/auth/password-reset/request",
        "POST /api/v1/auth/password-reset/confirm",
        "POST /api/v1/auth/mfa/verify",
        "POST /api/v1/auth/step-up",
        "POST /api/v1/auth/mfa/totp",
        "POST /api/v1/auth/tokens",
        "DELETE /api/v1/auth/tokens/{token_id}",
        "POST /api/v1/auth/session/household",
        "DELETE /api/v1/auth/sessions",
        "POST /api/v1/auth/oidc/logout",
        "POST /api/v1/auth/oidc/link",
        "POST /api/v1/auth/oidc/backchannel-logout",
        "PATCH /api/v1/households/{household_id}",
        "POST /api/v1/admin/users",
    ):
        assert expected in unsafe, expected


def test_the_enumeration_matches_the_openapi_document(settings: Settings) -> None:
    """Every documented operation is enumerated (health is not in the schema)."""
    documented = {
        Endpoint(method.upper(), path)
        for path, operations in build_app(settings).openapi()["paths"].items()
        for method in operations
    }
    enumerated = {e for e in ENDPOINTS if e.method != "HEAD"}
    assert documented == enumerated


def test_backchannel_logout_is_the_only_csrf_exemption(settings: Settings) -> None:
    protect = build_app(settings).state.csrf
    assert isinstance(protect, CSRFProtect)
    assert protect.exempt_paths == frozenset({BACKCHANNEL}) == DEFAULT_EXEMPT_PATHS


@pytest.fixture
def surface_app(settings: Settings) -> FastAPI:
    return build_app(settings)


@pytest.fixture
def web(surface_app: FastAPI) -> Iterator[TestClient]:
    with browser(surface_app) as client:
        yield client


@pytest.fixture
def member(factories: Factories) -> UserRecord:
    return factories.user(role="owner", with_totp=True)


def _is_csrf_failure(response: Any) -> bool:
    if response.status_code != 403:
        return False
    if response.headers.get("content-type") != PROBLEM_MEDIA_TYPE:
        return False
    return bool(response.json().get("type") == f"{PROBLEM_TYPE_PREFIX}csrf-failed")


@pytest.mark.parametrize("endpoint", UNSAFE, ids=str)
def test_unsafe_routes_need_a_csrf_token(
    endpoint: Endpoint, web: TestClient, factories: Factories, member: UserRecord
) -> None:
    url = endpoint.url
    # Anonymous, same-origin, no token (pre-auth routes included).
    anonymous = web.request(endpoint.method, url, headers=SAME_ORIGIN, json={})
    # With a valid session cookie but no token.
    factories.login(web, member)
    authed = web.request(endpoint.method, url, headers=SAME_ORIGIN, json={})
    if endpoint.template == BACKCHANNEL:
        # Exempt (server to server): it reaches the handler and is refused
        # there (OIDC is off in these settings) with a problem.
        for response in (anonymous, authed):
            assert not _is_csrf_failure(response)
            assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
        return
    assert _is_csrf_failure(anonymous), (endpoint, anonymous.status_code, anonymous.text)
    assert _is_csrf_failure(authed), (endpoint, authed.status_code, authed.text)


def _assert_problem(endpoint: Endpoint, response: Any) -> None:
    status = response.status_code
    if 300 <= status < 400:
        assert endpoint.template in NAVIGATIONS, (endpoint, status)
        return
    if status < 400:
        return
    assert response.headers.get("content-type") == PROBLEM_MEDIA_TYPE, (
        endpoint,
        status,
        response.headers.get("content-type"),
        response.text,
    )
    body = response.json()
    assert body["status"] == status, (endpoint, body)
    assert str(body["type"]).startswith(PROBLEM_TYPE_PREFIX), (endpoint, body)
    assert response.headers.get("cache-control") == "no-store", endpoint


@pytest.mark.parametrize("endpoint", PHASE1, ids=str)
@pytest.mark.parametrize("body", [None, {"unexpected": True}], ids=["empty", "junk"])
def test_phase1_errors_are_problem_details(
    endpoint: Endpoint,
    body: dict[str, Any] | None,
    web: TestClient,
    factories: Factories,
    member: UserRecord,
) -> None:
    kwargs: dict[str, Any] = {"follow_redirects": False}
    if endpoint.method not in SAFE_METHODS and body is not None:
        kwargs["json"] = body
    # Anonymous, with the pre-auth CSRF token (as the login form sends it).
    anonymous = web.request(endpoint.method, endpoint.url, headers=pre_auth_headers(web), **kwargs)
    _assert_problem(endpoint, anonymous)
    # Logged in (fresh MFA) with the session's CSRF token.
    login = factories.login(web, member)
    authed = web.request(endpoint.method, endpoint.url, headers=login.headers(), **kwargs)
    _assert_problem(endpoint, authed)
