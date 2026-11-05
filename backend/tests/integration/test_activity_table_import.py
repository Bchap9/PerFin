# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brokerage activity exports (CSV/TSV/XLSX) end to end (INGESTION.md "Brokerage activity exports").

upload -> worker ``ingest_document`` -> real sandbox (delimited extractor) ->
structured importer (``app.domain.imports.service.import_extraction``) ->
brokerage activity table parser (``app.ingestion.parsers.brokerage_table``) ->
``app.domain.investments.statement_import``: investment transactions, lots
and holdings per account, cash deposits and withdrawals as ledger rows. The
file is the synthetic Fidelity-style export of
``tests/fixtures/synth_activity_csv.py`` (the real header, invented accounts,
funds and amounts).

* Each account's rows land on the account with its last four digits; the
  document is linked to neither; the same file again (re-upload or
  apply-mapping re-run) adds nothing.
* A saved cash-ledger mapping for the same header does not win silently:
  the summary names it (``mapping_bypassed``) and choosing it explicitly
  imports the table as a cash ledger.
* An account no account matches waits (``unknown_account``, the digits read
  again from the stored table on "remember").
* Unknown actions are skipped and reported; a guessed date order holds the
  statement; a sale of units never held is a lot conflict; a table that is
  not brokerage activity still goes through the column-mapping path.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.models import AuditLog, Event
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentStatus,
    IngestJob,
    IngestStage,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports import service as imports_service
from app.domain.imports.models import ImportMapping
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE, ApplyMapping
from app.domain.investments.models import Holding, InvestmentTxn, Security
from app.domain.transactions.models import Transaction
from app.ingestion.extract.delimited import extract_delimited
from app.ingestion.intake.upload import receive_upload
from app.ingestion.sandbox.protocol import ExtractOptions
from app.workers.handlers import ingest
from tests.fixtures.factories import Factories, UserRecord, without_ids
from tests.fixtures.households import Actors
from tests.fixtures.imports import (
    make_extracted_document,
    make_import_mapping,
    run_jobs,
    table_result,
)
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.synth_activity_csv import (
    HEADER,
    LEDGER_HEADER,
    SPEC,
    ActivityAccount,
    ActivityRow,
    activity_accounts,
    activity_csv_bytes,
    activity_rows,
    investment_rows,
)

pytestmark = pytest.mark.integration

API = "/api/v1"
MAX = 10 * 1024 * 1024
Q = Decimal("0.000001")
#: The leading words of the fixture's actions and the type each is.
TYPES = {
    "YOU BOUGHT": "buy",
    "YOU SOLD": "sell",
    "REINVESTMENT": "reinvest",
    "DIVIDEND RECEIVED": "dividend",
    "INTEREST EARNED": "interest",
    "FEE CHARGED": "fee",
    "DIRECT DEPOSIT": "deposit",
    "Electronic Funds Transfer Received": "deposit",
    "Electronic Funds Transfer Paid": "withdrawal",
}
CASH = ("deposit", "withdrawal")
Row = tuple[date, str, str | None, Decimal | None, Decimal]


