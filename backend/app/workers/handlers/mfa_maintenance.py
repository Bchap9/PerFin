# SPDX-License-Identifier: AGPL-3.0-or-later
"""``mfa_prune``: hourly deletion of expired ``mfa_challenge`` rows.

WebAuthn challenges and pending TOTP enrolments live minutes; rows whose
session ended are already gone (``ON DELETE CASCADE``). The table is not
household-scoped, so no RLS context is needed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.auth.mfa.challenges import prune
from app.core.logging import get_logger
from app.workers.registry import JobContext, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

INTERVAL: Final = timedelta(hours=1)


class MfaPrunePayload(BaseModel):
    """No parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@job_handler("mfa_prune", payload=MfaPrunePayload, timeout=120, max_attempts=3)
def mfa_prune(ctx: JobContext, payload: MfaPrunePayload) -> None:
    ctx.check_cancelled()
    removed = prune(ctx.session)
    log.info("expired mfa challenges pruned", extra={"mfa_challenges": removed})


recurring("mfa_prune", INTERVAL, MfaPrunePayload(), name="mfa.prune")
