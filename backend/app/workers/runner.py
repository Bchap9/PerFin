# SPDX-License-Identifier: AGPL-3.0-or-later
"""The worker loop (ADR-0002).

``Worker.run()``:

* starts a listener thread that ``LISTEN``\\s on ``perfin_jobs`` and wakes the
  loops on every notification; the loops also poll every
  ``poll_interval`` seconds (1 s) so a lost connection or notification only
  delays work;
* runs ``concurrency`` loop threads; each claims one job at a time with
  :func:`app.db.queue.dequeue`, validates its payload against the handler's
  model (unknown type or invalid payload: dead-letter), and runs the handler;
* runs maintenance from whichever loop is free: the lease reaper, the
  recurring-job scheduler and pruning of old finished jobs;
* on ``SIGTERM``/``SIGINT`` stops claiming jobs and returns once in-flight
  jobs finish; a second signal also cancels them (cooperatively through
  ``ctx.check_cancelled()``, and by terminating their database connection).

Executing a job: the handler runs in its own thread with its own session and
transaction, in which the runner first sets the RLS context to the job's
household (:func:`app.db.rls.set_context`; none for instance-level jobs) and
caps ``statement_timeout`` at the job timeout. On return the runner marks the
job ``succeeded`` **in that same transaction** and commits, so handler writes
and completion are atomic; if the lease was lost meanwhile everything is
rolled back. If the handler overruns its timeout the runner gives up on it:
it sets ``ctx.cancelled``, terminates the handler's database backend (which
aborts its transaction and releases its locks), records a failed attempt and
moves on. A handler thread that keeps running Python code after that is
"stuck"; when ``max_stuck_handlers`` accumulate the worker exits with
:data:`EXIT_STUCK` so the container is restarted (heavy parsing runs in the
sandboxed extraction subprocess, which has its own hard limits).
"""

from __future__ import annotations

import os
import signal
import socket
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from types import FrameType
from typing import Final

import psycopg
from prometheus_client import Counter, Histogram
from psycopg import sql
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.clock import utcnow
from app.core.logging import bind_request_id, get_logger, scrub_text
from app.core.metrics import REGISTRY as METRICS_REGISTRY
from app.db.models import JobState
from app.db.queue import (
    DEFAULT_HOUSEHOLD_CONCURRENCY,
    NOTIFY_CHANNEL,
    ClaimedJob,
    dequeue,
    mark_failed,
    mark_succeeded,
    prune_finished,
    reap_expired_leases,
    validation_summary,
)
from app.db.rls import set_context
from app.db.session import make_sessionmaker
from app.workers.registry import (
    REGISTRY,
    HandlerRegistry,
    HandlerSpec,
    JobCancelledError,
    JobContext,
    PermanentJobError,
)
from app.workers.scheduler import SCHEDULE, Schedule

log = get_logger(__name__)

EXIT_OK: Final = 0
#: Too many handlers overran their timeout and could not be stopped.
EXIT_STUCK: Final = 3

UNKNOWN_TYPE_ERROR: Final = "unknown job type: no handler is registered"

# Job metrics on the internal metrics port (H7). ``type`` is a registered
# handler name or ``<unknown>`` so a tampered row cannot blow up cardinality.
JOBS_FINISHED = Counter(
    "perfin_jobs",
    "Job attempts finished by the worker, by job type and outcome.",
    ("type", "outcome"),
    registry=METRICS_REGISTRY,
)
JOB_DURATION = Histogram(
    "perfin_job_duration_seconds",
    "Wall-clock time of job handler attempts, by job type.",
    ("type",),
    registry=METRICS_REGISTRY,
    buckets=(0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 15.0, 60.0, 300.0, 900.0, 3600.0),
)
UNKNOWN_TYPE_LABEL: Final = "<unknown>"


def default_worker_id() -> str:
    """``host:pid:random`` identifying this worker process in ``locked_by``."""
    host = socket.gethostname()[:100] or "worker"
    return f"{host}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class WorkerConfig:
    """Tunables of :class:`Worker` (defaults suit a single-household instance)."""

    queues: tuple[str, ...] = ("default",)
    concurrency: int = 1
    poll_interval: float = 1.0
    household_concurrency: int = DEFAULT_HOUSEHOLD_CONCURRENCY
    #: Lease = handler timeout + grace; the reaper requeues after that.
    lease_grace: float = 30.0
    reap_interval: float = 15.0
    schedule_interval: float = 10.0
    prune_interval: float = 3600.0
    max_stuck_handlers: int = 3
    listen: bool = True
    worker_id: str = field(default_factory=default_worker_id)

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be at least 1")
        if self.poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        if not self.queues:
            raise ValueError("at least one queue is required")
        if self.max_stuck_handlers < 1:
            raise ValueError("max_stuck_handlers must be at least 1")


