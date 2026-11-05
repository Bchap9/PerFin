# SPDX-License-Identifier: AGPL-3.0-or-later
"""A write is committed before its response goes out (read-your-writes).

``DbSession`` (``app.auth.deps``) commits when the request's transaction
ends. With FastAPI's default ``"request"`` scope for dependencies with
``yield`` that happens *after* the response has been sent, so a client that
got ``201`` could read the old state on its next request (found in the
Phases 5+6 live run: ``POST /accounts`` followed at once by
``GET /accounts/{id}`` failed once in 300 tries, and a session rotated by
``POST /auth/step-up`` was once not found by the next request), and a failed
commit would reach the client as a success. The dependency is
``scope="function"``: the commit runs before the response is sent.

The probe below is ASGI middleware that, when the last byte of the
``POST /accounts`` response is sent, looks for the new row from another
connection.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, MutableMapping
from contextlib import ExitStack
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app.core.settings import Settings
from app.main import create_app
from tests.fixtures.factories import Factories
from tests.fixtures.households import API, Actors

pytestmark = pytest.mark.integration

Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]


class CommitProbe:
    """ASGI middleware: on the final response body, run ``check`` and record it."""

    def __init__(self, app: Callable[[Message, Receive, Send], Awaitable[None]]) -> None:
        self.app = app
        self.check: Callable[[], bool] | None = None
        self.seen: list[bool] = []

    async def __call__(self, scope: Message, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") != "POST":
            await self.app(scope, receive, send)
            return

        async def probing_send(message: Message) -> None:
            last = message["type"] == "http.response.body" and not message.get("more_body")
            if last and self.check is not None:
                self.seen.append(self.check())
            await send(message)

        await self.app(scope, receive, probing_send)


def test_a_write_is_committed_before_its_response_is_sent(
    settings: Settings, factories: Factories, admin_engine: Engine
) -> None:
    app = create_app(settings)
    probes: list[CommitProbe] = []

    def factory(inner: Any) -> CommitProbe:
        probe = CommitProbe(inner)
        probes.append(probe)
        return probe

    # Sees the final body message after every inner layer has run.
    app.add_middleware(factory)
    name = f"commit-probe-{uuid.uuid4().hex[:12]}"

    def committed() -> bool:
        with admin_engine.connect() as conn:
            found = conn.execute(
                text("SELECT count(*) FROM account WHERE name = :name"), {"name": name}
            ).scalar_one()
        return bool(found)

    with ExitStack() as stack:
        actors = Actors(app, factories, stack)
        owner = actors.new(role="owner", username=f"own{uuid.uuid4().hex[:10]}")
        (probe,) = probes
        probe.check = committed
        response = owner.post(
            f"{API}/accounts", json={"type": "checking", "name": name, "currency": "USD"}
        )
        assert response.status_code == 201, response.text
        probe.check = None
    assert probe.seen == [True]
