# SPDX-License-Identifier: AGPL-3.0-or-later
"""Categories (API.md ``/categories``; rules in ``app.domain.taxonomy.service``).

* ``GET /categories?cursor=&limit=&kind=`` (default limit 500: the whole tree
  in one page for a usual household), ``GET /categories/{id}``: any role.
* ``POST /categories`` (``Idempotency-Key`` honoured), ``PATCH
  /categories/{id}`` (rename, move, kind, tax treatment): member or above.
* ``DELETE /categories/{id}``, ``POST /categories/{id}/merge``
  (``{"into": id}``): owner or admin, because they rewrite transactions on
  every account of the household.

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
    DEFAULT_CATEGORY_PAGE,
    MAX_PAGE,
    CategoryCreate,
    CategoryOut,
    CategoryPage,
    CategoryUpdate,
    MergeInto,
)
from app.domain.transactions.models import CategoryKind

router = APIRouter(tags=["categories"])

Reader = Annotated[Principal, Depends(scoped_role("transactions:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("transactions:write", HouseholdRole.MEMBER))]
Manager = Annotated[Principal, Depends(scoped_role("transactions:write", HouseholdRole.ADMIN))]


def require_category_writer(category_id: uuid.UUID, db: DbSession, principal: Writer) -> Principal:
    """:data:`Writer`, then the category must exist here (``404`` before the body is read)."""
    service.get_category(db, principal, category_id)
    return principal


CategoryWriter = Annotated[Principal, Depends(require_category_writer)]


def require_category_manager(
    category_id: uuid.UUID, db: DbSession, principal: Manager
) -> Principal:
    """:data:`Manager`, then the category must exist here (``404`` before the body is read)."""
    service.get_category(db, principal, category_id)
    return principal


CategoryManager = Annotated[Principal, Depends(require_category_manager)]


@router.get("/categories", response_model=CategoryPage, summary="Categories of the household")
def get_categories(
    db: DbSession,
    principal: Reader,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE)] = DEFAULT_CATEGORY_PAGE,
    kind: Annotated[CategoryKind | None, Query()] = None,
) -> CategoryPage:
    return service.list_categories(db, principal, cursor=cursor, limit=limit, kind=kind)


@router.post(
    "/categories",
    response_model=CategoryOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Create a category (member or above)",
)
def post_category(
    body: CategoryCreate,
    request: Request,
    db: DbSession,
    principal: Writer,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /categories",
        payload=payload_of(body),
        run=lambda: service.create_category(db, principal, body, request=request),
    )


@router.get(
    "/categories/{category_id}",
    response_model=CategoryOut,
    responses=PROBLEMS,
    summary="One category",
)
def get_category(category_id: uuid.UUID, db: DbSession, principal: Reader) -> CategoryOut:
    return service.get_category(db, principal, category_id)


@router.patch(
    "/categories/{category_id}",
    response_model=CategoryOut,
    responses=PROBLEMS,
    summary="Rename, move or re-kind a category (member or above)",
)
def patch_category(
    category_id: uuid.UUID,
    body: CategoryUpdate,
    request: Request,
    db: DbSession,
    principal: CategoryWriter,
) -> CategoryOut:
    return service.update_category(db, principal, category_id, body, request=request)


@router.delete(
    "/categories/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a category without subcategories (owner/admin)",
)
def delete_category(
    category_id: uuid.UUID, request: Request, db: DbSession, principal: Manager
) -> Response:
    service.delete_category(db, principal, category_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/categories/{category_id}/merge",
    response_model=CategoryOut,
    responses=PROBLEMS,
    summary="Merge a category into another (owner/admin)",
)
def post_category_merge(
    category_id: uuid.UUID,
    body: MergeInto,
    request: Request,
    db: DbSession,
    principal: CategoryManager,
) -> CategoryOut:
    return service.merge_category(db, principal, category_id, body, request=request)
