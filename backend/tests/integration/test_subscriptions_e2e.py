# SPDX-License-Identifier: AGPL-3.0-or-later
"""Subscriptions, end to end: statements in, series and their changes out (design note, section 6).

Nothing is stubbed. Statements go through the real upload and ingest job,
their rows are keyed and categorised by the shipped clean-up, dictionary and
engine, detection runs as its daily job, and people decide over HTTP
(``tests/fixtures/pipeline.py``). Only the wall clock is set where a story
needs "three months later": PerFin judges a series by its account's latest
row, and the tests show that the wall clock does not matter.

Two tests are the fourth and fifth scenario of the plan's "Review Focus";
the third imports the planted two-year ledger
(``tests/fixtures/synth_subscription_ledger.py``) one month at a time.
Merchant names the dictionary knows are real public brands; every number,
town, local business and person is invented.
"""

from __future__ import annotations

import calendar
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterator, Sequence
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.domain.accounts.models import Account
from app.domain.recurring import service as recurring_service
from app.domain.recurring.subscriptions import NEW_WINDOW_DAYS
from app.domain.transactions.models import ScheduledTransaction, Transaction
from tests.fixtures import synth_subscription_ledger as synth
from tests.fixtures.factories import Factories
from tests.fixtures.households import API, problem_type
from tests.fixtures.ledger import LedgerFactories, make_scheduled_transaction
from tests.fixtures.pipeline import RULES_JOB, Home, Line, ok, open_home, squash
from tests.fixtures.quality import FoundSeries, subscription_metrics

pytestmark = pytest.mark.integration

D = Decimal
SUBS = f"{API}/subscriptions"

NETFLIX = "NETFLIX.COM"
SPOTIFY = "SPOTIFY USA"
HULU = "HLU*HULU 7QD3KX9M-U HULU.COM/BILL CA"
DISNEY = "DISNEY PLUS 888-555-0190 CA"
KROGER = "KROGER #0412 ASHGROVE OH"
STARBUCKS = "STARBUCKS STORE 08213 ASHGROVE OH"
TOOL_HIRE = "HOLLOWAY TOOL HIRE"
YARD = "OSTLER YARD SUPPLY"


@pytest.fixture
def home(
    factories: Factories,
    ledger: LedgerFactories,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Home]:
    with open_home(factories, ledger, settings, tmp_path, monkeypatch) as built:
        yield built


def _series(home: Home, key: str) -> list[ScheduledTransaction]:
    """The household's series of one merchant."""
    return [row for row in home.series() if row.merchant_key == key]


def _one(home: Home, key: str) -> ScheduledTransaction:
    found = _series(home, key)
    assert len(found) == 1, (key, [row.status for row in found])
    return found[0]


def _standing(row: ScheduledTransaction) -> tuple[str, str, bool]:
    return (row.status, row.status_source, row.active)


def _changes(home: Home, schedule_id: uuid.UUID) -> list[tuple[str, date]]:
    return [
        (event.kind, event.occurred_on)
        for event in home.changes()
        if event.schedule_id == schedule_id
    ]


def _decide(home: Home, schedule_id: uuid.UUID, decision: str, **who: Any) -> Any:
    return home.client(**who).post(f"{SUBS}/{schedule_id}/decision", json={"decision": decision})


def _groceries(days: Sequence[date]) -> list[Line]:
    """A shop now and then, never the same amount: rows that are no series."""
    return [
        Line(day, -(D("41.07") + D("3.31") * (index % 7) + index), KROGER)
        for index, day in enumerate(days)
    ]


# ---------------------------------------------------------------------------
# Review focus 4
# ---------------------------------------------------------------------------


