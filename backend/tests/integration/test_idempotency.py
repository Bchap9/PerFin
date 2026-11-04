# SPDX-License-Identifier: AGPL-3.0-or-later
"""``app.core.idempotency`` against a real database: concurrency, expiry, scoping.

The HTTP behaviour (replay, reuse, failed requests) is covered with the
endpoints in ``tests/integration/test_accounts_api.py``; here the helper is
driven directly with two database sessions.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.errors import ProblemError
from app.core.idempotency import KEY_TTL, idempotent
from app.db.models import IdempotencyKey
from app.db.rls import set_context
from tests.fixtures.factories import Factories, UserRecord

pytestmark = pytest.mark.integration


class Made(BaseModel):
    n: int


def _principal(record: UserRecord) -> Principal:
    assert record.household_id is not None
    return Principal(
        user=record.user,
        household_id=record.household_id,
        role=HouseholdRole.OWNER,
        auth_method="session",
    )


@pytest.fixture
def owner(factories: Factories) -> UserRecord:
    return factories.user(role="owner")


@contextmanager
def _session(factories: Factories, record: UserRecord) -> Iterator[Session]:
    with factories.session_factory() as db, db.begin():
        set_context(db, record.household_id, record.id)
        yield db


def _run(
    factories: Factories,
    record: UserRecord,
    key: str | None,
    run: Callable[[], Made],
    *,
    payload: Any = None,
) -> Made | JSONResponse:
    principal = _principal(record)
    with _session(factories, record) as db:
        return idempotent(
            db, principal, key, scope="POST /test", payload=payload or {"a": 1}, run=run
        )


def _rows(factories: Factories, record: UserRecord) -> int:
    with _session(factories, record) as db:
        return int(db.execute(select(func.count()).select_from(IdempotencyKey)).scalar_one())


def test_without_a_key_it_just_runs(factories: Factories, owner: UserRecord) -> None:
    calls: list[int] = []

    def run() -> Made:
        calls.append(1)
        return Made(n=len(calls))

    assert _run(factories, owner, None, run) == Made(n=1)
    assert _run(factories, owner, None, run) == Made(n=2)
    assert _rows(factories, owner) == 0


def test_replay_and_mismatch(factories: Factories, owner: UserRecord) -> None:
    first = _run(factories, owner, "k1", lambda: Made(n=7))
    assert first == Made(n=7)
    replay = _run(factories, owner, "k1", lambda: pytest.fail("ran twice"))
    assert isinstance(replay, JSONResponse)
    assert replay.status_code == 201
    assert replay.body == b'{"n":7}'
    assert replay.headers["Idempotent-Replayed"] == "true"
    with pytest.raises(ProblemError) as caught:
        _run(factories, owner, "k1", lambda: Made(n=0), payload={"a": 2})
    assert caught.value.slug == "idempotency-key-reused"


def test_concurrent_requests_with_one_key_run_once(factories: Factories, owner: UserRecord) -> None:
    """The second request waits for the first and replays its committed answer."""
    principal = _principal(owner)
    first_inside = threading.Event()
    release_first = threading.Event()
    results: dict[str, Any] = {}
    runs: list[str] = []

    def first() -> None:
        with _session(factories, owner) as db:

            def run() -> Made:
                runs.append("first")
                first_inside.set()
                assert release_first.wait(10)
                return Made(n=1)

            results["first"] = idempotent(
                db, principal, "same", scope="POST /test", payload={}, run=run
            )

    def second() -> None:
        with _session(factories, owner) as db:

            def run() -> Made:
                runs.append("second")
                return Made(n=2)

            results["second"] = idempotent(
                db, principal, "same", scope="POST /test", payload={}, run=run
            )

    t1 = threading.Thread(target=first)
    t1.start()
    assert first_inside.wait(10)
    t2 = threading.Thread(target=second)
    t2.start()
    t2.join(0.5)
    assert t2.is_alive(), "the second request must wait for the first one's key row"
    release_first.set()
    t1.join(10)
    t2.join(10)
    assert runs == ["first"]
    assert results["first"] == Made(n=1)
    assert isinstance(results["second"], JSONResponse)
    assert results["second"].body == b'{"n":1}'


def test_a_rolled_back_first_request_lets_the_waiter_run(
    factories: Factories, owner: UserRecord
) -> None:
    principal = _principal(owner)
    first_inside = threading.Event()
    release_first = threading.Event()
    results: dict[str, Any] = {}

    def first() -> None:
        def run() -> Made:
            first_inside.set()
            assert release_first.wait(10)
            raise ProblemError(422, "nope", "Nope")

        try:
            with _session(factories, owner) as db:
                idempotent(db, principal, "flaky", scope="POST /test", payload={}, run=run)
        except ProblemError:
            results["first"] = "failed"

    def second() -> None:
        with _session(factories, owner) as db:
            results["second"] = idempotent(
                db, principal, "flaky", scope="POST /test", payload={}, run=lambda: Made(n=2)
            )

    t1 = threading.Thread(target=first)
    t1.start()
    assert first_inside.wait(10)
    t2 = threading.Thread(target=second)
    t2.start()
    release_first.set()
    t1.join(10)
    t2.join(10)
    assert results == {"first": "failed", "second": Made(n=2)}


def test_expired_keys_are_forgotten(
    factories: Factories, owner: UserRecord, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(factories, owner, "old", lambda: Made(n=1)) == Made(n=1)
    later = clock.utcnow() + KEY_TTL + timedelta(minutes=1)
    monkeypatch.setattr(clock, "utcnow", lambda: later)
    # Runs again (and with another body): the old answer expired and is removed.
    assert _run(factories, owner, "old", lambda: Made(n=2), payload={"b": 1}) == Made(n=2)
    assert _rows(factories, owner) == 1


def test_keys_are_per_user_and_household(factories: Factories, owner: UserRecord) -> None:
    other_household_owner = factories.user(role="owner")
    same_household = factories.user(role="admin", household=owner.household)
    for record, n in ((owner, 1), (other_household_owner, 2), (same_household, 3)):
        result = _run(factories, record, "shared-key", lambda n=n: Made(n=n))  # type: ignore[misc]
        assert result == Made(n=n), record.role
    # Row-level security: each household sees only its own rows.
    assert _rows(factories, other_household_owner) == 1
    assert _rows(factories, owner) == 2


def test_principal_without_household_is_a_bug(factories: Factories, owner: UserRecord) -> None:
    principal = Principal(user=owner.user, household_id=None, role=None, auth_method="session")
    with _session(factories, owner) as db, pytest.raises(RuntimeError):
        idempotent(db, principal, "k", scope="POST /test", payload={}, run=lambda: Made(n=1))


def test_key_is_bound_to_the_scope(factories: Factories, owner: UserRecord) -> None:
    principal = _principal(owner)
    with _session(factories, owner) as db:
        idempotent(db, principal, "k", scope="POST /a", payload={}, run=lambda: Made(n=1))
    with _session(factories, owner) as db, pytest.raises(ProblemError) as caught:
        idempotent(db, principal, "k", scope="POST /b", payload={}, run=lambda: Made(n=1))
    assert caught.value.slug == "idempotency-key-reused"


def test_waiting_longer_than_lock_timeout_is_409(factories: Factories, owner: UserRecord) -> None:
    principal = _principal(owner)
    first_inside = threading.Event()
    release_first = threading.Event()

    def first() -> None:
        def run() -> Made:
            first_inside.set()
            assert release_first.wait(10)
            return Made(n=1)

        with _session(factories, owner) as db:
            idempotent(db, principal, "slow", scope="POST /test", payload={}, run=run)

    t1 = threading.Thread(target=first)
    t1.start()
    try:
        assert first_inside.wait(10)
        # The session must see the error (and roll back) inside pytest.raises.
        with pytest.raises(ProblemError) as caught, _session(factories, owner) as db:  # noqa: PT012
            db.execute(text("SET LOCAL lock_timeout = '200ms'"))
            idempotent(db, principal, "slow", scope="POST /test", payload={}, run=lambda: Made(n=2))
    finally:
        release_first.set()
        t1.join(10)
    assert (caught.value.status, caught.value.slug) == (409, "idempotency-key-in-use")
    assert caught.value.headers is not None
    assert caught.value.headers["Retry-After"] == "1"
