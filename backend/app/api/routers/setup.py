# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run setup (``app.auth.bootstrap``, AUTH.md "Admin bootstrap", SECURITY.md A10).

* ``GET /setup/status``: ``{"setup_required": true}`` until the first user
  exists (the SPA shows the setup page then).
* ``POST /setup``: the one-time token from the setup URL (the page reads it
  from the URL fragment and posts it in the body), username, password,
  display name, household name and base currency. Creates the first
  instance admin, their household (as owner) and a session (``login_method
  = bootstrap``; with ``PERFIN_REQUIRE_MFA`` it is restricted to enrolling a
  second factor). ``201`` with the session's CSRF token. A wrong or expired
  token is ``403 invalid-setup-token``; once setup has happened the route is
  ``404`` for good. Rate limited per client IP.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.auth import bootstrap
from app.auth.deps import DbSession, get_app_settings
from app.auth.local import (
    PASSWORD_INPUT_MAX,
    audit_auth,
    get_auth_limits,
    get_sessionmaker,
    request_ip,
    validate_new_password,
    weak_password,
)
from app.auth.models import USERNAME_MAX, LoginMethod, normalize_username
from app.auth.passwords import PasswordPolicyError
from app.auth.ratelimit import RateLimiter, ip_key
from app.auth.sessions import SessionPolicy, create_session, set_session_cookie
from app.core.audit import RequestMeta, audit
from app.core.csrf import get_csrf
from app.core.errors import ProblemError
from app.db.rls import set_context

router = APIRouter(tags=["setup"])

USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
CURRENCY_PATTERN = re.compile(r"^[A-Z]{3}$")


class SetupStatusOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    setup_required: bool


class SetupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: Annotated[str, Field(min_length=1, max_length=256)]
    username: Annotated[str, Field(min_length=1, max_length=USERNAME_MAX)]
    password: Annotated[str, Field(min_length=1, max_length=PASSWORD_INPUT_MAX)]
    display_name: Annotated[str, Field(min_length=1, max_length=200)]
    household_name: Annotated[str, Field(min_length=1, max_length=200)]
    base_currency: Annotated[str, Field(min_length=3, max_length=3)] = "USD"

    @field_validator("username")
    @classmethod
    def _username(cls, value: str) -> str:
        name = normalize_username(value)
        if not USERNAME_PATTERN.fullmatch(name):
            raise ValueError(
                "use 1-64 lower-case letters, digits, '.', '_' or '-', starting with a letter "
                "or digit"
            )
        return name

    @field_validator("display_name", "household_name")
    @classmethod
    def _text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or not cleaned.isprintable():
            raise ValueError("must be printable text")
        return cleaned

    @field_validator("base_currency")
    @classmethod
    def _currency(cls, value: str) -> str:
        code = value.strip().upper()
        if not CURRENCY_PATTERN.fullmatch(code):
            raise ValueError("must be an ISO 4217 code such as USD")
        return code


class SetupUserOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    username: str
    display_name: str
    is_instance_admin: bool


class SetupHouseholdOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    name: str
    base_currency: str
    role: Literal["owner"] = "owner"


class SetupOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    user: SetupUserOut
    household: SetupHouseholdOut
    csrf_token: str


_PROBLEMS: dict[int | str, dict[str, Any]] = {
    code: {"description": "Problem details", "content": {"application/problem+json": {}}}
    for code in (403, 404, 422, 429)
}


def _unavailable() -> ProblemError:
    return ProblemError(404, "not-found", "Not found", "Setup has already been completed.")


def _bad_token() -> ProblemError:
    return ProblemError(
        403,
        "invalid-setup-token",
        "Setup link not valid",
        "The setup link is invalid or expired. Run `perfin bootstrap-url` for a new one.",
    )


@router.get("/setup/status", response_model=SetupStatusOut, summary="Is first-run setup pending")
def get_setup_status(db: DbSession) -> SetupStatusOut:
    return SetupStatusOut(setup_required=bootstrap.setup_required(db))


@router.post(
    "/setup",
    status_code=status.HTTP_201_CREATED,
    response_model=SetupOut,
    responses=_PROBLEMS,
    summary="Create the first administrator (one-time setup token)",
)
def post_setup(body: SetupIn, request: Request, response: Response) -> SetupOut:
    settings = get_app_settings(request)
    factory = get_sessionmaker(request)
    RateLimiter(factory).hit(
        get_auth_limits(request).setup_per_ip, ip_key(request)
    ).raise_if_denied()
    # Cheap checks before hashing anything; repeated under the lock below.
    try:
        with factory() as db, db.begin():
            bootstrap.check_token(db, body.token)
    except bootstrap.SetupUnavailable:
        raise _unavailable() from None
    except bootstrap.InvalidSetupToken:
        raise _bad_token() from None
    try:
        validate_new_password(body.password, settings, username=body.username)
    except PasswordPolicyError as exc:
        raise weak_password(exc) from None

    meta = RequestMeta.from_request(request)
    try:
        with factory() as db, db.begin():
            result = bootstrap.complete_setup(
                db,
                token=body.token,
                username=body.username,
                password=body.password,
                display_name=body.display_name,
                household_name=body.household_name,
                base_currency=body.base_currency,
            )
            user, household = result.user, result.household
            issued = create_session(
                db,
                user_id=user.id,
                policy=SessionPolicy.from_settings(settings),
                login_method=LoginMethod.BOOTSTRAP,
                household_id=household.id,
                ip=request_ip(request),
                user_agent=request.headers.get("user-agent"),
            )
            audit_auth(
                db,
                action="instance.bootstrap",
                user_id=user.id,
                meta=meta,
                after={"username": user.username, "household_id": household.id},
            )
            set_context(db, household.id, user.id)
            audit(
                db,
                action="household.create",
                target_type="household",
                target_id=household.id,
                household_id=household.id,
                user_id=user.id,
                after={"name": household.name, "base_currency": household.base_currency},
                meta=meta,
            )
    except bootstrap.SetupUnavailable:
        raise _unavailable() from None
    except bootstrap.InvalidSetupToken:
        raise _bad_token() from None
    bootstrap.remove_url_file(settings.bootstrap_url_file)
    set_session_cookie(response, settings, issued)
    csrf = get_csrf(request)
    csrf.clear_pre_auth(response)
    return SetupOut(
        user=SetupUserOut(
            id=str(user.id),
            username=user.username,
            display_name=user.display_name,
            is_instance_admin=user.is_instance_admin,
        ),
        household=SetupHouseholdOut(
            id=str(household.id), name=household.name, base_currency=household.base_currency
        ),
        csrf_token=csrf.token_for_session(issued.raw_id),
    )
