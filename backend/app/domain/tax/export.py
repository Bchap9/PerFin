# SPDX-License-Identifier: AGPL-3.0-or-later
"""Accountant export of a tax year (API.md ``/tax-years/{year}/export``, ROADMAP Phase 6).

A zip for the household's accountant with:

* ``originals/<document id>/<file name>``: the original file of every
  accepted document of the year (``document.tax_year`` is the year, it is
  named by the year's checklist or its form data was filed under the year),
  decrypted from the blob store and checked against its SHA-256 and size;
* ``summary.csv``: the year's facts, one row per fact (``section`` =
  ``year``, ``checklist``, ``form``, ``category`` or ``hint``); every text
  cell is neutralised against formula injection (SECURITY.md C7,
  ``app.core.csv_safety``);
* ``summary.pdf``: the same summary drawn with reportlab (plain text).

Rules (same as the full export, docs/PORTABILITY.md):

* Only an owner or admin may ask for it, from a browser session with a
  step-up verification in the last 10 minutes (the router's guard); owners
  and admins see every account, so it is the whole household's year. One
  export may be pending per household (``409 tax-export-in-progress``).
* :func:`run_export_job` (job ``tax.export``) writes the zip into the blob
  store's ``permanent`` area through ``EncryptingWriter`` (a per-object key
  wrapped by the master key: encrypted at rest, ADR-0003) and marks it
  ``ready`` for ``PERFIN_EXPORT_TTL_MINUTES``; ``tax.export_expire``
  crypto-shreds it then. The job re-checks that the requester is still an
  owner or admin of the household.
* :func:`open_download` is **one-time** and only for the requester: it opens
  the decrypting stream, destroys the blob's key and deletes the blob row in
  the same transaction (committed before the first byte), then unlinks the
  file. Everyone else gets ``404 tax-export-not-found``.

Every step is audited (``tax_export.request|complete|fail|download|expire``;
ids, counts and error slugs only) and emits ``tax_export.*`` events.
"""

from __future__ import annotations

import csv
import hashlib
import io
import itertools
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import delete, event, insert, or_, select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.authz import role_at_least
from app.auth.models import HouseholdMember, HouseholdRole, User
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.crypto import DecryptionError, Keyring
from app.core.csv_safety import neutralise_formula
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.core.settings import Settings
from app.db.queue import enqueue
from app.domain.documents import repo as documents_repo
from app.domain.documents import service as documents_service
from app.domain.documents.blobstore import (
    BlobStore,
    BlobStoreError,
    EncryptingWriter,
    LocalFSBlobStore,
    SealedBlob,
)
from app.domain.documents.models import Blob, BlobArea, Document, DocumentStatus
from app.domain.portability.export import member_filename
from app.domain.portability.import_ import blob_row
from app.domain.tax.models import (
    TaxChecklistItem,
    TaxExport,
    TaxExportState,
    TaxFormData,
    TaxYear,
)
from app.domain.tax.rollup import by_hint, form_totals, year_rollup
from app.domain.tax.schemas import (
    EXPORT_EXPIRE_JOB_TYPE,
    EXPORT_JOB_TYPE,
    TaxExportJob,
    TaxExportOut,
    TaxExportQueuedOut,
)
from app.domain.tax.service import (
    box_amount,
    box_label,
    checklist_counts,
    coverage_months,
    form_boxes,
    require_year,
)

log = get_logger(__name__)

_DOS_EPOCH: Final = (1980, 1, 1, 0, 0, 0)
_ERROR_DETAIL_MAX: Final = 300
#: Documents in one export at most (bounded work).
MAX_DOCUMENTS: Final = 2000
CSV_HEADER: Final = (
    "section",
    "form_type",
    "issuer",
    "box",
    "label",
    "category",
    "tax_treatment",
    "schedule",
    "line",
    "amount",
    "currency",
    "status",
)


@dataclass(frozen=True)
class TaxExportDeps:
    store: BlobStore
    keyring: Keyring
    export_ttl: timedelta

    @classmethod
    def from_settings(cls, settings: Settings, keyring: Keyring | None = None) -> TaxExportDeps:
        return cls(
            store=LocalFSBlobStore.from_settings(settings),
            keyring=keyring or Keyring.from_settings(settings),
            export_ttl=timedelta(minutes=settings.export_ttl_minutes),
        )


class ExportFailedError(Exception):
    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug


