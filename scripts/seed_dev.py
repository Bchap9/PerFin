#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Seed a development database with a synthetic household (docs/DEVELOPMENT.md, "Dev stack").

Usage (from the repository root; ``scripts/dev.sh`` runs it after the migrations)::

    cd backend && PERFIN_DEV_MODE=true uv run python ../scripts/seed_dev.py

The script refuses to run unless ``PERFIN_DEV_MODE=true`` and unless the
database is either empty or already holds this seed (it never touches a
database that has real users). On an empty database it

* completes first-run setup like the setup page does (one-time token, the
  production code path) and creates the **dev admin** with a **generated**
  password. The username and password are printed **once**, to stdout, and
  stored nowhere; there are no default credentials. To get new ones, reset
  the dev database (``make dev-reset``).
* builds the household through the real HTTP API (an in-process app with a
  short-lived seeding session): accounts of every type, about six months of
  synthetic transactions from ``tests/fixtures/synth_statements.py``,
  rules, a budget, goals, a brokerage and a retirement account with
  holdings, a mortgage and a car loan, manual assets and liabilities, net
  worth history, a tax year with synthetic forms, and a revoked ("disabled")
  AI client example whose token is discarded;
* uploads synthetic documents (a statement, a receipt, a brokerage statement
  and tax forms) through the real upload endpoint and runs the ingestion jobs
  in this process (sandboxed extraction, so Linux), which leaves items in the
  review queue.

A second run finds the seed and changes nothing. Every name, number and
amount is generated; nothing here is real.
"""

from __future__ import annotations

import argparse
import secrets
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402

from app.auth import bootstrap  # noqa: E402
from app.auth.models import HouseholdMember, LoginMethod, SessionStage, User  # noqa: E402
from app.auth.sessions import SessionPolicy, cookie_name, create_session  # noqa: E402
from app.core import clock  # noqa: E402
from app.core.csrf import CSRF_HEADER, CSRFProtect, normalize_origin  # noqa: E402
from app.core.settings import Settings, get_settings  # noqa: E402
from app.db.models import Household  # noqa: E402
from app.db.registry import import_all_models  # noqa: E402
from app.db.rls import set_context  # noqa: E402
from app.db.session import make_app_engine, make_sessionmaker  # noqa: E402
from app.domain.transactions.models import Tag  # noqa: E402
from app.main import API_PREFIX, create_app  # noqa: E402
from tests.fixtures import synth_brokerage as brokerage  # noqa: E402
from tests.fixtures import synth_receipts as receipts  # noqa: E402
from tests.fixtures import synth_statements as synth  # noqa: E402
from tests.fixtures import synth_tax_forms as tax_forms  # noqa: E402

DEV_USERNAME = "dev-admin"
DEV_HOUSEHOLD = "Dev Household"
#: Calendar months of ledger history before today.
HISTORY_MONTHS = 6
USD = "USD"
CENT = Decimal("0.01")
#: A tag the seed creates last; its presence means the seed finished.
MARKER_TAG = "dev-seed"


class SeedRefusedError(Exception):
    """The seed must not run here (not dev mode, or the database holds real users)."""


@dataclass
class SeedResult:
    """What a run did. ``password`` is set only on the run that created the admin."""

    created: bool
    username: str = DEV_USERNAME
    password: str | None = field(default=None, repr=False)
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def usd(amount: str | Decimal) -> dict[str, str]:
    return {"amount": str(amount), "currency": USD}


def month_start(today: date, back: int) -> date:
    """The first day of the month ``back`` months before ``today``'s month."""
    index = today.year * 12 + (today.month - 1) - back
    return date(index // 12, index % 12 + 1, 1)


class Api:
    """The seeding session: a cookie and CSRF headers on an in-process client."""

    def __init__(self, client: TestClient, origin: str, csrf_token: str) -> None:
        self.client = client
        self._headers = {
            CSRF_HEADER: csrf_token,
            "Origin": origin,
            "Sec-Fetch-Site": "same-origin",
        }

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.client.get(f"{API_PREFIX}{path}", **kwargs)

    def send(self, method: str, path: str, expect: tuple[int, ...], **kwargs: Any) -> Any:
        response = self.client.request(
            method, f"{API_PREFIX}{path}", headers=self._headers, **kwargs
        )
        if response.status_code not in expect:
            raise RuntimeError(
                f"seed: {method} {path} answered {response.status_code}: {response.text[:300]}"
            )
        return response.json() if response.content else None

    def post(self, path: str, body: Any = None, **kwargs: Any) -> Any:
        return self.send("POST", path, (200, 201, 202), json=body, **kwargs)

    def put(self, path: str, body: Any) -> Any:
        return self.send("PUT", path, (200, 201), json=body)

    def delete(self, path: str) -> None:
        self.send("DELETE", path, (200, 204))

    def items(self, path: str) -> list[dict[str, Any]]:
        """All rows of a list endpoint (follows ``next_cursor``)."""
        out: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"limit": 200}
            if cursor:
                params["cursor"] = cursor
            response = self.get(path, params=params)
            if response.status_code != 200:
                raise RuntimeError(f"seed: GET {path} answered {response.status_code}")
            body = response.json()
            if isinstance(body, list):
                return [row for row in body if isinstance(row, dict)]
            out.extend(body["items"])
            cursor = body.get("next_cursor")
            if not cursor:
                return out


