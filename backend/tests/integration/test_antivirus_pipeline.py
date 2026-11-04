# SPDX-License-Identifier: AGPL-3.0-or-later
"""The pipeline's antivirus stage against a fake clamd (SECURITY.md C9), and XLSX imports.

A positive scan rejects the document with the signature recorded and keeps
it in quarantine (never served); a scanner that cannot scan fails closed by
default (retried through the queue, then ``failed``) or, configured to fail
open, lets the document through unscanned.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.models import AuditLog
from app.db.rls import set_context
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Blob,
    BlobArea,
    Document,
    DocumentSource,
    DocumentStatus,
    IngestJob,
    IngestStage,
    ReviewItem,
)
from app.domain.transactions.models import Transaction
from app.ingestion import pipeline
from app.ingestion.intake.upload import receive_stream
from app.integrations.antivirus.clamav import ClamdAddress, ClamdScanner
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.fake_clamd import MARKER, SIGNATURE, fake_clamd
from tests.fixtures.synth_xlsx import Cell, workbook

pytestmark = pytest.mark.integration

MAX = 5 * 1024 * 1024
CLEAN = b"Date,Description,Amount\n2026-01-02,Corner Grocer,-12.34\n2026-01-03,Payroll,100.00\n"
INFECTED = b"Date,Description,Amount\n2026-01-02," + MARKER + b",-1.00\n2026-01-03,Payroll,1.00\n"


@pytest.fixture
def store(tmp_path: Path) -> LocalFSBlobStore:
    return LocalFSBlobStore(tmp_path / "documents", tmp_path / "quarantine")


@pytest.fixture
def keyring(settings: Settings) -> Keyring:
    return Keyring.from_settings(settings)


def _receive(
    factories: Factories, owner: UserRecord, store: LocalFSBlobStore, keyring: Keyring, data: bytes
) -> uuid.UUID:
    assert owner.household_id is not None
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        return receive_stream(
            db,
            store=store,
            keyring=keyring,
            household_id=owner.household_id,
            chunks=(data,),
            max_bytes=MAX,
            source=DocumentSource.UPLOAD,
            original_filename="statement",
            uploaded_by=owner.user.id,
        ).document_id


def _deps(
    store: LocalFSBlobStore, keyring: Keyring, address: str | None, **kwargs: object
) -> pipeline.PipelineDeps:
    scanner = (
        ClamdScanner(address=ClamdAddress.parse(address), timeout_seconds=2)
        if address is not None
        else None
    )
    return pipeline.PipelineDeps(
        store=store,
        keyring=keyring,
        antivirus="clamav" if scanner else "none",
        scanner=scanner,
        importer=pipeline.structured_import,
        **kwargs,  # type: ignore[arg-type]
    )


def _ingest(
    factories: Factories,
    owner: UserRecord,
    document_id: uuid.UUID,
    deps: pipeline.PipelineDeps,
    *,
    final_attempt: bool = True,
) -> DocumentStatus | None:
    assert owner.household_id is not None
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        return pipeline.ingest_document(
            db, owner.household_id, document_id, deps, final_attempt=final_attempt
        )


def _state(
    factories: Factories, owner: UserRecord, document_id: uuid.UUID
) -> tuple[Document, Blob, list[IngestJob], list[ReviewItem]]:
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        document = db.get(Document, document_id)
        assert document is not None
        blob = db.get(Blob, document.blob_id)
        assert blob is not None
        stages = list(
            db.execute(
                select(IngestJob)
                .where(IngestJob.document_id == document_id)
                .order_by(IngestJob.started_at, IngestJob.id)
            ).scalars()
        )
        items = list(
            db.execute(select(ReviewItem).where(ReviewItem.document_id == document_id)).scalars()
        )
        db.expunge_all()
        return document, blob, stages, items


def test_infected_document_is_rejected_with_its_signature(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    document_id = _receive(factories, owner, store, keyring, INFECTED)
    with fake_clamd("signature") as clamd:
        status = _ingest(factories, owner, document_id, _deps(store, keyring, clamd.address))
    assert status is DocumentStatus.REJECTED
    # clamd saw the plaintext, not the ciphertext.
    assert clamd.streams == [INFECTED]
    document, blob, stages, _ = _state(factories, owner, document_id)
    assert document.error == f"malware detected: {SIGNATURE}"
    assert document.extraction is None  # nothing parsed it
    assert blob.area is BlobArea.QUARANTINE  # never served (409 document-quarantined)
    assert [s.stage for s in stages] == [IngestStage.SCAN]
    assert stages[0].log["verdict"] == "infected"
    assert stages[0].log["signature"] == SIGNATURE
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        audit_row = db.execute(
            select(AuditLog)
            .where(AuditLog.action == "document.ingest", AuditLog.target_id == str(document_id))
            .order_by(AuditLog.id.desc())
        ).scalars().first()
        assert audit_row is not None
        assert audit_row.after is not None
        assert audit_row.after["signature"] == SIGNATURE
        assert audit_row.after["status"] == "rejected"


def test_clean_document_goes_on(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    document_id = _receive(factories, owner, store, keyring, CLEAN)
    with fake_clamd("signature") as clamd:
        status = _ingest(factories, owner, document_id, _deps(store, keyring, clamd.address))
    assert status in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW)
    _, _, stages, _ = _state(factories, owner, document_id)
    assert stages[0].stage is IngestStage.SCAN
    assert stages[0].log == {"antivirus": "clamav", "scanned": True, "verdict": "clean"}
    assert IngestStage.EXTRACT in [s.stage for s in stages]


def test_scanner_down_fails_closed_retry_then_failed(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    document_id = _receive(factories, owner, store, keyring, CLEAN)
    with fake_clamd("clean") as clamd:
        address = clamd.address
    deps = _deps(store, keyring, address)  # nothing listens any more
    with pytest.raises(pipeline.ScanUnavailableError):
        _ingest(factories, owner, document_id, deps, final_attempt=False)
    document, _, stages, _ = _state(factories, owner, document_id)
    assert document.status is DocumentStatus.RECEIVED  # rolled back for the retry
    assert stages == []
    status = _ingest(factories, owner, document_id, deps, final_attempt=True)
    assert status is DocumentStatus.FAILED
    document, blob, stages, items = _state(factories, owner, document_id)
    assert document.error == "antivirus scan failed: unavailable"
    assert blob.area is BlobArea.QUARANTINE
    assert [s.stage for s in stages] == [IngestStage.SCAN]
    assert [(i.kind.value, i.payload) for i in items] == [
        ("unparsed", {"reason": "scan_failed", "code": "unavailable"})
    ]


def test_too_large_to_scan_fails_without_retry(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    document_id = _receive(factories, owner, store, keyring, CLEAN)
    with fake_clamd("clean") as clamd:
        deps = pipeline.PipelineDeps(
            store=store,
            keyring=keyring,
            scanner=ClamdScanner(address=ClamdAddress.parse(clamd.address), max_bytes=10),
        )
        status = _ingest(factories, owner, document_id, deps, final_attempt=False)
    assert status is DocumentStatus.FAILED
    assert clamd.streams == []


def test_fail_open_goes_on_unscanned(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    document_id = _receive(factories, owner, store, keyring, CLEAN)
    with fake_clamd("error") as clamd:
        deps = _deps(store, keyring, clamd.address, antivirus_fail_mode="open")
        status = _ingest(factories, owner, document_id, deps, final_attempt=False)
    assert status in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW)
    _, _, stages, _ = _state(factories, owner, document_id)
    assert stages[0].log == {
        "antivirus": "clamav",
        "scanned": False,
        "scan_error": "scanner_error",
        "fail_mode": "open",
    }


def test_handler_passes_final_attempt(
    factories: Factories,
    store: LocalFSBlobStore,
    keyring: Keyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers.handlers import ingest
    from app.workers.registry import JobContext

    owner = factories.user(role="owner")
    assert owner.household_id is not None
    document_id = _receive(factories, owner, store, keyring, CLEAN)
    with fake_clamd("clean") as clamd:
        address = clamd.address
    monkeypatch.setattr(ingest, "default_deps", lambda: _deps(store, keyring, address))
    from app.domain.documents.schemas import IngestDocumentPayload

    for attempt, expect_raise in ((1, True), (3, False)):
        with factories.transaction() as db:
            set_context(db, owner.household_id)
            ctx = JobContext(
                job_id=uuid.uuid4(),
                job_type="ingest_document",
                attempt=attempt,
                max_attempts=3,
                household_id=owner.household_id,
                session=db,
                deadline=__import__("datetime").datetime.max,
            )
            payload = IngestDocumentPayload(document_id=document_id)
            if expect_raise:
                with pytest.raises(pipeline.ScanUnavailableError):
                    ingest.ingest_document(ctx, payload)
                db.rollback()
            else:
                ingest.ingest_document(ctx, payload)
    document, _, _, _ = _state(factories, owner, document_id)
    assert document.status is DocumentStatus.FAILED


def test_xlsx_statement_imports_like_csv(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    from tests.fixtures.imports import make_import_mapping
    from tests.fixtures.ledger import make_account

    owner = factories.user(role="owner")
    assert owner.household_id is not None
    rows: list[list[Cell]] = [
        ["Date", "Description", "Amount"],
        [date(2026, 2, 1), "Corner Grocer", Decimal("-12.34")],
        [date(2026, 2, 2), "Payroll Inc", Decimal("2500.00")],
        [date(2026, 2, 3), '=HYPERLINK("x")', Decimal("-1.00")],
    ]
    spec = {"date_column": 0, "date_format": "YYYY-MM-DD", "amount_column": 2, "payee_column": 1}
    with factories.transaction() as db:
        account = make_account(db, owner.household_id)
        make_import_mapping(
            db,
            owner.household_id,
            spec=spec,
            rows=[["Date", "Description", "Amount"], ["2026-02-01", "x", "1.00"]],
            account=account,
        )
        account_id = account.id
    document_id = _receive(factories, owner, store, keyring, workbook(rows))
    status = _ingest(factories, owner, document_id, _deps(store, keyring, None))
    document, blob, _, items = _state(factories, owner, document_id)
    assert [i.payload for i in items] == []
    assert status is DocumentStatus.PROCESSED
    assert document.mime_detected.endswith("spreadsheetml.sheet")
    assert document.extraction is not None
    assert document.extraction["kind"] == "xlsx"
    assert blob.area is BlobArea.PERMANENT
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        imported = list(
            db.execute(
                select(Transaction.posted_date, Transaction.amount, Transaction.raw_payee)
                .where(Transaction.account_id == account_id)
                .order_by(Transaction.posted_date)
            )
        )
    assert imported == [
        (date(2026, 2, 1), Decimal("-12.34"), "Corner Grocer"),
        (date(2026, 2, 2), Decimal("2500.00"), "Payroll Inc"),
        (date(2026, 2, 3), Decimal("-1.00"), None),
    ]
