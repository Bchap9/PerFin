# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loans over HTTP: terms, schedules, payments, balance sync, simulation, visibility.

Figures are exact (``Decimal`` strings); the engine is unit-tested in
``tests/unit/test_amortization.py``. "Today" is frozen at 2026-06-30.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.domain.accounts import service as accounts_service
from app.domain.accounts.models import Account, AccountType
from app.domain.loans import service as loans_service
from app.domain.loans.amortization import Terms, period_interest, schedule
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import (
    API,
    Actors,
    household_audit,
    household_events,
    problem_type,
)
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration

LOANS = f"{API}/loans"
TODAY = date(2026, 6, 30)
D = Decimal


@dataclass(frozen=True)
class World:
    owner: AuthedClient
    member: AuthedClient
    viewer: AuthedClient
    household_id: uuid.UUID
    mortgage: Account
    car: Account
    hidden: Account
    checking: Account
    ledger: LedgerFactories


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


@pytest.fixture
def world(actors: Actors, ledger: LedgerFactories, monkeypatch: pytest.MonkeyPatch) -> World:
    monkeypatch.setattr(loans_service, "today", lambda: TODAY)
    monkeypatch.setattr(accounts_service, "today", lambda: TODAY)
    owner = actors.new(role="owner")
    household = owner.record.household
    assert household is not None
    member = actors.new(role="member", household=household)
    viewer = actors.new(role="viewer", household=household)
    hid = household.id
    mortgage = ledger.account(hid, type=AccountType.MORTGAGE, name="Home loan")
    car = ledger.account(hid, type=AccountType.LOAN, name="Car loan")
    hidden = ledger.account(hid, type=AccountType.LOAN, name="Hidden loan")
    checking = ledger.account(hid, type=AccountType.CHECKING, name="Checking")
    for account in (mortgage, car, checking):
        ledger.grant(hid, member.record.id, account, GrantLevel.EDIT)
        ledger.grant(hid, viewer.record.id, account, GrantLevel.VIEW)
    return World(owner, member, viewer, hid, mortgage, car, hidden, checking, ledger)


def _usd(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "USD"}


def _post(client: AuthedClient, url: str, body: dict[str, Any], status: int = 201) -> Any:
    response = client.post(url, json=body)
    assert response.status_code == status, response.text
    return response.json()


def _mortgage_body(account: Account, **extra: Any) -> dict[str, Any]:
    return {
        "account_id": str(account.id),
        "principal_original": _usd("250000.00"),
        "rate": "0.06125",
        "term_months": 360,
        "origination_date": "2024-03-01",
        "payment_day": 1,
        "escrow_amount": _usd("350.00"),
        "lender": "Placeholder Home Lending",
        **extra,
    }


def _terms() -> Terms:
    return Terms(
        principal=D("250000.00"),
        annual_rate=D("0.06125"),
        term_months=360,
        origination_date=date(2024, 3, 1),
        payment_day=1,
        currency="USD",
        escrow=D("350.00"),
    )


def _balance(client: AuthedClient, account: Account) -> str:
    response = client.get(f"{API}/accounts/{account.id}")
    assert response.status_code == 200, response.text
    return str(response.json()["balance"]["amount"])


def test_create_loan_computes_the_payment_and_syncs_the_balance(
    world: World, factories: Factories
) -> None:
    loan = _post(world.member, LOANS, _mortgage_body(world.mortgage))
    assert loan["payment_amount"] == _usd("1519.03")
    assert loan["rate"] == "0.06125"
    assert loan["maturity_date"] == "2054-03-01"
    assert loan["escrow_amount"] == _usd("350.00")
    projection = loan["projection"]
    # No payments recorded: the original schedule, payments assumed made.
    original = schedule(_terms())
    paid = [r for r in original.rows if r.due_date <= TODAY]
    assert projection["balance_source"] == "scheduled"
    assert projection["balance"]["amount"] == f"{paid[-1].balance:f}"
    assert projection["next_due_date"] == "2026-07-01"
    assert projection["payoff_date"] == "2054-03-01"
    assert projection["remaining_payments"] == 360 - len(paid)
    # The account's balance is the principal owed (origination snapshot).
    assert _balance(world.member, world.mortgage) == "-250000.00"
    [entry] = household_audit(factories, world.household_id, "loan.create")
    assert entry.after is not None
    assert "lender" not in entry.after
    events = household_events(factories, world.household_id, "loan.created")
    assert events[-1].payload == {"loan_id": loan["id"], "account_id": str(world.mortgage.id)}

    listed = world.viewer.get(LOANS).json()["items"]
    assert [item["id"] for item in listed] == [loan["id"]]


