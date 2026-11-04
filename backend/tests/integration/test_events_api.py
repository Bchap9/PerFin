# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /events`` (Server-Sent Events) on the real app and database (F3, H8).

Streams in these tests end on their own after ``sse_max_stream_seconds``
(the ``TestClient`` reads a response to its end); events are emitted and
committed from another thread while a stream is open, through
``app.core.events.emit`` exactly as the services do.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, update

from app.auth.models import GrantLevel, UserSession
from app.core.clock import utcnow
from app.core.events import emit
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.events.stream import StreamSlots, uuid7_floor
from tests.fixtures.documents import make_document
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, problem_type
from tests.fixtures.ledger import make_account

pytestmark = pytest.mark.integration

EVENTS = f"{API}/events"
SECRET = "Synthetic Secret Memo 4242"


def sse_settings(settings: Settings, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "sse_max_stream_seconds": 1.5,
        "sse_heartbeat_seconds": 0.2,
        "sse_revalidate_seconds": 0.25,
    }
    values.update(overrides)
    return settings.model_copy(update=values)


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(sse_settings(settings), factories) as opened:
        yield opened


def hh_of(user: AuthedClient) -> uuid.UUID:
    household_id = user.record.household_id
    assert household_id is not None
    return household_id


def emit_now(
    factories: Factories, household_id: uuid.UUID, type_: str, payload: dict[str, Any]
) -> uuid.UUID:
    with factories.transaction() as db:
        set_context(db, household_id)
        return emit(db, household_id, type_, payload)


def while_streaming(
    actors: Actors, user: AuthedClient, action: Callable[[], None], *, settle: float = 0.3
) -> threading.Thread:
    """Run ``action`` once ``user``'s stream is open (not after a fixed delay).

    The stream holds a slot in the app's :class:`StreamSlots` from before it
    picks its start point; waiting for that slot, then ``settle`` seconds,
    keeps the test independent of how long the request takes to get there on
    a loaded machine (an event emitted before the start point is, correctly,
    never delivered to a fresh stream).
    """

    def run() -> None:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            slots = getattr(actors.app.state, "sse_slots", None)
            if isinstance(slots, StreamSlots) and slots.count(user.user.id) > 0:
                break
            time.sleep(0.01)
        time.sleep(settle)
        action()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def messages(body: str) -> list[dict[str, Any]]:
    """Parsed SSE messages: ``{"id"?, "data"?, "comment"?, "retry"?}`` in order."""
    out: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        if not block.strip():
            continue
        message: dict[str, Any] = {}
        for line in block.split("\n"):
            if line.startswith(":"):
                message["comment"] = line[1:].strip()
                continue
            name, _, value = line.partition(": ")
            message[name] = json.loads(value) if name == "data" else value
        out.append(message)
    return out


def data_of(body: str) -> list[dict[str, Any]]:
    return [m["data"] for m in messages(body) if "data" in m]


def test_stream_is_household_scoped_and_carries_ids_and_types_only(
    actors: Actors, factories: Factories
) -> None:
    alice = actors.new(role="owner")
    bob = actors.new(role="owner")
    home, other = hh_of(alice), hh_of(bob)
    sent: dict[str, uuid.UUID] = {}

    def produce() -> None:
        sent["other"] = emit_now(factories, other, "transaction.created", {"memo": SECRET})
        sent["mine"] = emit_now(
            factories, home, "transaction.created", {"id": str(uuid.uuid4()), "memo": SECRET}
        )

    thread = while_streaming(actors, alice, produce)
    response = alice.get(EVENTS)
    thread.join()
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-store"
    assert SECRET not in response.text
    assert data_of(response.text) == [{"id": str(sent["mine"]), "type": "transaction.created"}]
    ids = [m["id"] for m in messages(response.text) if "id" in m]
    assert ids == [str(sent["mine"])]
    assert any("retry" in m for m in messages(response.text))


