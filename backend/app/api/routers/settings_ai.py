# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/settings/ai`` and ``/settings/system/ai``: managing AI access (AI_ACCESS.md, E3, M2, M4, M5).

Every ``/settings/ai`` route needs the **owner** of the active household on
a **browser session** (tokens are refused, ``403 session-required``; an AI
token never gets here, ``403 ai-token-confined``). Changes need **step-up**
MFA:

* ``GET /settings/ai``: the household flag, the instance kill switch and the
  number of live clients. ``PATCH /settings/ai`` (step-up) ``{"enabled"}``
  turns AI access of the household off or on (``app.domain.ai.switches``).
* ``GET /settings/ai/clients``: the clients (never a token or hash).
  ``POST /settings/ai/clients`` (step-up): name, mode, options, transport and
  a **mandatory** ``expires_in_days``; ``201`` with ``token``, the raw
  ``ai:read`` token, shown this once (SHA-256 at rest).
  ``GET /settings/ai/clients/{id}``; ``DELETE /settings/ai/clients/{id}``
  (step-up) revokes the client and its token at once (``204``, idempotent).
* ``GET /settings/ai/clients/{id}/preview`` and ``POST /settings/ai/preview``
  (options in the body, before saving): every tool's body as the AI views
  would send it with those options, placeholders marked
  (``app.api.ai.preview``). Nothing is stored or sent.
* ``GET /settings/ai/calls?client_id&contains&limit``: the call audit, newest
  first; ``contains`` searches the stored responses to confirm something
  never left.
* ``GET /settings/ai/scrubber-preview?client_id&limit`` and ``POST
  /settings/ai/scrubber-preview`` (terms to try, ad-hoc text): the owner's
  own free text next to its scrubbed form, to tune the custom denylist.

``GET /settings/system/ai`` and ``PATCH /settings/system/ai`` (step-up) are
the instance kill switch, for an instance admin only.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response, status

from app.api.ai import preview
from app.auth.authz import require_instance_admin, require_role, require_step_up
from app.auth.deps import DbSession, get_app_settings
from app.auth.models import ApiToken, HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.errors import PROBLEM_MEDIA_TYPE, ProblemError
from app.domain.ai import service, switches
from app.domain.ai.models import AiCall, AiClient
from app.domain.ai.schemas import (
    AiAccessOut,
    AiAccessUpdate,
    AiCallOut,
    AiCallsOut,
    AiClientCreate,
    AiClientCreatedOut,
    AiClientOut,
    AiOptionsIn,
    ExposurePreviewOut,
    ScrubberPreviewIn,
    ScrubberPreviewOut,
    SystemAiOut,
    SystemAiUpdate,
)

router = APIRouter(tags=["settings"])

PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: {"description": "Problem details", "content": {PROBLEM_MEDIA_TYPE: {}}}
    for code in (401, 403, 404, 409, 422)
}

Owner = Annotated[Principal, Depends(require_role(HouseholdRole.OWNER))]


def require_owner_session(principal: Owner) -> Principal:
    """The household owner on a browser session (no tokens)."""
    if principal.auth_method != "session":
        raise ProblemError(
            403, "session-required", "Browser session required", "Use the web app for this."
        )
    return principal


OwnerSession = Annotated[Principal, Depends(require_owner_session)]


def require_owner_step_up(principal: OwnerSession) -> Principal:
    return require_step_up(principal)


OwnerSteppedUp = Annotated[Principal, Depends(require_owner_step_up)]
InstanceAdmin = Annotated[Principal, Depends(require_instance_admin)]


def require_admin_step_up(principal: InstanceAdmin) -> Principal:
    return require_step_up(principal)


AdminSteppedUp = Annotated[Principal, Depends(require_admin_step_up)]


def _household(principal: Principal) -> uuid.UUID:
    if principal.household_id is None:  # pragma: no cover - require_role checks it
        raise ProblemError(403, "forbidden", "Forbidden", "You are not in a household.")
    return principal.household_id


def _client_out(db: DbSession, client: AiClient, now: datetime) -> AiClientOut:
    prefix = None
    if client.token_id is not None:
        token = db.get(ApiToken, client.token_id)
        prefix = token.prefix if token is not None else None
    state: Literal["active", "expired", "revoked"] = (
        "revoked"
        if client.revoked_at is not None
        else "expired"
        if client.expires_at <= now
        else "active"
    )
    return AiClientOut(
        id=client.id,
        name=client.name,
        mode=client.mode,
        transport=client.transport,
        rounding=client.rounding,
        hide_nicknames=client.hide_nicknames,
        hide_merchant_location=client.hide_merchant_location,
        date_range_cap=client.date_range_cap,
        custom_denylist=list(client.custom_denylist or ()),
        member_aliases=list(client.member_aliases or ()),
        token_prefix=prefix,
        status=state,
        created_at=client.created_at,
        expires_at=client.expires_at,
        revoked_at=client.revoked_at,
        last_used_at=client.last_used_at,
    )


