# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flush-only factories for documents with a stored extraction and import mappings.

``make_extracted_document`` inserts a ``blob`` row (fake key material: no
file exists in any store) and a ``document`` whose ``extraction`` is a
validated sandbox result, as if the worker had extracted it. Use it where
only the stored output matters (preview, mappings, re-runs, the authz
matrix); end-to-end tests go through the real upload, sandbox and worker.
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Job, JobState
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.documents.models import (
    Blob,
    BlobArea,
    Document,
    DocumentKind,
    DocumentSource,
    DocumentStatus,
)
from app.domain.imports.models import ImportMapping
from app.domain.imports.schemas import MappingSpec
from app.ingestion.parsers.structured import spec_fingerprint
from app.ingestion.sandbox.protocol import ExtractResult, Table
from app.workers.registry import REGISTRY, JobContext
from tests.fixtures.factories import Factories
from tests.fixtures.ledger import household_context

#: A small synthetic CSV table (header plus three rows).
SAMPLE_ROWS: list[list[str]] = [
    ["Date", "Description", "Amount", "Balance"],
    ["01/05/2026", "Blue Owl Grocers", "-42.10", "957.90"],
    ["01/06/2026", "Initech Payroll", "1500.00", "2457.90"],
    ["01/07/2026", "Corner Fuel", "-30.00", "2427.90"],
]
SAMPLE_SPEC: dict[str, Any] = {
    "date_column": 0,
    "date_format": "MM/DD/YYYY",
    "payee_column": 1,
    "amount_column": 2,
    "balance_column": 3,
}


def table_result(rows: list[list[str]] | None = None, *, kind: str = "csv") -> ExtractResult:
    rows = rows if rows is not None else SAMPLE_ROWS
    return ExtractResult(
        kind=kind,
        ok=True,
        extractor_version="delimited/1",
        tables=[
            Table(
                encoding="utf-8",
                delimiter="," if kind == "csv" else "\t",
                has_header=True,
                rows=rows,
                column_count=max((len(r) for r in rows), default=0),
            )
        ],
    )


def make_extracted_document(
    db: Session,
    household_id: uuid.UUID,
    *,
    account: Account | None = None,
    result: ExtractResult | None = None,
    status: DocumentStatus = DocumentStatus.NEEDS_REVIEW,
    uploaded_by: uuid.UUID | None = None,
) -> Document:
    """A document (and its blob row) carrying ``result`` as its stored extraction."""
    result = result if result is not None else table_result()
    with household_context(db, household_id):
        blob = Blob(
            household_id=household_id,
            store="local",
            key=uuid.uuid4().hex,
            area=BlobArea.PERMANENT,
            size=0,
            stored_size=0,
            enc_alg="test-only",
            key_version=1,
            wrap_nonce=secrets.token_bytes(24),
            wrapped_dek=secrets.token_bytes(48),
            nonce=secrets.token_bytes(24),
        )
        db.add(blob)
        db.flush()
        document = Document(
            household_id=household_id,
            blob_id=blob.id,
            kind=DocumentKind.STATEMENT,
            status=status,
            source=DocumentSource.UPLOAD,
            sha256=hashlib.sha256(secrets.token_bytes(16)).hexdigest(),
            size=0,
            mime_detected="text/csv" if result.tables else "application/x-ofx",
            uploaded_by=uploaded_by,
            account_id=account.id if account is not None else None,
            extraction=result.model_dump(mode="json"),
            parser_version=result.extractor_version,
        )
        db.add(document)
        db.flush()
        return document


def make_import_mapping(
    db: Session,
    household_id: uuid.UUID,
    *,
    name: str | None = None,
    spec: dict[str, Any] | None = None,
    rows: list[list[str]] | None = None,
    account: Account | None = None,
) -> ImportMapping:
    """A saved mapping whose fingerprint is that of ``rows`` (default the sample table)."""
    parsed = MappingSpec.model_validate(spec if spec is not None else SAMPLE_SPEC)
    table = table_result(rows).tables[0]
    fingerprint = spec_fingerprint(table, parsed)
    assert fingerprint is not None
    with household_context(db, household_id):
        mapping = ImportMapping(
            household_id=household_id,
            name=name or f"Mapping {uuid.uuid4().hex[:8]}",
            account_id=account.id if account is not None else None,
            header_fingerprint=fingerprint,
            spec=parsed.model_dump(mode="json"),
        )
        db.add(mapping)
        db.flush()
        return mapping


def run_jobs(
    factories: Factories, household_id: uuid.UUID, job_type: str, *, done: set[uuid.UUID]
) -> list[uuid.UUID]:
    """Run the queued ``job_type`` jobs of the household through their real handlers.

    Each job runs in its own committed transaction in the household's RLS
    context, as the worker would; ``done`` remembers which ran (the rows stay
    ``queued``: the test does not own the queue's state machine).
    """
    import app.workers.handlers.imports
    import app.workers.handlers.ingest
    import app.workers.handlers.transfers  # noqa: F401 - registers the handlers

    spec = REGISTRY.get(job_type)
    assert spec is not None, job_type
    with factories.transaction() as db:
        set_context(db, household_id)
        jobs = [
            (job.id, job.payload)
            for job in db.execute(
                select(Job)
                .where(Job.type == job_type, Job.state == JobState.QUEUED)
                .order_by(Job.created_at, Job.id)
            ).scalars()
            if job.id not in done
        ]
    ran: list[uuid.UUID] = []
    for job_id, payload in jobs:
        with factories.transaction() as db:
            set_context(db, household_id)
            ctx = JobContext(
                job_id=job_id,
                job_type=job_type,
                attempt=1,
                max_attempts=spec.max_attempts,
                household_id=household_id,
                session=db,
                deadline=datetime.max.replace(tzinfo=UTC),
            )
            spec.func(ctx, spec.validate_payload(payload))
        done.add(job_id)
        ran.append(job_id)
    return ran
