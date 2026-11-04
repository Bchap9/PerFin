# SPDX-License-Identifier: AGPL-3.0-or-later
"""The API serves the built SPA under the strict CSP (SECURITY.md F1, F9; app.core.spa)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.settings import Settings
from app.main import create_app

pytestmark = pytest.mark.integration

INDEX = (
    "<!doctype html><html><head>"
    '<script type="module" src="/static/index-abc123.js"></script>'
    "</head><body><div id=root></div></body></html>"
)


@pytest.fixture
def static_dir(tmp_path: Path) -> Path:
    build = tmp_path / "dist"
    (build / "static").mkdir(parents=True)
    (build / "index.html").write_text(INDEX, encoding="utf-8")
    (build / "static" / "index-abc123.js").write_text("console.log(1);\n", encoding="utf-8")
    (build / "robots.txt").write_text("User-agent: *\nDisallow: /\n", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("outside the build\n", encoding="utf-8")
    (build / "static" / "link.js").symlink_to(tmp_path / "secret.txt")
    return build


@pytest.fixture
def spa_client(settings: Settings, static_dir: Path) -> Iterator[TestClient]:
    configured = settings.model_copy(update={"static_dir": static_dir})
    with TestClient(create_app(configured)) as client:
        yield client


@pytest.mark.parametrize(
    "path",
    [
        "/",
        "/accounts",
        "/settings/security",
        "/login",
        # The Assets pages (Phase 5) are client routes: the hashed bundles live
        # under /static/, not Vite's default /assets/ (found in the live run,
        # where a reload of /assets answered a problem 404).
        "/assets",
        "/assets/01a1a665-0dda-7633-b51c-5c46ea8eb3d1",
    ],
)
def test_client_routes_get_index_with_strict_headers(spa_client: TestClient, path: str) -> None:
    response = spa_client.get(path)
    assert response.status_code == 200
    assert response.text == INDEX
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    csp = response.headers["content-security-policy"]
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "require-trusted-types-for 'script'" in csp
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-frame-options"] == "DENY"


def test_hashed_assets_are_long_cached(spa_client: TestClient) -> None:
    response = spa_client.get("/static/index-abc123.js")
    assert response.status_code == 200
    assert "immutable" in response.headers["cache-control"]
    assert response.headers["x-content-type-options"] == "nosniff"


def test_missing_asset_is_404_not_index_and_not_cached(spa_client: TestClient) -> None:
    response = spa_client.get("/static/nope.js")
    assert response.status_code == 404
    assert response.text != INDEX
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "path",
    ["/static/link.js", "/static/../../secret.txt", "/static/%2e%2e/%2e%2e/secret.txt"],
)
def test_no_symlinks_or_traversal(spa_client: TestClient, path: str) -> None:
    response = spa_client.get(path)
    assert "outside the build" not in response.text
    assert response.status_code == 404 or response.text == INDEX


def test_root_files_by_exact_name(spa_client: TestClient) -> None:
    response = spa_client.get("/robots.txt")
    assert response.status_code == 200
    assert response.text.startswith("User-agent")


def test_pwa_files_are_served_from_the_root_under_the_strict_csp(
    settings: Settings, static_dir: Path
) -> None:
    """Manifest, service worker and icons live at the root (the worker's scope is ``/``)."""
    (static_dir / "manifest.webmanifest").write_text('{"name": "PerFin"}', encoding="utf-8")
    (static_dir / "sw.js").write_text("self.addEventListener('fetch', () => {});", encoding="utf-8")
    (static_dir / "icon-192.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    configured = settings.model_copy(update={"static_dir": static_dir})
    with TestClient(create_app(configured)) as client:
        manifest = client.get("/manifest.webmanifest")
        worker = client.get("/sw.js")
        icon = client.get("/icon-192.png")
        csp = manifest.headers["content-security-policy"]
    assert manifest.status_code == worker.status_code == icon.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert worker.headers["content-type"].split(";")[0] in {
        "text/javascript",
        "application/javascript",
    }
    assert icon.headers["content-type"] == "image/png"
    # The worker script is never cached by the browser's HTTP cache, so an update ships at once.
    assert worker.headers["cache-control"] == "no-store"
    assert "manifest-src 'self'" in csp
    assert "worker-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "'unsafe-inline'" not in csp


def test_api_paths_keep_problem_404(spa_client: TestClient) -> None:
    response = spa_client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 404


def test_wrong_method_on_real_route_is_still_405(spa_client: TestClient) -> None:
    response = spa_client.post("/healthz")
    assert response.status_code == 405
    assert "GET" in response.headers["allow"]


def test_unsafe_method_on_unknown_path_is_not_index(spa_client: TestClient) -> None:
    response = spa_client.delete("/accounts", headers={"Origin": "http://testserver"})
    assert response.status_code == 404
    assert response.text != INDEX


def test_probes_are_not_shadowed(spa_client: TestClient) -> None:
    assert spa_client.get("/healthz").json() == {"status": "ok"}


def test_without_a_build_nothing_is_served(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/problem+json")
