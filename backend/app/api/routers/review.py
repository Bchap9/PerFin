# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue (API.md ``/review``; rules in ``app.domain.review.service``).

* ``GET /review?state=open&kind=&document=&cursor=&limit=``: items the
  caller can see, oldest first (any role; ``state`` defaults to ``open``,
  ``all`` lists every state).
* ``GET /review/{id}``: one item (``404 review-item-not-found`` if hidden).
* ``POST /review/{id}/resolve``: a typed resolution whose ``kind`` matches
  the item (``app.domain.review.schemas``); member or above who may change
  the item's document.
* ``POST /review/{id}/dismiss``: close without acting.

Tokens need ``review:read`` / ``review:write``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Body, Depends, Query, Request

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.domain.documents.models import ReviewItemState, ReviewKind
from app.domain.review import service
from app.domain.review.schemas import (
    DEFAULT_PAGE,
    MAX_PAGE,
    ReviewItemOut,
    ReviewPage,
    ReviewResolution,
)

router = APIRouter(tags=["review"])

Reader = Annotated[Principal, Depends(scoped_role("review:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("review:write", HouseholdRole.MEMBER))]


def require_item_resolver(item_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer` who may resolve the item (``404``/``403`` before the body)."""
    service.require_resolvable(db, principal, item_id)
    return principal


Resolver = Annotated[Principal, Depends(require_item_resolver)]


@router.get("/review", response_model=ReviewPage, responses=PROBLEMS, summary="Review queue")
def get_review(
    db: DbSession,
    principal: Reader,
    state: Annotated[ReviewItemState | Literal["all"], Query()] = ReviewItemState.OPEN,
    kind: Annotated[ReviewKind | None, Query()] = None,
    document: Annotated[uuid.UUID | None, Query()] = None,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> ReviewPage:
    return service.list_items(
        db,
        principal,
        state=None if state == "all" else ReviewItemState(state),
        kind=kind,
        document_id=document,
        cursor=cursor,
        limit=limit,
    )


@router.get(
    "/review/{item_id}", response_model=ReviewItemOut, responses=PROBLEMS, summary="Review item"
)
def get_review_item(item_id: uuid.UUID, db: DbSession, principal: Reader) -> ReviewItemOut:
    return service.get_item(db, principal, item_id)


@router.post(
    "/review/{item_id}/resolve",
    response_model=ReviewItemOut,
    responses=PROBLEMS,
    summary="Resolve a review item",
)
def post_resolve(
    item_id: uuid.UUID,
    body: Annotated[ReviewResolution, Body()],
    request: Request,
    db: DbSession,
    principal: Resolver,
) -> ReviewItemOut:
    return service.resolve(db, principal, item_id, body, request=request)


@router.post(
    "/review/{item_id}/dismiss",
    response_model=ReviewItemOut,
    responses=PROBLEMS,
    summary="Dismiss a review item",
)
def post_dismiss(
    item_id: uuid.UUID, request: Request, db: DbSession, principal: Resolver
) -> ReviewItemOut:
    return service.dismiss(db, principal, item_id, request=request)
