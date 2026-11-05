# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phases 3+4 end to end: the real app, the real worker, the real sandbox.

One synthetic household goes through every smart-ingestion path, with the
production pipeline dependencies (``app.workers.handlers.ingest.default_deps``,
real sandbox, Tesseract OCR) and the real :class:`app.workers.runner.Worker`
claiming the queued jobs:

1. a text-layer PDF statement uploaded over HTTP is classified, parsed,
   reconciled and lands in the ledger (``GET /transactions``);
2. the same kind of statement as an image-only (scanned) PDF goes through
   OCR and ends in the ledger or, with a doubtful reading, in review;
3. a thermal-receipt photo is OCR'd, parsed and linked to its card
   transaction;
4. a file dropped into the member's drop-folder subfolder is taken by the
   ``watch_dropfolder`` job, ingested, and moved to the processed folder by
   ``dropfolder.finish`` after the run committed;
5. a mail from the member to the household alias on a fake IMAPS server is
   accepted by ``poll_mailbox`` and ingested; a spoofed sender is rejected
   and summarised in the day's ``rejected_email`` digest;
6. ``GET /documents?q=`` finds the statement by a word of its text;
7. ``perfin ingest --dry-run FILE`` prints every stage.

The worker runs only this test's jobs: other tests leave queued jobs in the
shared test database, which are held back for the duration of the test.
"""

from __future__ import annotations

import dataclasses
import importlib
import io
import ipaddress
import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, redirect_stdout
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, text

from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.cli import main
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.models import Job, JobState
from app.db.queue import enqueue
from app.db.rls import queue_scope, set_context
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentLink,
    DocumentSource,
    DocumentStatus,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.dropfolder.models import DropfolderEntry
from app.domain.intake_settings import service as intake_service
from app.domain.intake_settings.schemas import MailIntakeUpdate
from app.ingestion.intake import email
from app.ingestion.sandbox.protocol import ExtractResult
from app.workers.handlers import dropfolder as dropfolder_handler
from app.workers.handlers import ingest
from app.workers.handlers import mailbox as mailbox_handler
from app.workers.registry import REGISTRY, discover_handlers
from app.workers.runner import Worker, WorkerConfig
from app.workers.scheduler import Schedule
from tests.fixtures import synth_pdf_statements as synth
from tests.fixtures import synth_receipts as sr
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.fake_imap import (
    IMAP_HOST,
    PASSWORD,
    USERNAME,
    FakeImapServer,
    ImapPki,
    make_mail,
    make_pki,
    running_imap,
)
from tests.fixtures.households import API, Actors
from tests.fixtures.statements import csv_bytes, synth_transactions
from tests.fixtures.synth_statements import SynthStatement

pytestmark = pytest.mark.integration

PROCESSED_MAIL = "PerFin/Processed"
REJECTED_MAIL = "PerFin/Rejected"


@contextmanager
def only_these_jobs(
    factories: Factories, household_id: uuid.UUID, allowed: set[uuid.UUID]
) -> Iterator[None]:
    """Hold back queued jobs of other households while the worker runs ours.

    Instance-level jobs (no household) are held back too, except ``allowed``
    (the ``watch_dropfolder`` run this test enqueued itself).
    """
    with factories.transaction() as db, queue_scope(db):
        held = list(
            db.execute(
                text(
                    "UPDATE job SET run_after = run_after + interval '100 years' "
                    "WHERE state = 'queued' AND household_id IS DISTINCT FROM :h "
                    "AND NOT (id = ANY(:allowed)) RETURNING id"
                ),
                {"h": household_id, "allowed": list(allowed)},
            ).scalars()
        )
    try:
        yield
    finally:
        if held:
            with factories.transaction() as db, queue_scope(db):
                db.execute(
                    text(
                        "UPDATE job SET run_after = run_after - interval '100 years' "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": held},
                )


class Home:
    """The test's household: the owner's client, a member, and the worker."""

    def __init__(
        self,
        owner: AuthedClient,
        member: AuthedClient,
        factories: Factories,
        engine: Engine,
        settings: Settings,
    ) -> None:
        self.owner = owner
        self.member = member
        self.factories = factories
        self.settings = settings
        household_id = owner.record.household_id
        assert household_id is not None
        self.id = household_id
        self.allowed: set[uuid.UUID] = set()
        self.worker = Worker(
            engine,
            config=WorkerConfig(listen=False, poll_interval=0.05, concurrency=1),
            registry=REGISTRY,
            schedule=Schedule(),
        )

    def drain(self) -> list[tuple[str, JobState]]:
        """Run this household's queued jobs (and the jobs they queue) to the end."""
        with only_these_jobs(self.factories, self.id, self.allowed):
            for _ in range(200):
                if not self.worker.run_once():
                    break
        with self.factories.transaction() as db:
            set_context(db, self.id)
            return [
                (job.type, job.state)
                for job in db.execute(
                    select(Job).where(Job.household_id == self.id).order_by(Job.created_at, Job.id)
                ).scalars()
            ]

    def upload(self, data: bytes, name: str, **params: str) -> str:
        response = self.owner.post(
            f"{API}/documents",
            params=params,
            files={"file": (name, data, "application/octet-stream")},
        )
        assert response.status_code == 202, response.text
        document_id: str = response.json()["id"]
        return document_id

    def get(self, path: str, **params: Any) -> Any:
        response = self.owner.get(f"{API}{path}", params=params)
        assert response.status_code == 200, response.text
        return response.json()

    def transactions(self, account_id: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = self.get("/transactions", account=account_id, limit=500)[
            "items"
        ]
        return items

    def rows(self, model: Any, *where: Any) -> list[Any]:
        with self.factories.transaction() as db:
            set_context(db, self.id)
            found = list(db.execute(select(model).where(*where)).scalars())
            db.expunge_all()
            return found

    def document(self, document_id: str) -> Document:
        (found,) = self.rows(Document, Document.id == uuid.UUID(document_id))
        doc: Document = found
        return doc

    def open_reviews(self, document_id: str) -> list[ReviewItem]:
        return self.rows(
            ReviewItem,
            ReviewItem.document_id == uuid.UUID(document_id),
            ReviewItem.state == ReviewItemState.OPEN,
        )

    def account(self, stmt: SynthStatement, name: str) -> str:
        response = self.owner.post(
            f"{API}/accounts",
            json={
                "type": "credit_card" if stmt.account_type == "creditcard" else "checking",
                "name": name,
                "currency": stmt.currency,
                "number_last4": stmt.last4,
            },
        )
        assert response.status_code == 201, response.text
        account_id: str = response.json()["id"]
        return account_id


@pytest.fixture
def e2e_settings(settings: Settings, tmp_path: Path) -> Settings:
    inbox, processed = tmp_path / "inbox", tmp_path / "processed"
    inbox.mkdir()
    processed.mkdir()
    return settings.model_copy(
        update={
            "documents_dir": tmp_path / "documents",
            "quarantine_dir": tmp_path / "quarantine",
            "dropfolder_inbox": inbox,
            "dropfolder_processed": processed,
            "dropfolder_after": "move",
            "dropfolder_settle_seconds": 0,
            "dropfolder_watch_seconds": 0,
            "email_intake_enabled": True,
        }
    )


@pytest.fixture
def home(
    e2e_settings: Settings,
    factories: Factories,
    app_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Home]:
    discover_handlers()
    monkeypatch.setattr(ingest, "get_settings", lambda: e2e_settings)
    ingest.default_deps.cache_clear()
    try:
        with Actors.open(e2e_settings, factories) as actors:
            owner = actors.new(role="owner", username=f"own{uuid.uuid4().hex[:10]}")
            member = actors.new(
                role="member",
                household=owner.record.household,
                username=f"alex{uuid.uuid4().hex[:8]}",
                email=f"alex-{uuid.uuid4().hex[:8]}@home.invalid",
            )
            household = Home(owner, member, factories, app_engine, e2e_settings)
            settings = e2e_settings.model_copy(
                update={"dropfolder_enabled": True, "dropfolder_household_id": household.id}
            )
            monkeypatch.setattr(dropfolder_handler, "get_settings", lambda: settings)
            monkeypatch.setattr(mailbox_handler, "get_settings", lambda: settings)
            yield household
    finally:
        ingest.default_deps.cache_clear()


def _scanned(pdf: bytes, dpi: int = 200) -> bytes:
    """``pdf`` with every page replaced by a picture of it (no text layer)."""
    pymupdf: Any = importlib.import_module("pymupdf")  # untyped bindings
    source = pymupdf.open(stream=pdf, filetype="pdf")
    out = pymupdf.open()
    for page in source:
        pixmap = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY)
        target = out.new_page(width=page.rect.width, height=page.rect.height)
        target.insert_image(target.rect, stream=pixmap.tobytes("png"))
    data: bytes = out.tobytes()
    return data


