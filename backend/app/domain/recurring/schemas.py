# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request and response schemas of ``/recurring`` and ``/cashflow/calendar`` (API.md).

Money is ``{"amount": "12.34", "currency": "USD"}`` (``app.core.money``);
schedule amounts are in the account's currency (``422 currency-mismatch``
otherwise). Confidence and regularity are decimal strings in ``0..1``.
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
    """Fields to change; absent fields are left alone, ``null`` clears payee/category."""

    name: ScheduleName | None = None
    cadence: Cadence | None = None
    interval_count: IntervalCount | None = None
    next_due: BusinessDate | None = None
    amount_min: MoneyIn | None = None
    amount_max: MoneyIn | None = None
    payee_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    active: bool | None = None


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
