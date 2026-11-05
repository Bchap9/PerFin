# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/settings/notifications/channels``: where household notifications go (API.md).

Every route needs a **browser session** of an owner or admin of the active
household (tokens are refused: channels hold credentials).

* ``GET /settings/notifications/channels``: the channels, their kinds, the
  non-secret configuration, ``secrets_set`` and the last outcome.
* ``POST /settings/notifications/channels`` (**step-up**): create; a
  webhook's signing secret is in the answer once (``signing_secret``).
* ``PATCH /settings/notifications/channels/{channel_id}`` (**step-up**):
  rename, enable/disable, change kinds or configuration (credentials
  write-only, typed again when the destination changes),
  ``rotate_secret`` for a webhook.
* ``DELETE /settings/notifications/channels/{channel_id}`` (**step-up**).
* ``POST /settings/notifications/channels/{channel_id}/test``: send the
  fixed test sentence now; ``200 {"ok", "outcome"}`` with a short code,
  never the service's reply.

Rules in ``app.domain.notifications.service``; what is sent in
docs/security/notifications.md.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.api.routers.accounts import PROBLEMS
from app.api.routers.settings_intake import AdminSession, AppKeyring, SteppedUp
from app.auth.deps import DbSession, get_app_settings
from app.domain.notifications import service
from app.domain.notifications.schemas import (
    ChannelCreate,
    ChannelOut,
    ChannelSavedOut,
    ChannelTestOut,
    ChannelUpdate,
)
from app.integrations.notifications.base import DeliveryOptions

router = APIRouter(tags=["settings"])

BASE = "/settings/notifications/channels"


def delivery_options(request: Request) -> DeliveryOptions:
    """How the API reaches services (``app.state.notification_options`` in tests)."""
    override = getattr(request.app.state, "notification_options", None)
    if isinstance(override, DeliveryOptions):
        return override
    return DeliveryOptions.from_settings(get_app_settings(request))


Options = Annotated[DeliveryOptions, Depends(delivery_options)]


@router.get(
    BASE, response_model=list[ChannelOut], responses=PROBLEMS, summary="Notification channels"
)
def list_channels(db: DbSession, principal: AdminSession, keyring: AppKeyring) -> list[ChannelOut]:
    return service.list_channels(db, principal, keyring)


@router.post(
    BASE,
    status_code=201,
    response_model=ChannelSavedOut,
    responses=PROBLEMS,
    summary="Add a notification channel (step-up)",
)
def create_channel(
    body: ChannelCreate,
    request: Request,
    db: DbSession,
    principal: SteppedUp,
    keyring: AppKeyring,
    options: Options,
) -> ChannelSavedOut:
    return service.create_channel(db, principal, body, keyring, options, request=request)


@router.patch(
    BASE + "/{channel_id}",
    response_model=ChannelSavedOut,
    responses=PROBLEMS,
    summary="Change a notification channel (step-up)",
)
def update_channel(
    channel_id: uuid.UUID,
    body: ChannelUpdate,
    request: Request,
    db: DbSession,
    principal: SteppedUp,
    keyring: AppKeyring,
    options: Options,
) -> ChannelSavedOut:
    return service.update_channel(
        db, principal, channel_id, body, keyring, options, request=request
    )


@router.delete(
    BASE + "/{channel_id}",
    status_code=204,
    response_class=Response,
    responses=PROBLEMS,
    summary="Remove a notification channel (step-up)",
)
def delete_channel(
    channel_id: uuid.UUID, request: Request, db: DbSession, principal: SteppedUp
) -> Response:
    service.delete_channel(db, principal, channel_id, request=request)
    return Response(status_code=204)


@router.post(
    BASE + "/{channel_id}/test",
    response_model=ChannelTestOut,
    responses=PROBLEMS,
    summary="Send a test notification through a channel",
)
def test_channel(
    channel_id: uuid.UUID,
    db: DbSession,
    principal: AdminSession,
    keyring: AppKeyring,
    options: Options,
) -> ChannelTestOut:
    return service.send_test(db, principal, channel_id, keyring, options)
