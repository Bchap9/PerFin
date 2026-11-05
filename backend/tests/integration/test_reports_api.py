# SPDX-License-Identifier: AGPL-3.0-or-later
"""Spending, income, trend and cash-flow reports over HTTP (``app.domain.reports``).

Totals are checked exactly (``Decimal`` strings) on the synthetic ledger of
``tests/fixtures/report_ledger.py``: splits instead of split transactions,
transfers and transfer/ignore categories left out, pending review rows out
by default, EUR converted per posting day, GBP without a rate reported as
unconverted, and a member's reports limited to the accounts granted to them.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Any

import pytest

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.models import Setting, SettingScope
from app.db.rls import set_context
from app.domain.reports import service as reports_service
from app.domain.transactions.service import INCLUDE_PENDING_SETTING
from tests.fixtures import report_ledger
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, problem_type
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.report_ledger import ReportLedger

pytestmark = pytest.mark.integration

SPENDING = f"{API}/reports/spending"
INCOME = f"{API}/reports/income"
TRENDS = f"{API}/reports/trends"
CASHFLOW = f"{API}/reports/cashflow"
MARCH = {"date_from": "2026-03-01", "date_to": "2026-03-31"}
NO_GBP_RATE = [{"amount": "9.00", "currency": "GBP"}]


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


def _get(client: AuthedClient, url: str, **params: Any) -> dict[str, Any]:
    response = client.get(url, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _amounts(report: dict[str, Any]) -> dict[str | None, str]:
    return {item["key"]: item["amount"]["amount"] for item in report["items"]}


def _id(row: Any) -> str:
    return str(row.id)


# ---- spending ----------------------------------------------------------------------


def test_spending_by_top_level_category(world: World) -> None:
    book = world.book
    report = _get(world.owner, SPENDING, **MARCH)
    # Food: 100.25 + 50.10 + 50 (split) - 12.30 (refund) + 40 + 11.00 (10 EUR)
    #       + 3.666666 (3.333333 EUR) + 1000 (hidden account) = 1242.716666
    # Housing: 150 (split). Uncategorised: 7.77. GBP has no rate.
    assert _amounts(report) == {
        _id(book.food): "1242.716666",
        _id(book.housing): "150.00",
        None: "7.77",
    }
    assert [item["key"] for item in report["items"]] == [_id(book.food), _id(book.housing), None]
    assert report["items"][0]["name"] == "Food"
    assert report["total"] == {"amount": "1400.486666", "currency": "USD"}
    assert report["unconverted"] == NO_GBP_RATE
    assert (report["currency"], report["group_by"], report["depth"]) == ("USD", "category", 1)
    assert report["include_pending"] is False


def test_spending_depth_rolls_up_the_hierarchy(world: World) -> None:
    book = world.book
    two = _amounts(_get(world.owner, SPENDING, depth=2, **MARCH))
    assert two == {
        _id(book.groceries): "1188.05",  # 100.25 + 50.10 + 50 - 12.30 + 1000
        _id(book.restaurants): "54.666666",
        _id(book.rent): "150.00",
        None: "7.77",
    }
    three = _amounts(_get(world.owner, SPENDING, depth=3, **MARCH))
    assert three[_id(book.groceries)] == "1137.95"
    assert three[_id(book.organic)] == "50.10"


def test_spending_by_payee_account_and_month(world: World) -> None:
    book = world.book
    by_payee = _amounts(_get(world.owner, SPENDING, group_by="payee", **MARCH))
    assert by_payee == {_id(book.shop): "150.35", _id(book.diner): "40.00", None: "1210.136666"}
    by_account = _get(world.owner, SPENDING, group_by="account", **MARCH)
    assert _amounts(by_account) == {
        _id(book.checking): "345.82",  # 100.25 + 50.10 + 200 - 12.30 + 7.77
        _id(book.card): "40.00",
        _id(book.euro): "14.666666",
        _id(book.hidden): "1000.00",
    }
    assert {i["name"] for i in by_account["items"]} == {"Checking", "Card", "Euro", "Hidden"}
    by_month = _get(
        world.owner, SPENDING, group_by="month", date_from="2026-01-01", date_to="2026-03-31"
    )
    assert [(i["key"], i["amount"]["amount"]) for i in by_month["items"]] == [
        ("2026-01", "0.00"),
        ("2026-02", "60.00"),
        ("2026-03", "1400.486666"),
    ]
    assert by_month["total"]["amount"] == "1460.486666"


def test_income_report(world: World) -> None:
    book = world.book
    report = _get(world.owner, INCOME, **MARCH)
    assert _amounts(report) == {_id(book.salary): "3000.00", None: "5.55"}
    assert report["total"]["amount"] == "3005.55"
    assert report["unconverted"] == []


def test_member_sees_only_granted_accounts(world: World) -> None:
    book = world.book
    report = _get(world.member, SPENDING, **MARCH)
    assert _amounts(report) == {
        _id(book.food): "242.716666",
        _id(book.housing): "150.00",
        None: "7.77",
    }
    assert report["total"]["amount"] == "400.486666"
    assert report["unconverted"] == []  # the GBP account is not theirs
    by_account = _amounts(_get(world.member, SPENDING, group_by="account", **MARCH))
    assert _id(book.hidden) not in by_account
    hidden = world.member.get(SPENDING, params={"account": _id(book.hidden), **MARCH})
    assert (hidden.status_code, problem_type(hidden)) == (404, "account-not-found")
    foreign = world.member.get(SPENDING, params={"account": str(uuid.uuid4())})
    assert (foreign.status_code, problem_type(foreign)) == (404, "account-not-found")


def test_account_filter(world: World) -> None:
    book = world.book
    params = {"account": [_id(book.card), _id(book.euro)], **MARCH}
    report = _get(world.owner, SPENDING, **params)
    assert _amounts(report) == {_id(book.food): "54.666666"}


def test_pending_review_rows_are_left_out_unless_asked(world: World, factories: Factories) -> None:
    book = world.book
    included = _get(world.owner, SPENDING, include_pending="true", **MARCH)
    assert _amounts(included)[_id(book.food)] == "2241.716666"  # + 999
    # The household setting switches the default.
    with factories.transaction() as db:
        set_context(db, book.household_id)
        db.add(
            Setting(
                scope=SettingScope.HOUSEHOLD,
                scope_id=book.household_id,
                key=INCLUDE_PENDING_SETTING,
                value=True,
            )
        )
    by_default = _get(world.owner, SPENDING, **MARCH)
    assert by_default["include_pending"] is True
    assert by_default["total"]["amount"] == "2399.486666"
    explicit = _get(world.owner, SPENDING, include_pending="false", **MARCH)
    assert explicit["total"]["amount"] == "1400.486666"


def test_default_range_is_the_current_month(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(reports_service, "today", lambda: date(2026, 3, 18))
    report = _get(world.owner, SPENDING)
    assert (report["date_from"], report["date_to"]) == ("2026-03-01", "2026-03-31")
    assert report["total"]["amount"] == "1400.486666"
    cash = _get(world.owner, CASHFLOW)
    assert (cash["date_from"], cash["date_to"]) == ("2025-04-01", "2026-03-31")
    assert len(cash["months"]) == 12


# ---- trends ------------------------------------------------------------------------


def test_trends_month_on_month_and_year_on_year(world: World) -> None:
    book = world.book
    report = _get(world.owner, TRENDS, month="2026-03", months=3)
    assert report["series"] == [
        {"month": "2026-01", "amount": {"amount": "0.00", "currency": "USD"}},
        {"month": "2026-02", "amount": {"amount": "60.00", "currency": "USD"}},
        {"month": "2026-03", "amount": {"amount": "1400.486666", "currency": "USD"}},
    ]
    total = report["total"]
    assert total["amount"]["amount"] == "1400.486666"
    assert total["previous"]["amount"] == "60.00"
    assert total["mom_change"]["amount"] == "1340.486666"
    assert total["mom_percent"] == "2234.14"
    assert total["year_ago"]["amount"] == "80.00"
    assert total["yoy_change"]["amount"] == "1320.486666"
    assert total["yoy_percent"] == "1650.61"
    food = next(row for row in report["categories"] if row["key"] == _id(book.food))
    assert (food["amount"]["amount"], food["previous"]["amount"]) == ("1242.716666", "60.00")
    assert food["mom_percent"] == "1971.19"  # 1182.716666 / 60
    assert food["yoy_percent"] == "1453.40"  # 1162.716666 / 80
    housing = next(row for row in report["categories"] if row["key"] == _id(book.housing))
    assert housing["previous"]["amount"] == "0.00"
    assert housing["mom_percent"] is None  # nothing to compare with
    assert report["unconverted"] == NO_GBP_RATE


def test_income_trend(world: World) -> None:
    report = _get(world.owner, TRENDS, flow="income", month="2026-03", months=1)
    assert report["series"] == [
        {"month": "2026-03", "amount": {"amount": "3005.55", "currency": "USD"}}
    ]
    assert report["total"]["previous"]["amount"] == "0.00"


# ---- cash flow ---------------------------------------------------------------------


def test_cashflow_per_month(world: World) -> None:
    report = _get(world.owner, CASHFLOW, date_from="2026-02-01", date_to="2026-03-31")
    assert report["months"] == [
        {
            "month": "2026-02",
            "inflow": {"amount": "0.00", "currency": "USD"},
            "outflow": {"amount": "60.00", "currency": "USD"},
            "net": {"amount": "-60.00", "currency": "USD"},
        },
        {
            # In: 3000 + 12.30 (refund) + 5.55. Out: 100.25 + 50.10 + 40 + 11.00
            # + 3.666666 + 200 + 7.77 + 1000. Transfers, moves, ignored and the
            # pending row are left out; GBP has no rate.
            "month": "2026-03",
            "inflow": {"amount": "3017.85", "currency": "USD"},
            "outflow": {"amount": "1412.786666", "currency": "USD"},
            "net": {"amount": "1605.063334", "currency": "USD"},
        },
    ]
    assert report["net"]["amount"] == "1545.063334"
    assert report["unconverted"] == [{"amount": "-9.00", "currency": "GBP"}]
    # Net cash flow is income minus spending.
    income = _get(world.owner, INCOME, **MARCH)["total"]["amount"]
    spending = _get(world.owner, SPENDING, **MARCH)["total"]["amount"]
    assert income == "3005.55"
    assert spending == "1400.486666"


# ---- bounds and validation (SECURITY.md F8, F4) -------------------------------------


@pytest.mark.parametrize(
    ("url", "params", "expected"),
    [
        (SPENDING, {"date_from": "2026-03-02", "date_to": "2026-03-01"}, "invalid-range"),
        (SPENDING, {"date_from": "2000-01-01", "date_to": "2026-03-01"}, "range-too-long"),
        (CASHFLOW, {"date_from": "2000-01-01", "date_to": "2026-03-01"}, "range-too-long"),
        (TRENDS, {"month": "2026-13"}, "invalid-period"),
        (TRENDS, {"month": "March"}, "invalid-period"),
    ],
)
def test_bounded_and_validated(
    world: World, url: str, params: dict[str, str], expected: str
) -> None:
    response = world.owner.get(url, params=params)
    assert (response.status_code, problem_type(response)) == (422, expected)


@pytest.mark.parametrize(
    ("url", "params"),
    [
        (SPENDING, {"depth": "4"}),
        (SPENDING, {"depth": "0"}),
        (SPENDING, {"group_by": "tag"}),
        (TRENDS, {"months": "61"}),
        (TRENDS, {"flow": "net"}),
        (INCOME, {"account": "not-a-uuid"}),
    ],
)
def test_parameters_are_validated(world: World, url: str, params: dict[str, str]) -> None:
    response = world.owner.get(url, params=params)
    assert response.status_code == 422, response.text


def test_account_filter_is_capped(world: World) -> None:
    params = {"account": [str(uuid.uuid4()) for _ in range(51)]}
    response = world.owner.get(SPENDING, params=params)
    assert response.status_code == 422


def test_viewer_reads_and_other_household_sees_nothing(
    actors: Actors, world: World, ledger: LedgerFactories
) -> None:
    household = world.owner.record.household
    viewer = actors.new(role="viewer", household=household)
    assert _get(viewer, SPENDING, **MARCH)["total"]["amount"] == "0.00"  # no grants
    ledger.grant(world.book.household_id, viewer.record.id, world.book.card, GrantLevel.VIEW)
    assert _get(viewer, SPENDING, **MARCH)["total"]["amount"] == "40.00"
    outsider = actors.new(role="owner")
    assert _get(outsider, SPENDING, **MARCH)["items"] == []
    named = outsider.get(SPENDING, params={"account": _id(world.book.card)})
    assert (named.status_code, problem_type(named)) == (404, "account-not-found")
