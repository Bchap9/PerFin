# SPDX-License-Identifier: AGPL-3.0-or-later
"""Postgres job queue and worker (ADR-0002) against a real database.

Every test uses its own queue name and handler registry so tests (and other
modules' tests) never pick up each other's jobs.
"""

from __future__ import annotations

import dataclasses
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
from psycopg import sql
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    model_serializer,
    model_validator,
)
from sqlalchemy import CursorResult, Engine, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlalchemy.orm import Session, sessionmaker

from app.core.settings import Settings
from app.db.models import JobState
from app.db.queue import (
    LEASE_EXPIRED_ERROR,
    MAX_PAYLOAD_BYTES,
    NOTIFY_CHANNEL,
    ClaimedJob,
    DedupeKeyConflictError,
    PayloadTooLargeError,
    dequeue,
    enqueue,
    job_states,
    mark_failed,
    mark_succeeded,
    prune_finished,
    reap_expired_leases,
    requeue_dead,
)
from app.db.rls import get_context, queue_scope, set_context
from app.db.session import make_engine, make_sessionmaker
from app.workers.handlers.noop import NoopPayload
from app.workers.registry import (
    HandlerRegistry,
    JobContext,
    PermanentJobError,
    discover_handlers,
)
from app.workers.runner import EXIT_OK, EXIT_STUCK, UNKNOWN_TYPE_ERROR, Worker, WorkerConfig
from app.workers.scheduler import Schedule
from tests.conftest import TestDatabase

pytestmark = pytest.mark.integration

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Item(BaseModel):
    model_config = ConfigDict(extra="forbid")
    n: int = Field(ge=0, le=1_000_000)
    blob: str = ""


class Other(BaseModel):
    n: int = 0


# --------------------------------------------------------------------------- helpers


@pytest.fixture
def factory(app_engine: Engine) -> sessionmaker[Session]:
    return make_sessionmaker(app_engine)


@pytest.fixture
def queue_name() -> str:
    return f"t_{uuid.uuid4().hex[:16]}"


@pytest.fixture
def registry() -> HandlerRegistry:
    return HandlerRegistry()


def _worker(
    engine: Engine,
    registry: HandlerRegistry,
    queue: str,
    **overrides: Any,
) -> Worker:
    config = WorkerConfig(
        queues=(queue,),
        listen=overrides.pop("listen", False),
        poll_interval=overrides.pop("poll_interval", 0.05),
        **overrides,
    )
    return Worker(engine, config=config, registry=registry, schedule=Schedule())


def _job(session: Session, job_id: uuid.UUID) -> Any:
    # ``job`` is under RLS; inspection runs as the queue machinery does.
    with queue_scope(session):
        return session.execute(
            text(
                "SELECT id, state, attempts, max_attempts, last_error, locked_by, locked_at, "
                "lease_expires_at, run_after, payload, household_id, priority, queue, "
                "run_after - now() AS delay FROM job WHERE id = :id"
            ),
            {"id": job_id},
        ).one()


def _state(factory: sessionmaker[Session], job_id: uuid.UUID) -> Any:
    with factory() as session, session.begin():
        return _job(session, job_id)


def _make_runnable(factory: sessionmaker[Session], job_id: uuid.UUID) -> None:
    with factory() as session, session.begin(), queue_scope(session):
        session.execute(text("UPDATE job SET run_after = now() WHERE id = :id"), {"id": job_id})


def _expire_lease(session: Session, job_id: uuid.UUID) -> None:
    with queue_scope(session):
        session.execute(
            text("UPDATE job SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
            {"id": job_id},
        )


def _household(factory: sessionmaker[Session], name: str = "Synthetic") -> uuid.UUID:
    household_id = uuid.uuid4()
    with factory() as session, session.begin():
        # ``household`` is under RLS: a new household is written in its own context.
        set_context(session, household_id)
        session.execute(
            text("INSERT INTO household (id, name) VALUES (:id, :name)"),
            {"id": household_id, "name": name},
        )
    return household_id


def _enqueue(
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue: str,
    job_type: str,
    payload: BaseModel,
    **kwargs: Any,
) -> uuid.UUID:
    with factory() as session, session.begin():
        # A household job is enqueued in that household's context (RLS).
        set_context(session, kwargs.get("household_id"))
        return enqueue(session, job_type, payload, queue=queue, registry=registry, **kwargs)


def _lease(_job_type: str) -> float:
    return 60.0


def _listen(test_database: TestDatabase) -> psycopg.Connection[tuple[Any, ...]]:
    dsn = test_database.app_url.set(drivername="postgresql").render_as_string(hide_password=False)
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(NOTIFY_CHANNEL)))
    return conn


# --------------------------------------------------------------------------- enqueue


def test_enqueue_commits_with_the_caller_and_notifies(
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
    test_database: TestDatabase,
) -> None:
    registry.register("item.process", lambda _c, _p: None, payload=Item, max_attempts=7)
    household_id = _household(factory)
    with _listen(test_database) as listener:
        run_at = datetime.now(UTC) + timedelta(minutes=5)
        job_id = _enqueue(
            factory,
            registry,
            queue_name,
            "item.process",
            Item(n=3),
            household_id=household_id,
            priority=-1,
            run_after=run_at,
        )
        notes = list(listener.notifies(timeout=5.0, stop_after=1))
    assert [n.payload for n in notes] == [queue_name]
    row = _state(factory, job_id)
    assert row.state == "queued"
    assert row.payload == {"n": 3, "blob": ""}
    assert row.attempts == 0
    assert row.max_attempts == 7  # the handler's policy
    assert row.household_id == household_id
    assert row.priority == -1
    assert row.locked_by is None
    assert abs((row.run_after - run_at).total_seconds()) < 0.001


