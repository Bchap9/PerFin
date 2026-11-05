# SPDX-License-Identifier: AGPL-3.0-or-later
"""The SSE generator behind ``GET /events`` and the per-user stream cap.

Delivery: each stream holds one dedicated Postgres connection (autocommit,
application role) that ``LISTEN``\\ s on ``perfin_events``. A notification
only wakes the stream (its payload is checked for the household and never
trusted further); the stream then reads the household's ``event`` rows after
its cursor under RLS, filters them for the viewer
(:func:`app.domain.events.service.deliverable`) and sends id and type. The
same read also runs on every heartbeat, so a lost notification delays an
event by at most ``PERFIN_SSE_HEARTBEAT_SECONDS``.

Ordering: event ids are UUIDv7. A transaction that emitted an event commits
after a later one only within :data:`LATE_COMMIT_GRACE`; the stream re-reads
that window and skips what it already sent, so such an event still arrives.
Nothing older than the stream's starting point is ever sent.

Resume: ``Last-Event-ID`` (sent by ``EventSource`` on reconnect) or
``?last_event_id=`` continues after that event if it is within the event
retention (7 days); an unknown, malformed or expired id starts at "now" with
a ``{"type": "stream.resync"}`` message (the client refetches everything).
Without one the stream starts at "now".

Lifetime: a stream ends after ``PERFIN_SSE_MAX_STREAM_SECONDS`` (the browser
reconnects); every ``PERFIN_SSE_REVALIDATE_SECONDS`` it re-checks the
session and grants and ends with ``{"type": "session.ended"}`` when they are
gone. Each user may hold ``PERFIN_SSE_MAX_STREAMS_PER_USER`` streams per
API process (:class:`StreamSlots`; a slot also expires on its own after the
stream lifetime, so a crashed stream cannot keep it).
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Final

import anyio
import anyio.to_thread
import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import utcnow
from app.core.events import DEFAULT_RETENTION, EVENTS_CHANNEL, parse_notice
from app.core.ids import build_uuid7, uuid7_unix_ms
from app.core.logging import get_logger
from app.core.settings import Settings
from app.db.session import connection_options
from app.domain.events import service
from app.domain.events.service import StreamView

log = get_logger(__name__)

LATE_COMMIT_GRACE: Final = timedelta(seconds=60)
RETRY_MS: Final = 3000
BATCH: Final = 200
#: Pages read per wake-up at most (the next wake-up continues).
MAX_PAGES: Final = 20
KEEPALIVE: Final = b": keep-alive\n\n"


def uuid7_floor(moment: datetime) -> uuid.UUID:
    """The smallest UUIDv7 of ``moment``'s millisecond (every later id is greater)."""
    return build_uuid7(max(0, int(moment.timestamp() * 1000)), 0)


# ---------------------------------------------------------------------------
# Per-user stream cap
# ---------------------------------------------------------------------------


