# SPDX-License-Identifier: AGPL-3.0-or-later
"""The receipts mailbox settings of a household (API.md ``/settings/intake``, D1, D2).

* **Encryption (D2).** The mailbox username and app password and the alias
  token are stored only as envelopes under the master key, each bound to
  its column and row (:func:`column_ad`). The password is write-only.
* **Alias token.** 16 characters of lower-case base32 (80 bits), so it
  survives mail systems that fold the local part to lower case. It is
  created with the row and replaced by :func:`rotate_alias`; mail to an old
  token is rejected from then on. Its value is never written to the audit
  log or the application log.
* **Senders.** ``allowed_senders`` entries must name a current member,
  admin or owner (``422 invalid-sender-user`` otherwise); at poll time an
  entry whose user has since left or became a viewer is ignored, as is a
  viewer's own address.
* Enabling needs a host, a username and a password (``422
  mail-intake-incomplete``). Every change is audited
  (``mail_intake.update`` with the names of the changed fields, never
  their values; ``mail_intake.alias_rotate``).
"""

from __future__ import annotations

import secrets
import uuid
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.authz import forbidden
from app.auth.models import HouseholdMember, HouseholdRole, User
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.crypto import Keyring, column_ad
from app.core.errors import ProblemError
from app.core.ids import uuid7
from app.domain.intake_settings.models import (
    DEFAULT_INBOX,
    DEFAULT_PORT,
    DEFAULT_PROCESSED,
    DEFAULT_REJECTED,
    MailIntake,
)
from app.domain.intake_settings.schemas import (
    AliasOut,
    AllowedSender,
    MailIntakeOut,
    MailIntakeUpdate,
    MailTestOut,
)
from app.ingestion.intake.email import (
    AcceptancePolicy,
    ConnectOptions,
    MailboxConfig,
    check_connection,
)

TABLE: Final = "mail_intake"
ALIAS_ALPHABET: Final = "abcdefghijklmnopqrstuvwxyz234567"
ALIAS_LENGTH: Final = 16
#: Roles whose mail may become documents (viewers cannot upload).
SENDER_ROLES: Final = frozenset({HouseholdRole.MEMBER, HouseholdRole.ADMIN, HouseholdRole.OWNER})
_CREDENTIAL_FIELDS: Final = ("host", "port", "username", "password")


def _ad(row_id: uuid.UUID, column: str) -> bytes:
    return column_ad(TABLE, column, row_id)


def new_alias() -> str:
    return "".join(secrets.choice(ALIAS_ALPHABET) for _ in range(ALIAS_LENGTH))


def not_configured() -> ProblemError:
    return ProblemError(
        409,
        "mail-intake-not-configured",
        "Mailbox not configured",
        "Set the mailbox host, username and password first.",
    )


def _household(principal: Principal) -> uuid.UUID:
    if principal.household_id is None:
        raise forbidden("You are not a member of a household.")
    return principal.household_id


def get_row(db: Session, household_id: uuid.UUID, *, lock: bool = False) -> MailIntake | None:
    query = select(MailIntake).where(MailIntake.household_id == household_id)
    if lock:
        query = query.with_for_update()
    return db.execute(query).scalar_one_or_none()


def _ensure_row(db: Session, household_id: uuid.UUID, keyring: Keyring) -> MailIntake:
    row = get_row(db, household_id, lock=True)
    if row is not None:
        return row
    row_id = uuid7()
    row = MailIntake(
        id=row_id,
        household_id=household_id,
        enabled=False,
        port=DEFAULT_PORT,
        inbox_folder=DEFAULT_INBOX,
        processed_folder=DEFAULT_PROCESSED,
        rejected_folder=DEFAULT_REJECTED,
        alias_encrypted=keyring.encrypt_str(new_alias(), ad=_ad(row_id, "alias_encrypted")),
        alias_rotated_at=clock.utcnow(),
        allowed_senders=[],
        member_addresses=True,
        require_auth_results=True,
        last_poll_counts={},
    )
    db.add(row)
    db.flush()
    return row


def _decrypt(keyring: Keyring, row: MailIntake, column: str) -> str | None:
    value = getattr(row, column)
    if value is None:
        return None
    return keyring.decrypt_str(value, ad=_ad(row.id, column))


