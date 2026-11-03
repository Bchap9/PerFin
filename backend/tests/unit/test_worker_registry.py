# SPDX-License-Identifier: AGPL-3.0-or-later
"""Job handler registry, backoff, scheduler slots and worker config (no database)."""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app.cli import build_parser, cmd_worker
from app.db.queue import (
    BACKOFF_BASE_SECONDS,
    BACKOFF_CAP_SECONDS,
    LAST_ERROR_MAX_CHARS,
    compute_backoff,
    truncate_error,
    validation_summary,
)
from app.workers.handlers.noop import NoopPayload
from app.workers.registry import (
    MAX_TIMEOUT_SECONDS,
    REGISTRY,
    DuplicateJobHandlerError,
    HandlerRegistry,
    JobCancelledError,
    JobContext,
    UnknownJobTypeError,
    discover_handlers,
    validate_job_type,
)
from app.workers.runner import WorkerConfig, default_worker_id, describe_exception
from app.workers.scheduler import RecurringJob, Schedule


class Thing(BaseModel):
    model_config = ConfigDict(extra="forbid")
    thing_id: uuid.UUID
    count: int = Field(ge=0, le=10)


def _noop_handler(_ctx: JobContext, _payload: Thing) -> None:
    return None


# --------------------------------------------------------------------------- registry


def test_decorator_registers_spec_with_policy() -> None:
    registry = HandlerRegistry()

    @registry.job_handler("thing.process", payload=Thing, timeout=12.5, max_attempts=2)
    def handle(ctx: JobContext, payload: Thing) -> None:
        del ctx, payload

    spec = registry.require("thing.process")
    assert spec.func is handle
    assert spec.payload_model is Thing
    assert spec.timeout == 12.5
    assert spec.max_attempts == 2
    assert "thing.process" in registry
    assert registry.types() == ["thing.process"]
    assert len(registry) == 1
    assert [s.job_type for s in registry] == ["thing.process"]


def test_defaults_and_unknown_lookup() -> None:
    registry = HandlerRegistry()
    spec = registry.register("plain", _noop_handler, payload=Thing)
    assert spec.timeout == 300.0
    assert spec.max_attempts == 5
    assert registry.get("missing") is None
    with pytest.raises(UnknownJobTypeError):
        registry.require("missing")
    registry.unregister("plain")
    assert "plain" not in registry


def test_duplicate_type_is_rejected_but_reregistering_same_function_is_idempotent() -> None:
    registry = HandlerRegistry()
    registry.register("dup", _noop_handler, payload=Thing)
    registry.register("dup", _noop_handler, payload=Thing)

    def other(_ctx: JobContext, _payload: Thing) -> None:
        return None

    with pytest.raises(DuplicateJobHandlerError):
        registry.register("dup", other, payload=Thing)


@pytest.mark.parametrize(
    "name",
    ["", "Upper", "1starts_digit", "has space", "semi;colon", "a" * 65, "trailing.", "a..b"],
)
def test_invalid_type_names_rejected(name: str) -> None:
    with pytest.raises(ValueError, match="invalid job type"):
        validate_job_type(name)
    with pytest.raises(ValueError, match="invalid job type"):
        HandlerRegistry().register(name, _noop_handler, payload=Thing)


@pytest.mark.parametrize("name", ["noop", "ingest_document", "prices.refresh", "a1_b2.c3"])
def test_valid_type_names(name: str) -> None:
    assert validate_job_type(name) == name


@pytest.mark.parametrize(
    ("timeout", "max_attempts"),
    [(0, 5), (-1, 5), (MAX_TIMEOUT_SECONDS + 1, 5), (10, 0), (10, 51)],
)
def test_policy_bounds(timeout: float, max_attempts: int) -> None:
    with pytest.raises(ValueError, match="must be"):
        HandlerRegistry().register(
            "bounded", _noop_handler, payload=Thing, timeout=timeout, max_attempts=max_attempts
        )


def test_payload_must_be_a_model_class() -> None:
    with pytest.raises(TypeError):
        HandlerRegistry().register("bad", _noop_handler, payload=cast(Any, dict))


def test_validate_payload_from_json_and_from_instance() -> None:
    registry = HandlerRegistry()
    spec = registry.register("thing", _noop_handler, payload=Thing)
    thing_id = uuid.uuid4()
    validated = spec.validate_payload({"thing_id": str(thing_id), "count": 3})
    assert validated == Thing(thing_id=thing_id, count=3)
    # A model_construct-ed instance skips validation; validate_payload does not.
    sneaky = Thing.model_construct(thing_id=thing_id, count=99)
    with pytest.raises(ValidationError):
        spec.validate_payload(sneaky)
    with pytest.raises(ValidationError):
        spec.validate_payload({"thing_id": str(thing_id), "count": 1, "extra": "x"})


def test_validation_summary_never_echoes_input() -> None:
    secret_value = "4111111111111111"
    with pytest.raises(ValidationError) as info:
        Thing.model_validate({"thing_id": secret_value, "count": "nope"})
    summary = validation_summary(info.value)
    assert "thing_id" in summary
    assert "count" in summary
    assert secret_value not in summary
    assert "nope" not in summary