@dataclass
class Env:
    factories: Factories
    ledger: LedgerFactories
    store: LocalFSBlobStore
    keyring: Keyring
    settings: Settings
    owner: UserRecord
    done: set[uuid.UUID] = field(default_factory=set)

    @property
    def household_id(self) -> uuid.UUID:
        assert self.owner.household_id is not None
        return self.owner.household_id

    def principal(self) -> Principal:
        return Principal(
            user=self.owner.user,
            household_id=self.household_id,
            role=self.owner.role,
            auth_method="session",
        )

    def ingest(self, data: bytes, filename: str = "activity.csv") -> uuid.UUID:
        with self.factories.transaction() as db:
            apply_rls_context(db, self.principal())
            received = receive_upload(
                db,
                self.principal(),
                store=self.store,
                keyring=self.keyring,
                chunks=iter([data]),
                max_bytes=MAX,
                filename=filename,
                account_id=None,
            )
        assert run_jobs(self.factories, self.household_id, INGEST_JOB_TYPE, done=self.done)
        return received.document_id

    def rerun(self, document_id: uuid.UUID, **choices: Any) -> None:
        with self.factories.transaction() as db:
            apply_rls_context(db, self.principal())
            imports_service.request_apply(
                db, self.principal(), document_id, ApplyMapping(**choices), request=None
            )
        self.run_apply()

    def run_apply(self) -> None:
        assert run_jobs(self.factories, self.household_id, APPLY_MAPPING_JOB_TYPE, done=self.done)

    def query(self, statement: Any) -> list[Any]:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            rows = list(db.execute(statement).scalars())
            db.expunge_all()
            return rows

    def document(self, document_id: uuid.UUID) -> Document:
        found: list[Document] = self.query(select(Document).where(Document.id == document_id))
        return found[0]

    def open_reviews(self, document_id: uuid.UUID) -> list[ReviewItem]:
        return [
            r
            for r in self.query(select(ReviewItem).where(ReviewItem.document_id == document_id))
            if r.state is ReviewItemState.OPEN
        ]

    def parse_logs(self, document_id: uuid.UUID) -> list[dict[str, Any]]:
        jobs: list[IngestJob] = self.query(
            select(IngestJob)
            .where(IngestJob.document_id == document_id, IngestJob.stage == IngestStage.PARSE)
            .order_by(IngestJob.started_at, IngestJob.id)
        )
        return [j.log or {} for j in jobs]

    def activity(self, account: Account) -> list[Row]:
        names = {s.id: s.symbol for s in self.query(select(Security))}
        txns: list[InvestmentTxn] = self.query(
            select(InvestmentTxn).where(InvestmentTxn.account_id == account.id)
        )
        return sorted(
            (
                t.trade_date,
                str(t.type),
                names[t.security_id] if t.security_id else None,
                t.quantity,
                t.amount,
            )
            for t in txns
        )

    def cash(self, account: Account) -> list[tuple[date, Decimal, str | None]]:
        rows: list[Transaction] = self.query(
            select(Transaction).where(Transaction.account_id == account.id)
        )
        return sorted((t.posted_date, t.amount, t.raw_payee) for t in rows)

    def holdings(self, account: Account) -> list[tuple[str, Decimal]]:
        names = {s.id: s.symbol for s in self.query(select(Security))}
        rows: list[Holding] = self.query(select(Holding).where(Holding.account_id == account.id))
        return sorted((names[h.security_id], h.quantity) for h in rows if h.quantity != 0)

    def mappings(self) -> list[ImportMapping]:
        return self.query(select(ImportMapping))

    def count(self, model: Any) -> int:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            return int(db.execute(select(func.count()).select_from(model)).scalar_one())

    def trail(self) -> str:
        audits = [without_ids(a.after) for a in self.query(select(AuditLog))]
        events = [without_ids(e.payload) for e in self.query(select(Event))]
        logs = [without_ids(j.log) for j in self.query(select(IngestJob)) if j.log is not None]
        items = [without_ids(r.payload) for r in self.query(select(ReviewItem))]
        return str([audits, events, logs, items])

    def accounts(
        self, synthetic: tuple[ActivityAccount, ...], *, only: tuple[int, ...] = (0, 1)
    ) -> list[Account | None]:
        kinds = (AccountType.BROKERAGE, AccountType.RETIREMENT)
        return [
            self.ledger.account(
                self.household_id,
                type=kind,
                number_last4=account.last4,
                currency="USD",
                name=account.name,
            )
            if index in only
            else None
            for index, (account, kind) in enumerate(zip(synthetic, kinds, strict=True))
        ]


@pytest.fixture
def env(
    factories: Factories,
    ledger: LedgerFactories,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Env]:
    local = settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )
    monkeypatch.setattr(ingest, "get_settings", lambda: local)
    ingest.default_deps.cache_clear()
    deps = ingest.default_deps()
    assert isinstance(deps.store, LocalFSBlobStore)
    yield Env(
        factories=factories,
        ledger=ledger,
        store=deps.store,
        keyring=deps.keyring,
        settings=local,
        owner=factories.user(role="owner", with_totp=True),
    )
    ingest.default_deps.cache_clear()


