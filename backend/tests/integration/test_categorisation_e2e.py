# SPDX-License-Identifier: AGPL-3.0-or-later
"""Categorisation, end to end: imports, corrections, rules, re-reads (design note, sections 2 to 5).

Nothing is stubbed. Files go through the real upload and ingest job, the
clean-up, the bundled dictionary, the engine and the rules are the shipped
ones, people act over HTTP and background work runs through its job handler
(``tests/fixtures/pipeline.py``). Three tests are the first three scenarios
of the plan's "Review Focus"; the fourth and fifth are in
``test_subscriptions_e2e.py``.

Merchant names that the dictionary knows are real public brands, spelled as
banks print them; every store number, town, reference, amount, local shop
and person is invented, and no line comes from a statement.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select

from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.transactions.models import Category, CategoryKind, Rule, Transaction
from tests.fixtures.descriptors import LABELLED, Labelled
from tests.fixtures.factories import Factories
from tests.fixtures.households import API
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.pipeline import (
    DISPATCH_JOB,
    REFRESH_JOB,
    RULES_JOB,
    Home,
    Line,
    ok,
    open_home,
    reading,
    squash,
)

pytestmark = pytest.mark.integration

D = Decimal

STARBUCKS = "STARBUCKS STORE 08213 ASHGROVE OH"
STARBUCKS_2 = "STARBUCKS STORE 01177 PINE HOLLOW OH"
AMAZON = "AMZN Mktp US*MB5ZH9ZC"
AMAZON_2 = "AMZN Mktp US*QT4ZL2KD"
AMAZON_3 = "AMZN Mktp US*7HN2RD0P"
KROGER = "KROGER #0412 ASHGROVE OH"
SHELL = "SHELL OIL 57442190041 ASHGROVE OH"
MCDONALDS = "MCDONALD'S F12841 ASHGROVE OH"
#: Local shops no dictionary knows and no word gives away.
BRAMBLE = "BRAMBLE AND FINCH"
TOOL_HIRE = "HOLLOWAY TOOL HIRE"


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


def march(day: int, amount: str, text: str) -> Line:
    return Line(date(2026, 3, day), amount, text)


def april(day: int, amount: str, text: str) -> Line:
    return Line(date(2026, 4, day), amount, text)


def _decided(row: Transaction) -> tuple[Any, ...]:
    """What a row's categorisation says, and why."""
    return (
        row.category_id,
        row.category_source,
        row.category_confidence,
        row.suggested_category_id,
        row.category_rule_id,
    )


def _snapshot(home: Home) -> dict[uuid.UUID, tuple[Any, ...]]:
    """Every row of the household: its categorisation and when it was last touched."""
    return {row.id: (*_decided(row), row.updated_at) for row in home.rows()}


def _untouched(rows: Sequence[Transaction]) -> bool:
    return all(row.updated_at == row.created_at for row in rows)


def _refresh(home: Home) -> None:
    """An admin asks for a second look, and the job runs."""
    ok(home.client().post(f"{API}/categorisation/refresh"), 202)
    assert home.run_queued(REFRESH_JOB) == 1


def _rules(home: Home) -> list[Rule]:
    found: list[Rule] = home.query(select(Rule).where(Rule.household_id == home.household_id))
    return found


# ---------------------------------------------------------------------------
# Review focus 1
# ---------------------------------------------------------------------------


