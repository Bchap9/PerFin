# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subscriptions and bills (``/subscriptions``).

* ``GET /subscriptions?kind=&status=&account=``: any role; the recurring
  series of kind ``subscription`` and ``bill`` on visible accounts with their
  monthly and yearly cost (every status but ``dismissed`` by default).
* ``GET /subscriptions/events?state=&cursor=&limit=``: any role; what
  changed (a new one, a price, a missed charge...), newest first.
* ``POST /subscriptions/events/seen``: member or above; mark changes as seen.
* ``GET /subscriptions/{id}``: any role; one series with its charges and
  its changes (``404 schedule-not-found``).
* ``POST /subscriptions/{id}/decision``: member or above on an account the
  caller may edit; confirm, dismiss, pause, resume, end or reopen. A
  person's decision is never undone by detection.

Tokens need ``transactions:read`` / ``transactions:write``. Rules:
``app.domain.recurring.subscriptions``.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request

from app.api.routers.accounts import PROBLEMS, scoped_role
from app.api.routers.recurring import Accounts, ScheduleReader, ScheduleWriter
from app.auth.deps import DbSession
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.domain.recurring import subscriptions
from app.domain.recurring.schemas import (
    DEFAULT_EVENTS,
    MAX_EVENTS,
    EventsSeen,
    EventsSeenOut,
    EventState,
    SeriesStatus,
    SubscriptionDecision,
    SubscriptionDetail,
    SubscriptionEventPage,
    SubscriptionList,
    SubscriptionOut,
)

router = APIRouter(tags=["subscriptions"])

Reader = Annotated[Principal, Depends(scoped_role("transactions:read", HouseholdRole.VIEWER))]
Writer = Annotated[Principal, Depends(scoped_role("transactions:write", HouseholdRole.MEMBER))]
Kinds = Annotated[list[Literal["subscription", "bill"]] | None, Query(alias="kind", max_length=2)]
Statuses = Annotated[list[SeriesStatus] | None, Query(alias="status", max_length=6)]


@router.get(
    "/subscriptions",
    response_model=SubscriptionList,
    responses=PROBLEMS,
    summary="Subscriptions and bills I can see, with what they cost",
)
def get_subscriptions(
    db: DbSession,
    principal: Reader,
    kind: Kinds = None,
    status: Statuses = None,
    account: Accounts = None,
) -> SubscriptionList:
    return subscriptions.list_subscriptions(
        db, principal, kind=kind, status=status, account_ids=account
    )


# The two ``/subscriptions/events`` routes are declared above
# ``/subscriptions/{schedule_id}``: "events" is not a schedule id.
@router.get(
    "/subscriptions/events",
    response_model=SubscriptionEventPage,
    responses=PROBLEMS,
    summary="What changed about my subscriptions and bills",
)
def get_events(
    db: DbSession,
    principal: Reader,
    state: Annotated[EventState | None, Query()] = None,
    cursor: Annotated[uuid.UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_EVENTS)] = DEFAULT_EVENTS,
) -> SubscriptionEventPage:
    return subscriptions.list_events(db, principal, state=state, cursor=cursor, limit=limit)


@router.post(
    "/subscriptions/events/seen",
    response_model=EventsSeenOut,
    responses=PROBLEMS,
    summary="Mark changes as seen (member or above)",
)
def post_events_seen(body: EventsSeen, db: DbSession, principal: Writer) -> EventsSeenOut:
    return subscriptions.mark_seen(db, principal, body)


@router.get(
    "/subscriptions/{schedule_id}",
    response_model=SubscriptionDetail,
    responses=PROBLEMS,
    summary="One subscription or bill with its charges and its changes",
)
def get_subscription(
    schedule_id: uuid.UUID, db: DbSession, principal: ScheduleReader
) -> SubscriptionDetail:
    return subscriptions.get_subscription(db, principal, schedule_id)


@router.post(
    "/subscriptions/{schedule_id}/decision",
    response_model=SubscriptionOut,
    responses=PROBLEMS,
    summary="Confirm, dismiss, pause, resume, end or reopen one (member or above)",
)
def post_decision(
    schedule_id: uuid.UUID,
    body: SubscriptionDecision,
    request: Request,
    db: DbSession,
    principal: ScheduleWriter,
) -> SubscriptionOut:
    return subscriptions.decide(db, principal, schedule_id, body, request=request)
