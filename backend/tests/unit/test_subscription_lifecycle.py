# SPDX-License-Identifier: AGPL-3.0-or-later
"""A recurring series over time: what detection may write on later runs, and what it may not.

Every test builds a small invented ledger on one account, with merchant keys
set by hand (as the categorisation engine would have written them), and runs
``recurring.service.persist`` for a given day, as the daily job does. The
day is a parameter, so a test can let months pass.

Pinned here: a person's kind, pause and "ended" survive detection; the
detector's own status only rises; a series from before merchant keys, a
manual one and a renamed one are matched instead of doubled; charges are
linked without the rows looking touched; a change of cadence is the same
series; and the clock is the account's latest row.

What changed about a series (``recurring.subscriptions.settle``) is pinned
here too: a price change (pending on its first charge, drift without a word,
never for a variable amount), a missed charge and the end after two whole
cycles on the data clock, the one status the detector may take from a person
(confirmed, and its charges stopped), what a person paused, dismissed or
ended staying as it is, a charge after a person's "ended", the quiet first
run, a new series reported once, a trial that became a paid plan, and a
renewal announced once, a week ahead. Each change is one
``subscription_event`` however often detection runs.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, update

from app.db.models import Job
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.notifications.routing import SEND_JOB_TYPE
from app.domain.recurring import detection, service, subscriptions
from app.domain.recurring.detection import Candidate, Pattern
from app.domain.recurring.models import SubscriptionEvent
from app.domain.recurring.subscriptions import Charge, SeriesFacts
from app.domain.reports.dates import add_months
from app.domain.transactions.models import (
    Cadence,
    ScheduledTransaction,
    ScheduleOrigin,
    Transaction,
)
from app.workers.handlers import recurring as recurring_jobs
from app.workers.registry import REGISTRY, JobContext
from tests.fixtures.factories import Factories
from tests.fixtures.ledger import LedgerFactories, make_payee, make_scheduled_transaction
from tests.fixtures.notifications import make_channel

pytestmark = pytest.mark.integration

D = Decimal


@dataclass(frozen=True)
class Book:
    """One household with one checking account."""

    factories: Factories
    ledger: LedgerFactories
    household_id: uuid.UUID
    account: Account

    def charge(self, on: date, amount: str, text: str, key: str | None, **fields: Any) -> uuid.UUID:
        row = self.ledger.transaction(
            self.account, amount=amount, posted_date=on, raw_payee=text, merchant_key=key, **fields
        )
        return row.id

    def monthly(
        self, first: date, amounts: list[str], text: str, key: str | None, **fields: Any
    ) -> list[uuid.UUID]:
        return [
            self.charge(add_months(first, i), amount, text, key, **fields)
            for i, amount in enumerate(amounts)
        ]

    def detect(self, on: date) -> service.DetectCounts:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            counts, _ = service.persist(db, self.household_id, [self.account.id], on)
            return counts

    def found(self, on: date) -> list[Candidate]:
        """What detection sees on ``on``, nothing written."""
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            rows, as_of = service.observed(db, self.household_id, [self.account.id], on)
            return detection.detect(rows, as_of)

    def series(self) -> list[ScheduledTransaction]:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            return list(
                db.execute(
                    select(ScheduledTransaction)
                    .where(ScheduledTransaction.account_id == self.account.id)
                    .order_by(ScheduledTransaction.id)
                ).scalars()
            )

    def one(self) -> ScheduledTransaction:
        (row,) = self.series()
        return row

    def decide(self, schedule_id: uuid.UUID, **columns: Any) -> None:
        """Write a series' columns as a person's decision would (Task 21 does it over HTTP)."""
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            row = db.get_one(ScheduledTransaction, schedule_id)
            for name, value in columns.items():
                setattr(row, name, value)

    def charges(self) -> dict[uuid.UUID, Transaction]:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            rows = db.execute(
                select(Transaction).where(Transaction.account_id == self.account.id)
            ).scalars()
            return {row.id: row for row in rows}

    def tick(self, on: date) -> None:
        """An unrelated row on ``on``: it moves the account's data clock, nothing else."""
        self.charge(on, "-3.00", f"MARKER {on:%Y%m%d}", f"marker {on:%Y%m%d}")

    def events(self) -> list[SubscriptionEvent]:
        """Every change recorded for the household, oldest first."""
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            return list(
                db.execute(
                    select(SubscriptionEvent)
                    .where(SubscriptionEvent.household_id == self.household_id)
                    .order_by(SubscriptionEvent.id)
                ).scalars()
            )

    def kinds(self) -> list[str]:
        return [event.kind for event in self.events()]

    def sends(self) -> list[str]:
        """The routed event of every queued notification, oldest first."""
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            jobs = db.execute(
                select(Job).where(Job.type == SEND_JOB_TYPE).order_by(Job.created_at, Job.id)
            ).scalars()
            return [job.payload["event_type"] for job in jobs]

    def kinds_of(self, schedule_id: uuid.UUID) -> list[str]:
        return [event.kind for event in self.events() if event.schedule_id == schedule_id]

    def get(self, schedule_id: uuid.UUID) -> ScheduledTransaction:
        return next(row for row in self.series() if row.id == schedule_id)

    def by_key(self, key: str) -> ScheduledTransaction:
        return next(row for row in self.series() if row.merchant_key == key)

    def as_imported(self) -> None:
        """Equalise the timestamps, as the importer does last: every row reads as untouched."""
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            db.execute(
                update(Transaction)
                .where(Transaction.account_id == self.account.id)
                .values(updated_at=Transaction.created_at)
            )


@pytest.fixture
def book(factories: Factories, ledger: LedgerFactories) -> Book:
    household = factories.household()
    account = ledger.account(household.id, type=AccountType.CHECKING, name="Everyday")
    return Book(factories, ledger, household.id, account)