def describe_exception(exc: BaseException) -> str:
    """Class name plus PII-scrubbed message, for ``job.last_error`` and logs."""
    message = scrub_text(str(exc)).strip()
    name = type(exc).__name__
    return f"{name}: {message}" if message else name


class _LeaseLostError(Exception):
    """The claim was reaped or taken over; the handler's work is rolled back."""


@dataclass
class _Execution:
    """Shared state between the loop thread and one handler thread."""

    cancelled: threading.Event = field(default_factory=threading.Event)
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    timed_out: bool = False
    committing: bool = False
    succeeded: bool = False
    lease_lost: bool = False
    error: Exception | None = None
    backend_pid: int | None = None


@dataclass
class _Timer:
    interval: float
    next_at: float = 0.0

    def due(self, now: float) -> bool:
        if now < self.next_at:
            return False
        self.next_at = now + self.interval
        return True


class Worker:
    """Runs jobs from the ``job`` table until stopped."""

    def __init__(
        self,
        engine: Engine,
        *,
        config: WorkerConfig | None = None,
        registry: HandlerRegistry | None = None,
        schedule: Schedule | None = None,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        self.engine = engine
        self.config = config if config is not None else WorkerConfig()
        self.registry = registry if registry is not None else REGISTRY
        self.schedule = schedule if schedule is not None else SCHEDULE
        self.sessions = (
            session_factory if session_factory is not None else make_sessionmaker(engine)
        )
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.exit_code = EXIT_OK
        self._signals = 0
        self._maintenance_lock = threading.Lock()
        self._reap_timer = _Timer(self.config.reap_interval)
        self._schedule_timer = _Timer(self.config.schedule_interval)
        self._prune_timer = _Timer(self.config.prune_interval)
        self._inflight: dict[uuid.UUID, _Execution] = {}
        self._stuck: list[threading.Thread] = []
        self._state_lock = threading.Lock()

    @property
    def worker_id(self) -> str:
        return self.config.worker_id

    # ------------------------------------------------------------------ control

    def stop(self) -> None:
        """Stop claiming jobs; :meth:`run` returns once in-flight jobs finish."""
        self.stop_event.set()
        self.wake_event.set()

    def cancel_inflight(self) -> None:
        """Ask running handlers to stop (``ctx.check_cancelled()`` raises)."""
        with self._state_lock:
            executions = list(self._inflight.values())
        for execution in executions:
            execution.cancelled.set()

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        self._signals += 1
        if self._signals == 1:
            log.info("shutdown requested; finishing in-flight jobs", extra={"signal": signum})
            self.stop()
        else:
            log.warning("second shutdown signal; cancelling in-flight jobs")
            self.cancel_inflight()

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)

    # ------------------------------------------------------------------ main loop

    def run(self, *, install_signal_handlers: bool = False) -> int:
        """Run until :meth:`stop` (or a signal). Returns the process exit code."""
        if install_signal_handlers:
            self.install_signal_handlers()
        log.info(
            "worker starting",
            extra={
                "worker_id": self.worker_id,
                "queues": list(self.config.queues),
                "concurrency": self.config.concurrency,
                "job_types": self.registry.types(),
            },
        )
        listener: threading.Thread | None = None
        if self.config.listen:
            listener = threading.Thread(
                target=self._listen_loop, name="perfin-worker-listen", daemon=True
            )
            listener.start()
        loops = [
            threading.Thread(target=self._loop, name=f"perfin-worker-{i}", daemon=True)
            for i in range(self.config.concurrency)
        ]
        for thread in loops:
            thread.start()
        for thread in loops:
            while thread.is_alive():
                thread.join(timeout=0.5)
        if listener is not None:
            listener.join(timeout=5.0)
        log.info("worker stopped", extra={"worker_id": self.worker_id, "exit": self.exit_code})
        return self.exit_code

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                self.maintenance()
                ran = self.run_once()
            except SQLAlchemyError as exc:
                log.error("worker loop database error", extra={"error": describe_exception(exc)})
                self.stop_event.wait(min(5.0, self.config.poll_interval * 5))
                continue
            except Exception:
                log.exception("unexpected error in worker loop")
                self.stop_event.wait(min(5.0, self.config.poll_interval * 5))
                continue
            if not ran:
                self.wake_event.wait(self.config.poll_interval)
                self.wake_event.clear()

    def maintenance(self, *, force: bool = False) -> None:
        """Reap expired leases, tick the scheduler and prune, when due."""
        if not self._maintenance_lock.acquire(blocking=False):
            return
        try:
            now = time.monotonic()
            if self._reap_timer.due(now) or force:
                self.reap()
            if self._schedule_timer.due(now) or force:
                with self.sessions() as session, session.begin():
                    self.schedule.tick(session, registry=self.registry)
            if self._prune_timer.due(now) or force:
                with self.sessions() as session, session.begin():
                    pruned = prune_finished(session)
                if pruned:
                    log.info("pruned finished jobs", extra={"count": pruned})
        finally:
            self._maintenance_lock.release()

    def reap(self) -> int:
        with self.sessions() as session, session.begin():
            reaped = reap_expired_leases(session)
        for job in reaped:
            log.warning(
                "requeued job with expired lease",
                extra={"job_id": str(job.id), "state": job.state.value},
            )
        return len(reaped)

    def _lease_seconds(self, job_type: str) -> float:
        spec = self.registry.get(job_type)
        timeout = spec.timeout if spec is not None else 0.0
        return timeout + self.config.lease_grace

    def run_once(self) -> bool:
        """Claim and execute at most one job. ``True`` if a job was claimed."""
        if self._too_many_stuck():
            return False
        with self.sessions() as session, session.begin():
            claimed = dequeue(
                session,
                worker_id=self.worker_id,
                queues=self.config.queues,
                lease_seconds=self._lease_seconds,
                household_limit=self.config.household_concurrency,
            )
        if claimed is None:
            return False
        with bind_request_id():
            self.execute(claimed)
        return True

    # ------------------------------------------------------------------ execution

    def execute(self, job: ClaimedJob) -> JobState | None:
        """Validate and run a claimed job; return the state it ended in."""
        fields = {"job_id": str(job.id), "job_type": job.type, "attempt": job.attempts}
        spec = self.registry.get(job.type)
        if spec is None:
            log.error("dead-lettering job of unknown type", extra=fields)
            JOBS_FINISHED.labels(UNKNOWN_TYPE_LABEL, "unknown_type").inc()
            return self._fail(job, UNKNOWN_TYPE_ERROR, permanent=True)
        try:
            payload = spec.validate_payload(job.payload)
        except ValidationError as exc:
            log.error("dead-lettering job with invalid payload", extra=fields)
            JOBS_FINISHED.labels(job.type, "invalid_payload").inc()
            return self._fail(
                job, f"payload validation failed: {validation_summary(exc)}", permanent=True
            )

        started = time.monotonic()
        execution = self._run_handler(spec, job, payload)
        elapsed = time.monotonic() - started
        JOB_DURATION.labels(job.type).observe(elapsed)
        fields["duration_ms"] = int(elapsed * 1000)

        if execution.timed_out:
            log.error("job timed out", extra={**fields, "timeout_s": spec.timeout})
            JOBS_FINISHED.labels(job.type, "timeout").inc()
            return self._fail(job, f"timeout: handler exceeded {spec.timeout:g}s")
        if execution.succeeded:
            log.info("job succeeded", extra=fields)
            JOBS_FINISHED.labels(job.type, "succeeded").inc()
            return JobState.SUCCEEDED
        if execution.lease_lost:
            log.warning("job lease lost; result discarded", extra=fields)
            JOBS_FINISHED.labels(job.type, "lease_lost").inc()
            return None
        error = execution.error
        if error is None:
            JOBS_FINISHED.labels(job.type, "error").inc()
            return self._fail(job, "handler thread ended without a result")
        message = describe_exception(error)
        permanent = isinstance(error, PermanentJobError)
        log.warning(
            "job failed",
            extra={**fields, "error_class": type(error).__name__, "permanent": permanent},
        )
        JOBS_FINISHED.labels(job.type, "permanent_error" if permanent else "error").inc()
        return self._fail(job, message, permanent=permanent)

    def _fail(self, job: ClaimedJob, error: str, *, permanent: bool = False) -> JobState | None:
        with self.sessions() as session, session.begin():
            state = mark_failed(session, job, error, permanent=permanent)
        if state is JobState.DEAD:
            log.error(
                "job dead-lettered",
                extra={"job_id": str(job.id), "job_type": job.type, "attempt": job.attempts},
            )
        return state

    def _run_handler(self, spec: HandlerSpec, job: ClaimedJob, payload: BaseModel) -> _Execution:
        execution = _Execution()
        with self._state_lock:
            self._inflight[job.id] = execution
        if self.stop_event.is_set() and self._signals > 1:
            execution.cancelled.set()
        thread = threading.Thread(
            target=self._handler_thread,
            args=(spec, job, payload, execution),
            name=f"perfin-job-{job.id}",
            daemon=True,
        )
        try:
            thread.start()
            if not execution.done.wait(spec.timeout):
                with execution.lock:
                    if not execution.committing:
                        execution.timed_out = True
                if execution.timed_out:
                    execution.cancelled.set()
                    self._terminate_backend(execution.backend_pid)
                    with self._state_lock:
                        self._stuck.append(thread)
                else:
                    # Already committing: the commit is short; wait for its outcome.
                    execution.done.wait(self.config.lease_grace)
        finally:
            with self._state_lock:
                self._inflight.pop(job.id, None)
        return execution

    def _handler_thread(
        self, spec: HandlerSpec, job: ClaimedJob, payload: BaseModel, execution: _Execution
    ) -> None:
        try:
            with self.sessions() as session:
                with session.begin():
                    execution.backend_pid = int(
                        session.execute(text("SELECT pg_backend_pid()")).scalar_one()
                    )
                    set_context(session, job.household_id)
                    session.execute(
                        text("SELECT set_config('statement_timeout', :ms, true)"),
                        {"ms": str(max(1, int(spec.timeout * 1000)))},
                    )
                    ctx = JobContext(
                        job_id=job.id,
                        job_type=job.type,
                        attempt=job.attempts,
                        max_attempts=job.max_attempts,
                        household_id=job.household_id,
                        session=session,
                        deadline=utcnow() + timedelta(seconds=spec.timeout),
                        cancelled=execution.cancelled,
                    )
                    spec.func(ctx, payload)
                    with execution.lock:
                        if execution.timed_out:
                            raise JobCancelledError("timed out")
                        execution.committing = True
                    if not mark_succeeded(session, job):
                        raise _LeaseLostError
                execution.succeeded = True
        except _LeaseLostError:
            execution.lease_lost = True
        except Exception as exc:  # noqa: BLE001 - recorded on the job and logged by execute()
            execution.error = exc
        finally:
            execution.done.set()

    def _terminate_backend(self, pid: int | None) -> None:
        """End a timed-out handler's DB session: aborts its transaction, frees locks."""
        if pid is None:
            return
        try:
            with self.engine.connect() as conn:
                conn.execute(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})
                conn.commit()
        except SQLAlchemyError as exc:
            log.error(
                "could not terminate timed-out job connection",
                extra={"error": describe_exception(exc)},
            )

    def _too_many_stuck(self) -> bool:
        with self._state_lock:
            self._stuck = [t for t in self._stuck if t.is_alive()]
            stuck = len(self._stuck)
        if stuck >= self.config.max_stuck_handlers:
            if self.exit_code == EXIT_OK:
                log.critical(
                    "too many job handlers overran their timeout and are still running; "
                    "stopping the worker so it can be restarted",
                    extra={"stuck": stuck},
                )
            self.exit_code = EXIT_STUCK
            self.stop()
            return True
        return False

    # ------------------------------------------------------------------ LISTEN

    def _listen_loop(self) -> None:
        conninfo = self.engine.url.set(drivername="postgresql").render_as_string(
            hide_password=False
        )
        while not self.stop_event.is_set():
            try:
                with psycopg.connect(conninfo, autocommit=True, connect_timeout=10) as conn:
                    conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(NOTIFY_CHANNEL)))
                    log.debug("listening for job notifications")
                    while not self.stop_event.is_set():
                        for _notify in conn.notifies(timeout=1.0):
                            self.wake_event.set()
            except psycopg.Error as exc:
                log.warning(
                    "job notification listener lost its connection; polling meanwhile",
                    extra={"error": describe_exception(exc)},
                )
                self.stop_event.wait(5.0)


def run_worker(
    engine: Engine,
    *,
    config: WorkerConfig | None = None,
    install_signal_handlers: bool = True,
    on_start: Callable[[Worker], None] | None = None,
) -> int:
    """Build a :class:`Worker` over the process-wide registry and run it."""
    worker = Worker(engine, config=config)
    if on_start is not None:
        on_start(worker)
    return worker.run(install_signal_handlers=install_signal_handlers)