def _options(body: AiOptionsIn, *, expires_in_days: int) -> service.ClientOptions:
    return service.ClientOptions(
        name=body.name,
        mode=body.mode,
        transport=body.transport,
        rounding=body.rounding,
        hide_nicknames=body.hide_nicknames,
        hide_merchant_location=body.hide_merchant_location,
        date_range_cap=body.date_range_cap,
        custom_denylist=tuple(body.custom_denylist),
        member_aliases=tuple(body.member_aliases),
        expires_in_days=expires_in_days,
    )


# ---------------------------------------------------------------------------
# Switches
# ---------------------------------------------------------------------------


def _access_out(db: DbSession, household_id: uuid.UUID) -> AiAccessOut:
    state = switches.access_state(db, household_id)
    return AiAccessOut(
        enabled=state.enabled,
        household_enabled=state.household_enabled,
        system_enabled=state.system_enabled,
        active_clients=state.active_clients,
    )


@router.get(
    "/settings/ai",
    response_model=AiAccessOut,
    responses=PROBLEMS,
    summary="AI access of the household: switches and live clients",
)
def get_ai_access(db: DbSession, principal: OwnerSession) -> AiAccessOut:
    return _access_out(db, _household(principal))


@router.patch(
    "/settings/ai",
    response_model=AiAccessOut,
    responses=PROBLEMS,
    summary="Turn AI access of the household off or on (step-up)",
)
def patch_ai_access(
    body: AiAccessUpdate, request: Request, db: DbSession, principal: OwnerSteppedUp
) -> AiAccessOut:
    household_id = _household(principal)
    if body.enabled is not None:
        switches.set_household_enabled(db, principal, household_id, body.enabled, request=request)
    return _access_out(db, household_id)


@router.get(
    "/settings/system/ai",
    response_model=SystemAiOut,
    responses=PROBLEMS,
    summary="The instance AI kill switch (instance admin)",
)
def get_system_ai(db: DbSession, principal: InstanceAdmin) -> SystemAiOut:
    return SystemAiOut(enabled=switches.system_enabled(db))


@router.patch(
    "/settings/system/ai",
    response_model=SystemAiOut,
    responses=PROBLEMS,
    summary="Engage or release the instance AI kill switch (instance admin, step-up)",
)
def patch_system_ai(
    body: SystemAiUpdate, request: Request, db: DbSession, principal: AdminSteppedUp
) -> SystemAiOut:
    if body.enabled is not None:
        switches.set_system_enabled(db, principal, body.enabled, request=request)
    return SystemAiOut(enabled=switches.system_enabled(db))


# ---------------------------------------------------------------------------
# Clients
# ---------------------------------------------------------------------------


@router.get(
    "/settings/ai/clients",
    response_model=list[AiClientOut],
    responses=PROBLEMS,
    summary="AI clients of the household",
)
def list_ai_clients(db: DbSession, principal: OwnerSession) -> list[AiClientOut]:
    now = clock.utcnow()
    return [_client_out(db, c, now) for c in service.list_clients(db, principal)]


@router.post(
    "/settings/ai/clients",
    response_model=AiClientCreatedOut,
    status_code=status.HTTP_201_CREATED,
    responses=PROBLEMS,
    summary="Create an AI client and its ai:read token (step-up; token shown once)",
)
def create_ai_client(
    body: AiClientCreate, request: Request, db: DbSession, principal: OwnerSteppedUp
) -> AiClientCreatedOut:
    switches.require_enabled(db, _household(principal))
    created = service.create_client(
        db,
        principal,
        _options(body, expires_in_days=body.expires_in_days),
        settings=get_app_settings(request),
        request=request,
    )
    out = _client_out(db, created.client, clock.utcnow())
    return AiClientCreatedOut(**out.model_dump(), token=created.raw_token)


@router.get(
    "/settings/ai/clients/{client_id}",
    response_model=AiClientOut,
    responses=PROBLEMS,
    summary="One AI client",
)
def get_ai_client(client_id: uuid.UUID, db: DbSession, principal: OwnerSession) -> AiClientOut:
    return _client_out(db, service.get_client(db, principal, client_id), clock.utcnow())


