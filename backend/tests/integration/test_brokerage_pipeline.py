# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brokerage statements end to end through the worker (INGESTION.md "Brokerage statements").

upload -> worker ``ingest_document`` -> real sandbox (PDF pages, OFX
``INVSTMTRS``) -> classifier (shipped Placeholder Brokerage fingerprint) ->
brokerage parser -> reconciliation -> importer
(``app.domain.investments.statement_import``) -> securities, investment
transactions, opening positions, lots and holdings (the investments lot
engine), prices, a balance snapshot and cash rows in the ledger, with the
production pipeline dependencies on a temporary blob store. Statements come
from ``tests/fixtures/synth_brokerage.py`` (invented broker and instruments).

* A statement that reconciles is committed with provenance; holdings equal
  the reported positions; the next month adds only its own activity.
* The same month again (as OFX after the PDF, or a re-upload) adds nothing.
* A statement that does not reconcile (C12) writes nothing and waits in
  review with the discrepancy.
* Account matching respects grants (F3) and the account's type; a chosen
  account re-imports from the stored extraction.
* An older statement after a newer one is held (out of order).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel, HouseholdRole
from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType, BalanceSnapshot, BalanceSource
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentKind,
    DocumentStatus,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports.models import StatementRecord
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE, ApplyMapping
from app.domain.imports.service import request_apply
from app.domain.investments.models import (
    Holding,
    InvestmentTxn,
    InvestmentTxnType,
    Lot,
    Price,
    Security,
)
from app.domain.investments.statement_import import OPENING_MEMO
from app.domain.review import service as review_service
from app.domain.review.schemas import ResolveDuplicate
from app.domain.transactions.models import Transaction
from app.ingestion.intake.upload import receive_upload
from app.ingestion.sandbox.protocol import ExtractResult
from app.workers.handlers import ingest
from tests.fixtures import synth_brokerage as synth
from tests.fixtures import synth_fidelity as fid
from tests.fixtures import synth_fidelity_report as fidr
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.imports import run_jobs
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration

MAX = 10 * 1024 * 1024
Row = tuple[date, str, str | None, Decimal | None, Decimal]


@dataclass
class Env:
    factories: Factories
    ledger: LedgerFactories
    store: LocalFSBlobStore
    keyring: Keyring
    owner: UserRecord
    done: set[uuid.UUID] = field(default_factory=set)

    @property
    def household_id(self) -> uuid.UUID:
        assert self.owner.household_id is not None
        return self.owner.household_id

    def principal(self, record: UserRecord | None = None) -> Principal:
        record = record or self.owner
        return Principal(
            user=record.user,
            household_id=self.household_id,
            role=record.role,
            auth_method="session",
        )

    def ingest(
        self, data: bytes, filename: str, *, uploader: UserRecord | None = None
    ) -> uuid.UUID:
        principal = self.principal(uploader)
        with self.factories.transaction() as db:
            apply_rls_context(db, principal)
            received = receive_upload(
                db,
                principal,
                store=self.store,
                keyring=self.keyring,
                chunks=iter([data]),
                max_bytes=MAX,
                filename=filename,
                account_id=None,
            )
        assert run_jobs(self.factories, self.household_id, INGEST_JOB_TYPE, done=self.done)
        return received.document_id

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

    def reasons(self, document_id: uuid.UUID) -> set[str]:
        return {str(r.payload.get("reason")) for r in self.open_reviews(document_id)}

    def txns(self, account: Account) -> list[InvestmentTxn]:
        return self.query(
            select(InvestmentTxn)
            .where(InvestmentTxn.account_id == account.id)
            .order_by(InvestmentTxn.trade_date, InvestmentTxn.id)
        )

    def securities(self) -> dict[uuid.UUID, Security]:
        rows: list[Security] = self.query(select(Security))
        return {s.id: s for s in rows}

    def activity(self, account: Account) -> list[Row]:
        """Imported activity in the parser's terms (opening positions left out)."""
        names = self.securities()
        rows: list[Row] = []
        for t in self.txns(account):
            if t.memo == OPENING_MEMO:
                continue
            symbol = names[t.security_id].symbol if t.security_id else None
            rows.append((t.trade_date, str(t.type), symbol, t.quantity, t.amount))
        return sorted(rows, key=lambda r: (r[0], r[1], r[2] or "", r[4]))

    def holdings(self, account: Account) -> list[tuple[str, Decimal]]:
        names = self.securities()
        rows: list[Holding] = self.query(select(Holding).where(Holding.account_id == account.id))
        return sorted((names[h.security_id].symbol, h.quantity) for h in rows)

    def cash_rows(self, account: Account) -> list[Transaction]:
        return self.query(select(Transaction).where(Transaction.account_id == account.id))

    def snapshots(self, account: Account) -> list[BalanceSnapshot]:
        return self.query(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id))

    def brokerage(self, stmt: synth.SynthBrokerage, **kwargs: Any) -> Account:
        institution = self.ledger.institution(self.household_id, name=synth.INSTITUTION)
        kwargs.setdefault("type", AccountType.BROKERAGE)
        return self.ledger.account(
            self.household_id,
            number_last4=stmt.last4,
            currency=stmt.currency,
            institution_id=institution.id,
            name="Brokerage",
            **kwargs,
        )


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
        owner=factories.user(role="owner", with_totp=True),
    )
    ingest.default_deps.cache_clear()


