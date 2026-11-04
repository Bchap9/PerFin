# SPDX-License-Identifier: AGPL-3.0-or-later
"""Application-layer request body limits (SECURITY.md F8, C2)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi import APIRouter, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from starlette.types import Message, Receive, Scope, Send

from app.core.limits import DEFAULT_MAX_BODY_BYTES, BodySizeLimitMiddleware
from app.core.settings import Settings
from app.main import API_PREFIX, UPLOAD_PATHS, create_app

pytestmark = pytest.mark.integration

BEARER = {"Authorization": "Bearer synthetic-token"}


class Note(BaseModel):
    text: str


@pytest.fixture
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings.model_copy(update={"upload_max_mb": 2}))
    router = APIRouter()

    @router.post("/notes")
    def notes(note: Note) -> dict[str, int]:
        return {"length": len(note.text)}

    @router.post("/raw")
    async def raw(request: Request) -> dict[str, int]:
        return {"length": len(await request.body())}

    @router.post("/documents")
    async def upload(request: Request) -> dict[str, int]:
        total = 0
        async for chunk in request.stream():
            total += len(chunk)
        return {"length": total}

    # The real POST /documents (authenticated, multipart) is shadowed by the
    # counting stub above (routes match in order) so only the body cap of
    # that exact path is measured.
    before = len(app.router.routes)
    app.include_router(router, prefix=API_PREFIX)
    added = app.router.routes[before:]
    del app.router.routes[before:]
    app.router.routes[:0] = added
    with TestClient(app, headers=BEARER) as test_client:
        yield test_client


def _assert_413(response: object) -> None:
    from typing import Any

    r: Any = response
    assert r.status_code == 413
    assert r.headers["content-type"] == "application/problem+json"
    assert r.json()["type"] == "urn:perfin:problem:body-too-large"
    assert r.headers["x-content-type-options"] == "nosniff"  # still behind the header layer


def test_small_bodies_pass(client: TestClient) -> None:
    response = client.post(f"{API_PREFIX}/notes", json={"text": "x" * 1000})
    assert response.json() == {"length": 1000}


def test_declared_oversized_body_rejected(client: TestClient) -> None:
    _assert_413(client.post(f"{API_PREFIX}/raw", content=b"x" * (DEFAULT_MAX_BODY_BYTES + 1)))


def test_json_body_over_limit_rejected(client: TestClient) -> None:
    _assert_413(client.post(f"{API_PREFIX}/notes", json={"text": "x" * DEFAULT_MAX_BODY_BYTES}))


def test_body_exactly_at_limit_accepted(client: TestClient) -> None:
    response = client.post(f"{API_PREFIX}/raw", content=b"x" * DEFAULT_MAX_BODY_BYTES)
    assert response.json() == {"length": DEFAULT_MAX_BODY_BYTES}


def _chunks(total: int, size: int = 64 * 1024) -> Iterator[bytes]:
    sent = 0
    while sent < total:
        n = min(size, total - sent)
        sent += n
        yield b"y" * n


def test_streamed_body_without_length_is_counted(client: TestClient) -> None:
    # A chunked body has no Content-Length; the cap must still hold.
    _assert_413(client.post(f"{API_PREFIX}/raw", content=_chunks(DEFAULT_MAX_BODY_BYTES + 10)))
    _assert_413(client.post(f"{API_PREFIX}/notes", content=_chunks(DEFAULT_MAX_BODY_BYTES * 2)))


def test_upload_route_uses_upload_limit(client: TestClient) -> None:
    assert f"{API_PREFIX}/documents" in UPLOAD_PATHS
    big = 2 * 1024 * 1024  # PERFIN_UPLOAD_MAX_MB=2 in this fixture
    ok = client.post(f"{API_PREFIX}/documents", content=b"z" * big)
    assert ok.json() == {"length": big}
    _assert_413(client.post(f"{API_PREFIX}/documents", content=b"z" * (big + 1)))
    _assert_413(client.post(f"{API_PREFIX}/documents", content=_chunks(big + 1)))


@pytest.mark.parametrize("value", [b"-5", b"abc", b"1e3"])
def test_invalid_content_length_rejected(value: bytes) -> None:
    called = False

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        nonlocal called
        called = True

    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/raw",
        "headers": [(b"content-length", value)],
    }
    asyncio.run(BodySizeLimitMiddleware(inner)(scope, receive, send))
    assert not called
    assert sent[0]["status"] == 400


def test_limit_for_normalises_trailing_slash() -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:  # pragma: no cover
        return None

    middleware = BodySizeLimitMiddleware(app, default_limit=10, overrides={"/upload": 99})
    assert middleware.limit_for("/upload/") == 99
    assert middleware.limit_for("/other") == 10
    with pytest.raises(ValueError, match=">= 0"):
        BodySizeLimitMiddleware(app, default_limit=-1)