def export_not_found() -> ProblemError:
    return ProblemError(404, "tax-export-not-found", "Tax export not found")


def export_expired() -> ProblemError:
    return ProblemError(
        410, "tax-export-expired", "Export no longer available", "Request a new export."
    )


# ---------------------------------------------------------------------------
# Rows, blobs, records
# ---------------------------------------------------------------------------


def _after(
    session: Session, *, on_commit: Callable[[], None], on_rollback: Callable[[], None]
) -> None:
    """Run exactly one callback when the session's transaction ends (file cleanup)."""
    done = [False]

    def run(callback: Callable[[], None]) -> None:
        if done[0]:
            return
        done[0] = True
        try:
            callback()
        except OSError as exc:
            # A left-over file is ciphertext whose key is gone or was never stored.
            log.warning("tax export file cleanup failed", extra={"error_class": type(exc).__name__})

    event.listen(session, "after_commit", lambda _: run(on_commit), once=True)
    event.listen(session, "after_rollback", lambda _: run(on_rollback), once=True)


def _delete_file(store: BlobStore, key: str) -> Callable[[], None]:
    def run() -> None:
        for area in BlobArea:
            store.delete(area, key)

    return run


def _discard_blob(session: Session, store: BlobStore, row: TaxExport) -> None:
    """Destroy the export's blob: key first, then the row; the file once committed."""
    if row.blob_id is None:
        return
    blob_id = row.blob_id
    blob = documents_repo.get_blob(session, row.household_id, blob_id)
    documents_repo.shred_blob_key(session, row.household_id, blob_id)
    row.blob_id = None
    session.flush()
    session.execute(delete(Blob).where(Blob.household_id == row.household_id, Blob.id == blob_id))
    if blob is not None:
        _after(session, on_commit=_delete_file(store, blob.key), on_rollback=lambda: None)


def _record(
    db: Session,
    row: TaxExport,
    action: str,
    *,
    after: dict[str, Any],
    user_id: uuid.UUID | None,
    request: HTTPConnection | None = None,
) -> None:
    audit(
        db,
        action=action,
        target_type="tax_export",
        target_id=row.id,
        after={"job_id": row.job_id, "year": row.year, **after},
        household_id=row.household_id,
        user_id=user_id,
        request=request,
    )
    emit(
        db,
        row.household_id,
        action,
        {"job_id": str(row.job_id), "year": row.year, "state": str(row.state)},
    )


def _own(
    db: Session, principal: Principal, year: int, job_id: uuid.UUID, *, lock: bool = False
) -> TaxExport:
    require_year(db, principal, year)
    query = select(TaxExport).where(
        TaxExport.household_id == principal.household_id,
        TaxExport.job_id == job_id,
        TaxExport.year == year,
        TaxExport.requested_by == principal.user_id,
    )
    row = db.execute(query.with_for_update() if lock else query).scalar_one_or_none()
    if row is None:
        raise export_not_found()
    return row


def export_out(row: TaxExport) -> TaxExportOut:
    counts = row.summary.get("counts", {}) if isinstance(row.summary, dict) else {}
    return TaxExportOut(
        job_id=row.job_id,
        year=row.year,
        state=row.state,
        size=row.size,
        sha256=row.sha256,
        created_at=row.created_at,
        finished_at=row.finished_at,
        expires_at=row.expires_at,
        downloaded_at=row.downloaded_at,
        error=row.error,
        counts={str(k): int(v) for k, v in counts.items()},
    )


# ---------------------------------------------------------------------------
# Request and state
# ---------------------------------------------------------------------------


def request_export(
    db: Session, principal: Principal, year: int, *, request: HTTPConnection | None
) -> TaxExportQueuedOut:
    tax_year = require_year(db, principal, year)
    pending = db.execute(
        select(TaxExport.id)
        .where(
            TaxExport.household_id == tax_year.household_id,
            TaxExport.state == TaxExportState.PENDING,
        )
        .limit(1)
    ).scalar_one_or_none()
    if pending is not None:
        raise ProblemError(
            409,
            "tax-export-in-progress",
            "A tax export is already running",
            "Wait until the household's current tax export has finished.",
        )
    export_id = uuid7()
    job_id = enqueue(
        db,
        EXPORT_JOB_TYPE,
        TaxExportJob(export_id=export_id),
        household_id=tax_year.household_id,
        max_attempts=3,
    )
    row = TaxExport(
        id=export_id,
        household_id=tax_year.household_id,
        tax_year_id=tax_year.id,
        year=tax_year.year,
        state=TaxExportState.PENDING,
        job_id=job_id,
        requested_by=principal.user_id,
    )
    db.add(row)
    db.flush()
    _record(db, row, "tax_export.request", after={}, user_id=principal.user_id, request=request)
    return TaxExportQueuedOut(job_id=job_id, year=row.year, state=row.state)