def _q(value: Decimal | None) -> Decimal | None:
    return value.quantize(Decimal("0.000001")) if value is not None else None


def _expected(stmt: synth.SynthBrokerage, **kwargs: Any) -> list[Row]:
    """Expected activity as stored: cash transfers are ledger rows, amounts at 6 places."""
    return [
        (d, t, s, q, a.quantize(Decimal("0.000001")))
        for d, t, s, q, a in synth.expected_activity(stmt, **kwargs)
        if t not in ("deposit", "withdrawal")
    ]


def _cash(stmt: synth.SynthBrokerage) -> list[tuple[date, Decimal]]:
    return sorted(
        (a.day, a.amount.quantize(Decimal("0.000001")))
        for a in stmt.activity
        if a.type in ("deposit", "withdrawal")
    )


def _positions(stmt: synth.SynthBrokerage) -> list[tuple[str, Decimal]]:
    return sorted((p.symbol, p.quantity) for p in stmt.positions)


def _ofx_xml(stmt: synth.SynthBrokerage) -> bytes:
    return synth.ofx_investment(stmt, xml=True)


FORMATS: dict[str, tuple[Callable[[synth.SynthBrokerage], bytes], str]] = {
    "pdf": (synth.placeholder_pdf, "statement.pdf"),
    "ofx": (synth.ofx_investment, "statement.ofx"),
    "ofx_xml": (_ofx_xml, "statement.qfx"),
}


@pytest.mark.parametrize("fmt", sorted(FORMATS))
def test_brokerage_statement_is_parsed_reconciled_and_committed(env: Env, fmt: str) -> None:
    january, _ = synth.brokerage_series(seed=61)
    account = env.brokerage(january)
    render, filename = FORMATS[fmt]
    document_id = env.ingest(render(january), filename)

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert document.kind is DocumentKind.STATEMENT
    assert document.account_id == account.id
    assert (document.period_start, document.period_end) == (january.start, january.end)
    stored = ExtractResult.model_validate(document.extraction)
    assert len(stored.investments) == 1
    if fmt == "pdf":
        assert document.parser_version == "placeholder_brokerage/1"
        assert document.confidence is not None
        assert document.confidence["parse"]["investment_reconciliation"] == ["ok"]

    # Activity with provenance; cash transfers are ledger rows on the account.
    assert env.activity(account) == _expected(january)
    txns = env.txns(account)
    assert all(t.source_document_id == document_id and t.import_hash for t in txns)
    assert sorted((t.posted_date, t.amount) for t in env.cash_rows(account)) == _cash(january)
    assert all(t.source_document_id == document_id for t in env.cash_rows(account))

    # Holdings equal the reported positions: the units held before January
    # entered as opening positions, the month's trades went through the lots.
    assert env.holdings(account) == _positions(january)
    openings = [t for t in txns if t.memo == OPENING_MEMO]
    assert {env.securities()[t.security_id].symbol for t in openings if t.security_id} == {
        "PBXA",
        "PBXB",
        "PBXC",
    }
    assert all(t.type is InvestmentTxnType.TRANSFER_IN and t.amount == 0 for t in openings)
    assert all(t.trade_date == january.start for t in openings)
    sale = next(t for t in txns if t.type is InvestmentTxnType.SELL)
    assert sale.lot_links  # the lot engine named the lots it consumed
    lots: list[Lot] = env.query(select(Lot).where(Lot.account_id == account.id))
    assert sum(lot.remaining_quantity for lot in lots) == sum(p.quantity for p in january.positions)

    # Prices on the statement date and the account value as a snapshot.
    prices: list[Price] = env.query(select(Price).where(Price.price_date == january.end))
    by_id = env.securities()
    assert sorted((by_id[p.security_id].symbol, p.close) for p in prices) == sorted(
        (p.symbol, p.price) for p in january.positions
    )
    assert all(p.source == "statement" for p in prices)
    snapshots = env.snapshots(account)
    assert [(s.as_of_date, s.balance, s.source) for s in snapshots] == [
        (january.end, january.total_value, BalanceSource.STATEMENT)
    ]
    assert snapshots[0].source_document_id == document_id
    if fmt.startswith("ofx"):
        # The OFX names each instrument by CUSIP too.
        assert {s.cusip for s in by_id.values()} == {sec.cusip for sec in synth.SECURITIES}


