# SPDX-License-Identifier: AGPL-3.0-or-later
"""DB-backed sliding-window rate limits and progressive lockout (SECURITY.md A1)."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.auth.models import RateLimitEvent, User
from app.auth.ratelimit import (
    LOGIN_PER_IP,
    Limit,
    LockoutPolicy,
    RateLimiter,
    account_key,
    ip_key,
    is_locked,
    key_hash,
    register_login_failure,
    register_login_success,
    too_many_requests,
)
from app.core.errors import install_error_handlers
from app.core.request_context import RequestContextMiddleware
from app.db.session import make_sessionmaker
from tests.fixtures.factories import Factories, unique_name

pytestmark = pytest.mark.integration

T0 = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)


@pytest.fixture
def factory(app_engine: Engine) -> sessionmaker[Session]:
    return make_sessionmaker(app_engine)


@pytest.fixture
def limiter(factory: sessionmaker[Session]) -> RateLimiter:
    return RateLimiter(factory)


def _limit(max_events: int = 3, window: timedelta = timedelta(minutes=1)) -> Limit:
    return Limit(unique_name("test")[:60], max_events, window)


def test_allows_up_to_the_limit_then_denies(limiter: RateLimiter) -> None:
    limit = _limit(3)
    decisions = [limiter.hit(limit, "k", now=T0 + timedelta(seconds=i)) for i in range(5)]
    assert [d.allowed for d in decisions] == [True, True, True, False, False]
    assert decisions[2].count == 3
    denied = decisions[3]
    assert denied.retry_after_seconds == 57  # oldest (T0) + 60 s - (T0 + 3 s)
    with pytest.raises(Exception, match="429") as info:
        denied.raise_if_denied()
    assert info.value.headers["Retry-After"] == "57"  # type: ignore[attr-defined]
    decisions[0].raise_if_denied()  # allowed: no error


def test_window_slides(limiter: RateLimiter) -> None:
    limit = _limit(2, timedelta(seconds=10))
    assert limiter.hit(limit, "k", now=T0).allowed
    assert limiter.hit(limit, "k", now=T0 + timedelta(seconds=5)).allowed
    assert not limiter.hit(limit, "k", now=T0 + timedelta(seconds=9)).allowed
    assert limiter.hit(limit, "k", now=T0 + timedelta(seconds=11)).allowed  # T0 slid out
    assert not limiter.hit(limit, "k", now=T0 + timedelta(seconds=12)).allowed


def test_keys_and_scopes_are_independent(limiter: RateLimiter) -> None:
    a, b = _limit(1), _limit(1)
    assert limiter.hit(a, "ip-1", now=T0).allowed
    assert not limiter.hit(a, "ip-1", now=T0).allowed
    assert limiter.hit(a, "ip-2", now=T0).allowed
    assert limiter.hit(b, "ip-1", now=T0).allowed


def test_peek_and_reset(limiter: RateLimiter) -> None:
    limit = _limit(1)
    assert limiter.peek(limit, "k", now=T0).allowed
    limiter.hit(limit, "k", now=T0)
    assert not limiter.peek(limit, "k", now=T0).allowed
    limiter.reset(limit, "k")
    assert limiter.hit(limit, "k", now=T0).allowed


def test_counts_survive_a_rolled_back_request(
    limiter: RateLimiter, factory: sessionmaker[Session]
) -> None:
    """A failed login answers with an error (request rolls back); the hit stays."""
    limit = _limit(1)
    with factory() as request_db:
        request_db.begin()
        limiter.hit(limit, "k", now=T0)
        request_db.rollback()
    assert not limiter.hit(limit, "k", now=T0).allowed


def test_raw_keys_are_not_stored(limiter: RateLimiter, factory: sessionmaker[Session]) -> None:
    limit = _limit(5)
    limiter.hit(limit, "198.51.100.23", now=T0)
    with factory() as db:
        stored = (
            db.execute(select(RateLimitEvent.key_hash).where(RateLimitEvent.scope == limit.scope))
            .scalars()
            .all()
        )
    assert stored == [key_hash(limit.scope, "198.51.100.23")]
    assert all("198.51.100.23" not in value for value in stored)


def test_concurrent_hits_do_not_overshoot(limiter: RateLimiter) -> None:
    limit = _limit(5)
    results: list[bool] = []
    lock = threading.Lock()

    def attempt() -> None:
        decision = limiter.hit(limit, "shared", now=T0)
        with lock:
            results.append(decision.allowed)

    threads = [threading.Thread(target=attempt) for _ in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(True) == 5


def test_prune(limiter: RateLimiter, factory: sessionmaker[Session]) -> None:
    limit = _limit(10)
    limiter.hit(limit, "old", now=T0 - timedelta(days=2))
    limiter.hit(limit, "new", now=T0)
    limiter.prune(timedelta(days=1), now=T0)
    with factory() as db:
        count = db.execute(
            select(func.count()).where(RateLimitEvent.scope == limit.scope)
        ).scalar_one()
    assert count == 1


def test_too_many_requests_problem() -> None:
    error = too_many_requests(0)
    assert error.status == 429
    assert error.slug == "rate-limited"
    assert error.headers == {"Retry-After": "1"}


def test_ip_key_uses_trusted_proxy_resolution() -> None:
    from ipaddress import ip_network

    app = FastAPI()
    install_error_handlers(app)

    @app.get("/ip")
    def ip(request: Request) -> dict[str, str]:
        return {"key": ip_key(request)}

    app.add_middleware(RequestContextMiddleware, trusted_proxies=[ip_network("127.0.0.0/8")])
    spoof = {"X-Forwarded-For": "203.0.113.9"}
    # TestClient's peer is "testclient" (not a trusted proxy address): ignored.
    assert TestClient(app).get("/ip", headers=spoof).json() == {"key": "testclient"}
    trusted = TestClient(app, client=("127.0.0.1", 1234))
    assert trusted.get("/ip", headers=spoof).json() == {"key": "203.0.113.9"}


def test_default_login_limits_are_configured() -> None:
    assert LOGIN_PER_IP.max_events >= 1
    assert account_key(None, "Someone") == "name:someone"


# ---- lockout ---------------------------------------------------------------------


def test_progressive_lockout_and_reset(
    factories: Factories, factory: sessionmaker[Session]
) -> None:
    record = factories.user(role=None)
    policy = LockoutPolicy(delay_after=2, lock_after=4, lock_duration=timedelta(minutes=15))
    outcomes = [
        register_login_failure(factory, record.id, policy=policy, now=T0 + timedelta(seconds=i))
        for i in range(4)
    ]
    assert [o.failed_login_count for o in outcomes] == [1, 2, 3, 4]
    assert outcomes[0].locked_until is None
    assert outcomes[1].locked_until == T0 + timedelta(seconds=1) + timedelta(seconds=1)
    assert outcomes[2].locked_until == T0 + timedelta(seconds=2) + timedelta(seconds=2)
    assert outcomes[3].locked_until == T0 + timedelta(seconds=3) + timedelta(minutes=15)
    assert [o.newly_locked for o in outcomes] == [False, False, False, True]
    with factory() as db:
        user = db.get(User, record.id)
        assert user is not None
        assert is_locked(user, T0 + timedelta(minutes=10))
        assert not is_locked(user, T0 + timedelta(minutes=16))
    with factory() as db, db.begin():
        register_login_success(db, record.id, now=T0 + timedelta(minutes=20))
    with factory() as db:
        user = db.get(User, record.id)
        assert user is not None
        assert user.failed_login_count == 0
        assert user.locked_until is None
        assert user.last_login_at == T0 + timedelta(minutes=20)
