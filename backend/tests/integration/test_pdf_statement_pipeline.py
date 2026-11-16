# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF statements and payslips end to end through the worker (INGESTION.md, SECURITY.md C12).

upload -> worker ``ingest_document`` -> real sandbox (PyMuPDF pages) ->
classifier (shipped fingerprints) -> document parser -> reconciliation ->
structured importer -> ledger, with the production pipeline dependencies
(``app.workers.handlers.ingest.default_deps``) on a temporary blob store.
PDFs come from ``tests/fixtures/synth_pdf_statements.py`` (fictional
institutions, invented numbers).

* A statement that reconciles is committed with provenance (source
  document, import hash, confidence) and its closing balance snapshot.
* A statement that does not reconcile is held: every row pending, a
  ``low_confidence_field`` item with the discrepancy, no snapshot.
* Rows read with low confidence are pending with an item naming the fields.
* A payslip is kept on the document; one that does not add up waits in review.
* The stored extraction carries the parser output, so choosing an account
  later re-imports without a new upload or extraction.
* A statement whose rows print no year and that prints no period keeps its
  rows, each pending on its date, with one question naming the assumed year
  (``tests/fixtures/synth_bank_layouts.py``).
* A statement of unsigned amounts under section titles, and one whose header
  takes two lines, import clean; so do checks printed three across the
  page, every one of them a row
  (``synth_bank_layouts``, ``synth_statement_headers``).
* Round two (``tests/fixtures/synth_sections.py``, ``synth_bank_layouts2.py``):
  a statement with no header row anywhere and one whose rows carry ``CR`` /
  ``DB`` in a column of their own import clean; two accounts printed under
  one masked number are two statements, each imported into the account
  chosen for it; a lender's bill writes no bank transaction.
* A card "Statement of Account" with only a statement date
  (``tests/fixtures/synth_card_statement.py``) imports into its credit card
  account with no review item.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.models import AuditLog
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType, BalanceSnapshot, BalanceSource
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentKind,
    DocumentStatus,
    IngestJob,
    IngestStage,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports import proposals
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE, ApplyMapping, Assignment
from app.domain.imports.service import request_apply
from app.domain.transactions.models import ReviewState, Transaction
from app.ingestion.intake.upload import receive_upload
from app.ingestion.match.accounts import match_shared_number
from app.ingestion.sandbox.protocol import ExtractResult
from app.workers.handlers import ingest
from tests.fixtures import synth_bank_layouts as layouts
from tests.fixtures import synth_bank_layouts2 as layouts2
from tests.fixtures import synth_card_statement as card
from tests.fixtures import synth_pdf_statements as synth
from tests.fixtures import synth_sections as sections
from tests.fixtures import synth_statement_headers as headers
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.imports import run_jobs
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.synth_statements import SynthStatement

pytestmark = pytest.mark.integration

