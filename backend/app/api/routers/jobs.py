# SPDX-License-Identifier: AGPL-3.0-or-later
"""Background job status (``GET /jobs/{id}``; rules in ``app.domain.jobs.service``).

Any role in the household that queued the job; a job of another household
(or an instance-level job) is ``404 job-not-found``. Tokens need the read
scope of the job's family (``transactions:read`` for ``run_rules``).
Returns the state only, never the payload or error text.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.routers.accounts import PROBLEMS
from app.auth.authz import require_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.domain.jobs import service
from app.domain.jobs.schemas import JobOut

router = APIRouter(tags=["jobs"])

Reader = Annotated[Principal, Depends(require_role(HouseholdRole.VIEWER))]


@router.get("/jobs/{job_id}", response_model=JobOut, responses=PROBLEMS, summary="Job status")
def get_job(job_id: uuid.UUID, db: DbSession, principal: Reader) -> JobOut:
    return service.get_job(db, principal, job_id)
