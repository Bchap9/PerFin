# SPDX-License-Identifier: AGPL-3.0-or-later
"""events.emit: row + NOTIFY in the same transaction; pruning under RLS."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta

import psycopg
import pytest
from sqlalchemy import URL, Engine, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import utcnow
from app.core.events import EVENTS_CHANNEL, emit, parse_notice, prune_events
from app.db.models import Event, Household
from app.db.rls import get_context, set_context
from app.db.session import make_sessionmaker
from tests.conftest import TestDatabase

pytestmark = pytest.mark.integration

#: A payload value that cannot occur in the notice's ids or type (not hex).
PAYLOAD_MARKER = "payload-only-marker"


def _dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


@pytest.fixture
def factory(app_engine: Engine) -> sessionmaker[Session]:
    return make_sessionmaker(app_engine)


@pytest.fixture
def household_ids(
    factory: sessionmaker[Session], admin_engine: Engine
) -> Iterator[tuple[uuid.UUID, uuid.UUID]]:
    with factory() as session, session.begin():
        # ``household`` is under RLS: an instance admin creates households.
        set_context(session, None, instance_admin=True)
        a = Household(name="Synthetic Events A")
        b = Household(name="Synthetic Events B")
        session.add_all([a, b])
    try:
        yield a.id, b.id
    finally:
        with admin_engine.begin() as conn:
            conn.execute(text("DELETE FROM household WHERE id IN (:a, :b)"), {"a": a.id, "b": b.id})


@pytest.fixture
def listener(test_database: TestDatabase) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    """A LISTEN connection as the application role (as the SSE endpoint will be)."""
    conn = psycopg.connect(_dsn(test_database.app_url), autocommit=True)
    try:
        conn.execute(f"LISTEN {EVENTS_CHANNEL}")
        yield conn
    finally:
        conn.close()


def _received(
    conn: psycopg.Connection[tuple[object, ...]], timeout: float, stop_after: int | None = None
) -> list[str]:
    notifies = conn.notifies(timeout=timeout, stop_after=stop_after)
    return [n.payload for n in notifies if n.channel == EVENTS_CHANNEL]


def test_emit_inserts_row_and_notifies_on_commit(
    factory: sessionmaker[Session],
    household_ids: tuple[uuid.UUID, uuid.UUID],
    listener: psycopg.Connection[tuple[object, ...]],
) -> None:
    household, _ = household_ids
    with factory() as session, session.begin():
        set_context(session, household)
        event_id = emit(session, household, "document.processed", {"document_id": PAYLOAD_MARKER})
        # Not delivered before commit.
        assert _received(listener, timeout=0.2) == []
    payloads = _received(listener, timeout=5.0, stop_after=1)
    assert len(payloads) == 1
    notice = parse_notice(payloads[0])
    assert notice is not None
    assert notice.id == event_id
    assert notice.household_id == household
    assert notice.type == "document.processed"
    assert PAYLOAD_MARKER not in payloads[0]  # payload stays in the row, not the NOTIFY

    with factory() as session, session.begin():
        set_context(session, household)
        row = session.execute(select(Event).where(Event.id == event_id)).scalar_one()
        assert row.payload == {"document_id": PAYLOAD_MARKER}
        assert row.type == "document.processed"


def test_rolled_back_emit_is_neither_stored_nor_notified(
    factory: sessionmaker[Session],
    household_ids: tuple[uuid.UUID, uuid.UUID],
    listener: psycopg.Connection[tuple[object, ...]],
) -> None:
    household, _ = household_ids
    with factory() as session:
        session.begin()
        set_context(session, household)
        event_id = emit(session, household, "test.rolled_back")
        session.rollback()
    assert _received(listener, timeout=0.5) == []
    with factory() as session, session.begin():
        set_context(session, household)
        assert session.get(Event, event_id) is None


def test_emit_requires_matching_household_context(
    factory: sessionmaker[Session], household_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = household_ids
    with factory() as session, session.begin():
        set_context(session, a)
        with pytest.raises(DBAPIError, match="row-level security"):
            emit(session, b, "test.forged")
    with (
        factory() as session,
        session.begin(),
        pytest.raises(DBAPIError, match="row-level security"),
    ):
        emit(session, a, "test.no_context")


def test_events_invisible_to_other_household(
    factory: sessionmaker[Session], household_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = household_ids
    with factory() as session, session.begin():
        set_context(session, a)
        emit(session, a, "test.private")
    with factory() as session, session.begin():
        set_context(session, b)
        count = session.execute(
            select(func.count()).select_from(Event).where(Event.household_id == a)
        ).scalar_one()
        assert count == 0


def test_prune_events_per_household_and_restores_context(
    factory: sessionmaker[Session], household_ids: tuple[uuid.UUID, uuid.UUID]
) -> None:
    a, b = household_ids
    for household in (a, a, b):
        with factory() as session, session.begin():
            set_context(session, household)
            emit(session, household, "test.prunable")

    def remaining(household: uuid.UUID) -> int:
        with factory() as session, session.begin():
            set_context(session, household)
            return session.execute(
                select(func.count()).select_from(Event).where(Event.household_id == household)
            ).scalar_one()

    with factory() as session, session.begin():
        # Default retention keeps fresh events.
        assert prune_events(session, household_ids=[a, b]) == 0
    assert (remaining(a), remaining(b)) == (2, 1)

    caller = uuid.uuid4()
    with factory() as session, session.begin():
        set_context(session, a, caller)
        removed = prune_events(
            session, older_than=timedelta(0), household_ids=[b], now=utcnow() + timedelta(hours=1)
        )
        assert removed == 1
        ctx = get_context(session)
        assert (ctx.household_id, ctx.user_id, ctx.instance_admin) == (a, caller, False)
    assert (remaining(a), remaining(b)) == (2, 0)

    with factory() as session, session.begin():
        # Default: every household, each in its own RLS context.
        removed = prune_events(session, older_than=timedelta(0), now=utcnow() + timedelta(hours=1))
        assert removed >= 2
        assert get_context(session).household_id is None
    assert remaining(a) == 0
