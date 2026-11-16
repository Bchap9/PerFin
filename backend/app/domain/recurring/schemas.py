# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request and response schemas of ``/recurring`` and ``/cashflow/calendar`` (API.md).

Money is ``{"amount": "12.34", "currency": "USD"}`` (``app.core.money``);
schedule amounts are in the account's currency (``422 currency-mismatch``
otherwise). Confidence and regularity are decimal strings in ``0..1``.

A recurring series is a subscription, a bill, income or something else
(:data:`SeriesKind`) and stands somewhere (:data:`SeriesStatus`); each of the
two says who set it (:data:`Decider`). ``/subscriptions`` is the view of the
series of kind ``subscription`` and ``bill``: what they cost per month and
year, what changed (:data:`EventKind`) and a person's decision about one.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.core.dates import BusinessDate
from app.core.money import Money, MoneyIn
from app.domain.recurring.detection import Pattern
from app.domain.transactions.models import NAME_MAX, Cadence, ScheduleOrigin

#: Most schedules one list returns.
MAX_SCHEDULES: Final = 500
#: Calendar window bounds in days.
DEFAULT_CALENDAR_DAYS: Final = 30
MAX_CALENDAR_DAYS: Final = 366
#: Most account filters per request (as the reports).
MAX_FILTER_VALUES: Final = 50
#: Most charges one series' detail lists (newest first).
MAX_CHARGES: Final = 60
#: Page size bounds of the subscription events list.
DEFAULT_EVENTS: Final = 50
MAX_EVENTS: Final = 200
#: Most event ids one "seen" request may name.
MAX_SEEN_IDS: Final = 200

#: ``app.domain.transactions.models``: ``SERIES_KINDS``, ``SERIES_STATUSES``, ``DECIDERS``.
SeriesKind = Literal["subscription", "bill", "income", "other"]
SeriesStatus = Literal["suggested", "active", "confirmed", "paused", "ended", "dismissed"]
Decider = Literal["detector", "person"]
#: ``app.domain.recurring.models.EVENT_KINDS``.
EventKind = Literal[
    "new",
    "price_changed",
    "missed",
    "ended",
    "charged_after_end",
    "renewal_due",
    "trial_converted",
]
EventState = Literal["open", "seen"]

_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _clean_name(value: str) -> str:
    if _CONTROL_RE.search(value):
        raise ValueError("must be a single line without control characters")
    stripped = value.strip()
    if not stripped:
        raise ValueError("must not be blank")
    return stripped


ScheduleName = Annotated[str, Field(min_length=1, max_length=NAME_MAX), AfterValidator(_clean_name)]
IntervalCount = Annotated[int, Field(ge=1, le=366)]


def _unique_ids(values: list[uuid.UUID]) -> list[uuid.UUID]:
    if len(set(values)) != len(values):
        raise ValueError("ids must be unique")
    return values


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Output(BaseModel):
    model_config = ConfigDict(frozen=True)


class ScheduleCreate(_In):
    """A manual recurring transaction (``origin = manual``)."""

    account_id: uuid.UUID
    name: ScheduleName
    cadence: Cadence
    interval_count: IntervalCount = 1
    next_due: BusinessDate
    amount_min: MoneyIn
    amount_max: MoneyIn
    payee_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    active: bool = True


class ScheduleUpdate(_In):
    """Fields to change; absent fields are left alone, ``null`` clears payee/category.

    ``kind`` is the person's word on what the series is; detection does not
    change it afterwards. ``active: false`` pauses the series, ``active:
    true`` confirms it.
    """

    name: ScheduleName | None = None
    cadence: Cadence | None = None
    interval_count: IntervalCount | None = None
    next_due: BusinessDate | None = None
    amount_min: MoneyIn | None = None
    amount_max: MoneyIn | None = None
    payee_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    active: bool | None = None
    kind: SeriesKind | None = None


class ScheduleOut(_Output):
    id: uuid.UUID
    account_id: uuid.UUID
    name: str
    cadence: Cadence
    interval_count: int
    next_due: date
    amount_min: Money
    amount_max: Money
    payee_id: uuid.UUID | None
    category_id: uuid.UUID | None
    origin: ScheduleOrigin
    active: bool
    last_seen_on: date | None
    kind: SeriesKind
    status: SeriesStatus
    status_source: Decider
    #: The name to show for the series' merchant, when it has one.
    merchant: str | None


class ScheduleList(_Output):
    items: list[ScheduleOut]


class CandidateOut(_Output):
    """A detected pattern; ``schedule_id`` names the schedule it matches, if any."""

    account_id: uuid.UUID
    payee_id: uuid.UUID | None
    name: str
    pattern: Pattern
    cadence: Cadence
    interval_count: int
    amount_min: Money
    amount_max: Money
    category_id: uuid.UUID | None
    occurrences: int
    first_seen: date
    last_seen: date
    next_due: date
    regularity: str
    confidence: str
    schedule_id: uuid.UUID | None


