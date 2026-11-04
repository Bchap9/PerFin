# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notification channel rows for tests (flush-only, like ``tests/fixtures/ledger.py``).

``make_channel`` stores a configuration exactly as the service does (one
envelope bound to the row). The default is a webhook to an address that
never resolves (``.invalid``), so a channel made only to exist sends
nothing anywhere.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy.orm import Session

from app.core.crypto import Keyring, column_ad
from app.core.ids import uuid7
from app.domain.notifications.models import NotificationChannel
from app.domain.notifications.routing import KINDS
from app.integrations.notifications.base import ChannelType
from tests.fixtures.ledger import household_context

DEFAULT_CONFIG: dict[str, Any] = {
    "type": "webhook",
    "url": "https://hooks.perfin.invalid/notify",
    "secret": "synthetic-signing-secret-0123456789",
}


def make_channel(
    db: Session,
    household_id: uuid.UUID,
    keyring: Keyring,
    *,
    config: dict[str, Any] | None = None,
    events: Sequence[str] = KINDS,
    enabled: bool = True,
    name: str = "Test channel",
) -> NotificationChannel:
    data = dict(config or DEFAULT_CONFIG)
    row_id = uuid7()
    row = NotificationChannel(
        id=row_id,
        household_id=household_id,
        name=name,
        type=ChannelType(data["type"]),
        events=list(events),
        enabled=enabled,
        config_encrypted=keyring.encrypt_str(
            json.dumps(data, sort_keys=True),
            ad=column_ad("notification_channel", "config_encrypted", row_id),
        ),
    )
    with household_context(db, household_id):
        db.add(row)
        db.flush()
    return row