def test_create_loan_opens_its_account_in_one_step(
    world: World, actors: Actors, factories: Factories
) -> None:
    def body(**extra: Any) -> dict[str, Any]:
        terms = _mortgage_body(world.mortgage, **extra)
        del terms["account_id"]
        return {"account": {"name": "Lake house", "type": "mortgage"}, **terms}

    before = {a["id"] for a in world.member.get(f"{API}/accounts").json()["items"]}
    # Refused terms leave no account behind.
    low = world.member.post(LOANS, json=body(payment_amount=_usd("1000.00")))
    assert problem_type(low) == "loan-payment-too-low"
    wrong = body()
    wrong["account"]["type"] = "checking"
    assert problem_type(world.member.post(LOANS, json=wrong)) == "invalid-loan-account"
    both = {**body(), "account_id": str(world.car.id)}
    assert problem_type(world.member.post(LOANS, json=both)) == "validation-failed"
    neither = body()
    del neither["account"]
    assert problem_type(world.member.post(LOANS, json=neither)) == "validation-failed"
    assert problem_type(world.viewer.post(LOANS, json=body())) == "forbidden"
    raw = factories.token(world.member.record, scopes=("loans:read", "loans:write"))
    refused = actors.anonymous().post(
        LOANS, headers={"Authorization": f"Bearer {raw}"}, json=body()
    )
    assert (refused.status_code, problem_type(refused)) == (403, "insufficient-scope")
    assert {a["id"] for a in world.member.get(f"{API}/accounts").json()["items"]} == before
    assert household_audit(factories, world.household_id, "account.create") == []

    loan = _post(world.member, LOANS, body())
    assert loan["payment_amount"] == _usd("1519.03")
    account = world.member.get(f"{API}/accounts/{loan['account_id']}").json()
    assert loan["account_id"] not in before
    assert (account["name"], account["type"], account["currency"]) == (
        "Lake house",
        "mortgage",
        "USD",
    )
    assert account["opened_on"] == "2024-03-01"
    assert account["can_edit"] is True
    assert account["balance"]["amount"] == "-250000.00"
    [opened] = household_audit(factories, world.household_id, "account.create")
    assert opened.target_id == loan["account_id"]
    assert len(household_audit(factories, world.household_id, "loan.create")) == 1
    # A member who sees only granted accounts keeps the loan they added.
    assert [item["id"] for item in world.member.get(LOANS).json()["items"]] == [loan["id"]]

    # The defaults: a plain loan account.
    plain = body()
    plain["account"] = {"name": "Boat"}
    del plain["escrow_amount"]
    boat = _post(world.member, LOANS, plain)
    assert world.member.get(f"{API}/accounts/{boat['account_id']}").json()["type"] == "loan"


def test_loan_rules(world: World) -> None:
    body = _mortgage_body(world.checking)
    assert problem_type(world.member.post(LOANS, json=body)) == "invalid-loan-account"
    euro = _mortgage_body(world.car, principal_original={"amount": "1000", "currency": "EUR"})
    assert problem_type(world.member.post(LOANS, json=euro)) == "currency-mismatch"
    low = _mortgage_body(world.car, payment_amount=_usd("1000.00"))
    assert problem_type(world.member.post(LOANS, json=low)) == "loan-payment-too-low"
    hidden = _mortgage_body(world.hidden)
    assert problem_type(world.member.post(LOANS, json=hidden)) == "account-not-found"
    bad_rate = _mortgage_body(world.car, rate="6.125")
    assert world.member.post(LOANS, json=bad_rate).status_code == 422
    float_rate = _mortgage_body(world.car, rate=0.05)
    assert world.member.post(LOANS, json=float_rate).status_code == 422
    assert world.viewer.post(LOANS, json=_mortgage_body(world.car)).status_code == 403
    _post(world.member, LOANS, _mortgage_body(world.car))
    assert problem_type(world.member.post(LOANS, json=_mortgage_body(world.car))) == "loan-exists"


