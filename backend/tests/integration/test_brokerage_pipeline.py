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
* A held statement answered "Import as read" imports as printed, without a
  balance snapshot, audited; the answer and "import anyway" carry into each
  other's re-runs and into every later re-run, follow their statement
  wherever it moves and cover no other statement.
"""

from __future__ import annotations

import copy
import json
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel, HouseholdRole
from app.auth.principal import Principal, apply_rls_context
from app.core import clock
from app.core.crypto import Keyring
from app.core.errors import ProblemError
from app.core.settings import Settings
from app.db.models import AuditLog, Setting
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType, BalanceSnapshot, BalanceSource
from app.domain.documents import repo as documents_repo
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
from app.domain.imports import summary as import_summary
from app.domain.imports import wording
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
from app.domain.investments.statement_import import LINES_HELD, OPENING_MEMO
from app.domain.investments.statement_import import RECONCILIATION_FAILED as RECONCILIATION
from app.domain.review import service as review_service
from app.domain.review.schemas import (
    ResolveDuplicate,
    ResolveLowConfidence,
    ResolveUnknownAccount,
)
from app.domain.transactions.models import Transaction
from app.ingestion.intake.upload import receive_upload
from app.ingestion.parsers.vocabulary import Vocabulary
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
            symbol = _key(names[t.security_id]) if t.security_id else None
            rows.append((t.trade_date, str(t.type), symbol, t.quantity, t.amount))
        return sorted(rows, key=lambda r: (r[0], r[1], r[2] or "", r[4]))

    def holdings(self, account: Account) -> list[tuple[str, Decimal]]:
        names = self.securities()
        rows: list[Holding] = self.query(select(Holding).where(Holding.account_id == account.id))
        return sorted((_key(names[h.security_id]), h.quantity) for h in rows)

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


def _key(security: Security) -> str:
    """A security as the fixtures name it: its symbol, else (none printed) its name."""
    return security.symbol or security.name


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
        assert document.parser_version == "placeholder_brokerage/6"
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


@pytest.mark.parametrize("tamper", ["extra_row", "market_value"])
def test_a_security_in_doubt_waits_whole_and_the_rest_is_written(
    env: Env, tamper: synth.Tamper
) -> None:
    """A forged buy, or a holding whose value is not quantity x price.

    Nothing of that security is written (its lines, its position: the
    holding is what its lines add up to), no account value is recorded, and
    everything else of the statement is in the ledger.
    """
    january, _ = synth.brokerage_series(seed=64)
    account = env.brokerage(january)
    document_id = env.ingest(synth.placeholder_pdf(january, tamper=tamper), "statement.pdf")

    document = env.document(document_id)
    assert document.status is DocumentStatus.NEEDS_REVIEW
    item = _held_item(env, document_id)
    assert item.kind is ReviewKind.LOW_CONFIDENCE_FIELD
    assert item.payload["reason"] == LINES_HELD
    assert item.payload["account_id"] == str(account.id)
    assert item.payload["failed"]
    assert item.payload["selectable"] is False
    waiting = _waiting(item)
    assert len(waiting) == 1
    if tamper == "extra_row":
        # The one row that is the whole difference is the line in doubt; the
        # other lines of its security wait with it, doubted by nothing.
        doubted = [line for line in item.payload["lines"] if line["fields"] == ["reconciliation"]]
        (line,) = doubted
        assert (line["type"], line["amount"]) == ("buy", "-777.770000")
        assert line["bbox"] is not None
        others = [line for line in item.payload["lines"] if line is not doubted[0]]
        assert all(line["fields"] == ["held_with_security"] for line in others)
    else:
        (position,) = item.payload["positions"]
        assert "market_value" in position["fields"]
    written = env.activity(account)
    assert written
    # Lines moving the security wait; a dividend it paid is cash and is written.
    assert not any(symbol in waiting and kind in UNIT_KINDS for _, kind, symbol, _, _ in written)
    assert written == [
        row for row in _expected(january) if not (row[2] in waiting and row[1] in UNIT_KINDS)
    ]
    assert env.holdings(account) == [p for p in _positions(january) if p[0] not in waiting]
    assert sorted((t.posted_date, t.amount) for t in env.cash_rows(account)) == _cash(january)
    assert env.snapshots(account) == []


def test_a_wrong_account_value_alone_holds_no_row(env: Env) -> None:
    """Holdings and cash add up, the printed account value is not their sum.

    Every row is vouched for (the cash reconciles, the positions are the
    printed holdings total), so the statement is imported; the account's
    value is not taken from figures that disagree, and a person is told.
    """
    january, _ = synth.brokerage_series(seed=64)
    account = env.brokerage(january)
    document_id = env.ingest(synth.placeholder_pdf(january, tamper="total"), "statement.pdf")

    # Nothing waits on an answer: the document is done, the notice stays in
    # the queue until it is seen.
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    (item,) = env.open_reviews(document_id)
    assert item.payload["reason"] == "investment_figures_differ"
    assert item.payload["failed"] == ["total_value"]
    (difference,) = item.payload["differences"]
    assert difference["check"] == "total_value"
    assert Decimal(difference["difference"]) == Decimal("-250.00")
    assert env.activity(account) == _expected(january)
    assert env.holdings(account) == _positions(january)
    assert sorted((t.posted_date, t.amount) for t in env.cash_rows(account)) == _cash(january)
    assert env.snapshots(account) == []
    (record,) = env.query(select(StatementRecord).where(StatementRecord.account_id == account.id))
    assert (record.document_id, record.outcome) == (document_id, "imported")


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


def _snapshot_days(env: Env, account: Account) -> list[date]:
    return sorted(s.as_of_date for s in env.snapshots(account))


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
    assert document.parser_version == "fidelity_investments_statement/7"
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
        # The value the statement closes with and, as nothing earlier is
        # known of the account, the value it opens with (the day before).
        snapshots = sorted(env.snapshots(account), key=lambda s: s.as_of_date)
        assert [(s.as_of_date, s.balance, s.source) for s in snapshots] == [
            (stmt.start - timedelta(days=1), synthetic.begin_value, BalanceSource.STATEMENT),
            (stmt.end, synthetic.total_value, BalanceSource.STATEMENT),
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


def test_an_account_the_report_lists_and_does_not_print_is_said(env: Env) -> None:
    stmt = fid.fidelity_statement(seed=93)
    accounts = _fid_accounts(env, stmt)
    document_id = env.ingest(fid.fidelity_pdf(stmt, without_pages=1), "investment-report.pdf")

    # The two accounts with pages are imported; the one the index lists and
    # no page prints is named, so it is never taken for imported.
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert item.payload["reason"] == "accounts_not_read"
    assert item.payload["accounts"] == [stmt.accounts[1].last4]
    missing = accounts[1]
    assert missing is not None
    assert env.txns(missing) == []
    assert env.snapshots(missing) == []
    for index in (0, 2):
        account = accounts[index]
        assert account is not None
        assert env.activity(account) == _fid_expected(stmt.accounts[index])
        assert _fid_holdings(env, account) == _fid_positions(stmt.accounts[index])


def test_an_account_value_the_index_prints_differently_is_a_notice(env: Env) -> None:
    stmt = fid.fidelity_statement(seed=94)
    accounts = _fid_accounts(env, stmt)
    data = fid.fidelity_pdf(stmt, tamper="total", tamper_account=0)
    document_id = env.ingest(data, "investment-report.pdf")

    # Every row is vouched for; the account value printed on the account's
    # pages is neither its holdings plus cash nor the index's figure.
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    (item,) = env.open_reviews(document_id)
    assert item.payload["reason"] == "investment_figures_differ"
    assert item.payload["failed"] == ["total_value", "index_value"]
    by_check = {d["check"]: d for d in item.payload["differences"]}
    assert set(by_check) == {"total_value", "index_value"}
    # The index prints the true value; the account's own pages 250.00 more.
    assert Decimal(by_check["index_value"]["printed"]) == stmt.accounts[0].total_value
    assert Decimal(by_check["index_value"]["difference"]) == Decimal("250.00")
    account = accounts[0]
    assert account is not None
    assert env.activity(account) == _fid_expected(stmt.accounts[0])
    assert env.snapshots(account) == []


def test_the_opening_value_is_recorded_once_where_nothing_earlier_is_known(env: Env) -> None:
    october = fid.fidelity_statement(seed=95)
    accounts = _fid_accounts(env, october)
    env.ingest(fid.fidelity_pdf(october), "october.pdf")
    november = fid.fidelity_statement(seed=95, start=date(2026, 11, 1))
    env.ingest(fid.fidelity_pdf(november), "november.pdf")
    for account in accounts:
        assert account is not None
        # October opens the account's history with the value it begins with.
        # November begins where October ended: that day is already known, and
        # what November prints for it is not written over it.
        days = {s.as_of_date: s.balance for s in env.snapshots(account)}
        assert sorted(days) == [october.start - timedelta(days=1), october.end, november.end]
    for account, synthetic in zip(accounts, october.accounts, strict=True):
        assert account is not None
        closing = {s.as_of_date: s.balance for s in env.snapshots(account)}[october.end]
        assert closing == synthetic.total_value


def test_the_summary_a_statement_prints_is_shown_per_account(env: Env) -> None:
    stmt = fid.fidelity_statement(seed=96)
    _fid_accounts(env, stmt)
    document_id = env.ingest(fid.fidelity_pdf(stmt), "investment-report.pdf")
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        shown = import_summary.document_summary(db, env.principal(), document_id)
    assert shown.document_id == document_id
    assert [s.last4 for s in shown.statements] == [a.last4 for a in stmt.accounts]
    for statement, synthetic in zip(shown.statements, stmt.accounts, strict=True):
        assert (statement.period_start, statement.period_end) == (stmt.start, stmt.end)
        assert statement.reconciliation == "ok"
        assert statement.failed == []
        assert statement.total_value is not None
        assert Decimal(statement.total_value) == synthetic.total_value
        assert statement.positions > 0
        # The change in the account's value, as the statement prints it.
        value = next(g for g in statement.groups if g.group == "account_value")
        opening = next(line for line in value.lines if line.role == "opening")
        closing = next(line for line in value.lines if line.role == "closing")
        assert opening.label == "Beginning Account Value"
        assert opening.amount is not None
        assert Decimal(opening.amount) == synthetic.begin_value
        assert closing.amount is not None
        assert Decimal(closing.amount) == synthetic.total_value
        # Each section of activity against its printed total.
        assert statement.sections
        assert all(section.status in ("ok", "unchecked") for section in statement.sections)

    # A member who cannot change the document is refused its figures.
    member = env.factories.user(
        role=HouseholdRole.MEMBER, household=env.owner.household, with_totp=True
    )
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal(member))
        with pytest.raises(ProblemError) as refused:
            import_summary.document_summary(db, env.principal(member), document_id)
    assert refused.value.status in (403, 404)


def test_a_tampered_account_is_held_and_the_others_are_written(env: Env) -> None:
    stmt = fid.fidelity_statement(seed=92)
    accounts = _fid_accounts(env, stmt)
    data = fid.fidelity_pdf(stmt, tamper="market_value", tamper_account=1)
    document_id = env.ingest(data, "investment-report.pdf")

    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    item = _held_item(env, document_id)
    roth = accounts[1]
    assert roth is not None
    assert item.payload["account_id"] == str(roth.id)
    assert item.payload["failed"] == ["positions", "total_value"]
    # The holding that does not add up waits with its lines; no value is recorded.
    waiting = _waiting(item)
    assert len(waiting) == 1
    assert not any(symbol in waiting for symbol, _ in _fid_holdings(env, roth))
    assert not any(
        symbol in waiting and kind in UNIT_KINDS for _, kind, symbol, _, _ in env.activity(roth)
    )
    assert env.snapshots(roth) == []
    for index in (0, 2):
        account = accounts[index]
        assert account is not None
        assert env.activity(account) == _fid_expected(stmt.accounts[index])
        assert _fid_holdings(env, account) == _fid_positions(stmt.accounts[index])


def test_only_the_line_in_doubt_is_held_and_the_rest_is_imported(env: Env) -> None:
    """One deposit is printed 30.00 above what its section's total and the cash say.

    Every other section adds up to its printed total, so only that line
    waits for a person: the holdings, the trades, the other cash rows and the
    account's value (holdings + closing cash is the printed account value)
    are imported. "Import as read" then imports the line as printed, once.
    """
    stmt = fid.fidelity_statement(seed=95)
    accounts = _fid_accounts(env, stmt)
    data = fid.fidelity_pdf(stmt, tamper="misprinted_row", tamper_account=0)
    document_id = env.ingest(data, "investment-report.pdf")

    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    individual = accounts[0]
    assert individual is not None
    assert item.kind is ReviewKind.LOW_CONFIDENCE_FIELD
    assert item.payload["reason"] == LINES_HELD
    assert item.payload["account_id"] == str(individual.id)
    assert item.payload["failed"] == ["cash"]
    assert item.payload["fields"] == ["reconciliation"]
    printed = next(a for a in stmt.accounts[0].activity if a.table == "deposits")
    (line,) = item.payload["lines"]
    assert line["section"] == "Deposits"
    assert line["type"] == "deposit"
    assert Decimal(line["amount"]) == printed.amount + fid.MISPRINT
    assert line["date"] == printed.day.isoformat()
    (section,) = item.payload["sections"]
    assert section["label"] == "Total Deposits"
    assert Decimal(section["printed"]) == printed.amount
    assert Decimal(section["difference"]) == fid.MISPRINT
    assert (item.payload["held"], item.payload["imported"]) == (
        1,
        len(stmt.accounts[0].activity) - 1,
    )

    # Everything but the line is in the ledger, the account's value too.
    assert env.activity(individual) == _fid_expected(stmt.accounts[0])
    assert _fid_holdings(env, individual) == _fid_positions(stmt.accounts[0])
    cash = [a for a in stmt.accounts[0].activity if a.type in ("deposit", "withdrawal")]
    others = sorted((a.day, a.amount) for a in cash if a is not printed)
    assert sorted((t.posted_date, t.amount) for t in env.cash_rows(individual)) == others
    assert _snapshot_days(env, individual) == [stmt.start - timedelta(days=1), stmt.end]
    before = [t.id for t in env.txns(individual)]
    for index in (1, 2):
        account = accounts[index]
        assert account is not None
        assert env.activity(account) == _fid_expected(stmt.accounts[index])
    (imported,) = [
        a for a in _audits(env, "investment.import") if a["account_id"] == str(individual.id)
    ]
    assert imported["lines_held"] == 1

    resolution = _import_as_read(env, item)
    assert resolution["import_as_read"] is True
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.open_reviews(document_id) == []
    # The held line as printed, and nothing else twice.
    assert sorted((t.posted_date, t.amount) for t in env.cash_rows(individual)) == sorted(
        [*others, (printed.day, printed.amount + fid.MISPRINT)]
    )
    assert [t.id for t in env.txns(individual)] == before
    assert _snapshot_days(env, individual) == [stmt.start - timedelta(days=1), stmt.end]


@dataclass
class _Model:
    """A stand-in for the local model: what it answers and what it was sent."""

    words: Vocabulary | None
    asked: list[tuple[list[str], list[str]]] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    columns: list[list[str]] = field(default_factory=list)


def _with_model(monkeypatch: pytest.MonkeyPatch, words: Vocabulary | None) -> _Model:
    """The worker with a local model that answers ``words``."""
    model = _Model(words)

    def ask(
        actions: Sequence[str], headings: Sequence[str], text: str, columns: Sequence[str] = ()
    ) -> Vocabulary | None:
        model.asked.append((list(actions), list(headings)))
        model.columns.append(list(columns))
        model.texts.append(text)
        return model.words

    deps = replace(ingest.default_deps(), ml_vocabulary=ask)

    def patched() -> Any:
        return deps

    patched.cache_clear = lambda: None  # type: ignore[attr-defined]  # the fixture's teardown
    monkeypatch.setattr(ingest, "default_deps", patched)
    return model


def _reworded(stmt: fid.FidStatement, action: str) -> tuple[fid.FidStatement, fid.FidActivity]:
    """``stmt`` with the first account's first buy worded ``action`` (words no parser knows)."""
    account = stmt.accounts[0]
    row = next(a for a in account.activity if a.type == "buy")
    activity = tuple(replace(a, action=action) if a is row else a for a in account.activity)
    return replace(stmt, accounts=(replace(account, activity=activity), *stmt.accounts[1:])), row


