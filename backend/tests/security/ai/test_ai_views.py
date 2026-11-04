# SPDX-License-Identifier: AGPL-3.0-or-later
"""The AI views end to end (AI_ACCESS.md M1, M2, M4, M6; SECURITY.md E1, E3, E4).

One household full of planted identity data (a member's name as a payee, an
account's last four digits, an address in a manual asset, phone numbers and
masked account numbers in memos, an e-mail in a note, the custom denylist in
a payee) is read through **every** AI view; no response may contain any of
the planted values, and every call must be in ``ai_call`` byte for byte.
Then the modes and options (Aggregates, rounding, hidden nicknames and
merchant location, the date cap), the token rules (revoked, demoted owner,
other household, confinement) and the per-client rate limit.
"""

from __future__ import annotations

import json
import re
import secrets
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.auth.models import HouseholdMember, HouseholdRole, User
from app.auth.ratelimit import Limit
from app.core import clock
from app.core.settings import Settings
from app.db.models import Household
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.ai import service as ai_service
from app.domain.ai.models import AiCall, AiMode
from app.domain.ai.service import AiLimits
from app.main import create_app
from tests.fixtures.ai import ai_client, ai_headers, owner_principal
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.ledger import (
    LedgerFactories,
    make_account,
    make_budget,
    make_category,
    make_goal,
    make_goal_contribution,
    make_payee,
    make_scheduled_transaction,
    make_snapshot,
    make_tag,
    make_transaction,
    tag_transaction,
)
from tests.fixtures.tax import make_checklist_item, make_tax_year
from tests.fixtures.wealth import (
    make_holding,
    make_investment_txn,
    make_loan,
    make_manual_asset,
    make_price,
    make_security,
)

pytestmark = [pytest.mark.security, pytest.mark.integration]

API = "/api/v1/ai/v1"
TODAY = clock.utcnow().date()
RECENT = TODAY - timedelta(days=10)
OLD = TODAY - timedelta(days=200)

#: Values planted in the household that no AI response may contain.
PLANTED = (
    "Jane",
    "Doe",
    "jdoe42",
    "jane.doe@example.org",
    "Smithers",
    "Robert",
    "rsmithers",
    "9876",  # account.number_last4
    "Evergreen",
    "742",  # manual asset address
    "5678",  # masked account number in a memo
    "4111",  # card number in a note
    "5551234567",
    "555-123-4567",
    "Oakwood",  # custom denylist
    "kim@example.com",
    "123-45-6789",
    "9988776",  # a number in a security's name (P6B-SR-R1-01)
    "7788",  # a single-star masked card in a nickname (P6B-SR-R1-02)
    "3344",  # last-4 forms in a memo (P6B-SR-R1-02)
    "4455",
    "2211",
    "9012",
    "Wolcott",  # the household's (family) name (P6B-SR-R1-04)
    # P6B-SR-R2-01: account digits in grouped, bracketed and glued shapes and
    # a bare stored last four in an account's own name.
    "5432",  # second group of "Account 9876 5432"; also that account's last four
    "6611",  # "Sapphire 6611": the card's stored number_last4
    "3456",  # "acct 12 3456"
    "4419",  # "routing 0217 4419 3"
    "2299",  # "VISA (2299)"
    "2468",  # "visa2468"
    "1357",  # "AMEX·1357"
    "7722",  # "Card 4111 7722" (a payee)
    "8844",  # "loan 3300 8844" (a category)
    "3399",  # "Account 1100 3399" (a security's name)
    "7766",  # "Card 8800 7766" (a security's sector)
)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def leaks(body: str, extra: tuple[str, ...] = ()) -> list[str]:
    text = _UUID.sub("<id>", body).casefold()
    found = []
    for value in PLANTED + extra:
        needle = value.casefold()
        if needle.isdigit():
            if re.search(rf"(?<!\d){re.escape(needle)}(?!\d)", text):
                found.append(value)
        elif needle in text:
            found.append(value)
    return found


@dataclass
class World:
    owner: UserRecord
    household_id: uuid.UUID
    loan_id: uuid.UUID
    checking_id: uuid.UUID
    household_name: str
    usernames: tuple[str, ...]


