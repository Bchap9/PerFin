# SPDX-License-Identifier: AGPL-3.0-or-later
"""Budgets over HTTP: CRUD, validation, roles and the period report.

The report is checked exactly (``Decimal`` strings) against the synthetic
ledger of ``tests/fixtures/report_ledger.py`` (actuals, rollover, unbudgeted
spending, visibility, custom and annual periods) and a small dedicated
ledger with scheduled transactions (forecast: linear projection of the
period so far plus scheduled items).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest
from sqlalchemy import update

from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.budgets import service as budgets_service
from app.domain.transactions.models import Cadence, CategoryKind, ScheduledTransaction
from tests.fixtures import report_ledger
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import (
    API,
    Actors,
    household_audit,
    household_events,
    problem_type,
)
from tests.fixtures.ledger import LedgerFactories, make_scheduled_transaction
from tests.fixtures.report_ledger import ReportLedger

pytestmark = pytest.mark.integration

BUDGETS = f"{API}/budgets"


@dataclass(frozen=True)
class World:
    owner: AuthedClient
    member: AuthedClient
    book: ReportLedger


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


@pytest.fixture
def world(actors: Actors, ledger: LedgerFactories) -> World:
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    book = report_ledger.build(ledger, household.id)
    report_ledger.grant_member(ledger, book, member.record.id)
    return World(owner, member, book)


def _freeze(monkeypatch: pytest.MonkeyPatch, day: date) -> None:
    monkeypatch.setattr(budgets_service, "today", lambda: day)


def _usd(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "USD"}


def _line(category: Any, amount: str) -> dict[str, Any]:
    return {"category_id": str(category.id), "amount": _usd(amount)}


def _create(client: AuthedClient, body: dict[str, Any]) -> dict[str, Any]:
    response = client.post(BUDGETS, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def _report(client: AuthedClient, budget_id: str, **params: Any) -> dict[str, Any]:
    response = client.get(f"{BUDGETS}/{budget_id}/report", params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _by_category(report: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        line["category_id"]: {
            k: line[k]["amount"]
            for k in ("budgeted", "carried_over", "available", "actual", "remaining", "forecast")
        }
        for line in report["lines"]
    }


def _monthly(book: ReportLedger, *, rollover: bool = False) -> dict[str, Any]:
    return {
        "name": "Household plan",
        "period": "monthly",
        "start_date": "2026-01-01",
        "rollover": rollover,
        "lines": [
            _line(book.groceries, "300"),
            _line(book.restaurants, "100.00"),
            _line(book.food, "50"),
            _line(book.rent, "1000"),
            _line(book.salary, "2500"),
        ],
    }


# ---- CRUD ----------------------------------------------------------------------------


def test_create_read_update_delete(world: World, factories: Factories) -> None:
    book = world.book
    created = _create(world.member, _monthly(book))
    assert created["currency"] == "USD"  # the household's base currency
    assert (created["period"], created["start_date"], created["end_date"]) == (
        "monthly",
        "2026-01-01",
        None,
    )
    assert {(line["category_id"], line["amount"]["amount"]) for line in created["lines"]} == {
        (str(book.groceries.id), "300.00"),
        (str(book.restaurants.id), "100.00"),
        (str(book.food.id), "50.00"),
        (str(book.rent.id), "1000.00"),
        (str(book.salary.id), "2500.00"),
    }
    url = f"{BUDGETS}/{created['id']}"
    assert world.owner.get(url).json() == created
    listed = world.owner.get(BUDGETS).json()
    assert [b["id"] for b in listed["items"]] == [created["id"]]

    old_lines = {line["category_id"]: line["id"] for line in created["lines"]}
    patched = world.member.patch(
        url,
        json={
            "name": "Renamed",
            "rollover": True,
            "lines": [_line(book.groceries, "320.50"), _line(book.housing, "10")],
        },
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert (body["name"], body["rollover"]) == ("Renamed", True)
    lines = {line["category_id"]: line for line in body["lines"]}
    assert set(lines) == {str(book.groceries.id), str(book.housing.id)}
    assert lines[str(book.groceries.id)]["amount"]["amount"] == "320.50"
    assert lines[str(book.groceries.id)]["id"] == old_lines[str(book.groceries.id)]  # kept

    household_id = book.household_id
    assert len(household_audit(factories, household_id, "budget.create")) == 1
    [update_row] = household_audit(factories, household_id, "budget.update")
    assert update_row.before is not None
    assert update_row.after is not None
    assert update_row.after["lines"] == {
        str(book.groceries.id): "320.500000",
        str(book.housing.id): "10.000000",
    }
    assert world.member.delete(url).status_code == 204
    assert problem_type(world.member.get(url)) == "budget-not-found"
    assert len(household_audit(factories, household_id, "budget.delete")) == 1
    for event in ("budget.created", "budget.updated", "budget.deleted"):
        [row] = household_events(factories, household_id, event)
        assert row.payload == {"budget_id": created["id"]}


def test_pagination(world: World) -> None:
    ids = [
        _create(world.owner, {"name": f"B{i}", "period": "monthly", "start_date": "2026-01-01"})[
            "id"
        ]
        for i in range(3)
    ]
    first = world.owner.get(BUDGETS, params={"limit": 2}).json()
    assert [b["id"] for b in first["items"]] == ids[:2]
    second = world.owner.get(BUDGETS, params={"limit": 2, "cursor": first["next_cursor"]}).json()
    assert ([b["id"] for b in second["items"]], second["next_cursor"]) == (ids[2:], None)
    assert world.owner.get(BUDGETS, params={"limit": 501}).status_code == 422


def test_idempotent_create(world: World) -> None:
    body = {"name": "Once", "period": "annual", "start_date": "2026-04-01"}
    headers = {"Idempotency-Key": "budget-once"}
    first = world.owner.post(BUDGETS, json=body, headers=headers)
    again = world.owner.post(BUDGETS, json=body, headers=headers)
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.headers.get("Idempotent-Replayed") == "true"
    assert first.json()["id"] == again.json()["id"]
    assert len(world.owner.get(BUDGETS).json()["items"]) == 1


def test_roles_and_other_households(actors: Actors, world: World) -> None:
    household = world.owner.record.household
    viewer = actors.new(role="viewer", household=household)
    created = _create(world.owner, _monthly(world.book))
    url = f"{BUDGETS}/{created['id']}"
    assert viewer.get(url).status_code == 200
    assert viewer.get(f"{url}/report", params={"period": "2026-03"}).status_code == 200
    denied = viewer.post(
        BUDGETS, json={"name": "X", "period": "monthly", "start_date": "2026-01-01"}
    )
    assert (denied.status_code, problem_type(denied)) == (403, "forbidden")
    assert viewer.patch(url, json={"name": "X"}).status_code == 403
    assert viewer.delete(url).status_code == 403
    outsider = actors.new(role="owner")
    for response in (
        outsider.get(url),
        outsider.get(f"{url}/report"),
        outsider.patch(url, json={"name": "X"}),
        outsider.delete(url),
    ):
        assert (response.status_code, problem_type(response)) == (404, "budget-not-found")
    assert world.owner.get(url).json()["name"] == "Household plan"
    # A category of another household cannot be budgeted.
    other = outsider.record.household
    assert other is not None
    alien = _line(world.book.groceries, "1")
    refused = outsider.post(
        BUDGETS,
        json={"name": "X", "period": "monthly", "start_date": "2026-01-01", "lines": [alien]},
    )
    assert (refused.status_code, problem_type(refused)) == (404, "category-not-found")


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"period": "monthly", "start_date": "2026-01-15"}, "invalid-budget-dates"),
        (
            {"period": "annual", "start_date": "2026-01-01", "end_date": "2026-12-31"},
            "invalid-budget-dates",
        ),
        ({"period": "custom", "start_date": "2026-01-01"}, "invalid-budget-dates"),
        (
            {"period": "custom", "start_date": "2026-02-01", "end_date": "2026-01-31"},
            "invalid-budget-dates",
        ),
        (
            {"period": "custom", "start_date": "2000-01-01", "end_date": "2026-01-31"},
            "invalid-budget-dates",
        ),
    ],
)
def test_dates_are_validated(world: World, body: dict[str, Any], expected: str) -> None:
    response = world.owner.post(BUDGETS, json={"name": "X", **body})
    assert (response.status_code, problem_type(response)) == (422, expected)


def test_lines_are_validated(world: World) -> None:
    book = world.book
    base = {"name": "X", "period": "monthly", "start_date": "2026-01-01"}

    def refused(lines: list[dict[str, Any]], **extra: Any) -> tuple[int, str]:
        response = world.owner.post(BUDGETS, json={**base, **extra, "lines": lines})
        return response.status_code, problem_type(response)

    eur = {"category_id": str(book.groceries.id), "amount": {"amount": "1", "currency": "EUR"}}
    assert refused([eur]) == (422, "currency-mismatch")
    assert refused([_line(book.groceries, "1")], currency="EUR") == (422, "currency-mismatch")
    assert refused([_line(book.moves, "1")]) == (422, "budget-category-kind")
    assert refused([_line(book.groceries, "-1")]) == (422, "invalid-budget-amount")
    missing = {"category_id": str(uuid.uuid4()), "amount": _usd("1")}
    assert refused([missing]) == (404, "category-not-found")
    twice = world.owner.post(
        BUDGETS, json={**base, "lines": [_line(book.food, "1"), _line(book.food, "2")]}
    )
    assert twice.status_code == 422
    float_amount = {"category_id": str(book.food.id), "amount": {"amount": 1.5, "currency": "USD"}}
    assert world.owner.post(BUDGETS, json={**base, "lines": [float_amount]}).status_code == 422
    extra = world.owner.post(BUDGETS, json={**base, "household_id": str(uuid.uuid4())})
    assert extra.status_code == 422


def test_currency_change_needs_lines(world: World) -> None:
    book = world.book
    created = _create(world.owner, _monthly(book))
    url = f"{BUDGETS}/{created['id']}"
    response = world.owner.patch(url, json={"currency": "EUR"})
    assert (response.status_code, problem_type(response)) == (422, "budget-lines-required")
    eur_line = {"category_id": str(book.food.id), "amount": {"amount": "5", "currency": "EUR"}}
    changed = world.owner.patch(url, json={"currency": "EUR", "lines": [eur_line]})
    assert changed.status_code == 200, changed.text
    assert changed.json()["lines"][0]["amount"] == {"amount": "5.00", "currency": "EUR"}
    to_custom = world.owner.patch(url, json={"period": "custom"})
    assert problem_type(to_custom) == "invalid-budget-dates"
    custom = world.owner.patch(url, json={"period": "custom", "end_date": "2026-06-30"})
    assert custom.json()["end_date"] == "2026-06-30"
    back = world.owner.patch(url, json={"period": "monthly"})
    assert (back.json()["period"], back.json()["end_date"]) == ("monthly", None)
    assert world.owner.patch(url, json={"name": None}).status_code == 422


# ---- report: actuals, rollover, visibility -------------------------------------------


def test_report_past_period(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch, date(2026, 4, 10))
    book = world.book
    budget = _create(world.owner, _monthly(book))
    report = _report(world.owner, budget["id"], period="2026-03")
    assert report["period"] == {"start": "2026-03-01", "end": "2026-03-31", "state": "past"}
    assert report["as_of"] == "2026-04-10"
    lines = _by_category(report)
    # Groceries covers Organic (no line of its own): 100.25 + 50.10 + 50 - 12.30
    # + 1000 (hidden account). The pending row and the GBP row are left out.
    assert lines[str(book.groceries.id)] == {
        "budgeted": "300.00",
        "carried_over": "0.00",
        "available": "300.00",
        "actual": "1188.05",
        "remaining": "-888.05",
        "forecast": "1188.05",
    }
    assert lines[str(book.restaurants.id)]["actual"] == "54.666666"
    assert lines[str(book.food.id)]["actual"] == "0.00"  # children have their own lines
    assert lines[str(book.rent.id)]["actual"] == "150.00"
    assert lines[str(book.salary.id)]["actual"] == "3000.00"
    assert report["unbudgeted"] == [
        {"category_id": None, "name": None, "actual": _usd("7.77")},
    ]
    assert report["unbudgeted_total"] == _usd("7.77")
    assert report["expense_totals"] == {
        "budgeted": _usd("1450.00"),
        "carried_over": _usd("0.00"),
        "available": _usd("1450.00"),
        "actual": _usd("1392.716666"),
        "remaining": _usd("57.283334"),
        "forecast": _usd("1392.716666"),
    }
    assert report["income_totals"]["actual"] == _usd("3000.00")
    assert report["income_totals"]["budgeted"] == _usd("2500.00")
    assert report["unconverted"] == [{"amount": "-9.00", "currency": "GBP"}]
    assert [line["name"] for line in report["lines"]][-1] == "Salary"  # income last

    pending = _report(world.owner, budget["id"], period="2026-03-31", include_pending="true")
    assert _by_category(pending)[str(book.groceries.id)]["actual"] == "2187.05"


def test_report_respects_account_visibility(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch, date(2026, 4, 10))
    book = world.book
    budget = _create(world.owner, _monthly(book))
    lines = _by_category(_report(world.member, budget["id"], period="2026-03"))
    assert lines[str(book.groceries.id)]["actual"] == "188.05"  # no hidden account
    assert _report(world.member, budget["id"], period="2026-03")["unconverted"] == []


def test_rollover_carries_what_is_left(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch, date(2026, 4, 10))
    book = world.book
    budget = _create(world.owner, _monthly(book, rollover=True))
    january = _by_category(_report(world.owner, budget["id"], period="2026-01"))
    assert january[str(book.groceries.id)]["carried_over"] == "0.00"
    february = _by_category(_report(world.owner, budget["id"], period="2026-02"))
    assert february[str(book.groceries.id)]["carried_over"] == "300.00"  # Jan unspent
    assert february[str(book.groceries.id)]["actual"] == "60.00"
    march = _report(world.owner, budget["id"], period="2026-03")
    lines = _by_category(march)
    # 300 (Jan) + 300 - 60 (Feb) carried; available 840; spent 1188.05.
    assert lines[str(book.groceries.id)]["carried_over"] == "540.00"
    assert lines[str(book.groceries.id)]["available"] == "840.00"
    assert lines[str(book.groceries.id)]["remaining"] == "-348.05"
    assert lines[str(book.restaurants.id)]["carried_over"] == "200.00"
    assert lines[str(book.rent.id)]["available"] == "3000.00"
    assert lines[str(book.salary.id)]["carried_over"] == "0.00"  # income never carries
    april = _by_category(_report(world.owner, budget["id"], period="2026-04"))
    assert april[str(book.groceries.id)]["carried_over"] == "-348.05"  # overspent carries
    assert march["expense_totals"]["carried_over"] == _usd("2840.00")  # 540 + 200 + 100 + 2000


def test_custom_and_annual_periods(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    _freeze(monkeypatch, date(2026, 4, 10))
    book = world.book
    custom = _create(
        world.owner,
        {
            "name": "Trip",
            "period": "custom",
            "start_date": "2026-03-10",
            "end_date": "2026-03-20",
            "lines": [_line(book.groceries, "100")],
        },
    )
    report = _report(world.owner, custom["id"], period="2026-03-15")
    assert report["period"] == {"start": "2026-03-10", "end": "2026-03-20", "state": "past"}
    # Split groceries 50 (03-12) minus the 12.30 refund (03-15).
    assert _by_category(report)[str(book.groceries.id)]["actual"] == "37.70"
    assert report["unbudgeted"] == [
        {"category_id": str(book.rent.id), "name": "Rent", "actual": _usd("150.00")},
        {
            "category_id": str(book.restaurants.id),
            "name": "Restaurants",
            "actual": _usd("54.666666"),
        },
        {"category_id": None, "name": None, "actual": _usd("7.77")},
    ]
    assert report["unbudgeted_total"] == _usd("212.436666")
    outside = world.owner.get(f"{BUDGETS}/{custom['id']}/report")  # today is after the end
    assert (outside.status_code, problem_type(outside)) == (422, "period-out-of-range")

    annual = _create(
        world.owner,
        {
            "name": "Fiscal",
            "period": "annual",
            "start_date": "2025-04-01",
            "lines": [_line(book.groceries, "5000")],
        },
    )
    fiscal = _report(world.owner, annual["id"], period="2026-03")
    assert fiscal["period"] == {"start": "2025-04-01", "end": "2026-03-31", "state": "past"}
    assert _by_category(fiscal)[str(book.groceries.id)]["actual"] == "1248.05"  # Feb + Mar
    before = world.owner.get(f"{BUDGETS}/{annual['id']}/report", params={"period": "2025-03"})
    assert (before.status_code, problem_type(before)) == (422, "period-out-of-range")
    bad = world.owner.get(f"{BUDGETS}/{annual['id']}/report", params={"period": "2026-02-30"})
    assert (bad.status_code, problem_type(bad)) == (422, "invalid-period")


# ---- report: forecast ---------------------------------------------------------------


@dataclass(frozen=True)
class ForecastBook:
    owner: AuthedClient
    groceries: Any
    restaurants: Any
    rent: Any
    budget_id: str


@pytest.fixture
def forecast_book(actors: Actors, ledger: LedgerFactories, factories: Factories) -> ForecastBook:
    """A household on 2026-03-10 (10 days in, 21 to go) with three active schedules."""
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    hid = household.id
    checking = ledger.account(hid)
    card = ledger.account(hid)
    groceries = ledger.category(hid, name="Groceries")
    restaurants = ledger.category(hid, name="Restaurants")
    rent = ledger.category(hid, name="Rent")
    diner = ledger.payee(hid, name="Diner")
    landlord = ledger.payee(hid, name="Landlord")
    ledger.transaction(checking, posted_date=date(2026, 3, 2), amount="-100", category=groceries)
    ledger.transaction(checking, posted_date=date(2026, 3, 8), amount="-50", category=groceries)
    # Matches the card/diner/restaurants schedule: expected again, not extrapolated.
    ledger.transaction(
        card, posted_date=date(2026, 3, 5), amount="-40", category=restaurants, payee=diner
    )
    ledger.transaction(
        card, posted_date=date(2026, 3, 6), amount="-12.345678", category=restaurants
    )
    ledger.run(
        make_scheduled_transaction,
        card,
        cadence=Cadence.MONTHLY,
        next_due=date(2026, 4, 5),
        amount_min="-40",
        amount_max="-40",
        payee=diner,
        category=restaurants,
    )
    ledger.run(
        make_scheduled_transaction,
        checking,
        cadence=Cadence.MONTHLY,
        next_due=date(2026, 3, 25),
        amount_min="-1100",
        amount_max="-1000",
        payee=landlord,
        category=rent,
    )
    ledger.run(
        make_scheduled_transaction,
        checking,
        cadence=Cadence.WEEKLY,
        next_due=date(2026, 3, 4),
        amount_min="-30",
        amount_max="-20",
        category=groceries,
    )
    inactive = ledger.run(
        make_scheduled_transaction,
        checking,
        next_due=date(2026, 3, 20),
        amount_min="-5000",
        amount_max="-5000",
        category=rent,
    )
    with factories.transaction() as db:
        set_context(db, hid)
        db.execute(
            update(ScheduledTransaction)
            .where(ScheduledTransaction.id == inactive.id)
            .values(active=False)
        )
    budget = _create(
        owner,
        {
            "name": "March",
            "period": "monthly",
            "start_date": "2026-01-01",
            "lines": [
                _line(groceries, "400"),
                _line(restaurants, "200"),
                _line(rent, "1000"),
            ],
        },
    )
    return ForecastBook(owner, groceries, restaurants, rent, budget["id"])


def test_forecast_current_period(
    forecast_book: ForecastBook, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, date(2026, 3, 10))
    fb = forecast_book
    report = _report(fb.owner, fb.budget_id)
    assert report["period"] == {"start": "2026-03-01", "end": "2026-03-31", "state": "current"}
    lines = _by_category(report)
    # Groceries: 150 so far, projected 150 * 21 / 10 = 315, plus the weekly
    # schedule on 03-11, 03-18 and 03-25 at its midpoint 25: 150 + 315 + 75.
    assert lines[str(fb.groceries.id)]["actual"] == "150.00"
    assert lines[str(fb.groceries.id)]["forecast"] == "540.00"
    # Restaurants: 52.345678 so far, of which 40 matches a schedule (next due
    # in April); the rest projects 12.345678 * 21 / 10 = 25.9259238 -> 25.925924.
    assert lines[str(fb.restaurants.id)]["actual"] == "52.345678"
    assert lines[str(fb.restaurants.id)]["forecast"] == "78.271602"
    # Rent: nothing yet; the landlord schedule (midpoint 1050) is due 03-25;
    # the inactive schedule is ignored.
    assert lines[str(fb.rent.id)]["forecast"] == "1050.00"
    assert report["expense_totals"]["forecast"] == _usd("1668.271602")
    assert report["expense_totals"]["actual"] == _usd("202.345678")


def test_forecast_future_and_past_periods(
    forecast_book: ForecastBook, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, date(2026, 3, 10))
    fb = forecast_book
    april = _report(fb.owner, fb.budget_id, period="2026-04")
    assert april["period"]["state"] == "future"
    lines = _by_category(april)
    assert lines[str(fb.groceries.id)]["forecast"] == "125.00"  # 04-01, 08, 15, 22, 29
    assert lines[str(fb.restaurants.id)]["forecast"] == "40.00"  # 04-05
    assert lines[str(fb.rent.id)]["forecast"] == "1050.00"  # 04-25
    february = _report(fb.owner, fb.budget_id, period="2026-02")
    assert february["period"]["state"] == "past"
    assert february["expense_totals"]["forecast"] == _usd("0.00")


def test_forecast_hides_schedules_on_hidden_accounts(
    actors: Actors, forecast_book: ForecastBook, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, date(2026, 3, 10))
    fb = forecast_book
    member = actors.new(role="member", household=fb.owner.record.household)
    lines = _by_category(_report(member, fb.budget_id))
    assert all(line["actual"] == "0.00" for line in lines.values())
    assert all(line["forecast"] == "0.00" for line in lines.values())


def test_line_on_parent_category_covers_children(
    actors: Actors, ledger: LedgerFactories, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, date(2026, 4, 1))
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    hid = household.id
    account = ledger.account(hid)
    parent = ledger.category(hid, name="Leisure")
    child = ledger.category(hid, name="Cinema", parent=parent)
    income = ledger.category(hid, name="Gifts", kind=CategoryKind.INCOME)
    ledger.transaction(account, posted_date=date(2026, 3, 3), amount="-9.99", category=child)
    ledger.transaction(account, posted_date=date(2026, 3, 4), amount="-0.01", category=parent)
    ledger.transaction(account, posted_date=date(2026, 3, 5), amount="25", category=income)
    budget = _create(
        owner,
        {
            "name": "Leisure",
            "period": "monthly",
            "start_date": "2026-03-01",
            "lines": [_line(parent, "20")],
        },
    )
    report = _report(owner, budget["id"], period="2026-03")
    assert _by_category(report)[str(parent.id)]["actual"] == "10.00"
    assert report["unbudgeted"] == []  # income without a line is not "unbudgeted"
