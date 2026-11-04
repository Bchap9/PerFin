# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every error of the accounts, grants, categories, payees and tags routes is a problem.

Like ``test_phase1_errors_are_problem_details`` in
``tests/security/test_auth_surface.py`` (SECURITY.md F5): each route is called
anonymously and as a logged-in owner, with no body and with a junk body, on
random ids; whatever the answer, an error must be ``application/problem+json``
with a ``urn:perfin:problem:`` type, the right ``status`` and ``no-store``,
and must not echo the request's values.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.routers import account_grants, accounts, categories, payees, tags
from app.core.csrf import SAFE_METHODS
from app.core.errors import PROBLEM_MEDIA_TYPE, PROBLEM_TYPE_PREFIX
from app.core.settings import Settings
from app.main import API_PREFIX
from tests.fixtures.auth_clients import browser, build_app, pre_auth_headers
from tests.fixtures.factories import Factories, UserRecord

pytestmark = [pytest.mark.integration, pytest.mark.security]

_PARAM = re.compile(r"\{[^}]+\}")
MARKER = "zz-canary-4f1c"


def _endpoints() -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    for module in (accounts, account_grants, categories, payees, tags):
        for route in module.router.routes:
            if isinstance(route, APIRoute):
                for method in route.methods or ():
                    found.add((method, API_PREFIX + route.path))
    return sorted(found)


ENDPOINTS = _endpoints()


def test_the_enumeration_is_not_vacuous() -> None:
    assert len(ENDPOINTS) >= 30
    assert ("POST", f"{API_PREFIX}/categories/{{category_id}}/merge") in ENDPOINTS


@pytest.fixture
def web(settings: Settings) -> Iterator[TestClient]:
    app: FastAPI = build_app(settings)
    with browser(app) as client:
        yield client


@pytest.fixture
def owner(factories: Factories) -> UserRecord:
    return factories.user(role="owner", with_totp=True)


def _check(endpoint: tuple[str, str], response: Any) -> None:
    status = response.status_code
    if status < 400:
        return
    assert response.headers.get("content-type") == PROBLEM_MEDIA_TYPE, (endpoint, response.text)
    body = response.json()
    assert body["status"] == status, (endpoint, body)
    assert str(body["type"]).startswith(PROBLEM_TYPE_PREFIX), (endpoint, body)
    assert response.headers.get("cache-control") == "no-store", endpoint
    assert MARKER not in response.text, (endpoint, response.text)


@pytest.mark.parametrize("endpoint", ENDPOINTS, ids=" ".join)
@pytest.mark.parametrize("body", [None, {"unexpected": MARKER, "name": MARKER * 30}])
def test_errors_are_problem_details(
    endpoint: tuple[str, str],
    body: dict[str, Any] | None,
    web: TestClient,
    factories: Factories,
    owner: UserRecord,
) -> None:
    method, template = endpoint
    url = _PARAM.sub(lambda _m: str(uuid.uuid4()), template)
    kwargs: dict[str, Any] = {"follow_redirects": False}
    if method not in SAFE_METHODS and body is not None:
        kwargs["json"] = body
    _check(endpoint, web.request(method, url, headers=pre_auth_headers(web), **kwargs))
    login = factories.login(web, owner)
    _check(endpoint, web.request(method, url, headers=login.headers(), **kwargs))