def _amounts(items: list[dict[str, Any]]) -> list[tuple[str, Decimal]]:
    return sorted((t["posted_date"], Decimal(t["amount"]["amount"])) for t in items)


def _expected(stmt: SynthStatement) -> list[tuple[str, Decimal]]:
    return sorted((d.isoformat(), a) for d, a, _ in synth.expected(stmt))


def test_statements_receipts_drop_folder_mail_search_and_dry_run(
    home: Home, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. A text-layer PDF statement goes from upload to the ledger.
    stmt = synth.sample_national_statement(seed=51)
    checking = home.account(stmt, "Synthetic checking")
    statement_id = home.upload(synth.sample_national_pdf(stmt), "statement.pdf")
    home.drain()
    document = home.document(statement_id)
    assert document.status is DocumentStatus.PROCESSED, home.open_reviews(statement_id)
    assert str(document.account_id) == checking
    rows = home.transactions(checking)
    assert _amounts(rows) == _expected(stmt)
    assert {(row["status"], row["review_state"]) for row in rows} == {("cleared", "none")}

    # 2. The same layout scanned (no text layer): OCR, then ledger or review.
    scan_stmt = synth.example_fcu_statement(seed=52)
    savings = home.account(scan_stmt, "Synthetic share account")
    scanned_id = home.upload(_scanned(synth.example_fcu_pdf(scan_stmt)), "scan.pdf")
    home.drain()
    scanned = home.document(scanned_id)
    stored = ExtractResult.model_validate(scanned.extraction)
    assert stored.pages
    assert {page.text_source for page in stored.pages} == {"ocr"}
    assert scanned.status in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW)
    scanned_rows = home.transactions(savings)
    assert scanned.confidence is not None
    reconciled = scanned.confidence["parse"]["reconciliation"] == ["ok"]
    if reconciled:
        assert _amounts(scanned_rows) == _expected(scan_stmt)
    held = {row["id"] for row in scanned_rows if row["review_state"] == "pending"}
    if scanned.status is DocumentStatus.PROCESSED:
        assert reconciled
        assert held == set()
    else:
        # A doubtful OCR reading is never committed silently: the rows it
        # concerns are pending behind a review item naming them.
        reviews = home.open_reviews(scanned_id)
        assert reviews
        named = {
            row["transaction_id"]
            for item in reviews
            if item.kind is ReviewKind.LOW_CONFIDENCE_FIELD
            for row in item.payload.get("transactions", [])
        }
        assert held
        assert held <= named if reconciled else held == {row["id"] for row in scanned_rows}

    # 3. A thermal receipt photo is OCR'd and linked to its card transaction.
    spec = dataclasses.replace(sr.receipt_spec(20_004), payment="VISA", last4="8642")
    card = home.owner.post(
        f"{API}/accounts",
        json={"type": "credit_card", "name": "Synthetic card", "currency": spec.currency},
    )
    assert card.status_code == 201, card.text
    paid = home.owner.post(
        f"{API}/transactions",
        json={
            "account_id": card.json()["id"],
            "posted_date": (spec.when + timedelta(days=1)).isoformat(),
            "amount": {"amount": str(-spec.total), "currency": spec.currency},
            "raw_payee": f"POS {spec.merchant} 0412",
        },
    )
    assert paid.status_code == 201, paid.text
    receipt_id = home.upload(sr.thermal_png(spec, sr.Thermal(angle=-1.0)), "receipt.png")
    home.drain()
    receipt = home.document(receipt_id)
    assert receipt.status is DocumentStatus.PROCESSED, home.open_reviews(receipt_id)
    links = home.rows(DocumentLink, DocumentLink.document_id == uuid.UUID(receipt_id))
    assert [str(link.target_id) for link in links] == [paid.json()["id"]]
    assert receipt.confidence is not None
    assert receipt.confidence["receipt"]["match"] == "linked"

    # 4. A file in the member's drop-folder subfolder: taken, ingested, moved.
    inbox = home.settings.dropfolder_inbox
    processed = home.settings.dropfolder_processed
    member_name = home.member.record.user.username
    dropped = inbox / member_name / "march.csv"
    dropped.parent.mkdir(parents=True)
    dropped.write_bytes(csv_bytes(synth_transactions(3, seed=53)))
    old = time.time() - 120
    os.utime(dropped, (old, old))
    with home.factories.transaction() as db, queue_scope(db):
        home.allowed.add(
            enqueue(
                db, dropfolder_handler.WATCH_JOB_TYPE, dropfolder_handler.WatchDropfolderPayload()
            )
        )
    jobs = home.drain()
    assert (dropfolder_handler.FINISH_JOB_TYPE, JobState.SUCCEEDED) in jobs
    (from_folder,) = home.rows(Document, Document.source == DocumentSource.DROPFOLDER)
    assert from_folder.uploaded_by == home.member.record.user.id
    # A CSV without a saved mapping waits for one; the pipeline ran to the end.
    assert from_folder.status in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW)
    assert not dropped.exists()
    moved = [p for p in processed.rglob("*") if p.is_file()]
    assert [p.name.endswith("-march.csv") for p in moved] == [True]
    (entry,) = home.rows(DropfolderEntry)
    assert (entry.outcome, entry.after_state, entry.after_result) == ("accepted", "done", "moved")

    # 5. Mail: the member's message is accepted; a spoofed sender is digested.
    pki: ImapPki = make_pki(tmp_path / "pki")
    with running_imap(pki) as server:
        _mail_intake(home, server, pki, monkeypatch)

    # 6. Search finds the statement by a word of its text, over HTTP.
    found = home.get("/documents", q="Sample National")["items"]
    assert statement_id in [item["id"] for item in found]
    assert home.get("/documents", q="zzyzx-not-in-any-document")["items"] == []

    # 7. The dry run prints every stage and writes nothing.
    path = tmp_path / "dry.pdf"
    path.write_bytes(synth.sample_national_pdf(stmt))
    before = len(home.rows(Document))
    out = io.StringIO()
    with redirect_stdout(out):
        assert main(["ingest", "--dry-run", str(path)]) == 0
    stages = [json.loads(line)["stage"] for line in out.getvalue().splitlines()]
    assert stages[:5] == ["receive", "scan", "extract", "classify", "parse"]
    assert len(home.rows(Document)) == before