MAX = 10 * 1024 * 1024


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

    def principal(self) -> Principal:
        return Principal(
            user=self.owner.user,
            household_id=self.owner.household_id,
            role=self.owner.role,
            auth_method="session",
        )

    def ingest(self, data: bytes) -> uuid.UUID:
        with self.factories.transaction() as db:
            apply_rls_context(db, self.principal())
            received = receive_upload(
                db,
                self.principal(),
                store=self.store,
                keyring=self.keyring,
                chunks=iter([data]),
                max_bytes=MAX,
                filename="statement.pdf",
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

    def transactions(self, account: Account) -> list[Transaction]:
        return self.query(
            select(Transaction)
            .where(Transaction.account_id == account.id)
            .order_by(Transaction.posted_date, Transaction.id)
        )

    def reviews(self, document_id: uuid.UUID) -> list[ReviewItem]:
        return self.query(
            select(ReviewItem)
            .where(ReviewItem.document_id == document_id)
            .order_by(ReviewItem.created_at, ReviewItem.id)
        )

    def open_reviews(self, document_id: uuid.UUID) -> list[ReviewItem]:
        return [r for r in self.reviews(document_id) if r.state is ReviewItemState.OPEN]

    def snapshots(self, account: Account) -> list[BalanceSnapshot]:
        return self.query(select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id))

    def account(self, stmt: SynthStatement, *, with_institution: bool = True) -> Account:
        kwargs: dict[str, Any] = {}
        if with_institution:
            institution = self.ledger.institution(self.household_id, name=stmt.institution.name)
            kwargs["institution_id"] = institution.id
        kind = AccountType.CREDIT_CARD if stmt.account_type == "creditcard" else "checking"
        return self.ledger.account(
            self.household_id,
            number_last4=stmt.last4,
            type=kind,
            currency=stmt.currency,
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
    assert deps.confidence_threshold == local.ingest_confidence_threshold == 0.85
    assert isinstance(deps.store, LocalFSBlobStore)
    yield Env(
        factories=factories,
        ledger=ledger,
        store=deps.store,
        keyring=deps.keyring,
        owner=factories.user(role="owner", with_totp=True),
    )
    ingest.default_deps.cache_clear()


def _rows(rows: list[Transaction]) -> list[tuple[date, Decimal, str | None]]:
    return sorted((t.posted_date, t.amount, t.raw_payee) for t in rows)


def _expected(stmt: SynthStatement, **kwargs: Any) -> list[tuple[date, Decimal, str | None]]:
    return [(d, a.quantize(Decimal("0.000001")), p) for d, a, p in synth.expected(stmt, **kwargs)]


PENDING = (synth.PendingItem(date(2026, 1, 31), Decimal("-9.99"), "Pending Cafe"),)
LAYOUTS: dict[str, tuple[Callable[[], SynthStatement], Callable[[SynthStatement], bytes]]] = {
    "sample_national": (
        synth.sample_national_statement,
        lambda s: synth.sample_national_pdf(s, rows_per_page=10, pending=PENDING),
    ),
    "example_fcu": (synth.example_fcu_statement, synth.example_fcu_pdf),
    "demo_card": (synth.demo_card_statement, lambda s: synth.demo_card_pdf(s, pending=PENDING)),
    "mock_savings": (synth.mock_savings_statement, synth.mock_savings_pdf),
}


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_pdf_statement_is_classified_parsed_reconciled_and_committed(env: Env, layout: str) -> None:
    make, render = LAYOUTS[layout]
    stmt = make()
    account = env.account(stmt)
    document_id = env.ingest(render(stmt))

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert document.kind is DocumentKind.STATEMENT
    assert document.account_id == account.id
    assert document.institution_id == account.institution_id
    assert (document.period_start, document.period_end) == (stmt.start, stmt.end)
    assert document.parser_version is not None
    assert document.parser_version.endswith("/7")
    assert document.confidence is not None
    assert document.confidence["parse"]["reconciliation"] == ["ok"]

    rows = env.transactions(account)
    assert _rows(rows) == _expected(stmt)
    for row in rows:
        # Provenance on every row (C12).
        assert row.source_document_id == document_id
        assert row.import_hash
        assert row.review_state is ReviewState.NONE
        assert row.confidence == Decimal("1")
    assert "Pending Cafe" not in {r.raw_payee for r in rows}

    snapshots = env.snapshots(account)
    assert len(snapshots) == 1
    assert snapshots[0].source is BalanceSource.STATEMENT
    assert snapshots[0].source_document_id == document_id
    assert snapshots[0].balance == stmt.closing_balance

    # The parser output stays with the pages it was read from.
    stored = ExtractResult.model_validate(document.extraction)
    assert stored.parse is not None
    assert stored.parse.parser is not None
    assert stored.parse.parser.institution == stmt.institution.key
    assert len(stored.statements) == 1
    assert stored.statements[0].source_format == "pdf"
    assert stored.pages

    parse_logs = env.query(
        select(IngestJob).where(
            IngestJob.document_id == document_id, IngestJob.stage == IngestStage.PARSE
        )
    )
    assert parse_logs[-1].log["parser"] == stored.parse.parser.name
    assert parse_logs[-1].log["imported"] == len(rows)
    audits = env.query(
        select(AuditLog).where(
            AuditLog.action == "transaction.import", AuditLog.target_id == str(document_id)
        )
    )
    assert audits

    # The same statement rendered again (other bytes) is the same statement:
    # held for review (statement_overlap), nothing imported twice.
    again = env.ingest(render(stmt))
    assert env.document(again).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(again)
    assert (item.kind, item.payload["reason"]) == (
        ReviewKind.POSSIBLE_DUPLICATE,
        "statement_overlap",
    )
    assert item.payload["other_document_id"] == str(document_id)
    assert item.payload["account_id"] == str(account.id)
    assert len(env.transactions(account)) == len(rows)
    log = env.query(
        select(IngestJob).where(
            IngestJob.document_id == again, IngestJob.stage == IngestStage.PARSE
        )
    )[-1].log
    assert (log["imported"], log["statement_overlaps"]) == (0, 1)


def test_card_statement_of_account_imports_into_its_card_account(env: Env) -> None:
    stmt = card.card_statement()
    account = env.ledger.account(
        env.household_id,
        number_last4=stmt.last4,
        type=AccountType.CREDIT_CARD,
        currency="CAD",
        name="Corporate card",
    )
    document_id = env.ingest(card.card_pdf(stmt))

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert env.reviews(document_id) == []
    assert document.kind is DocumentKind.STATEMENT
    assert document.account_id == account.id
    assert (document.period_start, document.period_end) == (
        stmt.period_start,
        stmt.statement_date,
    )
    assert document.parser_version == "card_statement_generic/9"
    assert document.confidence is not None
    assert document.confidence["parse"]["reconciliation"] == ["ok"]

    rows = env.transactions(account)
    assert len(rows) == len(stmt.rows)
    assert _rows(rows) == [
        (d, a.quantize(Decimal("0.000001")), p) for d, a, p in card.expected(stmt)
    ]
    assert all(r.review_state is ReviewState.NONE for r in rows)
    assert all(r.currency == "CAD" for r in rows)
    assert max(r.posted_date for r in rows) <= stmt.statement_date
    snapshots = env.snapshots(account)
    assert [s.balance for s in snapshots] == [stmt.closing_balance]


def test_statement_that_does_not_reconcile_is_held_for_review(env: Env) -> None:
    stmt = synth.sample_national_statement(seed=41)
    account = env.account(stmt)
    document_id = env.ingest(synth.sample_national_pdf(stmt, tamper="extra_row"))

    document = env.document(document_id)
    assert document.status is DocumentStatus.NEEDS_REVIEW
    rows = env.transactions(account)
    assert _rows(rows) == _expected(stmt, tamper="extra_row")
    # The row no running balance vouches for does not count until a person
    # looks; the others are the statement's own and are in the ledger.
    forged = next(r for r in rows if r.raw_payee == "Forged Payee")
    assert [r.id for r in rows if r.review_state is ReviewState.PENDING] == [forged.id]
    assert all(r.source_document_id == document_id for r in rows)
    assert env.snapshots(account) == []

    items = env.open_reviews(document_id)
    reasons = {(i.kind, i.payload.get("reason")) for i in items}
    assert (ReviewKind.LOW_CONFIDENCE_FIELD, "reconciliation_failed") in reasons
    assert (ReviewKind.LOW_CONFIDENCE_FIELD, "low_confidence") in reasons
    failed = next(i for i in items if i.payload.get("reason") == "reconciliation_failed")
    assert failed.payload["difference"] == "-777.77"
    assert failed.payload["opening"] == f"{stmt.opening_balance:f}"
    assert failed.payload["closing"] == f"{stmt.closing_balance:f}"
    assert failed.payload["running_mismatches"] == 1
    assert (failed.payload["rows_in_doubt"], failed.payload["rows_total"]) == (1, len(rows))
    assert failed.payload["account_id"] == str(account.id)
    low = next(i for i in items if i.payload.get("reason") == "low_confidence")
    assert low.payload["count"] == 1
    assert "reconciliation" in low.payload["fields"]
    (entry,) = low.payload["transactions"]
    assert entry["transaction_id"] == str(forged.id)
    assert "amount" in entry["fields"]
    assert entry["page"] == 1


def test_low_confidence_fields_are_imported_pending(env: Env) -> None:
    stmt = synth.generic_statement(seed=42)
    account = env.account(stmt, with_institution=False)
    document_id = env.ingest(synth.generic_pdf(stmt))

    document = env.document(document_id)
    assert document.status is DocumentStatus.NEEDS_REVIEW
    assert document.account_id == account.id
    rows = env.transactions(account)
    assert len(rows) == len(stmt.transactions)
    assert all(r.review_state is ReviewState.PENDING for r in rows)
    assert all(r.confidence is not None and r.confidence < Decimal("0.85") for r in rows)
    items = env.open_reviews(document_id)
    low = [i for i in items if i.kind is ReviewKind.LOW_CONFIDENCE_FIELD]
    assert [i.payload["reason"] for i in low] == ["low_confidence"]
    assert low[0].payload["fields"] == ["posted_date"]
    assert low[0].payload["parser"] == "statement_generic/9"
    assert {t["transaction_id"] for t in low[0].payload["transactions"]} == {
        str(r.id) for r in rows
    }
    # The statement itself reconciled: its closing balance is recorded.
    assert len(env.snapshots(account)) == 1


def test_yearless_statement_without_a_period_waits_with_a_year_question(env: Env) -> None:
    account = env.ledger.account(
        env.household_id, number_last4="5127", type="checking", currency="USD"
    )
    document_id = env.ingest(layouts.yearless_no_period())

    # The latest year in which the last row (27 March) is not after today.
    today = date.today()
    year = today.year if (today.month, today.day) >= (3, 27) else today.year - 1
    expected = [
        (posted.replace(year=year), amount.quantize(Decimal("0.000001")), payee)
        for posted, amount, payee in layouts.EXPECTED_ROWS["yearless_no_period"]
    ]
    rows = env.transactions(account)
    assert _rows(rows) == sorted(expected)
    assert {row.review_state for row in rows} == {ReviewState.PENDING}

    document = env.document(document_id)
    assert document.status is DocumentStatus.NEEDS_REVIEW
    assert document.account_id == account.id
    items = env.open_reviews(document_id)
    reasons = [item.payload.get("reason") for item in items]
    assert reasons.count("year_assumed") == 1, reasons
    (question,) = [item for item in items if item.payload.get("reason") == "year_assumed"]
    assert question.kind is ReviewKind.LOW_CONFIDENCE_FIELD
    assert question.payload["year"] == year
    assert question.payload["field"] == "posted_date"
    assert question.payload["parser"]
    # The rows wait under the item that names the field.
    (low,) = [item for item in items if item.payload.get("reason") == "low_confidence"]
    assert "posted_date" in low.payload["fields"]
    # An assumed year is no stated period.
    assert (document.period_start, document.period_end) == (None, None)
    # Nor a day a balance can be recorded on: no snapshot dated by a guess.
    assert env.snapshots(account) == []


def _money6(amount: Decimal) -> Decimal:
    return amount.quantize(Decimal("0.000001"))


def test_sectioned_unsigned_statement_imports_clean(env: Env) -> None:
    account = env.ledger.account(
        env.household_id, number_last4="7318", type="checking", currency="USD"
    )
    document_id = env.ingest(layouts.sectioned_unsigned_checking())

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert env.open_reviews(document_id) == []
    assert document.account_id == account.id
    rows = env.transactions(account)
    assert len(rows) == 9
    assert _rows(rows) == sorted(
        (posted, _money6(amount), payee)
        for posted, amount, payee in layouts.EXPECTED_ROWS["sectioned_unsigned_checking"]
    )
    assert all(r.review_state is ReviewState.NONE for r in rows)
    # Five withdrawals and a fee are money out, on both pages.
    assert sum(1 for r in rows if r.amount < 0) == 6
    _, closing = layouts.EXPECTED_BALANCES["sectioned_unsigned_checking"]
    assert [s.balance for s in env.snapshots(account)] == [closing]


def test_stacked_header_statement_imports_clean(env: Env) -> None:
    account = env.ledger.account(
        env.household_id, number_last4="3871", type="checking", currency="USD"
    )
    document_id = env.ingest(headers.stacked_header_checking())

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert env.open_reviews(document_id) == []
    assert document.account_id == account.id
    rows = env.transactions(account)
    assert sorted((r.raw_payee or "", r.amount) for r in rows) == sorted(
        (payee, _money6(amount)) for payee, amount in headers.STACKED_ROWS
    )
    assert all(r.review_state is ReviewState.NONE for r in rows)
    assert len(env.snapshots(account)) == 1


def test_checks_across_the_page_import_clean(env: Env) -> None:
    account = env.ledger.account(
        env.household_id, number_last4="5127", type="checking", currency="USD"
    )
    document_id = env.ingest(layouts.checks_three_across())

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert env.open_reviews(document_id) == []
    assert document.account_id == account.id
    # Every check of every group is a row: the statement adds up, and its
    # closing balance is recorded.
    rows = env.transactions(account)
    assert len(rows) == 16
    assert sorted((r.raw_payee or "", r.amount) for r in rows) == sorted(
        (payee or "", _money6(amount))
        for _, amount, payee in layouts.EXPECTED_ROWS["checks_three_across"]
    )
    assert all(r.review_state is ReviewState.NONE for r in rows)
    assert len(env.snapshots(account)) == 1


def test_headerless_statement_imports_clean(env: Env) -> None:
    account = env.ledger.account(
        env.household_id, number_last4="2946", type="checking", currency="USD"
    )
    document_id = env.ingest(sections.headerless_sections_checking())

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert env.open_reviews(document_id) == []
    assert document.account_id == account.id
    assert document.parser_version == "statement_generic/9"
    rows = env.transactions(account)
    assert len(rows) == 11
    assert _rows(rows) == sorted(
        (posted, _money6(amount), payee)
        for posted, amount, payee in sections.EXPECTED_ROWS["headerless_sections_checking"]
    )
    assert all(r.review_state is ReviewState.NONE for r in rows)
    # Four deposits in, seven withdrawals out: the titles give the direction.
    assert sum(1 for r in rows if r.amount < 0) == 7
    assert len(env.snapshots(account)) == 1


def test_two_accounts_by_summary_import_into_two_accounts(env: Env) -> None:
    checking = env.ledger.account(
        env.household_id, type="checking", currency="USD", name="Everyday Checking"
    )
    savings = env.ledger.account(
        env.household_id, type=AccountType.SAVINGS, currency="USD", name="Premier Savings"
    )
    document_id = env.ingest(sections.two_accounts_one_number())

    # Both accounts print one masked number and each statement carries it;
    # no account of the household has that number yet: each is asked about.
    stored = ExtractResult.model_validate(env.document(document_id).extraction)
    assert [len(s.transactions) for s in stored.statements] == [9, 3]
    assert [s.account.last4 for s in stored.statements] == ["0000", "0000"]
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert {i.payload.get("reason") for i in env.open_reviews(document_id)} == {"account_not_found"}
    assert env.transactions(checking) == env.transactions(savings) == []

    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        request_apply(
            db,
            env.principal(),
            document_id,
            ApplyMapping(
                assignments=[
                    Assignment(target="statement", index=0, account_id=checking.id),
                    Assignment(target="statement", index=1, account_id=savings.id),
                ]
            ),
            request=None,
        )
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert env.open_reviews(document_id) == []
    for account, (_, closing, expected) in zip(
        (checking, savings), sections.EXPECTED_ACCOUNTS, strict=True
    ):
        rows = env.transactions(account)
        assert _rows(rows) == sorted((d, _money6(a), p) for d, a, p in expected)
        assert all(r.review_state is ReviewState.NONE for r in rows)
        assert [s.balance for s in env.snapshots(account)] == [closing]
    assert len(env.transactions(checking)) == 9
    assert len(env.transactions(savings)) == 3


def _pair(env: Env) -> tuple[Account, Account]:
    """A checking and a savings account of the household under one masked number."""
    checking = env.ledger.account(
        env.household_id,
        type=AccountType.CHECKING,
        currency="USD",
        name="Everyday Checking",
        number_last4="0000",
    )
    savings = env.ledger.account(
        env.household_id,
        type=AccountType.SAVINGS,
        currency="USD",
        name="Premier Savings",
        number_last4="0000",
    )
    return checking, savings


@pytest.mark.parametrize("builder", ["two_accounts_one_number", "two_accounts_named_above"])
def test_two_accounts_under_one_number_land_on_their_accounts_by_type(
    env: Env, builder: str
) -> None:
    checking, savings = _pair(env)
    document_id = env.ingest(getattr(sections, builder)())

    stored = ExtractResult.model_validate(env.document(document_id).extraction)
    assert [(s.account.account_type, s.account.last4) for s in stored.statements] == [
        ("checking", "0000"),
        ("savings", "0000"),
    ]
    # The number is that of both accounts; each summary's title says which
    # of the two its statement is. Nothing is asked.
    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert env.open_reviews(document_id) == []
    for account, (_, closing, expected) in zip(
        (checking, savings), sections.EXPECTED_ACCOUNTS, strict=True
    ):
        rows = env.transactions(account)
        assert _rows(rows) == sorted((d, _money6(a), p) for d, a, p in expected)
        assert [s.balance for s in env.snapshots(account)] == [closing]
    # The Accounts dialog says the same of each statement.
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        found = proposals.detect_accounts(db, env.principal(), document_id)
    assert [(f.status, f.account_id) for f in found.accounts] == [
        ("matched", checking.id),
        ("matched", savings.id),
    ]


def test_unknown_accounts_under_one_number_are_proposed_by_their_titles(env: Env) -> None:
    document_id = env.ingest(sections.two_accounts_one_number())
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        found = proposals.detect_accounts(db, env.principal(), document_id)
    proposed = [f.proposal for f in found.accounts]
    assert all(p is not None for p in proposed)
    # A savings account is proposed as one, not as a checking account.
    assert [(p.type, p.number_last4) for p in proposed if p is not None] == [
        (AccountType.CHECKING, "0000"),
        (AccountType.SAVINGS, "0000"),
    ]


def test_two_accounts_under_one_number_wait_when_no_title_says_which(env: Env) -> None:
    checking, savings = _pair(env)
    document_id = env.ingest(sections.two_accounts_unnamed())

    stored = ExtractResult.model_validate(env.document(document_id).extraction)
    assert [(s.account.account_type, s.account.last4) for s in stored.statements] == [
        (None, "0000"),
        (None, "0000"),
    ]
    # Two accounts carry the number and no title names a kind: nothing is
    # guessed, each statement is asked about with both accounts as candidates.
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    items = [i for i in env.open_reviews(document_id) if i.kind is ReviewKind.UNKNOWN_ACCOUNT]
    assert [i.payload.get("reason") for i in items] == ["account_ambiguous"] * 2
    assert all(
        set(i.payload.get("candidates", [])) == {str(checking.id), str(savings.id)} for i in items
    )
    assert env.transactions(checking) == env.transactions(savings) == []


def test_a_savings_statement_never_lands_on_the_checking_account_by_its_number(
    env: Env,
) -> None:
    # The household knows the checking account only. The savings statement
    # carries the same number: it is asked about, not put on that account.
    checking = env.ledger.account(
        env.household_id,
        type=AccountType.CHECKING,
        currency="USD",
        name="Everyday Checking",
        number_last4="0000",
    )
    document_id = env.ingest(sections.two_accounts_one_number())

    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    items = [i for i in env.open_reviews(document_id) if i.kind is ReviewKind.UNKNOWN_ACCOUNT]
    assert [(i.payload.get("statement"), i.payload.get("account_type")) for i in items] == [
        (1, "savings")
    ]
    assert len(env.transactions(checking)) == 9


def test_a_checking_statement_never_lands_on_the_savings_account_by_its_number(
    env: Env,
) -> None:
    # The other way round: the only account with the digits is a savings
    # account, and the checking statement is asked about with it as the
    # candidate.
    savings = env.ledger.account(
        env.household_id,
        type=AccountType.SAVINGS,
        currency="USD",
        name="Premier Savings",
        number_last4="0000",
    )
    document_id = env.ingest(sections.two_accounts_one_number())

    items = [i for i in env.open_reviews(document_id) if i.kind is ReviewKind.UNKNOWN_ACCOUNT]
    assert [(i.payload.get("statement"), i.payload.get("reason")) for i in items] == [
        (0, "account_ambiguous")
    ]
    assert len(env.transactions(savings)) == 3


def test_a_shared_number_is_matched_by_type_then_by_name(env: Env) -> None:
    checking, savings = _pair(env)
    holiday = env.ledger.account(
        env.household_id,
        type=AccountType.SAVINGS,
        currency="USD",
        name="Holiday Club",
        number_last4="0000",
    )
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())

        def match(kind: str | None, name: str | None = None, **kwargs: Any) -> uuid.UUID | None:
            found = match_shared_number(
                db, env.household_id, last4="0000", account_type=kind, name=name, **kwargs
            )
            assert set(found.candidates) <= {checking.id, savings.id, holiday.id}
            return found.account_id

        assert match("checking") == checking.id
        # Two savings accounts carry the digits: the name says which, or nothing does.
        assert match("savings") is None
        assert match("savings", "PREMIER SAVINGS") == savings.id
        assert match("savings", "Holiday Club Savings") == holiday.id
        assert match("savings", "Thistlebeck Reserve") is None
        # No type named, a type no account with the digits has, and a type
        # another statement of the document names too.
        assert match(None, "Everyday Checking") is None
        assert match("creditcard") is None
        assert match("checking", told_apart=False) is None
        # Only accounts the uploader may import into.
        assert match("checking", allowed=frozenset({savings.id})) is None


