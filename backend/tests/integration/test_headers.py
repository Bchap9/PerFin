# SPDX-License-Identifier: AGPL-3.0-or-later
"""Security headers, CSP, caching and request context (SECURITY.md F1, F6, F7,
F9, C4) on the real application stack."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from ipaddress import ip_network

import pytest
from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.testclient import TestClient

from app.core.headers import (
    DOWNLOAD_CSP,
    HSTS_VALUE,
    PERMISSIONS_POLICY,
    STATIC_CACHE_CONTROL,
    attachment_headers,
    build_csp,
)
from app.core.request_context import client_ip, request_id, valid_request_id
from app.core.settings import Settings
from app.main import API_PREFIX, create_app

pytestmark = pytest.mark.integration

EXPECTED_CSP = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: blob:; "
    "font-src 'self'; connect-src 'self'; manifest-src 'self'; worker-src 'self'; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'self'; object-src 'none'; "
    "require-trusted-types-for 'script'"
)
BEARER = {"Authorization": "Bearer synthetic-token"}
PROXY = ("10.0.0.2", 40000)


def _test_router() -> tuple[APIRouter, APIRouter]:
    api = APIRouter()
    root = APIRouter()

    @api.get("/cacheable")
    def cacheable(response: Response) -> dict[str, str]:
        response.headers["Cache-Control"] = "public, max-age=600"
        response.headers["X-Frame-Options"] = "ALLOWALL"
        return {"ok": "yes"}

    @api.get("/download")
    def download() -> Response:
        return Response(
            b"%PDF-1.7 synthetic",
            headers=attachment_headers("statement.pdf", "application/pdf"),
        )

    @api.get("/whoami")
    def whoami(request: Request) -> dict[str, str | None]:
        return {
            "ip": client_ip(request),
            "scheme": request.url.scheme,
            "request_id": request_id(request),
        }

    @root.get("/assets/app-1234.js")
    def asset() -> Response:
        return Response(b"console.log(1)", media_type="text/javascript")

    return api, root


AppFactory = Callable[..., FastAPI]


@pytest.fixture
def make_app(settings: Settings) -> AppFactory:
    def factory(**overrides: object) -> FastAPI:
        app = create_app(settings.model_copy(update=overrides))
        api, root = _test_router()
        app.include_router(api, prefix=API_PREFIX)
        app.include_router(root)
        return app

    return factory


@pytest.fixture
def app_client(make_app: AppFactory) -> Iterator[TestClient]:
    with TestClient(make_app()) as client:
        yield client


def _assert_security_headers(headers: object, *, hsts: bool) -> None:
    from collections.abc import Mapping

    assert isinstance(headers, Mapping)
    assert headers["content-security-policy"] == EXPECTED_CSP
    assert headers["x-frame-options"] == "DENY"
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["referrer-policy"] == "no-referrer"
    assert headers["permissions-policy"] == PERMISSIONS_POLICY
    assert headers["cross-origin-opener-policy"] == "same-origin"
    assert headers["cross-origin-resource-policy"] == "same-origin"
    if hsts:
        assert headers["strict-transport-security"] == HSTS_VALUE
    else:
        assert "strict-transport-security" not in headers
    assert "server" not in headers


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        ("GET", "/healthz", 200),
        ("GET", f"{API_PREFIX}/cacheable", 200),
        ("GET", f"{API_PREFIX}/does-not-exist", 404),
        ("POST", f"{API_PREFIX}/cacheable", 403),  # CSRF rejection
        ("GET", "/not-an-api-path", 404),
    ],
)
def test_every_response_has_security_headers(
    app_client: TestClient, method: str, path: str, status: int
) -> None:
    response = app_client.request(method, path)
    assert response.status_code == status
    _assert_security_headers(response.headers, hsts=False)  # test settings are dev mode
    assert response.headers["cache-control"] == "no-store"


def test_hsts_outside_dev_mode(make_app: AppFactory) -> None:
    with TestClient(make_app(dev_mode=False)) as client:
        response = client.get("/healthz")
    _assert_security_headers(response.headers, hsts=True)


def test_trusted_types_can_be_disabled(make_app: AppFactory) -> None:
    with TestClient(make_app(csp_trusted_types=False)) as client:
        csp = client.get("/healthz").headers["content-security-policy"]
    assert "require-trusted-types-for" not in csp
    assert csp == build_csp(trusted_types=False)
    assert "frame-ancestors 'none'" in csp
    assert "'unsafe-inline'" not in csp
    assert "'unsafe-eval'" not in csp


def test_csp_has_no_unsafe_sources() -> None:
    csp = build_csp()
    assert csp == EXPECTED_CSP
    for forbidden in ("'unsafe-inline'", "'unsafe-eval'", "*", "http:", "https:"):
        assert forbidden not in csp


def test_api_responses_are_never_cacheable(app_client: TestClient) -> None:
    response = app_client.get(f"{API_PREFIX}/cacheable")
    assert response.headers["cache-control"] == "no-store"
    # A handler cannot weaken clickjacking protection either.
    assert response.headers["x-frame-options"] == "DENY"


def test_static_assets_get_long_cache(app_client: TestClient) -> None:
    response = app_client.get("/assets/app-1234.js")
    assert response.status_code == 200
    assert response.headers["cache-control"] == STATIC_CACHE_CONTROL
    assert response.headers["x-content-type-options"] == "nosniff"
    missing = app_client.get("/assets/missing.js")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"


def test_download_helper_keeps_sandbox_csp(app_client: TestClient) -> None:
    response = app_client.get(f"{API_PREFIX}/download", headers=BEARER)
    assert response.status_code == 200
    assert response.headers["content-security-policy"] == DOWNLOAD_CSP
    assert response.headers["content-security-policy"].startswith("sandbox")
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"].startswith("attachment;")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    ("filename", "ascii_name"),
    [
        ("statement.pdf", "statement.pdf"),
        ('evil"; filename=x.html', "evil__ filename=x.html"),
        ("../../etc/passwd", "_.._etc_passwd"),
        ("résumé\r\nSet-Cookie: x=1.pdf", "resume__Set-Cookie_ x=1.pdf"),
        ("\u0000", "_"),
        ("", "download"),
    ],
)
def test_attachment_headers_sanitise_filename(filename: str, ascii_name: str) -> None:
    headers = attachment_headers(filename, "application/pdf")
    disposition = headers["Content-Disposition"]
    assert disposition.startswith(f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''")
    assert "\r" not in disposition
    assert "\n" not in disposition
    assert disposition.count('"') == 2


@pytest.mark.parametrize("media_type", ["text/html; charset=utf-8", "", "image/svg+xml\r\nX: y"])
def test_attachment_headers_reject_odd_media_types(media_type: str) -> None:
    assert attachment_headers("a.bin", media_type)["Content-Type"] == "application/octet-stream"


def test_attachment_headers_keep_sniffed_type() -> None:
    headers = attachment_headers("a.png", "IMAGE/PNG")
    assert headers["Content-Type"] == "image/png"
    assert headers["Content-Security-Policy"] == DOWNLOAD_CSP


# ---- request id ---------------------------------------------------------------


def test_request_id_is_generated_and_unique(app_client: TestClient) -> None:
    first = app_client.get(f"{API_PREFIX}/whoami", headers={"X-Request-ID": "client-chosen-id-1"})
    second = app_client.get(f"{API_PREFIX}/whoami")
    rid1 = first.headers["x-request-id"]
    assert rid1 != "client-chosen-id-1"  # untrusted peer: header ignored
    assert first.json()["request_id"] == rid1
    assert valid_request_id(rid1)
    assert rid1 != second.headers["x-request-id"]


def test_request_id_from_trusted_proxy(make_app: AppFactory) -> None:
    app = make_app(trusted_proxies=[ip_network("10.0.0.0/8")])
    with TestClient(app, client=PROXY) as client:
        good = client.get(f"{API_PREFIX}/whoami", headers={"X-Request-ID": "proxy-req-0001"})
        bad = client.get(f"{API_PREFIX}/whoami", headers={"X-Request-ID": "bad id\twith spaces"})
        short = client.get(f"{API_PREFIX}/whoami", headers={"X-Request-ID": "abc"})
    assert good.headers["x-request-id"] == "proxy-req-0001"
    assert bad.headers["x-request-id"] != "bad id\twith spaces"
    assert valid_request_id(bad.headers["x-request-id"])
    assert short.headers["x-request-id"] != "abc"


# ---- trusted proxies --------------------------------------------------------------


def test_forwarded_headers_ignored_without_trusted_proxies(make_app: AppFactory) -> None:
    with TestClient(make_app(), client=PROXY) as client:
        body = client.get(
            f"{API_PREFIX}/whoami",
            headers={"X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"},
        ).json()
    assert body["ip"] == "10.0.0.2"
    assert body["scheme"] == "http"


def test_forwarded_headers_ignored_from_untrusted_peer(make_app: AppFactory) -> None:
    app = make_app(trusted_proxies=[ip_network("10.0.0.0/8")])
    with TestClient(app, client=("198.51.100.7", 5555)) as client:
        body = client.get(
            f"{API_PREFIX}/whoami",
            headers={"X-Forwarded-For": "127.0.0.1", "X-Forwarded-Proto": "https"},
        ).json()
    assert body["ip"] == "198.51.100.7"
    assert body["scheme"] == "http"


def test_forwarded_headers_honoured_from_trusted_proxy(make_app: AppFactory) -> None:
    app = make_app(trusted_proxies=[ip_network("10.0.0.0/8")])
    with TestClient(app, client=PROXY) as client:
        body = client.get(
            f"{API_PREFIX}/whoami",
            # Leftmost entry is attacker-controlled; the proxy appended the real peer.
            headers={
                "X-Forwarded-For": "127.0.0.1, 203.0.113.9, 10.1.2.3",
                "X-Forwarded-Proto": "https",
            },
        ).json()
    assert body["ip"] == "203.0.113.9"
    assert body["scheme"] == "https"


def test_forwarded_proto_must_be_http_or_https(make_app: AppFactory) -> None:
    app = make_app(trusted_proxies=[ip_network("10.0.0.0/8")])
    with TestClient(app, client=PROXY) as client:
        body = client.get(
            f"{API_PREFIX}/whoami", headers={"X-Forwarded-Proto": "javascript"}
        ).json()
    assert body["scheme"] == "http"


def test_metrics_are_not_served_by_the_api(app_client: TestClient) -> None:
    for path in ("/metrics", f"{API_PREFIX}/metrics"):
        assert app_client.get(path).status_code == 404