def test_review_focus_1_a_re_read_after_categorisation(home: Home) -> None:
    # A household of the release before: the first edition of the default
    # categories (no "fast food" yet), never looked at by a dictionary.
    with home.factories.transaction() as db:
        set_context(db, home.household_id)
        db.execute(
            delete(Category).where(
                Category.household_id == home.household_id,
                Category.system_key == "food.fast_food",
            )
        )
    home.set_state(taxonomy=1)
    account = home.ledger.account(home.household_id, number_last4="6621", currency="USD")
    lines = [
        march(2, "-9.87", MCDONALDS),
        march(5, "-64.20", KROGER),
        march(9, "-5.45", STARBUCKS),
        march(12, "-31.18", AMAZON),
        march(17, "-21.00", BRAMBLE),
        march(24, "-40.11", SHELL),
    ]

    document_id, summary = home.statement(reading("6621", lines))

    # The engine guessed: four facts, one suggestion, one row nobody can name.
    assert summary["imported"] == len(lines)
    assert summary["categorised"] == {"dictionary": 4}
    assert summary["suggested"] == 1
    food = home.category("food")
    burger = home.named(account, MCDONALDS)[0]
    # Without the leaf, the group answers.
    assert _decided(burger)[:3] == (food.id, "dictionary", D("0.90"))
    assert _untouched(home.rows(account))

    # A person changes one category.
    own = home.ledger.category(home.household_id, name="Client meetings")
    coffee = home.named(account, STARBUCKS)[0]
    home.choose(coffee.id, own.id)

    # The hourly dispatcher finds the household behind this release's dictionary.
    home.run_now(DISPATCH_JOB, {}, instance=True)
    assert home.run_queued(REFRESH_JOB) == 1
    refreshed = home.audits("categorise.refresh")
    assert len(refreshed) == 1
    counts = refreshed[0].after or {}
    assert (counts["categories_added"], counts["forced"]) == (1, False)
    assert counts["changed"] >= 1
    assert ok(home.client().get(f"{API}/categorisation/summary"))["up_to_date"] is True
    # The refresh moved a guess, and still nothing looks touched but the person's row.
    fast_food = home.category("food.fast_food")
    burger = home.row(burger.id)
    assert _decided(burger)[:3] == (fast_food.id, "dictionary", D("0.90"))
    rows = home.rows(account)
    assert _untouched([row for row in rows if row.id != coffee.id])
    assert _decided(home.row(coffee.id))[:3] == (own.id, "person", D("1.00"))
    # A second dispatch has nothing to queue.
    home.run_now(DISPATCH_JOB, {}, instance=True)
    assert home.queued(REFRESH_JOB) == 0

    # The document is read again, and the new reading holds two lines only.
    kept_lines = [line for line in lines if line.text in (BRAMBLE, SHELL)]
    summary = home.read(document_id, reading("6621", kept_lines))

    # Rows only the engine wrote are gone, whether the guess came at the
    # import, from the refresh, or was only a suggestion; the person's row
    # is kept and listed, and it is the only one listed.
    after = {squash(row.raw_payee): row for row in home.rows(account)}
    assert set(after) == {BRAMBLE, SHELL, STARBUCKS}
    assert (summary["replaced_rows"], summary["kept_rows"], summary["imported"]) == (3, 1, 0)
    assert after[STARBUCKS].id == coffee.id
    assert _decided(after[STARBUCKS])[:2] == (own.id, "person")
    assert home.kept_items(document_id) == [
        {
            "reason": "earlier_rows_kept",
            "account_id": str(account.id),
            "count": 1,
            "transactions": [{"transaction_id": str(coffee.id)}],
        }
    ]


# ---------------------------------------------------------------------------
# Review focus 2
# ---------------------------------------------------------------------------


def test_review_focus_2_a_person_s_category_under_every_later_pass(home: Home) -> None:
    account = home.ledger.account(home.household_id, currency="USD")
    lines = [
        march(3, "-5.45", STARBUCKS),
        march(5, "-4.95", STARBUCKS),
        march(8, "-31.18", AMAZON),
        march(11, "-6.10", STARBUCKS_2),
        march(14, "-3.75", STARBUCKS_2),
        march(16, "-12.40", AMAZON_2),
        march(20, "-21.00", BRAMBLE),
    ]
    home.upload(account, lines, "march.csv")
    by_hand, cleared = home.named(account, STARBUCKS)
    accepted = home.named(account, AMAZON)[0]
    source_row, sibling = home.named(account, STARBUCKS_2)
    offered = home.named(account, AMAZON_2)[0]
    coffee, general = home.category("food.coffee"), home.category("shopping.general")
    assert _decided(by_hand)[:2] == _decided(cleared)[:2] == (coffee.id, "dictionary")
    assert (accepted.category_id, accepted.suggested_category_id) == (None, general.id)

    # Three ways a person decides: a category set by hand, a category
    # cleared by hand, a suggestion accepted.
    client = home.client()
    meetings = home.ledger.category(home.household_id, name="Client meetings")
    home.choose(by_hand.id, meetings.id)
    home.choose(cleared.id, None)
    result = ok(
        client.post(
            f"{API}/transactions/bulk",
            json={"action": "accept_suggestions", "ids": [str(accepted.id)]},
        )
    )
    assert (result["changed"], [item["result"] for item in result["results"]]) == (1, ["updated"])
    mine = (by_hand.id, cleared.id, accepted.id)

    def theirs() -> list[tuple[Any, ...]]:
        return [(*_decided(home.row(row_id)), home.row(row_id).updated_at) for row_id in mine]

    kept = theirs()
    assert [(row[0], row[1], row[3]) for row in kept] == [
        (meetings.id, "person", None),
        (None, "person", None),
        (general.id, "person", None),
    ]

    # 1. The same lines again, in a later export that has one more.
    later = march(27, "-5.25", "QUARRYSIDE KIOSK")
    _, summary = home.upload(account, [*lines, later], "march-again.csv")
    assert (summary["imported"], summary["duplicates"]) == (1, len(lines))
    assert theirs() == kept

    # 2. A second look at everything nobody decided.
    _refresh(home)
    assert theirs() == kept

    # 3. A rule that matches the accepted row, re-applied on request.
    hobby = home.ledger.category(home.household_id, name="Model railway")
    ok(
        client.post(
            f"{API}/rules",
            json={
                "name": "Marketplace orders",
                "conditions": {"type": "payee", "op": "contains", "value": "amzn"},
                "actions": [{"type": "set_category", "category_id": str(hobby.id)}],
            },
        ),
        201,
    )
    ok(client.post(f"{API}/rules/apply", json={}), 202)
    assert home.run_queued(RULES_JOB) == 1
    # It ran and it matched: the other marketplace row is the rule's now.
    assert _decided(home.row(offered.id))[:2] == (hobby.id, "rule")
    assert theirs() == kept

    # 4. "Always categorise this merchant as...", applied to existing rows.
    treats = home.ledger.category(home.household_id, name="Treats")
    made = ok(
        client.post(
            f"{API}/rules/merchant",
            json={
                "transaction_id": str(source_row.id),
                "category_id": str(treats.id),
                "apply_to_existing": True,
            },
        ),
        201,
    )
    assert made["job_id"] is not None
    assert home.run_queued(RULES_JOB) == 1
    rule_id = made["rule"]["id"]
    assert _decided(home.row(source_row.id))[:2] == (treats.id, "person")
    assert _decided(home.row(sibling.id)) == (
        treats.id,
        "rule",
        D("1.00"),
        None,
        uuid.UUID(rule_id),
    )
    assert theirs() == kept

    # 5. The undo of that rule, and the second look it queues.
    reverted = ok(client.post(f"{API}/rules/{rule_id}/revert"))
    assert reverted["reverted"] == 1
    assert home.run_queued(REFRESH_JOB) == 1
    assert home.row(sibling.id).category_source != "rule"
    assert theirs() == kept