def test_view_only_and_hidden_loans(world: World) -> None:
    hidden = _post(world.owner, LOANS, _mortgage_body(world.hidden))
    assert problem_type(world.member.get(f"{LOANS}/{hidden['id']}")) == "loan-not-found"
    assert world.member.get(LOANS).json()["items"] == []
    viewed = world.ledger.account(world.household_id, type=AccountType.MORTGAGE, name="Shared")
    world.ledger.grant(world.household_id, world.member.record.id, viewed, GrantLevel.VIEW)
    shared = _post(world.owner, LOANS, _mortgage_body(viewed))
    url = f"{LOANS}/{shared['id']}"
    assert world.member.get(url).status_code == 200
    assert world.member.patch(url, json={"lender": "x"}).status_code == 403
    assert world.member.post(f"{url}/simulate", json={}).status_code == 200
    payment = {"paid_on": "2024-04-01", "principal": _usd("1"), "interest": _usd("1")}
    assert world.member.post(f"{url}/payments", json=payment).status_code == 403
    assert problem_type(world.viewer.get(f"{LOANS}/{hidden['id']}/schedule")) == "loan-not-found"


def test_schedule_original_and_projected(world: World) -> None:
    loan = _post(world.member, LOANS, _mortgage_body(world.mortgage))
    original = world.viewer.get(f"{LOANS}/{loan['id']}/schedule").json()
    assert original["basis"] == "original"
    assert len(original["rows"]) == 360
    assert original["rows"][0] == {
        "number": 1,
        "due_date": "2024-04-01",
        "rate": "0.06125",
        "payment": _usd("1519.03"),
        "principal": _usd("242.99"),
        "interest": _usd("1276.04"),
        "extra": _usd("0.00"),
        "escrow": _usd("350.00"),
        "total": _usd("1869.03"),
        "balance": _usd("249757.01"),
    }
    assert original["totals"]["principal"] == _usd("250000.00")
    assert original["totals"]["payoff_date"] == "2054-03-01"
    projected = world.viewer.get(f"{LOANS}/{loan['id']}/schedule?basis=projected").json()
    assert projected["rows"][0]["due_date"] == "2026-07-01"
    assert projected["rows"] == original["rows"][27:]


def test_payments_split_balance_sync_and_schedule_check(world: World) -> None:
    loan = _post(world.member, LOANS, _mortgage_body(world.mortgage))
    url = f"{LOANS}/{loan['id']}/payments"
    rows = schedule(_terms()).rows
    first = _post(
        world.member,
        url,
        {
            "paid_on": "2024-04-01",
            "principal": _usd(f"{rows[0].principal}"),
            "interest": _usd(f"{rows[0].interest}"),
            "escrow": _usd("350.00"),
        },
    )
    assert first["schedule_check"] == {
        "status": "ok",
        "expected_interest": _usd("1276.04"),
        "difference": _usd("0.00"),
    }
    assert first["total"] == _usd("1869.03")
    assert first["balance_after"] == _usd("249757.01")
    # Paid three days early, with an extra 1000 of principal.
    second = _post(
        world.member,
        url,
        {
            "paid_on": "2024-04-28",
            "principal": _usd(f"{rows[1].principal + 1000}"),
            "interest": _usd(f"{rows[1].interest}"),
            "escrow": _usd("350.00"),
        },
    )
    assert second["schedule_check"]["status"] == "ok"
    owed = D("249757.01") - rows[1].principal - 1000
    assert second["balance_after"] == _usd(f"{owed}")
    assert _balance(world.member, world.mortgage) == f"-{owed}"
    # Interest that does not match the balance owed is flagged.
    third = _post(
        world.member,
        url,
        {"paid_on": "2024-06-01", "principal": _usd("300.00"), "interest": _usd("1200.00")},
    )
    assert third["schedule_check"]["status"] == "differs"
    listed = world.viewer.get(url).json()["items"]
    assert [p["paid_on"] for p in listed] == ["2024-04-01", "2024-04-28", "2024-06-01"]

    projection = world.viewer.get(f"{LOANS}/{loan['id']}").json()["projection"]
    assert projection["balance_source"] == "recorded"
    assert projection["balance"] == _usd(f"{owed - 300}")
    assert projection["next_due_date"] == "2024-07-01"
    assert projection["payoff_date"] < "2054-03-01"

    assert world.member.delete(f"{url}/{third['id']}").status_code == 204
    assert _balance(world.member, world.mortgage) == f"-{owed}"
    future = {"paid_on": "2026-07-01", "principal": _usd("1"), "interest": _usd("0")}
    assert problem_type(world.member.post(url, json=future)) == "invalid-payment-date"
    zero = {"paid_on": "2025-01-01", "principal": _usd("0"), "interest": _usd("0")}
    assert problem_type(world.member.post(url, json=zero)) == "invalid-payment"
    negative = {"paid_on": "2025-01-01", "principal": _usd("-1"), "interest": _usd("0")}
    assert problem_type(world.member.post(url, json=negative)) == "invalid-payment"
    hidden_txn = world.ledger.transaction(world.hidden, amount="-10")
    linked = {
        "paid_on": "2025-01-01",
        "principal": _usd("1"),
        "interest": _usd("0"),
        "transaction_id": str(hidden_txn.id),
    }
    assert problem_type(world.member.post(url, json=linked)) == "transaction-not-found"