def test_a_person_s_kind_and_pause_survive_detection(book: Book) -> None:
    book.monthly(date(2026, 1, 7), ["-9.99"] * 4, "TUNEBOX 7781", "tunebox")
    book.monthly(date(2026, 1, 9), ["-30.00"] * 4, "LANTERN PRESS", "lantern press")
    assert book.detect(date(2026, 4, 12)).created == 2
    tunebox = next(r for r in book.series() if r.merchant_key == "tunebox")
    lantern = next(r for r in book.series() if r.merchant_key == "lantern press")
    assert (tunebox.kind, tunebox.kind_source) == ("other", "detector")
    assert (tunebox.status, tunebox.status_source) == ("active", "detector")
    book.decide(tunebox.id, kind="bill", kind_source="person")
    book.decide(tunebox.id, status="paused", status_source="person", active=False)
    book.decide(
        lantern.id,
        status="ended",
        status_source="person",
        active=False,
        ended_on=date(2026, 4, 12),
    )
    book.charge(date(2026, 5, 7), "-9.99", "TUNEBOX 7781", "tunebox")
    book.charge(date(2026, 5, 9), "-30.00", "LANTERN PRESS", "lantern press")
    counts = book.detect(date(2026, 5, 12))
    assert (counts.created, counts.updated) == (0, 2)
    after = next(r for r in book.series() if r.id == tunebox.id)
    assert (after.kind, after.kind_source) == ("bill", "person")
    assert (after.status, after.status_source, after.active) == ("paused", "person", False)
    # The facts are still kept up to date.
    assert (after.occurrences, after.last_seen_on) == (5, date(2026, 5, 7))
    assert after.next_due == date(2026, 6, 7)
    ended = next(r for r in book.series() if r.id == lantern.id)
    assert (ended.status, ended.status_source, ended.active) == ("ended", "person", False)
    assert ended.occurrences == 5
    # A series a person confirmed is not set back to what the detector thinks.
    book.decide(tunebox.id, status="confirmed", status_source="person", active=True)
    book.detect(date(2026, 5, 12))
    assert next(r for r in book.series() if r.id == tunebox.id).status == "confirmed"


def test_suggested_rises_to_active_with_more_charges_but_not_back(book: Book) -> None:
    book.monthly(date(2026, 1, 10), ["-5.00", "-5.50", "-5.20"], "BIKE LOCKER", "bike locker")
    book.detect(date(2026, 3, 15))
    first = book.one()
    assert (first.status, first.status_source, first.active) == ("suggested", "detector", False)
    book.charge(date(2026, 4, 10), "-5.30", "BIKE LOCKER", "bike locker")
    book.detect(date(2026, 4, 15))
    risen = book.one()
    assert risen.id == first.id
    assert (risen.status, risen.status_source, risen.active) == ("active", "detector", True)
    # Two more charges, one of them late: the detector is less sure again.
    book.charge(date(2026, 5, 25), "-5.10", "BIKE LOCKER", "bike locker")
    book.charge(date(2026, 6, 24), "-5.40", "BIKE LOCKER", "bike locker")
    (candidate,) = book.found(date(2026, 6, 25))
    assert candidate.status == "suggested"
    book.detect(date(2026, 6, 25))
    kept = book.one()
    assert (kept.status, kept.status_source, kept.active) == ("active", "detector", True)
    assert (kept.occurrences, kept.confidence) == (6, candidate.confidence)


def test_a_series_from_before_the_upgrade_is_adopted_not_doubled(book: Book) -> None:
    # Detected before merchant keys existed: named after the statement text...
    by_name = book.ledger.run(
        make_scheduled_transaction,
        book.account,
        name="TUNEBOX 7781",
        origin=ScheduleOrigin.DETECTED,
        next_due=date(2026, 5, 7),
        amount_min="-9.99",
        amount_max="-9.99",
    )
    # ...or after its payee, and renamed since.
    payee = book.ledger.run(make_payee, book.household_id, name="Hill Water")
    by_payee = book.ledger.run(
        make_scheduled_transaction,
        book.account,
        name="Water",
        origin=ScheduleOrigin.DETECTED,
        next_due=date(2026, 5, 15),
        amount_min="-41.00",
        amount_max="-39.00",
        payee=payee,
    )
    assert (by_name.merchant_key, by_name.status, by_name.status_source) == (
        None,
        "active",
        "detector",
    )
    book.monthly(date(2026, 1, 7), ["-9.99"] * 4, "TUNEBOX 7790", "tunebox")
    book.monthly(
        date(2026, 1, 15),
        ["-40.00", "-39.50", "-40.50", "-40.00"],
        "HILL WATER CO",
        "hill water",
        payee=payee,
    )
    counts = book.detect(date(2026, 4, 20))
    assert (counts.created, counts.updated) == (0, 2)
    rows = {r.id: r for r in book.series()}
    assert set(rows) == {by_name.id, by_payee.id}
    assert rows[by_name.id].merchant_key == "tunebox"
    assert rows[by_name.id].name == "TUNEBOX 7781"
    assert (rows[by_payee.id].merchant_key, rows[by_payee.id].name) == ("hill water", "Water")
    assert (rows[by_name.id].occurrences, rows[by_name.id].first_seen_on) == (4, date(2026, 1, 7))
    again = book.detect(date(2026, 4, 20))
    assert (again.created, again.updated, again.unchanged) == (0, 0, 2)


def test_a_manual_series_is_matched_and_never_rewritten(book: Book) -> None:
    manual = book.ledger.run(
        make_scheduled_transaction,
        book.account,
        name="Ironworks Gym",
        next_due=date(2026, 9, 1),
        amount_min="-36.00",
        amount_max="-34.00",
    )
    before = book.one()
    assert (before.origin, before.status, before.status_source) == (
        ScheduleOrigin.MANUAL,
        "confirmed",
        "person",
    )
    ids = book.monthly(date(2026, 1, 3), ["-35.00"] * 5, "IRONWORKS GYM", "ironworks gym")
    for _ in range(2):
        counts = book.detect(date(2026, 5, 6))
        assert (counts.created, counts.updated, counts.skipped_manual) == (0, 0, 1)
    after = book.one()
    assert after.id == manual.id
    for column in ScheduledTransaction.__table__.columns:
        assert getattr(after, column.name) == getattr(before, column.name), column.name
    # Its charges are linked to it all the same.
    charges = book.charges()
    assert {charges[i].scheduled_transaction_id for i in ids} == {manual.id}


def test_charges_are_linked_without_touching_the_rows(book: Book) -> None:
    ids = book.monthly(date(2025, 7, 7), ["-9.99"] * 12, "TUNEBOX 7781", "tunebox")
    other = book.charge(date(2026, 6, 1), "-3.25", "KERB KIOSK", "kerb kiosk")
    book.as_imported()
    book.detect(date(2026, 6, 10))
    series = book.one()
    charges = book.charges()
    assert {charges[i].scheduled_transaction_id for i in ids} == {series.id}
    assert charges[other].scheduled_transaction_id is None
    assert all(row.updated_at == row.created_at for row in charges.values())
    # The next charge joins on the next run, again without looking touched.
    late = book.charge(date(2026, 7, 7), "-9.99", "TUNEBOX 7781", "tunebox")
    book.as_imported()
    book.detect(date(2026, 7, 10))
    charges = book.charges()
    assert charges[late].scheduled_transaction_id == series.id
    assert all(row.updated_at == row.created_at for row in charges.values())
    # A charge that no longer fits the series (a person corrected its amount)
    # is not unlinked.
    with book.factories.transaction() as db:
        set_context(db, book.household_id)
        db.get_one(Transaction, ids[5]).amount = D("-30.00")
    (candidate,) = book.found(date(2026, 7, 10))
    assert ids[5] not in candidate.transaction_ids
    book.detect(date(2026, 7, 10))
    charges = book.charges()
    assert charges[ids[5]].scheduled_transaction_id == series.id
    assert book.one().occurrences == 12