def _learned(env: Env) -> list[dict[str, Any]]:
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        return [e.model_dump(mode="json") for e in wording.list_wording(db, env.principal()).items]


@pytest.mark.parametrize("answered", [True, False])
def test_a_kind_the_local_model_suggests_waits_for_a_person(
    env: Env, monkeypatch: pytest.MonkeyPatch, answered: bool
) -> None:
    """A buy worded "Periodic Investment Plan": words in no vocabulary of the parser's.

    The model is asked about that phrase, with the statement to read it in,
    and suggests ``buy``; the statement then adds up, and nothing of it is
    written until a person confirms the suggestion ("Import as read"). With
    no usable answer the document is read exactly as without a model.
    """
    stmt, row = _reworded(fid.fidelity_statement(seed=96), "Periodic Investment Plan")
    accounts = _fid_accounts(env, stmt)
    words = Vocabulary(actions={"periodic investment plan": "buy"}, model="example-model")
    model = _with_model(monkeypatch, words if answered else None)
    document_id = env.ingest(fid.fidelity_pdf(stmt), "investment-report.pdf")

    assert model.asked == [(["periodic investment plan"], [])]
    # The statement goes with the question: the pages wording it come first.
    assert "Periodic Investment Plan" in model.texts[0].split("\n\n")[0]
    individual = accounts[0]
    assert individual is not None
    document = env.document(document_id)
    assert document.status is DocumentStatus.NEEDS_REVIEW
    stored = ExtractResult.model_validate(document.extraction)
    assert stored.parse is not None
    if not answered:
        # As without a model: the row is unread and reported, and its section
        # does not add up, so that section's lines wait.
        assert "ml_vocabulary" not in (document.confidence or {})
        assert stored.parse.unknown_actions == ["periodic investment plan"]
        held = _held_item(env, document_id)
        assert held.payload["fields"] == ["reconciliation"]
        assert _learned(env) == []
        return
    assert (document.confidence or {})["ml_vocabulary"] == words.stored()
    assert stored.parse.unknown_actions == []
    (item,) = env.open_reviews(document_id)
    assert item.payload["reason"] == LINES_HELD
    assert item.payload["fields"] == ["suggested_type"]
    assert item.payload["reconciliation"] == "ok"
    (line,) = [entry for entry in item.payload["lines"] if entry["fields"] == ["suggested_type"]]
    assert line["type"] == "buy"
    assert line["date"] == row.day.isoformat()
    assert Decimal(line["amount"]) == row.amount
    # Nothing of the security it would buy is written before a person confirms;
    # the rest of the statement is.
    assert _waiting(item) == {row.symbol}
    assert not any(
        symbol == row.symbol and kind in UNIT_KINDS
        for _, kind, symbol, _, _ in env.activity(individual)
    )
    assert env.snapshots(individual) == []
    # A suggestion alone teaches nothing: nothing is remembered before a person confirms.
    assert _learned(env) == []

    resolution = _import_as_read(env, item)
    assert resolution["wording_learned"] == 1
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.open_reviews(document_id) == []
    assert env.activity(individual) == _fid_expected(stmt.accounts[0])
    assert _fid_holdings(env, individual) == _fid_positions(stmt.accounts[0])
    assert _snapshot_days(env, individual) == [stmt.start - timedelta(days=1), stmt.end]
    # The answer is kept: the confirmation's re-run did not ask again.
    assert len(model.asked) == 1


