# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subscriptions and bills: the list with its cost, one series, a decision, what changed.

A subscription is a recurring series (``scheduled_transaction``) of kind
``subscription`` or ``bill``; what changed about one is a
``subscription_event``. Everything is limited to the accounts the caller may
see; a decision needs a member who may edit the series' account. A series of
another household, or on a hidden account, is ``404 schedule-not-found``.

What changed
------------

:func:`settle` is called by detection (``recurring.service.persist``) for
every detected series, with the candidate detection found for it in this run
(or none), and writes what follows from it. PerFin has no bank feed, so every
judgement is made against the account's **data clock** (``as_of``, the day of
its latest row), never against today.

* **Price.** Only a series whose amount is fixed is watched. The latest
  charge is compared with ``last_amount``: under :data:`DRIFT_SHARE` nothing
  happens (``last_amount`` stays, so a slow creep adds up); up to
  ``max(PRICE_MIN_ABS, PRICE_MIN_SHARE)`` the amount is updated without a
  word; more is a ``price_changed`` event, with ``previous_amount``,
  ``last_amount`` and ``price_changed_on`` (the first charge at the new
  price) written. The change is *pending* while one charge shows it and the
  cadence is shorter than half a year (:func:`price_change`). A series whose
  cadence changed in the same run (a monthly plan that became annual) takes
  the new amount without an event.
* **Missed, ended.** Judged only when detection no longer finds the series
  and no charge of its merchant is newer than its last one (a merchant that
  still charges without a rhythm is not "missed"). :func:`missed_cycles`
  counts 1 once the due date plus its grace (:func:`grace_days`) passed, and
  one for every whole cycle that went by after the due date. One: a
  ``missed`` event, once per due date. Two: the series is ``ended`` by the
  detector, with one ``ended`` event. This is the one status the detector
  takes from a person: a *confirmed* series that stopped is ended too. A
  series the detector ended that charges again is ``active`` again.
* **A person's word.** A paused or dismissed series is left alone. A series
  a person ended stays ended; a charge after ``ended_on`` is a
  ``charged_after_end`` event. A series entered by hand is never touched.
* **New.** A series created in this run whose first charge is at most
  :data:`NEW_WINDOW_DAYS` before ``as_of`` is a ``new`` event; when its
  merchant is a dictionary subscription and a charge of at most
  :data:`TRIAL_MAX` came :data:`TRIAL_GAP` days before a full one, a
  ``trial_converted`` event as well (:func:`trial`).
* **Renewal.** :func:`renewals` (the daily job, after detection): a counted
  subscription or bill billed every half year or less often, whose next
  charge is at most :data:`RENEWAL_LEAD_DAYS` ahead of the account's data
  clock, is a ``renewal_due`` event dated that charge.

Who is told. An event is stored once (series, kind, day), queues the
notification routed for ``subscription.<kind>`` and emits the live event
``subscription.changed`` (``account_id``, ``schedule_id``). Income is
watched (it ends when it stops) but never announced: the sentences speak of
subscriptions and bills. A series that is only suggested is announced when
it is new and not otherwise. A series the detector of this release never saw
a charge of (``occurrences == 0``, written by an earlier release) ends
without an event. The first run of a household (``quiet``: it has no series
with a merchant key yet) announces only what is new inside the window:
series that stopped long ago are ended without a word.