def _mail_intake(
    home: Home, server: FakeImapServer, pki: ImapPki, monkeypatch: pytest.MonkeyPatch
) -> None:
    keyring = Keyring.from_settings(home.settings)
    store = LocalFSBlobStore.from_settings(home.settings)
    principal = Principal(
        user=home.owner.record.user,
        household_id=home.id,
        role=HouseholdRole.OWNER,
        auth_method="session",
    )
    with home.factories.transaction() as db:
        set_context(db, home.id, home.owner.record.user.id)
        intake_service.update_intake(
            db,
            principal,
            MailIntakeUpdate.model_validate(
                {
                    "host": IMAP_HOST,
                    "port": server.port,
                    "username": USERNAME,
                    "password": PASSWORD,
                    "enabled": True,
                }
            ),
            keyring,
            instance_enabled=True,
        )
        row = intake_service.get_row(db, home.id)
        assert row is not None
        alias = intake_service.alias_token(keyring, row)
    monkeypatch.setattr(
        mailbox_handler, "intake_deps", lambda s: email.IntakeDeps(store=store, keyring=keyring)
    )
    monkeypatch.setattr(
        mailbox_handler,
        "connect_options",
        lambda s: email.ConnectOptions(
            proxy=None,
            allow_private=True,
            allow_loopback=True,
            resolver=lambda host, port: [ipaddress.ip_address("127.0.0.1")],
            ssl_context_factory=pki.client_context,
            timeout_seconds=10.0,
        ),
    )
    sender = home.member.record.user.email
    assert sender is not None
    to = f"receipts+{alias}@perfin.test"
    server.deliver(make_mail(sender=sender, to=to, message_id="<e2e-ok@shop.invalid>"))
    server.deliver(
        make_mail(
            sender=f"Alex <{sender}.attacker.invalid>", to=to, message_id="<e2e-spoof@x.invalid>"
        )
    )
    with home.factories.transaction() as db:
        set_context(db, home.id)
        enqueue(db, "poll_mailbox", mailbox_handler.PollMailboxPayload(), household_id=home.id)
    home.drain()
    (mail,) = home.rows(Document, Document.source == DocumentSource.EMAIL)
    assert mail.uploaded_by == home.member.record.user.id
    assert mail.mime_detected == "message/rfc822"
    assert mail.status in (DocumentStatus.PROCESSED, DocumentStatus.NEEDS_REVIEW)
    (digest,) = home.rows(ReviewItem, ReviewItem.kind == ReviewKind.REJECTED_EMAIL)
    assert digest.payload["reasons"] == {"sender_not_allowed": 1}
    # The follow-up poll moved both messages out of the inbox.
    assert server.count("INBOX") == 0
    assert server.count(PROCESSED_MAIL) == 1
    assert server.count(REJECTED_MAIL) == 1
