# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subscriptions and bills over HTTP: the list with its cost, one series, a
decision, what changed.

"Today" is frozen at 2026-06-30. Everything is invented. A household with an
owner, a member (edit on ``checking``, view on ``shared``, nothing on
``hidden``) and a viewer (view on ``checking``). Series and their changes are
written as detection would leave them; one test runs the real detection to
show a dismissed series stays away.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.recurring import service as recurring_service
from app.domain.recurring.models import SubscriptionEvent
from app.domain.reports.dates import add_months
from app.domain.transactions.models import (
    Cadence,
    ScheduledTransaction,
    ScheduleOrigin,
    Transaction,
)
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, household_audit, household_events, problem_type
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration

SUBS = f"{API}/subscriptions"
REC = f"{API}/recurring"
TODAY = date(2026, 6, 30)
D = Decimal


def usd(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "USD"}


@dataclass(frozen=True)
class World:
    owner: AuthedClient
    member: AuthedClient
    viewer: AuthedClient
    household_id: uuid.UUID
    checking: Account
    hidden: Account
    shared: Account
    ledger: LedgerFactories
    factories: Factories

    def series(
        self,
        name: str,
        *,
        account: Account | None = None,
        amount: str = "-10.00",
        cadence: Cadence = Cadence.MONTHLY,
        next_due: date = date(2026, 7, 5),
        **columns: Any,
    ) -> uuid.UUID:
        """A series written as detection (or a person's decision) would leave it."""
        on = account or self.checking
        columns.setdefault("kind", "subscription")
        columns.setdefault("status", "active")
        columns.setdefault("amount_min", D(amount))
        columns.setdefault("amount_max", D(amount))
        with self.factories.transaction() as db:
            set_context(db, on.household_id)
            row = ScheduledTransaction(
                household_id=on.household_id,
                account_id=on.id,
                name=name,
                cadence=cadence,
                next_due=next_due,
                currency=on.currency,
                origin=ScheduleOrigin.DETECTED,
                **columns,
            )
            db.add(row)
            db.flush()
            return row.id

    def event(
        self,
        schedule_id: uuid.UUID,
        kind: str,
        occurred_on: date,
        *,
        account: Account | None = None,
        **columns: Any,
    ) -> uuid.UUID:
        on = account or self.checking
        with self.factories.transaction() as db:
            set_context(db, on.household_id)
            row = SubscriptionEvent(
                household_id=on.household_id,
                schedule_id=schedule_id,
                account_id=on.id,
                kind=kind,
                occurred_on=occurred_on,
                **columns,
            )
            db.add(row)
            db.flush()
            return row.id

    def row(self, schedule_id: uuid.UUID) -> ScheduledTransaction:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            return db.get_one(ScheduledTransaction, schedule_id)

    def states(self, *event_ids: uuid.UUID, household_id: uuid.UUID | None = None) -> list[str]:
        with self.factories.transaction() as db:
            set_context(db, household_id or self.household_id)
            return [db.get_one(SubscriptionEvent, event_id).state for event_id in event_ids]

    def charges(self, account: Account) -> list[Transaction]:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            return list(
                db.execute(
                    select(Transaction)
                    .where(Transaction.account_id == account.id)
                    .order_by(Transaction.posted_date, Transaction.id)
                ).scalars()
            )


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


@pytest.fixture
def world(
    actors: Actors, ledger: LedgerFactories, factories: Factories, monkeypatch: pytest.MonkeyPatch
) -> World:
    monkeypatch.setattr(recurring_service, "today", lambda: TODAY)
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    viewer = actors.new(role="viewer", household=household)
    hid = household.id
    checking = ledger.account(hid, type=AccountType.CHECKING, name="Checking")
    hidden = ledger.account(hid, type=AccountType.CHECKING, name="Hidden")
    shared = ledger.account(hid, type=AccountType.CHECKING, name="Shared")
    ledger.grant(hid, member.record.id, checking, GrantLevel.EDIT)
    ledger.grant(hid, member.record.id, shared, GrantLevel.VIEW)
    ledger.grant(hid, viewer.record.id, checking, GrantLevel.VIEW)
    return World(owner, member, viewer, hid, checking, hidden, shared, ledger, factories)


def _ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def _names(body: Any) -> list[str]:
    return [item["name"] for item in body["items"]]


def _decide(client: AuthedClient, schedule_id: uuid.UUID, decision: str, **more: Any) -> Any:
    return client.post(f"{SUBS}/{schedule_id}/decision", json={"decision": decision, **more})


