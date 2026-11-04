# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic data and helpers for the export/import tests (docs/PORTABILITY.md).

* :data:`CHEAP_KDF`: the cheapest Argon2id cost ``app.core.archive_crypto``
  accepts, so passphrase tests run in milliseconds.
* :func:`make_ready_export` / :func:`make_import_record`: flush-only
  archive rows (the export's bytes really encrypted into the given blob
  store) for the authorisation matrix and the download tests.
* :func:`build_rich_ledger`: the report ledger (``tests.fixtures.report_ledger``)
  plus one of everything else an export carries: an institution, tags on
  transactions, imported transactions with import hashes, a balance
  snapshot, a schedule, rules naming categories and tags, a budget, a goal
  with a contribution, an import mapping, a document linked to a
  transaction, and text cells that look like spreadsheet formulas.
* :func:`rezip`: rewrite an archive's members (and, by default, its
  manifest hashes) to build hostile archives from a real one.
* :func:`run_portability_jobs`: run the queued jobs of one type through the
  real handlers with test dependencies.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from unittest.mock import patch

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.archive_crypto import KdfParams
from app.core.crypto import Keyring
from app.core.ids import uuid7
from app.domain.accounts.models import Account, AccountType
from app.domain.ai.models import AiMode
from app.domain.documents.blobstore import BlobStore, EncryptingWriter
from app.domain.documents.models import (
    Blob,
    BlobArea,
    Document,
    DocumentKind,
    DocumentLink,
    LinkTarget,
)
from app.domain.investments.statement_import import activity_hash
from app.domain.networth.service import snapshot_household
from app.domain.portability.import_ import blob_row
from app.domain.portability.models import ArchiveKind, ArchiveState, PortabilityArchive
from app.domain.portability.service import PortabilityDeps
from app.domain.tax.models import ChecklistStatus, TaxChecklistItem, TaxYear
from app.domain.transactions.models import Transaction
from app.ingestion.classify.fingerprints import remember_layout
from app.ingestion.sandbox.protocol import InvestmentActivity
from tests.fixtures.ai import make_ai_client
from tests.fixtures.documents import make_document
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API
from tests.fixtures.imports import make_import_mapping, run_jobs
from tests.fixtures.ledger import (
    LedgerFactories,
    household_context,
    make_goal_contribution,
    make_scheduled_transaction,
    make_transaction,
    tag_transaction,
)
from tests.fixtures.report_ledger import ReportLedger, build
from tests.fixtures.tax import make_form_data
from tests.fixtures.wealth import make_investment_txn

CHEAP_KDF = KdfParams(1, 8 * 1024)

#: Cells a spreadsheet would run as formulas (C7).
FORMULA_MEMO = '=HYPERLINK("https://evil.example","click")'
FORMULA_PAYEE = "+cmd|' /C calc'!A0"
FORMULA_NOTE = "@SUM(1+1)*cmd|' /C calc'!A0"

DOCUMENT_BYTES = b"Date,Amount,Payee\n2026-03-02,-100.25,Shop\n"


def small_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", "{}")
    return buffer.getvalue()


def make_ready_export(
    db: Session,
    store: BlobStore,
    keyring: Keyring,
    household_id: uuid.UUID,
    requested_by: uuid.UUID | None,
    *,
    data: bytes | None = None,
    state: ArchiveState = ArchiveState.READY,
    expires_in: timedelta = timedelta(hours=1),
) -> PortabilityArchive:
    """A finished export whose (synthetic) bytes are encrypted in ``store``."""
    payload = small_zip() if data is None else data
    with EncryptingWriter(store, keyring, uuid7(), area=BlobArea.PERMANENT) as writer:
        writer.write(payload)
        sealed = writer.finish()
    now = clock.utcnow()
    with household_context(db, household_id):
        db.execute(insert(Blob), [blob_row(household_id, sealed)])
        archive = PortabilityArchive(
            household_id=household_id,
            kind=ArchiveKind.EXPORT,
            state=state,
            job_id=uuid7(),
            requested_by=requested_by,
            blob_id=sealed.blob_id,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            finished_at=now,
            expires_at=now + expires_in,
            summary={"counts": {}},
        )
        db.add(archive)
        db.flush()
    return archive