def test_payment_by_amount_is_split_interest_first(world: World, factories: Factories) -> None:
    loan = _post(world.member, LOANS, _mortgage_body(world.mortgage))
    url = f"{LOANS}/{loan['id']}/payments"
    rows = schedule(_terms()).rows
    # The scheduled payment with escrow: split exactly as the schedule does.
    first = _post(world.member, url, {"paid_on": "2024-04-01", "amount": _usd("1869.03")})
    assert first["principal"] == _usd(f"{rows[0].principal}")
    assert first["interest"] == _usd("1276.04")
    assert first["escrow"] == _usd("350.00")
    assert first["fees"] == _usd("0.00")
    assert first["total"] == _usd("1869.03")
    assert first["schedule_check"]["status"] == "ok"
    assert first["balance_after"] == _usd("249757.01")
    # The period's interest and escrow are paid: a second payment is principal.
    extra = _post(world.member, url, {"paid_on": "2024-04-10", "amount": _usd("500.00")})
    assert (extra["principal"], extra["interest"], extra["escrow"]) == (
        _usd("500.00"),
        _usd("0.00"),
        _usd("0.00"),
    )
    assert extra["schedule_check"] == {
        "status": "ok",
        "expected_interest": _usd("0.00"),
        "difference": _usd("0.00"),
    }
    assert extra["balance_after"] == _usd("249257.01")
    assert _balance(world.member, world.mortgage) == "-249257.01"
    # Less than the interest due: all interest; the next payment pays the rest.
    due = period_interest(D("249257.01"), D("0.06125"), "USD")
    assert due == D("1272.25")
    short = _post(world.member, url, {"paid_on": "2024-05-01", "amount": _usd("1000.00")})
    assert (short["principal"], short["interest"], short["escrow"]) == (
        _usd("0.00"),
        _usd("1000.00"),
        _usd("0.00"),
    )
    rest = _post(world.member, url, {"paid_on": "2024-05-02", "amount": _usd("1000.00")})
    assert (rest["principal"], rest["interest"], rest["escrow"]) == (
        _usd("377.75"),
        _usd("272.25"),
        _usd("350.00"),
    )
    assert rest["balance_after"] == _usd("248879.26")
    checks = [p["schedule_check"]["status"] for p in world.viewer.get(url).json()["items"]]
    assert checks == ["ok", "ok", "differs", "ok"]
    audited = household_audit(factories, world.household_id, "loan.payment_add")
    recorded = audited[-1].after
    assert recorded is not None
    assert D(recorded["principal"]) == D("377.75")

    both = {"paid_on": "2024-06-01", "amount": _usd("10"), "interest": _usd("1")}
    assert problem_type(world.member.post(url, json=both)) == "validation-failed"
    neither = {"paid_on": "2024-06-01", "escrow": _usd("10")}
    assert problem_type(world.member.post(url, json=neither)) == "validation-failed"
    zero = {"paid_on": "2024-06-01", "amount": _usd("0")}
    assert problem_type(world.member.post(url, json=zero)) == "invalid-payment"
    euros = {"paid_on": "2024-06-01", "amount": {"amount": "10", "currency": "EUR"}}
    assert problem_type(world.member.post(url, json=euros)) == "currency-mismatch"


def test_payment_by_amount_without_interest_or_escrow_is_all_principal(world: World) -> None:
    body = {
        "account_id": str(world.car.id),
        "principal_original": _usd("12000.00"),
        "rate": "0",
        "term_months": 24,
        "origination_date": "2025-01-15",
    }
    loan = _post(world.member, LOANS, body)
    url = f"{LOANS}/{loan['id']}/payments"
    paid = _post(world.member, url, {"paid_on": "2025-02-15", "amount": _usd("500.00")})
    assert (paid["principal"], paid["interest"], paid["escrow"]) == (
        _usd("500.00"),
        _usd("0.00"),
        _usd("0.00"),
    )
    assert _balance(world.member, world.car) == "-11500.00"