def test_next_month_adds_its_activity_and_moves_the_holdings(env: Env) -> None:
    january, february = synth.brokerage_series(seed=62)
    account = env.brokerage(january)
    first = env.ingest(synth.placeholder_pdf(january), "january.pdf")
    second = env.ingest(synth.placeholder_pdf(february), "february.pdf")
    assert env.document(first).status is DocumentStatus.PROCESSED
    assert env.document(second).status is DocumentStatus.PROCESSED, env.open_reviews(second)
    assert env.activity(account) == sorted(
        [*_expected(january), *_expected(february)],
        key=lambda r: (r[0], r[1], r[2] or "", r[4]),
    )
    # February's history explains its positions: no new opening position.
    assert len([t for t in env.txns(account) if t.memo == OPENING_MEMO]) == 3
    assert env.holdings(account) == _positions(february)
    assert sorted(s.as_of_date for s in env.snapshots(account)) == [january.end, february.end]


def test_same_month_as_ofx_after_the_pdf_adds_nothing(env: Env) -> None:
    january, _ = synth.brokerage_series(seed=63)
    account = env.brokerage(january)
    pdf = env.ingest(synth.placeholder_pdf(january), "statement.pdf")
    before = env.txns(account)
    cash_before = len(env.cash_rows(account))

    document_id = env.ingest(synth.ofx_investment(january), "statement.ofx")
    # The same month in another format is the same statement: held for review.
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert (item.kind, item.payload["reason"]) == (
        ReviewKind.POSSIBLE_DUPLICATE,
        "statement_overlap",
    )
    assert item.payload["account_id"] == str(account.id)
    assert item.payload["investment"] is True
    assert item.payload["other_document_id"] == str(pdf)
    assert (item.payload["period_start"], item.payload["period_end"]) == (
        january.start.isoformat(),
        january.end.isoformat(),
    )
    assert [t.id for t in env.txns(account)] == [t.id for t in before]
    # Imported anyway: the row hashes still add nothing twice.
    _resolve_overlap(env, item, "reject")
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)
    assert env.document(document_id).status is DocumentStatus.PROCESSED, env.open_reviews(
        document_id
    )
    assert [t.id for t in env.txns(account)] == [t.id for t in before]
    assert len(env.cash_rows(account)) == cash_before
    assert env.holdings(account) == _positions(january)
    # The OFX filled in the CUSIPs of the securities the PDF created.
    assert {s.cusip for s in env.securities().values()} == {s.cusip for s in synth.SECURITIES}