# ---------------------------------------------------------------------------
# The list and its totals
# ---------------------------------------------------------------------------


def test_totals_count_active_and_confirmed_only_and_split_subscriptions_from_bills(
    world: World,
) -> None:
    world.series("Lumaflix", amount_min=D("-12.00"), amount_max=D("-8.00"), last_amount=D("-10.00"))
    world.series(
        "Atlas Maps",
        amount="-120.00",
        cadence=Cadence.YEARLY,
        status="confirmed",
        status_source="person",
    )
    world.series("Dawn Paper", amount="-5.00", cadence=Cadence.WEEKLY)
    world.series("Maybe Radio", amount="-9.00", status="suggested")
    world.series("Idle Gym", amount="-30.00", status="paused", status_source="person")
    world.series("Brook Water", amount="-60.00", kind="bill")
    world.series("Gone Club", amount="-3.00", status="dismissed", status_source="person")
    world.series("Wages", amount="2500.00", kind="income")
    world.series("Odd Thing", amount="-1.00", kind="other")

    body = _ok(world.owner.get(SUBS))
    assert body["as_of"] == TODAY.isoformat()
    assert body["subscriptions"] == {
        "currency": "USD",
        "monthly": usd("41.67"),
        "yearly": usd("500.00"),
        "count": 3,
        "unconverted": [],
    }
    assert body["bills"] == {
        "currency": "USD",
        "monthly": usd("60.00"),
        "yearly": usd("720.00"),
        "count": 1,
        "unconverted": [],
    }
    # Dismissed, income and "other" series are not in the default list.
    assert sorted(_names(body)) == [
        "Atlas Maps",
        "Brook Water",
        "Dawn Paper",
        "Idle Gym",
        "Lumaflix",
        "Maybe Radio",
    ]
    by_name = {item["name"]: item for item in body["items"]}
    paper = by_name["Dawn Paper"]
    assert (paper["amount"], paper["monthly_cost"], paper["yearly_cost"]) == (
        usd("-5.00"),
        usd("21.67"),
        usd("260.00"),
    )
    # The latest charge when one was seen, else the middle of the range.
    assert by_name["Lumaflix"]["amount"] == usd("-10.00")
    assert by_name["Atlas Maps"]["amount"] == usd("-120.00")
    assert by_name["Atlas Maps"]["monthly_cost"] == usd("10.00")
    assert (by_name["Atlas Maps"]["status"], by_name["Atlas Maps"]["status_source"]) == (
        "confirmed",
        "person",
    )
    assert by_name["Lumaflix"]["kind_source"] == "detector"

    # Filters choose the rows; the totals stay those of everything counted.
    dismissed = _ok(world.owner.get(SUBS, params={"status": "dismissed"}))
    assert _names(dismissed) == ["Gone Club"]
    assert dismissed["items"][0]["next_due"] is None
    assert dismissed["subscriptions"] == body["subscriptions"]
    bills = _ok(world.owner.get(SUBS, params={"kind": "bill"}))
    assert _names(bills) == ["Brook Water"]
    both = _ok(world.owner.get(SUBS, params={"status": ["paused", "suggested"]}))
    assert sorted(_names(both)) == ["Idle Gym", "Maybe Radio"]
    assert world.owner.get(SUBS, params={"kind": "income"}).status_code == 422


def test_a_foreign_currency_series_is_converted_or_listed(world: World) -> None:
    hid = world.household_id
    euro = world.ledger.account(hid, type=AccountType.CHECKING, name="Euro", currency="EUR")
    franc = world.ledger.account(hid, type=AccountType.CHECKING, name="Franc", currency="CHF")
    world.ledger.fx_rate(hid, base="EUR", quote="USD", rate_date=date(2026, 6, 1), rate="1.1")
    world.series("Lumaflix")
    world.series("Brise Radio", account=euro)
    lost = world.series("Lingua Club", account=franc, amount="-20.00")

    body = _ok(world.owner.get(SUBS))
    assert body["subscriptions"] == {
        "currency": "USD",
        "monthly": usd("21.00"),
        "yearly": usd("252.00"),
        "count": 2,
        "unconverted": [str(lost)],
    }
    by_name = {item["name"]: item for item in body["items"]}
    # A row's own cost stays in its account's currency.
    assert by_name["Brise Radio"]["monthly_cost"] == {"amount": "10.00", "currency": "EUR"}
    assert by_name["Lingua Club"]["yearly_cost"] == {"amount": "240.00", "currency": "CHF"}