def test_members_only_hear_about_what_they_can_see(
    actors: Actors, factories: Factories, settings: Settings, tmp_path: Any
) -> None:
    owner = actors.new(role="owner")
    member = actors.new(role="member", household=owner.record.household)
    hh = hh_of(owner)
    store = LocalFSBlobStore(tmp_path / "documents", tmp_path / "quarantine")
    with factories.transaction() as db:
        granted = make_account(db, hh)
        hidden = make_account(db, hh)
        hidden_doc = make_document(db, store, factories.keyring, hh, account_id=hidden.id)
        visible_doc = make_document(db, store, factories.keyring, hh, account_id=granted.id)
    factories.grant(hh, member.user.id, granted.id, GrantLevel.VIEW)
    sent: dict[str, uuid.UUID] = {}

    def produce() -> None:
        for name, type_, payload in (
            ("hidden", "account.updated", {"account_id": str(hidden.id)}),
            ("granted", "account.updated", {"account_id": str(granted.id)}),
            (
                "mixed",
                "transaction.bulk_updated",
                {"account_ids": [str(hidden.id), str(granted.id)]},
            ),
            ("hidden_only", "transaction.bulk_updated", {"account_ids": [str(hidden.id)]}),
            (
                "addressed",
                "account_grant.changed",
                {"user_id": str(member.user.id), "account_id": str(hidden.id)},
            ),
            ("hidden_doc", "document.updated", {"id": str(hidden_doc.id), "status": "processed"}),
            ("visible_doc", "document.updated", {"id": str(visible_doc.id), "status": "processed"}),
            (
                "review_hidden",
                "review.resolved",
                {"id": str(uuid.uuid4()), "document_id": str(hidden_doc.id)},
            ),
            ("household", "category.changed", {"id": str(uuid.uuid4())}),
        ):
            sent[name] = emit_now(factories, hh, type_, payload)

    thread = while_streaming(actors, member, produce)
    as_member = member.get(EVENTS)
    thread.join()
    delivered = [d["id"] for d in data_of(as_member.text)]
    expected = ["granted", "mixed", "addressed", "visible_doc", "household"]
    assert delivered == [str(sent[name]) for name in expected]

    # The owner sees everything (a fresh stream starts at "now": resume instead).
    first = str(sent["hidden"])
    as_owner = owner.get(EVENTS, headers={"Last-Event-ID": first})
    owner_ids = [d["id"] for d in data_of(as_owner.text)]
    assert owner_ids == [str(v) for k, v in sent.items() if k != "hidden"]


def test_last_event_id_resumes_after_that_event(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    hh = hh_of(owner)
    ids = [
        emit_now(factories, hh, "budget.updated", {"budget_id": str(uuid.uuid4())})
        for _ in range(3)
    ]
    response = owner.get(EVENTS, headers={"Last-Event-ID": str(ids[0])})
    assert [d["id"] for d in data_of(response.text)] == [str(ids[1]), str(ids[2])]
    by_query = owner.get(EVENTS, params={"last_event_id": str(ids[1])})
    assert [d["id"] for d in data_of(by_query.text)] == [str(ids[2])]
    fresh = owner.get(EVENTS)
    assert data_of(fresh.text) == []


@pytest.mark.parametrize(
    "value", ["not-a-uuid", pytest.param(str(uuid.uuid4()), id="unknown-uuid"), "expired"]
)
def test_unusable_last_event_id_asks_for_a_resync(
    actors: Actors, factories: Factories, value: str
) -> None:
    owner = actors.new(role="owner")
    if value == "expired":
        value = str(uuid7_floor(utcnow() - timedelta(days=8)))
    emit_now(factories, hh_of(owner), "budget.updated", {})
    response = owner.get(EVENTS, headers={"Last-Event-ID": value})
    assert response.status_code == 200
    assert data_of(response.text) == [{"type": "stream.resync"}]


def test_heartbeats_keep_an_idle_stream_open(actors: Actors) -> None:
    owner = actors.new(role="owner")
    started = time.monotonic()
    response = owner.get(EVENTS)
    elapsed = time.monotonic() - started
    comments = [m for m in messages(response.text) if m.get("comment") == "keep-alive"]
    assert len(comments) >= 3
    assert 1.4 <= elapsed < 5


def test_tokens_are_refused(actors: Actors, factories: Factories) -> None:
    owner = actors.new(role="owner")
    token = factories.token(owner.record, scopes=("documents:read",))
    response = actors.anonymous().get(EVENTS, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert problem_type(response) == "session-required"
    assert actors.anonymous().get(EVENTS).status_code == 401


def test_a_revoked_session_ends_the_stream(
    factories: Factories, settings: Settings, admin_engine: Engine
) -> None:
    with Actors.open(sse_settings(settings, sse_max_stream_seconds=10), factories) as slow:
        owner = slow.new(role="owner")

        def revoke() -> None:
            with admin_engine.begin() as conn:
                conn.execute(
                    update(UserSession)
                    .where(UserSession.id == owner.login.session_id)
                    .values(revoked_at=utcnow(), revoked_reason="test")
                )

        thread = while_streaming(slow, owner, revoke)
        started = time.monotonic()
        response = owner.get(EVENTS)
        thread.join()
        assert response.status_code == 200, response.text
        assert time.monotonic() - started < 5
        assert data_of(response.text)[-1] == {"type": "session.ended"}


def test_streams_per_user_are_capped(factories: Factories, settings: Settings) -> None:
    with Actors.open(
        sse_settings(settings, sse_max_streams_per_user=1, sse_max_stream_seconds=1.2), factories
    ) as capped:
        owner = capped.new(role="owner")
        second = capped.login(owner.record)
        results: dict[str, Any] = {}

        def first_stream() -> None:
            results["first"] = owner.get(EVENTS)

        thread = threading.Thread(target=first_stream, daemon=True)
        thread.start()
        time.sleep(0.4)
        refused = second.get(EVENTS)
        thread.join()
        assert results["first"].status_code == 200
        assert refused.status_code == 429
        assert problem_type(refused) == "too-many-streams"
        assert refused.headers["retry-after"] == "10"
        # The slot is released when the stream ends.
        assert second.get(EVENTS).status_code == 200