def test_rerunning_the_same_document_never_trips_the_statement_guard(env: Env) -> None:
    january, _ = synth.brokerage_series(seed=71)
    account = env.brokerage(january)
    document_id = env.ingest(synth.placeholder_pdf(january), "statement.pdf")
    before = [t.id for t in env.txns(account)]
    (record,) = env.query(select(StatementRecord).where(StatementRecord.account_id == account.id))
    assert (record.kind, record.outcome, record.source_format) == ("investment", "imported", "pdf")
    assert (record.period_start, record.period_end, record.period_stated) == (
        january.start,
        january.end,
        True,
    )
    assert record.total_value == january.total_value
    _choose(env, document_id, account)
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.open_reviews(document_id) == []
    assert [t.id for t in env.txns(account)] == before
    assert len(env.query(select(StatementRecord))) == 1


def test_document_period_comes_from_the_header_band_of_its_statements(env: Env) -> None:
    """The multi-account report prints its period only in every page's header band."""
    stmt, extras = fidr.report()
    document_id = env.ingest(fidr.report_pdf(stmt, extras), "investment-report.pdf")
    document = env.document(document_id)
    assert document.kind is DocumentKind.STATEMENT
    assert (document.period_start, document.period_end) == (date(2019, 6, 1), date(2019, 6, 30))
    assert document.confidence is not None
    assert document.confidence["period"]["source"] == "header"
    stored = ExtractResult.model_validate(document.extraction)
    assert stored.investments
    assert {(i.period_start, i.period_end) for i in stored.investments} == {
        (date(2019, 6, 1), date(2019, 6, 30))
    }


@pytest.mark.parametrize("tamper", ["extra_row", "market_value", "total"])
def test_statement_that_does_not_reconcile_writes_nothing(env: Env, tamper: synth.Tamper) -> None:
    january, _ = synth.brokerage_series(seed=64)
    account = env.brokerage(january)
    document_id = env.ingest(synth.placeholder_pdf(january, tamper=tamper), "statement.pdf")

    document = env.document(document_id)
    assert document.status is DocumentStatus.NEEDS_REVIEW
    (item,) = [
        r
        for r in env.open_reviews(document_id)
        if r.payload.get("reason") == "investment_reconciliation_failed"
    ]
    assert item.kind is ReviewKind.LOW_CONFIDENCE_FIELD
    assert item.payload["account_id"] == str(account.id)
    assert item.payload["failed"]
    if tamper == "extra_row":
        assert item.payload["cash_difference"] == "-777.77"
    assert env.txns(account) == []
    assert env.holdings(account) == []
    assert env.cash_rows(account) == []
    assert env.snapshots(account) == []
    assert env.securities() == {}


def test_tampered_ofx_positions_are_held(env: Env) -> None:
    january, _ = synth.brokerage_series(seed=65)
    account = env.brokerage(january)
    document_id = env.ingest(synth.ofx_investment(january, tamper="market_value"), "x.ofx")
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert "investment_reconciliation_failed" in env.reasons(document_id)
    assert env.txns(account) == []


def test_unknown_account_is_chosen_later_from_the_stored_extraction(env: Env) -> None:
    january, _ = synth.brokerage_series(seed=66)
    document_id = env.ingest(synth.placeholder_pdf(january), "statement.pdf")
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert "account_not_found" in env.reasons(document_id)

    account = env.ledger.account(
        env.household_id, type=AccountType.BROKERAGE, currency="USD", name="Brokerage"
    )
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        request_apply(
            db, env.principal(), document_id, ApplyMapping(account_id=account.id), request=None
        )
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.activity(account) == _expected(january)
    assert env.holdings(account) == _positions(january)


def test_statement_never_lands_on_an_account_hidden_from_the_uploader(env: Env) -> None:
    january, _ = synth.brokerage_series(seed=67)
    account = env.brokerage(january)
    member = env.factories.user(
        role=HouseholdRole.MEMBER, household=env.owner.household, with_totp=True
    )
    env.ledger.grant(env.household_id, member.user.id, account, GrantLevel.VIEW)

    document_id = env.ingest(synth.placeholder_pdf(january), "statement.pdf", uploader=member)
    assert env.document(document_id).account_id is None
    assert "account_not_found" in env.reasons(document_id)
    assert env.txns(account) == []
    assert env.holdings(account) == []


def test_account_of_another_type_is_not_written(env: Env) -> None:
    january, _ = synth.brokerage_series(seed=68)
    checking = env.brokerage(january, type=AccountType.CHECKING)
    document_id = env.ingest(synth.ofx_investment(january), "statement.ofx")
    assert "account_type_mismatch" in env.reasons(document_id)
    assert env.txns(checking) == []
    assert env.cash_rows(checking) == []


