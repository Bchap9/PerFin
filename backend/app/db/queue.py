# SPDX-License-Identifier: AGPL-3.0-or-later
"""PostgreSQL job queue (ADR-0002).

* :func:`enqueue` inserts a ``job`` row **in the caller's transaction**, so a
  job exists if and only if the business write that caused it commits, and
  sends ``pg_notify('perfin_jobs', queue)`` (delivered on commit only).
* :func:`dequeue` claims one runnable job with ``SELECT ... FOR UPDATE SKIP
  LOCKED`` ordered by ``priority`` (ascending), ``run_after``, ``id``; it marks
  it ``running`` with a lease (``locked_by``/``locked_at``/
  ``lease_expires_at``) and increments ``attempts``. A per-household limit
  caps how many of one household's jobs run at once (SECURITY.md C10); the
  check is serialised per household with a transaction-level advisory lock.
* :func:`mark_succeeded` / :func:`mark_failed` finish a claim. They match the
  claim exactly (``locked_by`` *and* ``attempts``), so a worker whose lease was
  reaped can never overwrite the outcome of a later attempt.
* Failed attempts go back to ``queued`` with exponential backoff and jitter
  (:func:`compute_backoff`); after ``max_attempts`` (or a permanent error) the
  job is dead-lettered (``dead``).
* :func:`reap_expired_leases` returns jobs whose lease ran out (crashed or
  killed worker) to ``queued``, or ``dead`` when attempts are exhausted. A
  crash counts as an attempt, so a job that kills its worker cannot loop.

Every statement is fixed SQL with bound parameters.

``job`` has forced RLS (``app.db.rls``): a household context sees only that
household's jobs. :func:`enqueue` runs under the caller's context (a job for
another household is refused). The cross-household operations (claim, finish,
reap, prune, operator requeue) run inside :func:`app.db.rls.queue_scope`.
``payload`` is always a JSON object: :func:`enqueue` refuses anything else
and a check constraint backs it; :func:`dequeue` passes the stored value
through unchanged so the runner dead-letters a malformed row instead of the
claim crashing (and wedging its queue).
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import Callable, Iterable, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from pydantic import BaseModel, RootModel, ValidationError
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.core.ids import uuid7
from app.db.models import Job, JobState
from app.db.rls import queue_scope
from app.workers.registry import REGISTRY, HandlerRegistry, validate_job_type

NOTIFY_CHANNEL: Final = "perfin_jobs"
DEFAULT_QUEUE: Final = "default"
DEFAULT_MAX_ATTEMPTS: Final = 5
#: Payloads carry ids and small parameters only, never document content.
MAX_PAYLOAD_BYTES: Final = 64 * 1024
LAST_ERROR_MAX_CHARS: Final = 2000
DEDUPE_KEY_MAX_CHARS: Final = 200
QUEUE_NAME_MAX_CHARS: Final = 64

#: Backoff after the n-th failed attempt: base * 2**(n-1), capped, with jitter.
BACKOFF_BASE_SECONDS: Final = 5.0
BACKOFF_CAP_SECONDS: Final = 3600.0

#: Default number of jobs of one household that may run at the same time.
DEFAULT_HOUSEHOLD_CONCURRENCY: Final = 2

# Namespace (first key) for pg_try_advisory_xact_lock(int4, int4) on a household.
_HOUSEHOLD_LOCK_NAMESPACE: Final = 0x5046_4A51  # "PFJQ"
_MAX_CANDIDATES: Final = 20

_sysrand = secrets.SystemRandom()


class PayloadTooLargeError(ValueError):
    """The serialised payload exceeds :data:`MAX_PAYLOAD_BYTES`."""


class DedupeKeyConflictError(ValueError):
    """``dedupe_key`` is already taken by a job of another household (or scope)."""


@dataclass(frozen=True)
class ClaimedJob:
    """A job leased to one worker by :func:`dequeue`."""

    id: uuid.UUID
    queue: str
    type: str
    #: The stored JSON exactly as read: normally an object, but never coerced,
    #: so a malformed legacy row reaches validation and is dead-lettered.
    payload: Any
    priority: int
    attempts: int
    max_attempts: int
    household_id: uuid.UUID | None
    locked_by: str
    locked_at: datetime
    lease_expires_at: datetime

    @property
    def is_last_attempt(self) -> bool:
        return self.attempts >= self.max_attempts


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_backoff(
    attempt: int,
    *,
    base: float = BACKOFF_BASE_SECONDS,
    cap: float = BACKOFF_CAP_SECONDS,
    rand: Callable[[float, float], float] | None = None,
) -> float:
    """Seconds to wait before retrying after failed attempt number ``attempt`` (1-based).

    Exponential (``base * 2**(attempt-1)``, capped at ``cap``) with "equal
    jitter": half the delay is fixed, the other half uniformly random, so
    retries of many jobs that failed together spread out but never retry
    sooner than half the nominal delay.
    """
    exponent = max(attempt, 1) - 1
    nominal = cap if exponent >= 63 else min(cap, base * float(2**exponent))
    uniform = rand if rand is not None else _sysrand.uniform
    return nominal / 2.0 + uniform(0.0, nominal / 2.0)


def validation_summary(exc: ValidationError) -> str:
    """Field paths and error types only; input values are never echoed."""
    parts = []
    for error in exc.errors(include_input=False, include_url=False, include_context=False):
        loc = ".".join(str(p) for p in error["loc"]) or "<root>"
        parts.append(f"{loc}: {error['type']}")
    return "; ".join(parts)


def truncate_error(message: str) -> str:
    if len(message) <= LAST_ERROR_MAX_CHARS:
        return message
    return message[: LAST_ERROR_MAX_CHARS - 3] + "..."


def _check_queue_name(queue: str) -> None:
    if not queue or len(queue) > QUEUE_NAME_MAX_CHARS or not queue.isascii():
        raise ValueError("invalid queue name")


def _serialise_payload(payload: BaseModel) -> dict[str, Any]:
    data = payload.model_dump(mode="json")
    if not isinstance(data, dict):
        raise TypeError("job payload must serialise to a JSON object")
    encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise PayloadTooLargeError(
            f"job payload is {len(encoded)} bytes; the limit is {MAX_PAYLOAD_BYTES}"
        )
    return data


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------

_SELECT_DEDUPED = text("SELECT id, household_id FROM job WHERE dedupe_key = :key")


def enqueue(
    session: Session,
    job_type: str,
    payload: BaseModel,
    *,
    queue: str = DEFAULT_QUEUE,
    run_after: datetime | None = None,
    household_id: uuid.UUID | None = None,
    max_attempts: int | None = None,
    priority: int = 0,
    dedupe_key: str | None = None,
    registry: HandlerRegistry | None = None,
) -> uuid.UUID:
    """Add a job in the caller's transaction and notify workers on commit.

    The payload is re-validated through its Pydantic model (so a
    ``model_construct``-ed or mutated instance cannot smuggle unvalidated
    data). When ``job_type`` is registered in this process, ``payload`` must
    be an instance of the handler's declared model and ``max_attempts``
    defaults to the handler's. Processes that do not import handler modules
    (the API) can still enqueue; the worker validates again on dequeue and
    dead-letters unknown types.

    With ``dedupe_key`` the insert is idempotent: if a job with that key
    already exists for the same ``household_id``, nothing is inserted and the
    existing job's id is returned; a key held by another household's job
    raises :class:`DedupeKeyConflictError` (its id is never returned).

    RLS applies: a household job can only be enqueued in that household's
    context; instance-level jobs (``household_id=None``) from any context.

    Returns the job id. Nothing is committed here.
    """
    validate_job_type(job_type)
    _check_queue_name(queue)
    if not isinstance(payload, BaseModel):
        raise TypeError("payload must be a pydantic model instance")
    if isinstance(payload, RootModel):
        raise TypeError("payload must be a model with fields, not a RootModel")
    if run_after is not None and run_after.tzinfo is None:
        raise ValueError("run_after must be timezone-aware")
    if household_id is not None and not isinstance(household_id, uuid.UUID):
        raise TypeError("household_id must be a UUID")
    if dedupe_key is not None and not 0 < len(dedupe_key) <= DEDUPE_KEY_MAX_CHARS:
        raise ValueError("invalid dedupe_key")

    spec = (registry if registry is not None else REGISTRY).get(job_type)
    if spec is not None:
        if not isinstance(payload, spec.payload_model):
            raise TypeError(
                f"payload for {job_type!r} must be {spec.payload_model.__name__}, "
                f"not {type(payload).__name__}"
            )
        validated = spec.validate_payload(payload)
        attempts_limit = spec.max_attempts if max_attempts is None else max_attempts
    else:
        validated = type(payload).model_validate(payload.model_dump(mode="json"))
        attempts_limit = DEFAULT_MAX_ATTEMPTS if max_attempts is None else max_attempts
    if attempts_limit < 1:
        raise ValueError("max_attempts must be at least 1")
    data = _serialise_payload(validated)

    job_id = uuid7()
    insert = pg_insert(Job).values(
        id=job_id,
        queue=queue,
        type=job_type,
        payload=data,
        state=JobState.QUEUED,
        priority=priority,
        run_after=run_after if run_after is not None else func.now(),
        attempts=0,
        max_attempts=attempts_limit,
        household_id=household_id,
        dedupe_key=dedupe_key,
    )
    if dedupe_key is not None:
        insert = insert.on_conflict_do_nothing(index_elements=[Job.dedupe_key])
    # RETURNING and ON CONFLICT check the new row against the SELECT policy,
    # which an instance-level job does not pass from a household context. An
    # instance-level row (household_id NULL by construction) is therefore
    # inserted inside queue_scope; that cannot reach any household's jobs. A
    # household job is always inserted under the caller's own context, so RLS
    # refuses a job for another household.
    scope: AbstractContextManager[None] = (
        queue_scope(session) if household_id is None else nullcontext()
    )
    with scope:
        inserted = session.execute(insert.returning(Job.id)).scalar_one_or_none()
    if inserted is None:
        with queue_scope(session):
            existing = session.execute(_SELECT_DEDUPED, {"key": dedupe_key}).one_or_none()
        if existing is None or existing.household_id != household_id:
            raise DedupeKeyConflictError("dedupe_key is already used by another job")
        return uuid.UUID(str(existing.id))
    session.execute(
        text("SELECT pg_notify(:channel, :queue)"), {"channel": NOTIFY_CHANNEL, "queue": queue}
    )
    return job_id


# ---------------------------------------------------------------------------
# Dequeue
# ---------------------------------------------------------------------------

_SELECT_CANDIDATE = text(
    """
    SELECT j.id, j.household_id, j.type
    FROM job AS j
    WHERE j.state = 'queued'
      AND j.run_after <= now()
      AND j.queue = ANY(CAST(:queues AS varchar[]))
      AND j.id <> ALL(CAST(:skip AS uuid[]))
      AND (
        j.household_id IS NULL
        OR (
          SELECT count(*) FROM job AS r
          WHERE r.household_id = j.household_id AND r.state = 'running'
        ) < :household_limit
      )
    ORDER BY j.priority, j.run_after, j.id
    LIMIT 1
    FOR UPDATE OF j SKIP LOCKED
    """
)

_TRY_HOUSEHOLD_LOCK = text(
    "SELECT pg_try_advisory_xact_lock(:namespace, hashtext(CAST(:household AS text)))"
)

_COUNT_RUNNING = text(
    "SELECT count(*) FROM job WHERE household_id = :household AND state = 'running'"
)

_CLAIM = text(
    """
    UPDATE job
    SET state = 'running',
        attempts = attempts + 1,
        locked_by = :worker,
        locked_at = now(),
        lease_expires_at = now() + CAST(:lease AS double precision) * interval '1 second',
        updated_at = now()
    WHERE id = :id AND state = 'queued'
    RETURNING id, queue, type, payload, priority, attempts, max_attempts, household_id,
              locked_by, locked_at, lease_expires_at
    """
)


def dequeue(
    session: Session,
    *,
    worker_id: str,
    queues: Sequence[str] = (DEFAULT_QUEUE,),
    lease_seconds: Callable[[str], float],
    household_limit: int = DEFAULT_HOUSEHOLD_CONCURRENCY,
) -> ClaimedJob | None:
    """Claim the next runnable job, or return ``None``.

    Runs in the caller's transaction; the caller commits right away so the
    claim becomes visible (and the row lock is released). ``lease_seconds``
    maps the job type to its lease length (handler timeout plus grace).
    """
    if not worker_id or len(worker_id) > 200:
        raise ValueError("invalid worker_id")
    if household_limit < 1:
        raise ValueError("household_limit must be at least 1")
    queue_list = list(queues)
    for name in queue_list:
        _check_queue_name(name)
    with queue_scope(session):
        return _claim_next(session, worker_id, queue_list, lease_seconds, household_limit)


def _claim_next(
    session: Session,
    worker_id: str,
    queue_list: list[str],
    lease_seconds: Callable[[str], float],
    household_limit: int,
) -> ClaimedJob | None:
    skip: list[uuid.UUID] = []
    for _ in range(_MAX_CANDIDATES):
        row = session.execute(
            _SELECT_CANDIDATE,
            {"queues": queue_list, "skip": skip, "household_limit": household_limit},
        ).one_or_none()
        if row is None:
            return None
        job_id = uuid.UUID(str(row.id))
        household = row.household_id
        if household is not None and not _household_has_capacity(
            session, uuid.UUID(str(household)), household_limit
        ):
            skip.append(job_id)
            continue
        lease = float(lease_seconds(str(row.type)))
        claimed = session.execute(_CLAIM, {"id": job_id, "worker": worker_id, "lease": lease}).one()
        return ClaimedJob(
            id=uuid.UUID(str(claimed.id)),
            queue=claimed.queue,
            type=claimed.type,
            payload=claimed.payload,
            priority=int(claimed.priority),
            attempts=int(claimed.attempts),
            max_attempts=int(claimed.max_attempts),
            household_id=(
                None if claimed.household_id is None else uuid.UUID(str(claimed.household_id))
            ),
            locked_by=claimed.locked_by,
            locked_at=claimed.locked_at,
            lease_expires_at=claimed.lease_expires_at,
        )
    return None


def _household_has_capacity(session: Session, household: uuid.UUID, limit: int) -> bool:
    """Serialise claims per household, then recount with a fresh snapshot.

    ``pg_try_advisory_xact_lock`` never waits (so two dequeuers cannot
    deadlock); if another worker is claiming for this household right now we
    skip the job this round. The lock is held until our claim commits, and the
    recount (a new statement under READ COMMITTED) sees every claim committed
    before we got the lock, so the limit is never exceeded.
    """
    locked = session.execute(
        _TRY_HOUSEHOLD_LOCK,
        {"namespace": _HOUSEHOLD_LOCK_NAMESPACE, "household": str(household)},
    ).scalar_one()
    if not locked:
        return False
    running = session.execute(_COUNT_RUNNING, {"household": household}).scalar_one()
    return int(running) < limit


# ---------------------------------------------------------------------------
# Finishing a claim
# ---------------------------------------------------------------------------

_SUCCEED = text(
    """
    UPDATE job
    SET state = 'succeeded', locked_by = NULL, locked_at = NULL, lease_expires_at = NULL,
        last_error = NULL, updated_at = now()
    WHERE id = :id AND state = 'running' AND locked_by = :worker AND attempts = :attempts
    """
)

_RETRY = text(
    """
    UPDATE job
    SET state = 'queued', locked_by = NULL, locked_at = NULL, lease_expires_at = NULL,
        run_after = now() + CAST(:delay AS double precision) * interval '1 second',
        last_error = :error, updated_at = now()
    WHERE id = :id AND state = 'running' AND locked_by = :worker AND attempts = :attempts
    """
)

_DEAD = text(
    """
    UPDATE job
    SET state = 'dead', locked_by = NULL, locked_at = NULL, lease_expires_at = NULL,
        last_error = :error, updated_at = now()
    WHERE id = :id AND state = 'running' AND locked_by = :worker AND attempts = :attempts
    """
)


def _claim_params(job: ClaimedJob) -> dict[str, Any]:
    return {"id": job.id, "worker": job.locked_by, "attempts": job.attempts}


def mark_succeeded(session: Session, job: ClaimedJob) -> bool:
    """Mark the claim succeeded; ``False`` if this worker no longer holds it.

    The runner calls this in the handler's own transaction so the handler's
    writes and the completion commit together; on ``False`` it rolls back.
    """
    with queue_scope(session):
        result = session.execute(_SUCCEED, _claim_params(job))
    return _rowcount(result) == 1


def mark_failed(
    session: Session,
    job: ClaimedJob,
    error: str,
    *,
    permanent: bool = False,
    backoff: float | None = None,
) -> JobState | None:
    """Record a failed attempt: retry later (``queued``) or dead-letter (``dead``).

    ``error`` must already be scrubbed of PII; it is truncated here. Returns
    the new state, or ``None`` if this worker no longer holds the claim.
    """
    params = {**_claim_params(job), "error": truncate_error(error)}
    with queue_scope(session):
        if permanent or job.attempts >= job.max_attempts:
            result = session.execute(_DEAD, params)
            return JobState.DEAD if _rowcount(result) == 1 else None
        delay = compute_backoff(job.attempts) if backoff is None else max(0.0, backoff)
        result = session.execute(_RETRY, {**params, "delay": delay})
    return JobState.QUEUED if _rowcount(result) == 1 else None


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


# ---------------------------------------------------------------------------
# Reaper, pruning, operator actions
# ---------------------------------------------------------------------------

LEASE_EXPIRED_ERROR: Final = "lease expired: worker stopped or crashed while running the job"

_REAP = text(
    """
    UPDATE job
    SET state = CASE WHEN attempts >= max_attempts
                     THEN CAST('dead' AS job_state) ELSE CAST('queued' AS job_state) END,
        run_after = CASE WHEN attempts >= max_attempts THEN run_after
                         ELSE now() + least(CAST(:cap AS double precision),
                                            CAST(:base AS double precision)
                                            * power(2, least(attempts, 30) - 1))
                                      * interval '1 second' END,
        locked_by = NULL, locked_at = NULL, lease_expires_at = NULL,
        last_error = :error, updated_at = now()
    WHERE id IN (
        SELECT id FROM job
        WHERE state = 'running' AND lease_expires_at < now()
        ORDER BY lease_expires_at
        LIMIT :batch
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, state
    """
)


@dataclass(frozen=True)
class ReapedJob:
    id: uuid.UUID
    state: JobState


def reap_expired_leases(session: Session, *, batch: int = 100) -> list[ReapedJob]:
    """Return jobs whose lease expired to ``queued`` (with backoff) or ``dead``."""
    with queue_scope(session):
        rows = session.execute(
            _REAP,
            {
                "cap": BACKOFF_CAP_SECONDS,
                "base": BACKOFF_BASE_SECONDS,
                "error": LEASE_EXPIRED_ERROR,
                "batch": batch,
            },
        ).all()
    return [ReapedJob(id=uuid.UUID(str(r.id)), state=JobState(str(r.state))) for r in rows]


_PRUNE = text(
    """
    DELETE FROM job
    WHERE id IN (
        SELECT id FROM job
        WHERE (state = 'succeeded'
               AND updated_at < now() - CAST(:succeeded AS double precision) * interval '1 second')
           OR (state IN ('dead', 'failed')
               AND updated_at < now() - CAST(:dead AS double precision) * interval '1 second')
        LIMIT :batch
        FOR UPDATE SKIP LOCKED
    )
    """
)


def prune_finished(
    session: Session,
    *,
    succeeded_after_seconds: float = 7 * 86400.0,
    dead_after_seconds: float = 30 * 86400.0,
    batch: int = 1000,
) -> int:
    """Delete old finished jobs (succeeded after 7 days, dead/failed after 30)."""
    with queue_scope(session):
        result = session.execute(
            _PRUNE,
            {"succeeded": succeeded_after_seconds, "dead": dead_after_seconds, "batch": batch},
        )
    return _rowcount(result)


_REQUEUE_DEAD = text(
    """
    UPDATE job
    SET state = 'queued', attempts = 0, run_after = now(), last_error = NULL,
        locked_by = NULL, locked_at = NULL, lease_expires_at = NULL, updated_at = now()
    WHERE id = :id AND state IN ('dead', 'failed')
    RETURNING queue
    """
)


def requeue_dead(session: Session, job_id: uuid.UUID) -> bool:
    """Operator action: give a dead-lettered job a fresh set of attempts.

    Runs across households (``queue_scope``): call it only from operator
    tooling, never with an id taken from a household user's request.
    """
    with queue_scope(session):
        queue = session.execute(_REQUEUE_DEAD, {"id": job_id}).scalar_one_or_none()
    if queue is None:
        return False
    session.execute(
        text("SELECT pg_notify(:channel, :queue)"), {"channel": NOTIFY_CHANNEL, "queue": queue}
    )
    return True


def job_states(session: Session, ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, JobState]:
    """States of the given jobs visible in the caller's RLS context.

    Missing ids, and jobs of other households, are omitted.
    """
    id_list = list(ids)
    if not id_list:
        return {}
    rows = session.execute(
        text("SELECT id, state FROM job WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": id_list}
    ).all()
    return {uuid.UUID(str(r.id)): JobState(str(r.state)) for r in rows}
