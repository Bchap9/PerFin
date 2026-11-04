# SPDX-License-Identifier: AGPL-3.0-or-later
"""Platform tables: household, job, event, audit_log, setting, idempotency_key.

Row-level security for these tables is declared in ``app.db.rls``:

* ``household`` has forced RLS: a row is readable, updatable and deletable
  only in its own context (an instance admin may also read and create).
  Household-scoped tables cascade from it, and referential actions bypass
  RLS, so the parent row is what keeps one household from deleting another's
  data.
* ``event`` is ``HouseholdScoped`` (forced ``household_isolation`` policy).
* ``audit_log`` has its own policies (household rows in that household's
  context, instance-level rows only for an instance admin), is append-only
  (grants plus triggers) and hash-chained.
* ``setting`` has scope-aware policies (system readable by all, writable by an
  instance admin; household/user settings follow the context).
* ``idempotency_key`` is ``HouseholdScoped`` (``app.core.idempotency``).
* ``job`` has forced RLS: a household context sees only that household's
  jobs; the queue machinery works across households inside
  ``app.db.rls.queue_scope``. A trigger keeps ``id``, ``queue``, ``type``,
  ``payload``, ``household_id``, ``dedupe_key`` and ``created_at`` immutable.
  Code that reads jobs on behalf of a user (``GET /jobs/{id}``) still filters
  on ``household_id`` explicitly (defence in depth).

Household members and roles arrive in Phase 1 (``app.auth.models``).
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    FetchedValue,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.clock import utcnow
from app.core.ids import uuid7
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [str(member.value) for member in enum_cls]


class Household(UUIDPk, Timestamps, Base):
    """The unit of data sharing. Most instances have exactly one."""

    __tablename__ = "household"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    base_currency: Mapped[str] = mapped_column(
        CHAR(3), nullable=False, default="USD", server_default=text("'USD'")
    )


# ---------------------------------------------------------------------------
# job: Postgres queue (ADR-0002)
# ---------------------------------------------------------------------------


class JobState(enum.StrEnum):
    """Life cycle of a job row.

    * ``queued``: waiting; eligible once ``run_after <= now()``.
    * ``running``: leased by ``locked_by`` until ``lease_expires_at``; the
      reaper returns expired leases to ``queued`` (or ``dead``).
    * ``succeeded``: finished.
    * ``failed``: the last attempt failed and the job is not runnable as is
      (terminal unless re-queued explicitly).
    * ``dead``: dead-lettered (``max_attempts`` exhausted or non-retryable).
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    DEAD = "dead"


class Job(UUIDPk, Timestamps, Base):
    """A unit of background work, dequeued with ``FOR UPDATE SKIP LOCKED``.

    Dequeue order is ``priority`` ascending (lower runs first, default 0),
    then ``run_after``, then ``id`` (UUIDv7, i.e. enqueue order); the partial
    index ``ix_job_dequeue`` covers exactly that for ``state = 'queued'``.
    While ``running`` a job must carry ``locked_by`` and ``lease_expires_at``
    (check constraint); ``ix_job_lease_expiry`` serves the reaper.
    ``payload`` is a JSONB *object* (check constraint) validated by the
    handler's Pydantic model and must not contain secrets or document text
    (ids only).
    """

    __tablename__ = "job"

    queue: Mapped[str] = mapped_column(
        String(64), nullable=False, default="default", server_default=text("'default'")
    )
    type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    state: Mapped[JobState] = mapped_column(
        Enum(JobState, name="job_state", values_callable=_enum_values, validate_strings=True),
        nullable=False,
        default=JobState.QUEUED,
        server_default=text("'queued'"),
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    run_after: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default=text("5")
    )
    locked_by: Mapped[str | None] = mapped_column(String(200))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    household_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("household.id", ondelete="CASCADE"), index=True
    )
    # Added by the queue track: optional idempotency key. ``enqueue(...,
    # dedupe_key=...)`` inserts with ON CONFLICT DO NOTHING, so the scheduler
    # (``app.workers.scheduler``) enqueues each recurring slot exactly once
    # even with several worker replicas. NULL for ordinary jobs.
    dedupe_key: Mapped[str | None] = mapped_column(String(200), unique=True)

    __table_args__ = (
        CheckConstraint("attempts >= 0", name="attempts_nonnegative"),
        CheckConstraint("max_attempts >= 1", name="max_attempts_positive"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="payload_is_object"),
        CheckConstraint(
            "state <> 'running' OR (locked_by IS NOT NULL AND lease_expires_at IS NOT NULL)",
            name="running_has_lease",
        ),
        Index(
            "ix_job_dequeue",
            "queue",
            "priority",
            "run_after",
            "id",
            postgresql_where=text("state = 'queued'"),
        ),
        Index(
            "ix_job_lease_expiry",
            "lease_expires_at",
            postgresql_where=text("state = 'running'"),
        ),
        Index("ix_job_state_updated_at", "state", "updated_at"),
    )


