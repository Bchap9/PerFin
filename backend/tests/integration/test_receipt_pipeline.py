# SPDX-License-Identifier: AGPL-3.0-or-later
"""Receipts end to end through the worker (INGESTION.md "Parse", "Match and deduplicate").

upload -> worker ``ingest_document`` -> real sandbox (Tesseract OCR for a
thermal-print photo, the text layer of an e-receipt PDF, the HTML body of a
receipt mail) -> receipt parser -> matcher -> ``document_link`` or a
``receipt_match`` review item, with the production pipeline dependencies
(``app.workers.handlers.ingest.default_deps``) on a temporary blob store.
Receipts come from ``tests/fixtures/synth_receipts.py``.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.principal import Principal, apply_rls_context
from app.core.settings import Settings
from app.db.models import AuditLog
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentKind,
    DocumentLink,
    DocumentStatus,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.transactions.models import Transaction
from app.ingestion.intake.upload import receive_upload
from app.ingestion.sandbox.protocol import ExtractResult
from app.workers.handlers import ingest
from tests.fixtures import synth_receipts as sr
from tests.fixtures import synth_statements as ss
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.imports import run_jobs
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration

MAX = 10 * 1024 * 1024
#: Card digits printed on the receipts below; they must never reach a log.
LAST4 = "8642"


@dataclass
class Env:
    factories: Factories
    ledger: LedgerFactories
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

    def upload(self, data: bytes, filename: str, store: LocalFSBlobStore) -> uuid.UUID:
        deps = ingest.default_deps()
        with self.factories.transaction() as db:
            apply_rls_context(db, self.principal())
            received = receive_upload(
                db,
                self.principal(),
                store=deps.store,
                keyring=deps.keyring,
                chunks=iter([data]),
                max_bytes=MAX,
                filename=filename,
                account_id=None,
            )
        # Containers queue their children: run until nothing is left.
        while run_jobs(self.factories, self.household_id, INGEST_JOB_TYPE, done=self.done):
            pass
        return received.document_id

    def rows(self, statement: Any) -> list[Any]:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            found = list(db.execute(statement).scalars())
            db.expunge_all()
            return found

    def document(self, document_id: uuid.UUID) -> Document:
        found: list[Document] = self.rows(select(Document).where(Document.id == document_id))
        return found[0]

    def links(self, document_id: uuid.UUID) -> list[uuid.UUID]:
        return [
            link.target_id
            for link in self.rows(
                select(DocumentLink).where(DocumentLink.document_id == document_id)
            )
        ]

    def items(self, document_id: uuid.UUID) -> list[ReviewItem]:
        return self.rows(
            select(ReviewItem)
            .where(ReviewItem.document_id == document_id)
            .order_by(ReviewItem.created_at, ReviewItem.id)
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
    yield Env(factories=factories, ledger=ledger, owner=factories.user(role="owner"))
    ingest.default_deps.cache_clear()


@pytest.fixture
def store(env: Env) -> LocalFSBlobStore:
    deps = ingest.default_deps()
    assert isinstance(deps.store, LocalFSBlobStore)
    return deps.store


def _spec(seed: int, **changes: Any) -> sr.ReceiptSpec:
    spec = sr.receipt_spec(seed)
    return dataclasses.replace(spec, last4=LAST4, payment="VISA", **changes)


def _account(env: Env, spec: sr.ReceiptSpec, **kwargs: Any) -> Account:
    return env.ledger.account(env.household_id, currency=spec.currency, **kwargs)


def _paid(env: Env, account: Account, spec: sr.ReceiptSpec, *, days: int = 1) -> Transaction:
    """The card transaction for ``spec``: posted a day later, bank-style payee text."""
    return env.ledger.transaction(
        account,
        amount=-spec.total,
        posted_date=spec.when + timedelta(days=days),
        raw_payee=f"POS {spec.merchant} 0412",
    )


def _no_digits_logged(caplog: pytest.LogCaptureFixture) -> None:
    assert LAST4 not in caplog.text


def _receipt(document: Document) -> Any:
    stored = ExtractResult.model_validate(document.extraction)
    assert stored.parse is not None
    assert stored.parse.receipt is not None
    return stored.parse.receipt


def test_thermal_receipt_photo_is_ocrd_parsed_and_linked(
    env: Env, store: LocalFSBlobStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    spec = _spec(20_004)
    account = _account(env, spec)
    paid = _paid(env, account, spec)
    env.ledger.transaction(account, amount=-spec.total, posted_date=spec.when + timedelta(days=9))
    doc_id = env.upload(sr.thermal_png(spec, sr.Thermal(angle=-1.5)), "receipt.png", store)
    document = env.document(doc_id)
    assert document.status is DocumentStatus.PROCESSED, env.items(doc_id)
    assert document.kind is DocumentKind.RECEIPT
    assert document.parser_version is not None
    assert document.parser_version.startswith("receipt_basic/")
    receipt = _receipt(document)
    assert receipt.total == spec.total
    assert receipt.payment.last4 == LAST4  # stored ...
    assert env.links(doc_id) == [paid.id]
    assert document.confidence["receipt"]["match"] == "linked"
    stored = ExtractResult.model_validate(document.extraction)
    assert stored.pages[0].text_source == "ocr"
    # ... but never logged, audited or put in a review item.
    _no_digits_logged(caplog)
    audit = env.rows(select(AuditLog).where(AuditLog.target_id == str(doc_id)))
    assert audit
    assert all(LAST4 not in str(row.after) for row in audit)


def test_pdf_e_receipt_is_linked(env: Env, store: LocalFSBlobStore) -> None:
    spec = _spec(20_011)
    account = _account(env, spec)
    paid = _paid(env, account, spec, days=0)
    doc_id = env.upload(sr.receipt_pdf(spec), "receipt.pdf", store)
    document = env.document(doc_id)
    assert document.status is DocumentStatus.PROCESSED, env.items(doc_id)
    assert document.kind is DocumentKind.RECEIPT
    assert (document.period_start, document.period_end) == (spec.when, spec.when)
    receipt = _receipt(document)
    assert (receipt.subtotal, receipt.tax, receipt.total) == (spec.subtotal, spec.tax, spec.total)
    assert receipt.low_confidence == []
    assert env.links(doc_id) == [paid.id]


def test_receipt_mail_body_and_its_pdf_attachment_are_both_linked(
    env: Env, store: LocalFSBlobStore, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    spec = _spec(20_017)
    account = _account(env, spec)
    paid = _paid(env, account, spec)
    mail = sr.receipt_html_email(
        spec, attachments=[("receipt.pdf", "application/pdf", sr.receipt_pdf(spec))]
    )
    mail_id = env.upload(mail, "receipt.eml", store)
    document = env.document(mail_id)
    assert document.status is DocumentStatus.PROCESSED, env.items(mail_id)
    assert document.kind is DocumentKind.RECEIPT
    receipt = _receipt(document)
    assert receipt.source == "email_body"
    assert receipt.merchant == spec.payee
    assert env.links(mail_id) == [paid.id]
    (child,) = env.rows(
        select(Document).where(Document.source_meta["parent_document_id"].astext == str(mail_id))
    )
    assert child.status is DocumentStatus.PROCESSED, env.items(child.id)
    # The attachment is the same purchase: it may share the transaction.
    assert env.links(child.id) == [paid.id]
    _no_digits_logged(caplog)


def test_receipt_without_a_transaction_waits_and_links_when_the_statement_arrives(
    env: Env, store: LocalFSBlobStore
) -> None:
    spec = _spec(20_023, currency="USD")
    statement = ss.SynthStatement(
        institution=ss.INSTITUTIONS[0],
        account_number="000123457777",
        account_type="checking",
        currency="USD",
        start=spec.when - timedelta(days=10),
        end=spec.when + timedelta(days=10),
        opening_balance=Decimal("1000.00"),
        transactions=(
            ss.SynthTxn(
                spec.when + timedelta(days=1),
                -spec.total,
                f"{spec.merchant} SPRINGFIELD",
                "",
                "FIT-1",
            ),
            ss.SynthTxn(spec.when, Decimal("-3.50"), "COFFEE", "", "FIT-2"),
        ),
    )
    account = env.ledger.account(env.household_id, number_last4="7777", currency="USD")
    receipt_id = env.upload(sr.receipt_pdf(spec), "receipt.pdf", store)
    waiting = env.document(receipt_id)
    assert waiting.status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.items(receipt_id)
    assert item.kind is ReviewKind.RECEIPT_MATCH
    assert item.payload["reason"] == "no_candidates"
    statement_id = env.upload(ss.ofx_sgml_bytes(statement), "statement.ofx", store)
    assert env.document(statement_id).status is DocumentStatus.PROCESSED
    (paid,) = env.rows(
        select(Transaction).where(
            Transaction.account_id == account.id, Transaction.amount == -spec.total
        )
    )
    assert env.links(receipt_id) == [paid.id]
    (item,) = env.items(receipt_id)
    assert item.state is ReviewItemState.RESOLVED
    assert item.resolution["action"] == "auto_link"
    assert env.document(receipt_id).status is DocumentStatus.PROCESSED


def test_two_candidate_transactions_leave_the_choice_to_a_person(
    env: Env, store: LocalFSBlobStore
) -> None:
    spec = _spec(20_011)
    account = _account(env, spec)
    first = _paid(env, account, spec, days=0)
    second = _paid(env, account, spec, days=0)
    doc_id = env.upload(sr.receipt_pdf(spec), "receipt.pdf", store)
    assert env.document(doc_id).status is DocumentStatus.NEEDS_REVIEW
    assert env.links(doc_id) == []
    (item,) = env.items(doc_id)
    assert item.kind is ReviewKind.RECEIPT_MATCH
    assert item.payload["reason"] == "ambiguous"
    assert {s["transaction_id"] for s in item.payload["suggestions"]} == {
        str(first.id),
        str(second.id),
    }
    assert LAST4 not in str(item.payload)