# ---------------------------------------------------------------------------
# Review focus 3
# ---------------------------------------------------------------------------


def test_review_focus_3_a_mixed_store_and_a_split_household(home: Home) -> None:
    account = home.ledger.account(home.household_id, currency="USD")
    mixed = [
        march(2, "-31.18", AMAZON),
        march(4, "-58.02", "TARGET 00012841 ASHGROVE OH"),
        march(6, "-142.77", "COSTCO WHSE #0388 PINE HOLLOW OH"),
        march(7, "-23.64", "WALMART SUPERCENTER #1123 ASHGROVE OH"),
    ]
    fuel_stops = [
        march(9, "-40.11", SHELL),
        march(13, "-38.20", SHELL),
        march(18, "-44.75", SHELL),
    ]
    _, summary = home.upload(
        account, [*mixed, *fuel_stops, march(21, "-64.20", KROGER)], "march.csv"
    )
    general, fuel = home.category("shopping.general"), home.category("transport.fuel")

    # Stores that sell everything arrive as suggestions, never as facts.
    assert summary["suggested"] == len(mixed)
    assert summary["categorised"] == {"dictionary": 4}
    for line in mixed:
        row = home.named(account, line.text)[0]
        assert _decided(row) == (None, "dictionary", D("0.60"), general.id, None), line.text
    client = home.client()
    counted = ok(client.get(f"{API}/categorisation/summary"))
    assert (counted["suggestions"], counted["uncategorised"]) == (4, 4)
    listed = ok(client.get(f"{API}/transactions", params={"suggested": "true"}))
    assert len(listed["items"]) == len(mixed)
    # A suggestion is in no category: nothing is filed under it.
    filed = ok(client.get(f"{API}/transactions", params={"category": str(general.id)}))
    assert filed["items"] == []

    # The household files one merchant under two categories.
    first, second, third = home.named(account, SHELL)
    assert _decided(third)[:3] == (fuel.id, "dictionary", D("0.90"))
    travel = home.ledger.category(home.household_id, name="Work travel")
    bike = home.ledger.category(home.household_id, name="Motorbike")
    home.choose(first.id, travel.id)
    home.choose(second.id, bike.id)

    # Next month: neither what was learned nor the dictionary states a fact.
    _, summary = home.upload(
        account, [april(3, "-41.90", SHELL), april(10, "-18.25", AMAZON_2)], "april.csv"
    )
    assert summary["categorised"] == {}
    assert summary["suggested"] == 2
    newest = home.named(account, SHELL)[-1]
    assert _decided(newest) == (None, "dictionary", D("0.60"), fuel.id, None)
    assert newest.updated_at == newest.created_at
    # A second look takes back the fact the dictionary stated before they disagreed.
    _refresh(home)
    third = home.row(third.id)
    assert _decided(third) == (None, "dictionary", D("0.60"), fuel.id, None)
    assert third.updated_at == third.created_at
    undecided = [row for row in home.named(account, SHELL) if row.category_source != "person"]
    assert [row.category_id for row in undecided] == [None, None]
    assert _decided(home.row(first.id))[:2] == (travel.id, "person")
    assert _decided(home.row(second.id))[:2] == (bike.id, "person")


