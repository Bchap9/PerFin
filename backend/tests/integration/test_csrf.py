# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSRF protection (SECURITY.md A5): provenance checks plus session-bound and
pre-auth signed double-submit tokens."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.testclient import TestClient
from starlette.requests import HTTPConnection

from app.core.csrf import (
    CSRF_HEADER,
    PRE_AUTH_COOKIE,
    CSRFProtect,
    get_csrf,
    normalize_origin,
)
from app.core.settings import Settings
from app.main import API_PREFIX, create_app

pytestmark = pytest.mark.integration

SESSION_COOKIE = "synthetic-session"
# A stand-in for ``GET /auth/session`` that exercises CSRFProtect with a fake
# resolver; the real endpoint is tested in tests/integration/test_auth_session.py.
PROBE_PATH = "/csrf-probe/session"
# A stand-in login form target; the real ``POST /auth/login`` (auth_local router)
# owns that path and is tested in tests/integration/test_local_login.py.
PROBE_LOGIN = "/csrf-probe/login"
ORIGIN = "http://testserver"


def _build(settings: Settings) -> FastAPI:
    app = create_app(settings)
    api = APIRouter()
    root = APIRouter()

    @api.get(PROBE_PATH)
    def session(request: Request, response: Response) -> dict[str, str]:
        csrf = get_csrf(request)
        sid = csrf.session_id(request)
        if sid:
            return {"csrf_token": csrf.token_for_session(sid)}
        return {"csrf_token": csrf.issue_pre_auth(response)}

    @api.post(PROBE_LOGIN)
    def login() -> dict[str, str]:
        return {"ok": "login"}

    @api.api_route("/things", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    def things() -> dict[str, str]:
        return {"ok": "things"}

    @api.api_route("/dav", methods=["PROPFIND", "MKCOL", "TRACE", "REPORT"])
    def dav() -> dict[str, str]:
        return {"ok": "dav"}

    @api.post("/auth/oidc/backchannel-logout")
    def backchannel() -> dict[str, str]:
        return {"ok": "backchannel"}

    @root.post("/root-post")
    def root_post() -> dict[str, str]:
        return {"ok": "root"}

    app.include_router(api, prefix=API_PREFIX)
    app.include_router(root)
    return app


def _cookie_session_resolver(conn: HTTPConnection) -> str | None:
    """Stand-in for the Phase 1 resolver: the session id is the cookie value."""
    return conn.cookies.get(SESSION_COOKIE) or None


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return _build(settings)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def _pre_auth(client: TestClient) -> tuple[str, str]:
    """Fetch a pre-auth token; return ``(nonce cookie value, token)``."""
    response = client.get(f"{API_PREFIX}{PROBE_PATH}")
    assert response.status_code == 200
    nonce = response.cookies.get(PRE_AUTH_COOKIE)
    assert nonce
    return nonce, response.json()["csrf_token"]


def _post(
    client: TestClient,
    path: str = "/things",
    *,
    method: str = "POST",
    token: str | None = None,
    cookies: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> int:
    all_headers = {"Origin": ORIGIN, "Sec-Fetch-Site": "same-origin"}
    if token is not None:
        all_headers[CSRF_HEADER] = token
    if cookies:
        all_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    all_headers.update(headers or {})
    response = client.request(method, f"{API_PREFIX}{path}", headers=all_headers)
    if response.status_code == 403:
        assert response.headers["content-type"] == "application/problem+json"
        assert response.json()["type"] == "urn:perfin:problem:csrf-failed"
    return response.status_code


# ---- pre-auth double submit ---------------------------------------------------------


def test_safe_methods_are_not_checked(client: TestClient) -> None:
    assert client.get(f"{API_PREFIX}/things").status_code == 200
    assert client.head(f"{API_PREFIX}/things").status_code in (200, 405)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_unsafe_methods_need_a_token(client: TestClient, method: str) -> None:
    assert _post(client, method=method) == 403
    nonce, token = _pre_auth(client)
    assert _post(client, method=method, token=token, cookies={PRE_AUTH_COOKIE: nonce}) == 200


@pytest.mark.parametrize("method", ["PROPFIND", "MKCOL", "TRACE", "REPORT"])
def test_extension_methods_need_a_token(client: TestClient, method: str) -> None:
    """Review finding SR-7: only GET/HEAD/OPTIONS skip the check (allowlist)."""
    assert _post(client, "/dav", method=method) == 403
    nonce, token = _pre_auth(client)
    assert (
        _post(client, "/dav", method=method, token=token, cookies={PRE_AUTH_COOKIE: nonce}) == 200
    )


def test_pre_auth_cookie_attributes(client: TestClient) -> None:
    response = client.get(f"{API_PREFIX}{PROBE_PATH}")
    set_cookie = response.headers["set-cookie"]
    assert set_cookie.startswith(f"{PRE_AUTH_COOKIE}=")
    lowered = set_cookie.lower()
    for attribute in ("path=/", "secure", "httponly", "samesite=strict"):
        assert attribute in lowered
    assert "domain=" not in lowered  # required by the __Host- prefix


def test_login_with_pre_auth_token(client: TestClient) -> None:
    nonce, token = _pre_auth(client)
    assert _post(client, PROBE_LOGIN, token=token, cookies={PRE_AUTH_COOKIE: nonce}) == 200


def test_cookie_alone_is_not_enough(client: TestClient) -> None:
    nonce, _ = _pre_auth(client)
    assert _post(client, PROBE_LOGIN, cookies={PRE_AUTH_COOKIE: nonce}) == 403


def test_token_alone_is_not_enough(client: TestClient) -> None:
    _, token = _pre_auth(client)
    assert _post(client, PROBE_LOGIN, token=token) == 403


def test_naive_double_submit_is_rejected(client: TestClient) -> None:
    # An attacker who can plant a cookie cannot just echo it: the token is an HMAC.
    assert _post(client, token="planted", cookies={PRE_AUTH_COOKIE: "planted"}) == 403


def test_token_for_another_nonce_is_rejected(client: TestClient) -> None:
    nonce_a, _ = _pre_auth(client)
    _, token_b = _pre_auth(client)
    assert _post(client, token=token_b, cookies={PRE_AUTH_COOKIE: nonce_a}) == 403


def test_oversized_token_is_rejected(client: TestClient) -> None:
    nonce, _ = _pre_auth(client)
    assert _post(client, token="A" * 10_000, cookies={PRE_AUTH_COOKIE: nonce}) == 403


def test_token_from_another_secret_is_rejected(client: TestClient) -> None:
    other = CSRFProtect(secret_key=b"another-secret-key-0123456789", public_origin=ORIGIN)
    nonce = "synthetic-nonce"
    assert _post(client, token=other.pre_auth_token(nonce), cookies={PRE_AUTH_COOKIE: nonce}) == 403


# ---- session-bound tokens ------------------------------------------------------------


def protect_for(app: FastAPI) -> CSRFProtect:
    protect = app.state.csrf
    assert isinstance(protect, CSRFProtect)
    return protect


@pytest.fixture
def session_client(app: FastAPI) -> Iterator[TestClient]:
    protect_for(app).set_session_id_resolver(_cookie_session_resolver)
    with TestClient(app) as test_client:
        yield test_client


def test_session_token_round_trip(session_client: TestClient) -> None:
    cookies = {SESSION_COOKIE: "session-one"}
    response = session_client.get(
        f"{API_PREFIX}{PROBE_PATH}", headers={"Cookie": f"{SESSION_COOKIE}=session-one"}
    )
    token = response.json()["csrf_token"]
    assert PRE_AUTH_COOKIE not in response.headers.get("set-cookie", "")
    assert _post(session_client, token=token, cookies=cookies) == 200


def test_session_token_is_bound_to_the_session(app: FastAPI, session_client: TestClient) -> None:
    protect = protect_for(app)
    token_for_other = protect.token_for_session("session-two")
    assert (
        _post(session_client, token=token_for_other, cookies={SESSION_COOKIE: "session-one"}) == 403
    )


def test_pre_auth_token_not_accepted_once_a_session_exists(
    app: FastAPI, session_client: TestClient
) -> None:
    protect = protect_for(app)
    nonce = "synthetic-nonce"
    cookies = {SESSION_COOKIE: "session-one", PRE_AUTH_COOKIE: nonce}
    assert _post(session_client, token=protect.pre_auth_token(nonce), cookies=cookies) == 403


# ---- provenance ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "http://testserver.evil.example"},
        {"Origin": "https://testserver"},  # scheme differs
        {"Origin": "http://testserver:8080"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cross_site_requests_rejected_even_with_valid_token(
    client: TestClient, headers: dict[str, str]
) -> None:
    nonce, token = _pre_auth(client)
    assert _post(client, token=token, cookies={PRE_AUTH_COOKIE: nonce}, headers=headers) == 403


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://TestServer:80"},  # canonicalised
        {"Sec-Fetch-Site": "none"},
    ],
)
def test_same_origin_variants_accepted(client: TestClient, headers: dict[str, str]) -> None:
    nonce, token = _pre_auth(client)
    assert _post(client, token=token, cookies={PRE_AUTH_COOKIE: nonce}, headers=headers) == 200


def test_requests_without_fetch_metadata_still_need_token(client: TestClient) -> None:
    response = client.post(f"{API_PREFIX}/things")
    assert response.status_code == 403


# ---- exemptions ----------------------------------------------------------------------


def test_bearer_requests_skip_the_token(client: TestClient) -> None:
    response = client.post(
        f"{API_PREFIX}/things", headers={"Authorization": "Bearer synthetic-pat"}
    )
    assert response.status_code == 200


@pytest.mark.parametrize("authorization", ["Bearer ", "Basic dXNlcjpwYXNz", "bearer"])
def test_non_bearer_authorization_is_not_exempt(client: TestClient, authorization: str) -> None:
    response = client.post(f"{API_PREFIX}/things", headers={"Authorization": authorization})
    assert response.status_code == 403


def test_bearer_requests_still_rejected_cross_site(client: TestClient) -> None:
    response = client.post(
        f"{API_PREFIX}/things",
        headers={"Authorization": "Bearer synthetic-pat", "Sec-Fetch-Site": "cross-site"},
    )
    assert response.status_code == 403


def test_backchannel_logout_is_exempt(client: TestClient) -> None:
    # The real OIDC router (app/api/routers/auth_oidc.py) owns this path and
    # shadows the stand-in above; with OIDC not configured it answers
    # ``404 oidc-disabled``. Reaching the handler without Origin,
    # Sec-Fetch-Site or a token is what proves the CSRF exemption.
    response = client.post(
        f"{API_PREFIX}/auth/oidc/backchannel-logout", data={"logout_token": "synthetic"}
    )
    assert response.status_code == 404
    assert response.json()["type"] == "urn:perfin:problem:oidc-disabled"
    # A non-exempt unsafe request under the same conditions is refused.
    assert client.post(f"{API_PREFIX}/auth/oidc/logout").status_code == 403


def test_non_api_paths_are_not_checked(client: TestClient) -> None:
    assert client.post("/root-post").status_code == 200


# ---- units ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://Perfin.Example.com/", "https://perfin.example.com"),
        ("https://perfin.example.com:443", "https://perfin.example.com"),
        ("http://localhost:5173/app", "http://localhost:5173"),
        ("http://[::1]:8000", "http://[::1]:8000"),
        ("ftp://perfin.example.com", None),
        ("null", None),
        ("https://", None),
        ("http://host:99999", None),
    ],
)
def test_normalize_origin(value: str, expected: str | None) -> None:
    assert normalize_origin(value) == expected


def test_protect_rejects_weak_configuration() -> None:
    with pytest.raises(ValueError, match="too short"):
        CSRFProtect(secret_key=b"short", public_origin=ORIGIN)
    with pytest.raises(ValueError, match="origin"):
        CSRFProtect(secret_key=b"x" * 32, public_origin="not a url")


def test_get_csrf_requires_installation() -> None:
    bare = FastAPI()
    request = Request({"type": "http", "app": bare, "headers": []})
    with pytest.raises(RuntimeError, match="not installed"):
        get_csrf(request)


def test_tokens_are_deterministic_per_key() -> None:
    a = CSRFProtect(secret_key=b"k" * 32, public_origin=ORIGIN)
    b = CSRFProtect(secret_key=b"k" * 32, public_origin=ORIGIN)
    assert a.token_for_session("s1") == b.token_for_session("s1")
    assert a.token_for_session("s1") != a.token_for_session("s2")
    assert a.token_for_session("s1") != a.pre_auth_token("s1")  # domain separated