def alias_token(keyring: Keyring, row: MailIntake) -> str:
    token = _decrypt(keyring, row, "alias_encrypted")
    if token is None:  # pragma: no cover - the column is NOT NULL
        raise RuntimeError("alias missing")
    return token


def _out(row: MailIntake | None, keyring: Keyring, *, instance_enabled: bool) -> MailIntakeOut:
    if row is None:
        return MailIntakeOut(
            configured=False,
            instance_enabled=instance_enabled,
            enabled=False,
            host=None,
            port=DEFAULT_PORT,
            username=None,
            password_set=False,
            inbox_folder=DEFAULT_INBOX,
            processed_folder=DEFAULT_PROCESSED,
            rejected_folder=DEFAULT_REJECTED,
            alias_token=None,
            alias_rotated_at=None,
            allowed_senders=[],
            member_addresses=True,
            require_auth_results=True,
            authserv_id=None,
            last_poll_at=None,
            last_poll_outcome=None,
            last_poll_counts={},
        )
    return MailIntakeOut(
        configured=row.host is not None
        and row.username_encrypted is not None
        and row.password_encrypted is not None,
        instance_enabled=instance_enabled,
        enabled=row.enabled,
        host=row.host,
        port=row.port,
        username=_decrypt(keyring, row, "username_encrypted"),
        password_set=row.password_encrypted is not None,
        inbox_folder=row.inbox_folder,
        processed_folder=row.processed_folder,
        rejected_folder=row.rejected_folder,
        alias_token=alias_token(keyring, row),
        alias_rotated_at=row.alias_rotated_at,
        allowed_senders=[AllowedSender.model_validate(s) for s in row.allowed_senders],
        member_addresses=row.member_addresses,
        require_auth_results=row.require_auth_results,
        authserv_id=row.authserv_id,
        last_poll_at=row.last_poll_at,
        last_poll_outcome=row.last_poll_outcome,
        last_poll_counts=dict(row.last_poll_counts or {}),
    )


def get_intake(
    db: Session, principal: Principal, keyring: Keyring, *, instance_enabled: bool
) -> MailIntakeOut:
    return _out(get_row(db, _household(principal)), keyring, instance_enabled=instance_enabled)


def _sender_roles(db: Session, household_id: uuid.UUID) -> dict[uuid.UUID, HouseholdRole]:
    rows = db.execute(
        select(HouseholdMember.user_id, HouseholdMember.role).where(
            HouseholdMember.household_id == household_id
        )
    ).all()
    return dict(rows)


