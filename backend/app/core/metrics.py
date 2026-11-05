# SPDX-License-Identifier: AGPL-3.0-or-later
"""Prometheus metrics (SECURITY.md H7).

Metrics live in a dedicated :data:`REGISTRY` and are served **only** by a
separate plain HTTP server (:func:`start_metrics_server`) on
``PERFIN_METRICS_PORT``. The API application has no ``/metrics`` route, so
the reverse proxy (which forwards only to the API port) can never expose
them. The server binds ``PERFIN_METRICS_BIND_HOST``, which defaults to
``127.0.0.1`` everywhere, including the container (independently of
``PERFIN_BIND_HOST``, which is ``0.0.0.0`` there for the API port): the
proxy and the mcp container share the ``web`` network with the API and must
not reach it. No service publishes the metrics port. An operator who scrapes
with Prometheus sets it to the container's ``backend``-network address.

HTTP metrics are labelled with the **route template** (``/api/v1/accounts/
{account_id}``), never the raw path, so ids do not leak into labels and
cardinality stays bounded. Requests that match no route share the label
``<unmatched>``.
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING
from wsgiref.simple_server import WSGIServer

from prometheus_client import (
    GC_COLLECTOR,
    PLATFORM_COLLECTOR,
    PROCESS_COLLECTOR,
    CollectorRegistry,
    Counter,
    Histogram,
    start_http_server,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.request_context import route_template

if TYPE_CHECKING:
    from app.core.settings import Settings

REGISTRY = CollectorRegistry(auto_describe=True)
for _collector in (PROCESS_COLLECTOR, PLATFORM_COLLECTOR, GC_COLLECTOR):
    REGISTRY.register(_collector)

UNMATCHED_ROUTE = "<unmatched>"
_KNOWN_METHODS = frozenset({"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"})

HTTP_REQUESTS = Counter(
    "perfin_http_requests",
    "HTTP requests handled by the API, by route template and status code.",
    ("method", "route", "status"),
    registry=REGISTRY,
)
HTTP_LATENCY = Histogram(
    "perfin_http_request_duration_seconds",
    "HTTP request latency by route template.",
    ("method", "route"),
    registry=REGISTRY,
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
)


def route_label(scope: Scope) -> str:
    """The matched route's full path template, or :data:`UNMATCHED_ROUTE`."""
    return route_template(scope) or UNMATCHED_ROUTE


def _method_label(method: str) -> str:
    upper = method.upper()
    return upper if upper in _KNOWN_METHODS else "OTHER"


class MetricsMiddleware:
    """Count requests and observe latency per (method, route template, status)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            method = _method_label(scope.get("method", ""))
            route = route_label(scope)
            HTTP_REQUESTS.labels(method, route, str(status)).inc()
            HTTP_LATENCY.labels(method, route).observe(time.perf_counter() - start)


def start_metrics_server(
    settings: Settings,
    *,
    port: int | None = None,
    registry: CollectorRegistry = REGISTRY,
) -> tuple[WSGIServer, threading.Thread]:
    """Serve ``registry`` on ``PERFIN_METRICS_PORT`` in a daemon thread.

    Called by the ``api`` and ``worker`` entrypoints, never by the API app.
    """
    return start_http_server(
        settings.metrics_port if port is None else port,
        addr=settings.metrics_bind_host,
        registry=registry,
    )
