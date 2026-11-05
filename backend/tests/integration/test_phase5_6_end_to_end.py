# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phases 5+6 end to end: the real app, the real worker, the real sandbox.

One synthetic household goes through every wealth and tax path over HTTP,
with the production pipeline dependencies
(``app.workers.handlers.ingest.default_deps``, real sandbox) and the real
:class:`app.workers.runner.Worker` claiming the queued jobs:

1. a brokerage statement uploaded as PDF (January) and as OFX (February) is
   parsed, reconciled and imported: ``GET /investments/holdings`` equals the
   February positions, ``GET /investments/performance`` covers the account;
2. a mortgage statement records the payment split (principal, interest,
   escrow) on the loan, and the loan account's balance follows it;
3. a manual asset gets a valuation;
4. the ``networth.snapshot`` job backfills the net worth history, and
   ``GET /networth`` serves it from the stored snapshots;
5. the ``recurring.detect`` job finds a monthly bill and a biweekly salary,
   and ``GET /cashflow/calendar`` shows them;
6. a synthetic W-2 and 1099-INT tick off the year's checklist with their
   amounts, and **no identity value of the forms is stored anywhere** in the
   database (every column of every table);
7. the accountant export holds both originals and the summary, downloadable
   once.

All documents come from code (``tests/fixtures/synth_*``): invented
institutions, instruments, lender, employer and payer. The worker runs only
this test's jobs: other tests leave queued jobs in the shared test database,
which are held back for the duration of the test.
"""

from __future__ import annotations

import csv
import io
import uuid
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, select, text

import app.workers.handlers.tax as tax_handlers
from app.core import clock
from app.core.settings import Settings
from app.db.models import Job, JobState
from app.db.queue import enqueue
from app.db.rls import queue_scope, set_context
from app.domain.accounts.models import Account
from app.domain.documents.models import Document, DocumentStatus, ReviewItem, ReviewItemState
from app.domain.loans.amortization import add_months
from app.domain.networth.models import NetWorthSnapshot
from app.workers.handlers import ingest
from app.workers.handlers import networth as networth_handler
from app.workers.handlers import recurring as recurring_handler
from app.workers.registry import REGISTRY, discover_handlers
from app.workers.runner import Worker, WorkerConfig
from app.workers.scheduler import Schedule
from tests.fixtures import synth_brokerage as brokerage
from tests.fixtures import synth_loans as loans
from tests.fixtures import synth_tax_forms as tax_forms
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration

D = Decimal
#: Brokerage account number (the loan fixture uses ``loans.LAST4``).
BROKERAGE_LAST4 = "7310"


@contextmanager
def only_these_jobs(factories: Factories, household_id: uuid.UUID) -> Iterator[None]:
    """Hold back queued jobs of other households (and instance jobs) while ours run."""
    with factories.transaction() as db, queue_scope(db):
        held = list(
            db.execute(
                text(
                    "UPDATE job SET run_after = run_after + interval '100 years' "
                    "WHERE state = 'queued' AND household_id IS DISTINCT FROM :h RETURNING id"
                ),
                {"h": household_id},
            ).scalars()
        )
    try:
        yield
    finally:
        if held:
            with factories.transaction() as db, queue_scope(db):
                db.execute(
                    text(
                        "UPDATE job SET run_after = run_after - interval '100 years' "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": held},
                )


class Home:
    """The test's household: the owner's client and the worker."""

    def __init__(
        self, owner: AuthedClient, factories: Factories, ledger: LedgerFactories, engine: Engine
    ) -> None:
        self.owner = owner
        self.factories = factories
        self.ledger = ledger
        household_id = owner.record.household_id
        assert household_id is not None
        self.id = household_id
        self.worker = Worker(
            engine,
            config=WorkerConfig(listen=False, poll_interval=0.05, concurrency=1),
            registry=REGISTRY,
            schedule=Schedule(),
        )

    def drain(self) -> None:
        """Run this household's queued jobs (and the jobs they queue) to the end."""
        with only_these_jobs(self.factories, self.id):
            for _ in range(200):
                if not self.worker.run_once():
                    break
        failed = [
            (job.type, job.state, job.last_error)
            for job in self.rows(Job, Job.household_id == self.id)
            if job.state is not JobState.SUCCEEDED
            # Scheduled for later (the export's expiry): not due yet.
            and not (job.state is JobState.QUEUED and job.run_after > clock.utcnow())
        ]
        assert failed == [], failed

    def enqueue(self, job_type: str, payload: BaseModel) -> None:
        with self.factories.transaction() as db:
            set_context(db, self.id)
            enqueue(db, job_type, payload, household_id=self.id)

    def upload(self, data: bytes, name: str) -> str:
        response = self.owner.post(
            f"{API}/documents", files={"file": (name, data, "application/octet-stream")}
        )
        assert response.status_code == 202, response.text
        document_id: str = response.json()["id"]
        self.drain()
        document = self.document(document_id)
        assert document.status is DocumentStatus.PROCESSED, (
            name,
            document.error,
            [r.payload for r in self.open_reviews(document_id)],
        )
        return document_id

    def get(self, path: str, **params: Any) -> Any:
        response = self.owner.get(f"{API}{path}", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def post(self, path: str, body: dict[str, Any] | None = None, status: int = 201) -> Any:
        response = self.owner.post(f"{API}{path}", json=body or {})
        assert response.status_code == status, response.text
        return response.json() if response.content else None

    def rows(self, model: Any, *where: Any) -> list[Any]:
        with self.factories.transaction() as db:
            set_context(db, self.id)
            found = list(db.execute(select(model).where(*where)).scalars())
            db.expunge_all()
            return found

    def document(self, document_id: str) -> Document:
        (found,) = self.rows(Document, Document.id == uuid.UUID(document_id))
        document: Document = found
        return document

    def open_reviews(self, document_id: str) -> list[ReviewItem]:
        return self.rows(
            ReviewItem,
            ReviewItem.document_id == uuid.UUID(document_id),
            ReviewItem.state == ReviewItemState.OPEN,
        )

    def account(self, **body: Any) -> str:
        created = self.post("/accounts", {"currency": "USD", **body})
        account_id: str = created["id"]
        return account_id

    def account_row(self, account_id: str) -> Account:
        (found,) = self.rows(Account, Account.id == uuid.UUID(account_id))
        account: Account = found
        return account


@pytest.fixture
def home(
    settings: Settings,
    factories: Factories,
    ledger: LedgerFactories,
    app_engine: Engine,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Home]:
    local = settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )
    discover_handlers()
    monkeypatch.setattr(ingest, "get_settings", lambda: local)
    monkeypatch.setattr(tax_handlers, "get_settings", lambda: local)
    ingest.default_deps.cache_clear()
    try:
        with Actors.open(local, factories) as actors:
            owner = actors.new(role="owner", username=f"own{uuid.uuid4().hex[:10]}")
            yield Home(owner, factories, ledger, app_engine)
    finally:
        ingest.default_deps.cache_clear()