class StreamSlots:
    """Concurrent streams per user in this process, each slot with a lease."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: dict[uuid.UUID, dict[str, float]] = {}

    def acquire(self, user_id: uuid.UUID, *, limit: int, lease_seconds: float) -> str | None:
        now = time.monotonic()
        with self._lock:
            held = {k: v for k, v in self._slots.get(user_id, {}).items() if v > now}
            if len(held) >= limit:
                self._slots[user_id] = held
                return None
            token = secrets.token_hex(8)
            held[token] = now + lease_seconds
            self._slots[user_id] = held
            return token

    def release(self, user_id: uuid.UUID, token: str) -> None:
        with self._lock:
            held = self._slots.get(user_id)
            if held is not None:
                held.pop(token, None)
                if not held:
                    self._slots.pop(user_id, None)

    def count(self, user_id: uuid.UUID) -> int:
        now = time.monotonic()
        with self._lock:
            return sum(1 for v in self._slots.get(user_id, {}).values() if v > now)


# ---------------------------------------------------------------------------
# The stream
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StreamConfig:
    heartbeat: float
    max_seconds: float
    revalidate: float
    retention: timedelta = DEFAULT_RETENTION
    grace: timedelta = LATE_COMMIT_GRACE

    @classmethod
    def from_settings(cls, settings: Settings) -> StreamConfig:
        return cls(
            heartbeat=settings.sse_heartbeat_seconds,
            max_seconds=settings.sse_max_stream_seconds,
            revalidate=settings.sse_revalidate_seconds,
        )


def listen_dsn(settings: Settings) -> str:
    """A libpq URL for the application role (the SQLAlchemy URL without its driver)."""
    url = make_url(settings.database_url.get_secret_value()).set(drivername="postgresql")
    return url.render_as_string(hide_password=False)


async def open_listener(settings: Settings) -> psycopg.AsyncConnection[tuple[object, ...]]:
    """A dedicated autocommit connection listening on :data:`EVENTS_CHANNEL`."""
    options = connection_options(
        statement_timeout_ms=settings.db_statement_timeout_ms,
        lock_timeout_ms=settings.db_lock_timeout_ms,
    )
    if options:
        conn = await psycopg.AsyncConnection.connect(
            listen_dsn(settings), autocommit=True, options=options
        )
    else:
        conn = await psycopg.AsyncConnection.connect(listen_dsn(settings), autocommit=True)
    try:
        await conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(EVENTS_CHANNEL)))
    except BaseException:
        await conn.close()
        raise
    return conn


@dataclass(frozen=True)
class Start:
    """Where a stream starts: after ``cursor``; ``resync`` when history was lost."""

    cursor: uuid.UUID
    resync: bool


def start_point(last_event_id: str | None, *, retention: timedelta, now: datetime) -> Start:
    """Interpret ``Last-Event-ID`` (module doc)."""
    floor_now = uuid7_floor(now)
    if not last_event_id:
        return Start(floor_now, False)
    try:
        value = uuid.UUID(last_event_id.strip())
        stamp_ms = uuid7_unix_ms(value)
    except ValueError:
        return Start(floor_now, True)
    oldest = (now - retention).timestamp() * 1000
    newest = now.timestamp() * 1000 + 60_000
    if not oldest <= stamp_ms <= newest:
        return Start(floor_now, True)
    return Start(value, False)


@dataclass
class _Cursor:
    start: uuid.UUID
    position: uuid.UUID
    sent: dict[uuid.UUID, None] = field(default_factory=dict)

    def lower_bound(self, now: datetime, grace: timedelta) -> uuid.UUID:
        lower = min(self.position, uuid7_floor(now - grace))
        return max(lower, self.start)

    def forget_before(self, bound: uuid.UUID) -> None:
        for event_id in [e for e in self.sent if e <= bound]:
            del self.sent[event_id]


async def _wait_for_household(
    conn: psycopg.AsyncConnection[tuple[object, ...]], household_id: uuid.UUID, seconds: float
) -> bool:
    """``True`` once a notification for the household arrives, ``False`` after ``seconds``."""
    if seconds <= 0:
        return False
    notifications = conn.notifies(timeout=seconds)
    try:
        async for notification in notifications:
            notice = parse_notice(notification.payload)
            if notice is not None and notice.household_id == household_id:
                return True
    finally:
        await notifications.aclose()
    return False


@dataclass(frozen=True)
class StreamDeps:
    """What a stream needs besides its listener (built by the router)."""

    factory: sessionmaker[Session]
    refresh: Callable[[Session], StreamView | None]
    config: StreamConfig
    release: Callable[[], None]


async def event_stream(
    conn: psycopg.AsyncConnection[tuple[object, ...]],
    deps: StreamDeps,
    start: Start,
) -> AsyncIterator[bytes]:
    """The SSE body (module doc). Always releases its slot and closes ``conn``."""
    config = deps.config

    def refresh() -> StreamView | None:
        with deps.factory() as db, db.begin():
            return deps.refresh(db)

    def read(view: StreamView, cursor: _Cursor) -> list[bytes]:
        frames: list[bytes] = []
        now = utcnow()
        not_before = now - config.retention
        lower = cursor.lower_bound(now, config.grace)
        with deps.factory() as db, db.begin():
            for _ in range(MAX_PAGES):
                rows = service.fetch_events(
                    db, view, after=lower, not_before=not_before, limit=BATCH
                )
                fresh = [row for row in rows if row.id not in cursor.sent]
                for row in service.deliverable(db, view, fresh):
                    frames.append(service.event_frame(row))
                for row in fresh:
                    cursor.sent[row.id] = None
                    cursor.position = max(cursor.position, row.id)
                if len(rows) < BATCH:
                    break
                lower = rows[-1].id
        cursor.forget_before(uuid7_floor(now - config.grace))
        return frames

    loop_clock = time.monotonic
    started = loop_clock()
    try:
        view = await anyio.to_thread.run_sync(refresh)
        if view is None:
            yield service.frame(None, {"type": "session.ended"})
            return
        household_id = view.household_id
        yield f"retry: {RETRY_MS}\n\n".encode("ascii")
        if start.resync:
            yield service.frame(None, {"type": "stream.resync"})
        cursor = _Cursor(start=start.cursor, position=start.cursor)
        last_sent = loop_clock()
        next_check = started + config.revalidate
        deadline = started + config.max_seconds
        while True:
            frames = await anyio.to_thread.run_sync(read, view, cursor)
            for chunk in frames:
                yield chunk
            if frames:
                last_sent = loop_clock()
            now = loop_clock()
            if now >= deadline:
                return
            if now >= next_check:
                refreshed = await anyio.to_thread.run_sync(refresh)
                if refreshed is None:
                    yield service.frame(None, {"type": "session.ended"})
                    return
                view = refreshed
                next_check = now + config.revalidate
            if now - last_sent >= config.heartbeat:
                yield KEEPALIVE
                last_sent = now
            timeout = min(config.heartbeat - (now - last_sent), deadline - now, next_check - now)
            await _wait_for_household(conn, household_id, max(timeout, 0.01))
    except psycopg.Error as exc:
        log.warning("event stream lost its listener", extra={"error_class": type(exc).__name__})
    finally:
        deps.release()
        with anyio.CancelScope(shield=True):
            await conn.close()