# ---------------------------------------------------------------------------
# event: feeds SSE, pruned
# ---------------------------------------------------------------------------


class Event(UUIDPk, Timestamps, HouseholdScoped, Base):
    """A household-visible change notification (``app.core.events.emit``).

    ``payload`` carries ids and small facts only; clients refetch through the
    API. Rows are pruned by ``app.core.events.prune_events``.
    """

    __tablename__ = "event"

    type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        Index("ix_event_household_id_id", "household_id", "id"),
        Index("ix_event_created_at", "created_at"),
    )


# ---------------------------------------------------------------------------
# audit_log: append-only, hash-chained (SECURITY.md I4)
# ---------------------------------------------------------------------------


class AuditLog(Base):
    """One audited action. Written only through ``app.core.audit.audit``.

    ``seq``, ``prev_hash``, ``hash`` and ``created_at`` are assigned by the
    ``audit_log_chain`` trigger (server clock). ``household_id`` and
    ``user_id`` deliberately have no foreign keys: audit rows outlive the
    rows they describe, and a cascade would be an ``UPDATE``/``DELETE`` the
    append-only trigger refuses. There is no ``updated_at``: rows never change.
    """

    __tablename__ = "audit_log"
    # Never fetch trigger-set columns with INSERT ... RETURNING: RETURNING is
    # checked against the SELECT policy, which an instance-level row written
    # outside an instance-admin context does not pass.
    __mapper_args__ = {"eager_defaults": False}  # noqa: RUF012 - SQLAlchemy mapper config

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid7)
    seq: Mapped[int] = mapped_column(
        BigInteger, nullable=False, unique=True, server_default=FetchedValue()
    )
    household_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(100))
    target_id: Mapped[str | None] = mapped_column(String(200))
    before: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    after: Mapped[Any | None] = mapped_column(JSONB(none_as_null=True))
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(String(512))
    request_id: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    prev_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, server_default=FetchedValue())
    hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, server_default=FetchedValue())

    __table_args__ = (
        Index("ix_audit_log_household_id_seq", "household_id", "seq"),
        Index("ix_audit_log_user_id_seq", "user_id", "seq"),
        Index("ix_audit_log_target", "target_type", "target_id"),
    )


# ---------------------------------------------------------------------------
# setting
# ---------------------------------------------------------------------------


class SettingScope(enum.StrEnum):
    SYSTEM = "system"
    HOUSEHOLD = "household"
    USER = "user"


class Setting(UUIDPk, Timestamps, Base):
    """A configuration value at system, household or user scope.

    ``scope_id`` is NULL for ``system`` and the household/user id otherwise
    (check constraint). ``(scope, scope_id, key)`` is unique with NULLs not
    distinct, so a system key exists once. Secret values (OIDC client secret,
    provider keys) are stored encrypted by the caller (``app.core.crypto``),
    never in clear.
    """

    __tablename__ = "setting"

    scope: Mapped[SettingScope] = mapped_column(
        Enum(
            SettingScope, name="setting_scope", values_callable=_enum_values, validate_strings=True
        ),
        nullable=False,
    )
    scope_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True))
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    value: Mapped[Any] = mapped_column(JSONB, nullable=False)

    __table_args__ = (
        UniqueConstraint("scope", "scope_id", "key", postgresql_nulls_not_distinct=True),
        CheckConstraint("(scope = 'system') = (scope_id IS NULL)", name="scope_id_matches_scope"),
    )


# ---------------------------------------------------------------------------
# idempotency_key: replayable responses of creating POSTs (API.md)
# ---------------------------------------------------------------------------


class IdempotencyKey(UUIDPk, HouseholdScoped, Base):
    """A creating ``POST`` answered under an ``Idempotency-Key`` (``app.core.idempotency``).

    Unique per household, user and key. ``request_hash`` is SHA-256 over the
    route and the request (path parameters and body), so reusing a key for
    another request is refused. ``status_code``/``response_body`` hold the
    first answer, written in the same transaction as the change itself: a
    failed request leaves no row and may be retried with the same key.
    ``user_id`` has no foreign key (the row expires after
    ``app.core.idempotency.KEY_TTL`` anyway).
    """

    __tablename__ = "idempotency_key"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer)
    response_body: Mapped[Any] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("household_id", "user_id", "key", name="uq_idempotency_key_scope"),
        Index("ix_idempotency_key_household_expires", "household_id", "expires_at"),
    )
