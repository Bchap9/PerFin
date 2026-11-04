# SPDX-License-Identifier: AGPL-3.0-or-later
"""Classification against the database and through the real pipeline.

* "Remember this layout": a household fingerprint is validated, versioned,
  invisible to other households (RLS) and wins over the shipped catalogue.
* Account matching by last four digits and institution respects the
  uploader's grants (SECURITY.md F3) and never replaces an upload's account.
* A synthetic PDF in a shipped layout comes out of the pipeline with kind,
  institution, account and period set and per-field confidence stored.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Sequence
from datetime import date
from pathlib import Path

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import select

from app.auth.models import GrantLevel
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import Document, DocumentKind, DocumentSource, ParserFingerprint
from app.ingestion import pipeline
from app.ingestion.classify.fingerprints import (
    FingerprintError,
    household_fingerprints,
    remember_layout,
)
from app.ingestion.classify.service import (
    all_fingerprints,
    apply_classification,
    classify_view,
)
from app.ingestion.classify.text import DocumentView
from app.ingestion.intake.upload import receive_stream
from app.ingestion.sandbox.protocol import ExtractOptions
from app.ingestion.sandbox.runner import SandboxConfig
from tests.fixtures import synth_classify as synth
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration

MAX = 20 * 1024 * 1024
RULES = {"all": [{"header": "Riverbend Test Bank"}, {"text": "Savings statement"}]}


def _hid(record: UserRecord) -> uuid.UUID:
    assert record.household_id is not None
    return record.household_id


def _document(household_id: uuid.UUID, **fields: object) -> Document:
    """An unsaved document row (classification only sets its fields)."""
    return Document(
        household_id=household_id,
        source=DocumentSource.UPLOAD,
        sha256="0" * 64,
        size=1,
        mime_detected="application/pdf",
        **fields,
    )


def test_remembered_layout_is_validated_versioned_and_household_scoped(
    factories: Factories, ledger: LedgerFactories
) -> None:
    owner = factories.user(role="owner")
    other = factories.user(role="owner")
    bank = ledger.institution(_hid(owner), name="Riverbend Test Bank")
    with factories.transaction() as db:
        set_context(db, _hid(owner))
        first = remember_layout(
            db,
            _hid(owner),
            institution_id=bank.id,
            kind=DocumentKind.STATEMENT,
            match_rules=RULES,
            parser_name="statement_generic",
        )
        second = remember_layout(
            db,
            _hid(owner),
            institution_id=bank.id,
            kind=DocumentKind.STATEMENT,
            match_rules=RULES,
            parser_name="statement_generic",
        )
        assert (first.version, second.version) == (1, 2)
        bad: object
        for bad in ({"all": [{"regex": "(a+)+$"}]}, {"all": []}, {"nope": 1}):
            with pytest.raises(FingerprintError):
                remember_layout(
                    db,
                    _hid(owner),
                    institution_id=bank.id,
                    kind=DocumentKind.STATEMENT,
                    match_rules=bad,
                    parser_name="statement_generic",
                )
        with pytest.raises(FingerprintError):
            remember_layout(
                db,
                _hid(owner),
                institution_id=bank.id,
                kind=DocumentKind.UNKNOWN,
                match_rules=RULES,
                parser_name="statement_generic",
            )
        # Only the latest version of a layout is matched (P34-SR-R2-01).
        fps = household_fingerprints(db, _hid(owner))
        assert [fp.origin for fp in fps] == ["household"]
        assert fps[0].fingerprint_id == second.id
        assert fps[0].spec.version == 2
        assert all(fp.institution_id == bank.id for fp in fps)
    with factories.transaction() as db:
        set_context(db, _hid(other))
        assert household_fingerprints(db, _hid(other)) == []
        assert household_fingerprints(db, _hid(owner)) == []
        assert db.execute(select(ParserFingerprint)).scalars().all() == []


def test_a_broken_stored_row_is_skipped(factories: Factories, ledger: LedgerFactories) -> None:
    owner = factories.user(role="owner")
    bank = ledger.institution(_hid(owner), name="Hilltop Invented Savings")
    with factories.transaction() as db:
        set_context(db, _hid(owner))
        db.add(
            ParserFingerprint(
                household_id=_hid(owner),
                institution_id=bank.id,
                kind=DocumentKind.STATEMENT,
                match_rules={"all": [{"regex": "(x+)+y"}]},
                parser_name="statement_generic",
                version=1,
            )
        )
        db.flush()
        assert household_fingerprints(db, _hid(owner)) == []


def test_household_fingerprint_classifies_and_resolves_its_institution(
    factories: Factories, ledger: LedgerFactories
) -> None:
    owner = factories.user(role="owner")
    bank = ledger.institution(_hid(owner), name="Riverbend Test Bank")
    account = ledger.account(_hid(owner), institution_id=bank.id, number_last4="4455")
    doc = synth.SynthDoc(
        "statement",
        (
            "Riverbend Test Bank",
            "Savings statement",
            "Account ending 4455",
            "Statement period 2026-03-01 to 2026-03-31",
        ),
    )
    with factories.transaction() as db:
        set_context(db, _hid(owner))
        remember_layout(
            db,
            _hid(owner),
            institution_id=bank.id,
            kind=DocumentKind.STATEMENT,
            match_rules=RULES,
            parser_name="statement_generic",
        )
        result = classify_view(
            DocumentView.from_text(doc.text), fingerprints=all_fingerprints(db, _hid(owner))
        )
        assert result.method == "fingerprint"
        assert result.fingerprint is not None
        assert result.fingerprint.origin == "household"
        document = _document(_hid(owner), uploaded_by=owner.user.id)
        facts = apply_classification(db, document, result)
        assert document.kind is DocumentKind.STATEMENT
        assert document.institution_id == bank.id
        assert document.account_id == account.id
        assert (document.period_start, document.period_end) == (date(2026, 3, 1), date(2026, 3, 31))
        assert document.confidence is not None
        assert document.confidence["fields"]["account"] > 0.8
        assert document.confidence["fingerprint"]["origin"] == "household"
        assert facts["account_candidates"] == 1
        assert "4455" not in str(document.confidence)


def _sample_doc() -> synth.SynthDoc:
    return synth.institution_document("sample-national-bank-checking", 2)


def test_account_match_respects_grants_and_uploads(
    factories: Factories, ledger: LedgerFactories
) -> None:
    owner = factories.user(role="owner")
    household = owner.household
    doc = _sample_doc()
    bank = ledger.institution(_hid(owner), name="Sample National Bank")
    hidden = ledger.account(_hid(owner), institution_id=bank.id, number_last4=doc.last4)
    member = factories.user(role="member", household=household)
    result = classify_view(DocumentView.from_text(doc.text, table_headers=doc.table_headers))
    with factories.transaction() as db:
        set_context(db, _hid(owner))
        mine = _document(_hid(owner), uploaded_by=member.user.id)
        apply_classification(db, mine, result)
        assert mine.account_id is None  # the member cannot edit the account
        assert mine.institution_id == bank.id
    factories.grant(_hid(owner), member.user.id, hidden.id, GrantLevel.EDIT)
    with factories.transaction() as db:
        set_context(db, _hid(owner))
        granted = _document(_hid(owner), uploaded_by=member.user.id)
        apply_classification(db, granted, result)
        assert granted.account_id == hidden.id
        named = ledger.account(_hid(owner), number_last4="0000")
        explicit = _document(_hid(owner), uploaded_by=owner.user.id, account_id=named.id)
        apply_classification(db, explicit, result)
        assert explicit.account_id == named.id


def test_same_last4_is_broken_by_institution(factories: Factories, ledger: LedgerFactories) -> None:
    owner = factories.user(role="owner")
    doc = _sample_doc()
    bank = ledger.institution(_hid(owner), name="Sample National Bank")
    elsewhere = ledger.institution(_hid(owner), name="Lakeshore Imaginary Trust")
    right = ledger.account(_hid(owner), institution_id=bank.id, number_last4=doc.last4)
    ledger.account(_hid(owner), institution_id=elsewhere.id, number_last4=doc.last4)
    result = classify_view(DocumentView.from_text(doc.text, table_headers=doc.table_headers))
    with factories.transaction() as db:
        set_context(db, _hid(owner))
        document = _document(_hid(owner), uploaded_by=owner.user.id)
        facts = apply_classification(db, document, result)
        assert document.account_id == right.id
        assert facts["account_candidates"] == 2


# ---------------------------------------------------------------------------
# Through the pipeline (real sandbox)
# ---------------------------------------------------------------------------


def _pdf(lines: Sequence[str]) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica", 10)
    for i, line in enumerate(lines):
        c.drawString(60, 740 - i * 16, line)
    c.showPage()
    c.save()
    return buf.getvalue()


@pytest.fixture
def store(tmp_path: Path) -> LocalFSBlobStore:
    return LocalFSBlobStore(tmp_path / "documents", tmp_path / "quarantine")


def _run(
    factories: Factories,
    owner: UserRecord,
    store: LocalFSBlobStore,
    settings: Settings,
    data: bytes,
) -> Document:
    keyring = Keyring.from_settings(settings)
    deps = pipeline.PipelineDeps(
        store=store,
        keyring=keyring,
        sandbox=SandboxConfig(timeout_seconds=90),
        options=ExtractOptions(previews=False),
        child_max_bytes=MAX,
    )
    with factories.transaction() as db:
        set_context(db, _hid(owner))
        document_id = receive_stream(
            db,
            store=store,
            keyring=keyring,
            household_id=_hid(owner),
            chunks=(data,),
            max_bytes=MAX,
            source=DocumentSource.UPLOAD,
            original_filename="doc.pdf",
            uploaded_by=owner.user.id,
        ).document_id
    with factories.transaction() as db:
        set_context(db, _hid(owner))
        pipeline.ingest_document(db, _hid(owner), document_id, deps)
    with factories.transaction() as db:
        set_context(db, _hid(owner))
        document = db.execute(select(Document).where(Document.id == document_id)).scalar_one()
        db.expunge(document)
        return document


def test_pipeline_classifies_a_pdf_in_a_shipped_layout(
    factories: Factories, ledger: LedgerFactories, store: LocalFSBlobStore, settings: Settings
) -> None:
    owner = factories.user(role="owner")
    doc = _sample_doc()
    bank = ledger.institution(_hid(owner), name="Sample National Bank")
    account = ledger.account(_hid(owner), institution_id=bank.id, number_last4=doc.last4)
    document = _run(factories, owner, store, settings, _pdf(doc.lines))
    assert document.kind is DocumentKind.STATEMENT
    assert document.institution_id == bank.id
    assert document.account_id == account.id
    assert (document.period_start, document.period_end) == (doc.period_start, doc.period_end)
    assert document.confidence is not None
    assert document.confidence["method"] == "fingerprint"
    assert document.confidence["fingerprint"]["key"] == "sample-national-bank-checking"
    assert set(document.confidence["fields"]) >= {
        "kind",
        "institution",
        "period",
        "last4",
        "account",
    }


def test_pipeline_classifies_a_receipt_and_a_tax_form_without_a_layout(
    factories: Factories, store: LocalFSBlobStore, settings: Settings
) -> None:
    owner = factories.user(role="owner")
    receipt = synth.generic_document("receipt", 8)
    document = _run(factories, owner, store, settings, _pdf(receipt.lines))
    assert document.kind is DocumentKind.RECEIPT
    assert document.period_start == document.period_end == receipt.printed_date
    assert document.confidence is not None
    assert document.confidence["method"] == "classifier"
    form = synth.generic_document("tax_form", 2)
    taxed = _run(factories, owner, store, settings, _pdf(form.lines))
    assert taxed.kind is DocumentKind.TAX_FORM
    assert taxed.tax_year == form.tax_year


def test_unrecognisable_pdf_stays_unknown(
    factories: Factories, store: LocalFSBlobStore, settings: Settings
) -> None:
    owner = factories.user(role="owner")
    document = _run(factories, owner, store, settings, _pdf(["zzqx vvkp", "mmrt"]))
    assert document.kind is DocumentKind.UNKNOWN
    assert document.confidence is not None
    assert "low_confidence_kind" in document.confidence["review"]
    with factories.transaction() as db:
        set_context(db, _hid(owner))
        assert db.execute(select(Account)).scalars().all() == []