def make_import_record(
    db: Session,
    household_id: uuid.UUID,
    requested_by: uuid.UUID | None,
    *,
    state: ArchiveState = ArchiveState.IMPORTED,
) -> PortabilityArchive:
    """A finished import (no blob left), as the import job leaves it."""
    with household_context(db, household_id):
        archive = PortabilityArchive(
            household_id=household_id,
            kind=ArchiveKind.IMPORT,
            state=state,
            job_id=uuid7(),
            requested_by=requested_by,
            finished_at=clock.utcnow(),
            summary={"created": {}, "merged": {}, "skipped": {}},
        )
        db.add(archive)
        db.flush()
    return archive


@dataclass(frozen=True)
class RichLedger:
    book: ReportLedger
    document: Document
    hashed: list[Transaction]
    formula: Transaction


def build_rich_ledger(
    factories: Factories,
    ledger: LedgerFactories,
    store: BlobStore,
    household_id: uuid.UUID,
) -> RichLedger:
    hid = household_id
    book = build(ledger, hid)
    institution = ledger.institution(hid, name="Synthetic Bank", country="US")
    with factories.transaction() as db, household_context(db, hid):
        brokerage = db.get(Account, book.savings.id)
        assert brokerage is not None
        brokerage.institution_id = institution.id
        db.flush()
    tag_a = ledger.tag(hid, name="Holiday")
    tag_b = ledger.tag(hid, name="Work")
    hashed = [
        ledger.transaction(
            book.card,
            posted_date=date(2026, 4, 2),
            amount="-12.00",
            raw_payee="COFFEE CART #12",
            with_import_hash=True,
            hash_index=index,
        )
        for index in range(2)
    ]
    formula = ledger.transaction(
        book.checking,
        posted_date=date(2026, 4, 3),
        amount="-1.00",
        raw_payee=FORMULA_PAYEE,
        memo=FORMULA_MEMO,
        notes=FORMULA_NOTE,
    )
    ledger.snapshot(book.checking, as_of_date=date(2026, 1, 31), balance="2500.00")
    ledger.rule(
        hid,
        name="Coffee",
        conditions={"type": "payee", "op": "contains", "value": "coffee"},
        actions=[
            {"type": "set_category", "category_id": str(book.restaurants.id)},
            {"type": "add_tags", "tag_ids": [str(tag_b.id)]},
        ],
    )
    ledger.budget(
        hid,
        name="Monthly",
        lines={book.groceries.id: "400.00", book.restaurants.id: "150.00"},
    )
    goal = ledger.goal(hid, name="Rainy day", accounts=[book.savings])
    with factories.transaction() as db, household_context(db, hid):
        tag_transaction(db, db.get(Transaction, hashed[0].id), tag_a)  # type: ignore[arg-type]
        tag_transaction(db, db.get(Transaction, formula.id), tag_b)  # type: ignore[arg-type]
        contribution_txn = make_transaction(
            db, book.savings, posted_date=date(2026, 4, 5), amount="250.00"
        )
        make_goal_contribution(
            db, goal, amount="250.00", contributed_on=date(2026, 4, 5), transaction=contribution_txn
        )
        make_scheduled_transaction(
            db, book.checking, name="Rent", payee=book.shop, category=book.rent
        )
        make_import_mapping(db, hid, name="Synthetic layout", account=book.checking)
        document = make_document(
            db,
            store,
            factories.keyring,
            hid,
            data=DOCUMENT_BYTES,
            filename="../../March statement?.csv",
            account_id=book.checking.id,
        )
        row = db.get(Transaction, hashed[1].id)
        assert row is not None
        row.source_document_id = document.id
        db.add(
            DocumentLink(
                household_id=hid,
                document_id=document.id,
                target_type=LinkTarget.TRANSACTION,
                target_id=hashed[1].id,
            )
        )
        db.flush()
    return RichLedger(book=book, document=document, hashed=hashed, formula=formula)