# ---------------------------------------------------------------------------
# Guards and the admin
# ---------------------------------------------------------------------------


def _seed_present(db: Session) -> bool:
    """True when the database holds a finished seed; refuses anything else that is not empty."""
    users = db.execute(select(User.id, User.username)).all()
    if not users:
        return False
    if [name for _, name in users] != [DEV_USERNAME]:
        raise SeedRefusedError(
            "the database already has users other than the dev admin; the seed only runs on an "
            "empty database or on its own earlier run (make dev-reset starts over)"
        )
    # A user reads their own memberships (the self-read policy).
    set_context(db, None, users[0][0])
    household_id = db.execute(select(HouseholdMember.household_id)).scalars().first()
    if household_id is None:
        raise SeedRefusedError("the dev admin has no household; make dev-reset starts over")
    set_context(db, household_id)
    if db.execute(select(Tag.id).where(Tag.name == MARKER_TAG)).first() is None:
        raise SeedRefusedError(
            "an earlier seed run did not finish; make dev-reset starts over with a clean database"
        )
    return True


def _create_admin(db: Session, settings: Settings) -> tuple[str, Household]:
    """First-run setup through the production code path; returns ``(password, household)``."""
    token = bootstrap.issue_setup_token(db)
    if token is None:
        raise SeedRefusedError("first-run setup has already been completed")
    password = secrets.token_urlsafe(18)
    result = bootstrap.complete_setup(
        db,
        token=token,
        username=DEV_USERNAME,
        password=password,
        display_name="Dev Admin",
        household_name=DEV_HOUSEHOLD,
        base_currency=USD,
    )
    return password, result.household


@dataclass
class Seeding:
    api: Api
    settings: Settings
    sessions: sessionmaker[Session]
    household_id: Any
    today: date
    log: Callable[[str], None]
    counts: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def count(self, what: str, n: int = 1) -> None:
        self.counts[what] = self.counts.get(what, 0) + n


# ---------------------------------------------------------------------------
# Building the household
# ---------------------------------------------------------------------------

#: Merchant noun (from ``synth_statements``) -> default category name.
NOUN_CATEGORY = {
    "Grocers": "Groceries",
    "Fuel": "Fuel",
    "Bakery": "Restaurants",
    "Books": "Hobbies",
    "Pharmacy": "Pharmacy",
    "Hardware": "Household supplies",
    "Cafe": "Coffee and snacks",
}
#: Category path used by ``synth_ledger`` -> default category name.
LEDGER_CATEGORY = {
    "Housing:Rent": "Rent",
    "Income:Salary": "Salary and wages",
    "Food:Coffee": "Coffee and snacks",
    "Food:Groceries": "Groceries",
    "Shopping": "Shopping",
}


def _account(seeding: Seeding, **body: Any) -> dict[str, Any]:
    created: dict[str, Any] = seeding.api.post("/accounts", body)
    seeding.count("accounts")
    return created


def _categories(api: Api) -> dict[str, str]:
    return {row["name"]: row["id"] for row in api.items("/categories")}


def _category_for(txn: synth.SynthTxn, categories: dict[str, str]) -> str | None:
    if txn.category in LEDGER_CATEGORY:
        name = LEDGER_CATEGORY[txn.category]
        if txn.category == "Food:Groceries":
            name = NOUN_CATEGORY.get(txn.payee.rsplit(" ", 1)[-1], name)
        return categories.get(name)
    return None


