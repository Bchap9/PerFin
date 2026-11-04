# SPDX-License-Identifier: AGPL-3.0-or-later
"""Notification jobs (``app.domain.notifications``).

* ``send_notification`` (household job, queued by
  :func:`app.domain.notifications.routing.notify`): deliver one generic
  notification through one channel. A failure the service may get over
  (``connect_failed``, ``timeout``, ``server_error``) is retried with the
  queue's exponential backoff, up to six attempts; the last attempt's
  outcome, or a permanent one (``blocked``, ``tls_failed``,
  ``auth_failed``, ``rejected``, ...), is recorded on the channel
  (``last_outcome``) and the job ends. A channel deleted, disabled or
  unsubscribed from the kind meanwhile sends nothing.
* ``notify_review_waiting`` (daily, instance-level): for each household
  with open review items, routes ``review.waiting`` (one notification per
  household and day, whatever the number of items).

Settings and the connection policy come from this module's
:func:`delivery_options` and :func:`get_settings` (tests patch them).
"""

from __future__ import annotations

from datetime import timedelta
from typing import Final

from sqlalchemy import func, select

from app.core import clock
from app.core.crypto import Keyring
from app.core.logging import get_logger
from app.core.settings import get_settings
from app.db.rls import set_context
from app.domain.documents.models import ReviewItem, ReviewItemState
from app.domain.notifications import service
from app.domain.notifications.models import NotificationChannel
from app.domain.notifications.routing import (
    REVIEW_DISPATCH_JOB_TYPE,
    SEND_JOB_TYPE,
    SEND_MAX_ATTEMPTS,
    ReviewWaitingDispatch,
    SendNotificationJob,
    message_for,
    notify,
    subscribed_channels,
)
from app.integrations.notifications.base import RETRYABLE, DeliveryOptions, Notification
from app.workers.handlers.networth import household_ids
from app.workers.registry import JobContext, PermanentJobError, job_handler
from app.workers.scheduler import recurring

log = get_logger(__name__)

REVIEW_INTERVAL: Final = timedelta(days=1)


class DeliveryRetryError(Exception):
    """A retryable outcome before the last attempt (the queue retries the job)."""


def delivery_options() -> DeliveryOptions:
    return DeliveryOptions.from_settings(get_settings())


@job_handler(
    SEND_JOB_TYPE, payload=SendNotificationJob, timeout=120.0, max_attempts=SEND_MAX_ATTEMPTS
)
def send_notification(ctx: JobContext, payload: SendNotificationJob) -> None:
    if ctx.household_id is None:
        raise PermanentJobError("send_notification needs a household")
    route = message_for(payload.event_type)
    if route is None:
        raise PermanentJobError("event type is not routed")
    row = ctx.session.execute(
        select(NotificationChannel).where(
            NotificationChannel.household_id == ctx.household_id,
            NotificationChannel.id == payload.channel_id,
        )
    ).scalar_one_or_none()
    if row is None or not row.enabled or route.kind not in (row.events or []):
        log.info("notification skipped", extra={"kind": route.kind})
        return
    notification = Notification(
        id=payload.delivery_id, kind=route.kind, message=route.message, sent_at=clock.utcnow()
    )
    outcome = service.deliver(
        row, notification, Keyring.from_settings(get_settings()), delivery_options()
    )
    if outcome in RETRYABLE and ctx.attempt < ctx.max_attempts:
        # Rolled back with the job: last_outcome is written by the final attempt.
        raise DeliveryRetryError(outcome)
    extra = {"kind": route.kind, "channel_type": row.type.value, "outcome": outcome}
    if outcome == "ok":
        log.info("notification sent", extra=extra)
    else:
        log.warning("notification not delivered", extra=extra)


@job_handler(REVIEW_DISPATCH_JOB_TYPE, payload=ReviewWaitingDispatch, timeout=300.0)
def notify_review_waiting(ctx: JobContext, payload: ReviewWaitingDispatch) -> None:
    if ctx.household_id is not None:
        raise PermanentJobError("notify_review_waiting is an instance-level job")
    day = clock.utcnow().date().isoformat()
    queued = 0
    for household_id in household_ids(ctx.session):
        ctx.check_cancelled()
        set_context(ctx.session, household_id)
        if not subscribed_channels(ctx.session, household_id, "review_waiting"):
            continue
        waiting = ctx.session.execute(
            select(func.count())
            .select_from(ReviewItem)
            .where(
                ReviewItem.household_id == household_id,
                ReviewItem.state == ReviewItemState.OPEN,
            )
        ).scalar_one()
        if waiting:
            queued += len(
                notify(ctx.session, household_id, "review.waiting", source_id=f"review:{day}")
            )
    set_context(ctx.session, None)
    log.info("review waiting notifications", extra={"count": queued})


recurring(REVIEW_DISPATCH_JOB_TYPE, REVIEW_INTERVAL, ReviewWaitingDispatch())