def _items(env: Env, document_id: uuid.UUID) -> list[tuple[str, dict[str, Any]]]:
    return [(str(r.kind), r.payload) for r in env.open_reviews(document_id)]


def _type(row: ActivityRow) -> str:
    return next(kind for words, kind in TYPES.items() if row.action.startswith(words))


def _expected(rows: list[ActivityRow], account: ActivityAccount) -> list[Row]:
    """The fixture's non-cash rows as ``investment_txn`` stores them."""
    out: list[Row] = []
    for row in rows:
        kind = _type(row)
        if row.account != account or kind in CASH or row.amount is None:
            continue
        quantity = abs(row.quantity) if row.quantity else None
        out.append((row.run_date, kind, row.symbol or None, quantity, row.amount.quantize(Q)))
    return sorted(out)


def _expected_cash(
    rows: list[ActivityRow], account: ActivityAccount
) -> list[tuple[date, Decimal, str | None]]:
    return sorted(
        (r.run_date, r.amount.quantize(Q), r.action)
        for r in rows
        if r.account == account and _type(r) in CASH and r.amount is not None
    )


def _units(rows: list[ActivityRow], account: ActivityAccount) -> list[tuple[str, Decimal]]:
    held: dict[str, Decimal] = {}
    for row in rows:
        if row.account == account and _type(row) in ("buy", "sell", "reinvest"):
            held[row.symbol] = held.get(row.symbol, Decimal(0)) + row.quantity
    return sorted((symbol, units) for symbol, units in held.items() if units)


# ---------------------------------------------------------------------------
# Through the worker
# ---------------------------------------------------------------------------


def test_activity_export_lands_as_investment_activity_on_each_account(env: Env) -> None:
    synthetic = activity_accounts(61)
    rows = investment_rows(synthetic)
    accounts = env.accounts(synthetic)
    data = activity_csv_bytes(rows)
    document_id = env.ingest(data)

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, _items(env, document_id)
    assert document.account_id is None  # two accounts: linked to neither
    assert (document.period_start, document.period_end) == (
        min(r.run_date for r in rows),
        max(r.run_date for r in rows),
    )
    for account, mine in zip(accounts, synthetic, strict=True):
        assert account is not None
        assert env.activity(account) == _expected(rows, mine), mine.name
        assert env.cash(account) == _expected_cash(rows, mine)
        assert env.holdings(account) == _units(rows, mine)
    taxable = accounts[0]
    assert taxable is not None
    assert env.holdings(taxable) == [
        ("SBND", Decimal(40)),
        ("SGRX", Decimal("6.05")),
    ]
    # Not a cash ledger: no mapping was suggested or saved.
    assert env.mappings() == []
    (log,) = env.parse_logs(document_id)
    assert log["brokerage_table"]["statements"] == 2
    assert log["brokerage_table"]["rows"] == len(rows)
    assert log["brokerage_table"]["notes"] == 3
    assert log["mapping_bypassed"] is None
    assert log["mapping_suggested"] is False
    assert log["investments"]["activity"] == sum(1 for r in rows if _type(r) not in CASH)
    assert log["investments"]["cash_rows"] == sum(1 for r in rows if _type(r) in CASH)
    # Account numbers never reach the trail, not even the last four digits.
    trail = env.trail()
    for account_ in synthetic:
        assert account_.number not in trail
        assert account_.last4 not in trail

    # The same export again adds nothing to either account.
    before = {a.id: (env.activity(a), env.cash(a)) for a in accounts if a is not None}
    # A later download of the same rows (another file: the intake would
    # recognise identical bytes before any import).
    again = env.ingest(activity_csv_bytes(rows, downloaded=date(2026, 11, 3)), "again.csv")
    assert env.document(again).status is DocumentStatus.PROCESSED, _items(env, again)
    assert {a.id: (env.activity(a), env.cash(a)) for a in accounts if a is not None} == before
    (log,) = env.parse_logs(again)
    assert log["investments"]["duplicates"] == 11  # every non-cash row
    assert log["investments"]["activity"] == 0
    assert log["duplicates"] == 3  # the three cash rows

    # So does re-running the import from the stored extraction.
    env.rerun(document_id)
    assert {a.id: (env.activity(a), env.cash(a)) for a in accounts if a is not None} == before
    rerun = env.parse_logs(document_id)[-1]
    assert rerun["rerun"] is True
    assert rerun["investments"]["activity"] == 0
    assert env.document(document_id).status is DocumentStatus.PROCESSED


