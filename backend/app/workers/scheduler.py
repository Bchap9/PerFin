# SPDX-License-Identifier: AGPL-3.0-or-later
"""Periodic enqueue of recurring jobs.

A recurring job is ``(name, job type, interval, payload)``. Time is divided
into fixed slots of ``interval`` seconds since the Unix epoch; on every tick
the scheduler enqueues the *current* slot's job with the dedupe key
``sched:<name>:<interval>:<slot>``. ``job.dedupe_key`` is unique, so however
many worker replicas tick, each slot is enqueued exactly once. Slots missed
while no worker ran are not back-filled (a daily snapshot that was missed
runs once, now, not N times).

The slot is computed from the database clock (``now()``) so replicas with
skewed clocks agree. Registration::

    from app.workers.scheduler import recurring
    recurring("snapshot_net_worth", timedelta(days=1), SnapshotPayload())
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.db.queue import DEFAULT_QUEUE, enqueue
from app.workers.registry import REGISTRY, HandlerRegistry, validate_job_type

log = get_logger(__name__)

MIN_INTERVAL: Final = timedelta(seconds=60)
_NAME_RE: Final = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")


@dataclass(frozen=True)
class RecurringJob:
    """One periodic job definition."""

    name: str
    job_type: str
    interval: timedelta
    payload: BaseModel
    queue: str = DEFAULT_QUEUE
    priority: int = 0

    @property
    def interval_seconds(self) -> int:
        return int(self.interval.total_seconds())

    def slot(self, now: datetime) -> int:
        """Index of the slot containing ``now``."""
        return int(now.timestamp()) // self.interval_seconds

    def slot_start(self, slot: int) -> datetime:
        return datetime.fromtimestamp(slot * self.interval_seconds, tz=UTC)

    def dedupe_key(self, slot: int) -> str:
        return f"sched:{self.name}:{self.interval_seconds}:{slot}"


class Schedule:
    """A set of recurring jobs. The process-wide one is :data:`SCHEDULE`."""

    def __init__(self) -> None:
        self._jobs: dict[str, RecurringJob] = {}
        self._lock = threading.Lock()

    def add(
        self,
        job_type: str,
        interval: timedelta,
        payload: BaseModel,
        *,
        name: str | None = None,
        queue: str = DEFAULT_QUEUE,
        priority: int = 0,
    ) -> RecurringJob:
        validate_job_type(job_type)
        key = name if name is not None else job_type
        if not _NAME_RE.fullmatch(key):
            raise ValueError(f"invalid recurring job name: {key!r}")
        if interval < MIN_INTERVAL or interval.total_seconds() % 1:
            raise ValueError("interval must be a whole number of seconds, at least 60")
        if not isinstance(payload, BaseModel):
            raise TypeError("payload must be a pydantic model instance")
        job = RecurringJob(
            name=key,
            job_type=job_type,
            interval=interval,
            payload=payload,
            queue=queue,
            priority=priority,
        )
        with self._lock:
            if key in self._jobs and self._jobs[key] != job:
                raise ValueError(f"recurring job {key!r} is already registered")
            self._jobs[key] = job
        return job

    def remove(self, name: str) -> None:
        with self._lock:
            self._jobs.pop(name, None)

    def jobs(self) -> list[RecurringJob]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.name)

    def tick(
        self,
        session: Session,
        *,
        now: datetime | None = None,
        registry: HandlerRegistry | None = None,
    ) -> list[uuid.UUID]:
        """Enqueue the current slot of every recurring job (idempotent).

        Runs in the caller's transaction. Jobs whose type has no handler in
        ``registry`` are skipped with a warning. Returns the ids of the
        current-slot jobs (new or already present).
        """
        handlers = registry if registry is not None else REGISTRY
        current = now if now is not None else _db_now(session)
        ids: list[uuid.UUID] = []
        for job in self.jobs():
            if job.job_type not in handlers:
                log.warning(
                    "recurring job has no handler; skipped",
                    extra={"recurring": job.name, "job_type": job.job_type},
                )
                continue
            slot = job.slot(current)
            ids.append(
                enqueue(
                    session,
                    job.job_type,
                    job.payload,
                    queue=job.queue,
                    priority=job.priority,
                    run_after=job.slot_start(slot),
                    dedupe_key=job.dedupe_key(slot),
                    registry=handlers,
                )
            )
        return ids


def _db_now(session: Session) -> datetime:
    value = session.execute(text("SELECT now()")).scalar_one()
    if not isinstance(value, datetime):  # pragma: no cover - driver contract
        raise TypeError("now() did not return a datetime")
    return value


#: Recurring jobs registered by handler modules at import time.
SCHEDULE: Final = Schedule()


def recurring(
    job_type: str,
    interval: timedelta,
    payload: BaseModel,
    *,
    name: str | None = None,
    queue: str = DEFAULT_QUEUE,
    priority: int = 0,
) -> RecurringJob:
    """Register a recurring job in :data:`SCHEDULE`."""
    return SCHEDULE.add(job_type, interval, payload, name=name, queue=queue, priority=priority)
