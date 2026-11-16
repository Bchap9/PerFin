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
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, update

from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.recurring import detection, service
from app.domain.recurring.detection import Candidate, Pattern
from app.domain.reports.dates import add_months
from app.domain.transactions.models import (
    Cadence,
    ScheduledTransaction,
    ScheduleOrigin,
    Transaction,
)
from tests.fixtures.factories import Factories
from tests.fixtures.ledger import LedgerFactories, make_payee, make_scheduled_transaction

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
