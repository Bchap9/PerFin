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
from app.ingestion.parsers.brokerage_table import read_activity_table
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
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
    broker_activity_csv,
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
        names = {s.id: s.symbol or s.name for s in self.query(select(Security))}
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
        names = {s.id: s.symbol or s.name for s in self.query(select(Security))}
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
        "MYSTERY SYNTHETIC SPECIAL ITEM (Cash)",
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
        "unknown_actions": [{"action": "mystery synthetic special", "rows": 1}],
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


def _import(
    env: Env, data: bytes, *, account: Account | None = None
) -> tuple[imports_service.ImportResult, list[ReviewItem]]:
    """Import ``data``; ``account``: the document's (an export that names none)."""
    cells = extract_delimited(data, ExtractOptions(), "csv").tables[0].rows
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        result = table_result(cells)
        document = make_extracted_document(
            db, env.household_id, account=account, result=result, uploaded_by=env.owner.user.id
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


def test_a_sale_of_units_held_before_the_export_opens_them_at_unknown_cost(env: Env) -> None:
    """An export starts on a date: units sold before it buys them were held already."""
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
    assert outcome.summary["investments"]["held"] == 0
    (item,) = items
    assert item.payload["reason"] == "investment_opening_basis_unknown"
    assert item.payload["account_id"] == str(account.id)
    (opened,) = item.payload["securities"]
    assert (opened["quantity"], opened["trade_date"]) == ("5.0000000000", "2026-10-20")
    assert env.activity(account) == [
        (date(2026, 10, 20), "sell", "SGRX", Decimal(5), Decimal("50.00")),
        (date(2026, 10, 20), "transfer_in", "SGRX", Decimal(5), Decimal(0)),
    ]
    assert env.holdings(account) == []
    assert [a for _, a, _ in env.cash(account)] == [Decimal("100.00")]
    # Importing the same file again adds nothing.
    again, _ = _import(env, activity_csv_bytes([sale, deposit]))
    assert again.summary["investments"]["activity"] == 0
    assert len(env.activity(account)) == 2


def test_a_table_that_is_not_brokerage_activity_goes_through_the_mapping_path(env: Env) -> None:
    synthetic = activity_accounts(67)
    rows = activity_rows(67, synthetic)
    accounts = env.accounts(synthetic)
    outcome, items = _import(env, activity_csv_bytes(rows, header=LEDGER_HEADER))
    assert "brokerage_table" not in outcome.summary
    assert "investments" not in outcome.summary
    # Without an action column it is not brokerage activity; with a symbol, a
    # quantity and a price it is not a cash ledger on a guess either: the
    # mapping path asks a person, and nothing is imported as cash.
    assert outcome.status is DocumentStatus.NEEDS_REVIEW
    assert outcome.summary["mapping_suggested"] is False
    (item,) = items
    assert item.payload["reason"] == "mapping_required"
    assert item.payload["auto_import"] == "not_confident"
    assert item.payload["suggested_spec"]["mode"] == "cash"
    assert "investment_columns" in item.payload["suggestion"]["doubts"]
    for account in accounts:
        assert account is not None
        assert env.activity(account) == []
        assert env.cash(account) == []
    # The same rows with the brokerage header are investment activity.
    assert HEADER != LEDGER_HEADER


def test_the_same_trades_dated_by_settlement_are_not_recorded_twice(env: Env) -> None:
    """The activity CSV dates a trade by its run date, the PDF by its settlement date."""
    synthetic = activity_accounts(67)
    rows = investment_rows(synthetic)
    accounts = env.accounts(synthetic)
    data = activity_csv_bytes(rows)
    document_id = env.ingest(data)
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    before = {a.id: env.activity(a) for a in accounts if a is not None}
    held = {a.id: env.holdings(a) for a in accounts if a is not None}

    # The same activity as a statement whose table has only a settlement
    # date (the Fidelity PDF): every settled trade is dated a day later.
    found = read_activity_table(extract_delimited(data, ExtractOptions(), "csv").tables[0])
    assert found is not None
    settled = [
        statement.model_copy(
            update={
                "source_format": "ofx",
                "parser": None,
                "reconciliation": None,
                "activity": [
                    a.model_copy(
                        update={"trade_date": a.settle_date or a.trade_date, "settle_date": None}
                    )
                    for a in statement.activity
                    if a.type not in ("deposit", "withdrawal")
                ],
            }
        )
        for statement in found.statements
    ]
    moved = sum(1 for s in found.statements for a in s.activity if a.settle_date is not None)
    assert moved > 0
    result = ExtractResult(kind="ofx", ok=True, extractor_version="ofx/1", investments=settled)
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        document = make_extracted_document(
            db, env.household_id, result=result, uploaded_by=env.owner.user.id
        )
        outcome = imports_service.import_extraction(db, document, result)
        items = list(
            db.execute(select(ReviewItem).where(ReviewItem.document_id == document.id)).scalars()
        )
        db.expunge_all()
    assert {a.id: env.activity(a) for a in accounts if a is not None} == before
    assert {a.id: env.holdings(a) for a in accounts if a is not None} == held
    assert outcome.summary["investments"]["possible_duplicates"] == moved
    assert outcome.status is DocumentStatus.NEEDS_REVIEW
    near = [i for i in items if i.payload.get("reason") == "investment_possible_duplicate"]
    assert sum(i.payload["rows"] for i in near) == moved
    recorded = {str(t.id) for t in env.query(select(InvestmentTxn))}
    for item in near:
        assert item.kind is ReviewKind.LOW_CONFIDENCE_FIELD
        assert set(item.payload["duplicate_of"]) <= recorded
        # Ids and counts only: no statement text, no account digits.
        for account_ in synthetic:
            assert account_.last4 not in str(without_ids(item.payload))


# ---------------------------------------------------------------------------
# Masked account numbers, activity-mode mappings (Fidelity exports)
# ---------------------------------------------------------------------------


def _masked(synthetic: tuple[ActivityAccount, ...]) -> tuple[ActivityAccount, ...]:
    """The same accounts as an export that masks the numbers (``Z2xxxxxxxx``)."""
    return tuple(
        ActivityAccount(a.name, a.number[:1] + str(i + 2) + "x" * (len(a.number) - 2))
        for i, a in enumerate(synthetic)
    )


def test_masked_accounts_stay_apart_and_are_chosen_one_at_a_time(env: Env) -> None:
    synthetic = _masked(activity_accounts(81))
    rows = investment_rows(synthetic)
    document_id = env.ingest(activity_csv_bytes(rows))
    items = [i for i in env.open_reviews(document_id) if i.kind is ReviewKind.UNKNOWN_ACCOUNT]
    assert sorted(i.payload["statement"] for i in items) == [0, 1]
    assert {i.payload["account_type"] for i in items} == {"brokerage", "retirement"}
    first = env.ledger.account(
        env.household_id, type=AccountType.BROKERAGE, currency="USD", name="Mine"
    )
    env.rerun(document_id, account_id=first.id, statement=0)
    assert env.activity(first) == _expected(rows, synthetic[0])
    left = [i for i in env.open_reviews(document_id) if i.kind is ReviewKind.UNKNOWN_ACCOUNT]
    assert [i.payload["statement"] for i in left] == [1]


def _mostly_unknown(synthetic: tuple[ActivityAccount, ...]) -> list[ActivityRow]:
    buy = ActivityRow(
        date(2026, 10, 2),
        synthetic[0],
        "YOU BOUGHT SYNTHETIC GROWTH INDEX ETF (SGRX) (Cash)",
        "SGRX",
        "SYNTHETIC GROWTH INDEX ETF",
        Decimal("10.00"),
        Decimal(2),
        Decimal("-20.00"),
        None,
    )
    odd = [
        ActivityRow(
            date(2026, 10, 3 + i),
            synthetic[0],
            f"MYSTERY ITEM {i}",
            "",
            "No Description",
            None,
            Decimal(0),
            Decimal("-1.00"),
            None,
        )
        for i in range(3)
    ]
    return [buy, *odd]


def _activity_spec(rows: list[ActivityRow]) -> dict[str, Any]:
    from app.ingestion.parsers.brokerage_table import activity_spec

    cells = extract_delimited(activity_csv_bytes(rows), ExtractOptions(), "csv").tables[0]
    spec = activity_spec(cells)
    assert spec is not None
    return spec.model_dump(mode="json")


def test_a_saved_activity_mapping_imports_activity_not_cash(env: Env) -> None:
    synthetic = activity_accounts(82)
    rows = _mostly_unknown(synthetic)
    (account, _) = env.accounts(synthetic, only=(0,))
    assert account is not None
    data = activity_csv_bytes(rows)
    cells = extract_delimited(data, ExtractOptions(), "csv").tables[0].rows
    with env.factories.transaction() as db:
        make_import_mapping(db, env.household_id, spec=_activity_spec(rows), rows=cells)
    document_id = env.ingest(data)
    assert env.activity(account) == [
        (date(2026, 10, 2), "buy", "SGRX", Decimal(2), Decimal("-20.00"))
    ]
    assert env.cash(account) == []  # the unknown rows are skipped, never cash
    (skipped,) = [
        i for i in env.open_reviews(document_id) if i.payload.get("reason") == "lines_skipped"
    ]
    assert skipped.payload["unknown_activity"] == 3
    assert skipped.payload["unknown_actions"] == [{"action": "mystery item #", "rows": 3}]


def test_an_activity_mapping_chosen_on_a_rerun_reads_activity(env: Env) -> None:
    synthetic = activity_accounts(83)
    rows = _mostly_unknown(synthetic)
    (account, _) = env.accounts(synthetic, only=(0,))
    assert account is not None
    data = activity_csv_bytes(rows)
    document_id = env.ingest(data)
    # Not recognised on its own and never imported as cash on a guess.
    assert env.activity(account) == []
    assert env.cash(account) == []
    (required,) = [
        i for i in env.open_reviews(document_id) if i.payload.get("reason") == "mapping_required"
    ]
    assert required.payload["suggested_spec"]["mode"] == "activity"
    assert required.payload["auto_import"] == "not_confident"
    cells = extract_delimited(data, ExtractOptions(), "csv").tables[0].rows
    with env.factories.transaction() as db:
        mapping = make_import_mapping(db, env.household_id, spec=_activity_spec(rows), rows=cells)
        mapping_id = mapping.id
    env.rerun(document_id, mapping_id=mapping_id)
    assert [r[1] for r in env.activity(account)] == ["buy"]
    assert env.cash(account) == []


# ---------------------------------------------------------------------------
# Options in a broker's activity export (one account per file)
# ---------------------------------------------------------------------------

_PUT = "ZZOP 06/19/2026 40.00 P"
_PUT_OCC = "ZZOP260619P00040000"
_PUT_NAME = "PUT SYNTHETIC OPTION CO $40 EXP 06/19/26"


def _broker_row(
    day: str,
    action: str,
    *,
    symbol: str = "SGRX",
    quantity: str = "",
    price: str = "",
    fees: str = "",
    amount: str = "",
) -> list[str]:
    names = {"SGRX": "SYNTHETIC GROWTH INDEX ETF", _PUT: _PUT_NAME}
    return [day, action, symbol, names.get(symbol, ""), quantity, price, fees, amount]


def _broker_import(
    env: Env, *rows: list[str]
) -> tuple[Account, imports_service.ImportResult, list[ReviewItem]]:
    account = env.ledger.account(
        env.household_id, type=AccountType.BROKERAGE, currency="USD", name="Synthetic Taxable"
    )
    outcome, items = _import(env, broker_activity_csv(rows), account=account)
    return account, outcome, items


def _option_trades(env: Env, account: Account) -> list[Row]:
    return [row for row in env.activity(account) if row[2] == _PUT_OCC]


def test_written_option_in_an_activity_export(env: Env) -> None:
    """An option sold to open is left out with its closing; the rest of the file imports."""
    account, outcome, items = _broker_import(
        env,
        _broker_row(
            "10/14/2026", "Buy", quantity="10", price="$25.00", fees="$4.95", amount="-$254.95"
        ),
        _broker_row("10/15/2026", "MoneyLink Transfer", symbol="", amount="$500.00"),
        _broker_row(
            "10/16/2026",
            "Sell to Open",
            symbol=_PUT,
            quantity="1",
            price="$1.10",
            fees="$0.65",
            amount="$109.35",
        ),
        _broker_row(
            "10/21/2026",
            "Buy to Close",
            symbol=_PUT,
            quantity="1",
            price="$0.40",
            fees="$0.65",
            amount="-$40.65",
        ),
    )
    assert outcome.summary["investments"]["held"] == 0
    assert env.activity(account) == [
        (date(2026, 10, 14), "buy", "SGRX", Decimal(10), Decimal("-254.95")),
    ]
    assert env.holdings(account) == [("SGRX", Decimal(10))]
    assert [a for _, a, _ in env.cash(account)] == [Decimal("500.00")]
    # No opening at an unknown cost basis is made up for the option.
    (item,) = items
    assert item.payload["reason"] == "investment_short_position"
    assert item.payload["from_activity"] is True
    assert item.payload["lines_left_out"] == 2
    assert item.payload["positions"] == [
        {
            "symbol": _PUT_OCC,
            "name": _PUT_NAME,
            "quantity": "-1",
            "market_value": None,
            "page": None,
        }
    ]
    assert env.query(select(Security).where(Security.symbol == _PUT_OCC)) == []


def test_a_stock_sold_before_the_exports_first_row_still_opens_at_unknown_basis(env: Env) -> None:
    """Only options are written: a share sold with none on record was held before the export."""
    account, _, items = _broker_import(
        env, _broker_row("10/20/2026", "Sell", quantity="3", price="$25.00", amount="$75.00")
    )
    assert [i.payload["reason"] for i in items] == ["investment_opening_basis_unknown"]
    assert env.activity(account) == [
        (date(2026, 10, 20), "sell", "SGRX", Decimal(3), Decimal("75.00")),
        (date(2026, 10, 20), "transfer_in", "SGRX", Decimal(3), Decimal(0)),
    ]


def test_long_option_round_trip_goes_through_the_lot_engine(env: Env) -> None:
    account, _, items = _broker_import(
        env,
        _broker_row(
            "10/14/2026",
            "Buy to Open",
            symbol=_PUT,
            quantity="2",
            price="$1.85",
            fees="$0.65",
            amount="-$370.65",
        ),
        _broker_row(
            "10/21/2026",
            "Sell to Close",
            symbol=_PUT,
            quantity="2",
            price="$2.40",
            fees="$0.65",
            amount="$479.35",
        ),
    )
    assert items == []
    assert _option_trades(env, account) == [
        (date(2026, 10, 14), "buy", _PUT_OCC, Decimal(2), Decimal("-370.65")),
        (date(2026, 10, 21), "sell", _PUT_OCC, Decimal(2), Decimal("479.35")),
    ]
    assert env.holdings(account) == []


def test_a_long_option_sold_in_a_later_export_is_not_written(env: Env) -> None:
    """The account's own history counts: units bought in an earlier file may be sold."""
    account = env.ledger.account(
        env.household_id, type=AccountType.BROKERAGE, currency="USD", name="Synthetic Taxable"
    )
    bought = _broker_row(
        "10/14/2026", "Buy to Open", symbol=_PUT, quantity="2", price="$1.85", amount="-$370.00"
    )
    sold = _broker_row(
        "10/21/2026", "Sell to Close", symbol=_PUT, quantity="2", price="$2.40", amount="$480.00"
    )
    _, first = _import(env, broker_activity_csv([bought]), account=account)
    _, second = _import(env, broker_activity_csv([sold]), account=account)
    assert (first, second) == ([], [])
    assert len(_option_trades(env, account)) == 2
    assert env.holdings(account) == []

    # The second file read again: its own sale is not counted twice.
    cells = extract_delimited(broker_activity_csv([sold]), ExtractOptions(), "csv").tables[0].rows
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        sale = db.execute(
            select(InvestmentTxn).where(
                InvestmentTxn.account_id == account.id, InvestmentTxn.type == "sell"
            )
        ).scalar_one()
        document = db.get(Document, sale.source_document_id)
        assert document is not None
        imports_service.import_extraction(db, document, table_result(cells))
        again = list(
            db.execute(select(ReviewItem).where(ReviewItem.document_id == document.id)).scalars()
        )
    assert again == []
    assert len(_option_trades(env, account)) == 2
    assert env.holdings(account) == []


def _put_row(day: str, action: str, quantity: str, amount: str) -> list[str]:
    return _broker_row(day, action, symbol=_PUT, quantity=quantity, price="$1.00", amount=amount)


def test_a_long_option_closed_and_then_written_keeps_its_closing_sale(env: Env) -> None:
    """Only the lines that move the option while it is short are left out."""
    account = env.ledger.account(
        env.household_id, type=AccountType.BROKERAGE, currency="USD", name="Synthetic Taxable"
    )
    first = [_put_row("10/14/2026", "Buy to Open", "1", "-$185.00")]
    second = [
        _put_row("10/21/2026", "Sell to Close", "1", "$240.00"),
        _put_row("10/22/2026", "Sell to Open", "1", "$200.00"),
        _put_row("10/28/2026", "Buy to Close", "1", "-$50.00"),
    ]
    _, before = _import(env, broker_activity_csv(first), account=account)
    _, items = _import(env, broker_activity_csv(second), account=account)

    assert before == []
    # The option bought in the first file is sold: the account holds none.
    assert _option_trades(env, account) == [
        (date(2026, 10, 14), "buy", _PUT_OCC, Decimal(1), Decimal("-185.00")),
        (date(2026, 10, 21), "sell", _PUT_OCC, Decimal(1), Decimal("240.00")),
    ]
    assert env.holdings(account) == []
    (item,) = items
    assert item.payload["reason"] == "investment_short_position"
    assert item.payload["lines_left_out"] == 2
    (position,) = item.payload["positions"]
    assert position["quantity"] == "-1"


def test_a_long_round_trip_before_a_written_one_in_one_file_is_kept(env: Env) -> None:
    account, _, items = _broker_import(
        env,
        _put_row("10/14/2026", "Buy to Open", "1", "-$185.00"),
        _put_row("10/21/2026", "Sell to Close", "1", "$240.00"),
        _put_row("10/22/2026", "Sell to Open", "1", "$200.00"),
        _put_row("10/28/2026", "Buy to Close", "1", "-$50.00"),
    )
    assert [(day, kind) for day, kind, _, _, _ in _option_trades(env, account)] == [
        (date(2026, 10, 14), "buy"),
        (date(2026, 10, 21), "sell"),
    ]
    assert env.holdings(account) == []
    (item,) = items
    assert item.payload["lines_left_out"] == 2


def test_one_sale_that_closes_and_writes_leaves_the_whole_option_out(env: Env) -> None:
    """Negative: a line that sells more than is held cannot be split, so no line of it is kept."""
    account, _, items = _broker_import(
        env,
        _put_row("10/14/2026", "Buy to Open", "1", "-$185.00"),
        _put_row("10/21/2026", "Sell to Open", "2", "$400.00"),
        _put_row("10/28/2026", "Buy to Close", "1", "-$50.00"),
    )
    assert _option_trades(env, account) == []
    assert env.holdings(account) == []
    (item,) = items
    assert item.payload["reason"] == "investment_short_position"
    assert item.payload["lines_left_out"] == 3
