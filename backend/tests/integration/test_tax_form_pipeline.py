# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tax forms end to end: worker, tax vault, accountant export, reminders.

upload -> worker ``ingest_document`` -> real sandbox (PDF pages) ->
classifier -> tax form parser -> ``redact_identity`` ->
``app.domain.tax.ingest`` (form data, tax year, checklist) -> API.
Forms come from ``tests/fixtures/synth_tax_forms.py``: every US form type,
with obviously fake identity values (``000-00-0000``, ``00-0000000``, an
invented name, address, account, control, policy and state id numbers).

* Each form's amounts are stored exactly; the year is created when its
  first form arrives and its checklist learned from the year before; a form
  expected from last year is ticked off, an unexpected one is ``detected``.
* **No identity value from the fixtures appears anywhere in the database**
  (every column of every table, search vectors included) **or in an API
  response** (documents, word regions, search, review queue, tax years,
  checklist, forms, summary); the issuer's name is kept and searchable.
* The accountant export (owner, step-up) is a zip of the year's originals,
  ``summary.csv`` (formula cells neutralised, C7) and ``summary.pdf``,
  encrypted at rest and downloadable once.
* Reminders: on a configured day, an open year with expected documents
  missing raises ``tax.reminder`` ("N expected documents still missing").
"""

from __future__ import annotations

import csv
import io
import uuid
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, text

import app.workers.handlers.tax as tax_handlers
from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.queue import enqueue
from app.db.rls import set_context
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import Blob, Document, DocumentKind, DocumentStatus
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.tax.models import (
    ChecklistOrigin,
    ChecklistStatus,
    TaxChecklistItem,
    TaxExport,
    TaxFormData,
    TaxYear,
    TaxYearState,
)
from app.domain.tax.schemas import EXPORT_JOB_TYPE, REMINDER_JOB_TYPE, TaxReminderJob
from app.domain.tax.service import form_boxes
from app.ingestion.intake.upload import receive_upload
from app.workers.handlers import ingest
from tests.fixtures import synth_tax_forms as synth
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, household_events
from tests.fixtures.imports import run_jobs

pytestmark = pytest.mark.integration

MAX = 10 * 1024 * 1024


@dataclass
class Env:
    factories: Factories
    store: LocalFSBlobStore
    keyring: Keyring
    owner: AuthedClient
    done: set[uuid.UUID] = field(default_factory=set)

    @property
    def household_id(self) -> uuid.UUID:
        household_id = self.owner.record.household_id
        assert household_id is not None
        return household_id

    def ingest(self, data: bytes, filename: str) -> Document:
        record = self.owner.record
        principal = Principal(
            user=record.user,
            household_id=self.household_id,
            role=record.role,
            auth_method="session",
        )
        with self.factories.transaction() as db:
            apply_rls_context(db, principal)
            received = receive_upload(
                db,
                principal,
                store=self.store,
                keyring=self.keyring,
                chunks=iter([data]),
                max_bytes=MAX,
                filename=filename,
                account_id=None,
            )
        assert run_jobs(self.factories, self.household_id, INGEST_JOB_TYPE, done=self.done)
        found: list[Document] = self.query(
            select(Document).where(Document.id == received.document_id)
        )
        return found[0]

    def query(self, statement: Any) -> list[Any]:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            rows = list(db.execute(statement).scalars())
            db.expunge_all()
            return rows

    def run(self, job_type: str) -> list[uuid.UUID]:
        return run_jobs(self.factories, self.household_id, job_type, done=self.done)


@pytest.fixture
def env(
    factories: Factories,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Env]:
    local = settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )
    monkeypatch.setattr(ingest, "get_settings", lambda: local)
    monkeypatch.setattr(tax_handlers, "get_settings", lambda: local)
    ingest.default_deps.cache_clear()
    deps = ingest.default_deps()
    assert isinstance(deps.store, LocalFSBlobStore)
    with Actors.open(local, factories) as actors:
        yield Env(
            factories=factories,
            store=deps.store,
            keyring=deps.keyring,
            owner=actors.new(role="owner"),
        )
    ingest.default_deps.cache_clear()


def _database_text(admin_engine: Engine) -> str:
    """Every row of every table of the test database as text (lower case)."""
    parts: list[str] = []
    with admin_engine.connect() as conn:
        tables = conn.execute(
            text(
                "SELECT quote_ident(table_name) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            )
        ).scalars()
        for table in list(tables):
            # Identifiers come from the catalogue, never from input.
            rows = conn.execute(text(f"SELECT t::text FROM {table} t")).scalars()  # noqa: S608
            parts.extend(str(r) for r in rows)
    return "\n".join(parts).lower()


def _assert_no_identity(blob: str, where: str) -> None:
    low = blob.lower()
    for token in synth.IDENTITY_TOKENS:
        assert token not in low, f"{token!r} found in {where}"


def test_forms_reach_the_vault_and_identity_reaches_nothing(env: Env, admin_engine: Engine) -> None:
    owner = env.owner
    # Last year's W-2 (creates 2024, its item received) ...
    last_w2 = synth.synth_form("W-2", seed=1, tax_year=2024)
    old = env.ingest(synth.form_pdf(last_w2), "w2-2024.pdf")
    assert old.status is DocumentStatus.PROCESSED, old.error
    # ... so this year's checklist expects it again.
    created = owner.post(f"{API}/tax-years", json={"year": 2025})
    assert created.status_code == 201, created.text
    expected = owner.get(f"{API}/tax-years/2025/checklist").json()["items"]
    assert [(i["expected_form"], i["issuer"], i["status"]) for i in expected] == [
        ("W-2", synth.ISSUERS["W-2"], "expected")
    ]

    forms = synth.all_forms(seed=5, tax_year=2025)
    documents: dict[str, Document] = {}
    for form in forms:
        document = env.ingest(synth.form_pdf(form), f"{form.form_type}.pdf")
        assert document.status is DocumentStatus.PROCESSED, (form.form_type, document.error)
        assert document.kind is DocumentKind.TAX_FORM
        assert document.tax_year == 2025
        documents[form.form_type] = document

    for form in forms:
        (row,) = env.query(
            select(TaxFormData).where(TaxFormData.document_id == documents[form.form_type].id)
        )
        assert row.form_type == form.form_type
        assert row.tax_year == 2025
        assert row.issuer_name == form.issuer
        assert form_boxes(row) == synth.expected_boxes(form)
    items = env.query(select(TaxChecklistItem).order_by(TaxChecklistItem.expected_form))
    this_year = [i for i in items if i.document_id in {d.id for d in documents.values()}]
    assert all(i.status is ChecklistStatus.RECEIVED for i in this_year)
    w2_item = next(i for i in this_year if i.expected_form == "W-2")
    assert w2_item.origin is ChecklistOrigin.PRIOR_YEAR
    assert w2_item.document_id == documents["W-2"].id
    assert {i.origin for i in this_year if i.expected_form != "W-2"} == {ChecklistOrigin.DETECTED}
    (year_row,) = env.query(select(TaxYear).where(TaxYear.year == 2025))
    assert year_row.state is TaxYearState.OPEN

    # Nothing identifying anywhere in the database...
    _assert_no_identity(_database_text(admin_engine), "the database")
    # ...nor in any API response about these documents and the year.
    responses = [
        owner.get(f"{API}/tax-years"),
        owner.get(f"{API}/tax-years/2025"),
        owner.get(f"{API}/tax-years/2025/checklist"),
        owner.get(f"{API}/tax-years/2025/forms"),
        owner.get(f"{API}/tax-years/2025/summary"),
        owner.get(f"{API}/tax-years/2024/forms"),
        owner.get(f"{API}/documents?limit=100"),
        owner.get(f"{API}/review"),
    ]
    for document in [old, *documents.values()]:
        responses.append(owner.get(f"{API}/documents/{document.id}"))
        responses.append(owner.get(f"{API}/documents/{document.id}/regions"))
    for token in ("testperson", "imaginary", "nowhereville", "makebelieve"):
        responses.append(owner.get(f"{API}/documents", params={"q": token}))
    for response in responses:
        assert response.status_code == 200, (response.url, response.text)
        _assert_no_identity(response.text, str(response.url))
    for token in ("testperson", "imaginary"):
        found = owner.get(f"{API}/documents", params={"q": token}).json()
        assert found["items"] == [], token
    # The issuer's name is kept, and the documents are found by it.
    hits = owner.get(f"{API}/documents", params={"q": '"Fictional Widgets"'}).json()["items"]
    assert {h["id"] for h in hits} >= {str(documents["W-2"].id), str(documents["1095-C"].id)}
    forms_out = owner.get(f"{API}/tax-years/2025/forms").json()["items"]
    coverage = next(f for f in forms_out if f["form_type"] == "1095-B")["coverage_months"]
    assert coverage == list(next(f for f in forms if f.form_type == "1095-B").coverage)


def test_a_reparsed_form_is_not_counted_twice(env: Env) -> None:
    form = synth.synth_form("1099-INT", seed=9, tax_year=2025)
    document = env.ingest(synth.form_pdf(form), "1099-int.pdf")
    reprocess = env.owner.post(f"{API}/documents/{document.id}/reprocess")
    assert reprocess.status_code in (200, 202), reprocess.text
    env.run(INGEST_JOB_TYPE)
    rows = env.query(select(TaxFormData).where(TaxFormData.document_id == document.id))
    assert len(rows) == 1
    items = env.query(select(TaxChecklistItem))
    assert len(items) == 1
    assert items[0].status is ChecklistStatus.RECEIVED


def test_accountant_export(env: Env) -> None:
    owner = env.owner
    w2 = synth.synth_form("W-2", seed=2, tax_year=2025)
    w2_pdf = synth.form_pdf(w2)
    document = env.ingest(w2_pdf, "w2.pdf")
    other = env.ingest(synth.form_pdf(synth.synth_form("1098", seed=2, tax_year=2024)), "1098.pdf")
    # An issuer typed by hand that looks like a spreadsheet formula (C7).
    formula = owner.post(
        f"{API}/tax-years/2025/checklist",
        json={"expected_form": "1099-INT", "issuer": '=HYPERLINK("http://x.invalid","bank")'},
    )
    assert formula.status_code == 201, formula.text

    queued = owner.post(f"{API}/tax-years/2025/export")
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]
    busy = owner.post(f"{API}/tax-years/2025/export")
    assert busy.status_code == 409
    assert env.run(EXPORT_JOB_TYPE)
    state = owner.get(f"{API}/tax-years/2025/export/{job_id}").json()
    assert state["state"] == "ready", state
    assert state["counts"]["documents"] == 1

    download = owner.get(f"{API}/tax-years/2025/export/{job_id}/download")
    assert download.status_code == 200
    assert download.headers["content-disposition"].startswith("attachment")
    with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
        names = set(archive.namelist())
        assert names == {f"originals/{document.id}/w2.pdf", "summary.csv", "summary.pdf"}
        assert archive.read(f"originals/{document.id}/w2.pdf") == w2_pdf
        assert archive.read("summary.pdf").startswith(b"%PDF")
        rows = list(csv.DictReader(io.StringIO(archive.read("summary.csv").decode("utf-8"))))
    assert str(other.id) not in names
    boxes = {r["box"]: r["amount"] for r in rows if r["section"] == "form"}
    assert boxes["1"] == f"{w2.amounts['1']:.2f}"
    issuers = [r["issuer"] for r in rows if r["section"] == "checklist"]
    assert '\'=HYPERLINK("http://x.invalid","bank")' in issuers
    assert not any(cell.startswith("=") for r in rows for cell in r.values())
    _assert_no_identity(archive_text(rows), "summary.csv")

    again = owner.get(f"{API}/tax-years/2025/export/{job_id}/download")
    assert again.status_code == 410
    (export_row,) = env.query(select(TaxExport))
    assert export_row.blob_id is None
    assert env.query(select(Blob).where(Blob.key == "x")) == []


def archive_text(rows: list[dict[str, str]]) -> str:
    return "\n".join(",".join(r.values()) for r in rows)


def test_reminders_as_the_year_closes(env: Env) -> None:
    owner = env.owner
    assert owner.post(f"{API}/tax-years", json={"year": 2025}).status_code == 201
    for form in ("W-2", "1099-INT", "1098"):
        owner.post(f"{API}/tax-years/2025/checklist", json={"expected_form": form})
    assert owner.post(f"{API}/tax-years", json={"year": 2024}).status_code == 201
    owner.post(f"{API}/tax-years/2024/checklist", json={"expected_form": "W-2"})
    owner.patch(f"{API}/tax-years/2024", json={"state": "filed"})

    def remind(day: date) -> list[dict[str, Any]]:
        with env.factories.transaction() as db:
            set_context(db, env.household_id)
            enqueue(db, REMINDER_JOB_TYPE, TaxReminderJob(day=day), household_id=env.household_id)
        env.run(REMINDER_JOB_TYPE)
        return [
            e.payload for e in household_events(env.factories, env.household_id, "tax.reminder")
        ]

    assert remind(date(2026, 2, 14)) == []  # not a reminder day
    assert remind(date(2026, 2, 15)) == [
        {"year": 2025, "missing": 3, "message": "3 expected documents still missing"}
    ]
    # The filed 2024 year stays quiet on its reminder days.
    assert len(remind(date(2025, 2, 15))) == 1
    item = owner.get(f"{API}/tax-years/2025/checklist").json()["items"][0]["id"]
    owner.patch(f"{API}/tax-years/2025/checklist/{item}", json={"status": "not_applicable"})
    events = remind(date(2026, 3, 15))
    assert events[-1]["missing"] == 2
    assert events[-1]["message"] == "2 expected documents still missing"


@pytest.mark.parametrize("kind", ["1040", "benefits"])
def test_unregistered_tax_documents_keep_no_identity(
    env: Env, admin_engine: Engine, kind: str
) -> None:
    """A tax document outside the registered set is redacted too (P56-SR-R1-02).

    A prior-year Form 1040 and an unrecognised benefits statement carry the
    fake SSN, name and address; whatever the classifier calls them, none of
    it may reach the stored extraction, a parsed receipt or the search index.
    """
    document = env.ingest(synth.unregistered_pdf(kind), f"{kind}.pdf")
    assert document.status in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW)
    assert document.kind is not DocumentKind.RECEIPT
    extraction = document.extraction or {}
    parse = extraction.get("parse") or {}
    assert parse.get("receipt") is None
    assert "tax_identity_redacted" in parse.get("warnings", [])
    _assert_no_identity(_database_text(admin_engine), "the database")
    for token in ("testperson", "imaginary", "nowhereville"):
        found = env.owner.get(f"{API}/documents", params={"q": token}).json()
        assert found["items"] == [], token
    detail = env.owner.get(f"{API}/documents/{document.id}")
    assert detail.status_code == 200
    _assert_no_identity(detail.text, "the document")