def get_export(db: Session, principal: Principal, year: int, job_id: uuid.UUID) -> TaxExportOut:
    return export_out(_own(db, principal, year, job_id))


def _lock(db: Session, household_id: uuid.UUID, export_id: uuid.UUID) -> TaxExport | None:
    return db.execute(
        select(TaxExport)
        .where(TaxExport.household_id == household_id, TaxExport.id == export_id)
        .with_for_update()
    ).scalar_one_or_none()


def _fail(session: Session, row: TaxExport, slug: str, store: BlobStore) -> None:
    _discard_blob(session, store, row)
    row.state = TaxExportState.FAILED
    row.error = slug
    row.finished_at = clock.utcnow()
    session.flush()
    _record(session, row, "tax_export.fail", after={"error": slug}, user_id=row.requested_by)


def _requester(session: Session, row: TaxExport) -> Principal | None:
    """The requester as an owner/admin principal, or None if they no longer are one."""
    if row.requested_by is None:
        return None
    user = session.get(User, row.requested_by)
    role = session.execute(
        select(HouseholdMember.role).where(
            HouseholdMember.household_id == row.household_id,
            HouseholdMember.user_id == row.requested_by,
        )
    ).scalar_one_or_none()
    if user is None or not role_at_least(role, HouseholdRole.ADMIN):
        return None
    return Principal(user=user, household_id=row.household_id, role=role, auth_method="session")


def run_export_job(
    session: Session,
    household_id: uuid.UUID,
    export_id: uuid.UUID,
    deps: TaxExportDeps,
    *,
    check_cancelled: Callable[[], None] = lambda: None,
) -> TaxExportState | None:
    """Build the export (``tax.export``); ``None`` when there is nothing to do."""
    row = _lock(session, household_id, export_id)
    if row is None or row.state is not TaxExportState.PENDING:
        return None
    principal = _requester(session, row)
    tax_year = (
        session.execute(
            select(TaxYear).where(
                TaxYear.household_id == household_id, TaxYear.id == row.tax_year_id
            )
        ).scalar_one_or_none()
        if row.tax_year_id is not None
        else None
    )
    if principal is None:
        _fail(session, row, "requester-not-allowed", deps.store)
        return row.state
    if tax_year is None:
        _fail(session, row, "tax-year-missing", deps.store)
        return row.state
    try:
        written = write_export(
            session,
            principal,
            tax_year,
            store=deps.store,
            keyring=deps.keyring,
            blob_id=uuid7(),
            created=clock.utcnow(),
            check_cancelled=check_cancelled,
        )
    except ExportFailedError as exc:
        _fail(session, row, exc.slug, deps.store)
        return row.state
    sealed = written.sealed
    _after(session, on_commit=lambda: None, on_rollback=_delete_file(deps.store, sealed.key))
    session.execute(insert(Blob), [blob_row(household_id, sealed)])
    row.blob_id = sealed.blob_id
    row.state = TaxExportState.READY
    row.size = written.size
    row.sha256 = written.sha256
    row.finished_at = clock.utcnow()
    row.expires_at = row.finished_at + deps.export_ttl
    row.summary = {"counts": written.counts}
    session.flush()
    enqueue(
        session,
        EXPORT_EXPIRE_JOB_TYPE,
        TaxExportJob(export_id=row.id),
        household_id=household_id,
        run_after=row.expires_at,
    )
    _record(
        session,
        row,
        "tax_export.complete",
        after={"size": written.size, "sha256": written.sha256, "counts": written.counts},
        user_id=row.requested_by,
    )
    return row.state


def _due(row: TaxExport) -> bool:
    return (
        row.state is TaxExportState.READY
        and row.expires_at is not None
        and row.expires_at <= clock.utcnow()
    )


def _expire(session: Session, row: TaxExport, store: BlobStore) -> None:
    _discard_blob(session, store, row)
    row.state = TaxExportState.EXPIRED
    session.flush()
    _record(session, row, "tax_export.expire", after={}, user_id=None)