def test_a_saved_cash_mapping_is_named_and_wins_only_when_chosen(env: Env) -> None:
    synthetic = activity_accounts(62)
    rows = investment_rows(synthetic)
    accounts = env.accounts(synthetic)
    data = activity_csv_bytes(rows)
    cells = extract_delimited(data, ExtractOptions(), "csv").tables[0].rows
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        mapping_id = make_import_mapping(db, env.household_id, spec=SPEC, rows=cells).id

    document_id = env.ingest(data)
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    (log,) = env.parse_logs(document_id)
    assert log["mapping_bypassed"] == str(mapping_id)
    assert log["mapping_id"] is None
    first = accounts[0]
    assert first is not None
    assert env.activity(first) == _expected(rows, synthetic[0])

    # Chosen explicitly, the mapping imports the table as a cash ledger.
    for db in _db(env):
        document = make_extracted_document(
            db, env.household_id, result=table_result(cells), uploaded_by=env.owner.user.id
        )
        outcome = imports_service.import_extraction(db, document, table_result(cells))
        assert "brokerage_table" in outcome.summary
        chosen = imports_service.import_extraction(
            db, document, table_result(cells), mapping_id=mapping_id
        )
        assert chosen.summary["mapping_id"] == str(mapping_id)
        assert "brokerage_table" not in chosen.summary
        assert "investments" not in chosen.summary


def _db(env: Env) -> Iterator[Any]:
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        yield db