def test_payment_by_amount_on_the_origination_date_is_all_principal(world: World) -> None:
    """No interest has run and no period has started on day zero: no interest, no escrow."""
    loan = _post(world.member, LOANS, _mortgage_body(world.mortgage))
    url = f"{LOANS}/{loan['id']}/payments"
    paid = _post(world.member, url, {"paid_on": "2024-03-01", "amount": _usd("5000.00")})
    assert (paid["principal"], paid["interest"], paid["escrow"]) == (
        _usd("5000.00"),
        _usd("0.00"),
        _usd("0.00"),
    )
    assert paid["schedule_check"]["status"] == "unchecked"
    assert paid["balance_after"] == _usd("245000.00")
    # The first period's interest and escrow are still due in full.
    first = _post(world.member, url, {"paid_on": "2024-04-01", "amount": _usd("1869.03")})
    assert first["interest"] == _usd(f"{period_interest(D('245000.00'), D('0.06125'), 'USD')}")
    assert first["escrow"] == _usd("350.00")
    assert first["schedule_check"]["status"] == "ok"


def test_payment_on_the_origination_date_keeps_the_original_principal(world: World) -> None:
    """Regression: the day before origination has no balance; it is not zero."""
    loan = _post(world.member, LOANS, _mortgage_body(world.mortgage))
    url = f"{LOANS}/{loan['id']}/payments"
    first = _post(
        world.member,
        url,
        {"paid_on": "2024-03-01", "principal": _usd("100.00"), "interest": _usd("0.00")},
    )
    assert first["balance_after"] == _usd("249900.00")
    assert _balance(world.member, world.mortgage) == "-249900.00"
    # No interest has run by day zero: there is nothing to compare with.
    assert first["schedule_check"] == {
        "status": "unchecked",
        "expected_interest": None,
        "difference": None,
    }
    # A second payment that day adds up; fees alone leave the principal alone.
    second = _post(
        world.member,
        url,
        {"paid_on": "2024-03-01", "principal": _usd("50.00"), "interest": _usd("0.00")},
    )
    fees = _post(
        world.member,
        url,
        {
            "paid_on": "2024-03-01",
            "principal": _usd("0.00"),
            "interest": _usd("0.00"),
            "fees": _usd("25.00"),
        },
    )
    assert second["balance_after"] == fees["balance_after"] == _usd("249850.00")
    assert _balance(world.member, world.mortgage) == "-249850.00"
    # Later payments chain from the origination-day balance.
    rows = schedule(_terms()).rows
    later = _post(
        world.member,
        url,
        {
            "paid_on": "2024-04-01",
            "principal": _usd(f"{rows[0].principal}"),
            "interest": _usd("1275.28"),
        },
    )
    assert later["balance_after"] == _usd(f"{D('249850.00') - rows[0].principal}")
    assert later["schedule_check"]["status"] == "ok"
    # Without the origination-day payments the original principal is back.
    for payment in (first, second, fees, later):
        assert world.member.delete(f"{url}/{payment['id']}").status_code == 204
    assert _balance(world.member, world.mortgage) == "-250000.00"


def test_balance_recorded_before_origination_beats_the_original_principal(world: World) -> None:
    """A manual balance dated before origination is where that day's payments start from."""
    loan = _post(world.member, LOANS, _mortgage_body(world.mortgage))
    snapshot = world.owner.post(
        f"{API}/accounts/{world.mortgage.id}/balances",
        json={"as_of_date": "2024-02-29", "balance": _usd("-240000.00")},
    )
    assert snapshot.status_code in (200, 201), snapshot.text
    payment = _post(
        world.member,
        f"{LOANS}/{loan['id']}/payments",
        {"paid_on": "2024-03-01", "principal": _usd("100.00"), "interest": _usd("1225.00")},
    )
    assert payment["balance_after"] == _usd("239900.00")
    assert _balance(world.member, world.mortgage) == "-239900.00"
    # A balance is known for the day before, so the interest is checked against it.
    assert payment["schedule_check"] == {
        "status": "ok",
        "expected_interest": _usd("1225.00"),
        "difference": _usd("0.00"),
    }


