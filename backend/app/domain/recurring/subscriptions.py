# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subscriptions and bills: the list with its cost, one series, a decision, what changed.

A subscription is a recurring series (``scheduled_transaction``) of kind
``subscription`` or ``bill``; what changed about one is a
``subscription_event``. Everything is limited to the accounts the caller may
see; a decision needs a member who may edit the series' account. A series of
another household, or on a hidden account, is ``404 schedule-not-found``.

This is the declaration the router is built against: the events list is
empty for now, and the other operations answer ``409 not-ready`` once the
caller and the series they name are checked.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.domain.recurring import service as recurring_service
from app.domain.recurring.schemas import (
    EventsSeen,
    EventsSeenOut,
    SubscriptionDecision,
    SubscriptionDetail,
    SubscriptionEventPage,
    SubscriptionList,
    SubscriptionOut,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection


def not_ready() -> ProblemError:
    return ProblemError(409, "not-ready", "Not available yet", "This is not available yet.")


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def list_subscriptions(
    db: Session,
    principal: Principal,
    *,
    kind: Collection[str] | None,
    status: Collection[str] | None,
    account_ids: Collection[uuid.UUID] | None,
) -> SubscriptionList:
    """Subscriptions and bills on visible accounts, with what they cost."""
    del db, kind, status, account_ids
    _household(principal, HouseholdRole.VIEWER)
    raise not_ready()


def get_subscription(
    db: Session, principal: Principal, schedule_id: uuid.UUID
) -> SubscriptionDetail:
    """One series with its charges and its changes."""
    recurring_service.get_row(db, principal, schedule_id)
    raise not_ready()


def decide(
    db: Session,
    principal: Principal,
    schedule_id: uuid.UUID,
    data: SubscriptionDecision,
    *,
    request: HTTPConnection | None,
) -> SubscriptionOut:
    """Record a person's decision about a series; detection never undoes it."""
    del data, request
    recurring_service.require_editable(db, principal, schedule_id)
    raise not_ready()


def list_events(
    db: Session,
    principal: Principal,
    *,
    state: str | None,
    cursor: uuid.UUID | None,
    limit: int,
) -> SubscriptionEventPage:
    """One page of what changed on visible accounts, newest first."""
    del db, state, cursor, limit
    _household(principal, HouseholdRole.VIEWER)
    return SubscriptionEventPage(items=[], next_cursor=None, open=0)


def mark_seen(db: Session, principal: Principal, data: EventsSeen) -> EventsSeenOut:
    """Mark changes as seen; ids the caller cannot see are ignored."""
    del db, data
    _household(principal, HouseholdRole.MEMBER)
    raise not_ready()
