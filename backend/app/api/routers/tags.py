# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tags (API.md ``/tags``; rules in ``app.domain.taxonomy.service``).

* ``GET /tags?cursor=&limit=``, ``GET /tags/{id}``: any role.
* ``POST /tags`` (``Idempotency-Key`` honoured), ``PATCH /tags/{id}``
  (rename): member or above.
* ``DELETE /tags/{id}``: owner or admin (untags transactions on every
  account).

Tokens need ``transactions:read`` / ``transactions:write``.
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
from app.domain.taxonomy import service
from app.domain.taxonomy.schemas import (
    DEFAULT_PAGE,
    MAX_PAGE,
    TagCreate,
    TagOut,
    TagPage,
    TagUpdate,
)

router = APIRouter(tags=["tags"])

Reader = Annotated[Principal, Depends(scoped_role("transactions:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("transactions:write", HouseholdRole.MEMBER))]
Manager = Annotated[Principal, Depends(scoped_role("transactions:write", HouseholdRole.ADMIN))]


def require_tag_writer(tag_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the tag must exist here (``404`` before the body is read)."""
    service.get_tag(db, principal, tag_id)
    return principal


TagWriter = Annotated[Principal, Depends(require_tag_writer)]


@router.get("/tags", response_model=TagPage, summary="Tags of the household")
def get_tags(
    db: DbSession,
    principal: Reader,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_PAGE,
) -> TagPage:
    return service.list_tags(db, principal, cursor=cursor, limit=limit)


@router.post(
    "/tags",
    response_model=TagOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Create a tag (member or above)",
)
def post_tag(
    body: TagCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /tags",
        payload=payload_of(body),
        run=lambda: service.create_tag(db, principal, body, request=request),
    )


@router.get("/tags/{tag_id}", response_model=TagOut, responses=PROBLEMS, summary="One tag")
def get_tag(tag_id: uuid.UUID, db: DbSession, principal: Reader) -> TagOut:
    return service.get_tag(db, principal, tag_id)


@router.patch("/tags/{tag_id}", response_model=TagOut, responses=PROBLEMS, summary="Rename a tag")
def patch_tag(
    tag_id: uuid.UUID, body: TagUpdate, request: Request, db: DbSession, principal: TagWriter
) -> TagOut:
    return service.update_tag(db, principal, tag_id, body, request=request)


@router.delete(
    "/tags/{tag_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a tag (owner/admin)",
)
def delete_tag(tag_id: uuid.UUID, request: Request, db: DbSession, principal: Manager) -> Response:
    service.delete_tag(db, principal, tag_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
