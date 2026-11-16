# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Inbox count (``/inbox``).

* ``GET /inbox/summary``: any role; how many documents, loose review items
  and subscription series wait on the caller, the categorisation summary's
  suggestions and uncategorised rows, and how many Inbox cards that makes.
  Every count is limited to what the caller may see.

Tokens need ``review:read``. Rules: ``app.domain.inbox.service``. Nothing
here writes or leaves the host.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.domain.inbox import service
from app.domain.inbox.schemas import InboxSummary

router = APIRouter(tags=["inbox"])

Reader = Annotated[Principal, Depends(scoped_role("review:read", HouseholdRole.VIEWER))]


@router.get(
    "/inbox/summary",
    response_model=InboxSummary,
    responses=PROBLEMS,
    summary="What waits on a person",
)
def get_summary(db: DbSession, principal: Reader) -> InboxSummary:
    return service.summary(db, principal)
