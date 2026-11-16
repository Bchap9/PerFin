# SPDX-License-Identifier: AGPL-3.0-or-later
"""Categorisation: where it stands and what was learned (``/categorisation``).

* ``GET /categorisation/summary``: any role; versions of the bundled
  dictionary and clean-up, and how the visible transactions got their
  categories.
* ``GET /categorisation/learned?cursor=&limit=``: any role; what the
  household's own corrections say about each merchant (at most 200 a page).
* ``DELETE /categorisation/learned/{id}``: member or above; forget one
  merchant (``204``; ``404 memory-not-found``).
* ``POST /categorisation/refresh``: owner or admin; queues a job that looks
  again at the rows nobody decided and answers ``202`` with its id (poll
  ``GET /jobs/{id}``).

Tokens need ``transactions:read`` / ``transactions:write``. Rules:
``app.domain.categorisation.service``. Nothing here leaves the host.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.domain.categorisation import service
from app.domain.categorisation.schemas import (
    DEFAULT_LEARNED,
    MAX_LEARNED,
    CategorisationSummary,
    LearnedCategoryPage,
)
from app.domain.rules.schemas import JobAccepted

router = APIRouter(tags=["categorisation"])

Reader = Annotated[Principal, Depends(scoped_role("transactions:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("transactions:write", HouseholdRole.MEMBER))]
Manager = Annotated[Principal, Depends(scoped_role("transactions:write", HouseholdRole.ADMIN))]


def require_memory_writer(memory_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the entry must be this household's (``404`` first)."""
    service.require_memory(db, principal, memory_id)
    return principal


MemoryWriter = Annotated[Principal, Depends(require_memory_writer)]


@router.get(
    "/categorisation/summary",
    response_model=CategorisationSummary,
    responses=PROBLEMS,
    summary="How my transactions got their categories",
)
def get_summary(db: DbSession, principal: Reader) -> CategorisationSummary:
    return service.summary(db, principal)


@router.get(
    "/categorisation/learned",
    response_model=LearnedCategoryPage,
    responses=PROBLEMS,
    summary="What PerFin learned from the household's corrections",
)
def get_learned(
    db: DbSession,
    principal: Reader,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_LEARNED)] = DEFAULT_LEARNED,
) -> LearnedCategoryPage:
    return service.list_learned(db, principal, cursor=cursor, limit=limit)


@router.delete(
    "/categorisation/learned/{memory_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Forget what was learned about one merchant (member or above)",
)
def delete_learned(
    memory_id: uuid.UUID, request: Request, db: DbSession, principal: MemoryWriter
) -> Response:
    service.forget(db, principal, memory_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/categorisation/refresh",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses=PROBLEMS,
    summary="Look again at the transactions nobody decided, as a job (owner/admin)",
)
def post_refresh(request: Request, db: DbSession, principal: Manager) -> JobAccepted:
    return service.request_refresh(db, principal, request=request)