def build_ledger(seeding: Seeding) -> dict[str, dict[str, Any]]:
    """Cash accounts with months of synthetic transactions, rules, a budget and goals."""
    api, today = seeding.api, seeding.today
    first = month_start(today, HISTORY_MONTHS)
    days = (today - first).days
    categories = _categories(api)
    opened = first - timedelta(days=1)

    accounts = {
        "checking": _account(
            seeding,
            type="checking",
            name="Everyday Checking",
            number_last4="4417",
            opening_balance=usd("4200.00"),
            opening_balance_date=opened.isoformat(),
        ),
        "savings": _account(
            seeding,
            type="savings",
            name="Emergency Savings",
            number_last4="9023",
            opening_balance=usd("9000.00"),
            opening_balance_date=opened.isoformat(),
        ),
        "card": _account(
            seeding,
            type="credit_card",
            name="Rewards Card",
            number_last4="3308",
            opening_balance=usd("0.00"),
            opening_balance_date=opened.isoformat(),
        ),
        "cash": _account(
            seeding,
            type="cash",
            name="Wallet Cash",
            opening_balance=usd("150.00"),
            opening_balance_date=opened.isoformat(),
        ),
    }

    def post_all(account: dict[str, Any], ledger: Sequence[synth.SynthTxn]) -> None:
        for txn in ledger:
            body: dict[str, Any] = {
                "account_id": account["id"],
                "posted_date": txn.posted.isoformat(),
                "amount": usd(txn.amount),
                "raw_payee": txn.payee,
                "memo": txn.memo,
                "status": "cleared",
            }
            category = _category_for(txn, categories)
            if category is not None:
                body["category_id"] = category
            api.post("/transactions", body)
            seeding.count("transactions")

    checking = synth.synth_ledger(1, start=first, days=days)
    savings_side: list[synth.SynthTxn] = []
    for txn in checking:
        if txn.payee == "Transfer to savings":
            savings_side.append(
                synth.SynthTxn(txn.posted, -txn.amount, "Transfer from checking", txn.memo, "")
            )
    post_all(accounts["checking"], checking)
    post_all(accounts["savings"], savings_side)
    post_all(accounts["card"], synth.synth_ledger(2, start=first, days=days, credit_card=True))
    cash = [
        synth.SynthTxn(
            first + timedelta(days=12 * i + 3), -Decimal(f"{9 + 3 * i}.50"), payee, "", ""
        )
        for i, payee in enumerate(("Maple Bakery", "Corner Cafe", "Harbor Fuel", "Lantern Books"))
    ]
    post_all(accounts["cash"], cash)
    api.post("/transactions/transfers/auto-pair", {"window_days": 3})

    # Rules: they show in the rules list and categorise future imports.
    for name, text, category in (
        ("Coffee shops", "cafe", "Coffee and snacks"),
        ("Fuel stations", "fuel", "Fuel"),
        ("Pharmacies", "pharmacy", "Pharmacy"),
    ):
        api.post(
            "/rules",
            {
                "name": name,
                "conditions": {"type": "payee", "op": "contains", "value": text},
                "actions": [{"type": "set_category", "category_id": categories[category]}],
            },
        )
        seeding.count("rules")

    api.post(
        "/budgets",
        {
            "name": "Monthly household budget",
            "period": "monthly",
            "start_date": first.isoformat(),
            "lines": [
                {"category_id": categories[name], "amount": usd(amount)}
                for name, amount in (
                    ("Groceries", "350.00"),
                    ("Restaurants", "120.00"),
                    ("Coffee and snacks", "40.00"),
                    ("Fuel", "90.00"),
                    ("Household supplies", "80.00"),
                    ("Rent", "1450.00"),
                )
            ],
        },
    )
    seeding.count("budgets")

    emergency = api.post(
        "/goals",
        {
            "name": "Emergency fund",
            "target": usd("15000.00"),
            "target_date": date(today.year + 1, 6, 30).isoformat(),
            "account_ids": [accounts["savings"]["id"]],
        },
    )
    api.post(
        "/goals",
        {"name": "Vacation", "target": usd("3000.00"), "account_ids": [accounts["savings"]["id"]]},
    )
    api.post(
        f"/goals/{emergency['id']}/contributions",
        {"amount": usd("250.00"), "contributed_on": (today - timedelta(days=20)).isoformat()},
    )
    seeding.count("goals", 2)
    api.post("/recurring/detect", {})
    return accounts