def build_world(factories: Factories, ledger: LedgerFactories) -> World:
    suffix = secrets.token_hex(3)
    household_name = f"The Wolcott{suffix} Family"
    owner = factories.user(
        role="owner",
        with_totp=True,
        username=f"jdoe42{suffix}",
        display_name="Jane Q. Doe",
        email="jane.doe@example.org",
    )
    household_id = owner.household_id
    assert household_id is not None
    assert owner.household is not None
    with factories.transaction() as db:
        set_context(db, household_id)
        household = db.get(Household, household_id)
        assert household is not None
        household.name = household_name
    factories.user(
        role="member",
        household=owner.household,
        username=f"rsmithers{suffix}",
        display_name="Robert Smithers",
    )

    def plant(db: Any, hid: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
        groceries = make_category(db, hid, name="Groceries")
        gifts = make_category(db, hid, name="Gifts for Jane")
        checking = make_account(
            db,
            hid,
            type=AccountType.CHECKING,
            name="Chase Checking 9876",
            nickname="Jane's everyday",
            number_last4="9876",
        )
        card = make_account(
            db, hid, type=AccountType.CREDIT_CARD, name="Rewards card", nickname="VISA *7788"
        )
        sapphire = make_account(
            db,
            hid,
            type=AccountType.CREDIT_CARD,
            name="Sapphire 6611",
            nickname="Account 9876 5432",
            number_last4="6611",
        )
        savings = make_account(
            db, hid, type=AccountType.SAVINGS, name="Savings (2299)", number_last4="5432"
        )
        make_snapshot(db, checking, as_of_date=OLD, balance="5000.00")
        member_payee = make_payee(db, hid, name="Jane Doe")
        clinic = make_payee(db, hid, name="Oakwood Clinic")
        trust = make_payee(db, hid, name=f"WOLCOTT{suffix} FAMILY TRUST")
        tag = make_tag(db, hid, name=f"for rsmithers{suffix}")
        rows = [
            make_transaction(
                db,
                checking,
                amount="-250.00",
                posted_date=RECENT,
                payee=member_payee,
                memo="ZELLE TO JANE DOE ACCT ...5678",
                category=gifts,
            ),
            make_transaction(
                db,
                card,
                amount="-4.75",
                posted_date=RECENT,
                raw_payee="STARBUCKS #1234 SEATTLE WA",
                notes="receipt to kim@example.com",
                category=groceries,
            ),
            make_transaction(
                db,
                checking,
                amount="-80.00",
                posted_date=RECENT,
                raw_payee="CITY UTILITIES",
                memo="call 555-123-4567 card 4111 1111 1111 1111",
            ),
            make_transaction(
                db,
                checking,
                amount="-120.00",
                posted_date=RECENT,
                payee=clinic,
                notes="Robert Smithers copay, SSN 123-45-6789",
            ),
            make_transaction(
                db,
                checking,
                amount="3000.00",
                posted_date=RECENT,
                raw_payee="PAYROLL Smithers",
                memo="Deposit to 742 Evergreen Terrace",
            ),
            make_transaction(
                db, card, amount="-60.00", posted_date=OLD, raw_payee="OLD SHOP", category=groceries
            ),
            make_transaction(
                db,
                checking,
                amount="-45.00",
                posted_date=RECENT,
                payee=trust,
                memo="VISA *3344 AMEX 4455 x 2211 1234/5678/9012",
            ),
        ]
        grouped_payee = make_payee(db, hid, name="Card 4111 7722")
        grouped_category = make_category(db, hid, name="loan 3300 8844")
        rows += [
            make_transaction(
                db,
                sapphire,
                amount="-12.00",
                posted_date=RECENT,
                payee=grouped_payee,
                category=grouped_category,
                memo="acct 12 3456 routing 0217 4419 3",
            ),
            make_transaction(
                db,
                savings,
                amount="-13.00",
                posted_date=RECENT,
                raw_payee="VISA (2299)",
                notes="visa2468 AMEX·1357 paid from 6611",
            ),
        ]
        tag_transaction(db, rows[0], tag)
        make_manual_asset(db, hid, description="742 Evergreen Terrace, Springfield")
        loan = make_loan(db, hid, lender="Placeholder Home Lending for Jane Doe")
        make_scheduled_transaction(
            db, checking, name="ZELLE TO JANE DOE", payee=member_payee, next_due=TODAY
        )
        make_budget(
            db,
            hid,
            name="Jane's budget",
            start_date=date(TODAY.year, 1, 1),
            lines={groceries.id: "400.00"},
        )
        goal = make_goal(db, hid, name="Robert's college", accounts=[checking])
        make_goal_contribution(db, goal, contributed_on=RECENT)
        brokerage = make_account(db, hid, type=AccountType.BROKERAGE, name="Brokerage of Jane")
        security = make_security(db, hid, symbol="SYNTH", name="Synthetic Index Fund")
        make_investment_txn(db, brokerage, security=security, trade_date=OLD)
        make_holding(db, brokerage, security, as_of=OLD)
        make_price(db, security, close="110.00", price_date=RECENT)
        # Identity in a security's name and sector (members create securities;
        # OFX SECNAME names them): P6B-SR-R1-01.
        planted = make_security(
            db,
            hid,
            symbol="JDTR",
            name="Jane Doe Family Trust 9988776",
            sector="Smithers Oakwood Clinic",
        )
        make_investment_txn(db, brokerage, security=planted, trade_date=OLD)
        make_holding(db, brokerage, planted, as_of=OLD)
        make_price(db, planted, close="25.00", price_date=RECENT)
        grouped = make_security(
            db, hid, symbol="GRPD", name="Account 1100 3399", sector="Card 8800 7766"
        )
        make_investment_txn(db, brokerage, security=grouped, trade_date=OLD)
        make_holding(db, brokerage, grouped, as_of=OLD)
        make_price(db, grouped, close="3.00", price_date=RECENT)
        year = make_tax_year(db, hid, year=TODAY.year - 1)
        make_checklist_item(db, year, expected_form="W-2", issuer="Smithers & Doe LLC")
        make_checklist_item(db, year, expected_form="1099-INT", issuer="Oakwood Clinic")
        return loan.id, checking.id

    loan_id, checking_id = ledger.run(plant, household_id)
    return World(
        owner,
        household_id,
        loan_id,
        checking_id,
        household_name,
        (f"jdoe42{suffix}", f"rsmithers{suffix}"),
    )


@pytest.fixture
def world(factories: Factories, ledger: LedgerFactories) -> World:
    return build_world(factories, ledger)


@pytest.fixture
def app_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


def every_call(world: World) -> list[tuple[str, dict[str, Any]]]:
    year = str(TODAY.year)
    return [
        ("/policy", {}),
        ("/overview", {"period": "last_12_months"}),
        ("/spending", {"period": year}),
        ("/spending", {"period": year, "group_by": "payee"}),
        ("/spending", {"period": year, "group_by": "account"}),
        ("/spending", {"period": year, "group_by": "month"}),
        ("/income", {"period": year}),
        ("/trends", {"metric": "spending", "months": 12}),
        ("/budgets/status", {}),
        ("/goals", {}),
        ("/recurring", {}),
        ("/net-worth/history", {"range": "1y"}),
        ("/allocation", {}),
        ("/accounts", {}),
        ("/transactions", {"period": "last_90_days"}),
        ("/transactions", {"date_from": (TODAY - timedelta(days=400)).isoformat()}),
        ("/anomalies", {"period": "last_90_days"}),
        ("/holdings", {}),
        ("/performance", {"range": "1y"}),
        ("/loans", {}),
        (f"/loans/{world.loan_id}/schedule", {"limit": 24}),
        (f"/loans/{world.loan_id}/simulate", {"extra_monthly": "200"}),
        (f"/tax/{TODAY.year - 1}/summary", {}),
    ]


def calls_of(factories: Factories, world: World) -> list[AiCall]:
    with factories.transaction() as db:
        set_context(db, world.household_id)
        rows = list(
            db.execute(
                select(AiCall)
                .where(AiCall.household_id == world.household_id)
                .order_by(AiCall.created_at, AiCall.id)
            ).scalars()
        )
        for row in rows:
            db.expunge(row)
    return rows


# ---------------------------------------------------------------------------
# No identity in any view, and the full audit
# ---------------------------------------------------------------------------


def test_no_planted_identity_leaves_through_any_view(
    app_client: TestClient, factories: Factories, world: World
) -> None:
    _, raw = ai_client(factories, world.owner, custom_denylist=["Oakwood Clinic"])
    bodies: list[tuple[str, str]] = []
    for path, params in every_call(world):
        response = app_client.get(API + path, params=params, headers=ai_headers(raw))
        assert response.status_code == 200, (path, response.text)
        assert response.headers["cache-control"] == "no-store"
        bodies.append((path, response.text))
    problems = {
        path: leaks(body, world.usernames) for path, body in bodies if leaks(body, world.usernames)
    }
    assert problems == {}
    assert all(world.household_name.casefold() not in b.casefold() for _, b in bodies)
    # The scrubber really ran (the planted values became placeholders).
    joined = "\n".join(b for _, b in bodies)
    for placeholder in ("[MEMBER]", "[ACCT]", "[PHONE]", "[EMAIL]", "[ADDRESS]", "[REDACTED]"):
        assert placeholder in joined, placeholder

    # M4: every call is in ai_call with its parameters and the exact body.
    rows = calls_of(factories, world)
    assert len(rows) == len(bodies)
    for row, (path, body) in zip(rows, bodies, strict=True):
        assert row.status == 200
        assert row.bytes == len(body.encode())
        assert row.response == json.loads(body), path
    assert rows[2].tool == "get_spending"
    assert rows[2].params == {
        "depth": 1,
        "group_by": "category",
        "period": str(TODAY.year),
    }


def test_detail_view_contents(app_client: TestClient, factories: Factories, world: World) -> None:
    _, raw = ai_client(factories, world.owner)
    headers = ai_headers(raw)
    accounts = app_client.get(f"{API}/accounts", headers=headers).json()
    checking = next(a for a in accounts["items"] if a["id"] == str(world.checking_id))
    assert checking["label"] == "[MEMBER]'s everyday"
    assert checking["type"] == "checking"
    assert set(checking) == {
        "id",
        "label",
        "type",
        "asset_kind",
        "institution",
        "currency",
        "balance",
        "is_asset",
        "closed",
    }
    txns = app_client.get(
        f"{API}/transactions", params={"period": "last_90_days"}, headers=headers
    ).json()["items"]
    starbucks = next(t for t in txns if t["payee"] and "STARBUCKS" in t["payee"])
    assert starbucks["payee"] == "STARBUCKS #1234 SEATTLE WA"
    assert (starbucks["merchant_city"], starbucks["merchant_state"]) == ("SEATTLE", "WA")
    zelle = next(t for t in txns if t["amount"]["amount"] == "-250.00")
    assert zelle["payee"] == "[MEMBER]"
    assert zelle["memo"] == "ZELLE TO [MEMBER] [ACCT]"
    assert zelle["category"] == "Gifts for [MEMBER]"
    assert zelle["tags"] == ["for [MEMBER]"]
    # Filters match canonical names as the client sees them (scrubbed).
    by_payee = app_client.get(
        f"{API}/transactions",
        params={"period": "last_90_days", "payee": "[member]"},
        headers=headers,
    ).json()["items"]
    assert [t["amount"]["amount"] for t in by_payee] == ["-250.00"]
    by_category = app_client.get(
        f"{API}/transactions",
        params={"period": "last_90_days", "category": "groceries"},
        headers=headers,
    ).json()["items"]
    assert {t["amount"]["amount"] for t in by_category} == {"-4.75"}
    pattern = app_client.get(
        f"{API}/transactions", params={"period": "last_90_days", "payee": "%"}, headers=headers
    ).json()["items"]
    assert pattern == []
    loans = app_client.get(f"{API}/loans", headers=headers).json()["items"]
    assert loans[0]["lender"] == "Placeholder Home Lending for [MEMBER]"
    assert "number" not in json.dumps(loans)


def test_name_filters_do_not_confirm_scrubbed_identity(
    app_client: TestClient, factories: Factories, world: World
) -> None:
    """P6B-SR-R1-03: guessing a hidden name through a filter answers like a miss."""
    _, raw = ai_client(factories, world.owner, custom_denylist=["Oakwood Clinic"])
    headers = ai_headers(raw)

    def amounts(**params: str) -> list[str]:
        response = app_client.get(
            f"{API}/transactions", params={"period": "last_90_days", **params}, headers=headers
        )
        assert response.status_code == 200, response.text
        return [t["amount"]["amount"] for t in response.json()["items"]]

    miss = amounts(payee="Nobody At All")
    assert miss == []
    # Right guesses of a member's name and of a denylisted term: same as a miss.
    for guess in ("Jane Doe", "jane doe", "JANE  DOE", "Oakwood Clinic", "oakwood clinic"):
        assert amounts(payee=guess) == miss, guess
    for guess in ("Gifts for Jane", "gifts for jane"):
        assert amounts(category=guess) == miss, guess
    # What the client was shown still filters (it learns nothing new).
    assert amounts(category="Gifts for [MEMBER]") == ["-250.00"]
    assert amounts(payee="[REDACTED]") == ["-120.00"]
    # A wrong guess at the placeholder's content is a miss too.
    assert amounts(category="Gifts for John") == miss


def test_payee_filter_does_not_confirm_a_hidden_location(
    app_client: TestClient, factories: Factories, ledger: LedgerFactories, world: World
) -> None:
    """P6B-SR-R2-03: with the location hidden, the payee filter compares the
    shown name; a guessed city and state answer like a miss."""

    def plant(db: Any, hid: uuid.UUID) -> None:
        set_context(db, hid)
        checking = db.get(Account, world.checking_id)
        assert checking is not None
        hardware = make_payee(db, hid, name="ACME HARDWARE SPRINGFIELD IL")
        make_transaction(db, checking, amount="-33.00", posted_date=RECENT, payee=hardware)

    ledger.run(plant, world.household_id)
    _, raw = ai_client(factories, world.owner, hide_merchant_location=True)
    headers = ai_headers(raw)

    def amounts(payee: str) -> list[str]:
        response = app_client.get(
            f"{API}/transactions",
            params={"period": "last_90_days", "payee": payee},
            headers=headers,
        )
        assert response.status_code == 200, response.text
        return [t["amount"]["amount"] for t in response.json()["items"]]

    shown = app_client.get(
        f"{API}/transactions", params={"period": "last_90_days"}, headers=headers
    ).json()["items"]
    assert "ACME HARDWARE" in {t["payee"] for t in shown}
    assert all("SPRINGFIELD" not in (t["payee"] or "") for t in shown)
    miss = amounts("Nobody At All")
    assert miss == []
    for guess in ("ACME HARDWARE SPRINGFIELD IL", "acme hardware springfield il"):
        assert amounts(guess) == miss, guess
    # The name the client was shown still filters.
    assert amounts("ACME HARDWARE") == ["-33.00"]
    assert amounts("acme hardware") == ["-33.00"]


def test_security_names_and_sectors_are_scrubbed(
    app_client: TestClient, factories: Factories, world: World
) -> None:
    """P6B-SR-R1-01: free text in securities goes through the scrubber."""
    _, raw = ai_client(factories, world.owner, custom_denylist=["Oakwood Clinic"])
    holdings = app_client.get(f"{API}/holdings", headers=ai_headers(raw)).json()["items"]
    trust = next(h for h in holdings if h["symbol"] == "JDTR")
    assert trust["name"] == "[MEMBER] Family Trust [ACCT]"
    assert trust["sector"] == "[MEMBER] [REDACTED]"
    allocation = app_client.get(f"{API}/allocation", headers=ai_headers(raw)).json()
    assert "[MEMBER] [REDACTED]" in {r["key"] for r in allocation["by"]["sector"]}
    _, agg = ai_client(
        factories, world.owner, mode=AiMode.AGGREGATES, custom_denylist=["Oakwood Clinic"]
    )
    allocation = app_client.get(f"{API}/allocation", headers=ai_headers(agg)).text
    assert leaks(allocation) == []


# ---------------------------------------------------------------------------
# Modes and options (E3, M8)
# ---------------------------------------------------------------------------


def test_aggregates_mode(app_client: TestClient, factories: Factories, world: World) -> None:
    _, raw = ai_client(factories, world.owner, mode=AiMode.AGGREGATES)
    headers = ai_headers(raw)
    for path in ("/transactions", "/recurring", "/holdings", "/loans", "/anomalies"):
        response = app_client.get(API + path, headers=headers)
        assert response.status_code == 403, path
        assert response.json()["type"] == "urn:perfin:problem:not-in-aggregates-mode"
    payee = app_client.get(f"{API}/spending", params={"group_by": "payee"}, headers=headers)
    assert payee.status_code == 403
    accounts = app_client.get(f"{API}/accounts", headers=headers).json()
    assert accounts["mode"] == "aggregates"
    assert accounts["items"] == []
    assert {t["type"] for t in accounts["by_type"]} >= {"checking", "credit_card"}
    by_account = app_client.get(
        f"{API}/spending",
        params={"group_by": "account", "period": str(TODAY.year)},
        headers=headers,
    ).json()
    assert by_account["group_by"] == "account_type"
    assert {i["name"] for i in by_account["items"]} <= {t.value for t in AccountType}
    goals = app_client.get(f"{API}/goals", headers=headers).json()["items"]
    assert goals[0]["name"] == "Goal A"
    summary = app_client.get(f"{API}/tax/{TODAY.year - 1}/summary", headers=headers).json()
    assert all(item["issuer"] is None for item in summary["checklist"])
    # Refusals are audited too.
    statuses = [r.status for r in calls_of(factories, world)]
    assert statuses.count(403) == 6


def test_rounding_hidden_nicknames_and_location(
    app_client: TestClient, factories: Factories, world: World
) -> None:
    _, raw = ai_client(
        factories, world.owner, rounding=100, hide_nicknames=True, hide_merchant_location=True
    )
    headers = ai_headers(raw)
    accounts = app_client.get(f"{API}/accounts", headers=headers).json()["items"]
    labels = {a["label"] for a in accounts}
    assert {"Checking A", "Credit card A"} <= labels
    for a in accounts:
        assert Decimal(a["balance"]["amount"]) % 100 == 0
        assert "." not in a["balance"]["amount"]
    txns = app_client.get(
        f"{API}/transactions", params={"period": "last_90_days"}, headers=headers
    ).json()["items"]
    coffee = next(t for t in txns if t["payee"] and "STARBUCKS" in t["payee"])
    assert coffee["payee"] == "STARBUCKS #1234"
    assert coffee["merchant_city"] is None
    assert coffee["merchant_state"] is None
    assert coffee["amount"]["amount"] == "0"  # -4.75 to the nearest 100
    assert all(t["account"] in labels for t in txns)
    policy = app_client.get(f"{API}/policy", headers=headers).json()
    assert policy["rounding"] == 100
    assert policy["hide_nicknames"] is True


def test_date_range_cap(app_client: TestClient, factories: Factories, world: World) -> None:
    _, raw = ai_client(factories, world.owner, date_range_cap=30)
    headers = ai_headers(raw)
    response = app_client.get(
        f"{API}/transactions",
        params={"date_from": (TODAY - timedelta(days=400)).isoformat()},
        headers=headers,
    )
    body = response.json()
    assert date.fromisoformat(body["date_from"]) == TODAY - timedelta(days=30)
    assert all(
        date.fromisoformat(t["posted_date"]) >= TODAY - timedelta(days=30) for t in body["items"]
    )
    assert not any(t["payee"] == "OLD SHOP" for t in body["items"])
    before = app_client.get(
        f"{API}/spending",
        params={"date_from": OLD.isoformat(), "date_to": (OLD + timedelta(days=5)).isoformat()},
        headers=headers,
    )
    assert before.status_code == 422
    assert before.json()["type"] == "urn:perfin:problem:outside-date-range"
    too_long = app_client.get(f"{API}/performance", params={"range": "max"}, headers=headers)
    assert too_long.status_code == 422
    history = app_client.get(f"{API}/net-worth/history", params={"range": "1y"}, headers=headers)
    first = TODAY - timedelta(days=30)
    assert all(date.fromisoformat(p["as_of"]) >= first for p in history.json()["points"])


def test_bounded_periods(app_client: TestClient, factories: Factories, world: World) -> None:
    _, raw = ai_client(factories, world.owner)
    headers = ai_headers(raw)
    long = app_client.get(
        f"{API}/spending",
        params={"date_from": "2000-01-01", "date_to": "2026-01-01"},
        headers=headers,
    )
    assert long.status_code == 422
    bad = app_client.get(f"{API}/spending", params={"period": "forever"}, headers=headers)
    assert bad.status_code == 422
    huge = app_client.get(f"{API}/transactions", params={"limit": 10_000}, headers=headers)
    assert huge.status_code == 422


# ---------------------------------------------------------------------------
# Tokens (M4, E4)
# ---------------------------------------------------------------------------


def test_only_an_ai_client_token_reads_the_views(
    app_client: TestClient, factories: Factories, world: World
) -> None:
    # A session of the owner, and a personal token, are refused.
    login = factories.login(app_client, world.owner)
    assert login is not None
    response = app_client.get(f"{API}/accounts")
    assert response.status_code == 403
    assert response.json()["type"] == "urn:perfin:problem:ai-client-required"
    app_client.cookies.clear()
    pat = factories.token(world.owner, scopes=["accounts:read"])
    response = app_client.get(f"{API}/accounts", headers=ai_headers(pat))
    assert response.json()["type"] == "urn:perfin:problem:ai-client-required"


def test_ai_token_is_confined_to_the_ai_views(
    app_client: TestClient, factories: Factories, world: World
) -> None:
    _, raw = ai_client(factories, world.owner)
    for path in (
        "/api/v1/accounts",
        "/api/v1/auth/session",
        "/api/v1/households",
        "/api/v1/transactions",
        "/api/v1/documents",
    ):
        response = app_client.get(path, headers=ai_headers(raw))
        assert response.status_code == 403, path
        assert response.json()["type"] == "urn:perfin:problem:ai-token-confined"


def test_revoked_client_and_demoted_owner(
    app_client: TestClient, factories: Factories, world: World, settings: Settings
) -> None:
    client, raw = ai_client(factories, world.owner)
    assert app_client.get(f"{API}/policy", headers=ai_headers(raw)).status_code == 200
    with factories.transaction() as db:
        set_context(db, world.household_id, world.owner.id)
        ai_service.revoke_client(db, owner_principal(world.owner), client.id)
    response = app_client.get(f"{API}/policy", headers=ai_headers(raw))
    assert response.status_code == 401  # the token was revoked with the client

    _, raw2 = ai_client(factories, world.owner)
    with factories.transaction() as db:
        set_context(db, world.household_id)
        member = db.execute(
            select(HouseholdMember).where(HouseholdMember.user_id == world.owner.id)
        ).scalar_one()
        member.role = HouseholdRole.ADMIN
    response = app_client.get(f"{API}/policy", headers=ai_headers(raw2))
    assert response.status_code == 403
    assert response.json()["type"] == "urn:perfin:problem:insufficient-scope"
    still_confined = app_client.get("/api/v1/accounts", headers=ai_headers(raw2))
    assert still_confined.json()["type"] == "urn:perfin:problem:ai-token-confined"


def test_token_reads_only_its_household(
    app_client: TestClient, factories: Factories, ledger: LedgerFactories, world: World
) -> None:
    other = factories.user(role="owner", with_totp=True)
    _, raw = ai_client(factories, other)
    for path in (f"/loans/{world.loan_id}/schedule", f"/loans/{world.loan_id}/simulate"):
        response = app_client.get(API + path, headers=ai_headers(raw))
        assert response.status_code == 404, path
        assert response.json()["type"] == "urn:perfin:problem:loan-not-found"
    accounts = app_client.get(f"{API}/accounts", headers=ai_headers(raw)).json()
    assert accounts["items"] == []
    foreign = app_client.get(
        f"{API}/transactions",
        params={"account_id": str(world.checking_id)},
        headers=ai_headers(raw),
    )
    assert foreign.status_code == 404


def test_only_the_owner_on_a_stepped_up_session_creates_clients(
    factories: Factories, world: World, settings: Settings
) -> None:
    from app.core.errors import ProblemError
    from app.domain.ai.service import ClientOptions, create_client

    principal = owner_principal(world.owner)
    stale = type(principal)(
        **{**principal.__dict__, "mfa_verified_at": clock.utcnow() - timedelta(minutes=11)}
    )
    admin = type(principal)(**{**principal.__dict__, "role": HouseholdRole.ADMIN})
    token = type(principal)(**{**principal.__dict__, "auth_method": "token"})
    with factories.transaction() as db:
        set_context(db, world.household_id, world.owner.id)
        for who, slug in (
            (stale, "step-up-required"),
            (admin, "forbidden"),
            (token, "session-required"),
        ):
            with pytest.raises(ProblemError) as caught:
                create_client(db, who, ClientOptions(name="x"), settings=settings)
            assert caught.value.slug == slug
        with pytest.raises(ProblemError) as bad:
            create_client(db, principal, ClientOptions(name="x", rounding=5), settings=settings)
        assert bad.value.slug == "invalid-ai-options"
        created = create_client(db, principal, ClientOptions(name="x"), settings=settings)
        assert created.token.scopes == ["ai:read"]
        assert created.raw_token.startswith("pfn_")
        assert created.token.token_hash != created.raw_token


# ---------------------------------------------------------------------------
# Rate limits, cache and search (M6, M4)
# ---------------------------------------------------------------------------


def test_per_client_rate_limit(settings: Settings, factories: Factories, world: World) -> None:
    app = create_app(settings)
    app.state.ai_limits = AiLimits(
        per_minute=Limit("ai:test:minute", 3, timedelta(minutes=1)),
        per_ten_minutes=Limit("ai:test:10min", 100, timedelta(minutes=10)),
    )
    _, raw = ai_client(factories, world.owner)
    _, raw_other = ai_client(factories, world.owner)
    with TestClient(app, base_url="https://testserver") as client:
        codes = [client.get(f"{API}/policy", headers=ai_headers(raw)).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429]
        # Another client of the same household has its own budget.
        assert client.get(f"{API}/policy", headers=ai_headers(raw_other)).status_code == 200


def test_cache_is_keyed_by_the_scrubber_terms(
    app_client: TestClient, factories: Factories, ledger: LedgerFactories, world: World
) -> None:
    """P6B-SR-R1-05: a renamed member is never served a body scrubbed with old terms."""
    _, raw = ai_client(factories, world.owner)

    def plant(db: Any, hid: uuid.UUID) -> None:
        lessons = make_category(db, hid, name="Lessons for Maximilian")
        cash = make_account(db, hid, type=AccountType.CHECKING, name="Cash")
        make_transaction(db, cash, amount="-70.00", posted_date=RECENT, category=lessons)

    ledger.run(plant, world.household_id)
    params = {"period": str(TODAY.year)}
    first = app_client.get(f"{API}/spending", params=params, headers=ai_headers(raw)).text
    assert "Lessons for Maximilian" in first
    # A member is renamed (a new member joining is the same case).
    with factories.transaction() as db:
        user = db.get(User, world.owner.id)
        assert user is not None
        user.display_name = "Maximilian Doe"
    second = app_client.get(f"{API}/spending", params=params, headers=ai_headers(raw)).text
    assert "Maximilian" not in second
    assert "Lessons for [MEMBER]" in second


def test_cached_aggregates_are_still_audited_and_searchable(
    app_client: TestClient, factories: Factories, world: World
) -> None:
    _, raw = ai_client(factories, world.owner)
    params = {"period": str(TODAY.year), "group_by": "payee"}
    first = app_client.get(f"{API}/spending", params=params, headers=ai_headers(raw))
    second = app_client.get(f"{API}/spending", params=params, headers=ai_headers(raw))
    assert first.text == second.text
    rows = calls_of(factories, world)
    assert [r.tool for r in rows] == ["get_spending", "get_spending"]
    with factories.transaction() as db:
        set_context(db, world.household_id, world.owner.id)
        principal = owner_principal(world.owner)
        hits = ai_service.search_calls(db, principal, contains="[member]")
        assert len(hits) == 2
        assert ai_service.search_calls(db, principal, contains="Jane") == []
        assert ai_service.search_calls(db, principal, contains="%") == []