def test_a_plan_that_became_annual_is_the_same_series(book: Book) -> None:
    book.monthly(date(2023, 10, 10), ["-9.99"] * 3, "LANTERN PRESS", "lantern press")
    book.detect(date(2023, 12, 20))
    monthly = book.one()
    assert (monthly.cadence, monthly.interval_count, monthly.amount_min) == (
        Cadence.MONTHLY,
        1,
        D("-9.99"),
    )
    for year in (2024, 2025, 2026):
        book.charge(date(year, 1, 10), "-99.00", "LANTERN PRESS", "lantern press")
    (candidate,) = book.found(date(2026, 1, 15))
    assert candidate.pattern is Pattern.ANNUAL
    counts = book.detect(date(2026, 1, 15))
    assert (counts.created, counts.updated) == (0, 1)
    annual = book.one()
    assert annual.id == monthly.id
    assert (annual.cadence, annual.interval_count) == (Cadence.YEARLY, 1)
    assert (annual.amount_min, annual.amount_max, annual.last_amount) == (
        D("-99.00"),
        D("-99.00"),
        D("-99.00"),
    )
    assert annual.next_due == date(2027, 1, 10)
    # First seen does not move forward when the cadence changes.
    assert annual.first_seen_on == date(2023, 10, 10)
    # A plan that moved to another cadence is not a price change.
    assert (annual.previous_amount, annual.price_changed_on) == (None, None)
    assert book.kinds() == []


def test_a_renamed_series_without_a_key_is_found_by_its_charges(book: Book) -> None:
    # "Payroll" names nobody, so the series has no merchant key to be found by.
    first = date(2026, 1, 2)
    for i in range(4):
        book.charge(add_months(first, i), "2500.00", "Payroll", None)
    book.detect(date(2026, 4, 5))
    salary = book.one()
    assert (salary.merchant_key, salary.kind, salary.name) == (None, "income", "Payroll")
    with book.factories.transaction() as db:
        set_context(db, book.household_id)
        db.get_one(ScheduledTransaction, salary.id).name = "My salary"
    # A raise: the new amount does not overlap the old range either.
    for i in range(4, 8):
        book.charge(add_months(first, i), "2700.00", "Payroll", None)
    counts = book.detect(date(2026, 8, 5))
    assert (counts.created, counts.updated) == (0, 1)
    after = book.one()
    assert (after.id, after.name, after.occurrences) == (salary.id, "My salary", 8)
    assert after.last_amount == D("2700.00")


def test_a_series_is_judged_on_the_account_s_latest_row(book: Book) -> None:
    book.monthly(date(2025, 1, 12), ["-9.99"] * 12, "TUNEBOX 7781", "tunebox")
    # Nobody uploaded a statement for five months: the clock stopped with the data.
    today = date(2026, 5, 20)
    with book.factories.transaction() as db:
        set_context(db, book.household_id)
        _, as_of = service.observed(db, book.household_id, [book.account.id], today)
    assert as_of == {book.account.id: date(2025, 12, 12)}
    book.detect(today)
    series = book.one()
    assert (series.status, series.active, series.next_due) == ("active", True, date(2026, 1, 12))
    # A row dated after "today" does not move the clock past today.
    book.charge(date(2026, 6, 30), "-1.00", "KERB KIOSK", "kerb kiosk")
    with book.factories.transaction() as db:
        set_context(db, book.household_id)
        _, as_of = service.observed(db, book.household_id, [book.account.id], today)
    assert as_of == {book.account.id: today}


# ---------------------------------------------------------------------------
# What changed (Task 20)
# ---------------------------------------------------------------------------

JAN = date(2026, 1, 7)
APR = date(2026, 4, 7)


def _facts(
    last: str | None,
    *,
    fixed: bool = True,
    cadence: Cadence = Cadence.MONTHLY,
    previous: str | None = None,
    changed_on: date | None = None,
) -> SeriesFacts:
    return SeriesFacts(
        cadence=cadence,
        interval_count=1,
        fixed=fixed,
        last_amount=D(last) if last is not None else None,
        previous_amount=D(previous) if previous is not None else None,
        price_changed_on=changed_on,
    )


def _dated(amounts: list[str], *, months: int = 1) -> list[Charge]:
    return [
        Charge(uuid.uuid4(), add_months(JAN, i * months), D(amount))
        for i, amount in enumerate(amounts)
    ]


@pytest.mark.parametrize(
    ("facts", "charges", "expected"),
    [
        pytest.param(
            _facts("-10.99"),
            _dated(["-10.99"] * 3 + ["-12.99"]),
            (True, True, D("-10.99"), D("-12.99"), APR),
            id="raised: changed and pending on the first charge",
        ),
        pytest.param(
            _facts("-10.99"),
            _dated(["-10.99"] * 3 + ["-12.99"] * 2),
            (True, False, D("-10.99"), D("-12.99"), APR),
            id="two charges at the new price arrive together: not pending",
        ),
        pytest.param(
            _facts("-12.99", previous="-10.99", changed_on=APR),
            _dated(["-10.99"] * 3 + ["-12.99"]),
            (True, True, D("-10.99"), D("-12.99"), APR),
            id="recorded on one charge: still pending",
        ),
        pytest.param(
            _facts("-12.99", previous="-10.99", changed_on=APR),
            _dated(["-10.99"] * 3 + ["-12.99"] * 2),
            (True, False, D("-10.99"), D("-12.99"), APR),
            id="a second charge at the new price clears pending",
        ),
        pytest.param(
            _facts("-10.99"),
            _dated(["-10.99"] * 3 + ["-11.30"]),
            (False, False, D("-10.99"), D("-11.30"), APR),
            id="between 2% and 5% is drift",
        ),
        pytest.param(
            _facts("-3.00"),
            _dated(["-3.00"] * 3 + ["-3.60"]),
            (False, False, D("-3.00"), D("-3.60"), APR),
            id="a fifth more but under 1.00 is drift",
        ),
        pytest.param(
            _facts("-10.99"), _dated(["-10.99"] * 3 + ["-11.05"]), None, id="under 2% is nothing"
        ),
        pytest.param(
            _facts("-99.00", cadence=Cadence.YEARLY),
            _dated(["-99.00", "-119.00"], months=12),
            (True, False, D("-99.00"), D("-119.00"), date(2027, 1, 7)),
            id="an annual price changes at once",
        ),
        pytest.param(
            _facts("-40.00", fixed=False),
            _dated(["-40.00", "-41.00", "-39.00", "-55.00"]),
            None,
            id="a variable series never",
        ),
        pytest.param(
            _facts(None), _dated(["-10.99", "-12.99"]), None, id="no earlier amount to compare"
        ),
        pytest.param(
            # Raised in July and back in August, read from the charges at the old price
            # alone (the July one sits in another amount cluster).
            _facts("-12.99", previous="-10.99", changed_on=date(2026, 7, 7)),
            [c for i, c in enumerate(_dated(["-10.99"] * 8)) if i != 6],
            (True, True, D("-12.99"), D("-10.99"), date(2026, 8, 7)),
            id="back to an earlier price: dated where it changed back",
        ),
    ],
)
def test_price_change(
    facts: SeriesFacts,
    charges: list[Charge],
    expected: tuple[bool, bool, Decimal, Decimal, date] | None,
) -> None:
    verdict = subscriptions.price_change(facts, charges)
    if expected is None:
        assert verdict is None
        return
    assert verdict is not None
    assert (
        verdict.changed,
        verdict.pending,
        verdict.previous,
        verdict.current,
        verdict.on,
    ) == expected
    assert verdict.drift is not verdict.changed


