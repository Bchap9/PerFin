# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF, image, EML and ZIP documents through the pipeline and the real sandbox.

Previews are encrypted blobs referenced from the stored extraction (never
image bytes in the database), children of mails and archives are received
like uploads with their depth (D4), password-protected PDFs and nested
containers wait in review, and deleting a document shreds its previews (G5).
"""

from __future__ import annotations

import io
import uuid
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import select

from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.documents import service
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Blob,
    BlobArea,
    Document,
    DocumentSource,
    DocumentStatus,
    ReviewItem,
)
from app.domain.documents.previews import read_preview, stored_previews
from app.ingestion import pipeline
from app.ingestion.intake.upload import receive_stream
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from app.ingestion.sandbox.runner import SandboxConfig
from tests.fixtures import synth_documents as synth
from tests.fixtures.factories import Factories, UserRecord

pytestmark = pytest.mark.integration

MAX = 20 * 1024 * 1024


@pytest.fixture
def store(tmp_path: Path) -> LocalFSBlobStore:
    return LocalFSBlobStore(tmp_path / "documents", tmp_path / "quarantine")


@pytest.fixture
def keyring(settings: Settings) -> Keyring:
    return Keyring.from_settings(settings)


def _deps(store: LocalFSBlobStore, keyring: Keyring) -> pipeline.PipelineDeps:
    return pipeline.PipelineDeps(
        store=store,
        keyring=keyring,
        sandbox=SandboxConfig(timeout_seconds=90),
        options=ExtractOptions(preview_dpi=72),
        child_max_bytes=MAX,
    )


def _receive(
    factories: Factories,
    owner: UserRecord,
    store: LocalFSBlobStore,
    keyring: Keyring,
    data: bytes,
    name: str,
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
            original_filename=name,
            uploaded_by=owner.user.id,
        ).document_id


def _ingest(
    factories: Factories, owner: UserRecord, document_id: uuid.UUID, deps: pipeline.PipelineDeps
) -> DocumentStatus | None:
    assert owner.household_id is not None
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        return pipeline.ingest_document(db, owner.household_id, document_id, deps)


def _documents(factories: Factories, owner: UserRecord) -> list[Document]:
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        rows = list(db.execute(select(Document).order_by(Document.created_at)).scalars())
        db.expunge_all()
        return rows


def _document(factories: Factories, owner: UserRecord, document_id: uuid.UUID) -> Document:
    return next(d for d in _documents(factories, owner) if d.id == document_id)


def _items(factories: Factories, owner: UserRecord, document_id: uuid.UUID) -> list[ReviewItem]:
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        items = list(
            db.execute(select(ReviewItem).where(ReviewItem.document_id == document_id)).scalars()
        )
        db.expunge_all()
        return items


def test_pdf_pages_and_encrypted_previews(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring, tmp_path: Path
) -> None:
    owner = factories.user(role="owner")
    pdf = synth.statement_pdf(pages=2)
    document_id = _receive(factories, owner, store, keyring, pdf, "statement.pdf")
    status = _ingest(factories, owner, document_id, _deps(store, keyring))
    # No statement parser for PDFs yet: the extracted pages wait in review.
    assert status is DocumentStatus.NEEDS_REVIEW
    document = _document(factories, owner, document_id)
    assert document.extraction is not None
    stored = ExtractResult.model_validate(document.extraction)
    assert [p.number for p in stored.pages] == [1, 2]
    assert "Statement period" in stored.pages[0].text
    assert stored.previews == []  # no image bytes in the database
    refs = stored_previews(document.extraction)
    assert [r.page for r in refs] == [1, 2]
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        found = read_preview(db, store, keyring, document, 1)
        assert found is not None
        ref, png = found
        image = Image.open(io.BytesIO(png))
        assert image.size == (ref.width, ref.height)
        blob = db.get(Blob, ref.blob_id)
        assert blob is not None
        assert blob.area is BlobArea.PERMANENT
        on_disk = store.path(BlobArea.PERMANENT, blob.key).read_bytes()
        assert b"IHDR" not in on_disk  # ciphertext only
        assert read_preview(db, store, keyring, document, 9) is None
    # Deleting the document shreds and removes the previews too.
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        service.delete_document(
            db, store, household_id=document.household_id, document_id=document_id
        )
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        assert db.get(Blob, refs[0].blob_id) is None
    assert [p for p in (tmp_path / "documents").rglob("*") if p.is_file()] == []


def test_encrypted_pdf_waits_in_review(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    document_id = _receive(factories, owner, store, keyring, synth.encrypted_pdf(), "x.pdf")
    status = _ingest(factories, owner, document_id, _deps(store, keyring))
    assert status is DocumentStatus.NEEDS_REVIEW
    (item,) = _items(factories, owner, document_id)
    assert item.payload == {"reason": "encrypted", "kind": "pdf"}


def test_malformed_pdf_fails(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    data = b"%PDF-1.4\n" + b"\x00\x01broken" * 200
    document_id = _receive(factories, owner, store, keyring, data, "x.pdf")
    assert _ingest(factories, owner, document_id, _deps(store, keyring)) is DocumentStatus.FAILED
    (item,) = _items(factories, owner, document_id)
    assert item.payload == {"reason": "extraction_failed", "code": "malformed_pdf"}


def test_mail_attachments_become_documents_one_level_deep(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    logo = synth.image_bytes(Image.new("RGB", (16, 16), "blue"), "PNG")
    inner_zip = synth.zip_bytes([("deep.csv", b"Date,Amount\n2026-01-01,1.00\n")])
    mail = synth.receipt_email(
        attachments=[
            ("receipt.pdf", "application/pdf", synth.statement_pdf()),
            ("tracker.html", "text/html", b"<html><script>x()</script></html>"),
            ("nested.zip", "application/zip", inner_zip),
        ],
        inline_image=logo,
    )
    mail_id = _receive(factories, owner, store, keyring, mail, "receipt.eml")
    deps = _deps(store, keyring)
    status = _ingest(factories, owner, mail_id, deps)
    # The HTML attachment is refused by the intake (C5), so the mail needs a look.
    assert status is DocumentStatus.NEEDS_REVIEW
    mail_doc = _document(factories, owner, mail_id)
    stored = ExtractResult.model_validate(mail_doc.extraction)
    assert stored.children == []
    assert stored.email is not None
    assert "tracker.invalid" not in stored.email.body_text
    rejected = [c for c in stored.stored_children if c.rejected]
    assert [(c.name, c.rejected) for c in rejected] == [("tracker.html", "markup")]
    children = {d.original_filename: d for d in _documents(factories, owner) if d.id != mail_id}
    assert set(children) == {"receipt.pdf", "nested.zip", None}  # the cid image has no name
    for child in children.values():
        assert child.source_meta["parent_document_id"] == str(mail_id)
        assert child.source_meta["depth"] == 1
        assert child.status is DocumentStatus.RECEIVED
    inline = children[None]
    assert inline.source_meta["origin"] == "inline"
    assert inline.source_meta["content_id"] == "logo@example.invalid"
    assert inline.mime_detected == "image/png"
    # The nested archive is not expanded: depth limit 1 (D4).
    zip_doc = children["nested.zip"]
    assert _ingest(factories, owner, zip_doc.id, deps) is DocumentStatus.NEEDS_REVIEW
    (item,) = _items(factories, owner, zip_doc.id)
    assert item.payload == {"reason": "depth_limit", "kind": "zip"}
    assert len(_documents(factories, owner)) == 4


def test_zip_upload_expands_once_and_is_processed(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    archive = synth.zip_bytes(
        [("a/jan.csv", b"Date,Amount\n2026-01-01,1.00\n"), ("../feb.csv", b"Date,Amount\n")]
    )
    zip_id = _receive(factories, owner, store, keyring, archive, "statements.zip")
    assert _ingest(factories, owner, zip_id, _deps(store, keyring)) is DocumentStatus.PROCESSED
    names = sorted(
        d.original_filename or "" for d in _documents(factories, owner) if d.id != zip_id
    )
    assert names == ["feb.csv", "jan.csv"]


def test_zip_bomb_upload_fails(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    zip_id = _receive(
        factories, owner, store, keyring, synth.zip_bomb(20 * 1024 * 1024), "bomb.zip"
    )
    assert _ingest(factories, owner, zip_id, _deps(store, keyring)) is DocumentStatus.FAILED
    (item,) = _items(factories, owner, zip_id)
    assert item.payload == {"reason": "extraction_failed", "code": "zip_bomb"}
    assert len(_documents(factories, owner)) == 1


def test_scanned_image_is_ocrd(
    factories: Factories, store: LocalFSBlobStore, keyring: Keyring
) -> None:
    owner = factories.user(role="owner")
    png = synth.image_bytes(synth.receipt_image(), "PNG")
    document_id = _receive(factories, owner, store, keyring, png, "receipt.png")
    _ingest(factories, owner, document_id, _deps(store, keyring))
    document = _document(factories, owner, document_id)
    stored = ExtractResult.model_validate(document.extraction)
    assert stored.pages[0].text_source == "ocr"
    assert "11.43" in stored.text
    assert len(stored_previews(document.extraction)) == 1
