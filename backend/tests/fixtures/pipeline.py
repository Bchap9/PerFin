# SPDX-License-Identifier: AGPL-3.0-or-later
"""A household seen through the whole pipeline, for the end-to-end tests.

:class:`Home` is one household with the production default categories and
an owner. Files go in through the real upload and the real ingest job, people
act through the HTTP API, and background work runs through the registered
job handlers. Nothing of the clean-up, the dictionary, the engine, the rules
or the detector is replaced: what a test reads back is what a person would
see.

``open_home`` is a context manager and not a pytest fixture, so each test
module wraps it in its own.
"""

from __future__ import annotations

import csv
import io
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.models import AuditLog, Job, JobState, Setting, SettingScope
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.categorisation.models import MerchantMemory
from app.domain.categorisation.service import STATE_KEY
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentStatus,
    IngestJob,
    IngestStage,
    ReviewItem,
    ReviewItemState,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports.service import import_extraction
from app.domain.notifications.routing import SEND_JOB_TYPE
from app.domain.recurring.models import SubscriptionEvent
from app.domain.review.layout import stored_result
from app.domain.transactions.models import Category, ScheduledTransaction, Transaction
from app.ingestion.intake.upload import receive_upload
from app.ingestion.sandbox.protocol import ExtractResult
from app.workers.handlers import ingest
from app.workers.registry import REGISTRY, JobContext, discover_handlers
from tests.fixtures.documents import make_document
from tests.fixtures.factories import AuthedClient, Factories, UserRecord
from tests.fixtures.households import API, Actors, household_audit
from tests.fixtures.imports import run_jobs
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.notifications import make_channel

MAX_UPLOAD = 10 * 1024 * 1024
PDF = b"%PDF-1.7\n% synthetic placeholder, never parsed by the API\n%%EOF\n"
REFRESH_JOB = "categorise.refresh"
DISPATCH_JOB = "categorise.dispatch"
RULES_JOB = "run_rules"
DETECT_JOB = "recurring.detect"


@dataclass(frozen=True)
class Line:
    """One line of a bank export: the day, the signed amount, the descriptor."""

    posted: date
    amount: Decimal | str
    text: str


