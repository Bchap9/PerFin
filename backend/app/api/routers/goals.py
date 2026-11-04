# SPDX-License-Identifier: AGPL-3.0-or-later
"""Savings goals (API.md ``/goals``; rules in ``app.domain.goals.service``).

* ``GET /goals?cursor=&limit=&archived=``, ``GET /goals/{id}``: any role;
  only goals whose accounts the caller may all see, with progress and
  projected completion.
* ``POST /goals`` (``Idempotency-Key`` honoured), ``PATCH /goals/{id}``,
  ``DELETE /goals/{id}``: member or above.
* ``GET /goals/{id}/contributions``: any role; ``POST`` (``Idempotency-Key``
  honoured) and ``DELETE /goals/{id}/contributions/{contribution_id}``:
  member or above.

Tokens need ``budgets:read`` / ``budgets:write``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.idempotency import IdempotencyKeyHeader, idempotent, payload_of
from app.domain.goals import service
from app.domain.goals.schemas import (
    DEFAULT_PAGE,
    MAX_PAGE,
    ContributionCreate,
    ContributionOut,
    ContributionPage,
    GoalCreate,
    GoalOut,
    GoalPage,
    GoalUpdate,
)

router = APIRouter(tags=["goals"])

Reader = Annotated[Principal, Depends(scoped_role("budgets:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("budgets:write", HouseholdRole.MEMBER))]
Cursor = Annotated[uuid.UUID | None, Query()]
Limit = Annotated[int, Query(ge=1, le=MAX_PAGE)]


def require_goal_writer(goal_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the goal must be visible (``404`` before the body is read)."""
    service.require_goal(db, principal, goal_id)
    return principal


GoalWriter = Annotated[Principal, Depends(require_goal_writer)]


@router.get("/goals", response_model=GoalPage, summary="Savings goals I can see")
def get_goals(
    db: DbSession,
    principal: Reader,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_PAGE,
    archived: Annotated[bool | None, Query()] = None,
) -> GoalPage:
    return service.list_goals(db, principal, cursor=cursor, limit=limit, archived=archived)


@router.post(
    "/goals",
    response_model=GoalOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Create a savings goal (member or above)",
)
def post_goal(
    body: GoalCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /goals",
        payload=payload_of(body),
        run=lambda: service.create_goal(db, principal, body, request=request),
    )


@router.get("/goals/{goal_id}", response_model=GoalOut, responses=PROBLEMS, summary="One goal")
def get_goal(goal_id: uuid.UUID, db: DbSession, principal: Reader) -> GoalOut:
    return service.get_goal(db, principal, goal_id)


@router.patch(
    "/goals/{goal_id}",
    response_model=GoalOut,
    responses=PROBLEMS,
    summary="Change a goal (member or above)",
)
def patch_goal(
    goal_id: uuid.UUID, body: GoalUpdate, request: Request, db: DbSession, principal: GoalWriter
) -> GoalOut:
    return service.update_goal(db, principal, goal_id, body, request=request)


@router.delete(
    "/goals/{goal_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a goal and its contributions (member or above)",
)
def delete_goal(goal_id: uuid.UUID, request: Request, db: DbSession, principal: Writer) -> Response:
    service.delete_goal(db, principal, goal_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/goals/{goal_id}/contributions",
    response_model=ContributionPage,
    responses=PROBLEMS,
    summary="Contributions to a goal",
)
def get_contributions(
    goal_id: uuid.UUID,
    db: DbSession,
    principal: Reader,
    cursor: Cursor = None,
    limit: Limit = DEFAULT_PAGE,
) -> ContributionPage:
    return service.list_contributions(db, principal, goal_id, cursor=cursor, limit=limit)


@router.post(
    "/goals/{goal_id}/contributions",
    response_model=ContributionOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Add a contribution (member or above)",
)
def post_contribution(
    goal_id: uuid.UUID,
    body: ContributionCreate,
    request: Request,
    db: DbSession,
    principal: GoalWriter,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /goals/{goal_id}/contributions",
        payload=payload_of(body, goal_id=goal_id),
        run=lambda: service.add_contribution(db, principal, goal_id, body, request=request),
    )


@router.delete(
    "/goals/{goal_id}/contributions/{contribution_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Remove a contribution (member or above)",
)
def delete_contribution(
    goal_id: uuid.UUID,
    contribution_id: uuid.UUID,
    request: Request,
    db: DbSession,
    principal: Writer,
) -> Response:
    service.delete_contribution(db, principal, goal_id, contribution_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