class CandidateList(_Output):
    as_of: date
    items: list[CandidateOut]


class DetectIn(_In):
    """``POST /recurring/detect``: optionally only some accounts (each editable)."""

    account_ids: Annotated[list[uuid.UUID], Field(max_length=MAX_FILTER_VALUES)] | None = None


class DetectResult(_Output):
    as_of: date
    created: int
    updated: int
    unchanged: int
    skipped_manual: int
    items: list[CandidateOut]


class CalendarEvent(_Output):
    schedule_id: uuid.UUID
    name: str
    account_id: uuid.UUID
    kind: Literal["income", "bill"]
    amount: Money
    amount_min: Money
    amount_max: Money
    amount_base: Money | None


class CalendarDay(_Output):
    on: date
    events: list[CalendarEvent]
    income: Money
    bills: Money
    net: Money
    balance: Money


class CashflowCalendar(_Output):
    """``GET /cashflow/calendar``: expected income and bills, day by day."""

    currency: str
    date_from: date
    date_to: date
    opening_balance: Money
    days: list[CalendarDay]
    income: Money
    bills: Money
    net: Money
    unconverted: list[uuid.UUID]


# ---------------------------------------------------------------------------
# Subscriptions and bills
# ---------------------------------------------------------------------------


class PriceChange(_Output):
    """The latest price change of a series. ``pending``: seen on one charge
    only so far (the next charge at the new price confirms it)."""

    previous: Money
    current: Money
    on: date
    pending: bool


class SubscriptionOut(_Output):
    """A recurring series of kind ``subscription`` or ``bill``.

    ``amount`` is the latest charge (the middle of the range before one was
    seen); ``monthly_cost`` and ``yearly_cost`` spread it over the cadence, in
    the account's currency. ``overdue``: the expected charge did not arrive,
    judged against the account's latest row and never against today.
    ``open_events``: changes nobody has looked at yet.
    """

    id: uuid.UUID
    account_id: uuid.UUID
    name: str
    merchant: str | None
    kind: SeriesKind
    kind_source: Decider
    status: SeriesStatus
    status_source: Decider
    origin: ScheduleOrigin
    cadence: Cadence
    interval_count: int
    amount: Money
    amount_min: Money
    amount_max: Money
    monthly_cost: Money
    yearly_cost: Money
    next_due: date | None
    overdue: bool
    first_seen_on: date | None
    last_seen_on: date | None
    occurrences: int
    confidence: str | None
    category_id: uuid.UUID | None
    payee_id: uuid.UUID | None
    price_change: PriceChange | None
    open_events: int


class SubscriptionTotals(_Output):
    """What the active and confirmed series of one kind cost, in ``currency``
    (the household's). ``unconverted``: series left out for want of a rate."""

    currency: str
    monthly: Money
    yearly: Money
    count: int
    unconverted: list[uuid.UUID]


class SubscriptionList(_Output):
    """``GET /subscriptions``. ``needs_look``: suggested series plus series
    with an open change."""

    as_of: date
    subscriptions: SubscriptionTotals
    bills: SubscriptionTotals
    items: list[SubscriptionOut]
    needs_look: int


class SubscriptionCharge(_Output):
    transaction_id: uuid.UUID
    posted_date: date
    amount: Money


class SubscriptionEventOut(_Output):
    """One change of one series; ``name`` is the series' name."""

    id: uuid.UUID
    schedule_id: uuid.UUID
    name: str
    kind: EventKind
    occurred_on: date
    amount_before: Money | None
    amount_after: Money | None
    transaction_id: uuid.UUID | None
    state: EventState


class SubscriptionDetail(_Output):
    """One series with its charges (newest first, at most 60) and its changes."""

    subscription: SubscriptionOut
    charges: list[SubscriptionCharge] = Field(max_length=MAX_CHARGES)
    events: list[SubscriptionEventOut]


class SubscriptionEventPage(_Output):
    """One page of changes, newest first; ``open`` counts every open one."""

    items: list[SubscriptionEventOut]
    next_cursor: uuid.UUID | None
    open: int


class SubscriptionDecision(_In):
    """A person's decision about a series; detection never undoes it.

    ``confirm`` (it is one), ``dismiss`` (it is not: it never comes back),
    ``pause`` / ``resume``, ``end`` (it stopped) and ``reopen`` (an ended or
    dismissed one is one after all). ``kind`` also says which of the two it is.
    """

    decision: Literal["confirm", "dismiss", "pause", "resume", "end", "reopen"]
    kind: Literal["subscription", "bill"] | None = None


class EventsSeen(_In):
    """Mark changes as seen; ids the caller cannot see are ignored."""

    ids: Annotated[
        list[uuid.UUID],
        Field(min_length=1, max_length=MAX_SEEN_IDS),
        AfterValidator(_unique_ids),
    ]


class EventsSeenOut(_Output):
    seen: int