def _money(value: dict[str, str]) -> Decimal:
    assert value["currency"] == "USD"
    return D(value["amount"])


def _database_text(admin_engine: Engine) -> str:
    """Every row of every table of the test database as text (lower case)."""
    parts: list[str] = []
    with admin_engine.connect() as conn:
        tables = conn.execute(
            text(
                "SELECT quote_ident(table_name) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        ).scalars()
        for table in list(tables):
            # Identifiers come from the catalogue, never from input.
            rows = conn.execute(text(f"SELECT t::text FROM {table} t")).scalars()  # noqa: S608
            parts.extend(str(r) for r in rows)
    return "\n".join(parts).lower()


def _investments(home: Home) -> None:
    january, february = brokerage.brokerage_series(seed=71, last4=BROKERAGE_LAST4)
    account = home.account(type="brokerage", name="Brokerage", number_last4=BROKERAGE_LAST4)
    pdf = home.upload(brokerage.placeholder_pdf(january), "brokerage-2026-01.pdf")
    ofx = home.upload(brokerage.ofx_investment(february), "brokerage-2026-02.ofx")
    for document_id in (pdf, ofx):
        assert str(home.document(document_id).account_id) == account

    holdings = home.get("/investments/holdings", account=account)
    got = sorted((h["security"]["symbol"], D(h["quantity"])) for h in holdings["holdings"])
    assert got == sorted((p.symbol, p.quantity) for p in february.positions)
    for entry in holdings["holdings"]:
        # Valued at the statement's own closing prices (no external feed).
        assert entry["price"]["price_date"] == february.end.isoformat()
        assert sum(D(lot["remaining_quantity"]) for lot in entry["lots"]) == D(entry["quantity"])
    # Market value is units x price unrounded; the statement prints cents.
    exact = sum((p.quantity * p.price for p in february.positions), D(0))
    # (each holding's value is rounded to 6 places before the sum)
    rounding = D("0.000001") * len(february.positions)
    assert abs(_money(holdings["market_value"]) - exact) <= rounding

    performance = home.get("/investments/performance", range="max", account=account)
    assert performance["account_id"] == account
    assert performance["start"] <= january.start.isoformat()
    assert _money(performance["end_value"]) == _money(holdings["market_value"])
    assert performance["unpriced_security_ids"] == []
    assert performance["missing_rates"] == []
    assert performance["series"]


def _loan(home: Home) -> tuple[str, Decimal]:
    terms = loans.TERMS
    account = home.account(type="mortgage", name="Home loan", number_last4=loans.LAST4)
    loan = home.post(
        "/loans",
        {
            "account_id": account,
            "principal_original": {"amount": f"{terms.principal}", "currency": "USD"},
            "rate": f"{terms.annual_rate}",
            "term_months": terms.term_months,
            "origination_date": terms.origination_date.isoformat(),
            "payment_day": terms.payment_day,
            "escrow_amount": {"amount": f"{terms.escrow}", "currency": "USD"},
            "lender": loans.LENDER,
        },
    )
    first, second = loans.loan_series(2)
    for statement in (first, second):
        name = f"mortgage-{statement.paid_on:%Y-%m}.pdf"
        document_id = home.upload(loans.loan_statement_pdf(statement), name)
        assert str(home.document(document_id).account_id) == account

    payments = home.get(f"/loans/{loan['id']}/payments")["items"]
    split = sorted(
        (
            p["paid_on"],
            _money(p["principal"]),
            _money(p["interest"]),
            _money(p["escrow"]),
            p["schedule_check"]["status"],
        )
        for p in payments
    )
    assert split == [
        (s.paid_on.isoformat(), s.principal, s.interest, s.escrow, "ok") for s in (first, second)
    ]
    assert all(p["source_document_id"] for p in payments)
    balance = home.get(f"/accounts/{account}")["balance"]
    assert _money(balance) == -second.balance
    return account, second.balance


def _manual_asset(home: Home, today: date) -> tuple[str, Decimal]:
    account = home.account(
        type="manual_asset", name="Synthetic house", asset_kind="property", manual_balance=True
    )
    asset = home.post("/assets", {"account_id": account, "valuation_method": "appraisal"})
    value = D("412500.00")
    home.post(
        f"/assets/{asset['id']}/valuations",
        {
            "valued_on": (today - timedelta(days=3)).isoformat(),
            "value": {"amount": f"{value}", "currency": "USD"},
        },
    )
    valuations = home.get(f"/assets/{asset['id']}/valuations")["items"]
    assert [_money(v["value"]) for v in valuations] == [value]
    assert _money(home.get(f"/accounts/{account}")["balance"]) == value
    return account, value


def _recurring(home: Home, today: date) -> None:
    checking_id = home.account(type="checking", name="Everyday checking")
    checking = home.account_row(checking_id)
    first_rent = add_months(date(today.year, today.month, 1), -6, 5)
    for i in range(6):
        home.ledger.transaction(
            checking,
            amount="-1450.00",
            posted_date=add_months(first_rent, i, 5),
            raw_payee="Synthetic Property Mgmt",
        )
    payday = today - timedelta(days=7 + 14 * 11)
    while payday <= today - timedelta(days=7):
        home.ledger.transaction(
            checking, amount="3100.00", posted_date=payday, raw_payee="Example Employer Payroll"
        )
        payday += timedelta(days=14)

    home.enqueue(
        recurring_handler.DETECT_JOB_TYPE, recurring_handler.HouseholdDetectPayload(as_of=today)
    )
    home.drain()
    schedules = home.get("/recurring")["items"]
    found = {
        (s["origin"], s["cadence"], s["interval_count"], s["amount_max"]["amount"])
        for s in schedules
    }
    assert ("detected", "monthly", 1, "-1450.00") in found, schedules
    assert ("detected", "weekly", 2, "3100.00") in found, schedules  # biweekly

    calendar = home.get("/cashflow/calendar", days=45)
    events = [
        (day["on"], e["kind"], e["amount"]["amount"])
        for day in calendar["days"]
        for e in day["events"]
    ]
    assert any(kind == "bill" and amount == "-1450.00" for _, kind, amount in events), events
    assert any(kind == "income" and amount == "3100.00" for _, kind, amount in events), events
    assert _money(calendar["net"]) == _money(calendar["income"]) - _money(calendar["bills"])


def _net_worth(
    home: Home, today: date, mortgage: tuple[str, Decimal], house: tuple[str, Decimal]
) -> None:
    home.enqueue(
        networth_handler.SNAPSHOT_JOB_TYPE, networth_handler.HouseholdSnapshotPayload(as_of=today)
    )
    home.drain()
    snapshots: list[NetWorthSnapshot] = home.rows(NetWorthSnapshot)
    days = sorted(s.as_of_date for s in snapshots)
    # Backfilled from the first data (the mortgage's origination) to today.
    assert days[0] == loans.TERMS.origination_date
    assert days[-1] == today
    assert len(days) == (today - days[0]).days + 1
    assert all(s.net == s.assets - s.liabilities for s in snapshots)

    history = home.get("/networth", range="1y")
    assert history["scope"] == "household"
    points = history["points"]
    assert points[-1]["as_of"] == today.isoformat()
    assert {p["source"] for p in points[:-1]} == {"snapshot"}

    latest = home.get("/networth/latest")
    assert latest["last_snapshot_date"] == today.isoformat()
    assert _money(latest["net"]) == _money(latest["assets"]) - _money(latest["liabilities"])
    values = {a["account_id"]: a for a in latest["accounts"]}
    mortgage_id, owed = mortgage
    assert values[mortgage_id]["side"] == "liability"
    assert abs(_money(values[mortgage_id]["amount"])) == owed
    house_id, worth = house
    assert values[house_id]["side"] == "asset"
    assert _money(values[house_id]["amount"]) == worth
    (today_row,) = [s for s in snapshots if s.as_of_date == today]
    assert today_row.net == _money(latest["net"])


def _tax(home: Home, admin_engine: Engine) -> None:
    year = 2025
    home.post("/tax-years", {"year": year})
    for form_type in ("W-2", "1099-INT"):
        home.post(
            f"/tax-years/{year}/checklist",
            {"expected_form": form_type, "issuer": tax_forms.ISSUERS[form_type]},
        )
    forms = {
        form_type: tax_forms.synth_form(form_type, seed=17, tax_year=year)
        for form_type in ("W-2", "1099-INT")
    }
    pdfs = {form_type: tax_forms.form_pdf(form) for form_type, form in forms.items()}
    documents = {
        form_type: home.upload(pdf, f"{form_type.lower()}-{year}.pdf")
        for form_type, pdf in pdfs.items()
    }

    checklist = home.get(f"/tax-years/{year}/checklist")["items"]
    assert sorted((i["expected_form"], i["status"], i["origin"]) for i in checklist) == [
        ("1099-INT", "received", "manual"),
        ("W-2", "received", "manual"),
    ]
    assert {i["expected_form"]: i["document_id"] for i in checklist} == documents
    stored = home.get(f"/tax-years/{year}/forms")["items"]
    for form_type, form in forms.items():
        (row,) = [f for f in stored if f["form_type"] == form_type]
        assert row["document_id"] == documents[form_type]
        assert row["issuer_name"] == form.issuer
        assert {b["box"]: D(b["amount"]) for b in row["boxes"]} == tax_forms.expected_boxes(form)

    # No identity value of the forms anywhere in the database.
    everything = _database_text(admin_engine)
    for token in tax_forms.IDENTITY_TOKENS:
        assert token not in everything, token

    # The accountant export: both originals and the summary, downloadable once.
    queued = home.post(f"/tax-years/{year}/export", status=202)
    home.drain()
    job_id = queued["job_id"]
    state = home.get(f"/tax-years/{year}/export/{job_id}")
    assert state["state"] == "ready", state
    download = home.owner.get(f"{API}/tax-years/{year}/export/{job_id}/download")
    assert download.status_code == 200, download.text
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        names = set(archive.namelist())
        expected = {f"originals/{documents[t]}/{t.lower()}-{year}.pdf" for t in ("W-2", "1099-INT")}
        assert names == expected | {"summary.csv", "summary.pdf"}
        for form_type, pdf in pdfs.items():
            name = f"originals/{documents[form_type]}/{form_type.lower()}-{year}.pdf"
            assert archive.read(name) == pdf
        summary = archive.read("summary.csv").decode("utf-8")
    rows = list(csv.DictReader(io.StringIO(summary)))
    w2_boxes = {
        r["box"]: r["amount"] for r in rows if r["section"] == "form" and r["form_type"] == "W-2"
    }
    assert w2_boxes["1"] == f"{forms['W-2'].amounts['1']:.2f}"
    for token in tax_forms.IDENTITY_TOKENS:
        assert token not in summary.lower(), token
    again = home.owner.get(f"{API}/tax-years/{year}/export/{job_id}/download")
    assert again.status_code == 410


def test_investments_loans_assets_net_worth_recurring_and_tax_vault(
    home: Home, admin_engine: Engine
) -> None:
    today = clock.utcnow().date()
    _investments(home)
    mortgage = _loan(home)
    house = _manual_asset(home, today)
    _recurring(home, today)
    _net_worth(home, today, mortgage, house)
    _tax(home, admin_engine)