def test_older_statement_after_a_newer_one_is_held(env: Env) -> None:
    january, february = synth.brokerage_series(seed=69)
    account = env.brokerage(january)
    env.ingest(synth.placeholder_pdf(february), "february.pdf")
    before = [t.id for t in env.txns(account)]
    holdings = env.holdings(account)
    assert holdings == _positions(february)

    document_id = env.ingest(synth.placeholder_pdf(january), "january.pdf")
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert "investment_out_of_order" in env.reasons(document_id)
    assert [t.id for t in env.txns(account)] == before
    assert env.holdings(account) == holdings


# ---------------------------------------------------------------------------
# Several accounts in one PDF (tests/fixtures/synth_fidelity.py)
# ---------------------------------------------------------------------------

FID_TYPES = (AccountType.BROKERAGE, AccountType.RETIREMENT, AccountType.RETIREMENT)


def _fid_accounts(
    env: Env, stmt: fid.FidStatement, *, only: tuple[int, ...] = (0, 1, 2)
) -> list[Account | None]:
    institution = env.ledger.institution(env.household_id, name=fid.INSTITUTION)
    out: list[Account | None] = []
    for index, (account, kind) in enumerate(zip(stmt.accounts, FID_TYPES, strict=True)):
        if index not in only:
            out.append(None)
            continue
        out.append(
            env.ledger.account(
                env.household_id,
                type=kind,
                number_last4=account.last4,
                currency="USD",
                institution_id=institution.id,
                name=account.title.title(),
            )
        )
    return out


def _fid_expected(account: fid.FidAccount) -> list[Row]:
    return [
        (d, t, s, q, a.quantize(Decimal("0.000001")))
        for d, t, s, q, a in fid.expected_activity(account)
        if t not in ("deposit", "withdrawal")
    ]


def _fid_cash(account: fid.FidAccount) -> list[tuple[date, Decimal]]:
    return sorted(
        (a.day, a.amount.quantize(Decimal("0.000001")))
        for a in account.activity
        if a.type in ("deposit", "withdrawal")
    )


def _fid_holdings(env: Env, account: Account) -> list[tuple[str, Decimal]]:
    return [(symbol, qty) for symbol, qty in env.holdings(account) if qty != 0]


def _fid_positions(account: fid.FidAccount) -> list[tuple[str, Decimal]]:
    return sorted((p.symbol, p.quantity) for p in account.positions)


def _resolve_overlap(env: Env, item: ReviewItem, decision: Literal["confirm", "reject"]) -> None:
    """Answer a ``statement_overlap`` item as the owner (the review queue's buttons)."""
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        review_service.resolve(
            db,
            env.principal(),
            item.id,
            ResolveDuplicate(kind="possible_duplicate", decision=decision),
            request=None,
        )


def _choose(env: Env, document_id: uuid.UUID, account: Account) -> None:
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        request_apply(
            db, env.principal(), document_id, ApplyMapping(account_id=account.id), request=None
        )
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)