def test_needs_look_counts_suggested_and_open_events(world: World) -> None:
    maybe = world.series("Maybe Radio", status="suggested", next_due=date(2026, 7, 9))
    changed = world.series("Lumaflix", next_due=date(2026, 7, 20))
    world.event(changed, "price_changed", date(2026, 6, 20))
    world.event(changed, "new", date(2026, 4, 20))
    quiet = world.series("Dawn Paper", next_due=date(2026, 7, 1))
    world.event(quiet, "new", date(2026, 5, 1), state="seen")
    stopped = world.series("Idle Gym", status="ended", next_due=date(2026, 7, 2))
    world.event(stopped, "ended", date(2026, 5, 2))
    gone = world.series("Gone Club", status="dismissed", status_source="person")
    world.event(gone, "new", date(2026, 5, 3))
    income = world.series("Wages", amount="2500.00", kind="income", status="suggested")

    body = _ok(world.owner.get(SUBS))
    assert body["needs_look"] == 3
    opened = {item["name"]: item["open_events"] for item in body["items"]}
    assert opened == {"Maybe Radio": 0, "Lumaflix": 2, "Dawn Paper": 0, "Idle Gym": 1}
    # Open changes first, then the soonest.
    assert _names(body) == ["Idle Gym", "Lumaflix", "Dawn Paper", "Maybe Radio"]
    del maybe, income

    events = _ok(world.owner.get(f"{SUBS}/events", params={"state": "open"}))
    assert events["open"] == 4
    assert [(e["name"], e["kind"]) for e in events["items"]] == [
        ("Lumaflix", "price_changed"),
        ("Gone Club", "new"),
        ("Idle Gym", "ended"),
        ("Lumaflix", "new"),
    ]


def test_events_are_paged_newest_first(world: World) -> None:
    series = world.series("Lumaflix")
    days = [date(2026, 3, 5), date(2026, 4, 5), date(2026, 5, 5)]
    ids = [str(world.event(series, "missed", day, state="seen")) for day in days]
    first = _ok(world.viewer.get(f"{SUBS}/events", params={"limit": 2}))
    assert [e["id"] for e in first["items"]] == [ids[2], ids[1]]
    assert first["next_cursor"] == ids[1]
    assert first["open"] == 0
    assert first["items"][0] == {
        "id": ids[2],
        "schedule_id": str(series),
        "name": "Lumaflix",
        "kind": "missed",
        "occurred_on": "2026-05-05",
        "amount_before": None,
        "amount_after": None,
        "transaction_id": None,
        "state": "seen",
    }
    rest = _ok(world.viewer.get(f"{SUBS}/events", params={"limit": 2, "cursor": ids[1]}))
    assert [e["id"] for e in rest["items"]] == [ids[0]]
    assert rest["next_cursor"] is None
    unknown = _ok(world.viewer.get(f"{SUBS}/events", params={"cursor": str(uuid.uuid4())}))
    assert unknown["items"] == []


def test_overdue_uses_the_account_s_latest_row(world: World) -> None:
    # Monthly: five days of grace. Checking's statements stop on 8 June, the
    # shared account's on 20 June; the hidden account has no row at all.
    world.ledger.transaction(world.checking, amount="-1.00", posted_date=date(2026, 6, 8))
    world.ledger.transaction(world.shared, amount="-1.00", posted_date=date(2026, 6, 20))
    due = date(2026, 6, 5)
    world.series("Lumaflix", next_due=due)
    world.series("Brise Radio", account=world.shared, next_due=due)
    world.series("Lingua Club", account=world.hidden, next_due=due)
    world.series(
        "Idle Gym", account=world.shared, next_due=due, status="paused", status_source="person"
    )
    body = _ok(world.owner.get(SUBS))
    overdue = {item["name"]: item["overdue"] for item in body["items"]}
    # Today is 30 June, but nobody uploaded checking's June statement yet.
    assert overdue == {
        "Lumaflix": False,
        "Brise Radio": True,
        "Lingua Club": False,
        "Idle Gym": False,
    }


# ---------------------------------------------------------------------------
# One series
# ---------------------------------------------------------------------------


