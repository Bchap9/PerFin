# SPDX-License-Identifier: AGPL-3.0-or-later
"""Job handler registry (ADR-0002).

Handlers live in modules under ``app/workers/handlers/`` and register with the
:func:`job_handler` decorator::

    class RefreshPayload(BaseModel):
        model_config = ConfigDict(extra="forbid")
        provider: str

    @job_handler("refresh_prices", payload=RefreshPayload, timeout=120, max_attempts=3)
    def refresh_prices(ctx: JobContext, payload: RefreshPayload) -> None: ...

The runner calls :func:`discover_handlers` to import every handler module. A
payload is validated against the declared model when the job is enqueued and
again when it is dequeued; a job whose type is not registered, or whose stored
payload no longer validates, goes straight to the dead-letter state.

Handler contract:

* ``ctx.session`` is a SQLAlchemy ``Session`` inside the job's transaction,
  with the RLS context already set to the job's household (none for
  instance-level jobs) and ``statement_timeout`` capped at the job timeout.
  The runner commits it together with the job's ``succeeded`` state, so the
  handler's writes and the job completion are atomic. Handlers never commit.
* Raise :class:`PermanentJobError` for failures that a retry cannot fix
  (dead-lettered at once); any other exception is retried with backoff.
* Long-running handlers call ``ctx.check_cancelled()`` between steps; after
  the timeout (or on shutdown past the deadline) it raises
  :class:`JobCancelledError`.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Final, Protocol

from pydantic import BaseModel, RootModel

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

#: Default per-job wall-clock limit in seconds (SECURITY.md C10).
DEFAULT_TIMEOUT_SECONDS: Final = 300.0
#: Hard upper bound on any handler timeout: a job lease never exceeds it.
MAX_TIMEOUT_SECONDS: Final = 6 * 3600.0
DEFAULT_MAX_ATTEMPTS: Final = 5
MAX_ATTEMPTS_LIMIT: Final = 50

#: Job type names: lower-case words joined by ``_`` or ``.``, at most 64 chars.
JOB_TYPE_RE: Final = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
JOB_TYPE_MAX_LEN: Final = 64

HANDLERS_PACKAGE: Final = "app.workers.handlers"


class JobError(Exception):
    """Base class for job execution errors raised by handlers."""


class PermanentJobError(JobError):
    """A failure that retrying cannot fix: the job is dead-lettered at once."""


class JobCancelledError(JobError):
    """Raised by :meth:`JobContext.check_cancelled` after the job's deadline."""


class UnknownJobTypeError(LookupError):
    """No handler is registered for a job type."""


class DuplicateJobHandlerError(ValueError):
    """Two handlers were registered for the same job type."""


def validate_job_type(job_type: str) -> str:
    """Return ``job_type`` if it is a well-formed type name, else raise ``ValueError``."""
    if len(job_type) > JOB_TYPE_MAX_LEN or not JOB_TYPE_RE.fullmatch(job_type):
        raise ValueError(f"invalid job type name: {job_type!r}")
    return job_type


@dataclass
class JobContext:
    """What a handler gets besides its payload."""

    job_id: uuid.UUID
    job_type: str
    attempt: int
    max_attempts: int
    household_id: uuid.UUID | None
    session: Session
    deadline: datetime
    cancelled: threading.Event = field(default_factory=threading.Event)

    def check_cancelled(self) -> None:
        """Raise :class:`JobCancelledError` if the runner has given up on this job."""
        if self.cancelled.is_set():
            raise JobCancelledError("job cancelled (timeout or shutdown)")


class HandlerFunc[P: BaseModel](Protocol):
    def __call__(self, ctx: JobContext, payload: P, /) -> None: ...


@dataclass(frozen=True)
class HandlerSpec:
    """A registered handler and its execution policy."""

    job_type: str
    payload_model: type[BaseModel]
    func: Callable[[JobContext, Any], None]
    timeout: float
    max_attempts: int

    def validate_payload(self, data: Any) -> BaseModel:
        """Validate stored JSON (or a model instance) against the declared model."""
        if isinstance(data, BaseModel):
            data = data.model_dump(mode="json")
        return self.payload_model.model_validate(data)


