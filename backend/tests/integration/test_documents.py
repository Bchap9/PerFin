# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document store, upload intake and the Phase 2 pipeline on a real database.

Covers SECURITY.md C1 (the API never parses: sniff + hash + encrypt only),
C3 (random keys, names as metadata), C4/C5 (rejections), G1 (encrypted at
rest, per-object key), G5 (crypto-shredding on delete) and F3 (cross-household
and account-level visibility).
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel, HouseholdRole
from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import DecryptionError, Keyring
from app.core.errors import ProblemError
from app.core.settings import Settings
from app.db.models import AuditLog, Event, Job
from app.db.rls import set_context
from app.domain.documents import repo, service
from app.domain.documents.blobstore import LocalFSBlobStore, decrypt_stream
from app.domain.documents.models import (
    Blob,
    BlobArea,
    Document,
    DocumentKind,
    DocumentSource,
    DocumentStatus,
    IngestJob,
    IngestStage,
    ReviewItem,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE, IngestDocumentPayload
from app.domain.transactions.models import Transaction
from app.ingestion import pipeline
from app.ingestion.intake.sniff import RejectReason
from app.ingestion.intake.upload import (
    UploadRejectedError,
    UploadTooLargeError,
    receive_stream,
    receive_upload,
)
from app.ingestion.sandbox.runner import SandboxError
from app.workers.registry import REGISTRY, JobContext
from tests.fixtures.factories import (
    Factories,
    UserRecord,
    add_member,
    grant_account,
    make_household,
    make_user,
)
from tests.fixtures.ledger import make_account, make_transaction
from tests.fixtures.statements import (
    XXE_PROLOG,
    account_number,
    csv_bytes,
    ofx_sgml,
    ofx_xml,
    synth_transactions,
)

pytestmark = pytest.mark.integration

MAX = 5 * 1024 * 1024
TXNS = synth_transactions(5, seed=21)


@pytest.fixture
def store(tmp_path: Path) -> LocalFSBlobStore:
    return LocalFSBlobStore(tmp_path / "documents", tmp_path / "quarantine")


@pytest.fixture
def keyring(settings: Settings) -> Keyring:
    return Keyring.from_settings(settings)


def _principal(record: UserRecord) -> Principal:
    return Principal(
        user=record.user,
        household_id=record.household_id,
        role=record.role,
        auth_method="session",
    )


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def _chunks(data: bytes, size: int = 4096) -> Iterator[bytes]:
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


@contextmanager
def _tx(factories: Factories, principal: Principal) -> Iterator[Session]:
    with factories.transaction() as db:
        apply_rls_context(db, principal)
        yield db


def _upload(
    db: Session,
    principal: Principal,
    store: LocalFSBlobStore,
    keyring: Keyring,
    data: bytes,
    **kwargs: object,
) -> service.ReceivedDocument:
    return receive_upload(
        db,
        principal,
        store=store,
        keyring=keyring,
        chunks=_chunks(data),
        max_bytes=MAX,
        filename="../../statement.csv",
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Upload intake
# ---------------------------------------------------------------------------


def test_upload_stores_encrypted_blob_document_and_job(
    db: Session, store: LocalFSBlobStore, keyring: Keyring
) -> None:

    owner = make_user(db, role="owner")
    principal = _principal(owner)
    apply_rls_context(db, principal)
    data = csv_bytes(TXNS)
    received = _upload(db, principal, store, keyring, data)

    assert received.status is DocumentStatus.RECEIVED
    assert received.job_id is not None
    document = db.get(Document, received.document_id)
    assert document is not None
    assert document.mime_detected == "text/csv"
    assert document.sha256 == hashlib.sha256(data).hexdigest()
    assert document.size == len(data)
    assert document.original_filename == "statement.csv"  # metadata only, no path
    assert document.uploaded_by == owner.id
    blob = db.get(Blob, document.blob_id)
    assert blob is not None
    assert blob.area is BlobArea.QUARANTINE
    assert blob.wrapped_dek is not None
    assert blob.key_version == 1
    assert blob.key != "statement.csv"
    uuid.UUID(blob.key)
    # On disk: ciphertext only, under the random key, in quarantine.
    (path,) = _files(store.path(BlobArea.QUARANTINE, blob.key).parents[2])
    assert path.name == blob.key
    assert TXNS[0].payee.encode() not in path.read_bytes()
    assert _files(store.path(BlobArea.PERMANENT, blob.key).parents[2]) == []
    assert b"".join(service.open_plaintext(store, keyring, blob)) == data

    job = db.execute(select(Job).where(Job.id == received.job_id)).scalar_one()
    assert job.type == INGEST_JOB_TYPE
    assert job.household_id == owner.household_id
    assert job.payload == {"document_id": str(document.id)}
    audit_row = db.execute(
        select(AuditLog).where(AuditLog.target_id == str(document.id))
    ).scalar_one()
    assert audit_row.action == "document.receive"
    assert "statement" not in str(audit_row.after)  # no file name in the trail
    event = db.execute(select(Event).where(Event.type == "document.created")).scalar_one()
    assert event.payload == {"id": str(document.id), "status": "received"}


def test_same_content_twice_is_a_possible_duplicate(
    db: Session, store: LocalFSBlobStore, keyring: Keyring
) -> None:

    owner = make_user(db, role="owner")
    principal = _principal(owner)
    apply_rls_context(db, principal)
    first = _upload(db, principal, store, keyring, csv_bytes(TXNS))
    second = _upload(db, principal, store, keyring, csv_bytes(TXNS))
    assert second.status is DocumentStatus.NEEDS_REVIEW
    assert second.duplicate_of == first.document_id
    assert second.job_id is None
    item = db.execute(
        select(ReviewItem).where(ReviewItem.document_id == second.document_id)
    ).scalar_one()
    assert item.kind is ReviewKind.POSSIBLE_DUPLICATE
    assert item.payload["duplicate_of"] == str(first.document_id)


@pytest.mark.parametrize(
    "data",
    [
        b'<svg xmlns="http://www.w3.org/2000/svg" onload="alert(1)"/>',
        b"<!DOCTYPE html><html><script>alert(1)</script></html>",
        b"%PDF-1.4\n<html><script>alert(1)</script></html>\n%%EOF",
        b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 100,
        b"",
    ],
    ids=["svg", "html", "polyglot-pdf", "elf", "empty"],
)
def test_rejected_uploads_store_nothing(
    db: Session, store: LocalFSBlobStore, keyring: Keyring, tmp_path: Path, data: bytes
) -> None:

    owner = make_user(db, role="owner")
    principal = _principal(owner)
    apply_rls_context(db, principal)
    with pytest.raises(UploadRejectedError) as caught:
        _upload(db, principal, store, keyring, data)
    assert caught.value.status == 415
    assert caught.value.slug == "unsupported-file-type"
    assert _files(tmp_path / "quarantine") == []
    assert db.execute(select(Document)).scalars().all() == []


@pytest.mark.parametrize(
    "tail", [b"<script>alert(1)</script>", b"javascript:alert(1)", b"<svg onload=x>"]
)
def test_text_polyglot_past_the_sniff_window_is_refused(
    db: Session, store: LocalFSBlobStore, keyring: Keyring, tmp_path: Path, tail: bytes
) -> None:
    # Review finding P2-SR-04: active markup after the first 8 KiB of a text
    # file, split across a chunk boundary (_chunks uses 4096-byte chunks).
    owner = make_user(db, role="owner")
    principal = _principal(owner)
    apply_rls_context(db, principal)
    head = csv_bytes(synth_transactions(1000))
    assert len(head) > 3 * 8192
    pad = b"x" * (4096 - len(head) % 4096 - 3)
    data = head + pad + tail + b"\n"
    with pytest.raises(UploadRejectedError) as caught:
        _upload(db, principal, store, keyring, data)
    assert (caught.value.status, caught.value.reason) == (415, RejectReason.POLYGLOT)
    assert _files(tmp_path / "quarantine") == []
    assert db.execute(select(Document)).scalars().all() == []


def test_binary_types_are_checked_in_the_sniff_window_only(
    db: Session, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    # Compressed bytes in a large PDF may spell a short tag by chance; the
    # download headers (attachment, nosniff, sandbox CSP) are the C4 control.
    owner = make_user(db, role="owner")
    principal = _principal(owner)
    apply_rls_context(db, principal)
    data = b"%PDF-1.7\n" + b"0" * 20_000 + b"<svg>" + b"\n%%EOF\n"
    received = _upload(db, principal, store, keyring, data)
    assert received.document_id is not None


def test_oversized_upload_is_refused_mid_stream(
    db: Session, store: LocalFSBlobStore, keyring: Keyring, tmp_path: Path
) -> None:

    owner = make_user(db, role="owner")
    principal = _principal(owner)
    apply_rls_context(db, principal)
    data = csv_bytes(synth_transactions(5000))
    assert len(data) > 100_000
    with pytest.raises(UploadTooLargeError) as caught:
        receive_upload(
            db, principal, store=store, keyring=keyring, chunks=_chunks(data), max_bytes=100_000
        )
    assert caught.value.status == 413
    assert _files(tmp_path / "quarantine") == []
    assert db.execute(select(Document)).scalars().all() == []


def test_failed_insert_removes_the_blob(
    db: Session, store: LocalFSBlobStore, keyring: Keyring, tmp_path: Path
) -> None:

    owner = make_user(db, role="owner")
    other = make_household(db)
    set_context(db, owner.household_id, owner.id)
    # The RLS check refuses the row for another household.
    with pytest.raises(DBAPIError), db.begin_nested():
        receive_stream(
            db,
            store=store,
            keyring=keyring,
            household_id=other.id,  # not the context's household
            chunks=[csv_bytes(TXNS)],
            max_bytes=MAX,
            source=DocumentSource.UPLOAD,
        )
    assert _files(tmp_path / "quarantine") == []


def test_upload_to_an_account_needs_edit_rights(
    db: Session, store: LocalFSBlobStore, keyring: Keyring
) -> None:

    owner = make_user(db, role="owner")
    assert owner.household_id is not None
    member = make_user(db, role=None)
    add_member(db, owner.household_id, member.id, HouseholdRole.MEMBER)
    member = UserRecord(member.user, owner.household, HouseholdRole.MEMBER, member.password)
    viewed = make_account(db, owner.household_id)
    edited = make_account(db, owner.household_id)
    hidden = make_account(db, owner.household_id)
    grant_account(db, owner.household_id, member.id, viewed.id, GrantLevel.VIEW)
    grant_account(db, owner.household_id, member.id, edited.id, GrantLevel.EDIT)
    foreign = make_account(db, make_household(db).id)
    principal = _principal(member)
    apply_rls_context(db, principal)
    for account in (viewed, hidden, foreign):
        with pytest.raises(ProblemError) as caught:
            _upload(db, principal, store, keyring, csv_bytes(TXNS), account_id=account.id)
        assert caught.value.status == 404
        assert caught.value.slug == "account-not-found"
    received = _upload(db, principal, store, keyring, csv_bytes(TXNS), account_id=edited.id)
    document = db.get(Document, received.document_id)
    assert document is not None
    assert document.account_id == edited.id


# ---------------------------------------------------------------------------
# Visibility (F3)
# ---------------------------------------------------------------------------


def test_document_visibility(db: Session, store: LocalFSBlobStore, keyring: Keyring) -> None:

    owner = make_user(db, role="owner")
    assert owner.household_id is not None
    records = {}
    for name in ("alice", "bob"):
        record = make_user(db, role=None)
        add_member(db, owner.household_id, record.id, HouseholdRole.MEMBER)
        records[name] = UserRecord(record.user, owner.household, HouseholdRole.MEMBER, "x")
    account = make_account(db, owner.household_id)
    grant_account(db, owner.household_id, records["bob"].id, account.id, GrantLevel.VIEW)

    alice = _principal(records["alice"])
    apply_rls_context(db, alice)
    alices = _upload(db, alice, store, keyring, csv_bytes(TXNS)).document_id
    owner_p = _principal(owner)
    apply_rls_context(db, owner_p)
    on_account = _upload(
        db, owner_p, store, keyring, csv_bytes(TXNS[:2]), account_id=account.id
    ).document_id

    assert service.get_visible_document(db, owner_p, alices).id == alices
    bob = _principal(records["bob"])
    apply_rls_context(db, bob)
    assert service.get_visible_document(db, bob, on_account).id == on_account
    with pytest.raises(ProblemError) as caught:
        service.get_visible_document(db, bob, alices)
    assert caught.value.status == 404
    assert caught.value.slug == "document-not-found"
    apply_rls_context(db, alice)
    with pytest.raises(ProblemError):
        service.get_visible_document(db, alice, on_account)


def test_other_households_never_see_documents(
    db: Session, store: LocalFSBlobStore, keyring: Keyring
) -> None:

    owner = make_user(db, role="owner")
    stranger = make_user(db, role="owner")
    apply_rls_context(db, _principal(owner))
    received = _upload(db, _principal(owner), store, keyring, csv_bytes(TXNS))
    outsider = _principal(stranger)
    apply_rls_context(db, outsider)
    with pytest.raises(ProblemError) as caught:
        service.get_visible_document(db, outsider, received.document_id)
    assert caught.value.status == 404
    # RLS: the rows are not even readable in the other household's context.
    for model in (Document, Blob, ReviewItem, IngestJob):
        assert db.execute(select(model)).scalars().all() == []
    assert stranger.household_id is not None
    with pytest.raises(ProblemError):
        service.delete_document(
            db, store, household_id=stranger.household_id, document_id=received.document_id
        )


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def _deps(store: LocalFSBlobStore, keyring: Keyring, **kwargs: object) -> pipeline.PipelineDeps:
    return pipeline.PipelineDeps(store=store, keyring=keyring, **kwargs)  # type: ignore[arg-type]


def _receive_committed(
    factories: Factories, owner: UserRecord, store: LocalFSBlobStore, keyring: Keyring, data: bytes
) -> uuid.UUID:
    with _tx(factories, _principal(owner)) as db:
        return _upload(db, _principal(owner), store, keyring, data).document_id


def _ingest(
    factories: Factories, owner: UserRecord, document_id: uuid.UUID, deps: pipeline.PipelineDeps
) -> DocumentStatus | None:
    assert owner.household_id is not None
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        return pipeline.ingest_document(db, owner.household_id, document_id, deps)


def _load(factories: Factories, owner: UserRecord, document_id: uuid.UUID) -> Document:
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        document = db.get(Document, document_id)
        assert document is not None
        db.expunge(document)
        return document


def _review_items(
    factories: Factories, owner: UserRecord, document_id: uuid.UUID
) -> list[ReviewItem]:
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        items = list(
            db.execute(select(ReviewItem).where(ReviewItem.document_id == document_id)).scalars()
        )
        db.expunge_all()
        return items


def test_pipeline_extracts_csv_in_the_sandbox_and_promotes_the_blob(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring, tmp_path: Path
) -> None:
    owner = factories.user(role="owner")
    document_id = _receive_committed(factories, owner, store, keyring, csv_bytes(TXNS))
    status = _ingest(factories, owner, document_id, _deps(store, keyring))
    assert status is DocumentStatus.NEEDS_REVIEW  # no structured importer installed
    document = _load(factories, owner, document_id)
    assert document.status is DocumentStatus.NEEDS_REVIEW
    assert document.kind is DocumentKind.STATEMENT
    assert document.parser_version == "delimited/1"
    assert document.extraction is not None
    rows = document.extraction["tables"][0]["rows"]
    assert rows[1][2] == TXNS[0].payee
    (item,) = _review_items(factories, owner, document_id)
    assert item.kind is ReviewKind.UNPARSED
    assert item.payload["reason"] == "import_pending"
    # Promoted: only in the permanent area once committed.
    assert _files(tmp_path / "quarantine") == []
    assert len(_files(tmp_path / "documents")) == 1
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        stages = [
            row.stage
            for row in db.execute(
                select(IngestJob)
                .where(IngestJob.document_id == document_id)
                .order_by(IngestJob.started_at, IngestJob.created_at)
            ).scalars()
        ]
        blob = db.execute(select(Blob)).scalar_one()
        assert blob.area is BlobArea.PERMANENT
        assert b"".join(service.open_plaintext(store, keyring, blob)) == csv_bytes(TXNS)
        events = [e.type for e in db.execute(select(Event)).scalars()]
    assert stages[:3] == [IngestStage.SCAN, IngestStage.EXTRACT, IngestStage.CLASSIFY]
    assert IngestStage.COMMIT in stages
    assert "document.updated" in events
    # Idempotent: a second run leaves the document alone.
    assert _ingest(factories, owner, document_id, _deps(store, keyring)) is (
        DocumentStatus.NEEDS_REVIEW
    )


def test_pipeline_matches_the_account_by_last4_and_runs_the_importer(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring, ledger: object
) -> None:
    owner = factories.user(role="owner")
    assert owner.household_id is not None
    acct = account_number(9)
    account = ledger.account(owner.household_id, number_last4=acct[-4:])  # type: ignore[attr-defined]
    document_id = _receive_committed(factories, owner, store, keyring, ofx_sgml(TXNS, acct=acct))
    seen: list[int] = []

    def importer(db: Session, document: Document, result: object) -> pipeline.ImportOutcome:
        seen.append(len(result.statements[0].transactions))  # type: ignore[attr-defined]
        return pipeline.ImportOutcome(DocumentStatus.PROCESSED, {"imported": 5})

    status = _ingest(factories, owner, document_id, _deps(store, keyring, importer=importer))
    assert status is DocumentStatus.PROCESSED
    assert seen == [len(TXNS)]
    document = _load(factories, owner, document_id)
    assert document.account_id == account.id
    assert document.period_start == TXNS[0].posted
    assert document.period_end == TXNS[-1].posted
    assert document.extraction is not None
    assert acct not in str(document.extraction)  # only the last four digits


def test_pipeline_records_refused_xml_as_failed(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring, tmp_path: Path
) -> None:
    owner = factories.user(role="owner")
    data = ofx_xml(TXNS, prolog=XXE_PROLOG)
    document_id = _receive_committed(factories, owner, store, keyring, data)
    status = _ingest(factories, owner, document_id, _deps(store, keyring))
    assert status is DocumentStatus.FAILED
    document = _load(factories, owner, document_id)
    assert document.error == "extraction failed: xml_forbidden"
    (item,) = _review_items(factories, owner, document_id)
    assert item.payload == {"reason": "extraction_failed", "code": "xml_forbidden"}
    assert len(_files(tmp_path / "quarantine")) == 1  # never promoted
    assert _files(tmp_path / "documents") == []


def test_pipeline_records_sandbox_failures(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    document_id = _receive_committed(factories, owner, store, keyring, csv_bytes(TXNS))

    def killed(kind: str, data: object, **_: object) -> object:
        raise SandboxError("timeout", stderr="synthetic")

    status = _ingest(factories, owner, document_id, _deps(store, keyring, extract=killed))
    assert status is DocumentStatus.FAILED
    (item,) = _review_items(factories, owner, document_id)
    assert item.payload["code"] == "timeout"


def test_pipeline_parks_unsupported_types_for_review(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring, tmp_path: Path
) -> None:
    owner = factories.user(role="owner")
    # Plain text has no extractor (every other accepted type has one).
    data = b"Notes about nothing in particular\nand a second line\n"
    document_id = _receive_committed(factories, owner, store, keyring, data)
    status = _ingest(factories, owner, document_id, _deps(store, keyring))
    assert status is DocumentStatus.NEEDS_REVIEW
    (item,) = _review_items(factories, owner, document_id)
    assert item.payload == {"reason": "format_not_supported", "mime": "text/plain"}
    assert len(_files(tmp_path / "documents")) == 1


def test_ingest_handler_is_registered_and_runs(
    factories: Factories,
    store: LocalFSBlobStore,
    keyring: Keyring,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.workers.handlers import ingest

    spec = REGISTRY.get(INGEST_JOB_TYPE)
    assert spec is not None
    assert spec.payload_model is IngestDocumentPayload
    owner = factories.user(role="owner")
    document_id = _receive_committed(factories, owner, store, keyring, csv_bytes(TXNS))
    monkeypatch.setattr(ingest, "default_deps", lambda: _deps(store, keyring))
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        ctx = JobContext(
            job_id=uuid.uuid4(),
            job_type=INGEST_JOB_TYPE,
            attempt=1,
            max_attempts=3,
            household_id=owner.household_id,
            session=db,
            deadline=date.max,  # type: ignore[arg-type]
        )
        ingest.ingest_document(ctx, IngestDocumentPayload(document_id=document_id))
    assert _load(factories, owner, document_id).status is DocumentStatus.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# Deletion: crypto-shredding (G5)
# ---------------------------------------------------------------------------


def test_delete_destroys_the_key_rows_and_files(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring, tmp_path: Path
) -> None:
    owner = factories.user(role="owner")
    assert owner.household_id is not None
    document_id = _receive_committed(factories, owner, store, keyring, csv_bytes(TXNS))
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        document = db.get(Document, document_id)
        assert document is not None
        blob = db.get(Blob, document.blob_id)
        assert blob is not None
        blob_id, key, header, size = blob.id, blob.key, blob.nonce, blob.size
        account = make_account(db, owner.household_id)
        txn = make_transaction(db, account, amount=Decimal("-1.00"))
        txn.source_document_id = document_id
        db.flush()
        txn_id = txn.id
    (path,) = _files(tmp_path / "quarantine")
    stolen = path.read_bytes()  # e.g. left in a snapshot

    with _tx(factories, _principal(owner)) as db:
        service.delete_document(
            db, store, household_id=owner.household_id, document_id=document_id, user_id=owner.id
        )
        # Inside the transaction the key is already gone.
        assert db.execute(select(Blob).where(Blob.id == blob_id)).scalar_one_or_none() is None
        assert _files(tmp_path / "quarantine") != []  # files go only after commit

    assert _files(tmp_path / "quarantine") == []
    assert _files(tmp_path / "documents") == []
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        assert db.get(Document, document_id) is None
        assert db.get(Blob, blob_id) is None
        transaction = db.get(Transaction, txn_id)
        assert transaction is not None
        assert transaction.source_document_id is None
        actions = [a.action for a in db.execute(select(AuditLog)).scalars()]
        assert "document.delete" in actions
    # The ciphertext that survived elsewhere cannot be decrypted: no key exists.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(stolen)
    with pytest.raises(DecryptionError):
        b"".join(
            decrypt_stream(
                store,
                keyring,
                blob_id=blob_id,
                area=BlobArea.QUARANTINE,
                key=key,
                header=header,
                wrapped=None,
                size=size,
            )
        )


def test_rolled_back_delete_keeps_the_files(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring, tmp_path: Path
) -> None:
    owner = factories.user(role="owner")
    assert owner.household_id is not None
    document_id = _receive_committed(factories, owner, store, keyring, csv_bytes(TXNS))
    with factories.session_factory() as db:
        db.begin()
        set_context(db, owner.household_id)
        service.delete_document(
            db,
            store,
            household_id=owner.household_id,
            document_id=document_id,
        )
        db.rollback()
    assert len(_files(tmp_path / "quarantine")) == 1
    assert _load(factories, owner, document_id).status is DocumentStatus.RECEIVED


def test_shred_nulls_the_wrapped_key(
    db: Session, store: LocalFSBlobStore, keyring: Keyring
) -> None:

    owner = make_user(db, role="owner")
    apply_rls_context(db, _principal(owner))
    received = _upload(db, _principal(owner), store, keyring, csv_bytes(TXNS))
    document = db.get(Document, received.document_id)
    assert document is not None
    assert owner.household_id is not None
    repo.shred_blob_key(db, owner.household_id, document.blob_id)
    blob = db.execute(select(Blob).where(Blob.id == document.blob_id)).scalar_one()
    db.refresh(blob)
    assert blob.wrapped_dek is None
    assert blob.wrap_nonce is None
    assert blob.shredded_at is not None
    with pytest.raises(DecryptionError):
        b"".join(service.open_plaintext(store, keyring, blob))