def test_detail_lists_charges_and_price_history(world: World) -> None:
    series = world.series(
        "Lumaflix",
        amount_min=D("-12.99"),
        amount_max=D("-10.99"),
        last_amount=D("-12.99"),
        previous_amount=D("-10.99"),
        price_changed_on=date(2026, 6, 5),
        merchant_key="lumaflix",
        first_seen_on=date(2026, 4, 5),
        last_seen_on=date(2026, 6, 5),
        occurrences=3,
        confidence=D("0.9"),
    )
    charges = [
        world.ledger.transaction(
            world.checking,
            amount=amount,
            posted_date=date(2026, month, 5),
            raw_payee=f"LUMAFLIX #{5100 + month}",
            merchant_key="lumaflix",
            scheduled_transaction_id=series,
        )
        for month, amount in ((4, "-10.99"), (5, "-10.99"), (6, "-12.99"))
    ]
    world.ledger.transaction(
        world.checking, amount="-12.99", posted_date=date(2026, 6, 6), raw_payee="LUMAFLIX GIFT"
    )
    world.event(series, "new", date(2026, 4, 5), state="seen", amount_after=D("-10.99"))
    change = world.event(
        series,
        "price_changed",
        date(2026, 6, 5),
        amount_before=D("-10.99"),
        amount_after=D("-12.99"),
        transaction_id=charges[2].id,
    )

    body = _ok(world.viewer.get(f"{SUBS}/{series}"))
    subscription = body["subscription"]
    assert subscription["id"] == str(series)
    assert subscription["merchant"] == "Lumaflix"
    assert subscription["amount"] == usd("-12.99")
    assert (subscription["amount_min"], subscription["amount_max"]) == (
        usd("-12.99"),
        usd("-10.99"),
    )
    assert subscription["monthly_cost"] == usd("12.99")
    assert subscription["yearly_cost"] == usd("155.88")
    assert subscription["next_due"] == "2026-07-05"
    assert (subscription["first_seen_on"], subscription["last_seen_on"]) == (
        "2026-04-05",
        "2026-06-05",
    )
    assert (subscription["occurrences"], subscription["confidence"]) == (3, "0.90")
    assert subscription["open_events"] == 1
    # One charge at the new price so far: not confirmed yet.
    assert subscription["price_change"] == {
        "previous": usd("-10.99"),
        "current": usd("-12.99"),
        "on": "2026-06-05",
        "pending": True,
    }
    # Its own charges, newest first; the unlinked row is not one of them.
    assert body["charges"] == [
        {
            "transaction_id": str(charge.id),
            "posted_date": charge.posted_date.isoformat(),
            "amount": usd(f"{charge.amount:.2f}"),
        }
        for charge in reversed(charges)
    ]
    assert [(e["kind"], e["state"]) for e in body["events"]] == [
        ("price_changed", "open"),
        ("new", "seen"),
    ]
    assert body["events"][0] == {
        "id": str(change),
        "schedule_id": str(series),
        "name": "Lumaflix",
        "kind": "price_changed",
        "occurred_on": "2026-06-05",
        "amount_before": usd("-10.99"),
        "amount_after": usd("-12.99"),
        "transaction_id": str(charges[2].id),
        "state": "open",
    }

    # A second charge at the new price confirms it.
    world.ledger.transaction(
        world.checking,
        amount="-12.99",
        posted_date=date(2026, 6, 28),
        raw_payee="LUMAFLIX #5199",
        scheduled_transaction_id=series,
    )
    listed = _ok(world.viewer.get(SUBS))["items"][0]
    assert listed["price_change"]["pending"] is False


# ---------------------------------------------------------------------------
# A decision
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("decision", "start", "status", "active"),
    [
        ("confirm", "suggested", "confirmed", True),
        ("dismiss", "active", "dismissed", False),
        ("pause", "active", "paused", False),
        ("resume", "paused", "confirmed", True),
        ("end", "active", "ended", False),
        ("reopen", "ended", "confirmed", True),
    ],
)
def test_each_decision(world: World, decision: str, start: str, status: str, active: bool) -> None:
    series = world.series("Lumaflix", status=start, ended_on=date(2026, 5, 5))
    body = _ok(_decide(world.member, series, decision))
    assert (body["id"], body["status"], body["status_source"]) == (str(series), status, "person")
    assert (body["kind"], body["kind_source"]) == ("subscription", "detector")
    row = world.row(series)
    assert (row.status, row.status_source, row.active) == (status, "person", active)
    assert row.ended_on == (TODAY if status == "ended" else None)
    (entry,) = household_audit(world.factories, world.household_id, "subscription.decide")
    assert entry.target_id == str(series)
    assert entry.before == {"kind": "subscription", "status": start}
    assert entry.after == {"decision": decision, "kind": "subscription", "status": status}
    (event,) = household_events(world.factories, world.household_id, "schedule.updated")
    assert event.payload == {"schedule_id": str(series), "account_id": str(world.checking.id)}


