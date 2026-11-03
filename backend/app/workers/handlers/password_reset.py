# SPDX-License-Identifier: AGPL-3.0-or-later
"""``password_reset_prune``: hourly removal of spent password-reset tokens.

Tokens that expired or were used more than :data:`RETENTION` ago are
deleted (``app.auth.reset.prune_reset_tokens``). ``password_reset_token`` is
not household-scoped, so no RLS context is needed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict

from app.auth.reset import prune_reset_tokens
from app.core import clock
from app.core.logging import get_logger
from app.workers.registry import JobContext, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

RETENTION: Final = timedelta(days=1)
INTERVAL: Final = timedelta(hours=1)


class PasswordResetPrunePayload(BaseModel):
    """No parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@job_handler("password_reset_prune", payload=PasswordResetPrunePayload, timeout=300, max_attempts=3)
def password_reset_prune(ctx: JobContext, payload: PasswordResetPrunePayload) -> None:
    ctx.check_cancelled()
    removed = prune_reset_tokens(ctx.session, older_than=RETENTION, now=clock.utcnow())
    log.info("password reset tokens pruned", extra={"removed": removed})


recurring(
    "password_reset_prune", INTERVAL, PasswordResetPrunePayload(), name="auth.password_reset_prune"
)