def run_expire_job(
    session: Session, household_id: uuid.UUID, export_id: uuid.UUID, deps: TaxExportDeps
) -> TaxExportState | None:
    """``tax.export_expire``: destroy a ready export whose time is up."""
    row = _lock(session, household_id, export_id)
    if row is None or row.state is not TaxExportState.READY:
        return None
    if not _due(row):
        enqueue(
            session,
            EXPORT_EXPIRE_JOB_TYPE,
            TaxExportJob(export_id=row.id),
            household_id=household_id,
            run_after=row.expires_at,
        )
        return row.state
    _expire(session, row, deps.store)
    return row.state


def expire_if_due(
    db: Session, principal: Principal, year: int, job_id: uuid.UUID, *, store: BlobStore
) -> bool:
    row = _own(db, principal, year, job_id, lock=True)
    if not _due(row):
        return False
    _expire(db, row, store)
    return True


@dataclass(frozen=True)
class TaxExportDownload:
    filename: str
    size: int
    chunks: Iterator[bytes]


def open_download(
    db: Session,
    principal: Principal,
    year: int,
    job_id: uuid.UUID,
    *,
    store: BlobStore,
    keyring: Keyring,
    request: HTTPConnection | None,
) -> TaxExportDownload:
    """Open the one-time download (module doc); the caller commits before streaming."""
    row = _own(db, principal, year, job_id, lock=True)
    if row.state is TaxExportState.DOWNLOADED:
        raise ProblemError(
            410,
            "tax-export-already-downloaded",
            "Export no longer available",
            "An export can be downloaded only once.",
        )
    if row.state is TaxExportState.EXPIRED or _due(row):
        raise export_expired()
    if row.state is not TaxExportState.READY or row.blob_id is None:
        raise ProblemError(
            409, "tax-export-not-ready", "Export not ready", "The export is not ready to download."
        )
    blob = documents_repo.get_blob(db, row.household_id, row.blob_id)
    if blob is None:
        raise ProblemError(500, "tax-export-unreadable", "Export unreadable")
    chunks = documents_service.open_plaintext(store, keyring, blob)
    try:
        first = next(chunks, b"")
    except (DecryptionError, BlobStoreError, OSError) as exc:
        log.error("tax export download failed", extra={"error_class": type(exc).__name__})
        raise ProblemError(500, "tax-export-unreadable", "Export unreadable") from None
    _discard_blob(db, store, row)
    row.state = TaxExportState.DOWNLOADED
    row.downloaded_at = clock.utcnow()
    db.flush()
    _record(
        db,
        row,
        "tax_export.download",
        after={"size": row.size},
        user_id=principal.user_id,
        request=request,
    )
    return TaxExportDownload(
        filename=f"perfin-tax-{row.year}-{row.created_at:%Y%m%d-%H%M%S}.zip",
        size=blob.size,
        chunks=itertools.chain((first,), chunks) if first else chunks,
    )


# ---------------------------------------------------------------------------
# Writing the archive
# ---------------------------------------------------------------------------


class _Sink(io.RawIOBase):
    """Unseekable stream into the blob writer; counts and hashes the bytes."""

    def __init__(self, writer: EncryptingWriter) -> None:
        super().__init__()
        self._writer = writer
        self.sha256 = hashlib.sha256()
        self.size = 0

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, data: Any) -> int:
        chunk = bytes(data)
        if chunk:
            self._writer.write(chunk)
            self.sha256.update(chunk)
            self.size += len(chunk)
        return len(chunk)


@dataclass(frozen=True)
class WrittenExport:
    sealed: SealedBlob
    size: int
    sha256: str
    counts: dict[str, int] = field(default_factory=dict)


@contextmanager
def _member(
    archive: zipfile.ZipFile, name: str, created: datetime
) -> Iterator[Callable[[bytes], None]]:
    info = zipfile.ZipInfo(name, date_time=max(created.timetuple()[:6], _DOS_EPOCH))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    with archive.open(info, "w", force_zip64=True) as handle:

        def write(data: bytes) -> None:
            handle.write(data)

        yield write


