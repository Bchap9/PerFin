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
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE, ApplyMapping
from app.domain.imports.service import request_apply
from app.domain.transactions.models import ReviewState, Transaction
from app.ingestion.intake.upload import receive_upload
from app.ingestion.sandbox.protocol import ExtractResult
from app.workers.handlers import ingest
from tests.fixtures import synth_pdf_statements as synth
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
    assert document.parser_version.endswith("/2")
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


def test_statement_that_does_not_reconcile_is_held_for_review(env: Env) -> None:
    stmt = synth.sample_national_statement(seed=41)
    account = env.account(stmt)
    document_id = env.ingest(synth.sample_national_pdf(stmt, tamper="extra_row"))

    document = env.document(document_id)
    assert document.status is DocumentStatus.NEEDS_REVIEW
    rows = env.transactions(account)
    assert _rows(rows) == _expected(stmt, tamper="extra_row")
    # Nothing of a statement that does not add up counts until a person looks.
    assert all(r.review_state is ReviewState.PENDING for r in rows)
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
    assert failed.payload["account_id"] == str(account.id)
    low = next(i for i in items if i.payload.get("reason") == "low_confidence")
    assert low.payload["count"] == len(rows)
    assert "reconciliation" in low.payload["fields"]
    forged = next(r for r in rows if r.raw_payee == "Forged Payee")
    entry = next(t for t in low.payload["transactions"] if t["transaction_id"] == str(forged.id))
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
    assert low[0].payload["parser"] == "statement_generic/3"
    assert {t["transaction_id"] for t in low[0].payload["transactions"]} == {
        str(r.id) for r in rows
    }
    # The statement itself reconciled: its closing balance is recorded.
    assert len(env.snapshots(account)) == 1


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