def test_a_decision_may_say_which_of_the_two_it_is(world: World) -> None:
    series = world.series("Brook Water", kind="other", status="suggested")
    body = _ok(_decide(world.member, series, "confirm", kind="bill"))
    assert (body["kind"], body["kind_source"], body["status"]) == ("bill", "person", "confirmed")
    assert _names(_ok(world.member.get(SUBS, params={"kind": "bill"}))) == ["Brook Water"]
    refused = _decide(world.member, series, "confirm", kind="income")
    assert refused.status_code == 422
    assert _decide(world.member, series, "confirm", note="x").status_code == 422
    assert _decide(world.member, series, "cancel").status_code == 422


def test_dismiss_unlinks_its_charges_and_detection_does_not_bring_it_back(world: World) -> None:
    streaming = world.ledger.category(
        world.household_id, name="Streaming", system_key="leisure.subscriptions"
    )
    for i in range(12):
        world.ledger.transaction(
            world.checking,
            amount="-15.99",
            posted_date=add_months(date(2025, 7, 5), i),
            raw_payee=f"LUMAFLIX #{4100 + i}",
            merchant_key="lumaflix",
            category=streaming,
        )
    stamps = {t.id: t.updated_at for t in world.charges(world.checking)}
    first = _ok(world.member.post(f"{REC}/detect", json={}))
    assert first["created"] == 1
    series = uuid.UUID(first["items"][0]["schedule_id"])
    assert {t.scheduled_transaction_id for t in world.charges(world.checking)} == {series}
    assert _names(_ok(world.member.get(SUBS, params={"kind": "subscription"}))) == ["Lumaflix"]

    body = _ok(_decide(world.member, series, "dismiss"))
    assert (body["status"], body["status_source"], body["next_due"]) == (
        "dismissed",
        "person",
        None,
    )
    rows = world.charges(world.checking)
    assert {t.scheduled_transaction_id for t in rows} == {None}
    # Linking and unlinking are system writes: the rows do not read as touched.
    assert {t.id: t.updated_at for t in rows} == stamps

    again = _ok(world.member.post(f"{REC}/detect", json={}))
    assert (again["created"], again["updated"]) == (0, 0)
    assert again["items"] == []
    row = world.row(series)
    assert (row.status, row.status_source, row.active) == ("dismissed", "person", False)
    assert {t.scheduled_transaction_id for t in world.charges(world.checking)} == {None}
    assert _names(_ok(world.member.get(SUBS))) == []
    assert _names(_ok(world.member.get(SUBS, params={"status": "dismissed"}))) == ["Lumaflix"]


def test_a_decision_marks_the_series_events_seen(world: World) -> None:
    series = world.series("Lumaflix")
    other = world.series("Brise Radio")
    mine = [
        world.event(series, "price_changed", date(2026, 6, 5)),
        world.event(series, "missed", date(2026, 5, 5)),
    ]
    theirs = world.event(other, "new", date(2026, 6, 1))
    assert _ok(world.member.get(SUBS))["needs_look"] == 2
    body = _ok(_decide(world.member, series, "confirm"))
    assert body["open_events"] == 0
    assert world.states(*mine, theirs) == ["seen", "seen", "open"]
    events = _ok(world.member.get(f"{SUBS}/events", params={"state": "open"}))
    assert (events["open"], [e["id"] for e in events["items"]]) == (1, [str(theirs)])
    assert _ok(world.member.get(SUBS))["needs_look"] == 1


# ---------------------------------------------------------------------------
# Who may see and decide what
# ---------------------------------------------------------------------------


def test_viewer_cannot_decide_and_a_member_needs_edit_on_the_account(world: World) -> None:
    mine = world.series("Lumaflix", status="suggested")
    viewed = world.series("Brise Radio", account=world.shared, status="suggested")
    event = world.event(mine, "new", date(2026, 6, 1))

    assert _decide(world.viewer, mine, "confirm").status_code == 403
    assert world.viewer.post(f"{SUBS}/events/seen", json={"ids": [str(event)]}).status_code == 403
    assert _ok(world.viewer.get(f"{SUBS}/{mine}"))["subscription"]["status"] == "suggested"
    # The member sees the shared account's series and may not decide about it.
    assert _ok(world.member.get(f"{SUBS}/{viewed}"))["subscription"]["name"] == "Brise Radio"
    assert _decide(world.member, viewed, "confirm").status_code == 403
    assert world.row(viewed).status == "suggested"
    assert world.states(event) == ["open"]
    assert household_audit(world.factories, world.household_id, "subscription.decide") == []

    assert _ok(_decide(world.member, mine, "confirm"))["status"] == "confirmed"
    assert _ok(_decide(world.owner, viewed, "dismiss"))["status"] == "dismissed"