def year_documents(session: Session, tax_year: TaxYear) -> list[tuple[Document, Blob]]:
    """Accepted documents of the year with their (permanent) blobs (module doc)."""
    household_id = tax_year.household_id
    named = select(TaxChecklistItem.document_id).where(
        TaxChecklistItem.household_id == household_id,
        TaxChecklistItem.tax_year_id == tax_year.id,
        TaxChecklistItem.document_id.is_not(None),
    )
    filed = select(TaxFormData.document_id).where(
        TaxFormData.household_id == household_id,
        or_(TaxFormData.tax_year_id == tax_year.id, TaxFormData.tax_year == tax_year.year),
    )
    rows = session.execute(
        select(Document, Blob)
        .join(Blob, (Blob.household_id == Document.household_id) & (Blob.id == Document.blob_id))
        .where(
            Document.household_id == household_id,
            Document.status.in_((DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW)),
            Blob.area == BlobArea.PERMANENT,
            or_(
                Document.tax_year == tax_year.year,
                Document.id.in_(named),
                Document.id.in_(filed),
            ),
        )
        .order_by(Document.id)
        .limit(MAX_DOCUMENTS + 1)
    ).all()
    if len(rows) > MAX_DOCUMENTS:
        raise ExportFailedError("too-many-documents")
    return [(d, b) for d, b in rows]


def _cell(value: object) -> str:
    text = "" if value is None else str(value)
    return neutralise_formula(text)


def summary_rows(
    session: Session, principal: Principal, tax_year: TaxYear
) -> tuple[list[list[str]], dict[str, int]]:
    """The rows of ``summary.csv`` (module doc) and counts per section."""
    rows: list[list[str]] = []
    counts: dict[str, int] = {}

    def add(section: str, **values: object) -> None:
        rows.append([_cell(section if col == "section" else values.get(col)) for col in CSV_HEADER])
        counts[section] = counts.get(section, 0) + 1

    cl = checklist_counts(session, tax_year)
    add(
        "year",
        label=f"{tax_year.year} {tax_year.jurisdiction}",
        status=str(tax_year.state),
        line=tax_year.filing_status,
    )
    items = session.execute(
        select(TaxChecklistItem)
        .where(
            TaxChecklistItem.household_id == tax_year.household_id,
            TaxChecklistItem.tax_year_id == tax_year.id,
        )
        .order_by(TaxChecklistItem.expected_form, TaxChecklistItem.issuer)
    ).scalars()
    for item in items:
        add("checklist", form_type=item.expected_form, issuer=item.issuer, status=str(item.status))
    forms = session.execute(
        select(TaxFormData)
        .where(
            TaxFormData.household_id == tax_year.household_id,
            TaxFormData.tax_year == tax_year.year,
            TaxFormData.jurisdiction == tax_year.jurisdiction,
        )
        .order_by(TaxFormData.form_type, TaxFormData.issuer_name, TaxFormData.id)
    ).scalars()
    for form in forms:
        boxes = form_boxes(form)
        months = coverage_months(form.form_type, boxes)
        for key, value in sorted(boxes.items()):
            if key.startswith("cov_"):
                continue
            add(
                "form",
                form_type=form.form_type,
                issuer=form.issuer_name,
                box=key,
                label=box_label(form.jurisdiction, form.form_type, key),
                amount=box_amount(value),
            )
        if months is not None:
            add(
                "form",
                form_type=form.form_type,
                issuer=form.issuer_name,
                box="coverage",
                label="Months of coverage",
                status=" ".join(months),
            )
    rollup = year_rollup(session, principal, tax_year)
    for c in rollup.categories:
        add(
            "category",
            category=c.name,
            tax_treatment=c.treatment.value if c.treatment is not None else "flagged",
            schedule=c.schedule,
            line=c.line,
            amount=f"{c.total:f}",
            currency=rollup.currency,
        )
    for schedule, line, total in by_hint(rollup):
        add("hint", schedule=schedule, line=line, amount=f"{total:f}", currency=rollup.currency)
    counts["expected_missing"] = cl.expected
    return rows, counts


