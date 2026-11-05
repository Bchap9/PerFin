# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notification channels of a household (API.md ``/settings/notifications``).

* **Who.** Owners and admins of the household, on a browser session; every
  change (create, update, delete) also needs fresh step-up MFA (the router).
* **Encryption (D2).** The whole configuration is one envelope bound to its
  row and column; credentials are write-only (``secrets_set`` lists the
  stored ones). The webhook signing secret (32 random bytes, URL-safe
  base64) is made here and returned only by create, ``rotate_secret`` and
  an update that moves the webhook to another host or port.
* **Destinations.** https URLs or an SMTP host; a host outside
  ``PERFIN_NOTIFICATIONS_ALLOWED_HOSTS`` (when set) is refused with ``422
  notification-host-not-allowed``. Moving a channel to another destination
  needs its credentials typed again (``422 notification-secret-required``),
  so a stored token or password is never sent to a new host it was not
  entered for. A webhook moved to another host or port gets a new signing
  secret, returned once like on create, so bodies signed for one receiver
  cannot be replayed to the other.
* At most :data:`~app.domain.notifications.models.CHANNELS_MAX` channels
  (``409 notification-channel-limit``).
* Every change is audited (``notification_channel.create|update|delete``
  with the type, the kinds and the names of the changed fields, never
  values).
* :func:`deliver` sends one notification through a channel and records the
  outcome (used by the ``send_notification`` job and ``POST .../test``).