def test_series_on_invisible_accounts_are_absent_from_list_totals_and_events(
    world: World,
) -> None:
    world.series("Lumaflix")
    secret = world.series("Lingua Club", account=world.hidden, amount="-99.00")
    maybe = world.series("Brise Radio", account=world.hidden, status="suggested")
    hidden_event = world.event(secret, "price_changed", date(2026, 6, 9), account=world.hidden)
    del maybe

    body = _ok(world.member.get(SUBS))
    assert _names(body) == ["Lumaflix"]
    assert (body["subscriptions"]["monthly"], body["subscriptions"]["count"]) == (usd("10.00"), 1)
    assert body["needs_look"] == 0
    events = _ok(world.member.get(f"{SUBS}/events"))
    assert (events["items"], events["open"]) == ([], 0)
    refused = world.member.get(SUBS, params={"account": str(world.hidden.id)})
    assert refused.status_code == 404
    assert problem_type(refused) == "account-not-found"

    full = _ok(world.owner.get(SUBS))
    assert sorted(_names(full)) == ["Brise Radio", "Lingua Club", "Lumaflix"]
    assert (full["subscriptions"]["monthly"], full["subscriptions"]["count"]) == (usd("109.00"), 2)
    assert full["needs_look"] == 2
    seen = _ok(world.owner.get(f"{SUBS}/events"))
    assert ([e["id"] for e in seen["items"]], seen["open"]) == ([str(hidden_event)], 1)
    only = _ok(world.owner.get(SUBS, params={"account": str(world.checking.id)}))
    assert (_names(only), only["subscriptions"]["count"], only["needs_look"]) == (
        ["Lumaflix"],
        1,
        0,
    )


def test_mark_seen_ignores_foreign_ids(world: World, actors: Actors) -> None:
    mine = world.series("Lumaflix")
    visible = world.event(mine, "new", date(2026, 6, 1))
    already = world.event(mine, "missed", date(2026, 5, 1), state="seen")
    secret = world.series("Lingua Club", account=world.hidden)
    hidden_event = world.event(secret, "new", date(2026, 6, 2), account=world.hidden)
    stranger = actors.new(role="owner")
    assert stranger.record.household is not None
    elsewhere = world.ledger.account(stranger.record.household.id, name="Elsewhere")
    foreign = world.series("Brise Radio", account=elsewhere)
    foreign_event = world.event(foreign, "new", date(2026, 6, 3), account=elsewhere)

    ids = [visible, already, hidden_event, foreign_event, uuid.uuid4()]
    body = _ok(world.member.post(f"{SUBS}/events/seen", json={"ids": [str(i) for i in ids]}))
    assert body == {"seen": 1}
    assert world.states(visible, already, hidden_event) == ["seen", "seen", "open"]
    assert world.states(foreign_event, household_id=elsewhere.household_id) == ["open"]
    again = _ok(world.member.post(f"{SUBS}/events/seen", json={"ids": [str(visible)]}))
    assert again == {"seen": 0}
    assert world.member.post(f"{SUBS}/events/seen", json={"ids": []}).status_code == 422


def test_a_foreign_schedule_is_404(world: World, actors: Actors) -> None:
    stranger = actors.new(role="owner")
    assert stranger.record.household is not None
    elsewhere = world.ledger.account(stranger.record.household.id, name="Elsewhere")
    foreign = world.series("Brise Radio", account=elsewhere, status="suggested")
    secret = world.series("Lingua Club", account=world.hidden, status="suggested")
    for client, schedule_id in (
        (world.owner, foreign),
        (world.member, foreign),
        (world.member, secret),
        (world.owner, uuid.uuid4()),
    ):
        read = client.get(f"{SUBS}/{schedule_id}")
        assert read.status_code == 404
        assert problem_type(read) == "schedule-not-found"
        decided = _decide(client, schedule_id, "dismiss")
        assert decided.status_code == 404
        assert problem_type(decided) == "schedule-not-found"
    assert world.row(secret).status == "suggested"
    with world.factories.transaction() as db:
        set_context(db, elsewhere.household_id)
        assert db.get_one(ScheduledTransaction, foreign).status == "suggested"