def summary_pdf(session: Session, principal: Principal, tax_year: TaxYear) -> bytes:
    """``summary.pdf``: the year, its checklist, form totals and schedule hints (plain text)."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter, invariant=True)
    c.setTitle(f"Tax year {tax_year.year}")
    y = 750.0

    def text(value: str, *, size: float = 10, indent: float = 0, bold: bool = False) -> None:
        nonlocal y
        if y < 60:
            c.showPage()
            y = 750.0
        c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
        c.drawString(54 + indent, y, value[:110])
        y -= size + 5

    text(f"Tax year {tax_year.year} ({tax_year.jurisdiction})", size=16, bold=True)
    text(f"State: {tax_year.state}   Filing status: {tax_year.filing_status or '-'}")
    text(
        f"Prepared {clock.utcnow():%Y-%m-%d} by PerFin. Schedule hints are not tax advice.", size=8
    )
    y -= 6
    counts = checklist_counts(session, tax_year)
    text("Documents", size=12, bold=True)
    text(
        f"{counts.received} received, {counts.expected} still expected, "
        f"{counts.not_applicable} not applicable"
    )
    items = session.execute(
        select(TaxChecklistItem)
        .where(
            TaxChecklistItem.household_id == tax_year.household_id,
            TaxChecklistItem.tax_year_id == tax_year.id,
        )
        .order_by(TaxChecklistItem.expected_form, TaxChecklistItem.issuer)
    ).scalars()
    for item in items:
        text(f"{item.expected_form}  {item.issuer or '-'}  {item.status}", indent=12)
    y -= 6
    text("Forms", size=12, bold=True)
    for form_type, count, sums in form_totals(session, principal, tax_year):
        text(f"{form_type} ({count})", bold=True, indent=6)
        for key, value in sorted(sums.items()):
            label = box_label(tax_year.jurisdiction, form_type, key)
            text(f"{key}  {label[:70]}  {value:,.2f}", size=9, indent=18)
    y -= 6
    rollup = year_rollup(session, principal, tax_year)
    text(f"Tax-relevant spending and income ({rollup.currency})", size=12, bold=True)
    for schedule, line, total in by_hint(rollup):
        text(f"{schedule or 'No schedule hint'} {line or ''}  {total:,.2f}", bold=True, indent=6)
        for cat in rollup.categories:
            if (cat.schedule, cat.line) == (schedule, line):
                treatment = cat.treatment.value if cat.treatment is not None else "flagged"
                text(
                    f"{cat.name or 'Uncategorised'} ({treatment})  {cat.total:,.2f}",
                    size=9,
                    indent=18,
                )
    for money in rollup.unconverted:
        text(f"Not converted (no exchange rate): {money.amount} {money.currency}", size=9)
    c.showPage()
    c.save()
    return buf.getvalue()


def write_export(
    session: Session,
    principal: Principal,
    tax_year: TaxYear,
    *,
    store: BlobStore,
    keyring: Keyring,
    blob_id: uuid.UUID,
    created: datetime,
    check_cancelled: Callable[[], None] = lambda: None,
) -> WrittenExport:
    """Write the zip into a new ``permanent`` object of ``store`` (module doc)."""
    documents = year_documents(session, tax_year)
    writer = EncryptingWriter(store, keyring, blob_id, area=BlobArea.PERMANENT)
    try:
        sink = _Sink(writer)
        with zipfile.ZipFile(
            sink, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            for document, blob in documents:
                check_cancelled()
                _write_original(archive, store, keyring, document, blob, created)
            rows, counts = summary_rows(session, principal, tax_year)
            csv_buf = io.StringIO()
            out = csv.writer(csv_buf, lineterminator="\r\n")
            out.writerow(CSV_HEADER)
            out.writerows(rows)
            with _member(archive, "summary.csv", created) as write:
                write(csv_buf.getvalue().encode("utf-8"))
            with _member(archive, "summary.pdf", created) as write:
                write(summary_pdf(session, principal, tax_year))
        sealed = writer.finish()
    except BaseException:
        writer.abort()
        raise
    counts = {**counts, "documents": len(documents)}
    return WrittenExport(
        sealed=sealed, size=sink.size, sha256=sink.sha256.hexdigest(), counts=counts
    )


def _write_original(
    archive: zipfile.ZipFile,
    store: BlobStore,
    keyring: Keyring,
    document: Document,
    blob: Blob,
    created: datetime,
) -> None:
    name = f"originals/{document.id}/{member_filename(document)}"
    digest = hashlib.sha256()
    size = 0
    try:
        with _member(archive, name, created) as write:
            for chunk in documents_service.open_plaintext(store, keyring, blob):
                digest.update(chunk)
                size += len(chunk)
                write(chunk)
    except (DecryptionError, BlobStoreError, OSError):
        raise ExportFailedError("document-unreadable") from None
    if digest.hexdigest() != document.sha256 or size != document.size:
        raise ExportFailedError("document-unreadable")
