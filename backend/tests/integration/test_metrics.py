# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prometheus metrics: route-template labels and the internal-only server (H7)."""

from __future__ import annotations

from collections.abc import Iterator

import httpx
import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry, Counter

from app.core.metrics import HTTP_REQUESTS, REGISTRY, start_metrics_server
from app.core.settings import Settings
from app.main import API_PREFIX, create_app

pytestmark = pytest.mark.integration


def _count(method: str, route: str, status: str) -> float:
    value = REGISTRY.get_sample_value(
        "perfin_http_requests_total", {"method": method, "route": route, "status": status}
    )
    return value or 0.0


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    router = APIRouter(prefix="/metrics-test")

    @router.get("/accounts/{account_id}")
    def account(account_id: int) -> dict[str, int]:
        return {"id": account_id}

    app.include_router(router, prefix=API_PREFIX)
    with TestClient(app) as test_client:
        yield test_client


def test_requests_are_labelled_by_route_template(client: TestClient) -> None:
    template = f"{API_PREFIX}/metrics-test/accounts/{{account_id}}"
    before = _count("GET", template, "200")
    for account_id in (101, 202, 303):
        assert client.get(f"{API_PREFIX}/metrics-test/accounts/{account_id}").status_code == 200
    assert _count("GET", template, "200") == before + 3
    # Raw ids never become label values.
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            assert "/accounts/101" not in sample.labels.get("route", "")
    latency_count = REGISTRY.get_sample_value(
        "perfin_http_request_duration_seconds_count", {"method": "GET", "route": template}
    )
    assert latency_count is not None
    assert latency_count >= 3


def test_unmatched_and_odd_methods_have_bounded_labels(client: TestClient) -> None:
    before = _count("GET", "<unmatched>", "404")
    client.get(f"{API_PREFIX}/no/such/path/12345")
    client.get("/also-not-here")
    assert _count("GET", "<unmatched>", "404") == before + 2
    before_other = _count("OTHER", "<unmatched>", "404")
    client.request("PROPFIND", "/x")
    assert _count("OTHER", "<unmatched>", "404") == before_other + 1


def test_root_routes_are_labelled(client: TestClient) -> None:
    before = _count("GET", "/healthz", "200")
    client.get("/healthz")
    assert _count("GET", "/healthz", "200") == before + 1


def test_metrics_server_is_separate_from_the_api(settings: Settings) -> None:
    local = settings.model_copy(update={"metrics_bind_host": "127.0.0.1"})
    server, thread = start_metrics_server(local, port=0)
    try:
        port = server.server_port
        HTTP_REQUESTS.labels("GET", "/synthetic", "200").inc(0)
        response = httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5)
        assert response.status_code == 200
        assert "perfin_http_requests_total" in response.text
        assert "process_" in response.text or "python_" in response.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_metrics_server_serves_only_the_given_registry(settings: Settings) -> None:
    registry = CollectorRegistry()
    Counter("perfin_synthetic_only", "synthetic", registry=registry).inc()
    local = settings.model_copy(update={"metrics_bind_host": "127.0.0.1"})
    server, thread = start_metrics_server(local, port=0, registry=registry)
    try:
        text = httpx.get(f"http://127.0.0.1:{server.server_port}/metrics", timeout=5).text
        assert "perfin_synthetic_only_total 1.0" in text
        assert "perfin_http_requests_total" not in text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_metrics_bind_loopback_even_when_the_api_binds_everything(settings: Settings) -> None:
    """Review finding SR-9: the container's PERFIN_BIND_HOST=0.0.0.0 is for the
    API port only; metrics stay on loopback unless configured otherwise."""
    container_like = settings.model_copy(update={"bind_host": "0.0.0.0"})  # noqa: S104 - the point
    assert Settings().metrics_bind_host == "127.0.0.1"
    assert container_like.metrics_bind_host == "127.0.0.1"
    server, thread = start_metrics_server(container_like, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