def test_an_unmatched_account_waits_and_is_remembered(env: Env) -> None:
    synthetic = activity_accounts(63)
    rows = investment_rows(synthetic)
    first, _ = env.accounts(synthetic, only=(0,))
    assert first is not None
    document_id = env.ingest(activity_csv_bytes(rows))
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert env.activity(first) == _expected(rows, synthetic[0])
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.UNKNOWN_ACCOUNT
    assert item.payload["reason"] == "account_not_found"
    assert item.payload["investment"] is True
    assert item.payload["statement"] == 1
    assert synthetic[1].last4 not in str(without_ids(item.payload))

    roth = env.ledger.account(
        env.household_id, type=AccountType.RETIREMENT, currency="USD", name="Retirement"
    )
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        detail = owner.get(f"{API}/review/{item.id}").json()
        ranked = [a["value"] for a in detail["alternatives"] if a["field"] == "account_id"]
        assert str(roth.id) in ranked
        resolved = owner.post(
            f"{API}/review/{item.id}/resolve",
            json={"kind": "unknown_account", "account_id": str(roth.id), "remember": True},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["resolution"]["remembered"] == ["last4"]
    remembered: list[Account] = env.query(select(Account).where(Account.id == roth.id))
    assert remembered[0].number_last4 == synthetic[1].last4
    env.run_apply()
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.activity(roth) == _expected(rows, synthetic[1])
    assert env.cash(roth) == _expected_cash(rows, synthetic[1])
    assert env.activity(first) == _expected(rows, synthetic[0])  # not twice


def test_unknown_actions_are_skipped_and_reported(env: Env) -> None:
    synthetic = activity_accounts(64)
    rows = investment_rows(synthetic)
    odd = ActivityRow(
        rows[0].run_date,
        synthetic[0],
        "JOURNALED SYNTHETIC SPECIAL ITEM (Cash)",
        "",
        "No Description",
        None,
        Decimal(0),
        Decimal("-7.00"),
        None,
    )
    accounts = env.accounts(synthetic)
    document_id = env.ingest(activity_csv_bytes([odd, *rows]))
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.LOW_CONFIDENCE_FIELD
    assert item.payload == {
        "reason": "lines_skipped",
        "count": 1,
        "unknown_activity": 1,
        "parser": "brokerage_activity_table",
    }
    for account, mine in zip(accounts, synthetic, strict=True):
        assert account is not None
        assert env.activity(account) == _expected(rows, mine)
        assert env.cash(account) == _expected_cash(rows, mine)  # the odd row is not cash either
    # A re-run does not raise the same item again.
    env.rerun(document_id)
    assert len(env.open_reviews(document_id)) == 1


# ---------------------------------------------------------------------------
# Held statements and the fallback (stored extraction, no sandbox)
# ---------------------------------------------------------------------------


def _import(env: Env, data: bytes) -> tuple[imports_service.ImportResult, list[ReviewItem]]:
    cells = extract_delimited(data, ExtractOptions(), "csv").tables[0].rows
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        result = table_result(cells)
        document = make_extracted_document(
            db, env.household_id, result=result, uploaded_by=env.owner.user.id
        )
        outcome = imports_service.import_extraction(db, document, result)
        items = list(
            db.execute(select(ReviewItem).where(ReviewItem.document_id == document.id)).scalars()
        )
        db.expunge_all()
    return outcome, items


def test_a_guessed_date_order_holds_the_statement(env: Env) -> None:
    synthetic = activity_accounts(65)
    rows = [
        ActivityRow(
            date(2026, 10, 3 + i),
            synthetic[0],
            f"YOU BOUGHT SYNTHETIC GROWTH INDEX ETF (SGRX) (Cash) {i}",
            "SGRX",
            "SYNTHETIC GROWTH INDEX ETF",
            Decimal("10.00"),
            Decimal(1),
            Decimal("-10.00"),
            None,
        )
        for i in range(3)
    ]
    (account, _) = env.accounts(synthetic, only=(0,))
    assert account is not None
    outcome, items = _import(env, activity_csv_bytes(rows))
    assert outcome.status is DocumentStatus.NEEDS_REVIEW
    assert outcome.summary["brokerage_table"]["warnings"] == ["date_order_guessed"]
    assert outcome.summary["investments"]["held"] == 1
    (held,) = [i for i in items if i.payload.get("reason") == "investment_low_confidence"]
    assert held.payload["fields"] == ["trade_date"]
    assert env.activity(account) == []


def test_a_sale_of_units_never_held_is_a_lot_conflict(env: Env) -> None:
    synthetic = activity_accounts(66)
    sale = ActivityRow(
        date(2026, 10, 20),
        synthetic[0],
        "YOU SOLD SYNTHETIC GROWTH INDEX ETF (SGRX) (Cash)",
        "SGRX",
        "SYNTHETIC GROWTH INDEX ETF",
        Decimal("10.00"),
        Decimal(-5),
        Decimal("50.00"),
        None,
    )
    deposit = ActivityRow(
        date(2026, 10, 21),
        synthetic[0],
        "Electronic Funds Transfer Received (Cash)",
        "",
        "No Description",
        None,
        Decimal(0),
        Decimal("100.00"),
        None,
    )
    (account, _) = env.accounts(synthetic, only=(0,))
    assert account is not None
    outcome, items = _import(env, activity_csv_bytes([sale, deposit]))
    assert outcome.summary["investments"]["held"] == 1
    assert [i.payload["reason"] for i in items] == ["investment_lot_conflict"]
    assert env.activity(account) == []
    assert env.cash(account) == []  # nothing of the statement is kept


def test_a_table_that_is_not_brokerage_activity_goes_through_the_mapping_path(env: Env) -> None:
    synthetic = activity_accounts(67)
    rows = activity_rows(67, synthetic)
    accounts = env.accounts(synthetic)
    outcome, items = _import(env, activity_csv_bytes(rows, header=LEDGER_HEADER))
    assert outcome.status is DocumentStatus.PROCESSED, [i.payload for i in items]
    assert "brokerage_table" not in outcome.summary
    assert "investments" not in outcome.summary
    assert outcome.summary["mapping_suggested"] is True
    for account, mine in zip(accounts, synthetic, strict=True):
        assert account is not None
        assert env.activity(account) == []
        cash = sorted(
            (r.run_date, r.amount) for r in rows if r.account == mine and r.amount is not None
        )
        assert [(d, a) for d, a, _ in env.cash(account)] == [(d, a.quantize(Q)) for d, a in cash]
    # The same rows with the brokerage header are investment activity.
    assert HEADER != LEDGER_HEADER