def build_investments(seeding: Seeding) -> None:
    """A brokerage account (two lots sold in part) and a retirement account."""
    api, today = seeding.api, seeding.today
    brokerage = _account(seeding, type="brokerage", name="Brokerage")
    retirement = _account(seeding, type="retirement", name="Retirement IRA")
    securities = [
        api.post(
            "/securities",
            {"symbol": symbol, "name": name, "asset_class": kind, "currency": USD},
        )
        for symbol, name, kind in (
            ("SYNAAA", "Synthetic Growth Co", "equity"),
            ("SYNBBB", "Synthetic Total Market ETF", "etf"),
            ("SYNCCC", "Synthetic Bond Fund", "mutual_fund"),
        )
    ]
    for security, base in zip(securities, (Decimal(100), Decimal(50), Decimal(20)), strict=True):
        for back in range(8, -1, -1):
            wobble = Decimal((back * 7) % 11 - 5) / 100
            api.post(
                f"/securities/{security['id']}/prices",
                {
                    "price_date": (today - timedelta(days=30 * back)).isoformat(),
                    "close": str((base * (1 + wobble + Decimal(8 - back) / 100)).quantize(CENT)),
                },
            )
    seeding.count("securities", len(securities))
    aaa, bbb, ccc = (s["id"] for s in securities)
    txns = "/investments/transactions"

    def trade(
        account: dict[str, Any], kind: str, back_days: int, amount: str, **extra: Any
    ) -> dict[str, Any]:
        result: dict[str, Any] = api.post(
            txns,
            {
                "account_id": account["id"],
                "type": kind,
                "trade_date": (today - timedelta(days=back_days)).isoformat(),
                "amount": usd(amount),
                **extra,
            },
        )
        seeding.count("investment transactions")
        return result

    trade(brokerage, "buy", 240, "-1000.00", security_id=aaa, quantity="10", price="100")
    second = trade(brokerage, "buy", 180, "-1205.00", security_id=aaa, quantity="10", price="120")
    trade(brokerage, "buy", 200, "-2500.00", security_id=bbb, quantity="50", price="50")
    if second.get("lots"):
        trade(
            brokerage,
            "sell",
            90,
            "650.00",
            security_id=aaa,
            quantity="5",
            price="130",
            lots=[{"lot_id": second["lots"][0]["lot_id"], "quantity": "5"}],
        )
    trade(brokerage, "dividend", 60, "18.40", security_id=bbb)
    api.put(f"/investments/accounts/{retirement['id']}/cost-method", {"method": "average"})
    trade(retirement, "buy", 220, "-1500.00", security_id=ccc, quantity="75", price="20")
    trade(retirement, "buy", 150, "-1020.00", security_id=ccc, quantity="50", price="20.40")
    trade(retirement, "buy", 100, "-2000.00", security_id=bbb, quantity="40", price="50")


def build_loans_and_assets(seeding: Seeding, checking: dict[str, Any]) -> None:
    """A mortgage with payments, a car loan, manual assets and liabilities."""
    api, today = seeding.api, seeding.today
    mortgage = _account(seeding, type="mortgage", name="Home Mortgage", number_last4="7710")
    origination = month_start(today, 14)
    loan = api.post(
        "/loans",
        {
            "account_id": mortgage["id"],
            "principal_original": usd("260000.00"),
            "rate": "0.0625",
            "term_months": 360,
            "origination_date": origination.isoformat(),
            "payment_day": 1,
            "escrow_amount": usd("320.00"),
            "lender": "Placeholder Home Lending",
        },
    )
    balance = Decimal("260000.00")
    rate = Decimal("0.0625") / 12
    payment = (balance * rate / (1 - (1 + rate) ** -360)).quantize(CENT)
    for index in range(1, 14):
        interest = (balance * rate).quantize(CENT)
        principal = payment - interest
        balance -= principal
        api.post(
            f"/loans/{loan['id']}/payments",
            {
                "paid_on": month_start(today, 14 - index).isoformat(),
                "principal": usd(principal),
                "interest": usd(interest),
                "escrow": usd("320.00"),
            },
        )
        seeding.count("loan payments")
    seeding.count("loans")

    car = _account(seeding, type="loan", name="Car Loan", number_last4="2290")
    api.post(
        "/loans",
        {
            "account_id": car["id"],
            "principal_original": usd("18000.00"),
            "rate": "0.0549",
            "term_months": 60,
            "origination_date": month_start(today, 8).isoformat(),
            "payment_day": 15,
            "lender": "Sample Auto Finance",
        },
    )
    seeding.count("loans")

    line = _account(seeding, type="line_of_credit", name="Home Equity Line")
    liability = _account(seeding, type="manual_liability", name="Family Loan")
    for back in range(HISTORY_MONTHS, -1, -1):
        when = (today - timedelta(days=30 * back)).isoformat()
        api.post(
            f"/accounts/{line['id']}/balances",
            {"as_of_date": when, "balance": usd(f"-{2400 - 150 * (HISTORY_MONTHS - back)}.00")},
        )
        api.post(
            f"/accounts/{liability['id']}/balances",
            {"as_of_date": when, "balance": usd("-5000.00")},
        )

    house = _account(seeding, type="manual_asset", name="Family Home", asset_kind="property")
    vehicle = _account(seeding, type="manual_asset", name="Family Car", asset_kind="vehicle")
    for account, description, values in (
        (house, "Synthetic house", ("395000.00", "401000.00", "412500.00")),
        (vehicle, "Synthetic hatchback", ("21500.00", "20800.00", "20100.00")),
    ):
        asset = api.post(
            "/assets",
            {
                "account_id": account["id"],
                "description": description,
                "value": usd(values[0]),
                "valued_on": month_start(today, 12).isoformat(),
            },
        )
        for value, back in zip(values[1:], (6, 1), strict=True):
            api.post(
                f"/assets/{asset['id']}/valuations",
                {"valued_on": month_start(today, back).isoformat(), "value": usd(value)},
            )
        seeding.count("assets")
    _ = checking


