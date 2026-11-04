# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/settings/intake``: the household's receipts mailbox (API.md, INGESTION.md, D1, D2, D5).

Every route needs a **browser session** of an owner or admin of the active
household (tokens are refused, ``403 session-required``: the settings hold
mailbox credentials and the alias token).

* ``GET /settings/intake``: host, port, username, folders, the alias
  token, the sender policy and the last poll's outcome; the password is
  never returned (``password_set``). ``configured: false`` (and no alias)
  until the first ``PATCH`` or rotation.
* ``PATCH /settings/intake`` (**step-up**, AUTH.md): change any of those
  fields; the password is write-only. ``422 mail-intake-incomplete`` when
  enabling without host, username and password; ``422 invalid-sender-user``
  for an allowed sender mapped to someone who is not a member, admin or
  owner.
* ``POST /settings/intake/email/test``: log in to the stored mailbox and
  look at the inbox read-only; ``200 {"ok", "outcome", "messages",
  "move_supported"}`` where ``outcome`` is a short code (``tls_failed``,
  ``auth_failed``, ``blocked``, ...; never the server's reply). ``409
  mail-intake-not-configured`` without stored credentials.
* ``POST /settings/intake/email/rotate-alias`` (**step-up**): a new alias
  token; mail to the old one is rejected from now on.

Rules in ``app.domain.intake_settings.service``.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request

from app.api.routers.accounts import PROBLEMS
from app.auth.authz import require_role, require_step_up
from app.auth.deps import DbSession, get_app_settings, get_keyring
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.crypto import Keyring
from app.core.errors import ProblemError
from app.core.settings import Settings
from app.domain.intake_settings import service
from app.domain.intake_settings.schemas import (
    AliasOut,
    MailIntakeOut,
    MailIntakeUpdate,
    MailTestOut,
)
from app.ingestion.intake.email import ConnectOptions

router = APIRouter(tags=["settings"])

Admin = Annotated[Principal, Depends(require_role(HouseholdRole.ADMIN))]


def require_admin_session(principal: Admin) -> Principal:
    """Owner or admin, browser session (no tokens)."""
    if principal.auth_method != "session":
        raise ProblemError(
            403, "session-required", "Browser session required", "Use the web app for this."
        )
    return principal


AdminSession = Annotated[Principal, Depends(require_admin_session)]


def require_admin_step_up(principal: AdminSession) -> Principal:
    return require_step_up(principal)


SteppedUp = Annotated[Principal, Depends(require_admin_step_up)]
AppSettings = Annotated[Settings, Depends(get_app_settings)]
AppKeyring = Annotated[Keyring, Depends(get_keyring)]


def connect_options(request: Request) -> ConnectOptions:
    """How the API reaches IMAP servers (``app.state.mail_connect_options`` in tests)."""
    override = getattr(request.app.state, "mail_connect_options", None)
    if isinstance(override, ConnectOptions):
        return override
    return ConnectOptions.from_settings(get_app_settings(request))


@router.get(
    "/settings/intake",
    response_model=MailIntakeOut,
    responses=PROBLEMS,
    summary="Receipts mailbox settings",
)
def get_intake(
    db: DbSession, principal: AdminSession, settings: AppSettings, keyring: AppKeyring
) -> MailIntakeOut:
    return service.get_intake(
        db, principal, keyring, instance_enabled=settings.email_intake_enabled
    )


@router.patch(
    "/settings/intake",
    response_model=MailIntakeOut,
    responses=PROBLEMS,
    summary="Change the receipts mailbox settings (step-up)",
)
def patch_intake(
    body: MailIntakeUpdate,
    request: Request,
    db: DbSession,
    principal: SteppedUp,
    settings: AppSettings,
    keyring: AppKeyring,
) -> MailIntakeOut:
    return service.update_intake(
        db,
        principal,
        body,
        keyring,
        instance_enabled=settings.email_intake_enabled,
        request=request,
    )


@router.post(
    "/settings/intake/email/test",
    response_model=MailTestOut,
    responses=PROBLEMS,
    summary="Log in to the receipts mailbox and report the outcome",
)
def check_intake_email(
    request: Request, db: DbSession, principal: AdminSession, keyring: AppKeyring
) -> MailTestOut:
    return service.check(db, principal, keyring, connect_options(request))


@router.post(
    "/settings/intake/email/rotate-alias",
    response_model=AliasOut,
    responses=PROBLEMS,
    summary="Replace the alias token (step-up)",
)
def rotate_alias(
    request: Request, db: DbSession, principal: SteppedUp, keyring: AppKeyring
) -> AliasOut:
    return service.rotate_alias(db, principal, keyring, request=request)
