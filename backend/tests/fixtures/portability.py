# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic data and helpers for the export/import tests (docs/PORTABILITY.md).

* :data:`CHEAP_KDF`: the cheapest Argon2id cost ``app.core.archive_crypto``
  accepts, so passphrase tests run in milliseconds.
* :func:`make_ready_export` / :func:`make_import_record`: flush-only
  archive rows (the export's bytes really encrypted into the given blob
  store) for the authorisation matrix and the download tests.
* :func:`build_rich_ledger`: the report ledger (``tests.fixtures.report_ledger``)
  plus one of everything else an export carries: an institution, tags on
  transactions, imported transactions with import hashes, a balance
  snapshot, a schedule, rules naming categories and tags, a budget, a goal
  with a contribution, an import mapping, a document linked to a
  transaction, and text cells that look like spreadsheet formulas.
* :func:`rezip`: rewrite an archive's members (and, by default, its
  manifest hashes) to build hostile archives from a real one.
* :func:`run_portability_jobs`: run the queued jobs of one type through the
  real handlers with test dependencies.
"""

from __future__ import annotations

import hashlib
import io
import json
import uuid
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from unittest.mock import patch

from sqlalchemy import insert
from sqlalchemy.orm import Session

from app.core import clock
from app.core.archive_crypto import KdfParams
from app.core.crypto import Keyring
from app.core.ids import uuid7
from app.domain.accounts.models import Account
from app.domain.documents.blobstore import BlobStore, EncryptingWriter
from app.domain.documents.models import Blob, BlobArea, Document, DocumentLink, LinkTarget
from app.domain.portability.import_ import blob_row
from app.domain.portability.models import ArchiveKind, ArchiveState, PortabilityArchive
from app.domain.portability.service import PortabilityDeps
from app.domain.transactions.models import Transaction
from tests.fixtures.documents import make_document
from tests.fixtures.factories import Factories
from tests.fixtures.imports import make_import_mapping, run_jobs
from tests.fixtures.ledger import (
    LedgerFactories,
    household_context,
    make_goal_contribution,
    make_scheduled_transaction,
    make_transaction,
    tag_transaction,
)
from tests.fixtures.report_ledger import ReportLedger, build

CHEAP_KDF = KdfParams(1, 8 * 1024)

#: Cells a spreadsheet would run as formulas (C7).
FORMULA_MEMO = '=HYPERLINK("https://evil.example","click")'
FORMULA_PAYEE = "+cmd|' /C calc'!A0"
FORMULA_NOTE = "@SUM(1+1)*cmd|' /C calc'!A0"

DOCUMENT_BYTES = b"Date,Amount,Payee\n2026-03-02,-100.25,Shop\n"


def small_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("manifest.json", "{}")
    return buffer.getvalue()


def make_ready_export(
    db: Session,
    store: BlobStore,
    keyring: Keyring,
    household_id: uuid.UUID,
    requested_by: uuid.UUID | None,
    *,
    data: bytes | None = None,
    state: ArchiveState = ArchiveState.READY,
    expires_in: timedelta = timedelta(hours=1),
) -> PortabilityArchive:
    """A finished export whose (synthetic) bytes are encrypted in ``store``."""
    payload = small_zip() if data is None else data
    with EncryptingWriter(store, keyring, uuid7(), area=BlobArea.PERMANENT) as writer:
        writer.write(payload)
        sealed = writer.finish()
    now = clock.utcnow()
    with household_context(db, household_id):
        db.execute(insert(Blob), [blob_row(household_id, sealed)])
        archive = PortabilityArchive(
            household_id=household_id,
            kind=ArchiveKind.EXPORT,
            state=state,
            job_id=uuid7(),
            requested_by=requested_by,
            blob_id=sealed.blob_id,
            size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            finished_at=now,
            expires_at=now + expires_in,
            summary={"counts": {}},
        )
        db.add(archive)
        db.flush()
    return archive


def make_import_record(
    db: Session,
    household_id: uuid.UUID,
    requested_by: uuid.UUID | None,
    *,
    state: ArchiveState = ArchiveState.IMPORTED,
) -> PortabilityArchive:
    """A finished import (no blob left), as the import job leaves it."""
    with household_context(db, household_id):
        archive = PortabilityArchive(
            household_id=household_id,
            kind=ArchiveKind.IMPORT,
            state=state,
            job_id=uuid7(),
            requested_by=requested_by,
            finished_at=clock.utcnow(),
            summary={"created": {}, "merged": {}, "skipped": {}},
        )
        db.add(archive)
        db.flush()
    return archive


@dataclass(frozen=True)
class RichLedger:
    book: ReportLedger
    document: Document
    hashed: list[Transaction]
    formula: Transaction


def build_rich_ledger(
    factories: Factories,
    ledger: LedgerFactories,
    store: BlobStore,
    household_id: uuid.UUID,
) -> RichLedger:
    hid = household_id
    book = build(ledger, hid)
    institution = ledger.institution(hid, name="Synthetic Bank", country="US")
    with factories.transaction() as db, household_context(db, hid):
        brokerage = db.get(Account, book.savings.id)
        assert brokerage is not None
        brokerage.institution_id = institution.id
        db.flush()
    tag_a = ledger.tag(hid, name="Holiday")
    tag_b = ledger.tag(hid, name="Work")
    hashed = [
        ledger.transaction(
            book.card,
            posted_date=date(2026, 4, 2),
            amount="-12.00",
            raw_payee="COFFEE CART #12",
            with_import_hash=True,
            hash_index=index,
        )
        for index in range(2)
    ]
    formula = ledger.transaction(
        book.checking,
        posted_date=date(2026, 4, 3),
        amount="-1.00",
        raw_payee=FORMULA_PAYEE,
        memo=FORMULA_MEMO,
        notes=FORMULA_NOTE,
    )
    ledger.snapshot(book.checking, as_of_date=date(2026, 1, 31), balance="2500.00")
    ledger.rule(
        hid,
        name="Coffee",
        conditions={"type": "payee", "op": "contains", "value": "coffee"},
        actions=[
            {"type": "set_category", "category_id": str(book.restaurants.id)},
            {"type": "add_tags", "tag_ids": [str(tag_b.id)]},
        ],
    )
    ledger.budget(
        hid,
        name="Monthly",
        lines={book.groceries.id: "400.00", book.restaurants.id: "150.00"},
    )
    goal = ledger.goal(hid, name="Rainy day", accounts=[book.savings])
    with factories.transaction() as db, household_context(db, hid):
        tag_transaction(db, db.get(Transaction, hashed[0].id), tag_a)  # type: ignore[arg-type]
        tag_transaction(db, db.get(Transaction, formula.id), tag_b)  # type: ignore[arg-type]
        contribution_txn = make_transaction(
            db, book.savings, posted_date=date(2026, 4, 5), amount="250.00"
        )
        make_goal_contribution(
            db, goal, amount="250.00", contributed_on=date(2026, 4, 5), transaction=contribution_txn
        )
        make_scheduled_transaction(
            db, book.checking, name="Rent", payee=book.shop, category=book.rent
        )
        make_import_mapping(db, hid, name="Synthetic layout", account=book.checking)
        document = make_document(
            db,
            store,
            factories.keyring,
            hid,
            data=DOCUMENT_BYTES,
            filename="../../March statement?.csv",
            account_id=book.checking.id,
        )
        row = db.get(Transaction, hashed[1].id)
        assert row is not None
        row.source_document_id = document.id
        db.add(
            DocumentLink(
                household_id=hid,
                document_id=document.id,
                target_type=LinkTarget.TRANSACTION,
                target_id=hashed[1].id,
            )
        )
        db.flush()
    return RichLedger(book=book, document=document, hashed=hashed, formula=formula)


def members_of(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def zip_members(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return buffer.getvalue()


def rezip(
    data: bytes,
    mutate: Callable[[dict[str, bytes]], None],
    *,
    fix_manifest: bool = True,
    manifest: Callable[[dict[str, Any]], None] | None = None,
) -> bytes:
    """``data`` with its members changed by ``mutate`` (manifest hashes re-done)."""
    members = members_of(data)
    mutate(members)
    document = json.loads(members["manifest.json"])
    if fix_manifest:
        document["members"] = {
            name: {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
            for name, content in members.items()
            if name != "manifest.json"
        }
    if manifest is not None:
        manifest(document)
    members["manifest.json"] = json.dumps(document).encode()
    return zip_members(members)


def rows_of(members: dict[str, bytes], entity: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(members[f"data/{entity}.json"])
    return rows


def set_rows(members: dict[str, bytes], entity: str, rows: list[dict[str, Any]]) -> None:
    members[f"data/{entity}.json"] = json.dumps(rows).encode()


@contextmanager
def portability_deps(deps: PortabilityDeps) -> Iterator[None]:
    import app.workers.handlers.portability as handlers

    with patch.object(handlers, "default_deps", lambda: deps):
        yield


def run_portability_jobs(
    factories: Factories,
    household_id: uuid.UUID,
    job_type: str,
    deps: PortabilityDeps,
    done: set[uuid.UUID],
) -> list[uuid.UUID]:
    """Run the household's queued ``job_type`` jobs through the real handlers."""
    import app.workers.handlers.portability  # noqa: F401 - registers the handlers

    with portability_deps(deps):
        return run_jobs(factories, household_id, job_type, done=done)