def test_rolled_back_business_write_leaves_no_job_and_no_notification(
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
    test_database: TestDatabase,
) -> None:
    registry.register("item.process", lambda _c, _p: None, payload=Item)
    household_id = uuid.uuid4()
    job_id: uuid.UUID | None = None
    with _listen(test_database) as listener:
        with factory() as session:
            session.begin()
            set_context(session, household_id)
            session.execute(
                text("INSERT INTO household (id, name) VALUES (:id, 'Rolled back')"),
                {"id": household_id},
            )
            job_id = enqueue(
                session,
                "item.process",
                Item(n=1),
                queue=queue_name,
                household_id=household_id,
                registry=registry,
            )
            session.rollback()
        notes = list(listener.notifies(timeout=0.5))
    assert notes == []
    with factory() as session, session.begin(), queue_scope(session):
        set_context(session, None, instance_admin=True)
        assert (
            session.execute(
                text("SELECT count(*) FROM job WHERE id = :id OR queue = :q"),
                {"id": job_id, "q": queue_name},
            ).scalar_one()
            == 0
        )
        assert (
            session.execute(
                text("SELECT count(*) FROM household WHERE id = :id"), {"id": household_id}
            ).scalar_one()
            == 0
        )


def test_enqueue_validates_payload(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    registry.register("item.process", lambda _c, _p: None, payload=Item)
    with factory() as session, session.begin():
        # A registered type demands its declared model.
        with pytest.raises(TypeError, match="must be Item"):
            enqueue(session, "item.process", Other(n=1), queue=queue_name, registry=registry)
        # model_construct skips validation; enqueue re-validates.
        with pytest.raises(ValidationError):
            enqueue(
                session,
                "item.process",
                Item.model_construct(n=-5, blob=""),
                queue=queue_name,
                registry=registry,
            )
        # Unregistered types are validated through their own model.
        with pytest.raises(ValidationError):
            enqueue(
                session,
                "not.registered",
                Item.model_construct(n=-5, blob=""),
                queue=queue_name,
                registry=registry,
            )
        with pytest.raises(PayloadTooLargeError):
            enqueue(
                session,
                "item.process",
                Item(n=1, blob="x" * (MAX_PAYLOAD_BYTES + 1)),
                queue=queue_name,
                registry=registry,
            )
        with pytest.raises(TypeError):
            enqueue(session, "item.process", {"n": 1}, queue=queue_name, registry=registry)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="invalid job type"):
            enqueue(session, "Bad Type", Item(n=1), queue=queue_name, registry=registry)
        with pytest.raises(ValueError, match="timezone-aware"):
            enqueue(
                session,
                "item.process",
                Item(n=1),
                queue=queue_name,
                registry=registry,
                run_after=datetime(2030, 1, 1),  # naive on purpose
            )
        with pytest.raises(ValueError, match="max_attempts"):
            enqueue(
                session,
                "item.process",
                Item(n=1),
                queue=queue_name,
                registry=registry,
                max_attempts=0,
            )
        with queue_scope(session):
            assert (
                session.execute(
                    text("SELECT count(*) FROM job WHERE queue = :q"), {"q": queue_name}
                ).scalar_one()
                == 0
            )


