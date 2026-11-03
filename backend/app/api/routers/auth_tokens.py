# SPDX-License-Identifier: AGPL-3.0-or-later
"""Personal access tokens (API.md ``/auth/tokens``, SECURITY.md I3).

* ``GET /auth/tokens/scopes``: the scope catalog, marking what the caller's
  role may put on a token (``ai:read`` is never grantable here).
* ``GET /auth/tokens``: the caller's tokens in the active household (no
  secrets, no hashes), each with its status and the scopes still in effect
  under the caller's current role.
* ``POST /auth/tokens`` (step-up MFA): ``{"name", "scopes", "expires_in_days"}``
  returns ``201`` with the token metadata and ``token``, the raw value, shown
  this once only.
* ``DELETE /auth/tokens/{id}``: revoke (``204``; idempotent; ``404`` for
  anyone else's token).

All four need a browser session: a token can neither mint, list nor revoke
tokens. Business rules are in ``app.auth.tokens``; input schemas forbid
unknown fields (F4).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth import tokens as service
from app.auth.authz import require_authenticated, require_step_up
from app.auth.deps import DbSession, get_app_settings
from app.auth.models import ApiToken
from app.auth.principal import Principal
from app.auth.scopes import CATALOG, RESERVED_SCOPES, effective_scopes, grantable_for_role
from app.core import clock
from app.core.errors import ProblemError

router = APIRouter(tags=["auth"])

_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: {"description": "Problem details", "content": {"application/problem+json": {}}}
    for code in (403, 404, 409, 422)
}


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class TokenCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=service.TOKEN_NAME_MAX)]
    scopes: Annotated[list[str], Field(min_length=1, max_length=64)]
    expires_in_days: Annotated[int, Field(ge=1, le=service.TOKEN_MAX_DAYS_CAP)]

    @field_validator("name")
    @classmethod
    def _clean_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name must not be blank")
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in cleaned):
            raise ValueError("name must not contain control characters")
        return cleaned

    @field_validator("scopes")
    @classmethod
    def _unique_scopes(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("scopes must be unique")
        return value


class TokenOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    prefix: str
    scopes: list[str]
    active_scopes: list[str]
    status: service.TokenStatus
    created_at: datetime
    expires_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class TokenCreatedOut(TokenOut):
    token: str = Field(description="The raw token. Shown once; it cannot be retrieved again.")


class ScopeOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    scope: str
    description: str
    grantable: bool
    reserved: bool


class ScopeCatalogOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    scopes: list[ScopeOut]
    max_expires_in_days: int


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def _session_required() -> ProblemError:
    return ProblemError(
        403, "session-required", "Browser session required", "Use the web app for this."
    )


def require_token_manager(
    principal: Annotated[Principal, Depends(require_authenticated)],
) -> Principal:
    """A browser session with an active household (tokens never manage tokens)."""
    if principal.auth_method != "session" or principal.session_id is None:
        raise _session_required()
    if principal.household_id is None or principal.role is None:
        raise ProblemError(
            403, "forbidden", "Forbidden", "A token belongs to a household; join one first."
        )
    return principal


def require_token_creator(
    principal: Annotated[Principal, Depends(require_token_manager)],
) -> Principal:
    """:func:`require_token_manager` plus a step-up within the last 10 minutes."""
    return require_step_up(principal)


TokenManager = Annotated[Principal, Depends(require_token_manager)]
TokenCreator = Annotated[Principal, Depends(require_token_creator)]


def _token_out(row: ApiToken, principal: Principal, now: datetime) -> TokenOut:
    return TokenOut(
        id=row.id,
        name=row.name,
        prefix=row.prefix,
        scopes=sorted(row.scopes),
        active_scopes=sorted(effective_scopes(row.scopes, principal.role)),
        status=service.token_status(row, now),
        created_at=row.created_at,
        expires_at=row.expires_at,
        last_used_at=row.last_used_at,
        revoked_at=row.revoked_at,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/auth/tokens/scopes",
    response_model=ScopeCatalogOut,
    responses=_PROBLEMS,
    summary="Token scopes and which ones my role may grant",
)
def get_token_scopes(request: Request, principal: TokenManager) -> ScopeCatalogOut:
    grantable = grantable_for_role(principal.role)
    settings = get_app_settings(request)
    return ScopeCatalogOut(
        scopes=[
            ScopeOut(
                scope=scope,
                description=description,
                grantable=scope in grantable,
                reserved=scope in RESERVED_SCOPES,
            )
            for scope, description in CATALOG.items()
        ],
        max_expires_in_days=min(settings.token_max_days, service.TOKEN_MAX_DAYS_CAP),
    )


@router.get(
    "/auth/tokens",
    response_model=list[TokenOut],
    responses=_PROBLEMS,
    summary="List my personal access tokens",
)
def get_tokens(db: DbSession, principal: TokenManager) -> list[TokenOut]:
    now = clock.utcnow()
    return [_token_out(row, principal, now) for row in service.list_tokens(db, principal)]


@router.post(
    "/auth/tokens",
    response_model=TokenCreatedOut,
    status_code=status.HTTP_201_CREATED,
    responses=_PROBLEMS,
    summary="Create a personal access token (step-up; the token is shown once)",
)
def post_token(
    body: TokenCreate, request: Request, db: DbSession, principal: TokenCreator
) -> TokenCreatedOut:
    now = clock.utcnow()
    created = service.create_token(
        db,
        principal,
        name=body.name,
        scopes=body.scopes,
        expires_in_days=body.expires_in_days,
        settings=get_app_settings(request),
        limits=service.get_token_limits(request),
        request=request,
        now=now,
    )
    out = _token_out(created.row, principal, now)
    return TokenCreatedOut(**out.model_dump(), token=created.raw)


@router.delete(
    "/auth/tokens/{token_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses=_PROBLEMS,
    summary="Revoke one of my personal access tokens",
)
def delete_token(
    token_id: uuid.UUID, request: Request, db: DbSession, principal: TokenManager
) -> Response:
    service.revoke_token(db, principal, token_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
