# SPDX-License-Identifier: AGPL-3.0-or-later
"""``run_rules``: re-apply the household's rules to existing transactions.

Queued by ``POST /rules/apply`` (owner/admin) through
:func:`app.domain.rules.service.request_apply`; runs in the job's household
context over every account, in locked batches, checking for cancellation
between batches. Semantics: ``app.domain.rules.engine``.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.domain.rules import service
from app.domain.rules.schemas import RUN_RULES_JOB_TYPE, RunRulesJob
from app.workers.registry import JobContext, PermanentJobError, job_handler

log = get_logger(__name__)


@job_handler(RUN_RULES_JOB_TYPE, payload=RunRulesJob, timeout=900.0, max_attempts=3)
def run_rules(ctx: JobContext, payload: RunRulesJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("run_rules needs a household")
    summary = service.run_rules(
        ctx.session,
        ctx.household_id,
        payload,
        job_id=ctx.job_id,
        check_cancelled=ctx.check_cancelled,
    )
    log.info(
        "rules applied",
        extra={"scanned": summary.scanned, "matched": summary.matched, "changed": summary.changed},
    )