def test_confirmed_wording_is_remembered_and_nothing_else_of_the_statement(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """What a confirmation teaches: the phrase and its kind, for that layout.

    The next statement wording a row the same way is read without the model
    and without a question; forgetting the phrase asks again.
    """
    stmt, row = _reworded(fid.fidelity_statement(seed=97), "Periodic Investment Plan")
    _fid_accounts(env, stmt)
    words = Vocabulary(actions={"periodic investment plan": "buy"}, model="example-model")
    model = _with_model(monkeypatch, words)
    first = env.ingest(fid.fidelity_pdf(stmt), "october.pdf")
    (item,) = env.open_reviews(first)
    _import_as_read(env, item)

    (entry,) = _learned(env)
    assert entry == {
        **entry,
        "layout": "fidelity-investments-statement",
        "kind": "action",
        "phrase": "periodic investment plan",
        "meaning": "buy",
        "learned_from_document_id": str(first),
    }
    # The whole memory: no amount, date, security, account or name of the statement.
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        (kept,) = db.execute(
            select(Setting.value).where(Setting.key == wording.SETTING_KEY)
        ).scalars()
    (stored,) = kept["entries"]
    assert set(stored) == {"layout", "kind", "phrase", "meaning", "learned_at", "document_id"}
    account = stmt.accounts[0]
    private = [account.number, account.number[-4:], str(abs(row.amount)), row.symbol or ""]
    assert not any(value and value in json.dumps(kept) for value in private)
    (added,) = _audits(env, "learned_wording.add")
    assert (added["layout"], added["count"]) == ("fidelity-investments-statement", 1)
    assert "periodic" not in json.dumps(added)

    # The next month's statement, worded the same way: the model has nothing to say now.
    model.words = None
    november, later = _reworded(
        fid.fidelity_statement(seed=98, start=date(2026, 11, 1)), "Periodic Investment Plan"
    )
    second = env.ingest(fid.fidelity_pdf(november), "november.pdf")
    assert len(model.asked) == 1  # not asked: the household knows these words
    read = ExtractResult.model_validate(env.document(second).extraction)
    assert read.parse is not None
    assert read.parse.unknown_actions == []
    typed = [a for a in read.investments[0].activity if a.trade_date == later.day]
    assert [(a.type, a.amount, a.low_confidence, a.suggestion) for a in typed if a.type == "buy"][
        :1
    ] == [("buy", later.amount, [], None)]
    # No question about it: nothing of this statement waits for its kind or its cash.
    open_items = env.open_reviews(second)
    assert "investment_reconciliation_failed" not in env.reasons(second)
    assert not any("suggested_type" in (r.payload.get("fields") or []) for r in open_items)

    # Forgotten: the same wording is unread again and the model is asked again.
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        wording.forget_wording(db, env.principal(), entry["id"], request=None)
    assert _learned(env) == []
    december, _ = _reworded(
        fid.fidelity_statement(seed=99, start=date(2026, 12, 1)), "Periodic Investment Plan"
    )
    env.ingest(fid.fidelity_pdf(december), "december.pdf")
    assert model.asked[-1] == (["periodic investment plan"], [])
    assert len(model.asked) == 2
    (forgotten,) = env.query(select(AuditLog).where(AuditLog.action == "learned_wording.delete"))
    assert forgotten.before == {
        "id": entry["id"],
        "layout": "fidelity-investments-statement",
        "kind": "action",
        "meaning": "buy",
    }


UNKNOWN_COLUMNS = ("Posted", "Money In/Out")


def test_a_table_under_unknown_column_headings_is_read_on_a_confirmed_suggestion(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cash table whose header names no date or amount column the parser knows.

    Without a model its entries are reported as unread. With one, the model
    says what the headings name, the parser reads the table, and every row
    of it waits for a person; the confirmation is remembered as wording.
    """
    stmt = fid.fidelity_statement(seed=101)
    accounts = _fid_accounts(env, stmt)
    individual = accounts[0]
    assert individual is not None
    cash = [a for a in stmt.accounts[0].activity if a.table in ("deposits", "withdrawals", "fees")]
    assert cash

    model = _with_model(monkeypatch, None)
    alone = env.ingest(fid.fidelity_pdf(stmt, cash_columns=UNKNOWN_COLUMNS), "alone.pdf")
    stored = ExtractResult.model_validate(env.document(alone).extraction)
    assert stored.parse is not None
    # The headings are what the model is asked about; the entries are unread, and said.
    assert {"posted", "money in out"} <= set(stored.parse.unknown_columns)
    assert {"posted", "money in out"} <= set(model.columns[0])
    assert "lines_unread" in stored.parse.warnings
    assert "lines_skipped" in env.reasons(alone)
    written = {(t.posted_date, t.amount) for t in env.cash_rows(individual)}
    assert not any((a.day, a.amount) in written for a in cash)


def test_confirmed_column_headings_are_remembered(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    stmt = fid.fidelity_statement(seed=102)
    accounts = _fid_accounts(env, stmt)
    individual = accounts[0]
    assert individual is not None
    cash = sorted(
        (a.day, a.amount)
        for a in stmt.accounts[0].activity
        if a.table in ("deposits", "withdrawals", "fees")
    )
    words = Vocabulary(columns={"posted": "date", "money in out": "amount"}, model="example-model")
    model = _with_model(monkeypatch, words)
    first = env.ingest(fid.fidelity_pdf(stmt, cash_columns=UNKNOWN_COLUMNS), "october.pdf")
    stored = ExtractResult.model_validate(env.document(first).extraction)
    assert stored.parse is not None
    assert stored.parse.unknown_columns == [
        c for c in stored.parse.unknown_columns if c not in ("posted", "money in out")
    ]
    assert "lines_unread" not in stored.parse.warnings
    # Every row of the tables read through the suggestion waits for a person.
    items = [i for i in env.open_reviews(first) if i.payload["reason"] == LINES_HELD]
    assert items
    held = sorted(
        (date.fromisoformat(line["date"]), Decimal(line["amount"]))
        for item in items
        for line in item.payload["lines"]
        if "suggested_type" in line["fields"]
    )
    mine = [i for i in items if i.payload["account_id"] == str(individual.id)]
    assert mine
    assert set(cash) <= set(held)
    written = {(t.posted_date, t.amount) for t in env.cash_rows(individual)}
    assert not any(row in written for row in cash)

    for item in items:
        _import_as_read(env, item)
    written = {(t.posted_date, t.amount) for t in env.cash_rows(individual)}
    assert set(cash) <= written
    learned = {(e["kind"], e["phrase"], e["meaning"]) for e in _learned(env)}
    assert learned == {("column", "posted", "date"), ("column", "money in out", "amount")}

    # The next statement with the same headings is read without the model and without a question.
    model.words = None
    asked = len(model.asked)
    november = fid.fidelity_statement(seed=103, start=date(2026, 11, 1))
    second = env.ingest(fid.fidelity_pdf(november, cash_columns=UNKNOWN_COLUMNS), "november.pdf")
    read = ExtractResult.model_validate(env.document(second).extraction)
    assert read.parse is not None
    assert not {"posted", "money in out"} & set(read.parse.unknown_columns)
    assert "lines_unread" not in read.parse.warnings
    assert not any(
        "suggested_type" in (r.payload.get("fields") or []) for r in env.open_reviews(second)
    )
    assert all(not {"posted", "money in out"} & set(columns) for columns in model.columns[asked:])


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


# ---------------------------------------------------------------------------
# Import as read: a held statement imported as printed, on a person's word
# ---------------------------------------------------------------------------


def _import_as_read(env: Env, item: ReviewItem, lines: list[int] | None = None) -> dict[str, Any]:
    """Answer a held statement's item "Import as read" as the owner; run the job.

    ``lines``: only these lines of the item (the others it lists are left out).
    """
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        out = review_service.resolve(
            db,
            env.principal(),
            item.id,
            ResolveLowConfidence(kind="low_confidence_field", import_as_read=True, lines=lines),
            request=None,
        )
        assert out.resolution is not None
        resolution = dict(out.resolution)
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)
    return resolution


def _audits(env: Env, action: str) -> list[dict[str, Any]]:
    rows: list[AuditLog] = env.query(
        select(AuditLog)
        .where(AuditLog.household_id == env.household_id, AuditLog.action == action)
        .order_by(AuditLog.seq)
    )
    return [dict(r.after or {}) for r in rows]


def _held_item(env: Env, document_id: uuid.UUID) -> ReviewItem:
    """The item holding a statement's rows back: the statement whole, or the
    rows in doubt (a security waits whole, the rest is written)."""
    (item,) = [
        r
        for r in env.open_reviews(document_id)
        if r.payload.get("reason") in (RECONCILIATION, LINES_HELD)
    ]
    return item


#: Activity that moves units of a security (it waits with its security).
UNIT_KINDS = ("buy", "sell", "reinvest", "transfer_in", "transfer_out")


def _waiting(item: ReviewItem) -> set[str]:
    """The symbols of the securities an item holds back whole."""
    return {str(p["symbol"]) for p in item.payload.get("positions") or []}


@pytest.mark.parametrize(
    ("tamper", "check"),
    [("extra_row", "cash"), ("market_value", "positions")],
)
def test_held_statement_imported_as_read(env: Env, tamper: synth.Tamper, check: str) -> None:
    january, _ = synth.brokerage_series(seed=71)
    account = env.brokerage(january)
    document_id = env.ingest(synth.placeholder_pdf(january, tamper=tamper), "statement.pdf")
    item = _held_item(env, document_id)
    assert item.payload["statement"] == 0
    assert item.payload["currency"] == "USD"
    # What did not add up: per failed check, printed vs computed and the difference.
    differences = {d["check"]: d for d in item.payload["differences"]}
    assert check in differences
    assert set(differences) == set(item.payload["failed"])
    for found in differences.values():
        assert Decimal(found["difference"]) == Decimal(found["computed"]) - Decimal(
            found["printed"]
        )
        assert Decimal(found["difference"]) != 0
    if tamper == "extra_row":
        assert Decimal(differences["cash"]["difference"]) == Decimal("-777.77")
    if tamper == "market_value":
        assert differences["positions"]["count"] == 1
    assert env.snapshots(account) == []

    resolution = _import_as_read(env, item)
    assert resolution["import_as_read"] is True
    assert resolution["account_id"] == str(account.id)
    assert resolution["statement"] == 0
    assert resolution["failed"] == item.payload["failed"]
    assert resolution["snapshot"] is False

    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.open_reviews(document_id) == []
    # Imported as printed (the forged row too: it is on the page) ...
    assert env.activity(account) == _expected(january, tamper=tamper)
    assert env.holdings(account) == _positions(january)
    assert sorted((t.posted_date, t.amount) for t in env.cash_rows(account)) == _cash(january)
    # ... but the account's value is never taken from figures that do not add up.
    assert env.snapshots(account) == []
    (record,) = env.query(select(StatementRecord).where(StatementRecord.account_id == account.id))
    assert (record.document_id, record.outcome) == (document_id, "imported")
    # The acceptance is in the audit log: the answer (for this statement), the
    # re-run it queued (a plain one: the answer travels with the statement), the import.
    (resolved,) = _audits(env, "review.resolve")
    assert resolved["resolution"]["import_as_read"] is True
    assert resolved["resolution"]["statement_key"] == item.payload["statement_key"]
    (requested,) = _audits(env, "document.import_requested")
    assert requested["assignments"] == []
    assert "accept_as_read" not in requested
    partly, imported = _audits(env, "investment.import")
    assert partly["lines_held"] >= 1
    assert partly["positions_held"] == 1
    assert imported["accepted_as_read"] is True
    assert imported["failed"] == item.payload["failed"]
    assert imported["snapshot"] is False


def test_one_account_of_a_report_imported_as_read(env: Env) -> None:
    stmt = fid.fidelity_statement(seed=94)
    accounts = _fid_accounts(env, stmt)
    document_id = env.ingest(
        fid.fidelity_pdf(stmt, tamper="market_value", tamper_account=1), "investment-report.pdf"
    )
    item = _held_item(env, document_id)
    roth = accounts[1]
    assert roth is not None
    assert item.payload["statement"] == 1
    assert [d["check"] for d in item.payload["differences"]] == ["positions", "total_value"]
    others = [a for a in accounts if a is not None and a.id != roth.id]
    written = {a.id: [t.id for t in env.txns(a)] for a in others}
    assert all(written.values())
    snapshots = {a.id: len(env.snapshots(a)) for a in others}
    assert set(snapshots.values()) == {2}  # opening and closing value

    _import_as_read(env, item)
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.open_reviews(document_id) == []
    assert env.activity(roth) == _fid_expected(stmt.accounts[1])
    assert _fid_holdings(env, roth) == _fid_positions(stmt.accounts[1])
    assert env.snapshots(roth) == []
    # The other accounts were imported already: nothing added, nothing doubled.
    assert {a.id: [t.id for t in env.txns(a)] for a in others} == written
    assert {a.id: len(env.snapshots(a)) for a in others} == snapshots


def test_accepted_and_overlapping_answers_carry_into_each_other(env: Env) -> None:
    """ "Import anyway" then "Import as read" (one statement, both holds): no loop."""
    january, _ = synth.brokerage_series(seed=72)
    account = env.brokerage(january)
    env.ingest(synth.ofx_investment(january), "statement.ofx")
    before = env.activity(account)
    snapshots = [(s.as_of_date, s.balance) for s in env.snapshots(account)]
    # The same month again as a PDF that does not add up: the overlap holds it first.
    document_id = env.ingest(synth.placeholder_pdf(january, tamper="market_value"), "statement.pdf")
    (overlap,) = env.open_reviews(document_id)
    assert overlap.payload["reason"] == "statement_overlap"
    _resolve_overlap(env, overlap, "reject")
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)
    item = _held_item(env, document_id)
    # Both items are about the one statement.
    assert item.payload["statement_key"] == overlap.payload["statement_key"]

    resolution = _import_as_read(env, item)
    assert (resolution["target"], resolution["statement"]) == ("investment", 0)
    assert resolution["statement_key"] == item.payload["statement_key"]
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.open_reviews(document_id) == []
    # The rows were already recorded: nothing doubled; the OFX's snapshot stays alone.
    assert env.activity(account) == before
    assert [(s.as_of_date, s.balance) for s in env.snapshots(account)] == snapshots


def _rerun(env: Env, document_id: uuid.UUID, data: ApplyMapping | None = None) -> None:
    """A plain ``apply-mapping`` re-run, as the API or the Accounts dialog starts one."""
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        request_apply(db, env.principal(), document_id, data or ApplyMapping(), request=None)
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)


def _edit_extraction(env: Env, document_id: uuid.UUID, change: Callable[[Any], None]) -> None:
    """Change the stored extraction as a reprocess that reads it differently would."""
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        document = db.get(Document, document_id)
        assert document is not None
        assert document.extraction is not None
        extraction = copy.deepcopy(document.extraction)
        change(extraction["investments"])
        document.extraction = extraction


def _investment_imports(env: Env) -> list[tuple[str, bool, int]]:
    return [
        (i["account_id"], i.get("accepted_as_read") is True, i["positions"])
        for i in _audits(env, "investment.import")
    ]


def test_an_accepted_statement_stays_imported_on_any_rerun(env: Env) -> None:
    """Re-runs the answer did not start (another item's answer, a plain re-run) keep it."""
    stmt = fid.fidelity_statement(seed=94)
    accounts = _fid_accounts(env, stmt, only=(0, 1))
    roth = accounts[1]
    assert roth is not None
    document_id = env.ingest(
        fid.fidelity_pdf(stmt, tamper="market_value", tamper_account=1), "investment-report.pdf"
    )
    _import_as_read(env, _held_item(env, document_id))
    assert env.reasons(document_id) == {"account_not_found"}
    written = [t.id for t in env.txns(roth)]
    assert written
    # The third statement's account is answered the usual way: its re-run
    # carries the earlier answer, so the Roth statement is not held again.
    (unknown,) = env.open_reviews(document_id)
    assert unknown.payload["statement"] == 2
    third = env.ledger.account(
        env.household_id, type=AccountType.RETIREMENT, currency="USD", name="Third"
    )
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        review_service.resolve(
            db,
            env.principal(),
            unknown.id,
            ResolveUnknownAccount(kind="unknown_account", account_id=third.id),
            request=None,
        )
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.activity(third) == _fid_expected(stmt.accounts[2])
    # A plain re-run (the API, the Accounts dialog) changes nothing either.
    _rerun(env, document_id, ApplyMapping(account_id=third.id, statement=2))
    assert env.open_reviews(document_id) == []
    assert [t.id for t in env.txns(roth)] == written
    assert env.snapshots(roth) == []


def test_an_answer_follows_its_statement_not_its_place(env: Env) -> None:
    """A reprocess that reorders or re-reads the statements never moves an answer."""
    stmt = fid.fidelity_statement(seed=94)
    accounts = _fid_accounts(env, stmt, only=(0, 1))
    roth = accounts[1]
    assert roth is not None
    document_id = env.ingest(
        fid.fidelity_pdf(stmt, tamper="market_value", tamper_account=1), "investment-report.pdf"
    )
    _import_as_read(env, _held_item(env, document_id))
    written = [t.id for t in env.txns(roth)]

    # The statements read in another order (the Roth's now third), open items
    # dismissed as a reprocess does: the answer follows the Roth statement and
    # the statement now at its old place is not put on the Roth account.
    def swap(investments: Any) -> None:
        investments[1], investments[2] = investments[2], investments[1]

    _edit_extraction(env, document_id, swap)
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        for open_item in db.execute(
            select(ReviewItem).where(
                ReviewItem.document_id == document_id, ReviewItem.state == ReviewItemState.OPEN
            )
        ).scalars():
            open_item.state = ReviewItemState.DISMISSED
            open_item.resolved_at = clock.utcnow()
    _rerun(env, document_id)
    (unknown,) = env.open_reviews(document_id)
    assert (unknown.payload["reason"], unknown.payload["statement"]) == ("account_not_found", 1)
    assert [t.id for t in env.txns(roth)] == written
    assert {a for a, _, _ in _investment_imports(env)} == {
        str(a.id) for a in accounts if a is not None
    }

    # The Roth statement read differently: no answer covers it, so it is held again.
    def reread(investments: Any) -> None:
        investments[2]["reconciliation"]["reported_total_value"] = "1.00"

    _edit_extraction(env, document_id, reread)
    _rerun(env, document_id)
    item = _held_item(env, document_id)
    assert item.payload["statement"] == 2
    assert item.payload["account_id"] == str(roth.id)
    imports = len(_audits(env, "investment.import"))
    _import_as_read(env, item)
    assert env.reasons(document_id) == {"account_not_found"}
    # Only the re-read Roth statement was imported on that answer, into the Roth.
    assert _investment_imports(env)[imports:] == [
        (str(accounts[0].id), False, len(stmt.accounts[0].positions)),  # type: ignore[union-attr]
        (str(roth.id), True, len(stmt.accounts[1].positions)),
    ]
    assert [t.id for t in env.txns(roth)] == written


def test_an_answer_covers_only_its_statement(env: Env) -> None:
    """Another statement of the same account that does not add up stays held."""
    stmt = fid.fidelity_statement(seed=94)
    accounts = _fid_accounts(env, stmt, only=(0, 1))
    roth = accounts[1]
    assert roth is not None
    document_id = env.ingest(
        fid.fidelity_pdf(stmt, tamper="market_value", tamper_account=1), "investment-report.pdf"
    )

    # The third statement made a second, failing statement of the Roth account.
    def second_roth(investments: Any) -> None:
        investments[2]["account"] = copy.deepcopy(investments[1]["account"])
        investments[2]["reconciliation"].update(
            status="mismatch", failed=["total_value"], reported_total_value="1.00"
        )

    _edit_extraction(env, document_id, second_roth)
    _rerun(env, document_id)
    held = sorted(
        (
            r
            for r in env.open_reviews(document_id)
            if r.payload.get("reason") in (RECONCILIATION, LINES_HELD)
        ),
        key=lambda r: r.payload["statement"],
    )
    assert [(r.payload["statement"], r.payload["account_id"]) for r in held] == [
        (1, str(roth.id)),
        (2, str(roth.id)),
    ]
    assert held[0].payload["statement_key"] != held[1].payload["statement_key"]
    imports = len(_audits(env, "investment.import"))

    _import_as_read(env, held[0])
    accepted = [entry for entry in _investment_imports(env)[imports:] if entry[1]]
    assert accepted == [(str(roth.id), True, len(stmt.accounts[1].positions))]
    (still,) = [
        r
        for r in env.open_reviews(document_id)
        if r.payload.get("reason") in (RECONCILIATION, LINES_HELD)
    ]
    assert still.payload["statement"] == 2
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW

    # Its own answer imports it.
    imports = len(_audits(env, "investment.import"))
    _import_as_read(env, still)
    accepted = [entry for entry in _investment_imports(env)[imports:] if entry[1]]
    assert sorted(accepted) == sorted(
        [
            (str(roth.id), True, len(stmt.accounts[1].positions)),
            (str(roth.id), True, len(stmt.accounts[2].positions)),
        ]
    )
    # Nothing is held for its figures any more (the two statements' holdings
    # on one day of one account disagree, which the lot engine reports).
    assert env.reasons(document_id) == {"position_mismatch"}


def test_an_item_without_a_statement_key_is_stale(env: Env) -> None:
    """An item raised before statements had keys names no statement to import."""
    january, _ = synth.brokerage_series(seed=75)
    account = env.brokerage(january)
    document_id = env.ingest(synth.placeholder_pdf(january, tamper="market_value"), "statement.pdf")
    before = [t.id for t in env.txns(account)]
    item = _held_item(env, document_id)
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        row = db.get(ReviewItem, item.id)
        assert row is not None
        row.payload = {k: v for k, v in row.payload.items() if k != "statement_key"}
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        with pytest.raises(ProblemError) as refused:
            review_service.resolve(
                db,
                env.principal(),
                item.id,
                ResolveLowConfidence(kind="low_confidence_field", import_as_read=True),
                request=None,
            )
    assert (refused.value.status, refused.value.slug) == (409, "review-item-stale")
    assert [t.id for t in env.txns(account)] == before  # nothing more was written


def test_import_as_read_is_only_for_statements_held_by_their_figures(env: Env) -> None:
    january, _ = synth.brokerage_series(seed=73)
    account = env.brokerage(january)
    document_id = env.ingest(synth.placeholder_pdf(january, tamper="market_value"), "statement.pdf")
    before = [t.id for t in env.txns(account)]
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        guessed = documents_repo.add_review_item(
            db,
            env.household_id,
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "investment_low_confidence",
                "fields": ["reconciliation", "trade_date"],
                "account_id": str(account.id),
                "statement": 0,
            },
            document_id=document_id,
        )
        guessed_id = guessed.id
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        with pytest.raises(ProblemError) as refused:
            review_service.resolve(
                db,
                env.principal(),
                guessed_id,
                ResolveLowConfidence(kind="low_confidence_field", import_as_read=True),
                request=None,
            )
    assert refused.value.status == 422
    assert [t.id for t in env.txns(account)] == before  # nothing more was written


def test_import_as_read_needs_edit_on_the_account(env: Env) -> None:
    january, _ = synth.brokerage_series(seed=74)
    account = env.brokerage(january)
    document_id = env.ingest(synth.placeholder_pdf(january, tamper="market_value"), "statement.pdf")
    before = [t.id for t in env.txns(account)]
    item = _held_item(env, document_id)
    member = env.factories.user(
        role=HouseholdRole.MEMBER, household=env.owner.household, with_totp=True
    )
    env.ledger.grant(env.household_id, member.user.id, account, GrantLevel.VIEW)
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal(member))
        with pytest.raises(ProblemError) as refused:
            review_service.resolve(
                db,
                env.principal(member),
                item.id,
                ResolveLowConfidence(kind="low_confidence_field", import_as_read=True),
                request=None,
            )
    assert refused.value.status in (403, 404)
    assert [r.id for r in env.open_reviews(document_id)] == [item.id]
    assert [t.id for t in env.txns(account)] == before  # nothing more was written


# ---------------------------------------------------------------------------
# Lines answered one by one; what a statement does not say about a holding
# ---------------------------------------------------------------------------


def _unimported(env: Env, stmt: fid.FidStatement, change: Callable[[Any], None]) -> uuid.UUID:
    """The report ingested before its accounts exist, read as ``change`` says, then imported."""
    document_id = env.ingest(fid.fidelity_pdf(stmt), "investment-report.pdf")
    _edit_extraction(env, document_id, change)
    _fid_accounts(env, stmt)
    _rerun(env, document_id)
    return document_id


def test_cash_lines_in_doubt_are_answered_one_by_one(env: Env) -> None:
    """Two cash lines wait; the person imports one and leaves the other out for good."""
    stmt = fid.fidelity_statement(seed=101)
    account = stmt.accounts[0]
    chosen: list[int] = []

    def doubt_two_cash_lines(investments: Any) -> None:
        rows = investments[0]["activity"]
        cash = [i for i, r in enumerate(rows) if r["type"] in ("dividend", "deposit", "withdrawal")]
        chosen.extend(cash[:2])
        for i in chosen:
            rows[i]["low_confidence"] = ["reconciliation"]
        investments[0]["reconciliation"].update(
            status="mismatch", failed=["cash"], doubtful_activity=chosen, doubtful_positions=[]
        )

    document_id = _unimported(env, stmt, doubt_two_cash_lines)
    individual = env.query(select(Account).where(Account.number_last4 == account.last4))[0]
    (item,) = [r for r in env.open_reviews(document_id) if r.payload.get("reason") == LINES_HELD]
    assert item.payload["selectable"] is True
    assert [line["index"] for line in item.payload["lines"]] == chosen
    assert all(line["bbox"] is not None and line["page"] for line in item.payload["lines"])
    before = len(env.txns(individual)) + len(env.cash_rows(individual))

    # A line the item does not list, or none at all, is refused.
    for bad in ([], [chosen[0], 999]):
        with env.factories.transaction() as db:
            apply_rls_context(db, env.principal())
            with pytest.raises(ProblemError) as refused:
                review_service.resolve(
                    db,
                    env.principal(),
                    item.id,
                    ResolveLowConfidence(
                        kind="low_confidence_field", import_as_read=True, lines=bad
                    ),
                    request=None,
                )
            assert refused.value.status == 422

    resolution = _import_as_read(env, item, lines=[chosen[0]])
    assert resolution["lines"] == [chosen[0]]
    # One more row in the ledger; the other is left out and nobody is asked again.
    assert len(env.txns(individual)) + len(env.cash_rows(individual)) == before + 1
    assert LINES_HELD not in env.reasons(document_id)
    imported = [a for a in _audits(env, "investment.import") if a.get("lines_left_out")]
    assert [a["lines_left_out"] for a in imported] == [1]
    # Any later import of the document keeps the answer: still one, still no question.
    _rerun(env, document_id)
    assert len(env.txns(individual)) + len(env.cash_rows(individual)) == before + 1
    assert LINES_HELD not in env.reasons(document_id)


def test_a_security_that_waits_cannot_be_answered_line_by_line(env: Env) -> None:
    january, _ = synth.brokerage_series(seed=102)
    env.brokerage(january)
    document_id = env.ingest(synth.placeholder_pdf(january, tamper="extra_row"), "statement.pdf")
    item = _held_item(env, document_id)
    assert item.payload["selectable"] is False
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        with pytest.raises(ProblemError) as refused:
            review_service.resolve(
                db,
                env.principal(),
                item.id,
                ResolveLowConfidence(
                    kind="low_confidence_field",
                    import_as_read=True,
                    lines=[item.payload["lines"][0]["index"]],
                ),
                request=None,
            )
    assert refused.value.status == 422


def test_an_unknown_cost_basis_and_a_short_position_are_said(env: Env) -> None:
    """What a statement leaves open about a holding is asked, not assumed.

    A holding printed ``unknown`` beside others that print their cost is
    opened at its price (no gain shown) and a person is asked for its cost;
    a short position is in the statement's value but not among the holdings.
    """
    stmt = fid.fidelity_statement(seed=103)
    symbols: dict[str, str] = {}

    def unknown_and_short(investments: Any) -> None:
        positions = investments[0]["positions"]
        assert len(positions) >= 3
        assert all(p["cost_basis"] is not None for p in positions)
        positions[0]["cost_basis"] = None
        symbols["unknown"] = positions[0]["security"]["symbol"]
        short = positions[1]
        symbols["short"] = short["security"]["symbol"]
        # A short position: as many units owed, their value owed too. The
        # statement's totals are edited with it, so its figures still add up.
        check = investments[0]["reconciliation"]
        twice = 2 * Decimal(short["market_value"])
        short["quantity"] = str(-Decimal(short["quantity"]))
        short["market_value"] = str(-Decimal(short["market_value"]))
        for holder, name in (
            (investments[0]["positions_value"], "amount"),
            (investments[0]["total_value"], "amount"),
            (check, "positions_total"),
            (check, "reported_positions_total"),
            (check, "total_value"),
            (check, "reported_total_value"),
        ):
            holder[name] = str(Decimal(holder[name]) - twice)

    document_id = _unimported(env, stmt, unknown_and_short)
    individual = env.query(select(Account).where(Account.number_last4 == stmt.accounts[0].last4))[0]
    by_reason = {str(r.payload.get("reason")): r for r in env.open_reviews(document_id)}
    assert {"investment_opening_basis_unknown", "investment_short_position"} <= set(by_reason)

    names = {s.id: _key(s) for s in env.securities().values()}
    (asked,) = by_reason["investment_opening_basis_unknown"].payload["securities"]
    assert names[uuid.UUID(asked["security_id"])] == symbols["unknown"]
    opening = next(t for t in env.txns(individual) if str(t.id) == asked["transaction_id"])
    assert opening.memo == OPENING_MEMO

    (short,) = by_reason["investment_short_position"].payload["positions"]
    assert short["symbol"] == symbols["short"]
    assert Decimal(short["quantity"]) < 0
    # Not held short (the lot engine holds no short position) ...
    assert all(quantity > 0 for _, quantity in _fid_holdings(env, individual))
    # ... and in the account's value, which is the statement's.
    (snapshot,) = env.snapshots(individual)
    stored = ExtractResult.model_validate(env.document(document_id).extraction)
    assert stored.investments[0].total_value is not None
    assert snapshot.balance == stored.investments[0].total_value.amount