def test_simulate_extra_payments(world: World) -> None:
    loan = _post(world.member, LOANS, _mortgage_body(world.mortgage))
    url = f"{LOANS}/{loan['id']}/simulate"
    plain = _post(world.viewer, url, {}, 200)
    assert plain["interest_saved"] == _usd("0.00")
    assert plain["payments_saved"] == 0
    assert plain["baseline"] == plain["with_extras"]
    assert plain["schedule"] is None
    result = _post(
        world.viewer,
        url,
        {
            "one_off": [{"on": "2026-08-15", "amount": _usd("10000")}],
            "recurring": {"amount": _usd("250"), "start": "2026-07-01"},
            "include_schedule": True,
        },
        200,
    )
    assert result["as_of"] == "2026-06-30"
    assert result["baseline"]["payoff_date"] == "2054-03-01"
    assert result["with_extras"]["payoff_date"] < "2045-01-01"
    assert D(result["interest_saved"]["amount"]) > D("100000")
    assert result["payments_saved"] == (
        result["baseline"]["payments"] - result["with_extras"]["payments"]
    )
    rows = result["schedule"]
    assert rows[0]["extra"] == _usd("250.00")
    assert rows[2]["due_date"] == "2026-09-01"
    assert rows[2]["extra"] == _usd("10250.00")
    total = sum(D(r["principal"]["amount"]) + D(r["extra"]["amount"]) for r in rows)
    assert total == D(result["balance"]["amount"])
    euro = {"recurring": {"amount": {"amount": "1", "currency": "EUR"}}}
    assert problem_type(world.viewer.post(url, json=euro)) == "currency-mismatch"
    zero = {"one_off": [{"on": "2026-07-01", "amount": _usd("0")}]}
    assert problem_type(world.viewer.post(url, json=zero)) == "invalid-extra-payment"


def test_variable_rate_changes(world: World, factories: Factories) -> None:
    body = _mortgage_body(
        world.car,
        principal_original=_usd("30000.00"),
        rate="0.05",
        rate_type="variable",
        term_months=60,
        origination_date="2025-01-15",
        payment_day=15,
        escrow_amount=None,
    )
    loan = _post(world.member, LOANS, body)
    url = f"{LOANS}/{loan['id']}/rate-changes"
    change = _post(world.member, url, {"effective_date": "2026-01-15", "new_rate": "0.07"})
    assert change["new_rate"] == "0.07"
    assert (
        problem_type(
            world.member.post(url, json={"effective_date": "2026-01-15", "new_rate": "0.08"})
        )
        == "rate-change-exists"
    )
    early = {"effective_date": "2025-01-01", "new_rate": "0.08"}
    assert problem_type(world.member.post(url, json=early)) == "invalid-rate-change"
    rows = world.viewer.get(f"{LOANS}/{loan['id']}/schedule").json()["rows"]
    assert rows[11]["rate"] == "0.05"
    assert rows[12]["rate"] == "0.07"
    assert rows[12]["payment"] != rows[11]["payment"]
    total = sum(D(r["principal"]["amount"]) for r in rows)
    assert total == D("30000.00")
    assert world.viewer.get(url).json()["items"] == [change]
    assert world.member.delete(f"{url}/{change['id']}").status_code == 204
    rows = world.viewer.get(f"{LOANS}/{loan['id']}/schedule").json()["rows"]
    assert {r["rate"] for r in rows} == {"0.05"}
    assert len(household_audit(factories, world.household_id, "loan.rate_change_delete")) == 1


def test_update_and_delete_loan(world: World, factories: Factories) -> None:
    loan = _post(world.member, LOANS, _mortgage_body(world.mortgage))
    url = f"{LOANS}/{loan['id']}"
    changed = world.member.patch(url, json={"principal_original": _usd("200000.00")})
    assert changed.status_code == 200, changed.text
    assert _balance(world.member, world.mortgage) == "-200000.00"
    cleared = world.member.patch(url, json={"escrow_amount": None, "lender": None}).json()
    assert cleared["escrow_amount"] is None
    assert cleared["lender"] is None
    assert world.member.patch(url, json={"rate": None}).status_code == 422
    low = world.member.patch(url, json={"payment_amount": _usd("100.00")})
    assert problem_type(low) == "loan-payment-too-low"
    _post(
        world.member,
        f"{url}/payments",
        {"paid_on": "2024-04-01", "principal": _usd("300"), "interest": _usd("1000")},
    )
    late = world.member.patch(url, json={"origination_date": "2024-05-01"})
    assert problem_type(late) == "invalid-payment-date"
    assert world.member.delete(url).status_code == 204
    assert problem_type(world.member.get(url)) == "loan-not-found"
    # The computed balances went with the loan; the account stays.
    assert _balance(world.member, world.mortgage) == "0.00"
    assert len(household_audit(factories, world.household_id, "loan.delete")) == 1