def test_review_focus_4_a_subscription_with_a_late_statement(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    today = [date(2026, 1, 5)]
    monkeypatch.setattr(recurring_service, "today", lambda: today[0])
    home.channel()
    account = home.ledger.account(home.household_id, currency="USD")
    client = home.client()

    # A year of statements: four services, twelve charges each.
    year: list[Line] = []
    for month in range(1, 13):
        year += [
            Line(date(2025, month, 4), "-10.99", SPOTIFY),
            Line(date(2025, month, 9), "-13.99", DISNEY),
            Line(date(2025, month, 12), "-15.49", NETFLIX),
            Line(date(2025, month, 22), "-7.99", HULU),
        ]
    year += _groceries([date(2025, month, 27) for month in range(1, 13)])
    home.upload(account, year, "2025.csv")
    found = ok(client.post(f"{API}/recurring/detect", json={}))
    assert found["created"] == 4
    stops, dismissed, ended, runs_on = (
        _one(home, key) for key in ("netflix", "spotify", "hulu", "disney plus")
    )
    for series in (stops, dismissed, ended, runs_on):
        assert (series.kind, series.occurrences) == ("subscription", 12)
        assert _standing(series) == ("active", "detector", True)

    # A person says: this one is not a subscription, and that one I cancelled.
    ok(_decide(home, dismissed.id, "dismiss"))
    ok(_decide(home, ended.id, "end"))
    assert _one(home, "hulu").ended_on == today[0]

    # Three months go by and nobody uploads a statement. The nightly job runs.
    today[0] = date(2026, 4, 5)
    home.detect(today[0])
    assert _standing(_one(home, "netflix")) == ("active", "detector", True)
    assert home.changes() == []
    assert home.alerts() == []
    listed = {item["id"]: item for item in ok(client.get(SUBS))["items"]}
    assert listed[str(stops.id)]["status"] == "active"
    assert listed[str(stops.id)]["overdue"] is False
    assert ok(client.get(f"{SUBS}/events"))["open"] == 0

    # The next statement arrives. The first service is not on it, twice over;
    # the others charged as before.
    late = [
        Line(date(2026, 1, 4), "-10.99", SPOTIFY),
        Line(date(2026, 2, 4), "-10.99", SPOTIFY),
        Line(date(2026, 3, 4), "-10.99", SPOTIFY),
        Line(date(2026, 1, 9), "-13.99", DISNEY),
        Line(date(2026, 2, 9), "-13.99", DISNEY),
        Line(date(2026, 3, 9), "-13.99", DISNEY),
        Line(date(2026, 1, 22), "-7.99", HULU),
        Line(date(2026, 2, 22), "-7.99", HULU),
        *_groceries([date(2026, 1, 27), date(2026, 2, 27), date(2026, 3, 20)]),
    ]
    home.upload(account, late, "2026-q1.csv")
    home.detect(today[0])

    # It ended, on its last charge, and that is said once.
    gone = _one(home, "netflix")
    assert _standing(gone) == ("ended", "detector", False)
    assert gone.ended_on == date(2025, 12, 12)
    assert [kind for kind, _ in _changes(home, gone.id)] == ["ended"]
    assert home.alerts().count("subscription.ended") == 1
    assert "subscription.missed" not in home.alerts()
    # The dismissed one stayed dismissed: no second series, no charge linked, no word.
    still = _one(home, "spotify")
    assert _standing(still) == ("dismissed", "person", False)
    assert _changes(home, still.id) == []
    assert {row.scheduled_transaction_id for row in home.named(account, SPOTIFY)} == {None}
    # The one a person ended stayed ended, and its charges since are reported.
    cancelled = _one(home, "hulu")
    assert _standing(cancelled) == ("ended", "person", False)
    assert cancelled.ended_on == date(2026, 1, 5)
    assert _changes(home, cancelled.id) == [
        ("charged_after_end", date(2026, 1, 22)),
        ("charged_after_end", date(2026, 2, 22)),
    ]
    assert home.alerts().count("subscription.charged_after_end") == 2
    # The fourth just runs on.
    assert _standing(_one(home, "disney plus")) == ("active", "detector", True)
    assert _changes(home, runs_on.id) == []
    assert len(home.series()) == 4

    # The same night again changes nothing and says nothing twice.
    said, alerted = home.changes(), home.alerts()
    home.detect(today[0])
    assert [event.id for event in home.changes()] == [event.id for event in said]
    assert home.alerts() == alerted
    totals = ok(client.get(SUBS))
    assert totals["subscriptions"]["count"] == 1
    assert totals["subscriptions"]["monthly"] == {"amount": "13.99", "currency": "USD"}


# ---------------------------------------------------------------------------
# Review focus 5
# ---------------------------------------------------------------------------


def _monthly(text: str, amount: str, day: int) -> list[Line]:
    """Two charges, a month apart, the first five weeks before the account's latest row."""
    return [Line(date(2026, 2, day), amount, text), Line(date(2026, 3, day), amount, text)]


def test_review_focus_5_a_member_and_accounts_they_cannot_see(
    home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(recurring_service, "today", lambda: date(2026, 4, 25))
    hid = home.household_id
    mine = home.ledger.account(hid, name="Mine", currency="USD")
    hidden = home.ledger.account(hid, name="Hidden", currency="USD")
    member = home.person("member")
    home.ledger.grant(hid, member.id, mine, GrantLevel.EDIT)
    owner_client, member_client = home.client(), home.client(member)

    coffees = [date(2026, 2, 3), date(2026, 2, 17), date(2026, 3, 6)]
    home.upload(
        mine,
        [
            *_monthly(NETFLIX, "-15.49", 12),
            *(Line(day, "-5.45", STARBUCKS) for day in coffees),
            Line(date(2026, 3, 2), "-63.40", TOOL_HIRE),
            *_groceries([date(2026, 3, 20)]),
        ],
        "mine.csv",
    )
    home.upload(
        hidden,
        [
            *_monthly(SPOTIFY, "-10.99", 4),
            *(Line(day, "-4.95", STARBUCKS) for day in [*coffees, date(2026, 3, 14)]),
            Line(date(2026, 2, 21), "-88.00", YARD),
            Line(date(2026, 3, 11), "-19.50", YARD),
            *_groceries([date(2026, 3, 20)]),
        ],
        "hidden.csv",
    )
    # Two charges of a known service: found, announced as new, and only a
    # suggestion until the third.
    home.detect(date(2026, 3, 25))
    theirs, others = _one(home, "netflix"), _one(home, "spotify")
    assert (theirs.account_id, others.account_id) == (mine.id, hidden.id)
    for series in (theirs, others):
        assert (series.kind, *_standing(series)) == ("subscription", "suggested", "detector", False)
        assert [kind for kind, _ in _changes(home, series.id)] == ["new"]
    home.upload(
        mine,
        [Line(date(2026, 4, 12), "-15.49", NETFLIX), *_groceries([date(2026, 4, 20)])],
        "mine-april.csv",
    )
    home.upload(
        hidden,
        [Line(date(2026, 4, 4), "-10.99", SPOTIFY), *_groceries([date(2026, 4, 21)])],
        "hidden-april.csv",
    )
    home.detect(date(2026, 4, 25))
    for key in ("netflix", "spotify"):
        assert _standing(_one(home, key)) == ("active", "detector", True)
    assert len(home.changes()) == 2
    # Each of them taught PerFin one merchant.
    shed = home.ledger.category(hid, name="Garden and shed")
    home.choose(home.named(mine, TOOL_HIRE)[0].id, shed.id, by=member)
    home.choose(home.named(hidden, YARD)[0].id, shed.id)

    # The preview counts the merchant's rows on the member's account only.
    treats = home.ledger.category(hid, name="Treats")
    source_row, *siblings = home.named(mine, STARBUCKS)
    body = {"transaction_id": str(source_row.id), "category_id": str(treats.id)}
    preview = ok(member_client.post(f"{API}/rules/merchant/preview", json=body))
    counted = sum(
        preview[name] for name in ("uncategorised", "guessed", "decided", "same_category")
    )
    assert (counted, preview["guessed"]) == (3, 3)
    assert set(preview["sample"]) <= {str(row.id) for row in home.rows(mine)}

    # "Apply to existing" changes rows on that account and no other.
    unseen = {
        row.id: (row.category_id, row.category_source, row.updated_at) for row in home.rows(hidden)
    }
    made = ok(
        member_client.post(f"{API}/rules/merchant", json={**body, "apply_to_existing": True}), 201
    )
    assert home.run_queued(RULES_JOB) == 1
    assert [home.row(row.id).category_source for row in siblings] == ["rule", "rule"]
    assert home.row(source_row.id).category_source == "person"
    assert {
        row.id: (row.category_id, row.category_source, row.updated_at) for row in home.rows(hidden)
    } == unseen
    # Nor does the member's list show rows of the merchant on the other account.
    seen = ok(member_client.get(f"{API}/transactions", params={"merchant": "starbucks"}))
    assert {item["account_id"] for item in seen["items"]} == {str(mine.id)}
    assert len(seen["items"]) == 3

    # What was learned: a merchant that is only on the other account is not shown.
    def learned(client: Any) -> list[str]:
        page = ok(client.get(f"{API}/categorisation/learned"))
        return sorted(item["merchant_key"] for item in page["items"])

    assert learned(member_client) == ["holloway tool hire", "starbucks"]
    assert learned(owner_client) == ["holloway tool hire", "ostler yard supply", "starbucks"]
    summary = ok(member_client.get(f"{API}/categorisation/summary"))
    assert summary["learned_merchants"] == 2
    assert sum(count["transactions"] for count in summary["by_source"]) == len(home.rows(mine))

    # Subscriptions: the list, what they cost, and what changed.
    for client, ids, monthly in (
        (member_client, {str(theirs.id)}, "15.49"),
        (owner_client, {str(theirs.id), str(others.id)}, "26.48"),
    ):
        listed = ok(client.get(SUBS))
        assert {item["id"] for item in listed["items"]} == ids
        assert listed["subscriptions"]["count"] == len(ids), listed
        assert listed["subscriptions"]["monthly"]["amount"] == monthly
        assert listed["needs_look"] == len(ids)
        events = ok(client.get(f"{SUBS}/events"))
        assert {event["schedule_id"] for event in events["items"]} == ids
        assert events["open"] == len(ids)
    # Marking a change on the other account as seen does nothing.
    foreign_event = next(event.id for event in home.changes() if event.schedule_id == others.id)
    ok(member_client.post(f"{SUBS}/events/seen", json={"ids": [str(foreign_event)]}))
    assert {event.id: event.state for event in home.changes()}[foreign_event] == "open"
    # A series on it does not exist for the member.
    for response in (
        member_client.get(f"{SUBS}/{others.id}"),
        _decide(home, others.id, "dismiss", record=member),
    ):
        assert (response.status_code, problem_type(response)) == (404, "schedule-not-found")
    assert _standing(_one(home, "spotify")) == ("active", "detector", True)

    # Another household's rule, learned merchant and series are not there at all.
    stranger = home.factories.user(role="owner", with_totp=True)
    assert stranger.household_id is not None
    far = stranger.household_id
    home.ledger.seed_categories(far)
    far_account = home.ledger.account(far, currency="USD")
    far_client = home.client(stranger)
    far_category = home.ledger.category(far, name="Evenings")
    row = ok(
        far_client.post(
            f"{API}/transactions",
            json={
                "account_id": str(far_account.id),
                "posted_date": "2026-03-12",
                "amount": {"amount": "-15.49", "currency": "USD"},
                "raw_payee": NETFLIX,
                "status": "cleared",
            },
        ),
        201,
    )
    ok(
        far_client.patch(
            f"{API}/transactions/{row['id']}", json={"category_id": str(far_category.id)}
        )
    )
    far_memory = ok(far_client.get(f"{API}/categorisation/learned"))["items"][0]["id"]
    far_rule = ok(
        far_client.post(
            f"{API}/rules/merchant",
            json={"transaction_id": row["id"], "category_id": str(far_category.id)},
        ),
        201,
    )["rule"]["id"]
    far_series = home.ledger.run(make_scheduled_transaction, far_account).id
    for client in (member_client, owner_client):
        refused = [
            (client.post(f"{API}/rules/{far_rule}/revert"), "rule-not-found"),
            (client.delete(f"{API}/categorisation/learned/{far_memory}"), "memory-not-found"),
            (client.get(f"{SUBS}/{far_series}"), "schedule-not-found"),
            (
                client.post(f"{SUBS}/{far_series}/decision", json={"decision": "dismiss"}),
                "schedule-not-found",
            ),
        ]
        for response, problem in refused:
            assert (response.status_code, problem_type(response)) == (404, problem)
    # All three are still there for the household they belong to.
    assert ok(far_client.get(f"{API}/rules/{far_rule}"))["id"] == far_rule
    assert [
        item["id"] for item in ok(far_client.get(f"{API}/categorisation/learned"))["items"]
    ] == [far_memory]
    assert made["rule"]["id"] != far_rule


# ---------------------------------------------------------------------------
# Two years, a month at a time
# ---------------------------------------------------------------------------

#: The planted series a person dismisses a few months in; its price changes later.
DISMISSED = "netflix"
#: The months imported before that happens.
DISMISSED_AFTER = 6


def _months(first: date, last: date) -> list[tuple[int, int]]:
    out = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append((year, month))
        year, month = (year, month + 1) if month < 12 else (year + 1, 1)
    return out


def test_two_years_of_statements_month_by_month(home: Home) -> None:
    ledger = synth.build()
    hid = home.household_id
    accounts: dict[str, Account] = {
        name: home.ledger.account(hid, name=name.title(), currency="USD")
        for name in ledger.coverage
    }
    name_of = {account.id: name for name, account in accounts.items()}
    owner_of = {index: name for name, indexes in ledger.members.items() for index in indexes}
    # Ledger rows that wait to be matched to an imported row: by account and line.
    waiting: dict[tuple[str, date, Decimal, str], list[int]] = defaultdict(list)
    by_month: dict[tuple[int, int, str], list[int]] = defaultdict(list)
    for index, planted in enumerate(ledger.rows):
        by_month[(planted.posted.year, planted.posted.month, planted.account)].append(index)
    index_of: dict[uuid.UUID, int] = {}

    def found() -> tuple[list[FoundSeries], dict[uuid.UUID, str | None]]:
        """The detected series in the ledger's terms, and which planted series or decoy each is."""
        linked: dict[uuid.UUID, set[int]] = defaultdict(set)
        charges = home.query(
            select(Transaction).where(
                Transaction.household_id == hid, Transaction.scheduled_transaction_id.is_not(None)
            )
        )
        for charge in charges:
            linked[charge.scheduled_transaction_id].add(index_of[charge.id])
        prices: dict[uuid.UUID, list[date]] = defaultdict(list)
        for event in home.changes():
            if event.kind == "price_changed":
                prices[event.schedule_id].append(event.occurred_on)
        series, owners = [], {}
        for row in home.series():
            rows = frozenset(linked[row.id])
            series.append(
                FoundSeries(
                    account=name_of[row.account_id],
                    rows=rows,
                    status=row.status,
                    kind=row.kind,
                    price_changes=tuple(prices[row.id]),
                )
            )
            counts = Counter(owner_of[index] for index in rows if index in owner_of)
            top = counts.most_common(1)
            owners[row.id] = top[0][0] if top and 2 * top[0][1] > len(rows) else None
        return series, owners

    announced: dict[uuid.UUID, date] = {}
    #: Each account's data clock: the day of its latest imported row.
    clock: dict[uuid.UUID, date] = {}
    dismissed_id: uuid.UUID | None = None
    months = _months(synth.START, synth.TODAY)
    for number, (year, month) in enumerate(months, start=1):
        month_end = date(year, month, calendar.monthrange(year, month)[1])
        for name, account in accounts.items():
            indexes = by_month.get((year, month, name), [])
            if not indexes:
                continue
            lines = [
                Line(ledger.rows[index].posted, ledger.rows[index].amount, ledger.rows[index].text)
                for index in indexes
            ]
            for index, line in zip(indexes, lines, strict=True):
                waiting[(name, line.posted, D(line.amount), squash(line.text))].append(index)
            clock[account.id] = max(line.posted for line in lines)
            document_id, summary = home.upload(account, lines, f"{name}-{year}-{month:02d}.csv")
            assert summary["imported"] == len(lines), (name, year, month, summary)
            imported: list[Transaction] = home.query(
                select(Transaction).where(Transaction.source_document_id == document_id)
            )
            for stored in imported:
                index_of[stored.id] = waiting[
                    (name, stored.posted_date, stored.amount, squash(stored.raw_payee))
                ].pop()
        # The nightly job on the last day of the month.
        home.detect(month_end)

        series, owners = found()
        metrics = subscription_metrics(series, ledger)
        # A habit, a transfer or a payment is never shown as a subscription.
        assert metrics.decoys_active == 0, (year, month, metrics.misses.get("decoys_active"))
        # A series is announced as new once, and only while it is new.
        by_id = {row.id: row for row in home.series()}
        for event in home.changes():
            if event.kind != "new" or event.id in announced:
                continue
            announced[event.id] = event.occurred_on
            started = by_id[event.schedule_id].first_seen_on
            assert started == event.occurred_on
            age = (clock[event.account_id] - started).days
            assert 0 <= age <= NEW_WINDOW_DAYS, (year, month, owners[event.schedule_id], age)
            assert started >= synth.START
        new_events = [event for event in home.changes() if event.kind == "new"]
        assert len({event.schedule_id for event in new_events}) == len(new_events)

        if number == DISMISSED_AFTER:
            mine = [row_id for row_id, owner in owners.items() if owner == DISMISSED]
            assert len(mine) == 1
            dismissed_id = mine[0]
            assert by_id[dismissed_id].status == "active"
            ok(home.client().post(f"{SUBS}/{dismissed_id}/decision", json={"decision": "dismiss"}))
        elif dismissed_id is not None:
            # It stays dismissed, and its later charges make no second series.
            tombstone = by_id[dismissed_id]
            assert (tombstone.status, tombstone.status_source) == ("dismissed", "person")
            assert DISMISSED not in set(owners.values()), (year, month)

    assert dismissed_id is not None
    series, owners = found()
    metrics = subscription_metrics(series, ledger)
    # The run was not empty: most of what was planted was found, and announced when new.
    assert metrics.found >= 0.75, metrics.misses.get("found")
    assert len(announced) >= 10
    # The joint account was last uploaded in July: nothing on it is missed or ended for that.
    joint = accounts["joint"].id
    assert [
        row.status for row in home.series() if row.account_id == joint and row.status == "ended"
    ] == []
    assert [
        event.kind
        for event in home.changes()
        if event.account_id == joint and event.kind in ("missed", "ended")
    ] == []
    # No change was reported about the dismissed series after the person's word.
    assert [
        event.kind
        for event in home.changes()
        if event.schedule_id == dismissed_id and event.occurred_on > date(2025, 4, 30)
    ] == []