"""

from __future__ import annotations

import json
import secrets
import uuid
from typing import Any, Final

import httpx
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.authz import forbidden
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.crypto import Keyring, column_ad
from app.core.errors import ProblemError
from app.core.ids import uuid7
from app.domain.notifications.models import CHANNELS_MAX, NotificationChannel
from app.domain.notifications.routing import TEST_KIND, TEST_MESSAGE
from app.domain.notifications.schemas import (
    CONFIG_MODELS,
    DESTINATION_FIELDS,
    SECRET_FIELDS,
    ChannelConfigOut,
    ChannelCreate,
    ChannelOut,
    ChannelSavedOut,
    ChannelTestOut,
    ChannelUpdate,
)
from app.integrations.notifications.base import (
    ChannelError,
    ChannelType,
    DeliveryOptions,
    Notification,
)
from app.integrations.notifications.registry import build_channel

TABLE: Final = "notification_channel"
COLUMN: Final = "config_encrypted"
_PUBLIC_FIELDS: Final = tuple(ChannelConfigOut.model_fields)


def _ad(row_id: uuid.UUID) -> bytes:
    return column_ad(TABLE, COLUMN, row_id)


def _household(principal: Principal) -> uuid.UUID:
    if principal.household_id is None:
        raise forbidden("You are not a member of a household.")
    return principal.household_id


def not_found() -> ProblemError:
    return ProblemError(
        404, "notification-channel-not-found", "Channel not found", "No such channel."
    )


def new_signing_secret() -> str:
    return secrets.token_urlsafe(32)


def load_config(keyring: Keyring, row: NotificationChannel) -> dict[str, Any]:
    data = json.loads(keyring.decrypt_str(row.config_encrypted, ad=_ad(row.id)))
    if not isinstance(data, dict):  # pragma: no cover - written by this module only
        raise ValueError("channel configuration is not an object")
    return data


def _store(keyring: Keyring, row: NotificationChannel, config: dict[str, Any]) -> None:
    text = json.dumps(config, separators=(",", ":"), sort_keys=True)
    row.config_encrypted = keyring.encrypt_str(text, ad=_ad(row.id))


def _out(
    row: NotificationChannel, config: dict[str, Any], *, signing_secret: str | None = None
) -> ChannelSavedOut:
    hidden = SECRET_FIELDS[row.type]
    public = {k: config.get(k) for k in _PUBLIC_FIELDS if k not in hidden}
    return ChannelSavedOut(
        id=row.id,
        name=row.name,
        type=row.type,
        enabled=row.enabled,
        events=list(row.events or []),  # type: ignore[arg-type]
        config=ChannelConfigOut.model_validate(public),
        secrets_set=[name for name in hidden if config.get(name)],
        last_sent_at=row.last_sent_at,
        last_outcome=row.last_outcome,  # type: ignore[arg-type]
        created_at=row.created_at,
        updated_at=row.updated_at,
        signing_secret=signing_secret,
    )


def _destination(channel_type: ChannelType, config: dict[str, Any]) -> tuple[str, int]:
    """Host and port a configuration sends to."""
    if channel_type is ChannelType.SMTP:
        return str(config["host"]), int(config["port"])
    field = DESTINATION_FIELDS[channel_type][0]
    url = httpx.URL(str(config[field]))
    return url.host, url.port or 443


def _check_destination(
    channel_type: ChannelType, config: dict[str, Any], options: DeliveryOptions
) -> None:
    host, port = _destination(channel_type, config)
    if not options.host_allowed(host, port):
        raise ProblemError(
            422,
            "notification-host-not-allowed",
            "Destination not allowed",
            "This PerFin instance only sends notifications to the hosts its administrator "
            "listed in PERFIN_NOTIFICATIONS_ALLOWED_HOSTS.",
        )


def _get(
    db: Session, household_id: uuid.UUID, channel_id: uuid.UUID, *, lock: bool = False
) -> NotificationChannel:
    query = select(NotificationChannel).where(
        NotificationChannel.household_id == household_id, NotificationChannel.id == channel_id
    )
    if lock:
        query = query.with_for_update()
    row = db.execute(query).scalar_one_or_none()
    if row is None:
        raise not_found()
    return row


def _audit(
    db: Session,
    principal: Principal,
    row: NotificationChannel,
    action: str,
    after: dict[str, Any],
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=action,
        target_type=TABLE,
        target_id=row.id,
        after={"type": row.type.value, **after},
        household_id=row.household_id,
        user_id=principal.user_id,
        request=request,
    )


def list_channels(db: Session, principal: Principal, keyring: Keyring) -> list[ChannelOut]:
    household_id = _household(principal)
    rows = db.execute(
        select(NotificationChannel)
        .where(NotificationChannel.household_id == household_id)
        .order_by(NotificationChannel.created_at, NotificationChannel.id)
    ).scalars()
    return [ChannelOut.model_validate(_out(r, load_config(keyring, r)).model_dump()) for r in rows]


def create_channel(
    db: Session,
    principal: Principal,
    body: ChannelCreate,
    keyring: Keyring,
    options: DeliveryOptions,
    *,
    request: HTTPConnection | None = None,
) -> ChannelSavedOut:
    household_id = _household(principal)
    count = db.execute(
        select(func.count())
        .select_from(NotificationChannel)
        .where(NotificationChannel.household_id == household_id)
    ).scalar_one()
    if count >= CHANNELS_MAX:
        raise ProblemError(
            409,
            "notification-channel-limit",
            "Too many channels",
            f"A household can have at most {CHANNELS_MAX} notification channels.",
        )
    channel_type = ChannelType(body.config.type)
    config = body.config.model_dump(exclude_none=True)
    signing_secret: str | None = None
    if channel_type is ChannelType.WEBHOOK:
        signing_secret = new_signing_secret()
        config["secret"] = signing_secret
    _check_destination(channel_type, config, options)
    row = NotificationChannel(
        id=uuid7(),
        household_id=household_id,
        name=body.name,
        type=channel_type,
        events=list(dict.fromkeys(body.events)),
        enabled=body.enabled,
    )
    _store(keyring, row, config)
    db.add(row)
    db.flush()
    db.refresh(row)
    _audit(
        db,
        principal,
        row,
        "notification_channel.create",
        {"events": row.events, "enabled": row.enabled},
        request,
    )
    return _out(row, config, signing_secret=signing_secret)


def _merge_config(
    channel_type: ChannelType, stored: dict[str, Any], given: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    if given.get("type", channel_type.value) != channel_type.value:
        raise ProblemError(
            422,
            "notification-type-immutable",
            "Type cannot change",
            "Create a new channel for another kind of service.",
        )
    changes = {k: v for k, v in given.items() if k != "type"}
    merged = {**stored, **changes, "type": channel_type.value}
    merged = {k: v for k, v in merged.items() if v is not None}
    secret = merged.pop("secret", None)  # the webhook secret is not an input field
    try:
        validated = CONFIG_MODELS[channel_type].model_validate(merged)
    except ValidationError as exc:
        raise ProblemError(
            422,
            "validation-failed",
            "Invalid channel settings",
            "; ".join(
                f"{'.'.join(str(p) for p in err['loc']) or 'config'}: {err['msg']}"
                for err in exc.errors(include_input=False, include_url=False)
            ),
        ) from None
    config = validated.model_dump(exclude_none=True)
    if secret is not None:
        config["secret"] = secret
    moved = any(config.get(f) != stored.get(f) for f in DESTINATION_FIELDS[channel_type])
    if moved:
        for name in SECRET_FIELDS[channel_type]:
            if name != "secret" and stored.get(name) and name not in changes:
                raise ProblemError(
                    422,
                    "notification-secret-required",
                    "Credentials needed",
                    "Type the channel's credentials again when you change where it sends to.",
                )
    changed = sorted(k for k in set(config) | set(stored) if config.get(k) != stored.get(k))
    return config, changed


def update_channel(
    db: Session,
    principal: Principal,
    channel_id: uuid.UUID,
    body: ChannelUpdate,
    keyring: Keyring,
    options: DeliveryOptions,
    *,
    request: HTTPConnection | None = None,
) -> ChannelSavedOut:
    household_id = _household(principal)
    row = _get(db, household_id, channel_id, lock=True)
    stored = load_config(keyring, row)
    config = dict(stored)
    changed: list[str] = []
    given = body.model_fields_set
    if "name" in given and body.name is not None and body.name != row.name:
        row.name = body.name
        changed.append("name")
    if "enabled" in given and body.enabled is not None and body.enabled != row.enabled:
        row.enabled = body.enabled
        changed.append("enabled")
    if "events" in given and body.events is not None:
        events: list[str] = list(dict.fromkeys(body.events))
        if events != list(row.events or []):
            row.events = events
            changed.append("events")
    if body.config is not None:
        given_config = body.config.model_dump(mode="json", exclude_unset=True)
        config, fields = _merge_config(row.type, stored, given_config)
        changed.extend(f"config.{f}" for f in fields)
        _check_destination(row.type, config, options)
    signing_secret: str | None = None
    if body.rotate_secret and row.type is not ChannelType.WEBHOOK:
        raise ProblemError(
            422,
            "notification-no-signing-secret",
            "Nothing to rotate",
            "Only webhook channels have a signing secret.",
        )
    # A webhook moved to another host or port gets a new signing secret, so a
    # receiver the old secret was shared with cannot be fed bodies signed for
    # the new one (or the other way round) and replay them (P7-SR-R1-05).
    readdressed = row.type is ChannelType.WEBHOOK and _destination(
        row.type, config
    ) != _destination(row.type, stored)
    if body.rotate_secret or readdressed:
        signing_secret = new_signing_secret()
        config["secret"] = signing_secret
        changed.append("config.secret")
    if config != stored:
        _store(keyring, row, config)
        row.last_outcome = None
    if changed:
        row.updated_at = clock.utcnow()
    db.flush()
    _audit(
        db,
        principal,
        row,
        "notification_channel.update",
        {"changed": sorted(set(changed)), "events": row.events, "enabled": row.enabled},
        request,
    )
    return _out(row, config, signing_secret=signing_secret)


def delete_channel(
    db: Session,
    principal: Principal,
    channel_id: uuid.UUID,
    *,
    request: HTTPConnection | None = None,
) -> None:
    household_id = _household(principal)
    row = _get(db, household_id, channel_id, lock=True)
    _audit(db, principal, row, "notification_channel.delete", {"name": row.name}, request)
    db.delete(row)
    db.flush()


def deliver(
    row: NotificationChannel,
    notification: Notification,
    keyring: Keyring,
    options: DeliveryOptions,
) -> str:
    """Send through ``row``; record and return the outcome code (never raises ChannelError)."""
    try:
        config = load_config(keyring, row)
        build_channel(row.type, config, options).send(notification)
        outcome = "ok"
    except ChannelError as exc:
        outcome = exc.code
    except (KeyError, TypeError, ValueError):
        outcome = "protocol_error"  # a stored configuration this version cannot use
    row.last_outcome = outcome
    row.last_sent_at = clock.utcnow()
    return outcome


def send_test(
    db: Session,
    principal: Principal,
    channel_id: uuid.UUID,
    keyring: Keyring,
    options: DeliveryOptions,
) -> ChannelTestOut:
    """Send the fixed test sentence now and report the outcome."""
    row = _get(db, _household(principal), channel_id, lock=True)
    notification = Notification(
        id=uuid7(), kind=TEST_KIND, message=TEST_MESSAGE, sent_at=clock.utcnow()
    )
    outcome = deliver(row, notification, keyring, options)
    db.flush()
    return ChannelTestOut(ok=outcome == "ok", outcome=outcome)  # type: ignore[arg-type]