def test_marker_column_statement_imports_clean(env: Env) -> None:
    account = env.ledger.account(
        env.household_id, number_last4="6183", type="checking", currency="USD"
    )
    document_id = env.ingest(layouts2.marker_column_checking())

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert env.open_reviews(document_id) == []
    assert document.account_id == account.id
    rows = env.transactions(account)
    assert _rows(rows) == sorted(
        (posted, _money6(amount), payee)
        for posted, amount, payee in layouts2.EXPECTED_ROWS["marker_column_checking"]
    )
    assert all(r.review_state is ReviewState.NONE for r in rows)
    # DB is money out (five rows), CR money in (three).
    assert sum(1 for r in rows if r.amount < 0) == 5
    assert len(env.snapshots(account)) == 1


def test_loan_bill_writes_no_bank_rows(env: Env) -> None:
    # Accounts a bank reading of the bill could land on: its own digits.
    checking = env.ledger.account(
        env.household_id, number_last4="7714", type="checking", currency="USD"
    )
    document_id = env.ingest(sections.loan_bill_with_activity())

    document = env.document(document_id)
    # The loan parser has the bill or nobody has; no bank parser reads it.
    assert not (document.parser_version or "").startswith(
        ("statement_generic/", "card_statement_generic/")
    )
    stored = ExtractResult.model_validate(document.extraction)
    assert stored.statements == []
    assert env.query(select(Transaction).where(Transaction.household_id == env.household_id)) == []
    assert env.snapshots(checking) == []