def snapshot_net_worth(seeding: Seeding) -> None:
    """Store the net worth history: one snapshot per month end (and today)."""
    from app.db.rls import set_context
    from app.domain.networth.service import snapshot_household

    today = seeding.today
    days = [month_start(today, back) - timedelta(days=1) for back in range(HISTORY_MONTHS, 0, -1)]
    days.append(today)
    with seeding.sessions() as db, db.begin():
        set_context(db, seeding.household_id)
        for day in days:
            snapshot_household(db, seeding.household_id, day, max_days=1)
            seeding.count("net worth snapshots")


def build_ai_client(seeding: Seeding) -> None:
    """A client definition kept as an example, then revoked (a disabled client).

    The raw token is shown once by the API and discarded here.
    """
    api = seeding.api
    created = api.post(
        "/settings/ai/clients",
        {
            "name": "Example assistant (disabled)",
            "mode": "aggregates",
            "rounding": 10,
            "expires_in_days": 30,
            "transport": "stdio",
        },
    )
    api.delete(f"/settings/ai/clients/{created['id']}")
    seeding.count("ai clients (revoked)")


def drain_jobs(seeding: Seeding, *, limit: int = 500) -> int:
    """Run the queued jobs (ingestion, rule runs, transfer pairing) in this process."""
    from app.db.session import make_app_engine as make_engine
    from app.workers.registry import discover_handlers
    from app.workers.runner import Worker, WorkerConfig

    discover_handlers()
    engine = make_engine(seeding.settings)
    try:
        worker = Worker(engine, config=WorkerConfig(queues=("default",), concurrency=1))
        ran = 0
        while ran < limit and worker.run_once():
            ran += 1
        return ran
    finally:
        engine.dispose()


def upload(seeding: Seeding, name: str, data: bytes, mime: str, account_id: str | None) -> str:
    params = {"account_id": account_id} if account_id else {}
    accepted: dict[str, Any] = seeding.api.post(
        "/documents", params=params, files={"file": (name, data, mime)}
    )
    seeding.count("documents uploaded")
    return str(accepted.get("document_id") or accepted.get("id"))


def build_tax(seeding: Seeding, categories: dict[str, str]) -> None:
    """A tax year with an expectations checklist; its forms arrive as uploads."""
    api, year = seeding.api, seeding.today.year - 1
    api.post("/tax-years", {"year": year, "filing_status": "single"})
    seeding.count("tax years")
    for form_type in ("1099-B", "1098-T"):
        api.post(
            f"/tax-years/{year}/checklist",
            {"expected_form": form_type, "issuer": tax_forms.ISSUERS[form_type]},
        )
    api.put(
        f"/tax-category-map/{categories['Charitable donations']}",
        {"schedule": "Schedule A", "line": "12"},
    )
    for form_type in ("W-2", "1099-INT", "1099-DIV", "1098"):
        form = tax_forms.synth_form(form_type, seed=3, tax_year=year)
        upload(
            seeding, f"{form_type}-{year}.pdf", tax_forms.form_pdf(form), "application/pdf", None
        )