def members_of(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def zip_members(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def rezip(
    data: bytes,
    mutate: Callable[[dict[str, bytes]], None],
    *,
    fix_manifest: bool = True,
    manifest: Callable[[dict[str, Any]], None] | None = None,
) -> bytes:
    """``data`` with its members changed by ``mutate`` (manifest hashes re-done)."""
    members = members_of(data)
    mutate(members)
    document = json.loads(members["manifest.json"])
    if fix_manifest:
        document["members"] = {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for name, content in members.items()
            if name != "manifest.json"
        }
    if manifest is not None:
        manifest(document)
    members["manifest.json"] = json.dumps(document).encode()
    return zip_members(members)


def rows_of(members: dict[str, bytes], entity: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(members[f"data/{entity}.json"])
    return rows


def set_rows(members: dict[str, bytes], entity: str, rows: list[dict[str, Any]]) -> None:
    members[f"data/{entity}.json"] = json.dumps(rows).encode()


@contextmanager
def portability_deps(deps: PortabilityDeps) -> Iterator[None]:
    import app.workers.handlers.portability as handlers

    with patch.object(handlers, "default_deps", lambda: deps):
        yield


def run_portability_jobs(
    factories: Factories,
    household_id: uuid.UUID,
    job_type: str,
    deps: PortabilityDeps,
    done: set[uuid.UUID],
) -> list[uuid.UUID]:
    """Run the household's queued ``job_type`` jobs through the real handlers."""
    import app.workers.handlers.portability  # noqa: F401 - registers the handlers

    with portability_deps(deps):
        return run_jobs(factories, household_id, job_type, done=done)


# ---------------------------------------------------------------------------
# Every Phase 3-6 domain (format version 2)
# ---------------------------------------------------------------------------

#: A second synthetic original: the tax form the form data was read from.
TAX_FORM_BYTES = b"Form,Box,Amount\nW-2,1,61234.50\nW-2,2,7348.14\n"
FINGERPRINT_RULES = {"all": [{"header": "SYNTHETIC SAVINGS BANK"}]}


@dataclass(frozen=True)
class Wealth:
    brokerage: Account
    ira: Account
    mortgage: Account
    house: Account
    tax_document: Document
    security_ids: list[str]
    #: The AI client's bearer token (must never appear in an archive).
    ai_token: str


def _created(response: Any) -> dict[str, Any]:
    assert response.status_code in (200, 201), response.text
    body: dict[str, Any] = response.json()
    return body


def _usd(amount: str) -> dict[str, str]:
    return {"amount": amount, "currency": "USD"}


def build_wealth(
    factories: Factories,
    ledger: LedgerFactories,
    store: BlobStore,
    owner: AuthedClient,
    book: RichLedger,
) -> Wealth:
    """One of everything the Phase 3-6 tables hold, mostly through the real API.

    Investments (two securities, prices, buys, a sale of a chosen lot, a
    split, a dividend, an average-cost account), a variable-rate mortgage
    with payments (one linked to a ledger transaction) and a rate change, a
    manual asset with valuations, stored net worth snapshots, a tax year
    with checklist items, a category map and form data read from a
    document linked to the year, a learned parser fingerprint and an AI
    client. All values are invented.
    """

    hid = book.book.household_id
    brokerage = ledger.account(hid, type=AccountType.BROKERAGE, name="Brokerage")
    ira = ledger.account(hid, type=AccountType.RETIREMENT, name="Retirement")
    mortgage = ledger.account(hid, type=AccountType.MORTGAGE, name="Home loan")
    house = ledger.account(hid, type=AccountType.MANUAL_ASSET, name="House", asset_kind="property")

    # ---- investments ------------------------------------------------------
    securities = [
        _created(
            owner.post(
                f"{API}/securities",
                json={
                    "symbol": symbol,
                    "name": f"Synthetic {symbol}",
                    "asset_class": kind,
                    "currency": "USD",
                },
            )
        )
        for symbol, kind in (("SYNAAA", "equity"), ("SYNBBB", "etf"))
    ]
    syn_a, syn_b = securities
    for security, closes in ((syn_a, ("101.50", "64.25")), (syn_b, ("49.10", "52.75"))):
        for day, close in zip(("2026-03-31", "2026-06-30"), closes, strict=True):
            _created(
                owner.post(
                    f"{API}/securities/{security['id']}/prices",
                    json={"price_date": day, "close": close},
                )
            )
    txns = f"{API}/investments/transactions"

    def txn(account: Account, kind: str, day: str, amount: str, **extra: Any) -> dict[str, Any]:
        body = {
            "account_id": str(account.id),
            "type": kind,
            "trade_date": day,
            "amount": _usd(amount),
            **extra,
        }
        return _created(owner.post(txns, json=body))

    txn(
        brokerage,
        "buy",
        "2026-01-05",
        "-1000.00",
        security_id=syn_a["id"],
        quantity="10",
        price="100",
        fees=_usd("0.00"),
    )
    second = txn(
        brokerage,
        "buy",
        "2026-02-05",
        "-605.00",
        security_id=syn_a["id"],
        quantity="5",
        price="120",
        fees=_usd("5.00"),
    )
    lots = second["lots"]
    assert lots, second
    txn(
        brokerage,
        "sell",
        "2026-03-10",
        "540.00",
        security_id=syn_a["id"],
        quantity="4",
        price="135",
        lots=[{"lot_id": lots[0]["lot_id"], "quantity": "4"}],
    )
    txn(
        brokerage,
        "split",
        "2026-04-01",
        "0",
        security_id=syn_a["id"],
        ratio={"new": "2", "old": "1"},
    )
    txn(brokerage, "dividend", "2026-05-15", "12.34", security_id=syn_a["id"])
    txn(brokerage, "fee", "2026-05-31", "-3.00")
    # A statement row: its import hash embeds the account (re-computed on import).
    statement_row = InvestmentActivity.model_construct(
        type="interest", trade_date=date(2026, 6, 1), quantity=None, amount=Decimal("1.25")
    )
    with factories.transaction() as db:
        make_investment_txn(
            db,
            brokerage,
            type="interest",
            trade_date=date(2026, 6, 1),
            quantity=None,
            price=None,
            amount="1.25",
            import_hash=activity_hash(brokerage.id, statement_row, None, 0),
        )
    _created(
        owner.put(f"{API}/investments/accounts/{ira.id}/cost-method", json={"method": "average"})
    )
    txn(ira, "buy", "2026-01-10", "-1000.00", security_id=syn_b["id"], quantity="20", price="50")
    txn(ira, "buy", "2026-02-10", "-1100.00", security_id=syn_b["id"], quantity="20", price="55")
    txn(ira, "sell", "2026-04-20", "1560.00", security_id=syn_b["id"], quantity="30", price="52")
    txn(
        ira,
        "reinvest",
        "2026-05-20",
        "-49.10",
        security_id=syn_b["id"],
        quantity="1",
        price="49.10",
    )

    # ---- loans ------------------------------------------------------------
    loan = _created(
        owner.post(
            f"{API}/loans",
            json={
                "account_id": str(mortgage.id),
                "principal_original": _usd("240000.00"),
                "rate": "0.05875",
                "rate_type": "variable",
                "term_months": 360,
                "origination_date": "2025-11-01",
                "payment_day": 1,
                "escrow_amount": _usd("310.00"),
                "lender": "Placeholder Lending",
            },
        )
    )
    paid = ledger.transaction(
        book.book.checking, posted_date=date(2026, 1, 2), amount="-1729.70", raw_payee="MORTGAGE"
    )
    for day, principal, interest, link in (
        ("2025-12-01", "244.70", "1175.00", None),
        ("2026-01-01", "245.90", "1173.80", str(paid.id)),
    ):
        _created(
            owner.post(
                f"{API}/loans/{loan['id']}/payments",
                json={
                    "paid_on": day,
                    "principal": _usd(principal),
                    "interest": _usd(interest),
                    "escrow": _usd("310.00"),
                    "transaction_id": link,
                },
            )
        )
    _created(
        owner.post(
            f"{API}/loans/{loan['id']}/rate-changes",
            json={"effective_date": "2026-05-01", "new_rate": "0.0625"},
        )
    )

    # ---- manual assets ----------------------------------------------------
    asset = _created(
        owner.post(
            f"{API}/assets",
            json={
                "account_id": str(house.id),
                "description": "Synthetic house",
                "value": _usd("410000.00"),
                "valued_on": "2026-01-01",
            },
        )
    )
    _created(
        owner.post(
            f"{API}/assets/{asset['id']}/valuations",
            json={"valued_on": "2026-06-01", "value": _usd("425000.00")},
        )
    )

    # ---- tax --------------------------------------------------------------
    _created(owner.post(f"{API}/tax-years", json={"year": 2025, "filing_status": "single"}))
    _created(
        owner.post(
            f"{API}/tax-years/2025/checklist",
            json={"expected_form": "1099-INT", "issuer": "Synthetic Savings Bank"},
        )
    )
    _created(
        owner.put(
            f"{API}/tax-category-map/{book.book.rent.id}",
            json={"schedule": "Schedule E", "line": "19"},
        )
    )
    with factories.transaction() as db, household_context(db, hid):
        year = db.execute(
            select(TaxYear).where(TaxYear.household_id == hid, TaxYear.year == 2025)
        ).scalar_one()
        tax_document = make_document(
            db,
            store,
            factories.keyring,
            hid,
            data=TAX_FORM_BYTES,
            filename="W-2 2025.csv",
            kind=DocumentKind.TAX_FORM,
        )
        make_form_data(db, tax_document, tax_year=2025, tax_year_row=year)
        db.add(
            TaxChecklistItem(
                household_id=hid,
                tax_year_id=year.id,
                expected_form="W-2",
                issuer="Synthetic Employer Inc",
                status=ChecklistStatus.RECEIVED,
                document_id=tax_document.id,
            )
        )
        db.add(
            DocumentLink(
                household_id=hid,
                document_id=tax_document.id,
                target_type=LinkTarget.TAX_YEAR,
                target_id=year.id,
            )
        )
        # ---- learned layout and net worth history -------------------------
        savings = db.get(Account, book.book.savings.id)
        assert savings is not None
        assert savings.institution_id is not None
        remember_layout(
            db,
            hid,
            institution_id=savings.institution_id,
            kind=DocumentKind.STATEMENT,
            match_rules=FINGERPRINT_RULES,
            parser_name="csv_generic",
            document_id=book.document.id,
        )
        snapshot_household(db, hid, date(2026, 6, 30), max_days=5)
        db.flush()

    # ---- AI client --------------------------------------------------------
    with factories.transaction() as db:
        _, ai_token = make_ai_client(
            db,
            owner.record,
            factories.settings,
            name="Synthetic assistant",
            mode=AiMode.AGGREGATES,
            rounding=10,
            custom_denylist=["Synthetic School"],
        )
    return Wealth(
        brokerage=brokerage,
        ira=ira,
        mortgage=mortgage,
        house=house,
        tax_document=tax_document,
        security_ids=[syn_a["id"], syn_b["id"]],
        ai_token=ai_token,
    )