def test_dedupe_key_makes_enqueue_idempotent(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    key = f"dedupe:{queue_name}"
    first = _enqueue(factory, registry, queue_name, "item.process", Item(n=1), dedupe_key=key)
    second = _enqueue(factory, registry, queue_name, "item.process", Item(n=2), dedupe_key=key)
    assert first == second
    with factory() as session, session.begin(), queue_scope(session):
        assert (
            session.execute(
                text("SELECT count(*) FROM job WHERE queue = :q"), {"q": queue_name}
            ).scalar_one()
            == 1
        )


# --------------------------------------------------------------------------- dequeue


def test_dequeue_order_priority_then_run_after(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    now = datetime.now(UTC)
    late = _enqueue(
        factory, registry, queue_name, "x", Item(n=1), run_after=now - timedelta(seconds=1)
    )
    early = _enqueue(
        factory, registry, queue_name, "x", Item(n=2), run_after=now - timedelta(seconds=9)
    )
    urgent = _enqueue(factory, registry, queue_name, "x", Item(n=3), priority=-10)
    future = _enqueue(
        factory, registry, queue_name, "x", Item(n=4), run_after=now + timedelta(hours=1)
    )
    order: list[uuid.UUID] = []
    for _ in range(4):
        with factory() as session, session.begin():
            claimed = dequeue(session, worker_id="w", queues=[queue_name], lease_seconds=_lease)
            if claimed is not None:
                order.append(claimed.id)
    assert order == [urgent, early, late]
    assert _state(factory, future).state == "queued"


def test_claim_sets_lease_and_attempts(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    job_id = _enqueue(factory, registry, queue_name, "x", Item(n=1))
    with factory() as session, session.begin():
        claimed = dequeue(
            session, worker_id="host:1:abc", queues=[queue_name], lease_seconds=lambda _t: 42.0
        )
    assert claimed is not None
    assert claimed.id == job_id
    assert claimed.attempts == 1
    assert claimed.payload == {"n": 1, "blob": ""}
    row = _state(factory, job_id)
    assert row.state == "running"
    assert row.locked_by == "host:1:abc"
    lease = (row.lease_expires_at - row.locked_at).total_seconds()
    assert lease == pytest.approx(42.0, abs=0.01)


def test_concurrent_dequeue_skips_locked_rows(
    settings: Settings, registry: HandlerRegistry, queue_name: str
) -> None:
    """Two open transactions on separate connections never claim the same job."""
    engines = [make_engine(settings.database_url) for _ in range(2)]
    try:
        factory = make_sessionmaker(engines[0])
        ids = {_enqueue(factory, registry, queue_name, "x", Item(n=i)) for i in range(2)}
        first_claimed = threading.Event()
        release_first = threading.Event()
        claims: dict[str, ClaimedJob | None] = {}

        def first() -> None:
            with make_sessionmaker(engines[0])() as session, session.begin():
                claims["a"] = dequeue(
                    session, worker_id="a", queues=[queue_name], lease_seconds=_lease
                )
                first_claimed.set()
                release_first.wait(10)  # keep the row lock while B dequeues

        def second() -> None:
            first_claimed.wait(10)
            with make_sessionmaker(engines[1])() as session, session.begin():
                claims["b"] = dequeue(
                    session, worker_id="b", queues=[queue_name], lease_seconds=_lease
                )
            release_first.set()

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        a, b = claims["a"], claims["b"]
        assert a is not None
        assert b is not None
        assert a.id != b.id
        assert {a.id, b.id} == ids
    finally:
        for engine in engines:
            engine.dispose()


def test_two_workers_never_double_process(
    settings: Settings, registry: HandlerRegistry, queue_name: str
) -> None:
    """Two workers, each with its own engine (connections), drain 60 jobs exactly once."""
    seen: Counter[int] = Counter()
    seen_lock = threading.Lock()
    by_worker: Counter[str] = Counter()

    def handle(ctx: JobContext, payload: Item) -> None:
        with seen_lock:
            seen[payload.n] += 1
            by_worker[threading.current_thread().name.split("-job-")[0]] += 1
        ctx.session.execute(text("SELECT pg_sleep(0.005)"))

    registry.register("item.process", handle, payload=Item)
    engines = [make_engine(settings.database_url) for _ in range(2)]
    try:
        factory = make_sessionmaker(engines[0])
        ids = [
            _enqueue(factory, registry, queue_name, "item.process", Item(n=i)) for i in range(60)
        ]
        workers = [_worker(engine, registry, queue_name) for engine in engines]
        start = threading.Barrier(2)
        results: Counter[str] = Counter()

        def drain(worker: Worker) -> None:
            start.wait(10)
            while worker.run_once():
                results[worker.worker_id] += 1

        threads = [threading.Thread(target=drain, args=(w,)) for w in workers]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)

        assert sorted(seen) == list(range(60))
        assert set(seen.values()) == {1}, "a job was processed twice"
        assert sum(results.values()) == 60
        assert all(results[w.worker_id] > 0 for w in workers), "both workers should take jobs"
        with factory() as session, session.begin(), queue_scope(session):
            states = session.execute(
                text("SELECT state, count(*) FROM job WHERE id = ANY(:ids) GROUP BY state"),
                {"ids": ids},
            ).all()
        assert [(s, c) for s, c in states] == [("succeeded", 60)]
    finally:
        for engine in engines:
            engine.dispose()


def test_household_concurrency_limit(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    house = _household(factory)
    other_house = _household(factory)
    busy = [
        _enqueue(factory, registry, queue_name, "x", Item(n=i), household_id=house, priority=-5)
        for i in range(3)
    ]
    other = _enqueue(factory, registry, queue_name, "x", Item(n=9), household_id=other_house)
    instance = _enqueue(factory, registry, queue_name, "x", Item(n=10))

    claimed: list[uuid.UUID] = []
    for _ in range(5):
        with factory() as session, session.begin():
            job = dequeue(
                session,
                worker_id="w",
                queues=[queue_name],
                lease_seconds=_lease,
                household_limit=2,
            )
        if job is not None:
            claimed.append(job.id)
    # Two of the busy household's jobs run; its third waits; others are not blocked.
    assert claimed[:2] == busy[:2]
    assert set(claimed[2:]) == {other, instance}
    assert _state(factory, busy[2]).state == "queued"

    # Once one finishes, the waiting one becomes eligible.
    with factory() as session, session.begin(), queue_scope(session):
        session.execute(
            text(
                "UPDATE job SET state = 'succeeded', locked_by = NULL, lease_expires_at = NULL "
                "WHERE id = :id"
            ),
            {"id": busy[0]},
        )
    with factory() as session, session.begin():
        job = dequeue(
            session, worker_id="w", queues=[queue_name], lease_seconds=_lease, household_limit=2
        )
    assert job is not None
    assert job.id == busy[2]


def test_household_limit_holds_under_concurrent_dequeue(
    settings: Settings, registry: HandlerRegistry, queue_name: str
) -> None:
    """Many threads racing for one household's jobs never exceed the limit."""
    engine = make_engine(settings.database_url, pool_size=10)
    try:
        factory = make_sessionmaker(engine)
        house = _household(factory)
        for i in range(10):
            _enqueue(factory, registry, queue_name, "x", Item(n=i), household_id=house)
        barrier = threading.Barrier(8)
        got: list[uuid.UUID] = []
        lock = threading.Lock()

        def grab() -> None:
            barrier.wait(10)
            with factory() as session, session.begin():
                job = dequeue(
                    session,
                    worker_id=f"w{threading.get_ident()}",
                    queues=[queue_name],
                    lease_seconds=_lease,
                    household_limit=3,
                )
            if job is not None:
                with lock:
                    got.append(job.id)

        threads = [threading.Thread(target=grab) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        with factory() as session, session.begin(), queue_scope(session):
            running = session.execute(
                text("SELECT count(*) FROM job WHERE household_id = :h AND state = 'running'"),
                {"h": house},
            ).scalar_one()
        assert running == len(got)
        assert 1 <= running <= 3
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- execution


def test_worker_runs_handler_in_household_rls_context(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    contexts: list[tuple[uuid.UUID | None, uuid.UUID | None, str]] = []

    def handle(ctx: JobContext, payload: Item) -> None:
        del payload
        rls = get_context(ctx.session)
        timeout = ctx.session.execute(text("SHOW statement_timeout")).scalar_one()
        contexts.append((ctx.household_id, rls.household_id, str(timeout)))

    registry.register("ctx.check", handle, payload=Item, timeout=7)
    house = _household(factory)
    household_job = _enqueue(
        factory, registry, queue_name, "ctx.check", Item(n=1), household_id=house
    )
    instance_job = _enqueue(factory, registry, queue_name, "ctx.check", Item(n=2))
    worker = _worker(app_engine, registry, queue_name)
    assert worker.run_once()
    assert worker.run_once()
    assert not worker.run_once()
    assert contexts == [(house, house, "7s"), (None, None, "7s")]
    assert _state(factory, household_job).state == "succeeded"
    assert _state(factory, instance_job).state == "succeeded"


def test_handler_writes_commit_atomically_with_success(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    created: dict[int, uuid.UUID] = {}

    def handle(ctx: JobContext, payload: Item) -> None:
        household_id = uuid.uuid4()
        created[payload.n] = household_id
        set_context(ctx.session, household_id)
        ctx.session.execute(
            text("INSERT INTO household (id, name) VALUES (:id, 'From job')"),
            {"id": household_id},
        )
        if payload.n == 1:
            raise RuntimeError("synthetic failure after a write")

    registry.register("writer", handle, payload=Item)
    ok = _enqueue(factory, registry, queue_name, "writer", Item(n=0))
    bad = _enqueue(factory, registry, queue_name, "writer", Item(n=1))
    worker = _worker(app_engine, registry, queue_name)
    assert worker.run_once()
    assert worker.run_once()
    with factory() as session, session.begin():
        set_context(session, None, instance_admin=True)
        present = session.execute(
            text("SELECT id FROM household WHERE id = ANY(:ids)"), {"ids": list(created.values())}
        ).scalars()
        assert set(present) == {created[0]}
    assert _state(factory, ok).state == "succeeded"
    assert _state(factory, bad).state == "queued"


def test_retry_with_backoff_then_dead_letter(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    calls: list[int] = []

    def flaky(ctx: JobContext, payload: Item) -> None:
        del payload
        calls.append(ctx.attempt)
        raise RuntimeError(f"synthetic failure on attempt {ctx.attempt}")

    registry.register("flaky", flaky, payload=Item, max_attempts=3)
    job_id = _enqueue(factory, registry, queue_name, "flaky", Item(n=1))
    worker = _worker(app_engine, registry, queue_name)

    assert worker.run_once()
    row = _state(factory, job_id)
    assert row.state == "queued"
    assert row.attempts == 1
    assert row.locked_by is None
    assert row.lease_expires_at is None
    assert "RuntimeError: synthetic failure on attempt 1" in row.last_error
    # Attempt 1 backs off between 2.5 s and 5 s (base 5 s, equal jitter).
    assert 2.0 <= row.delay.total_seconds() <= 5.0
    assert not worker.run_once(), "not runnable before run_after"

    _make_runnable(factory, job_id)
    assert worker.run_once()
    row = _state(factory, job_id)
    assert row.state == "queued"
    assert row.attempts == 2
    assert 4.5 <= row.delay.total_seconds() <= 10.0  # attempt 2: 5-10 s

    _make_runnable(factory, job_id)
    assert worker.run_once()
    row = _state(factory, job_id)
    assert row.state == "dead"
    assert row.attempts == 3
    assert "attempt 3" in row.last_error
    assert calls == [1, 2, 3]
    assert not worker.run_once()

    # Operator requeue gives it a fresh set of attempts.
    with factory() as session, session.begin():
        assert requeue_dead(session, job_id)
    row = _state(factory, job_id)
    assert (row.state, row.attempts, row.last_error) == ("queued", 0, None)


def test_permanent_error_dead_letters_immediately(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    def broken(_ctx: JobContext, _payload: Item) -> None:
        raise PermanentJobError("synthetic: input can never be processed")

    registry.register("broken", broken, payload=Item, max_attempts=5)
    job_id = _enqueue(factory, registry, queue_name, "broken", Item(n=1))
    assert _worker(app_engine, registry, queue_name).run_once()
    row = _state(factory, job_id)
    assert (row.state, row.attempts) == ("dead", 1)
    assert row.last_error.startswith("PermanentJobError: ")


def test_unknown_type_is_dead_lettered(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    job_id = _enqueue(factory, registry, queue_name, "nobody.handles.this", Item(n=1))
    assert _worker(app_engine, registry, queue_name).run_once()
    row = _state(factory, job_id)
    assert row.state == "dead"
    assert row.last_error == UNKNOWN_TYPE_ERROR


def test_invalid_stored_payload_is_dead_lettered_without_echoing_it(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    ran: list[int] = []
    registry.register("item.process", lambda _c, p: ran.append(p.n), payload=Item)
    job_id = uuid.uuid4()
    with factory() as session, session.begin(), queue_scope(session):
        # Bypass enqueue(): simulate a row written by an older version or tampered with.
        session.execute(
            text(
                "INSERT INTO job (id, queue, type, payload) "
                "VALUES (:id, :q, 'item.process', CAST(:payload AS jsonb))"
            ),
            {"id": job_id, "q": queue_name, "payload": '{"n": "SYNTH-SECRET-777", "x": 1}'},
        )
    assert _worker(app_engine, registry, queue_name).run_once()
    row = _state(factory, job_id)
    assert row.state == "dead"
    assert row.last_error.startswith("payload validation failed: ")
    assert "n: int_parsing" in row.last_error
    assert "x: extra_forbidden" in row.last_error
    assert "SYNTH-SECRET-777" not in row.last_error
    assert ran == []


# --------------------------------------------------------------------------- SR-2


class ItemList(RootModel[list[int]]):
    pass


class SerialisesToList(BaseModel):
    """Round-trips through a JSON list: validation alone would not stop it."""

    n: int = 1

    @model_validator(mode="before")
    @classmethod
    def _from_list(cls, data: Any) -> Any:
        return {"n": data[0]} if isinstance(data, list) else data

    @model_serializer
    def _as_list(self) -> list[int]:
        return [self.n]


def test_non_object_payloads_are_refused_at_enqueue_and_registration(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    """Review finding SR-2: a payload must be a JSON object."""
    with pytest.raises(TypeError, match="RootModel"):
        registry.register("item.list", lambda _c, _p: None, payload=ItemList)
    with factory() as session, session.begin():
        for job_type in ("item.list", "not.registered"):
            with pytest.raises(TypeError, match="RootModel"):
                enqueue(session, job_type, ItemList([1, 2]), queue=queue_name, registry=registry)
        # A custom serializer cannot smuggle a non-object past enqueue either.
        with pytest.raises(TypeError, match="JSON object"):
            enqueue(
                session, "not.registered", SerialisesToList(), queue=queue_name, registry=registry
            )
        with queue_scope(session):
            assert (
                session.execute(
                    text("SELECT count(*) FROM job WHERE queue = :q"), {"q": queue_name}
                ).scalar_one()
                == 0
            )


@pytest.mark.parametrize("payload", ["[1, 2]", "7", '"text"', "null", "true"])
def test_database_refuses_non_object_payload(
    factory: sessionmaker[Session], queue_name: str, payload: str
) -> None:
    with (
        factory() as session,
        session.begin(),
        queue_scope(session),
        pytest.raises(IntegrityError, match="payload_is_object"),
    ):
        session.execute(
            text(
                "INSERT INTO job (id, queue, type, payload) "
                "VALUES (:id, :q, 'item.process', CAST(:payload AS jsonb))"
            ),
            {"id": uuid.uuid4(), "q": queue_name, "payload": payload},
        )


def test_non_object_payload_is_dead_lettered_and_does_not_wedge_the_queue(
    app_engine: Engine,
    owner_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    """Review finding SR-2: legacy rows whose payload is not an object.

    Before the fix ``dequeue`` coerced the payload with ``dict()``: the claim
    raised, rolled back, and the job stayed queued at the head of its queue
    forever, blocking every later job. Such rows can only predate the check
    constraint, so the test lifts it (as the table owner) for its duration.
    """
    ran: list[int] = []
    registry.register("item.process", lambda _c, p: ran.append(p.n), payload=Item)
    bad_payloads = ["[1, 2]", "7", "null"]
    bad_ids = [uuid.uuid4() for _ in bad_payloads]
    with owner_engine.begin() as conn:
        conn.execute(text("ALTER TABLE job DROP CONSTRAINT ck_job_payload_is_object"))
    try:
        with factory() as session, session.begin(), queue_scope(session):
            for job_id, payload in zip(bad_ids, bad_payloads, strict=True):
                session.execute(
                    text(
                        "INSERT INTO job (id, queue, type, payload, priority) "
                        "VALUES (:id, :q, 'item.process', CAST(:payload AS jsonb), -10)"
                    ),
                    {"id": job_id, "q": queue_name, "payload": payload},
                )
        good = _enqueue(factory, registry, queue_name, "item.process", Item(n=42))
        worker = _worker(app_engine, registry, queue_name)
        for _ in range(len(bad_ids) + 1):
            assert worker.run_once()
        assert not worker.run_once()
        for job_id in bad_ids:
            row = _state(factory, job_id)
            assert row.state == "dead", row
            assert row.attempts == 1
            assert row.last_error.startswith("payload validation failed: ")
        assert _state(factory, good).state == "succeeded"
        assert ran == [42]
    finally:
        with owner_engine.begin() as conn, queue_scope(conn):
            conn.execute(text("DELETE FROM job WHERE id = ANY(:ids)"), {"ids": bad_ids})
            conn.execute(
                text(
                    "ALTER TABLE job ADD CONSTRAINT ck_job_payload_is_object "
                    "CHECK (jsonb_typeof(payload) = 'object')"
                )
            )


def test_dequeue_passes_the_stored_payload_through_unchanged(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    """The runner, not the claim, judges the payload (dead-letter on mismatch)."""
    registry.register("item.process", lambda _c, _p: None, payload=Item)
    job_id = _enqueue(factory, registry, queue_name, "item.process", Item(n=1))
    with factory() as session, session.begin():
        claimed = dequeue(session, worker_id="w", queues=[queue_name], lease_seconds=_lease)
    assert claimed is not None
    assert claimed.payload == {"n": 1, "blob": ""}
    worker = _worker(app_engine, registry, queue_name)
    assert worker.execute(dataclasses.replace(claimed, payload=[1, 2])) is JobState.DEAD
    assert _state(factory, job_id).state == "dead"


# --------------------------------------------------------------------------- SR-4


def test_jobs_follow_the_household_context(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    """Review finding SR-4: a household context reaches only its own jobs."""
    a = _household(factory, "Synthetic Jobs A")
    b = _household(factory, "Synthetic Jobs B")
    job_a = _enqueue(factory, registry, queue_name, "x", Item(n=1), household_id=a)
    job_b = _enqueue(factory, registry, queue_name, "x", Item(n=2), household_id=b)
    instance = _enqueue(factory, registry, queue_name, "x", Item(n=3))
    for context in (b, None):
        with factory() as session, session.begin():
            set_context(session, context)
            visible = set(
                session.execute(
                    text("SELECT id FROM job WHERE queue = :q"), {"q": queue_name}
                ).scalars()
            )
            assert visible == ({job_b} if context == b else set())
            for statement in (
                "UPDATE job SET state = 'dead' WHERE id = :id",
                "UPDATE job SET run_after = now() + interval '1 day' WHERE id = :id",
                "DELETE FROM job WHERE id = :id",
            ):
                for target in (job_a, instance):
                    result = cast(
                        CursorResult[Any], session.execute(text(statement), {"id": target})
                    )
                    assert result.rowcount == 0, (statement, context)
            assert job_states(session, [job_a, job_b, instance]) == (
                {job_b: JobState.QUEUED} if context == b else {}
            )
    assert _state(factory, job_a).state == "queued"
    assert _state(factory, instance).state == "queued"
    # A household cannot enqueue for another one, or write an instance-level
    # job outside the queue machinery.
    with factory() as session, session.begin():
        set_context(session, b)
        with pytest.raises(ProgrammingError, match="row-level security"), session.begin_nested():
            enqueue(session, "x", Item(n=4), queue=queue_name, household_id=a, registry=registry)
        with pytest.raises(ProgrammingError, match="row-level security"), session.begin_nested():
            session.execute(
                text("INSERT INTO job (id, queue, type) VALUES (:id, :q, 'x')"),
                {"id": uuid.uuid4(), "q": queue_name},
            )
    # An instance admin sees instance-level jobs only.
    with factory() as session, session.begin():
        set_context(session, None, instance_admin=True)
        assert job_states(session, [job_a, job_b, instance]) == {instance: JobState.QUEUED}


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("household_id", "CAST(:other AS uuid)"),
        ("type", "'other.type'"),
        ("payload", "CAST('{\"n\": 999}' AS jsonb)"),
        ("queue", "'elsewhere'"),
        ("dedupe_key", "'stolen'"),
    ],
)
def test_job_identity_is_immutable(
    factory: sessionmaker[Session],
    owner_engine: Engine,
    registry: HandlerRegistry,
    queue_name: str,
    column: str,
    value: str,
) -> None:
    """Review finding SR-4: no role can retarget a job or change what it runs."""
    a = _household(factory, "Synthetic Immutable A")
    other = _household(factory, "Synthetic Immutable B")
    job_id = _enqueue(factory, registry, queue_name, "x", Item(n=1), household_id=a)
    statement = text(f"UPDATE job SET {column} = {value} WHERE id = :id")  # noqa: S608 - fixed test SQL
    params = {"id": job_id, "other": other}
    immutable = "job identity columns are immutable"
    with factory() as session, session.begin():
        set_context(session, a)
        with pytest.raises(ProgrammingError, match=immutable):
            session.execute(statement, params)
    with (
        factory() as session,
        session.begin(),
        queue_scope(session),
        pytest.raises(ProgrammingError, match=immutable),
    ):
        session.execute(statement, params)
    # Even the table owner (forced RLS: it needs the queue flag to see the row).
    with (
        owner_engine.begin() as conn,
        queue_scope(conn),
        pytest.raises(ProgrammingError, match=immutable),
    ):
        conn.execute(statement, params)
    row = _state(factory, job_id)
    assert (row.household_id, row.payload, row.queue) == (a, {"n": 1, "blob": ""}, queue_name)


def test_dedupe_key_of_another_household_is_not_revealed(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    a = _household(factory, "Synthetic Dedupe A")
    b = _household(factory, "Synthetic Dedupe B")
    key = f"dedupe-x:{queue_name}"
    first = _enqueue(factory, registry, queue_name, "x", Item(n=1), household_id=a, dedupe_key=key)
    again = _enqueue(factory, registry, queue_name, "x", Item(n=2), household_id=a, dedupe_key=key)
    assert again == first
    for household in (b, None):
        with pytest.raises(DedupeKeyConflictError) as excinfo:
            _enqueue(
                factory,
                registry,
                queue_name,
                "x",
                Item(n=3),
                household_id=household,
                dedupe_key=key,
            )
        assert str(first) not in str(excinfo.value)


# --------------------------------------------------------------------------- SR-10


def test_stale_claim_by_same_worker_cannot_finish_after_reclaim(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    """Review finding SR-10: the ``attempts`` guard fences a re-claimed job.

    A worker whose lease was reaped can claim the same job again under the
    same worker id; only ``attempts`` tells its stale first claim apart.
    """
    job_id = _enqueue(factory, registry, queue_name, "x", Item(n=1), max_attempts=3)
    with factory() as session, session.begin():
        first = dequeue(session, worker_id="same", queues=[queue_name], lease_seconds=_lease)
    assert first is not None
    with factory() as session, session.begin():
        _expire_lease(session, job_id)
    with factory() as session, session.begin():
        assert [r.id for r in reap_expired_leases(session) if r.id == job_id] == [job_id]
    _make_runnable(factory, job_id)
    with factory() as session, session.begin():
        second = dequeue(session, worker_id="same", queues=[queue_name], lease_seconds=_lease)
    assert second is not None
    assert (second.id, second.locked_by, second.attempts) == (job_id, "same", 2)
    with factory() as session, session.begin():
        assert not mark_succeeded(session, first)
        assert mark_failed(session, first, "stale") is None
    row = _state(factory, job_id)
    assert (row.state, row.attempts, row.last_error) == ("running", 2, LEASE_EXPIRED_ERROR)
    with factory() as session, session.begin():
        assert mark_succeeded(session, second)
    assert _state(factory, job_id).state == "succeeded"


def test_cooperative_timeout_fails_the_attempt(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    def slow(ctx: JobContext, _payload: Item) -> None:
        while True:
            ctx.check_cancelled()
            time.sleep(0.02)

    registry.register("slow", slow, payload=Item, timeout=0.3, max_attempts=2)
    job_id = _enqueue(factory, registry, queue_name, "slow", Item(n=1))
    worker = _worker(app_engine, registry, queue_name)
    started = time.monotonic()
    assert worker.run_once()
    assert time.monotonic() - started < 5
    row = _state(factory, job_id)
    assert row.state == "queued"
    assert row.last_error == "timeout: handler exceeded 0.3s"
    _make_runnable(factory, job_id)
    assert worker.run_once()
    assert _state(factory, job_id).state == "dead"


def test_stuck_handler_is_abandoned_and_its_locks_released(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    house = _household(factory, "Lock target")
    release = threading.Event()

    def stuck(ctx: JobContext, _payload: Item) -> None:
        ctx.session.execute(
            text("UPDATE household SET name = 'locked by job' WHERE id = :id"), {"id": house}
        )
        release.wait(30)  # ignores cancellation: simulates a wedged handler

    registry.register("stuck", stuck, payload=Item, timeout=0.5, max_attempts=1)
    job_id = _enqueue(factory, registry, queue_name, "stuck", Item(n=1), household_id=house)
    second = _enqueue(factory, registry, queue_name, "stuck", Item(n=2))
    worker = _worker(app_engine, registry, queue_name, max_stuck_handlers=1)
    try:
        assert worker.run_once()
        row = _state(factory, job_id)
        assert row.state == "dead"
        assert row.last_error.startswith("timeout")
        # The handler's backend was terminated: its row lock is gone and its
        # uncommitted write rolled back.
        with factory() as session, session.begin():
            set_context(session, house)
            session.execute(text("SET LOCAL lock_timeout = '3s'"))
            session.execute(
                text("UPDATE household SET name = 'free' WHERE id = :id"), {"id": house}
            )
        with factory() as session, session.begin():
            set_context(session, house)
            assert (
                session.execute(
                    text("SELECT name FROM household WHERE id = :id"), {"id": house}
                ).scalar_one()
                == "free"
            )
        # One stuck thread reaches max_stuck_handlers: the worker stops claiming.
        assert not worker.run_once()
        assert worker.exit_code == EXIT_STUCK
        assert worker.stop_event.is_set()
        assert _state(factory, second).state == "queued"
    finally:
        release.set()


def test_lease_lost_result_is_discarded(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    written = uuid.uuid4()

    def handle(ctx: JobContext, _payload: Item) -> None:
        set_context(ctx.session, written)
        ctx.session.execute(
            text("INSERT INTO household (id, name) VALUES (:id, 'Should vanish')"),
            {"id": written},
        )
        # Meanwhile the lease is reaped and another worker takes the job over.
        with factory() as other, other.begin(), queue_scope(other):
            other.execute(
                text(
                    "UPDATE job SET locked_by = 'someone-else', attempts = attempts + 1 "
                    "WHERE id = :id"
                ),
                {"id": ctx.job_id},
            )

    registry.register("taken", handle, payload=Item)
    job_id = _enqueue(factory, registry, queue_name, "taken", Item(n=1))
    worker = _worker(app_engine, registry, queue_name)
    assert worker.run_once()
    row = _state(factory, job_id)
    assert (row.state, row.locked_by) == ("running", "someone-else")
    with factory() as session, session.begin():
        set_context(session, None, instance_admin=True)
        assert (
            session.execute(
                text("SELECT count(*) FROM household WHERE id = :id"), {"id": written}
            ).scalar_one()
            == 0
        )


# --------------------------------------------------------------------------- reaper


def test_reaper_requeues_expired_lease_and_fences_the_old_claim(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    job_id = _enqueue(factory, registry, queue_name, "x", Item(n=1), max_attempts=2)
    with factory() as session, session.begin():
        crashed = dequeue(session, worker_id="crashed", queues=[queue_name], lease_seconds=_lease)
    assert crashed is not None
    with factory() as session, session.begin():
        assert all(r.id != job_id for r in reap_expired_leases(session))
        _expire_lease(session, job_id)
    with factory() as session, session.begin():
        reaped = [r for r in reap_expired_leases(session) if r.id == job_id]
    assert [(r.id, r.state) for r in reaped] == [(job_id, JobState.QUEUED)]
    row = _state(factory, job_id)
    assert row.state == "queued"
    assert row.locked_by is None
    assert row.lease_expires_at is None
    assert row.attempts == 1  # the crashed attempt counts
    assert row.last_error == LEASE_EXPIRED_ERROR
    assert row.delay.total_seconds() > 0  # backoff before the retry

    _make_runnable(factory, job_id)
    with factory() as session, session.begin():
        retry = dequeue(session, worker_id="healthy", queues=[queue_name], lease_seconds=_lease)
    assert retry is not None
    assert retry.attempts == 2
    # The crashed worker comes back: its stale claim can no longer finish the job.
    with factory() as session, session.begin():
        assert not mark_succeeded(session, crashed)
        assert mark_failed(session, crashed, "late") is None
    with factory() as session, session.begin():
        assert mark_succeeded(session, retry)
    assert _state(factory, job_id).state == "succeeded"


def test_reaper_dead_letters_when_attempts_exhausted(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    job_id = _enqueue(factory, registry, queue_name, "x", Item(n=1), max_attempts=1)
    with factory() as session, session.begin():
        assert dequeue(session, worker_id="gone", queues=[queue_name], lease_seconds=_lease)
        _expire_lease(session, job_id)
    worker = _worker(app_engine, registry, queue_name)
    assert worker.reap() >= 1
    row = _state(factory, job_id)
    assert (row.state, row.last_error) == ("dead", LEASE_EXPIRED_ERROR)


def test_worker_lease_is_handler_timeout_plus_grace(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    leases: list[float] = []

    def handle(ctx: JobContext, _payload: Item) -> None:
        row = _job(ctx.session, ctx.job_id)
        leases.append((row.lease_expires_at - row.locked_at).total_seconds())

    registry.register("leased", handle, payload=Item, timeout=20)
    _enqueue(factory, registry, queue_name, "leased", Item(n=1))
    assert _worker(app_engine, registry, queue_name, lease_grace=5.0).run_once()
    assert leases == [pytest.approx(25.0, abs=0.01)]


def test_prune_removes_old_finished_jobs(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    old = _enqueue(factory, registry, queue_name, "x", Item(n=1))
    recent = _enqueue(factory, registry, queue_name, "x", Item(n=2))
    waiting = _enqueue(factory, registry, queue_name, "x", Item(n=3))
    with factory() as session, session.begin(), queue_scope(session):
        session.execute(
            text(
                "UPDATE job SET state = 'succeeded', updated_at = now() - interval '8 days' "
                "WHERE id = :id"
            ),
            {"id": old},
        )
        session.execute(text("UPDATE job SET state = 'succeeded' WHERE id = :id"), {"id": recent})
        session.execute(
            text("UPDATE job SET updated_at = now() - interval '60 days' WHERE id = :id"),
            {"id": waiting},
        )
    with factory() as session, session.begin():
        assert prune_finished(session) >= 1
        with queue_scope(session):
            remaining = set(
                session.execute(text("SELECT id FROM job WHERE queue = :q"), {"q": queue_name})
                .scalars()
                .all()
            )
    assert remaining == {recent, waiting}


# --------------------------------------------------------------------------- scheduler


def test_scheduler_enqueues_each_slot_once(
    factory: sessionmaker[Session], registry: HandlerRegistry, queue_name: str
) -> None:
    registry.register("tick", lambda _c, _p: None, payload=Item)
    schedule = Schedule()
    name = f"tick_{queue_name}"
    schedule.add("tick", timedelta(minutes=5), Item(n=1), name=name, queue=queue_name)
    schedule.add("no.handler", timedelta(minutes=5), Item(n=1), name=f"x_{queue_name}")
    now = datetime(2026, 11, 3, 12, 1, tzinfo=UTC)
    with factory() as session, session.begin():
        first = schedule.tick(session, now=now, registry=registry)
    with factory() as session, session.begin():
        again = schedule.tick(session, now=now + timedelta(minutes=3), registry=registry)
    with factory() as session, session.begin():
        nxt = schedule.tick(session, now=now + timedelta(minutes=4), registry=registry)
    assert len(first) == 1
    assert again == first
    assert nxt != first
    with factory() as session, session.begin(), queue_scope(session):
        rows = session.execute(
            text("SELECT dedupe_key, run_after FROM job WHERE queue = :q ORDER BY run_after"),
            {"q": queue_name},
        ).all()
    assert [r.run_after for r in rows] == [
        datetime(2026, 11, 3, 12, 0, tzinfo=UTC),
        datetime(2026, 11, 3, 12, 5, tzinfo=UTC),
    ]
    assert all(r.dedupe_key.startswith(f"sched:{name}:300:") for r in rows)


def test_scheduler_is_safe_across_replicas(
    settings: Settings, registry: HandlerRegistry, queue_name: str
) -> None:
    registry.register("tick", lambda _c, _p: None, payload=Item)
    schedule = Schedule()
    schedule.add("tick", timedelta(hours=1), Item(n=1), name=f"r_{queue_name}", queue=queue_name)
    engine = make_engine(settings.database_url, pool_size=6)
    try:
        factory = make_sessionmaker(engine)
        barrier = threading.Barrier(6)
        ids: list[uuid.UUID] = []
        lock = threading.Lock()

        def replica() -> None:
            barrier.wait(10)
            with factory() as session, session.begin():
                result = schedule.tick(session, registry=registry)
            with lock:
                ids.extend(result)

        threads = [threading.Thread(target=replica) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        assert len(ids) == 6
        assert len(set(ids)) == 1
        with factory() as session, session.begin(), queue_scope(session):
            assert (
                session.execute(
                    text("SELECT count(*) FROM job WHERE queue = :q"), {"q": queue_name}
                ).scalar_one()
                == 1
            )
    finally:
        engine.dispose()


def test_worker_maintenance_runs_scheduler(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    ran: list[int] = []
    registry.register("tick", lambda _c, p: ran.append(p.n), payload=Item)
    schedule = Schedule()
    schedule.add("tick", timedelta(minutes=1), Item(n=5), name=f"m_{queue_name}", queue=queue_name)
    worker = Worker(
        app_engine,
        config=WorkerConfig(queues=(queue_name,), listen=False),
        registry=registry,
        schedule=schedule,
    )
    worker.maintenance(force=True)
    assert worker.run_once()
    assert ran == [5]


# --------------------------------------------------------------------------- run loop


def _wait_for(predicate: Callable[[], bool], timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return predicate()


def test_run_loop_wakes_on_notify_and_stops_gracefully(
    app_engine: Engine,
    factory: sessionmaker[Session],
    registry: HandlerRegistry,
    queue_name: str,
) -> None:
    done = threading.Event()
    started = threading.Event()

    def handle(_ctx: JobContext, payload: Item) -> None:
        if payload.n == 2:
            started.set()
            time.sleep(0.5)  # in flight when stop() is called
        done.set()

    registry.register("item.process", handle, payload=Item)
    # A 30 s poll interval: only LISTEN/NOTIFY can make this fast.
    worker = _worker(app_engine, registry, queue_name, listen=True, poll_interval=30.0)
    exit_codes: list[int] = []
    runner = threading.Thread(target=lambda: exit_codes.append(worker.run()))
    runner.start()
    try:
        time.sleep(1.0)  # let the loop go idle and the listener subscribe
        sent = time.monotonic()
        first = _enqueue(factory, registry, queue_name, "item.process", Item(n=1))
        assert done.wait(10)
        assert time.monotonic() - sent < 5, "worker did not wake on NOTIFY"
        assert _wait_for(lambda: _state(factory, first).state == "succeeded")

        second = _enqueue(factory, registry, queue_name, "item.process", Item(n=2))
        assert started.wait(10)
        worker.stop()  # graceful: the in-flight job finishes
        runner.join(20)
        assert not runner.is_alive()
        assert exit_codes == [EXIT_OK]
        assert _state(factory, second).state == "succeeded"
    finally:
        worker.stop()
        runner.join(20)


def test_loop_survives_database_errors(
    settings: Settings, registry: HandlerRegistry, queue_name: str
) -> None:
    broken = make_engine(
        settings.database_url.get_secret_value().replace("@localhost:", "@127.0.0.1:1"),
        connect_args={"connect_timeout": 1},
    )
    try:
        worker = _worker(broken, registry, queue_name)
        with pytest.raises(OperationalError):
            worker.run_once()
        runner = threading.Thread(target=worker.run)
        runner.start()
        time.sleep(0.5)
        assert runner.is_alive(), "loop must not die on a database error"
        worker.stop()
        runner.join(20)
        assert not runner.is_alive()
    finally:
        broken.dispose()


# --------------------------------------------------------------------------- CLI


@pytest.fixture
def worker_env(settings: Settings) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PERFIN_")}
    env.update(
        {
            "PERFIN_DEV_MODE": "true",
            "PERFIN_DATABASE_URL": settings.database_url.get_secret_value(),
            "PERFIN_MASTER_KEY": settings.master_key.get_secret_value()
            if settings.master_key
            else "",
            "PERFIN_SECRET_KEY": settings.secret_key.get_secret_value()
            if settings.secret_key
            else "",
            "PERFIN_LOG_LEVEL": "INFO",
            "PERFIN_BIND_HOST": "127.0.0.1",
            "PERFIN_METRICS_PORT": str(_free_port()),
        }
    )
    return env


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_perfin_worker_cli_processes_noop_and_exits_on_sigterm(
    factory: sessionmaker[Session], worker_env: dict[str, str], queue_name: str
) -> None:
    discover_handlers()
    with factory() as session, session.begin():
        job_id = enqueue(session, "noop", NoopPayload(label="cli smoke"), queue=queue_name)
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.cli", "worker", "--queue", queue_name, "--concurrency", "2"],
        cwd=BACKEND_DIR,
        env=worker_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert _wait_for(lambda: _state(factory, job_id).state == "succeeded", timeout=30), (
            "worker did not run the noop job"
        )
        # The worker serves job metrics on the internal metrics port (H7).
        metrics_url = f"http://127.0.0.1:{worker_env['PERFIN_METRICS_PORT']}/metrics"
        body = httpx.get(metrics_url, timeout=5).text
        assert 'perfin_jobs_total{outcome="succeeded",type="noop"} 1.0' in body
        proc.send_signal(signal.SIGTERM)
        out, err = proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 0, err
    assert "worker stopped" in out + err


def test_perfin_worker_cli_rejects_bad_options(worker_env: dict[str, str]) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "worker", "--concurrency", "0"],
        cwd=BACKEND_DIR,
        env=worker_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "concurrency must be at least 1" in result.stderr


def test_perfin_worker_cli_refuses_insecure_config(worker_env: dict[str, str]) -> None:
    env = {**worker_env, "PERFIN_DEV_MODE": "false", "PERFIN_SECRET_KEY": "changeme"}
    result = subprocess.run(
        [sys.executable, "-m", "app.cli", "worker"],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "perfin:" in result.stderr