def test_discover_imports_noop_handler() -> None:
    modules = discover_handlers()
    assert "app.workers.handlers.noop" in modules
    spec = REGISTRY.require("noop")
    assert spec.payload_model is NoopPayload
    assert spec.timeout == 30
    assert spec.max_attempts == 3


def test_noop_payload_is_strict() -> None:
    assert NoopPayload().label == ""
    with pytest.raises(ValidationError):
        NoopPayload.model_validate({"label": "x", "unexpected": 1})
    with pytest.raises(ValidationError):
        NoopPayload.model_validate({"label": "<script>"})


def test_job_context_cancellation() -> None:
    ctx = JobContext(
        job_id=uuid.uuid4(),
        job_type="noop",
        attempt=1,
        max_attempts=3,
        household_id=None,
        session=cast(Session, object()),
        deadline=datetime.now(UTC),
        cancelled=threading.Event(),
    )
    ctx.check_cancelled()
    ctx.cancelled.set()
    with pytest.raises(JobCancelledError):
        ctx.check_cancelled()


# --------------------------------------------------------------------------- backoff


def test_backoff_is_exponential_with_equal_jitter() -> None:
    def low(a: float, _b: float) -> float:
        return a

    def high(_a: float, b: float) -> float:
        return b

    for attempt in range(1, 8):
        nominal = min(BACKOFF_CAP_SECONDS, BACKOFF_BASE_SECONDS * 2 ** (attempt - 1))
        assert compute_backoff(attempt, rand=low) == pytest.approx(nominal / 2)
        assert compute_backoff(attempt, rand=high) == pytest.approx(nominal)


def test_backoff_is_capped_and_random_within_bounds() -> None:
    assert compute_backoff(10_000, rand=lambda _a, b: b) == pytest.approx(BACKOFF_CAP_SECONDS)
    samples = {compute_backoff(4) for _ in range(50)}
    nominal = BACKOFF_BASE_SECONDS * 8
    assert all(nominal / 2 <= s <= nominal for s in samples)
    assert len(samples) > 1  # jitter actually varies
    assert compute_backoff(0, base=2, cap=100, rand=lambda a, _b: a) == pytest.approx(1.0)


def test_error_text_is_truncated_and_scrubbed() -> None:
    assert len(truncate_error("x" * 10_000)) == LAST_ERROR_MAX_CHARS
    assert truncate_error("short") == "short"
    described = describe_exception(RuntimeError("card 4111 1111 1111 1111 rejected"))
    assert described.startswith("RuntimeError: ")
    assert "4111 1111 1111 1111" not in described
    assert describe_exception(ValueError()) == "ValueError"


# --------------------------------------------------------------------------- scheduler


def test_recurring_slot_and_dedupe_key() -> None:
    job = RecurringJob(
        name="nightly", job_type="noop", interval=timedelta(hours=1), payload=NoopPayload()
    )
    now = datetime(2026, 11, 3, 15, 42, 7, tzinfo=UTC)
    slot = job.slot(now)
    assert job.slot_start(slot) == datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    assert job.slot(now + timedelta(minutes=17)) == slot
    assert job.slot(now + timedelta(minutes=18)) == slot + 1
    assert job.dedupe_key(slot) == f"sched:nightly:3600:{slot}"


def test_schedule_validation() -> None:
    schedule = Schedule()
    schedule.add("noop", timedelta(minutes=5), NoopPayload())
    assert [j.name for j in schedule.jobs()] == ["noop"]
    schedule.add("noop", timedelta(minutes=5), NoopPayload())  # identical: idempotent
    with pytest.raises(ValueError, match="already registered"):
        schedule.add("noop", timedelta(minutes=10), NoopPayload())
    with pytest.raises(ValueError, match="interval"):
        schedule.add("noop", timedelta(seconds=30), NoopPayload(), name="too_fast")
    with pytest.raises(ValueError, match="interval"):
        schedule.add("noop", timedelta(seconds=90.5), NoopPayload(), name="fractional")
    with pytest.raises(ValueError, match="name"):
        schedule.add("noop", timedelta(minutes=5), NoopPayload(), name="Bad Name")
    with pytest.raises(TypeError):
        schedule.add("noop", timedelta(minutes=5), cast(Any, {"label": ""}), name="dict")
    schedule.remove("noop")
    assert schedule.jobs() == []


# --------------------------------------------------------------------------- runner config / CLI


def test_worker_config_validation() -> None:
    assert WorkerConfig().queues == ("default",)
    for kwargs in (
        {"concurrency": 0},
        {"poll_interval": 0},
        {"queues": ()},
        {"max_stuck_handlers": 0},
    ):
        with pytest.raises(ValueError, match=r"must be|required"):
            WorkerConfig(**cast(Any, kwargs))


def test_worker_ids_are_unique() -> None:
    first, second = default_worker_id(), default_worker_id()
    assert first != second
    assert len(first) <= 200


def test_cli_worker_subcommand_is_wired() -> None:
    args = build_parser().parse_args(["worker"])
    assert args.func is cmd_worker
    assert not getattr(args.func, "perfin_unavailable", False)