def test_each_account_of_a_multi_account_pdf_lands_on_its_own_account(env: Env) -> None:
    stmt = fid.fidelity_statement(seed=91)
    accounts = _fid_accounts(env, stmt)
    document_id = env.ingest(fid.fidelity_pdf(stmt), "investment-report.pdf")

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert document.kind is DocumentKind.STATEMENT
    assert document.parser_version == "fidelity_investments_statement/1"
    # The document spans three accounts: it is linked to none of them.
    assert document.account_id is None
    assert (document.period_start, document.period_end) == (stmt.start, stmt.end)
    assert document.confidence is not None
    assert document.confidence["parse"]["investment_reconciliation"] == ["ok", "ok", "ok"]
    stored = ExtractResult.model_validate(document.extraction)
    assert [i.account.last4 for i in stored.investments] == [a.last4 for a in stmt.accounts]

    for account, synthetic in zip(accounts, stmt.accounts, strict=True):
        assert account is not None
        assert env.activity(account) == _fid_expected(synthetic), synthetic.title
        cash = sorted((t.posted_date, t.amount) for t in env.cash_rows(account))
        assert cash == _fid_cash(synthetic)
        assert _fid_holdings(env, account) == _fid_positions(synthetic)
        assert all(t.source_document_id == document_id for t in env.txns(account))
        snapshots = env.snapshots(account)
        assert [(s.as_of_date, s.balance, s.source) for s in snapshots] == [
            (stmt.end, synthetic.total_value, BalanceSource.STATEMENT)
        ]

    # The same report again is the same statement of every account: each
    # waits as one, and nothing is added.
    before = {a.id: len(env.txns(a)) for a in accounts if a is not None}
    again = env.ingest(fid.fidelity_pdf(stmt), "investment-report-again.pdf")
    assert env.document(again).status is DocumentStatus.NEEDS_REVIEW
    items = env.open_reviews(again)
    assert {(i.payload["reason"], i.payload["account_id"]) for i in items} == {
        ("statement_overlap", str(a.id)) for a in accounts if a is not None
    }
    assert all(i.payload["other_document_id"] == str(document_id) for i in items)
    assert {a.id: len(env.txns(a)) for a in accounts if a is not None} == before
    # "Same statement" for each: skipped, the document is done.
    for item in items:
        _resolve_overlap(env, item, "confirm")
    assert env.document(again).status is DocumentStatus.PROCESSED
    assert {a.id: len(env.txns(a)) for a in accounts if a is not None} == before


def test_a_tampered_account_is_held_and_the_others_are_written(env: Env) -> None:
    stmt = fid.fidelity_statement(seed=92)
    accounts = _fid_accounts(env, stmt)
    data = fid.fidelity_pdf(stmt, tamper="total", tamper_account=1)
    document_id = env.ingest(data, "investment-report.pdf")

    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = [
        r
        for r in env.open_reviews(document_id)
        if r.payload.get("reason") == "investment_reconciliation_failed"
    ]
    roth = accounts[1]
    assert roth is not None
    assert item.payload["account_id"] == str(roth.id)
    assert item.payload["failed"] == ["total_value"]
    assert env.txns(roth) == []
    assert env.snapshots(roth) == []
    assert env.cash_rows(roth) == []
    for index in (0, 2):
        account = accounts[index]
        assert account is not None
        assert env.activity(account) == _fid_expected(stmt.accounts[index])
        assert _fid_holdings(env, account) == _fid_positions(stmt.accounts[index])


def test_a_chosen_account_takes_only_the_unmatched_statement(env: Env) -> None:
    stmt = fid.fidelity_statement(seed=93)
    accounts = _fid_accounts(env, stmt, only=(0, 1))
    document_id = env.ingest(fid.fidelity_pdf(stmt), "investment-report.pdf")
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert "account_not_found" in env.reasons(document_id)
    written = {a.id: len(env.txns(a)) for a in accounts if a is not None}
    assert all(written.values())

    hsa = env.ledger.account(
        env.household_id, type=AccountType.RETIREMENT, currency="USD", name="Health Savings"
    )
    _choose(env, document_id, hsa)
    assert env.document(document_id).status is DocumentStatus.PROCESSED, env.open_reviews(
        document_id
    )
    assert env.activity(hsa) == _fid_expected(stmt.accounts[2])
    assert _fid_holdings(env, hsa) == _fid_positions(stmt.accounts[2])
    # The matched statements stayed on their own accounts (nothing new, nothing moved).
    assert {a.id: len(env.txns(a)) for a in accounts if a is not None} == written


def test_a_chosen_account_never_takes_several_accounts(env: Env) -> None:
    stmt = fid.fidelity_statement(seed=94)
    _fid_accounts(env, stmt, only=(0,))
    document_id = env.ingest(fid.fidelity_pdf(stmt), "investment-report.pdf")
    assert "account_not_found" in env.reasons(document_id)

    chosen = env.ledger.account(
        env.household_id, type=AccountType.RETIREMENT, currency="USD", name="Retirement"
    )
    _choose(env, document_id, chosen)
    # Two statements matched no account: the choice cannot say which one it is for.
    assert env.txns(chosen) == []
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert "account_not_found" in env.reasons(document_id)
