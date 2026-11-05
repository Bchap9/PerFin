# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hostile corpus through the worker's ``ingest_document`` handler (C1, C2, C10).

Each accepted sample is received like an upload and its job (and the jobs of
any child documents) runs through the real handler with the real sandbox and
the worker's default limits. Every document must end in a terminal status
(the handler never raises), refused content stays in quarantine, and the same
worker process then ingests an ordinary CSV as usual.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.documents.models import (
    TERMINAL_STATUSES,
    Blob,
    BlobArea,
    Document,
    DocumentSource,
    DocumentStatus,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.ingestion.intake.upload import UploadRejectedError, receive_stream
from app.workers.handlers import ingest
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.imports import run_jobs
from tests.fixtures.statements import csv_bytes, synth_transactions
from tests.security.fuzz.corpus import SAMPLES, Sample

pytestmark = [pytest.mark.integration, pytest.mark.security]

ACCEPTED = [s for s in SAMPLES if s.expect != "rejected"]
MAX = 200 * 1024 * 1024


@pytest.fixture
def worker_settings(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Settings]:
    local = settings.model_copy(
        update={
            "documents_dir": tmp_path / "documents",
            "quarantine_dir": tmp_path / "quarantine",
            "upload_max_mb": MAX // (1024 * 1024),
        }
    )
    monkeypatch.setattr(ingest, "get_settings", lambda: local)
    ingest.default_deps.cache_clear()
    yield local
    ingest.default_deps.cache_clear()


def _receive(factories: Factories, owner: UserRecord, data: bytes) -> uuid.UUID:
    deps = ingest.default_deps()
    assert owner.household_id is not None
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        return receive_stream(
            db,
            store=deps.store,
            keyring=deps.keyring,
            household_id=owner.household_id,
            chunks=(data,),
            max_bytes=MAX,
            source=DocumentSource.UPLOAD,
            original_filename="fuzz.bin",
            uploaded_by=owner.user.id,
        ).document_id


def _drain(factories: Factories, household_id: uuid.UUID) -> int:
    done: set[uuid.UUID] = set()
    total = 0
    while ran := run_jobs(factories, household_id, INGEST_JOB_TYPE, done=done):
        total += len(ran)
        assert total < 500, "children must not multiply without bound"
    return total


def _documents(factories: Factories, household_id: uuid.UUID) -> list[tuple[Document, Blob]]:
    with factories.transaction() as db:
        set_context(db, household_id)
        rows = list(db.execute(select(Document, Blob).join(Blob, Blob.id == Document.blob_id)))
        db.expunge_all()
        return [(d, b) for d, b in rows]


@pytest.mark.parametrize("sample", ACCEPTED, ids=[s.name for s in ACCEPTED])
def test_worker_survives(sample: Sample, factories: Factories, worker_settings: Settings) -> None:
    owner = factories.user(role="owner")
    assert owner.household_id is not None
    try:
        hostile = _receive(factories, owner, sample.build())
    except UploadRejectedError:  # pragma: no cover - the corpus test pins intake outcomes
        pytest.fail(f"{sample.name} was refused at intake")
    assert _drain(factories, owner.household_id) >= 1
    documents = _documents(factories, owner.household_id)
    statuses = {d.id: d.status for d, _ in documents}
    assert statuses[hostile] in TERMINAL_STATUSES
    for document, blob in documents:
        assert document.status in TERMINAL_STATUSES, (sample.name, document.status)
        if document.status in (DocumentStatus.FAILED, DocumentStatus.REJECTED):
            assert blob.area is BlobArea.QUARANTINE
        # Children never nest deeper than one level (D4).
        assert int(document.source_meta.get("depth", 0)) <= 1
        assert document.extraction is None or "extractor_crashed" not in str(document.extraction)
    # The same process goes on with ordinary work.
    clean = _receive(factories, owner, csv_bytes(synth_transactions(3, seed=77)))
    _drain(factories, owner.household_id)
    after = {d.id: d.status for d, _ in _documents(factories, owner.household_id)}
    assert after[clean] in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW)
