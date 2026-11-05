# SPDX-License-Identifier: AGPL-3.0-or-later
"""``noop``: a job that does nothing (Phase 0 smoke test of the queue).

Useful to check a deployment end to end: enqueue ``noop`` and watch it reach
``succeeded``. It touches no household data.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.core.logging import get_logger
from app.workers.registry import JobContext, job_handler

log = get_logger(__name__)


class NoopPayload(BaseModel):
    """Payload for ``noop``: an optional short label echoed to the log."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(default="", max_length=100, pattern=r"^[A-Za-z0-9 _.:-]*$")


@job_handler("noop", payload=NoopPayload, timeout=30, max_attempts=3)
def noop(ctx: JobContext, payload: NoopPayload) -> None:
    """Do nothing, successfully."""
    ctx.check_cancelled()
    log.info(
        "noop job ran",
        extra={"job_id": str(ctx.job_id), "attempt": ctx.attempt, "label": payload.label},
    )