def csv_bytes(lines: Sequence[Line]) -> bytes:
    """``lines`` as a three-column CSV (quoted where a descriptor needs it)."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["Date", "Description", "Amount"])
    for line in lines:
        writer.writerow([line.posted.isoformat(), line.text, str(line.amount)])
    return out.getvalue().encode()


def reading(last4: str, lines: Sequence[Line]) -> dict[str, Any]:
    """``lines`` as a document parser stores one statement of the account ending ``last4``."""
    days = [line.posted for line in lines]
    return {
        "source_format": "pdf",
        "account": {"last4": last4, "currency": "USD"},
        "period_start": min(days).replace(day=1).isoformat(),
        "period_end": max(days).isoformat(),
        "transactions": [
            {"posted": line.posted.isoformat(), "amount": str(line.amount), "payee": line.text}
            for line in lines
        ],
        "confidence": {"period": 1.0},
    }


def squash(text: str | None) -> str:
    """A descriptor with its runs of white space collapsed (how rows are matched to lines)."""
    return " ".join((text or "").split())


def ok(response: Any, status: int = 200) -> Any:
    assert response.status_code == status, response.text
    return response.json() if response.content else None


@dataclass
class Home:
    factories: Factories
    ledger: LedgerFactories
    store: LocalFSBlobStore
    keyring: Keyring
    actors: Actors
    owner: UserRecord
    done: set[uuid.UUID] = field(default_factory=set)
    _clients: dict[uuid.UUID, AuthedClient] = field(default_factory=dict)

    @property
    def household_id(self) -> uuid.UUID:
        assert self.owner.household_id is not None
        return self.owner.household_id

    # -- people ---------------------------------------------------------

    def client(self, record: UserRecord | None = None) -> AuthedClient:
        """``record`` (default the owner), logged in; one browser per person."""
        who = record or self.owner
        if who.id not in self._clients:
            self._clients[who.id] = self.actors.login(who)
        return self._clients[who.id]

    def person(self, role: str) -> UserRecord:
        """Another person of the household."""
        return self.factories.user(role=role, household=self.owner.household, with_totp=True)

    def choose(
        self, row_id: uuid.UUID, category_id: uuid.UUID | None, *, by: UserRecord | None = None
    ) -> dict[str, Any]:
        """A person sets (or, with ``None``, clears) a row's category in the grid."""
        body = {"category_id": str(category_id) if category_id is not None else None}
        changed: dict[str, Any] = ok(
            self.client(by).patch(f"{API}/transactions/{row_id}", json=body)
        )
        return changed

    # -- files ----------------------------------------------------------

    def _principal(self) -> Principal:
        return Principal(
            user=self.owner.user,
            household_id=self.owner.household_id,
            role=self.owner.role,
            auth_method="session",
        )

    def upload(
        self, account: Account, lines: Sequence[Line], name: str
    ) -> tuple[uuid.UUID, dict[str, Any]]:
        """Upload ``lines`` as a CSV for ``account`` and run its ingest job.

        Returns the document's id and the import's summary.
        """
        with self.factories.transaction() as db:
            apply_rls_context(db, self._principal())
            received = receive_upload(
                db,
                self._principal(),
                store=self.store,
                keyring=self.keyring,
                chunks=iter([csv_bytes(lines)]),
                max_bytes=MAX_UPLOAD,
                filename=name,
                account_id=account.id,
            )
        assert run_jobs(self.factories, self.household_id, INGEST_JOB_TYPE, done=self.done)
        document_id = received.document_id
        documents: list[Document] = self.query(select(Document).where(Document.id == document_id))
        assert documents[0].status is DocumentStatus.PROCESSED, documents[0].status
        logs = self.query(
            select(IngestJob.log).where(
                IngestJob.document_id == document_id, IngestJob.stage == IngestStage.PARSE
            )
        )
        assert len(logs) == 1
        summary: dict[str, Any] = logs[0]
        return document_id, summary

    def read(self, document_id: uuid.UUID, statement: dict[str, Any]) -> dict[str, Any]:
        """Store ``statement`` as the document's reading and import it, as a re-read does."""
        result = ExtractResult.model_validate(
            {"kind": "pdf", "ok": True, "statements": [statement]}
        )
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            document = db.get(Document, document_id)
            assert document is not None
            document.extraction = result.model_dump(mode="json")
            db.flush()
            stored = stored_result(document.extraction)
            assert stored is not None
            outcome = import_extraction(db, document, stored)
            document.status = outcome.status
            summary: dict[str, Any] = outcome.summary
            return summary

    def statement(self, statement: dict[str, Any]) -> tuple[uuid.UUID, dict[str, Any]]:
        """A statement document whose first reading is ``statement``, imported."""
        with self.factories.transaction() as db:
            document = make_document(
                db,
                self.store,
                self.keyring,
                self.household_id,
                data=PDF + uuid.uuid4().hex.encode(),
                mime="application/pdf",
                filename="statement.pdf",
                uploaded_by=self.owner.user.id,
                status=DocumentStatus.NEEDS_REVIEW,
            )
            document_id = document.id
        return document_id, self.read(document_id, statement)

    def kept_items(self, document_id: uuid.UUID) -> list[dict[str, Any]]:
        """The open "earlier rows were kept" findings of a document."""
        items: list[ReviewItem] = self.query(
            select(ReviewItem).where(
                ReviewItem.document_id == document_id, ReviewItem.state == ReviewItemState.OPEN
            )
        )
        return [
            item.payload
            for item in items
            if (item.payload or {}).get("reason") == "earlier_rows_kept"
        ]

    # -- background work --------------------------------------------------

    def run_queued(self, job_type: str) -> int:
        """Run the household's queued ``job_type`` jobs through their handler; how many ran."""
        return len(run_jobs(self.factories, self.household_id, job_type, done=self.done))

    def queued(self, job_type: str) -> int:
        """How many ``job_type`` jobs of the household wait and have not been run here."""
        jobs: list[Job] = self.query(
            select(Job).where(
                Job.household_id == self.household_id,
                Job.type == job_type,
                Job.state == JobState.QUEUED,
            )
        )
        return len([job for job in jobs if job.id not in self.done])

    def run_now(self, job_type: str, payload: dict[str, Any], *, instance: bool = False) -> None:
        """Run a handler as the scheduler would, without a queued row.

        ``instance``: an instance-level job (no household context).
        """
        spec = REGISTRY.get(job_type)
        assert spec is not None, job_type
        household_id = None if instance else self.household_id
        with self.factories.transaction() as db:
            set_context(db, household_id)
            ctx = JobContext(
                job_id=uuid.uuid4(),
                job_type=job_type,
                attempt=1,
                max_attempts=spec.max_attempts,
                household_id=household_id,
                session=db,
                deadline=datetime.max.replace(tzinfo=UTC),
            )
            spec.func(ctx, spec.validate_payload(payload))

    def detect(self, as_of: date) -> None:
        """The daily detection job of the household, run on ``as_of``."""
        self.run_now(DETECT_JOB, {"as_of": as_of.isoformat()})

    # -- reading back -----------------------------------------------------

    def query(self, statement: Any) -> list[Any]:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            rows = list(db.execute(statement).scalars())
            db.expunge_all()
            return rows

    def rows(self, account: Account | None = None) -> list[Transaction]:
        """The household's (or one account's) transactions, oldest first."""
        statement = select(Transaction).where(Transaction.household_id == self.household_id)
        if account is not None:
            statement = statement.where(Transaction.account_id == account.id)
        found: list[Transaction] = self.query(
            statement.order_by(Transaction.posted_date, Transaction.id)
        )
        return found

    def row(self, row_id: uuid.UUID) -> Transaction:
        found: list[Transaction] = self.query(select(Transaction).where(Transaction.id == row_id))
        return found[0]

    def named(self, account: Account, text: str) -> list[Transaction]:
        """The account's rows whose descriptor is ``text``, oldest first."""
        return [row for row in self.rows(account) if squash(row.raw_payee) == squash(text)]

    def category(self, key: str) -> Category:
        """The household's default category with ``key``."""
        found: list[Category] = self.query(
            select(Category).where(
                Category.household_id == self.household_id, Category.system_key == key
            )
        )
        assert len(found) == 1, key
        return found[0]

    def category_keys(self) -> dict[uuid.UUID, str | None]:
        """Category id to its default key (``None`` for the household's own)."""
        found: list[Category] = self.query(
            select(Category).where(Category.household_id == self.household_id)
        )
        return {category.id: category.system_key for category in found}

    def categories(self) -> dict[uuid.UUID, Category]:
        found: list[Category] = self.query(
            select(Category).where(Category.household_id == self.household_id)
        )
        return {category.id: category for category in found}

    def series(self) -> list[ScheduledTransaction]:
        found: list[ScheduledTransaction] = self.query(
            select(ScheduledTransaction)
            .where(ScheduledTransaction.household_id == self.household_id)
            .order_by(ScheduledTransaction.id)
        )
        return found

    def changes(self) -> list[SubscriptionEvent]:
        """Every subscription event of the household, oldest first."""
        found: list[SubscriptionEvent] = self.query(
            select(SubscriptionEvent)
            .where(SubscriptionEvent.household_id == self.household_id)
            .order_by(SubscriptionEvent.occurred_on, SubscriptionEvent.id)
        )
        return found

    def memory(self) -> list[tuple[str, str, uuid.UUID, int]]:
        """What the household learned: key, direction, category, decisions."""
        found: list[MerchantMemory] = self.query(
            select(MerchantMemory).where(MerchantMemory.household_id == self.household_id)
        )
        return sorted(
            (entry.merchant_key, entry.direction, entry.category_id, entry.decisions)
            for entry in found
        )

    def alerts(self) -> list[str]:
        """The event type of every notification queued for the household, oldest first."""
        jobs: list[Job] = self.query(
            select(Job)
            .where(Job.household_id == self.household_id, Job.type == SEND_JOB_TYPE)
            .order_by(Job.created_at, Job.id)
        )
        return [str(job.payload["event_type"]) for job in jobs]

    def channel(self) -> None:
        """A notification channel that hears everything (and resolves nowhere)."""
        with self.factories.transaction() as db:
            make_channel(db, self.household_id, self.keyring)

    def audits(self, action: str) -> list[AuditLog]:
        return household_audit(self.factories, self.household_id, action)

    # -- the household's categorisation state -----------------------------

    def state(self) -> dict[str, Any]:
        """The versions the household's rows were last evaluated with."""
        found: list[Setting] = self.query(
            select(Setting).where(
                Setting.scope == SettingScope.HOUSEHOLD,
                Setting.scope_id == self.household_id,
                Setting.key == STATE_KEY,
            )
        )
        assert len(found) == 1
        value: dict[str, Any] = dict(found[0].value)
        return value

    def set_state(self, **changes: Any) -> None:
        """Rewrite the stored state, as an installation of an earlier release left it."""
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            row = db.execute(
                select(Setting).where(
                    Setting.scope == SettingScope.HOUSEHOLD,
                    Setting.scope_id == self.household_id,
                    Setting.key == STATE_KEY,
                )
            ).scalar_one()
            row.value = {**row.value, **changes}


@contextmanager
def open_home(
    factories: Factories,
    ledger: LedgerFactories,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Home]:
    """A household with the default categories, its owner, and a place for its files."""
    local = settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )
    # The ingest handler reads process settings, as the worker does.
    monkeypatch.setattr(ingest, "get_settings", lambda: local)
    ingest.default_deps.cache_clear()
    deps = ingest.default_deps()
    assert isinstance(deps.store, LocalFSBlobStore)
    discover_handlers()
    try:
        with Actors.open(local, factories) as actors:
            owner = factories.user(role="owner", with_totp=True)
            assert owner.household_id is not None
            ledger.seed_categories(owner.household_id)
            yield Home(
                factories=factories,
                ledger=ledger,
                store=deps.store,
                keyring=deps.keyring,
                actors=actors,
                owner=owner,
            )
    finally:
        ingest.default_deps.cache_clear()