# ---------------------------------------------------------------------------
# A first import
# ---------------------------------------------------------------------------

PER_GROUP = 20


def _sample(group: str) -> list[Labelled]:
    """Twenty lines of one group of the labelled set, evenly spread over it."""
    lines = [line for line in LABELLED if line.group == group]
    return lines[:: len(lines) // PER_GROUP][:PER_GROUP]


def test_a_new_household_s_first_csv(home: Home) -> None:
    account = home.ledger.account(home.household_id, currency="USD")
    labelled = [*_sample("known"), *_sample("invented"), *_sample("non_purchase")]
    assert len({squash(line.text) for line in labelled}) == 3 * PER_GROUP
    start = date(2026, 3, 1)
    lines = [
        Line(start + timedelta(days=index % 28), line.sign * (D("7.25") + index), line.text)
        for index, line in enumerate(labelled)
    ]

    _, summary = home.upload(account, lines, "first.csv")

    assert summary["imported"] == len(lines)
    rows = {squash(row.raw_payee): row for row in home.rows(account)}
    assert len(rows) == len(lines)
    categories = home.categories()

    def fact(line: Labelled) -> str | None:
        """The default key of the category stated for ``line``, if one was."""
        category_id = rows[squash(line.text)].category_id
        if category_id is None:
            return None
        key = categories[category_id].system_key
        assert key is not None
        return key

    known = [line for line in labelled if line.group == "known"]
    right = [line.text for line in known if line.category and fact(line) == line.category]
    assert len(right) >= 15, sorted({line.text for line in known} - set(right))

    # An invented shop is given a category only if it is the one its name says.
    wrong = [
        (line.text, fact(line))
        for line in labelled
        if line.group == "invented" and fact(line) not in (None, line.category)
    ]
    assert wrong == []

    # Money that is not spending is never filed as spending: what a transfer,
    # a card payment, a salary or a deposit gets is not an expense category
    # (a bank fee and interest paid are expenses, and are what the label says).
    misfiled = []
    for line in labelled:
        row = rows[squash(line.text)]
        if line.group != "non_purchase" or row.category_id is None:
            continue
        category = categories[row.category_id]
        if category.kind is CategoryKind.EXPENSE and category.system_key != line.category:
            misfiled.append((line.text, category.system_key))
    assert misfiled == []
    # Every row says how it was decided, and none looks touched.
    assert all(row.category_source is not None for row in rows.values() if row.category_id)
    assert _untouched(list(rows.values()))


# ---------------------------------------------------------------------------
# Teaching
# ---------------------------------------------------------------------------


def test_teach_once_then_import_next_month(home: Home) -> None:
    account = home.ledger.account(home.household_id, currency="USD")
    between_people = "ZELLE PAYMENT TO DESMOND KILLIGREW 9WZ0XDUL001"
    home.upload(
        account,
        [
            march(2, "-63.40", TOOL_HIRE),
            march(5, "-5.45", STARBUCKS),
            march(9, "-31.18", AMAZON),
            march(12, "-925.00", between_people),
            march(20, "-64.20", KROGER),
        ],
        "march.csv",
    )
    shed = home.ledger.category(home.household_id, name="Garden and shed")
    meetings = home.ledger.category(home.household_id, name="Client meetings")
    books = home.ledger.category(home.household_id, name="Books")
    rent = home.category("housing.rent")
    hire = home.named(account, TOOL_HIRE)[0]
    assert _decided(hire) == (None, None, None, None, None)
    # One correction each: a shop nobody knows, a merchant the dictionary
    # files elsewhere, a store that sells everything, a payment to a person.
    home.choose(hire.id, shed.id)
    home.choose(home.named(account, STARBUCKS)[0].id, meetings.id)
    home.choose(home.named(account, AMAZON)[0].id, books.id)
    home.choose(home.named(account, between_people)[0].id, rent.id)

    to_another = "ZELLE PAYMENT TO MARTA LINDQVIST 4HT7QPLC552"
    _, summary = home.upload(
        account,
        [
            april(3, "-71.15", f"{TOOL_HIRE} 0042"),
            april(6, "-6.10", STARBUCKS_2),
            april(10, "-18.25", AMAZON_2),
            april(12, "-60.00", to_another),
            april(21, "-58.90", KROGER),
        ],
        "april.csv",
    )

    groceries = home.category("food.groceries")
    assert summary["categorised"] == {"learned": 3, "dictionary": 1}
    assert summary["suggested"] == 0
    learned = {
        f"{TOOL_HIRE} 0042": shed.id,
        STARBUCKS_2: meetings.id,
        AMAZON_2: books.id,
    }
    for text, category_id in learned.items():
        row = home.named(account, text)[0]
        assert _decided(row) == (category_id, "learned", D("0.80"), None, None), text
        assert row.updated_at == row.created_at
    assert _decided(home.named(account, KROGER)[-1])[:2] == (groceries.id, "dictionary")
    # Money sent to one person says nothing about money sent to another.
    other = home.named(account, to_another)[0]
    assert _decided(other) == (None, None, None, None, None)
    assert other.merchant_key == "zelle"
    assert [entry[0] for entry in home.memory()] == ["amazon", "holloway tool hire", "starbucks"]
    listed = ok(home.client().get(f"{API}/categorisation/learned"))
    assert sorted(item["merchant_key"] for item in listed["items"]) == [
        "amazon",
        "holloway tool hire",
        "starbucks",
    ]


def test_always_then_undo_leaves_the_ledger_as_it_was(home: Home) -> None:
    account = home.ledger.account(home.household_id, currency="USD")
    orders = [
        march(2, "-31.18", AMAZON),
        march(6, "-12.40", AMAZON_2),
        march(11, "-18.25", AMAZON_3),
        march(15, "-9.99", AMAZON_3),
        march(27, "-54.60", AMAZON_3),
    ]
    home.upload(
        account,
        [
            *orders,
            march(3, "-5.45", STARBUCKS),
            march(9, "-64.20", KROGER),
            march(17, "-21.00", BRAMBLE),
            march(24, "-40.11", SHELL),
        ],
        "march.csv",
    )
    # The household files this store under two things, so nothing was
    # learned for it and its other orders stay suggestions.
    books = home.ledger.category(home.household_id, name="Books")
    garden = home.ledger.category(home.household_id, name="Garden and shed")
    general = home.category("shopping.general")
    first = home.named(account, AMAZON)[0]
    second = home.named(account, AMAZON_2)[0]
    others = [row.id for row in home.named(account, AMAZON_3)]
    home.choose(first.id, books.id)
    home.choose(second.id, garden.id)
    _refresh(home)
    before = _snapshot(home)
    suggestion = (None, "dictionary", D("0.60"), general.id, None)
    assert [before[row_id][:5] for row_id in others] == [suggestion] * 3

    # "Always categorise this store as Books", existing rows included.
    client = home.client()
    body = {"transaction_id": str(first.id), "category_id": str(books.id)}
    preview = ok(client.post(f"{API}/rules/merchant/preview", json=body))
    assert (
        preview["uncategorised"],
        preview["guessed"],
        preview["decided"],
        preview["same_category"],
    ) == (3, 0, 1, 1)
    made = ok(client.post(f"{API}/rules/merchant", json={**body, "apply_to_existing": True}), 201)
    rule_id = uuid.UUID(made["rule"]["id"])
    assert home.run_queued(RULES_JOB) == 1
    during = _snapshot(home)
    # The job changed exactly what the preview counted.
    changed = {row_id for row_id in before if during[row_id] != before[row_id]}
    assert changed == set(others)
    assert [during[row_id][:5] for row_id in others] == [
        (books.id, "rule", D("1.00"), None, rule_id)
    ] * 3

    # Undo, and the second look it queues.
    reverted = ok(client.post(f"{API}/rules/{rule_id}/revert"))
    assert reverted["reverted"] == 3
    assert home.run_queued(REFRESH_JOB) == 1

    after = _snapshot(home)
    assert _rules(home) == []
    # Every category and the reason for it are what they were before the rule.
    assert {row_id: row[:5] for row_id, row in after.items()} == {
        row_id: row[:5] for row_id, row in before.items()
    }
    # No row the rule left alone was touched. The rows it categorised were
    # written by the apply job, which is a change a person asked for and
    # moves ``updated_at``; the undo and the second look did not move it again.
    assert {row_id: row for row_id, row in after.items() if row_id not in others} == {
        row_id: row for row_id, row in before.items() if row_id not in others
    }
    for row_id in others:
        assert after[row_id][5] == during[row_id][5] > before[row_id][5]