def test_unknown_account_is_chosen_later_from_the_stored_extraction(env: Env) -> None:
    stmt = synth.mock_savings_statement(seed=43)
    document_id = env.ingest(synth.mock_savings_pdf(stmt))
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    reasons = {i.payload.get("reason") for i in env.open_reviews(document_id)}
    assert "account_not_found" in reasons
    extract_jobs = env.query(
        select(IngestJob).where(
            IngestJob.document_id == document_id, IngestJob.stage == IngestStage.EXTRACT
        )
    )

    account = env.ledger.account(env.household_id, currency="GBP", name="Savings")
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        request_apply(
            db, env.principal(), document_id, ApplyMapping(account_id=account.id), request=None
        )
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED
    assert _rows(env.transactions(account)) == _expected(stmt)
    # No second extraction: the parsed statement came from document.extraction.
    assert len(
        env.query(
            select(IngestJob).where(
                IngestJob.document_id == document_id, IngestJob.stage == IngestStage.EXTRACT
            )
        )
    ) == len(extract_jobs)


def test_payslip_is_kept_on_the_document(env: Env) -> None:
    document_id = env.ingest(synth.payslip_pdf())
    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert document.kind is DocumentKind.PAYSLIP
    assert (document.period_start, document.period_end) == (date(2026, 1, 1), date(2026, 1, 31))
    stored = ExtractResult.model_validate(document.extraction)
    assert stored.parse is not None
    assert stored.parse.payslip is not None
    assert stored.parse.payslip.net == Decimal("2847.50")

    wrong = env.ingest(synth.payslip_pdf(net_override=Decimal("3000.00")))
    assert env.document(wrong).status is DocumentStatus.NEEDS_REVIEW
    items = env.open_reviews(wrong)
    assert [i.payload["reason"] for i in items] == ["payslip_does_not_add_up"]
