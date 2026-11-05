# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET/PATCH /settings/user``: my display name, locale, theme and time zone.

Reading works for a browser session or a token with the ``settings:read``
scope (SECURITY.md I3: a token reads nothing its scopes do not name).
Changing needs a browser session: personal access tokens are for scripts
working on data, not for editing the person's profile. Unknown fields are a ``422``
(F4). Locale is a BCP 47 language tag (``en-US``), theme ``system``,
``light`` or ``dark``, time zone an IANA name known to the server's tz
database (``UTC`` always works).
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.auth.authz import Authenticated, require_scope
from app.auth.deps import DbSession
from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.domain.households import users
from app.domain.households.schemas import UserPreferencesOut, UserPreferencesUpdate

router = APIRouter(tags=["settings"])

_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: {"description": "Problem details", "content": {"application/problem+json": {}}}
    for code in (403, 422)
}


def require_browser_session(principal: Authenticated) -> Principal:
    if principal.auth_method != "session":
        raise ProblemError(
            403, "session-required", "Browser session required", "Use the web app for this."
        )
    return principal


BrowserPrincipal = Annotated[Principal, Depends(require_browser_session)]


SettingsReader = Annotated[Principal, Depends(require_scope("settings:read"))]


@router.get(
    "/settings/user",
    response_model=UserPreferencesOut,
    responses=_PROBLEMS,
    summary="My preferences",
)
def get_user_settings(db: DbSession, principal: SettingsReader) -> UserPreferencesOut:
    return users.get_preferences(db, principal)


@router.patch(
    "/settings/user",
    response_model=UserPreferencesOut,
    responses=_PROBLEMS,
    summary="Change my display name, locale, theme or time zone",
)
def patch_user_settings(
    body: UserPreferencesUpdate, request: Request, db: DbSession, principal: BrowserPrincipal
) -> UserPreferencesOut:
    return users.update_preferences(db, principal, body, request=request)