@router.delete(
    "/settings/ai/clients/{client_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    responses=PROBLEMS,
    summary="Revoke an AI client and its token now (step-up)",
)
def revoke_ai_client(
    client_id: uuid.UUID, request: Request, db: DbSession, principal: OwnerSteppedUp
) -> Response:
    service.revoke_client(db, principal, client_id, request=request)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Previews
# ---------------------------------------------------------------------------


@router.get(
    "/settings/ai/clients/{client_id}/preview",
    response_model=ExposurePreviewOut,
    responses=PROBLEMS,
    summary="What every tool returns to this client, with its saved options",
)
def preview_ai_client(
    client_id: uuid.UUID, request: Request, db: DbSession, principal: OwnerSession
) -> ExposurePreviewOut:
    client = service.get_client(db, principal, client_id)
    options = service.ClientOptions(
        name=client.name,
        mode=client.mode,
        transport=client.transport,
        rounding=client.rounding,
        hide_nicknames=client.hide_nicknames,
        hide_merchant_location=client.hide_merchant_location,
        date_range_cap=client.date_range_cap,
        custom_denylist=tuple(client.custom_denylist or ()),
        member_aliases=tuple(client.member_aliases or ()),
    )
    # A detached copy: the views never touch the saved row.
    copy = preview.transient_client(client.household_id, options, client_id=client.id)
    return preview.exposure_preview(db, request, principal, copy)


@router.post(
    "/settings/ai/preview",
    response_model=ExposurePreviewOut,
    responses=PROBLEMS,
    summary="What every tool would return with these options, before saving",
)
def preview_ai_options(
    body: AiOptionsIn, request: Request, db: DbSession, principal: OwnerSession
) -> ExposurePreviewOut:
    clean = service.check_options(_options(body, expires_in_days=1), get_app_settings(request))
    client = preview.transient_client(_household(principal), clean)
    return preview.exposure_preview(db, request, principal, client)


@router.get(
    "/settings/ai/scrubber-preview",
    response_model=ScrubberPreviewOut,
    responses=PROBLEMS,
    summary="My free text next to its scrubbed form (a client's terms, or none)",
)
def get_scrubber_preview(
    db: DbSession,
    principal: OwnerSession,
    client_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> ScrubberPreviewOut:
    denylist: list[str] = []
    aliases: list[str] = []
    if client_id is not None:
        client = service.get_client(db, principal, client_id)
        denylist = list(client.custom_denylist or ())
        aliases = list(client.member_aliases or ())
    return preview.scrubber_preview(
        db,
        principal,
        _household(principal),
        custom_denylist=denylist,
        member_aliases=aliases,
        text=None,
        limit=limit,
    )


@router.post(
    "/settings/ai/scrubber-preview",
    response_model=ScrubberPreviewOut,
    responses=PROBLEMS,
    summary="Try denylist terms and aliases on my free text before saving them",
)
def post_scrubber_preview(
    body: ScrubberPreviewIn, request: Request, db: DbSession, principal: OwnerSession
) -> ScrubberPreviewOut:
    denylist = list(body.custom_denylist)
    aliases = list(body.member_aliases)
    if body.client_id is not None:
        client = service.get_client(db, principal, body.client_id)
        denylist = list(client.custom_denylist or ()) + denylist
        aliases = list(client.member_aliases or ()) + aliases
    clean = service.check_options(
        service.ClientOptions(
            name="Preview",
            custom_denylist=tuple(denylist),
            member_aliases=tuple(aliases),
            expires_in_days=1,
        ),
        get_app_settings(request),
    )
    return preview.scrubber_preview(
        db,
        principal,
        _household(principal),
        custom_denylist=list(clean.custom_denylist),
        member_aliases=list(clean.member_aliases),
        text=body.text,
        limit=body.limit,
    )


# ---------------------------------------------------------------------------
# Call audit
# ---------------------------------------------------------------------------


def _call_out(row: AiCall) -> AiCallOut:
    return AiCallOut(
        id=row.id,
        client_id=row.ai_client_id,
        tool=row.tool,
        params=row.params,
        status=row.status,
        bytes=row.bytes,
        created_at=row.created_at,
        response=row.response,
    )


@router.get(
    "/settings/ai/calls",
    response_model=AiCallsOut,
    responses=PROBLEMS,
    summary="Audit of AI calls with the responses as sent; search them for a string",
)
def list_ai_calls(
    db: DbSession,
    principal: OwnerSession,
    client_id: uuid.UUID | None = None,
    contains: Annotated[str | None, Query(min_length=1, max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> AiCallsOut:
    if client_id is not None:
        service.get_client(db, principal, client_id)  # 404 for a foreign client
    rows = service.search_calls(db, principal, client_id=client_id, contains=contains, limit=limit)
    return AiCallsOut(items=[_call_out(r) for r in rows])
