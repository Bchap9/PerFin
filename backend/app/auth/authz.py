# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authorisation guards (AUTH.md "Authorisation model", "Step-up").

Use these as FastAPI dependencies; each returns the
:class:`~app.auth.principal.Principal`::

    @router.get("/things")
    def list_things(principal: Annotated[Principal, Depends(require_role(HouseholdRole.MEMBER))]):
        ...

* :func:`require_authenticated`: logged in (session or token) **and** not
  MFA-restricted. The default for every endpoint.
* :func:`require_mfa_if_enforced`: with ``PERFIN_REQUIRE_MFA`` a user with no
  second factor gets a restricted principal that may only enrol MFA; this
  guard turns it away (``403 mfa-enrollment-required``).
  :func:`require_authenticated` includes it; MFA enrolment endpoints depend on
  ``app.auth.deps.require_principal`` instead.
* :func:`require_household` / :func:`require_role`: an active household and a
  role at least ``minimum`` (owner > admin > member > viewer).
* :func:`require_instance_admin`: the local ``is_instance_admin`` flag, on a
  browser session (tokens never carry instance administration).
* :func:`require_scope`: tokens must carry the scope; sessions have every
  user-level scope.
* :func:`require_step_up`: a session whose MFA was verified within the last
  :data:`STEP_UP_WINDOW` (10 minutes). Tokens never pass.
* :func:`account_access` / :func:`require_account_access`: object-level
  check on ``account_grant`` (used from Phase 2). Failures are ``404`` so the
  existence of an account is not revealed.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Annotated, Final, Literal

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.deps import require_principal
from app.auth.models import AccountGrant, GrantLevel, HouseholdRole
from app.auth.principal import Principal
from app.auth.scopes import is_known
from app.core import clock
from app.core.errors import ProblemError

STEP_UP_WINDOW: Final = timedelta(minutes=10)

ROLE_RANK: Final[dict[HouseholdRole, int]] = {
    HouseholdRole.OWNER: 4,
    HouseholdRole.ADMIN: 3,
    HouseholdRole.MEMBER: 2,
    HouseholdRole.VIEWER: 1,
}

AccessLevel = Literal["view", "edit"]


def role_at_least(role: HouseholdRole | None, minimum: HouseholdRole) -> bool:
    """True when ``role`` ranks at or above ``minimum``."""
    return role is not None and ROLE_RANK[role] >= ROLE_RANK[minimum]


def forbidden(detail: str = "You do not have permission to do this.") -> ProblemError:
    return ProblemError(403, "forbidden", "Forbidden", detail)


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def require_mfa_if_enforced(
    principal: Annotated[Principal, Depends(require_principal)],
) -> Principal:
    """Turn away principals that must enrol MFA first."""
    if principal.restricted:
        raise ProblemError(
            403,
            "mfa-enrollment-required",
            "Multi-factor authentication required",
            "This instance requires a second factor. Set one up to continue.",
        )
    return principal


def require_authenticated(
    principal: Annotated[Principal, Depends(require_mfa_if_enforced)],
) -> Principal:
    """Authenticated and not MFA-restricted."""
    return principal


Authenticated = Annotated[Principal, Depends(require_authenticated)]


def require_household(principal: Authenticated) -> Principal:
    """An active household context (a current membership)."""
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    return principal


def require_role(minimum: HouseholdRole) -> Callable[[Principal], Principal]:
    """Dependency factory: household role at least ``minimum``."""

    def guard(principal: Annotated[Principal, Depends(require_household)]) -> Principal:
        if not role_at_least(principal.role, minimum):
            raise forbidden()
        return principal

    guard.__name__ = f"require_role_{minimum.value}"
    return guard


def require_instance_admin(principal: Authenticated) -> Principal:
    """The instance-admin flag from the local database, on a browser session."""
    if principal.auth_method != "session" or not principal.is_instance_admin:
        raise forbidden()
    return principal


def require_scope(scope: str) -> Callable[[Principal], Principal]:
    """Dependency factory: the caller holds ``scope`` (sessions hold all).

    ``scope`` must be in the catalog (``app.auth.scopes.CATALOG``); a typo
    raises at import time instead of silently refusing every token.
    """
    if not is_known(scope):
        raise ValueError(f"unknown token scope {scope!r}; add it to app.auth.scopes.CATALOG")

    def guard(principal: Authenticated) -> Principal:
        if not principal.has_scope(scope):
            raise ProblemError(
                403,
                "insufficient-scope",
                "Insufficient scope",
                f"This token lacks the {scope!r} scope.",
            )
        return principal

    guard.__name__ = f"require_scope_{scope.replace(':', '_')}"
    return guard


def step_up_is_fresh(mfa_verified_at: datetime | None, now: datetime | None = None) -> bool:
    if mfa_verified_at is None:
        return False
    now = now or clock.utcnow()
    return now - mfa_verified_at <= STEP_UP_WINDOW


def require_step_up(principal: Authenticated) -> Principal:
    """A browser session with MFA verified within :data:`STEP_UP_WINDOW`."""
    if principal.auth_method != "session" or not step_up_is_fresh(principal.mfa_verified_at):
        raise ProblemError(
            403,
            "step-up-required",
            "Recent verification required",
            "Verify your second factor again to continue.",
        )
    return principal


# ---------------------------------------------------------------------------
# Object-level: accounts (Phase 2 uses these)
# ---------------------------------------------------------------------------


def _grant_allows(level: GrantLevel, need: AccessLevel) -> bool:
    if need == "view":
        return level in (GrantLevel.VIEW, GrantLevel.EDIT)
    return level is GrantLevel.EDIT


def account_access(
    db: Session, principal: Principal, account_id: uuid.UUID, need: AccessLevel
) -> bool:
    """May ``principal`` ``need`` (view/edit) account ``account_id``?

    The caller must already have confirmed the account belongs to
    ``principal.household_id`` (RLS does as well). Owners and admins may do
    anything; members as granted; viewers at most view whatever is granted.
    """
    if principal.household_id is None or principal.role is None:
        return False
    if role_at_least(principal.role, HouseholdRole.ADMIN):
        return True
    if principal.role is HouseholdRole.VIEWER and need == "edit":
        return False
    level = db.execute(
        select(AccountGrant.level).where(
            AccountGrant.household_id == principal.household_id,
            AccountGrant.user_id == principal.user_id,
            AccountGrant.account_id == account_id,
        )
    ).scalar_one_or_none()
    return level is not None and _grant_allows(level, need)


def require_account_access(
    db: Session, principal: Principal, account_id: uuid.UUID, need: AccessLevel
) -> None:
    """Raise ``404 account-not-found`` unless :func:`account_access` allows it."""
    if not account_access(db, principal, account_id, need):
        raise ProblemError(404, "account-not-found", "Account not found")
