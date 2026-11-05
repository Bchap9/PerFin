# SPDX-License-Identifier: AGPL-3.0-or-later
"""``auth_prune``: hourly clean-up of identity bookkeeping.

* ``user_session`` rows that ended (expired or revoked) more than
  :data:`SESSION_RETENTION` ago; the list/revoke UI only shows live ones.
* ``rate_limit_event`` rows older than the longest default window
  (``app.auth.ratelimit``); each key's own window is also trimmed on use.

Neither table is household-scoped, so no RLS context is needed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete

from app.auth.models import RateLimitEvent
from app.auth.ratelimit import DEFAULT_LIMITS
from app.auth.sessions import prune_sessions
from app.core import clock
from app.core.logging import get_logger
from app.workers.registry import JobContext, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

SESSION_RETENTION: Final = timedelta(days=7)
INTERVAL: Final = timedelta(hours=1)


class AuthPrunePayload(BaseModel):
    """No parameters."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@job_handler("auth_prune", payload=AuthPrunePayload, timeout=300, max_attempts=3)
def auth_prune(ctx: JobContext, payload: AuthPrunePayload) -> None:
    ctx.check_cancelled()
    now = clock.utcnow()
    sessions = prune_sessions(ctx.session, older_than=SESSION_RETENTION, now=now)
    horizon = max(limit.window for limit in DEFAULT_LIMITS)
    events = ctx.session.execute(
        delete(RateLimitEvent).where(RateLimitEvent.created_at < now - horizon)
    )
    log.info(
        "auth bookkeeping pruned",
        extra={"sessions": sessions, "rate_limit_events": int(events.rowcount)},  # type: ignore[attr-defined]
    )


recurring("auth_prune", INTERVAL, AuthPrunePayload(), name="auth.prune")
