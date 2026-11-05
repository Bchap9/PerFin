# SPDX-License-Identifier: AGPL-3.0-or-later
"""Job status for API callers (``GET /jobs/{id}``).

A job is visible to every member of the household that queued it (the
explicit ``household_id`` filter, with ``job``'s forced RLS as backstop);
anything else, including instance-level jobs, is ``404 job-not-found``.

Personal access tokens additionally need the read scope of the job's
family (:data:`JOB_READ_SCOPES`); a token cannot read jobs of a family that
is not listed there. Browser sessions read every job of their household.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.authz import forbidden
from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.db.models import Job, JobState
from app.domain.jobs.schemas import JobOut

#: Token scope needed to read a job, by job type.
JOB_READ_SCOPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        "run_rules": "transactions:read",
        "transactions.auto_pair": "transactions:read",
        "ingest_document": "documents:read",
        "imports.apply_mapping": "documents:read",
    }
)

FINISHED: Final = frozenset({JobState.SUCCEEDED, JobState.FAILED, JobState.DEAD})


def job_not_found() -> ProblemError:
    return ProblemError(404, "job-not-found", "Job not found")


def get_job(db: Session, principal: Principal, job_id: uuid.UUID) -> JobOut:
    household_id = principal.household_id
    if household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    job = db.execute(
        select(Job).where(Job.id == job_id, Job.household_id == household_id)
    ).scalar_one_or_none()
    if job is None:
        raise job_not_found()
    if principal.scopes is not None:
        scope = JOB_READ_SCOPES.get(job.type)
        if scope is None or not principal.has_scope(scope):
            raise ProblemError(
                403,
                "insufficient-scope",
                "Insufficient scope",
                f"This token lacks the {scope!r} scope."
                if scope
                else "Tokens cannot read this job.",
            )
    return JobOut(
        id=job.id,
        type=job.type,
        state=job.state,
        attempts=job.attempts,
        max_attempts=job.max_attempts,
        finished=job.state in FINISHED,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )
