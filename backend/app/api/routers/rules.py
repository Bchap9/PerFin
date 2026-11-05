# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rules (API.md ``/rules``; rules in ``app.domain.rules.service``).

* ``GET /rules``, ``GET /rules/{id}``, ``GET /rules/export``: any role
  (account ids the reader cannot see show as the nil UUID).
* ``POST /rules`` (``Idempotency-Key`` honoured), ``PATCH`` / ``DELETE
  /rules/{id}``, ``POST /rules/reorder``, ``POST /rules/import``: owner or
  admin (rules rewrite transactions on every account).
* ``POST /rules/test``: any role; a dry run over the caller's visible
  history, paginated, that never writes.
* ``POST /rules/apply``: owner or admin; queues a ``run_rules`` job and
  answers ``202`` with its id (poll ``GET /jobs/{id}``).

Tokens need ``transactions:read`` / ``transactions:write``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.idempotency import IdempotencyKeyHeader, idempotent, payload_of
from app.domain.rules import service
from app.domain.rules.schemas import (
    JobAccepted,
    RuleApply,
    RuleCreate,
    RuleExport,
    RuleImport,
    RuleList,
    RuleOut,
    RuleReorder,
    RuleTest,
    RuleTestResult,
    RuleUpdate,
)

router = APIRouter(tags=["rules"])

Reader = Annotated[Principal, Depends(scoped_role("transactions:read", HouseholdRole.VIEWER))]
Manager = Annotated[Principal, Depends(scoped_role("transactions:write", HouseholdRole.ADMIN))]


def require_rule_manager(rule_id: uuid.UUID, db: DbSession, principal: Manager) -> Principal:
    """:data:`Manager`, then the rule must be this household's (``404`` before the body)."""
    service.require_rule(db, principal, rule_id)
    return principal


RuleManager = Annotated[Principal, Depends(require_rule_manager)]


@router.get("/rules", response_model=RuleList, summary="Rules in evaluation order")
def get_rules(db: DbSession, principal: Reader) -> RuleList:
    return service.list_rules(db, principal)


@router.post(
    "/rules",
    response_model=RuleOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Create a rule (owner/admin); it runs after the existing ones",
)
def post_rule(
    body: RuleCreate,
    request: Request,
    db: DbSession,
    principal: Manager,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /rules",
        payload=payload_of(body),
        run=lambda: service.create_rule(db, principal, body, request=request),
    )


@router.get(
    "/rules/export",
    response_model=RuleExport,
    summary="All rules as a portable JSON document",
)
def get_rules_export(db: DbSession, principal: Reader) -> RuleExport:
    return service.export_rules(db, principal)


@router.post(
    "/rules/import",
    response_model=RuleList,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Import rules from an export document (owner/admin)",
)
def post_rules_import(
    body: RuleImport,
    request: Request,
    db: DbSession,
    principal: Manager,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /rules/import",
        payload=payload_of(body),
        run=lambda: service.import_rules(db, principal, body, request=request),
    )


@router.post(
    "/rules/reorder",
    response_model=RuleList,
    responses=PROBLEMS,
    summary="Set the evaluation order (owner/admin)",
)
def post_rules_reorder(
    body: RuleReorder, request: Request, db: DbSession, principal: Manager
) -> RuleList:
    return service.reorder_rules(db, principal, body, request=request)


@router.post(
    "/rules/test",
    response_model=RuleTestResult,
    responses=PROBLEMS,
    summary="Dry-run rules against history (writes nothing)",
)
def post_rules_test(body: RuleTest, db: DbSession, principal: Reader) -> RuleTestResult:
    return service.test_rules(db, principal, body)


@router.post(
    "/rules/apply",
    response_model=JobAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    responses=PROBLEMS,
    summary="Re-run the rules over existing transactions as a job (owner/admin)",
)
def post_rules_apply(
    body: RuleApply,
    request: Request,
    db: DbSession,
    principal: Manager,
    idempotency_key: IdempotencyKeyHeader = None,
) -> Any:
    return idempotent(
        db,
        principal,
        idempotency_key,
        scope="POST /rules/apply",
        payload=payload_of(body),
        run=lambda: service.request_apply(db, principal, body, request=request),
        status_code=status.HTTP_202_ACCEPTED,
    )


@router.get("/rules/{rule_id}", response_model=RuleOut, responses=PROBLEMS, summary="One rule")
def get_rule(rule_id: uuid.UUID, db: DbSession, principal: Reader) -> RuleOut:
    return service.get_rule(db, principal, rule_id)


@router.patch(
    "/rules/{rule_id}",
    response_model=RuleOut,
    responses=PROBLEMS,
    summary="Change a rule (owner/admin)",
)
def patch_rule(
    rule_id: uuid.UUID, body: RuleUpdate, request: Request, db: DbSession, principal: RuleManager
) -> RuleOut:
    return service.update_rule(db, principal, rule_id, body, request=request)


@router.delete(
    "/rules/{rule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=PROBLEMS,
    summary="Delete a rule (owner/admin)",
)
def delete_rule(
    rule_id: uuid.UUID, request: Request, db: DbSession, principal: Manager
) -> Response:
    service.delete_rule(db, principal, rule_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
