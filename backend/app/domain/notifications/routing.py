# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which events become notifications, and the one sentence each one sends.

Call sites name the household event they just recorded
(:func:`notify`); the routing table below maps it to a notification
**kind** and a fixed, generic sentence. For every enabled channel of the
household subscribed to that kind, one ``send_notification`` job is queued
in the caller's transaction (so nothing is sent if the change rolls back),
with the dedupe key ``notify:<channel>:<source>`` (a source, e.g. an event,
is delivered to a channel at most once).

The sentences carry **no identity and no financial detail**: no names,
usernames, e-mail or IP addresses, no amounts, counts, accounts or dates
(docs/security/notifications.md, "What leaves the host"). The job payload
holds only the channel id, the kind, the routed event type and a delivery
id; the sentence is looked up again when the job runs.

========================================  ======================  =========
event                                     kind                    control
========================================  ======================  =========
``user.locked``                           ``account_locked``      A1
``security.recovery_code_used``           ``recovery_code_used``  A8
``user.mfa_reset``                        ``mfa_reset``           A8
``member.role_changed`` / ``.added`` /    ``role_changed``        I2
``.removed``
``security.new_device``                   ``new_device``          A6
``review.waiting`` (daily, open items)    ``review_waiting``
``tax.reminder``                          ``tax_reminder``
``backup.failed``                         ``backup_failed``
``ai_client.created``                     ``ai_client_created``   E3
========================================  ======================  =========
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Final, Literal, get_args

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.ids import uuid7
from app.db.queue import enqueue
from app.domain.notifications.models import NotificationChannel

SEND_JOB_TYPE: Final = "send_notification"
REVIEW_DISPATCH_JOB_TYPE: Final = "notify_review_waiting"
SEND_MAX_ATTEMPTS: Final = 6

Kind = Literal[
    "account_locked",
    "recovery_code_used",
    "mfa_reset",
    "role_changed",
    "new_device",
    "review_waiting",
    "tax_reminder",
    "backup_failed",
    "ai_client_created",
]
KINDS: Final[tuple[str, ...]] = get_args(Kind)
#: The kind of the message sent by ``POST .../test`` (not subscribable).
TEST_KIND: Final = "test"
TEST_MESSAGE: Final = "This is a test notification. The channel works."


@dataclass(frozen=True)
class Route:
    kind: str
    message: str


ROUTES: Final[dict[str, Route]] = {
    "user.locked": Route(
        "account_locked",
        "A household member's account was locked after repeated failed sign-in attempts.",
    ),
    "security.recovery_code_used": Route(
        "recovery_code_used",
        "A recovery code was used to sign in to a household member's account.",
    ),
    "user.mfa_reset": Route(
        "mfa_reset", "An administrator reset a household member's second factors."
    ),
    "member.role_changed": Route("role_changed", "A household member's role was changed."),
    "member.added": Route("role_changed", "Someone was added to the household."),
    "member.removed": Route("role_changed", "Someone was removed from the household."),
    "security.new_device": Route(
        "new_device", "A household member signed in from a new device or browser."
    ),
    "review.waiting": Route("review_waiting", "Items are waiting for you in the review queue."),
    "tax.reminder": Route(
        "tax_reminder", "Expected tax documents for last year are still missing."
    ),
    "backup.failed": Route("backup_failed", "A backup did not complete. Please check the server."),
    "ai_client.created": Route(
        "ai_client_created", "A new AI assistant connection was created for the household."
    ),
}


class SendNotificationJob(BaseModel):
    """Payload of ``send_notification`` (no message text, no identity)."""

    model_config = ConfigDict(extra="forbid")

    channel_id: uuid.UUID
    event_type: str
    delivery_id: uuid.UUID


class ReviewWaitingDispatch(BaseModel):
    model_config = ConfigDict(extra="forbid")


def message_for(event_type: str) -> Route | None:
    return ROUTES.get(event_type)


def subscribed_channels(db: Session, household_id: uuid.UUID, kind: str) -> list[uuid.UUID]:
    """Enabled channels of the household that receive ``kind``."""
    rows = db.execute(
        select(NotificationChannel.id, NotificationChannel.events)
        .where(
            NotificationChannel.household_id == household_id,
            NotificationChannel.enabled.is_(True),
        )
        .order_by(NotificationChannel.id)
    ).all()
    return [row_id for row_id, events in rows if kind in (events or [])]


def notify(
    db: Session,
    household_id: uuid.UUID,
    event_type: str,
    *,
    source_id: uuid.UUID | str | None = None,
) -> list[uuid.UUID]:
    """Queue ``send_notification`` for each subscribed channel; return the job ids.

    Runs in the caller's transaction and RLS context (which must be
    ``household_id``'s). An event type that is not routed queues nothing.
    """
    route = ROUTES.get(event_type)
    if route is None:
        return []
    source = str(source_id) if source_id is not None else str(uuid7())
    jobs: list[uuid.UUID] = []
    for channel_id in subscribed_channels(db, household_id, route.kind):
        jobs.append(
            enqueue(
                db,
                SEND_JOB_TYPE,
                SendNotificationJob(
                    channel_id=channel_id, event_type=event_type, delivery_id=uuid7()
                ),
                household_id=household_id,
                max_attempts=SEND_MAX_ATTEMPTS,
                dedupe_key=f"notify:{channel_id}:{source}"[:200],
            )
        )
    return jobs