def update_intake(
    db: Session,
    principal: Principal,
    body: MailIntakeUpdate,
    keyring: Keyring,
    *,
    instance_enabled: bool,
    request: HTTPConnection | None = None,
) -> MailIntakeOut:
    household_id = _household(principal)
    row = _ensure_row(db, household_id, keyring)
    changed: list[str] = []
    given = body.model_fields_set
    if "allowed_senders" in given and body.allowed_senders is not None:
        roles = _sender_roles(db, household_id)
        seen: set[str] = set()
        senders: list[dict[str, Any]] = []
        for sender in body.allowed_senders:
            if roles.get(sender.user_id) not in SENDER_ROLES:
                raise ProblemError(
                    422,
                    "invalid-sender-user",
                    "Invalid sender",
                    "Each allowed sender must belong to a member, admin or owner.",
                )
            if sender.address not in seen:
                seen.add(sender.address)
                senders.append({"address": sender.address, "user_id": str(sender.user_id)})
        row.allowed_senders = senders
        changed.append("allowed_senders")
    for name in ("host", "port", "inbox_folder", "processed_folder", "rejected_folder"):
        if name in given and getattr(body, name) is not None:
            setattr(row, name, getattr(body, name))
            changed.append(name)
    for name in ("member_addresses", "require_auth_results"):
        if name in given and getattr(body, name) is not None:
            setattr(row, name, getattr(body, name))
            changed.append(name)
    if "authserv_id" in given:
        row.authserv_id = body.authserv_id or None
        changed.append("authserv_id")
    if "username" in given and body.username is not None:
        row.username_encrypted = keyring.encrypt_str(
            body.username, ad=_ad(row.id, "username_encrypted")
        )
        changed.append("username")
    if "password" in given and body.password is not None:
        row.password_encrypted = keyring.encrypt_str(
            body.password, ad=_ad(row.id, "password_encrypted")
        )
        changed.append("password")
    if "enabled" in given and body.enabled is not None:
        if body.enabled and (
            row.host is None or row.username_encrypted is None or row.password_encrypted is None
        ):
            raise ProblemError(
                422,
                "mail-intake-incomplete",
                "Mailbox incomplete",
                "Set the host, username and password before enabling email intake.",
            )
        row.enabled = body.enabled
        changed.append("enabled")
    if any(name in changed for name in _CREDENTIAL_FIELDS):
        row.last_poll_outcome = None
    db.flush()
    audit(
        db,
        action="mail_intake.update",
        target_type="mail_intake",
        target_id=row.id,
        after={"changed": sorted(set(changed)), "enabled": row.enabled},
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    return _out(row, keyring, instance_enabled=instance_enabled)


def rotate_alias(
    db: Session, principal: Principal, keyring: Keyring, *, request: HTTPConnection | None = None
) -> AliasOut:
    household_id = _household(principal)
    row = _ensure_row(db, household_id, keyring)
    token = new_alias()
    row.alias_encrypted = keyring.encrypt_str(token, ad=_ad(row.id, "alias_encrypted"))
    row.alias_rotated_at = clock.utcnow()
    db.flush()
    audit(
        db,
        action="mail_intake.alias_rotate",
        target_type="mail_intake",
        target_id=row.id,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    return AliasOut(alias_token=token, alias_rotated_at=row.alias_rotated_at)


# ---------------------------------------------------------------------------
# Used by the poller and the connection test
# ---------------------------------------------------------------------------


def mailbox_config(row: MailIntake, keyring: Keyring) -> MailboxConfig | None:
    """The decrypted connection settings, or ``None`` while incomplete."""
    username = _decrypt(keyring, row, "username_encrypted")
    password = _decrypt(keyring, row, "password_encrypted")
    if row.host is None or username is None or password is None:
        return None
    return MailboxConfig(
        host=row.host,
        port=row.port,
        username=username,
        password=password,
        inbox_folder=row.inbox_folder,
        processed_folder=row.processed_folder,
        rejected_folder=row.rejected_folder,
    )


def acceptance_policy(db: Session, row: MailIntake, keyring: Keyring) -> AcceptancePolicy:
    """The policy with the sender map as it stands now (module doc, "Senders")."""
    members = db.execute(
        select(HouseholdMember.user_id, HouseholdMember.role, User.email)
        .join(User, User.id == HouseholdMember.user_id)
        .where(HouseholdMember.household_id == row.household_id)
        .order_by(HouseholdMember.created_at, HouseholdMember.id)
    ).all()
    eligible = {user_id for user_id, role, _ in members if role in SENDER_ROLES}
    senders: dict[str, uuid.UUID] = {}
    for entry in row.allowed_senders:
        try:
            user_id = uuid.UUID(str(entry.get("user_id")))
        except ValueError:
            continue
        address = str(entry.get("address", "")).lower()
        if user_id in eligible and address:
            senders.setdefault(address, user_id)
    if row.member_addresses:
        for user_id, role, email in members:
            if role in SENDER_ROLES and email:
                senders.setdefault(email.strip().lower(), user_id)
    return AcceptancePolicy(
        alias_token=alias_token(keyring, row),
        senders=senders,
        require_auth_results=row.require_auth_results,
        authserv_id=row.authserv_id,
    )


def record_poll(row: MailIntake, outcome: str, counts: dict[str, Any] | None = None) -> None:
    row.last_poll_at = clock.utcnow()
    row.last_poll_outcome = outcome
    row.last_poll_counts = counts or {}


def check(
    db: Session, principal: Principal, keyring: Keyring, options: ConnectOptions
) -> MailTestOut:
    """Log in and look at the inbox read-only (nothing is fetched or moved)."""
    row = get_row(db, _household(principal))
    config = mailbox_config(row, keyring) if row is not None else None
    if config is None:
        raise not_configured()
    result = check_connection(config, options)
    return MailTestOut(
        ok=result.outcome == "ok",
        outcome=result.outcome,
        messages=result.messages,
        move_supported=result.move_supported,
    )