def build_documents(seeding: Seeding, accounts: dict[str, dict[str, Any]]) -> None:
    """Statements and a receipt through the real upload endpoint and ingestion jobs."""
    first = month_start(seeding.today, 1)
    ledger = synth.synth_ledger(7, start=first, days=28)
    stmt = synth.statement(ledger, first, first + timedelta(days=27), seed=7)
    upload(
        seeding,
        "bank-statement.csv",
        synth.csv_bytes(stmt, "us_signed"),
        "text/csv",
        accounts["checking"]["id"],
    )
    upload(
        seeding,
        "brokerage-statement.pdf",
        brokerage.placeholder_pdf(brokerage.brokerage_statement()),
        "application/pdf",
        None,
    )
    upload(
        seeding,
        "receipt.png",
        receipts.thermal_png(receipts.receipt_spec(11)),
        "image/png",
        None,
    )


def build_all(seeding: Seeding) -> None:
    """Everything, in dependency order. The AI client goes first: it needs a fresh step-up."""
    api = seeding.api
    build_ai_client(seeding)
    accounts = build_ledger(seeding)
    build_investments(seeding)
    build_loans_and_assets(seeding, accounts["checking"])
    categories = _categories(api)
    build_tax(seeding, categories)
    build_documents(seeding, accounts)
    ran = drain_jobs(seeding)
    seeding.log(f"seed_dev: ran {ran} background jobs")
    snapshot_net_worth(seeding)
    review = api.items("/review")
    seeding.counts["open review items"] = len(review)
    # The completion marker: an unfinished run is detected by its absence.
    api.post("/tags", {"name": MARKER_TAG})


def seed_dev(
    settings: Settings,
    *,
    today: date | None = None,
    log: Callable[[str], None] = print,
) -> SeedResult:
    """Seed ``settings``' database (module doc). Raises :class:`SeedRefusedError`."""
    if not settings.dev_mode:
        raise SeedRefusedError("PERFIN_DEV_MODE is not true; the dev seed never runs elsewhere")
    import_all_models()
    engine = make_app_engine(settings)
    sessions = make_sessionmaker(engine)
    try:
        with sessions() as db, db.begin():
            if _seed_present(db):
                return SeedResult(
                    created=False,
                    notes=["already seeded; nothing changed (make dev-reset starts over)"],
                )
            password, household = _create_admin(db, settings)
            user_id = db.execute(select(User.id).where(User.username == DEV_USERNAME)).scalar_one()
            household_id = household.id
        origin = normalize_origin(settings.public_url) or "http://localhost:8000"
        app = create_app(settings)
        with TestClient(app, base_url=origin) as client:
            with sessions() as db, db.begin():
                issued = create_session(
                    db,
                    user_id=user_id,
                    policy=SessionPolicy.from_settings(settings),
                    login_method=LoginMethod.LOCAL,
                    stage=SessionStage.AUTHENTICATED,
                    household_id=household_id,
                    ip=None,
                    user_agent="seed_dev",
                    mfa_verified=True,
                )
                raw_id = issued.raw_id
            client.cookies.set(cookie_name(settings), raw_id)
            protect = app.state.csrf
            if not isinstance(protect, CSRFProtect):
                raise RuntimeError("seed: the app has no CSRF protection installed")
            api = Api(client, origin, protect.token_for_session(raw_id))
            seeding = Seeding(
                api=api,
                settings=settings,
                sessions=sessions,
                household_id=household_id,
                today=today or clock.utcnow().date(),
                log=log,
            )
            try:
                build_all(seeding)
            finally:
                api.send("POST", "/auth/logout", (200, 204))
        return SeedResult(
            created=True, password=password, counts=seeding.counts, notes=seeding.notes
        )
    finally:
        engine.dispose()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Seed the dev database with synthetic data.")
    parser.parse_args(argv)
    try:
        settings = get_settings()
        result = seed_dev(settings)
    except SeedRefusedError as exc:
        print(f"seed_dev: refused: {exc}", file=sys.stderr)
        return 2
    except RuntimeError as exc:
        print(f"seed_dev: {exc}", file=sys.stderr)
        return 1
    for note in result.notes:
        print(f"seed_dev: {note}")
    if result.created:
        print("seed_dev: seeded the dev household (synthetic data only)")
        for what, number in sorted(result.counts.items()):
            print(f"  {what}: {number}")
        print("")
        print("Dev admin (shown once, stored nowhere; `make dev-reset` makes a new one):")
        print(f"  username: {result.username}")
        print(f"  password: {result.password}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