The list, one series, a decision and the events list are the declaration the
router is built against: the events list is empty for now, and the other
operations answer ``409 not-ready`` once the caller and the series they name
are checked.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Final

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.ids import uuid7
from app.core.money import calc_context, to_storage
from app.domain.notifications.routing import notify
from app.domain.recurring import service as recurring_service
from app.domain.recurring.detection import Candidate
from app.domain.recurring.models import SubscriptionEvent
from app.domain.recurring.schemas import (
    EventsSeen,
    EventsSeenOut,
    SubscriptionDecision,
    SubscriptionDetail,
    SubscriptionEventPage,
    SubscriptionList,
    SubscriptionOut,
)
from app.domain.reports.dates import add_months
from app.domain.transactions.models import (
    ACTIVE_STATUSES,
    Cadence,
    ScheduledTransaction,
    ScheduleOrigin,
    Transaction,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: How late a charge may be before it is missed: ``(longest cycle in days, grace)``...
GRACE_DAYS: Final = ((16, 3), (35, 5), (99, 10))
#: ...and for every longer cycle (half-yearly, annual).
LONG_GRACE_DAYS: Final = 21
#: A cycle of at least this many days is "half-yearly or longer".
LONG_CYCLE_DAYS: Final = 170
#: A price change is at least this much...
PRICE_MIN_ABS: Final = Decimal("1.00")
#: ...and at least this share of the amount before.
PRICE_MIN_SHARE: Final = Decimal("0.05")
#: Under this share a different amount is the same price.
DRIFT_SHARE: Final = Decimal("0.02")
#: A series first charged at most this many days before the data clock is new.
NEW_WINDOW_DAYS: Final = 45
#: A renewal is announced this many days ahead.
RENEWAL_LEAD_DAYS: Final = 7
#: A trial's first charge is at most this much...
TRIAL_MAX: Final = Decimal("1.00")
#: ...and the full charge follows within this many days (inclusive).
TRIAL_GAP: Final = (3, 35)
#: :func:`missed_cycles` stops counting here (two is all anything asks about).
MISSED_CAP: Final = 12

DETECTOR: Final = "detector"
PERSON: Final = "person"
#: Statuses whose series are watched for a price, a missed charge and the end.
WATCHED: Final = ("active", "confirmed", "suggested")
#: The kinds :func:`renewals` announces.
RENEWAL_KINDS: Final = ("subscription", "bill")
LIVE_EVENT: Final = "subscription.changed"

_DAY_STEPS: Final = {Cadence.DAILY: 1, Cadence.WEEKLY: 7}
_MONTH_STEPS: Final = {Cadence.MONTHLY: 1, Cadence.QUARTERLY: 3, Cadence.YEARLY: 12}


# ---------------------------------------------------------------------------
# Pure: cycles, prices, trials
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Charge:
    """One ledger row of a series: its id, its day and its signed amount."""

    id: uuid.UUID
    posted_date: date
    amount: Decimal


@dataclass(frozen=True)
class SeriesFacts:
    """What :func:`price_change` needs to know about a series.

    ``fixed``: its amount does not move from charge to charge (a variable
    bill has no price to change). The last three are what was stored when a
    change was recorded earlier.
    """

    cadence: Cadence
    interval_count: int
    fixed: bool
    last_amount: Decimal | None
    previous_amount: Decimal | None = None
    price_changed_on: date | None = None


@dataclass(frozen=True)
class PriceVerdict:
    """A different price on the latest charge (:func:`price_change`).

    ``changed``: more than the threshold, an event. Otherwise it is
    ``drift``: the amount is updated and nothing is said. ``pending``: only
    one charge shows the new price so far and the cadence is short. ``on``
    and ``transaction_id`` are the first charge at the new price.
    """

    changed: bool
    pending: bool
    previous: Decimal
    current: Decimal
    on: date
    transaction_id: uuid.UUID | None

    @property
    def drift(self) -> bool:
        return not self.changed


def cycle_days(cadence: Cadence, interval: int) -> int:
    """The nominal length of one cycle in days (a month counts 30)."""
    if cadence in _DAY_STEPS:
        return _DAY_STEPS[cadence] * interval
    return 30 * _MONTH_STEPS[cadence] * interval


def is_long(cadence: Cadence, interval: int) -> bool:
    """Billed every half year or less often."""
    return cycle_days(cadence, interval) >= LONG_CYCLE_DAYS


def grace_days(cadence: Cadence, interval: int) -> int:
    """How many days late a charge may be before it is missed (:data:`GRACE_DAYS`)."""
    length = cycle_days(cadence, interval)
    for longest, grace in GRACE_DAYS:
        if length <= longest:
            return grace
    return LONG_GRACE_DAYS


def step(start: date, cadence: Cadence, interval: int, k: int) -> date:
    """``start`` moved by ``k`` cycles (calendar months, or days for weekly cadences)."""
    if cadence in _DAY_STEPS:
        return start + timedelta(days=_DAY_STEPS[cadence] * interval * k)
    return add_months(start, _MONTH_STEPS[cadence] * interval * k)


def missed_cycles(next_due: date, cadence: Cadence, interval: int, as_of: date) -> int:
    """How many cycles went by without the charge due on ``next_due`` (module doc).

    0 until the due date plus its grace has passed on the data clock; then 1,
    or the number of whole cycles that went by since the due date when that
    is more (at most :data:`MISSED_CAP`).
    """
    if as_of <= next_due + timedelta(days=grace_days(cadence, interval)):
        return 0
    whole = 0
    while whole < MISSED_CAP and step(next_due, cadence, interval, whole + 1) <= as_of:
        whole += 1
    return max(whole, 1)


def _ordered(charges: Iterable[Charge]) -> list[Charge]:
    return sorted(charges, key=lambda c: (c.posted_date, c.id.bytes))


def _same_price(a: Decimal, b: Decimal) -> bool:
    """Whether ``a`` is within :data:`DRIFT_SHARE` of ``b`` (sizes; the sign is ignored)."""
    with localcontext(calc_context()):
        return abs(abs(a) - abs(b)) < abs(b) * DRIFT_SHARE


def price_change(series: SeriesFacts, charges: Sequence[Charge]) -> PriceVerdict | None:
    """What the latest of ``charges`` says about the price of ``series`` (module doc).

    ``None``: nothing to say (a variable series, no earlier amount, or the
    same price as before with no change on record). When the latest charge
    is at the price already recorded as a change, that change is returned
    again with its current ``pending`` (a second charge clears it).
    """
    if not series.fixed or series.last_amount is None or not charges:
        return None
    ordered = _ordered(charges)
    latest = ordered[-1]
    long = is_long(series.cadence, series.interval_count)
    if _same_price(latest.amount, series.last_amount):
        if series.price_changed_on is None or series.previous_amount is None:
            return None
        since = [c for c in ordered if c.posted_date >= series.price_changed_on]
        return PriceVerdict(
            changed=True,
            pending=len(since) < 2 and not long,
            previous=series.previous_amount,
            current=series.last_amount,
            on=series.price_changed_on,
            transaction_id=since[0].id if since else None,
        )
    run = [latest]
    for charge in reversed(ordered[:-1]):
        if not _same_price(charge.amount, latest.amount):
            break
        run.append(charge)
    first = run[-1]
    with localcontext(calc_context()):
        base = abs(series.last_amount)
        changed = abs(abs(latest.amount) - base) > max(PRICE_MIN_ABS, base * PRICE_MIN_SHARE)
    return PriceVerdict(
        changed=changed,
        pending=changed and len(run) < 2 and not long,
        previous=series.last_amount,
        current=latest.amount,
        on=first.posted_date,
        transaction_id=first.id,
    )


def _trial_pair(charges: Sequence[Charge]) -> tuple[Charge, Charge] | None:
    ordered = _ordered(charges)
    if not ordered or abs(ordered[0].amount) > TRIAL_MAX:
        return None
    hold = ordered[0]
    full = next((c for c in ordered[1:] if abs(c.amount) > TRIAL_MAX), None)
    if full is None:
        return None
    gap = (full.posted_date - hold.posted_date).days
    return (hold, full) if TRIAL_GAP[0] <= gap <= TRIAL_GAP[1] else None


def trial(charges: Sequence[Charge], known: bool) -> date | None:
    """The day a trial became a paid plan, if ``charges`` show one.

    ``charges`` are every charge of the merchant on the account; ``known``
    says the dictionary names it as a subscription. Best effort: most
    statements never print the small first charge.
    """
    if not known:
        return None
    pair = _trial_pair(charges)
    return pair[1].posted_date if pair is not None else None


# ---------------------------------------------------------------------------
# What changed: events
# ---------------------------------------------------------------------------


def _set_status(series: ScheduledTransaction, status: str) -> None:
    """A status the detector sets; ``active`` follows."""
    series.status = status
    series.status_source = DETECTOR
    series.active = status in ACTIVE_STATUSES


def _record(
    db: Session,
    series: ScheduledTransaction,
    kind: str,
    occurred_on: date,
    *,
    before: Decimal | None = None,
    after: Decimal | None = None,
    transaction_id: uuid.UUID | None = None,
) -> SubscriptionEvent | None:
    """Store one change once, queue its notification and say so live.

    ``None`` when this change (series, kind, day) is already on record:
    nothing is queued or emitted a second time.
    """
    event_id = uuid7()
    inserted = db.execute(
        pg_insert(SubscriptionEvent)
        .values(
            id=event_id,
            household_id=series.household_id,
            schedule_id=series.id,
            account_id=series.account_id,
            kind=kind,
            occurred_on=occurred_on,
            amount_before=before,
            amount_after=after,
            transaction_id=transaction_id,
            state="open",
        )
        .on_conflict_do_nothing(
            index_elements=["household_id", "schedule_id", "kind", "occurred_on"]
        )
        .returning(SubscriptionEvent.id)
    ).scalar_one_or_none()
    if inserted is None:
        return None
    notify(db, series.household_id, f"subscription.{kind}", source_id=event_id)
    _live(db, series)
    return db.get_one(SubscriptionEvent, event_id)


def _live(db: Session, series: ScheduledTransaction) -> None:
    emit(
        db,
        series.household_id,
        LIVE_EVENT,
        {"account_id": str(series.account_id), "schedule_id": str(series.id)},
    )


def _keep(events: list[SubscriptionEvent], event: SubscriptionEvent | None) -> None:
    if event is not None:
        events.append(event)


def _reprice(
    db: Session,
    series: ScheduledTransaction,
    candidate: Candidate,
    charges: Sequence[Charge],
    *,
    loud: bool,
) -> SubscriptionEvent | None:
    """The price part of :func:`settle` for a series detection found again."""
    latest = to_storage(candidate.last_amount)
    moved = (series.cadence, series.interval_count) != (
        candidate.cadence,
        candidate.interval_count,
    )
    if series.last_amount is None or moved or not candidate.fixed:
        series.last_amount = latest
        return None
    own = set(candidate.transaction_ids)
    verdict = price_change(
        SeriesFacts(
            cadence=candidate.cadence,
            interval_count=candidate.interval_count,
            fixed=True,
            last_amount=series.last_amount,
            previous_amount=series.previous_amount,
            price_changed_on=series.price_changed_on,
        ),
        [c for c in charges if c.id in own],
    )
    if verdict is None:
        return None
    if verdict.drift:
        series.last_amount = to_storage(verdict.current)
        return None
    if verdict.on == series.price_changed_on and verdict.current == series.last_amount:
        return None  # already on record
    series.previous_amount = to_storage(verdict.previous)
    series.last_amount = to_storage(verdict.current)
    series.price_changed_on = verdict.on
    if not loud:
        return None
    return _record(
        db,
        series,
        "price_changed",
        verdict.on,
        before=series.previous_amount,
        after=series.last_amount,
        transaction_id=verdict.transaction_id,
    )


def _absent(
    db: Session,
    series: ScheduledTransaction,
    charges: Sequence[Charge],
    as_of: date,
    *,
    loud: bool,
) -> SubscriptionEvent | None:
    """The missed-and-ended part of :func:`settle` for a series detection did not find.

    ``charges`` are the series' own and the merchant's unclaimed ones.
    """
    cycle = (series.cadence, series.interval_count)
    last = series.last_seen_on or step(series.next_due, *cycle, -1)
    if any(charge.posted_date > last for charge in charges):
        return None  # it still charges; detection just sees no rhythm
    missed = missed_cycles(series.next_due, *cycle, as_of)
    if missed == 0:
        return None
    if missed == 1:
        return _record(db, series, "missed", series.next_due) if loud else None
    _set_status(series, "ended")
    series.ended_on = last
    return _record(db, series, "ended", series.next_due) if loud else None


def _first_seen(
    db: Session,
    series: ScheduledTransaction,
    candidate: Candidate,
    charges: Sequence[Charge],
    as_of: date,
) -> list[SubscriptionEvent]:
    """The ``new`` and ``trial_converted`` part of :func:`settle` for a series created now."""
    first = series.first_seen_on
    if first is None or not 0 <= (as_of - first).days <= NEW_WINDOW_DAYS:
        return []
    events: list[SubscriptionEvent] = []
    _keep(events, _record(db, series, "new", first, after=series.last_amount))
    pair = _trial_pair(charges) if candidate.known and series.kind == "subscription" else None
    if pair is not None:
        hold, full = pair
        _keep(
            events,
            _record(
                db,
                series,
                "trial_converted",
                full.posted_date,
                before=to_storage(hold.amount),
                after=to_storage(full.amount),
                transaction_id=full.id,
            ),
        )
    return events


def settle(
    db: Session,
    household_id: uuid.UUID,
    series: ScheduledTransaction,
    candidate: Candidate | None,
    charges: Sequence[Charge],
    as_of: date,
    *,
    quiet: bool,
    created: bool = False,
    loose: Sequence[Charge] = (),
) -> list[SubscriptionEvent]:
    """Write what one detection run says changed about ``series`` (module doc).

    ``candidate`` is what detection found for the series in this run, before
    its facts are refreshed from it (``None``: not found). ``charges`` are
    the rows of the series (linked to it, or in the candidate); ``loose``
    are rows of the same merchant, account and direction that belong to no
    series. ``as_of`` is the account's data clock. ``created``: the series
    was written in this run. ``quiet``: the household's first run (only
    what is new is announced). Returns the events stored now.
    """
    if series.household_id != household_id:
        raise ValueError("the series belongs to another household")
    if series.origin is ScheduleOrigin.MANUAL or series.status == "dismissed":
        return []
    own = _ordered(charges)
    announced = series.kind != "income"
    loud = announced and not quiet
    by_person = series.status_source == PERSON
    if series.status == "paused" or (series.status == "ended" and by_person):
        # A person's word stands. The latest amount is a fact and is kept up.
        if candidate is not None:
            series.last_amount = to_storage(candidate.last_amount)
        events: list[SubscriptionEvent] = []
        if series.status == "ended" and series.ended_on is not None and loud:
            for charge in own:
                if charge.posted_date > series.ended_on:
                    _keep(
                        events,
                        _record(
                            db,
                            series,
                            "charged_after_end",
                            charge.posted_date,
                            after=to_storage(charge.amount),
                            transaction_id=charge.id,
                        ),
                    )
        return events
    before = series.status
    if series.status == "ended":
        ended_on = series.ended_on
        if candidate is None or (ended_on is not None and candidate.last_seen <= ended_on):
            return []
        _set_status(series, "active")  # what the detector ended, it reopens
        series.ended_on = None
    events = []
    counted = before in ACTIVE_STATUSES
    if created and candidate is not None:
        if announced:
            events.extend(_first_seen(db, series, candidate, [*loose, *own], as_of))
    elif candidate is not None:
        _keep(events, _reprice(db, series, candidate, own, loud=loud and counted))
    elif series.status in WATCHED:
        seen = series.occurrences > 0  # the detector of this release knows the series
        _keep(
            events,
            _absent(db, series, [*own, *loose], as_of, loud=loud and counted and seen),
        )
    if series.status != before and not events:
        _live(db, series)
    return events


def renewals(db: Session, household_id: uuid.UUID, on: date) -> int:
    """Announce the long-cycle charges due within a week; how many events were stored.

    A counted (active or confirmed) subscription or bill billed every half
    year or less often, with ``next_due - RENEWAL_LEAD_DAYS <= as_of <
    next_due``, where ``as_of`` is its account's data clock on ``on`` (the
    day of the account's latest row, never later than ``on``). One event per
    series and due date, however often this runs.
    """
    latest = db.execute(
        select(Transaction.account_id, func.max(Transaction.posted_date))
        .where(Transaction.household_id == household_id, Transaction.posted_date <= on)
        .group_by(Transaction.account_id)
    )
    clock = {account_id: day for account_id, day in latest}  # noqa: C416 - Row tuples
    rows = db.execute(
        select(ScheduledTransaction)
        .where(
            ScheduledTransaction.household_id == household_id,
            ScheduledTransaction.status.in_(ACTIVE_STATUSES),
            ScheduledTransaction.kind.in_(RENEWAL_KINDS),
        )
        .order_by(ScheduledTransaction.id)
    ).scalars()
    stored = 0
    for series in rows:
        as_of = clock.get(series.account_id)
        if as_of is None or not is_long(series.cadence, series.interval_count):
            continue
        if series.next_due - timedelta(days=RENEWAL_LEAD_DAYS) <= as_of < series.next_due:
            event = _record(db, series, "renewal_due", series.next_due, after=series.last_amount)
            stored += event is not None
    return stored


# ---------------------------------------------------------------------------
# The list, one series, a decision, the events (declared; Task 21 fills them)
# ---------------------------------------------------------------------------


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