def test_a_price_rise_is_recorded_once_and_drift_is_silent(book: Book) -> None:
    book.monthly(JAN, ["-10.99"] * 4, "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 4, 10))
    assert book.one().last_amount == D("-10.99")
    # Under 2%: the amount on record stays.
    book.charge(date(2026, 5, 7), "-11.05", "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 5, 10))
    assert book.one().last_amount == D("-10.99")
    # Between 2% and 5%: updated, nothing said.
    book.charge(date(2026, 6, 7), "-11.30", "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 6, 10))
    drifted = book.one()
    assert (drifted.last_amount, drifted.previous_amount, drifted.price_changed_on) == (
        D("-11.30"),
        None,
        None,
    )
    assert book.kinds() == []
    # A real rise: recorded once, however often detection runs.
    raised = book.charge(date(2026, 7, 7), "-13.30", "TUNEBOX 7781", "tunebox")
    for _ in range(2):
        book.detect(date(2026, 7, 10))
    series = book.one()
    assert (series.last_amount, series.previous_amount, series.price_changed_on) == (
        D("-13.30"),
        D("-11.30"),
        date(2026, 7, 7),
    )
    assert (series.status, series.occurrences) == ("active", 7)
    (event,) = book.events()
    assert (event.kind, event.occurred_on, event.transaction_id) == (
        "price_changed",
        date(2026, 7, 7),
        raised,
    )
    assert (event.amount_before, event.amount_after) == (D("-11.30"), D("-13.30"))
    # The next charge at the new price changes nothing more.
    book.charge(date(2026, 8, 7), "-13.30", "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 8, 10))
    assert book.kinds() == ["price_changed"]
    assert book.one().previous_amount == D("-11.30")


def test_missed_then_ended_on_the_data_clock(book: Book) -> None:
    due = date(2026, 7, 7)
    monthly = (due, Cadence.MONTHLY, 1)
    assert subscriptions.missed_cycles(*monthly, due + timedelta(days=5)) == 0  # the grace
    assert subscriptions.missed_cycles(*monthly, due + timedelta(days=6)) == 1
    assert subscriptions.missed_cycles(*monthly, due + timedelta(days=40)) == 1
    assert subscriptions.missed_cycles(*monthly, due + timedelta(days=70)) == 2
    assert subscriptions.missed_cycles(due, Cadence.WEEKLY, 1, due + timedelta(days=3)) == 0
    assert subscriptions.missed_cycles(due, Cadence.WEEKLY, 1, due + timedelta(days=4)) == 1
    assert subscriptions.missed_cycles(due, Cadence.YEARLY, 1, due + timedelta(days=21)) == 0
    assert subscriptions.missed_cycles(due, Cadence.YEARLY, 1, due + timedelta(days=22)) == 1

    book.monthly(JAN, ["-9.99"] * 6, "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 6, 10))
    assert book.one().next_due == due
    # Nobody uploads for most of a year: the clock stopped with the data.
    long_after = date(2027, 3, 1)
    book.detect(long_after)
    book.tick(due - timedelta(days=2))
    book.detect(long_after)
    assert book.kinds() == []
    assert (book.by_key("tunebox").status, book.by_key("tunebox").active) == ("active", True)
    # A statement that reaches 40 days past the due date, without the charge.
    book.tick(due + timedelta(days=40))
    for _ in range(2):
        book.detect(long_after)
    (missed,) = book.events()
    assert (missed.kind, missed.occurred_on, missed.state) == ("missed", due, "open")
    assert (missed.schedule_id, missed.account_id) == (book.by_key("tunebox").id, book.account.id)
    assert book.by_key("tunebox").status == "active"
    # 70 days: two whole cycles went by.
    book.tick(due + timedelta(days=70))
    for _ in range(2):
        book.detect(long_after)
    assert book.kinds() == ["missed", "ended"]
    ended = book.by_key("tunebox")
    assert (ended.status, ended.status_source, ended.active) == ("ended", "detector", False)
    assert ended.ended_on == date(2026, 6, 7)


def test_charges_that_continue_without_a_rhythm_are_not_missed(book: Book) -> None:
    book.monthly(JAN, ["-9.99"] * 6, "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 6, 10))
    # Two late charges: detection no longer sees a rhythm, but the merchant still charges.
    book.charge(date(2026, 7, 20), "-9.99", "TUNEBOX 7781", "tunebox")
    book.charge(date(2026, 8, 29), "-9.99", "TUNEBOX 7781", "tunebox")
    on = date(2026, 8, 30)
    assert book.found(on) == []
    book.detect(on)
    assert book.kinds() == []
    assert (book.one().status, book.one().status_source) == ("active", "detector")


def test_a_confirmed_series_that_stopped_is_ended_with_an_alert(book: Book) -> None:
    with book.factories.transaction() as db:
        make_channel(db, book.household_id, book.factories.keyring)
    book.monthly(JAN, ["-9.99"] * 6, "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 6, 10))
    book.decide(book.one().id, status="confirmed", status_source="person", active=True)
    book.tick(date(2026, 9, 15))
    for _ in range(2):
        book.detect(date(2026, 9, 20))
    series = book.by_key("tunebox")
    assert (series.status, series.status_source, series.active) == ("ended", "detector", False)
    # One alert: the end, not the missed charge and the end.
    assert book.kinds() == ["ended"]
    assert book.sends() == ["subscription.ended"]
    # It charges again: what the detector ended, the detector reopens.
    book.charge(date(2026, 10, 7), "-9.99", "TUNEBOX 7781", "tunebox")
    book.charge(date(2026, 11, 7), "-9.99", "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 11, 10))
    again = book.by_key("tunebox")
    assert again.id == series.id
    assert (again.status, again.status_source, again.active) == ("active", "detector", True)
    assert again.ended_on is None
    assert book.kinds() == ["ended"]


def test_a_paused_a_dismissed_and_a_person_ended_series_are_left_alone(book: Book) -> None:
    book.monthly(JAN, ["-9.99"] * 6, "TUNEBOX 7781", "tunebox")
    book.monthly(date(2026, 1, 9), ["-30.00"] * 6, "LANTERN PRESS", "lantern press")
    book.monthly(date(2026, 1, 11), ["-5.00"] * 6, "BIKE LOCKER", "bike locker")
    assert book.detect(date(2026, 6, 12)).created == 3
    decided = {
        "tunebox": ("paused", None),
        "lantern press": ("dismissed", None),
        "bike locker": ("ended", date(2026, 6, 12)),
    }
    for key, (status, ended_on) in decided.items():
        book.decide(
            book.by_key(key).id,
            status=status,
            status_source="person",
            active=False,
            ended_on=ended_on,
        )
    book.tick(date(2026, 9, 20))
    for _ in range(2):
        book.detect(date(2026, 9, 25))
    for key, (status, ended_on) in decided.items():
        row = book.by_key(key)
        assert (row.status, row.status_source, row.active) == (status, "person", False), key
        assert row.ended_on == ended_on
    assert book.kinds() == []
    assert book.sends() == []


def test_a_charge_after_a_person_ended_it_is_reported(book: Book) -> None:
    book.monthly(JAN, ["-9.99"] * 6, "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 6, 10))
    book.decide(
        book.one().id,
        status="ended",
        status_source="person",
        active=False,
        ended_on=date(2026, 6, 12),
    )
    book.detect(date(2026, 6, 20))
    assert book.kinds() == []  # the charges before the end say nothing
    late = book.charge(date(2026, 7, 7), "-9.99", "TUNEBOX 7781", "tunebox")
    for _ in range(2):
        book.detect(date(2026, 7, 10))
    (event,) = book.events()
    assert (event.kind, event.occurred_on, event.transaction_id) == (
        "charged_after_end",
        date(2026, 7, 7),
        late,
    )
    assert event.amount_after == D("-9.99")
    series = book.one()
    assert (series.status, series.status_source, series.active) == ("ended", "person", False)
    assert series.ended_on == date(2026, 6, 12)


def test_the_first_run_is_quiet_about_old_series(book: Book) -> None:
    with book.factories.transaction() as db:
        make_channel(db, book.household_id, book.factories.keyring)
    # Two series the detector of an earlier release wrote: one stopped long
    # ago, one that will stop later. Neither has a merchant key.
    stopped = book.ledger.run(
        make_scheduled_transaction,
        book.account,
        name="Granite Gym",
        origin=ScheduleOrigin.DETECTED,
        next_due=date(2025, 3, 1),
        amount_min="-20.00",
        amount_max="-20.00",
    )
    fading = book.ledger.run(
        make_scheduled_transaction,
        book.account,
        name="Harbor Club",
        origin=ScheduleOrigin.DETECTED,
        next_due=date(2026, 7, 1),
        amount_min="-45.00",
        amount_max="-45.00",
    )
    # An old series and one that began inside the window.
    book.monthly(JAN, ["-9.99"] * 6, "TUNEBOX 7781", "tunebox")
    book.monthly(date(2026, 5, 9), ["-15.49"] * 2, "NETFLIX.COM", "netflix")
    for _ in range(2):
        book.detect(date(2026, 6, 12))
    rows = {row.id: row for row in book.series()}
    assert len(rows) == 4
    (new,) = book.events()
    assert (new.kind, new.schedule_id) == ("new", book.by_key("netflix").id)
    assert book.sends() == ["subscription.new"]
    # The one that stopped long ago is ended without a word.
    assert (rows[stopped.id].status, rows[stopped.id].status_source) == ("ended", "detector")
    assert rows[fading.id].status == "active"
    # Later the other one runs out too. The detector of this release never saw
    # a charge of it, so that is said nowhere either; a series it knows is.
    book.tick(date(2026, 9, 15))
    book.detect(date(2026, 9, 20))
    rows = {row.id: row for row in book.series()}
    assert rows[fading.id].status == "ended"
    assert rows[book.by_key("tunebox").id].status == "ended"
    by_series = {(event.schedule_id, event.kind) for event in book.events()}
    assert by_series == {
        (book.by_key("netflix").id, "new"),
        (book.by_key("tunebox").id, "ended"),
    }


def test_a_new_series_in_the_window_is_reported_once(book: Book) -> None:
    book.monthly(date(2026, 5, 9), ["-15.49"] * 2, "NETFLIX.COM", "netflix")
    # Income that began in the window is no subscription: nothing is said.
    for day in (1, 15, 29):
        book.charge(date(2026, 5, day), "2500.00", "Payroll", None)
    for _ in range(2):
        book.detect(date(2026, 6, 12))
    assert {row.kind for row in book.series()} == {"subscription", "income"}
    (event,) = book.events()
    netflix = book.by_key("netflix")
    assert (event.kind, event.schedule_id, event.account_id) == (
        "new",
        netflix.id,
        book.account.id,
    )
    assert (event.occurred_on, event.amount_after, event.state) == (
        date(2026, 5, 9),
        D("-15.49"),
        "open",
    )
    assert event.occurred_on == netflix.first_seen_on


def test_trial_then_full_price(book: Book) -> None:
    first = date(2026, 5, 2)
    full = [Charge(uuid.uuid4(), date(2026, 5, 9), D("-15.49"))]
    hold = Charge(uuid.uuid4(), first, D("-1.00"))
    assert subscriptions.trial([hold, *full], known=True) == date(2026, 5, 9)
    assert subscriptions.trial([hold, *full], known=False) is None
    assert subscriptions.trial(full, known=True) is None
    too_big = Charge(uuid.uuid4(), first, D("-5.00"))
    assert subscriptions.trial([too_big, *full], known=True) is None
    too_late = Charge(uuid.uuid4(), date(2026, 3, 20), D("-1.00"))
    assert subscriptions.trial([too_late, *full], known=True) is None
    same_day = Charge(uuid.uuid4(), date(2026, 5, 8), D("-1.00"))
    assert subscriptions.trial([same_day, *full], known=True) is None

    held = book.charge(first, "-1.00", "NETFLIX.COM", "netflix")
    paid = book.monthly(date(2026, 5, 9), ["-15.49"] * 2, "NETFLIX.COM", "netflix")
    for _ in range(2):
        book.detect(date(2026, 6, 12))
    assert book.kinds() == ["new", "trial_converted"]
    converted = book.events()[1]
    assert (converted.occurred_on, converted.transaction_id) == (date(2026, 5, 9), paid[0])
    assert (converted.amount_before, converted.amount_after) == (D("-1.00"), D("-15.49"))
    # The hold is not a charge of the series.
    assert book.charges()[held].scheduled_transaction_id is None


def _run_daily_job(book: Book, on: date) -> None:
    """``recurring.detect`` for the household, as the worker runs it."""
    spec = REGISTRY.get(recurring_jobs.DETECT_JOB_TYPE)
    assert spec is not None
    with book.factories.transaction() as db:
        set_context(db, book.household_id)
        ctx = JobContext(
            job_id=uuid.uuid4(),
            job_type=recurring_jobs.DETECT_JOB_TYPE,
            attempt=1,
            max_attempts=spec.max_attempts,
            household_id=book.household_id,
            session=db,
            deadline=datetime.max.replace(tzinfo=UTC),
        )
        spec.func(ctx, spec.validate_payload({"as_of": on.isoformat()}))


def test_renewal_is_announced_once_a_week_ahead(book: Book) -> None:
    book.charge(date(2024, 6, 20), "-99.00", "LANTERN PRESS", "lantern press")
    book.charge(date(2025, 6, 20), "-99.00", "LANTERN PRESS", "lantern press")
    book.detect(date(2025, 6, 25))
    annual = book.one()
    assert (annual.cadence, annual.status, annual.next_due) == (
        Cadence.YEARLY,
        "active",
        date(2026, 6, 20),
    )
    book.decide(annual.id, kind="subscription", kind_source="person")
    # A monthly subscription renews every month: that is never announced.
    book.monthly(date(2025, 12, 7), ["-9.99"] * 6, "TUNEBOX 7781", "tunebox")
    _run_daily_job(book, date(2026, 6, 1))
    book.decide(book.by_key("tunebox").id, kind="subscription", kind_source="person")
    _run_daily_job(book, date(2026, 6, 1))
    assert book.kinds() == []
    # A week ahead by the calendar, but the account's latest row is from May:
    # the clock is the data.
    _run_daily_job(book, date(2026, 6, 15))
    assert book.kinds() == []
    book.charge(date(2026, 6, 7), "-9.99", "TUNEBOX 7781", "tunebox")
    book.tick(date(2026, 6, 14))
    for _ in range(2):
        _run_daily_job(book, date(2026, 6, 15))
    (event,) = book.events()
    assert (event.kind, event.schedule_id, event.occurred_on) == (
        "renewal_due",
        annual.id,
        date(2026, 6, 20),
    )
    assert event.amount_after == D("-99.00")
    assert {row.merchant_key for row in book.series()} == {"lantern press", "tunebox"}


# ---------------------------------------------------------------------------
# Review findings, stage 2 lane S
# ---------------------------------------------------------------------------

#: A person-to-person payment: its series has no merchant key (the key is the service).
TO_A_PERSON = "ZELLE PAYMENT TO IMRE CASTELLAN"


def test_a_suggested_series_that_stops_and_resumes_stays_suggested(book: Book) -> None:
    book.monthly(date(2025, 1, 5), ["-650.00"] * 5, TO_A_PERSON, None)
    book.detect(date(2025, 5, 6))
    series = book.one()
    assert (series.status, series.kind, series.merchant_key) == ("suggested", "bill", None)
    book.tick(date(2025, 9, 20))
    book.detect(date(2025, 9, 21))
    assert (book.one().status, book.one().status_source) == ("ended", "detector")
    for i in range(3):
        day = add_months(date(2025, 10, 5), i)
        book.charge(day, "-650.00", TO_A_PERSON, None)
        (candidate,) = book.found(day)
        assert candidate.status == "suggested"
        book.detect(day + timedelta(days=1))
        again = book.one()
        assert again.id == series.id
        # What the detector reopens is what it finds: a suggestion stays one.
        assert (again.status, again.status_source, again.active) == ("suggested", "detector", False)
        assert again.ended_on is None


def test_a_series_without_a_merchant_key_that_still_charges_is_not_ended(book: Book) -> None:
    # A keyed series next to it, so the household's runs are ordinary (not quiet) ones.
    book.monthly(date(2025, 1, 7), ["-9.99"] * 6, "TUNEBOX 7781", "tunebox")
    book.monthly(date(2025, 1, 5), ["-650.00"] * 3, TO_A_PERSON, None)
    book.detect(date(2025, 3, 8))
    rent = next(row for row in book.series() if row.merchant_key is None)
    book.decide(rent.id, status="confirmed", status_source="person", active=True)
    # April is skipped; May and June are paid. Detection sees no rhythm in that,
    # but the payments go on.
    for month in (5, 6):
        book.charge(date(2025, month, 5), "-650.00", TO_A_PERSON, None)
        assert [c.payee_key for c in book.found(date(2025, month, 6))] == ["merchant:tunebox"]
        book.detect(date(2025, month, 6))
        after = book.get(rent.id)
        assert (after.status, after.status_source, after.active) == ("confirmed", "person", True)
    assert book.kinds_of(rent.id) == []
    # Once they really stop, the series ends as any other.
    book.tick(date(2025, 9, 20))
    book.detect(date(2025, 9, 21))
    assert (book.get(rent.id).status, book.get(rent.id).status_source) == ("ended", "detector")
    assert book.kinds_of(rent.id) == ["ended"]


def test_one_stray_charge_after_a_person_ended_it_is_reported(book: Book) -> None:
    book.monthly(date(2025, 1, 8), ["-8.00"] * 4, "FILMHUT 220", "filmhut")
    book.detect(date(2025, 4, 9))
    series = book.one()
    book.decide(
        series.id, status="ended", status_source="person", active=False, ended_on=date(2025, 4, 20)
    )
    # Something else bought there, at another price: not a charge of the series.
    other = book.charge(date(2025, 6, 2), "-23.75", "FILMHUT 220", "filmhut")
    book.detect(date(2025, 6, 3))
    assert book.kinds() == []
    # Months later the old price is charged once more. One charge has no rhythm,
    # so detection finds no series in it.
    stray = book.charge(date(2025, 8, 8), "-8.00", "FILMHUT 220", "filmhut")
    book.as_imported()
    assert book.found(date(2025, 8, 9)) == []
    for _ in range(2):
        book.detect(date(2025, 8, 9))
    (event,) = book.events()
    assert (event.kind, event.occurred_on, event.transaction_id) == (
        "charged_after_end",
        date(2025, 8, 8),
        stray,
    )
    assert event.amount_after == D("-8.00")
    after = book.one()
    assert (after.status, after.status_source, after.ended_on) == (
        "ended",
        "person",
        date(2025, 4, 20),
    )
    # The charge is filed with its series, and no row looks touched.
    charges = book.charges()
    assert charges[stray].scheduled_transaction_id == series.id
    assert charges[other].scheduled_transaction_id is None
    assert all(row.updated_at == row.created_at for row in charges.values())


def test_a_price_that_goes_back_is_dated_where_it_went_back(book: Book) -> None:
    book.monthly(date(2025, 1, 8), ["-10.99"] * 6 + ["-12.99"], "TUNEBOX 7781", "tunebox")
    book.detect(date(2025, 6, 9))
    book.detect(date(2025, 7, 9))
    back = book.charge(date(2025, 8, 8), "-10.99", "TUNEBOX 7781", "tunebox")
    for _ in range(2):
        book.detect(date(2025, 8, 9))
    up, down = book.events()
    assert (up.kind, up.occurred_on) == ("price_changed", date(2025, 7, 8))
    assert (down.kind, down.occurred_on, down.transaction_id) == (
        "price_changed",
        date(2025, 8, 8),
        back,
    )
    assert (down.amount_before, down.amount_after) == (D("-12.99"), D("-10.99"))
    series = book.one()
    assert (series.last_amount, series.previous_amount, series.price_changed_on) == (
        D("-10.99"),
        D("-12.99"),
        date(2025, 8, 8),
    )
    # The next charge at that price says nothing new.
    book.charge(date(2025, 9, 8), "-10.99", "TUNEBOX 7781", "tunebox")
    book.detect(date(2025, 9, 9))
    assert book.kinds() == ["price_changed", "price_changed"]
    assert book.one().price_changed_on == date(2025, 8, 8)


def test_missed_is_said_when_the_grace_has_passed(book: Book) -> None:
    with book.factories.transaction() as db:
        make_channel(db, book.household_id, book.factories.keyring)
    due = date(2026, 7, 7)
    book.monthly(JAN, ["-9.99"] * 6, "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 6, 10))
    long_after = date(2027, 3, 1)
    # Five days late is inside the grace of a monthly charge.
    book.tick(due + timedelta(days=5))
    book.detect(long_after)
    assert book.kinds() == []
    # The sixth day: detection still counts the series as running, and the charge is missed.
    book.tick(due + timedelta(days=6))
    assert [c.payee_key for c in book.found(long_after)] == ["merchant:tunebox"]
    for _ in range(2):
        book.detect(long_after)
    (missed,) = book.events()
    assert (missed.kind, missed.occurred_on, missed.schedule_id) == (
        "missed",
        due,
        book.by_key("tunebox").id,
    )
    assert book.sends() == ["subscription.missed"]
    assert (book.by_key("tunebox").status, book.by_key("tunebox").active) == ("active", True)
    # Later, when detection no longer finds it, the same missed charge is not said again.
    book.tick(due + timedelta(days=28))
    book.detect(long_after)
    assert book.kinds() == ["missed"]
    # A charge that arrives late after all: the series simply goes on.
    book.charge(due + timedelta(days=29), "-9.99", "TUNEBOX 7781", "tunebox")
    book.detect(long_after)
    assert book.kinds() == ["missed"]
    assert book.by_key("tunebox").occurrences == 7


def test_an_annual_charge_that_did_not_come_is_missed_after_three_weeks(book: Book) -> None:
    book.charge(date(2023, 3, 10), "-99.00", "LANTERN PRESS", "lantern press")
    book.charge(date(2024, 3, 10), "-99.00", "LANTERN PRESS", "lantern press")
    for _ in range(2):  # the second run is an ordinary one
        book.detect(date(2024, 3, 12))
    due = date(2025, 3, 10)
    assert (book.one().status, book.one().next_due) == ("active", due)
    book.tick(due + timedelta(days=21))
    book.detect(due + timedelta(days=21))
    assert book.kinds() == []
    book.tick(due + timedelta(days=22))
    for _ in range(2):
        book.detect(due + timedelta(days=22))
    (missed,) = book.events()
    assert (missed.kind, missed.occurred_on) == ("missed", due)
    # One missed charge ends nothing.
    assert book.by_key("lantern press").status == "active"


def test_a_new_plan_after_a_dismissed_one_is_suggested_not_swallowed(book: Book) -> None:
    book.monthly(date(2025, 1, 8), ["-4.99"] * 4, "GLYPHCLOUD STORAGE", "glyphcloud")
    book.detect(date(2025, 4, 9))
    dismissed = book.one()
    book.decide(dismissed.id, status="dismissed", status_source="person", active=False)
    # The small plan stops. Five months later the merchant bills another one.
    book.monthly(date(2025, 9, 15), ["-29.99"] * 5, "GLYPHCLOUD PRO", "glyphcloud")
    on = date(2026, 1, 16)
    (candidate,) = book.found(on)
    assert candidate.status == "active"
    counts = book.detect(on)
    assert (counts.created, counts.skipped_dismissed) == (1, 0)
    tombstone = book.get(dismissed.id)
    assert (tombstone.status, tombstone.status_source) == ("dismissed", "person")
    assert (tombstone.amount_min, tombstone.amount_max) == (D("-4.99"), D("-4.99"))
    (plan,) = [row for row in book.series() if row.id != dismissed.id]
    # The person said "not a subscription" about this merchant once: it is only asked.
    assert (plan.status, plan.status_source, plan.active) == ("suggested", "detector", False)
    assert plan.last_amount == D("-29.99")
    # ... on every later run too, until a person says so.
    again = book.detect(on)
    assert (again.created, again.skipped_dismissed) == (0, 0)
    assert book.get(plan.id).status == "suggested"
    book.decide(plan.id, status="confirmed", status_source="person", active=True)
    book.detect(on)
    assert len(book.series()) == 2
    assert book.get(plan.id).status == "confirmed"


def test_a_dismissed_series_whose_price_rises_stays_dismissed(book: Book) -> None:
    book.monthly(date(2025, 1, 8), ["-9.99"] * 4, "TUNEBOX 7781", "tunebox")
    book.detect(date(2025, 4, 9))
    book.decide(book.one().id, status="dismissed", status_source="person", active=False)
    # It goes on charging, and from July at a higher price: the same series.
    book.monthly(date(2025, 5, 8), ["-9.99"] * 2 + ["-11.99"] * 3, "TUNEBOX 7781", "tunebox")
    for on in (date(2025, 7, 9), date(2025, 9, 9), date(2025, 9, 9)):
        counts = book.detect(on)
        assert (counts.created, counts.skipped_dismissed) == (0, 1)
    tombstone = book.one()
    assert (tombstone.status, tombstone.status_source) == ("dismissed", "person")
    # The tombstone follows the price, so it still matches when the old charges
    # have left the look-back window.
    assert (tombstone.amount_min, tombstone.amount_max) == (D("-11.99"), D("-11.99"))
    assert book.kinds() == []


def test_the_first_run_says_nothing_about_a_price_or_an_end(book: Book) -> None:
    with book.factories.transaction() as db:
        make_channel(db, book.household_id, book.factories.keyring)
    # Two series from before merchant keys whose facts were kept: one whose price
    # rose since, one whose charges stopped.
    priced = book.ledger.run(
        make_scheduled_transaction,
        book.account,
        name="TUNEBOX 7781",
        origin=ScheduleOrigin.DETECTED,
        next_due=date(2026, 6, 7),
        amount_min="-13.00",
        amount_max="-9.00",
    )
    gone = book.ledger.run(
        make_scheduled_transaction,
        book.account,
        name="Granite Gym",
        origin=ScheduleOrigin.DETECTED,
        next_due=date(2026, 2, 1),
        amount_min="-20.00",
        amount_max="-20.00",
    )
    book.decide(priced.id, last_amount=D("-9.99"), occurrences=5, last_seen_on=date(2026, 5, 7))
    book.decide(gone.id, last_amount=D("-20.00"), occurrences=9, last_seen_on=date(2026, 1, 1))
    book.monthly(JAN, ["-9.99"] * 5 + ["-12.99"], "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 6, 10))
    tunebox = book.by_key("tunebox")
    assert tunebox.id == priced.id
    assert (tunebox.last_amount, tunebox.previous_amount, tunebox.price_changed_on) == (
        D("-12.99"),
        D("-9.99"),
        date(2026, 6, 7),
    )
    assert (book.get(gone.id).status, book.get(gone.id).status_source) == ("ended", "detector")
    # The household's first run: the facts are written and nothing is announced.
    assert book.kinds() == []
    assert book.sends() == []
    # From the second run on a change is said.
    book.charge(date(2026, 7, 7), "-15.99", "TUNEBOX 7781", "tunebox")
    book.detect(date(2026, 7, 10))
    assert book.kinds() == ["price_changed"]
    assert book.sends() == ["subscription.price_changed"]


def test_a_series_entered_by_hand_is_never_settled(book: Book) -> None:
    manual = book.ledger.run(
        make_scheduled_transaction,
        book.account,
        name="Ironworks Gym",
        next_due=date(2026, 2, 1),
        amount_min="-36.00",
        amount_max="-34.00",
    )
    before = book.get(manual.id)
    assert (before.origin, before.status) == (ScheduleOrigin.MANUAL, "confirmed")
    # No charge of it ever arrives, and the clock runs months past its due date.
    book.monthly(JAN, ["-9.99"] * 6, "TUNEBOX 7781", "tunebox")
    for on in (date(2026, 6, 10), date(2026, 6, 11)):
        book.detect(on)
    after = book.get(manual.id)
    for column in ScheduledTransaction.__table__.columns:
        assert getattr(after, column.name) == getattr(before, column.name), column.name
    assert book.kinds() == []


def test_only_a_counted_long_cycle_series_is_announced_as_renewing(book: Book) -> None:
    for text, key in (("LANTERN PRESS", "lantern press"), ("HARBOR CLUB", "harbor club")):
        book.charge(date(2024, 6, 20), "-99.00", text, key)
        book.charge(date(2025, 6, 20), "-99.00", text, key)
    # A monthly charge due on the 18th: inside the week too, and never a renewal.
    book.monthly(date(2025, 12, 18), ["-9.99"] * 6, "TUNEBOX 7781", "tunebox")
    assert book.detect(date(2026, 5, 20)).created == 3
    for row in book.series():
        assert row.status == "active"
        book.decide(row.id, kind="subscription", kind_source="person")
    paused = book.by_key("harbor club")
    book.decide(paused.id, status="paused", status_source="person", active=False)
    assert book.by_key("tunebox").next_due == date(2026, 6, 18)
    assert paused.next_due == book.by_key("lantern press").next_due == date(2026, 6, 20)
    book.tick(date(2026, 6, 14))
    for stored in (1, 0):
        with book.factories.transaction() as db:
            set_context(db, book.household_id)
            assert subscriptions.renewals(db, book.household_id, date(2026, 6, 15)) == stored
    (event,) = book.events()
    assert (event.kind, event.schedule_id, event.occurred_on) == (
        "renewal_due",
        book.by_key("lantern press").id,
        date(2026, 6, 20),
    )


def test_a_small_first_charge_at_an_unknown_merchant_is_no_trial(book: Book) -> None:
    streaming = book.ledger.category(
        book.household_id, name="Streaming", system_key="leisure.subscriptions"
    )
    text, key = "QUARRY LANE STUDIO", "quarry lane studio"
    book.charge(date(2026, 5, 2), "-1.00", text, key, category=streaming)
    for week in range(4):
        book.charge(
            date(2026, 5, 9) + timedelta(days=7 * week), "-15.49", text, key, category=streaming
        )
    for _ in range(2):
        book.detect(date(2026, 6, 1))
    series = book.one()
    assert (series.kind, series.status, series.last_amount) == (
        "subscription",
        "active",
        D("-15.49"),
    )
    # New, yes. A trial that became a paid plan is only read for a dictionary subscription.
    assert book.kinds() == ["new"]


def test_a_bill_whose_amount_varies_has_no_price_to_change(book: Book) -> None:
    power = book.ledger.category(
        book.household_id, name="Electricity", system_key="utilities.electricity"
    )
    amounts = ["-80.00", "-91.00", "-74.00", "-82.00"]
    book.monthly(JAN, amounts, "HILL POWER CO", "hill power", category=power)
    for _ in range(2):
        book.detect(date(2026, 4, 10))
    bill = book.one()
    assert (bill.kind, bill.status, bill.last_amount) == ("bill", "active", D("-82.00"))
    # A fifth more than last month: a variable bill does that.
    book.charge(date(2026, 5, 7), "-99.00", "HILL POWER CO", "hill power", category=power)
    for _ in range(2):
        book.detect(date(2026, 5, 10))
    after = book.one()
    assert (after.last_amount, after.previous_amount, after.price_changed_on) == (
        D("-99.00"),
        None,
        None,
    )
    assert book.kinds() == []