class HandlerRegistry:
    """Maps job types to :class:`HandlerSpec`. The process-wide one is :data:`REGISTRY`."""

    def __init__(self) -> None:
        self._specs: dict[str, HandlerSpec] = {}
        self._lock = threading.Lock()

    def register(
        self,
        job_type: str,
        func: Callable[[JobContext, Any], None],
        *,
        payload: type[BaseModel],
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> HandlerSpec:
        validate_job_type(job_type)
        if not (isinstance(payload, type) and issubclass(payload, BaseModel)):
            raise TypeError("payload must be a pydantic BaseModel subclass")
        if issubclass(payload, RootModel):
            # job.payload must be a JSON object (check constraint); a RootModel
            # dumps to a list or scalar.
            raise TypeError("payload must be a model with fields, not a RootModel")
        if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
            raise ValueError(f"timeout must be in (0, {MAX_TIMEOUT_SECONDS:g}] seconds")
        if not 1 <= max_attempts <= MAX_ATTEMPTS_LIMIT:
            raise ValueError(f"max_attempts must be in [1, {MAX_ATTEMPTS_LIMIT}]")
        spec = HandlerSpec(
            job_type=job_type,
            payload_model=payload,
            func=func,
            timeout=float(timeout),
            max_attempts=max_attempts,
        )
        with self._lock:
            existing = self._specs.get(job_type)
            if existing is not None and existing.func is not func:
                raise DuplicateJobHandlerError(f"job type {job_type!r} is already registered")
            self._specs[job_type] = spec
        return spec

    def job_handler[P: BaseModel](
        self,
        job_type: str,
        *,
        payload: type[P],
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    ) -> Callable[[HandlerFunc[P]], HandlerFunc[P]]:
        """Decorator registering ``func`` as the handler for ``job_type``."""

        def decorate(func: HandlerFunc[P]) -> HandlerFunc[P]:
            self.register(
                job_type, func, payload=payload, timeout=timeout, max_attempts=max_attempts
            )
            return func

        return decorate

    def get(self, job_type: str) -> HandlerSpec | None:
        with self._lock:
            return self._specs.get(job_type)

    def require(self, job_type: str) -> HandlerSpec:
        spec = self.get(job_type)
        if spec is None:
            raise UnknownJobTypeError(job_type)
        return spec

    def unregister(self, job_type: str) -> None:
        with self._lock:
            self._specs.pop(job_type, None)

    def types(self) -> list[str]:
        with self._lock:
            return sorted(self._specs)

    def __contains__(self, job_type: object) -> bool:
        with self._lock:
            return job_type in self._specs

    def __iter__(self) -> Iterator[HandlerSpec]:
        with self._lock:
            specs = list(self._specs.values())
        return iter(sorted(specs, key=lambda s: s.job_type))

    def __len__(self) -> int:
        with self._lock:
            return len(self._specs)


#: The process-wide registry used by ``@job_handler``, the runner and ``enqueue``.
REGISTRY: Final = HandlerRegistry()


def job_handler[P: BaseModel](
    job_type: str,
    *,
    payload: type[P],
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Callable[[HandlerFunc[P]], HandlerFunc[P]]:
    """Register a handler in :data:`REGISTRY` (see the module docstring)."""
    return REGISTRY.job_handler(
        job_type, payload=payload, timeout=timeout, max_attempts=max_attempts
    )


def discover_handlers(package: str = HANDLERS_PACKAGE) -> list[str]:
    """Import every module under ``package`` so their handlers register.

    Returns the imported module names (sorted). Sub-packages are walked too.
    """
    root = importlib.import_module(package)
    imported: list[str] = []
    for info in sorted(
        pkgutil.walk_packages(root.__path__, prefix=f"{package}."), key=lambda i: i.name
    ):
        importlib.import_module(info.name)
        imported.append(info.name)
    return imported
