# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading a document again replaces the rows nobody touched (import 1d, Task 23).

A document's stored reading is changed by hand to stand for a new reading
(a parser that now reads the page another way) and the importer runs again
(``app.domain.imports.service.import_extraction``), as ``POST
/documents/{id}/reprocess`` and every ``apply-mapping`` re-run do. Every
statement, payee and number here is invented.

* A row of the earlier reading that the new one no longer contains is
  removed when nobody touched it, without a tombstone; one a person edited,
  confirmed, categorised, linked as a transfer or attached a document to is
  kept and listed once (``earlier_rows_kept``).
* A line the new reading spells another way replaces the untouched row of
  the earlier reading: it is not a "possible duplicate" of the row it
  replaces. Beside a row a person changed it waits, as a near duplicate.
* A list of kept rows that no longer says what is kept is dismissed.
* Rows of another document on the account, rows of the document on another
  account and a row a person added for the document are never replaced.
* Nothing is replaced for an account while a statement of the document for
  it is held.
* A row a person deleted stays deleted: through a re-read, from another
  file, and for one of two identical purchases. The same row spelled
  another way by another file waits; it is never posted.
* A re-read that changes a statement's period leaves one statement record.
* Freshly imported rows are untouched (``updated_at == created_at``) even
  when a rule categorised them at import.
* The reconcile view names the kept rows as an earlier reading and removes
  them in one step.
* Teaching a table on a statement that was already imported imports it
  again and replaces the earlier rows.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.models import AuditLog
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentLink,
    DocumentStatus,
    LinkTarget,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports.models import ImportTombstone, StatementRecord
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE
from app.domain.imports.service import import_extraction
from app.domain.review import teach as teach_service
from app.domain.review.layout import stored_result
from app.domain.review.schemas import TeachOut, TeachRequest
from app.domain.transactions.models import ReviewState, Transaction
from app.ingestion.intake.upload import receive_upload
from app.ingestion.sandbox.protocol import ExtractResult, Word
from app.workers.handlers import ingest
from tests.fixtures import synth_taught_layout as taught
from tests.fixtures.documents import make_document
from tests.fixtures.factories import AuthedClient, Factories, UserRecord
from tests.fixtures.households import API, Actors
from tests.fixtures.imports import run_jobs
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration

MAX = 10 * 1024 * 1024
PDF = b"%PDF-1.7\n% synthetic placeholder, never parsed by the API\n%%EOF\n"

Entry = tuple[int, str, str]

#: A month as its first reading has it; the last line is one the page does
#: not print (a total read as a row), which the second reading leaves out.
BOGUS: Entry = (27, "-311.20", "Carried forward")
MARCH: list[Entry] = [
    (3, "-48.10", "Alderbank Grocers"),
    (9, "-17.25", "Fernhollow Cafe"),
    (14, "2150.00", "Wrenfield Payroll"),
    (21, "-63.40", "Mossgate Hardware"),
]


@dataclass
class Env:
    factories: Factories
    ledger: LedgerFactories
    store: LocalFSBlobStore
    keyring: Keyring
    owner: UserRecord
    actors: Actors
    done: set[uuid.UUID] = field(default_factory=set)

    @property
    def household_id(self) -> uuid.UUID:
        assert self.owner.household_id is not None
        return self.owner.household_id

    def principal(self) -> Principal:
        return Principal(
            user=self.owner.user,
            household_id=self.owner.household_id,
            role=self.owner.role,
            auth_method="session",
        )

    def client(self) -> AuthedClient:
        return self.actors.login(self.owner)

    def ingest(self, data: bytes, name: str = "ledger.pdf") -> uuid.UUID:
        with self.factories.transaction() as db:
            apply_rls_context(db, self.principal())
            received = receive_upload(
                db,
                self.principal(),
                store=self.store,
                keyring=self.keyring,
                chunks=iter([data]),
                max_bytes=MAX,
                filename=name,
                account_id=None,
            )
        self.run(INGEST_JOB_TYPE)
        return received.document_id

    def run(self, job_type: str) -> None:
        run_jobs(self.factories, self.household_id, job_type, done=self.done)

    def query(self, statement: Any) -> list[Any]:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            rows = list(db.execute(statement).scalars())
            db.expunge_all()
            return rows

    def document(self, document_id: uuid.UUID) -> Document:
        found: list[Document] = self.query(select(Document).where(Document.id == document_id))
        return found[0]

    def transactions(self, account: Account) -> list[Transaction]:
        return self.query(
            select(Transaction)
            .where(Transaction.account_id == account.id)
            .order_by(Transaction.posted_date, Transaction.id)
        )

    def open_reviews(self, document_id: uuid.UUID) -> list[ReviewItem]:
        return self.query(
            select(ReviewItem)
            .where(ReviewItem.document_id == document_id, ReviewItem.state == ReviewItemState.OPEN)
            .order_by(ReviewItem.created_at, ReviewItem.id)
        )

    def tombstones(self, account: Account) -> list[ImportTombstone]:
        return self.query(select(ImportTombstone).where(ImportTombstone.account_id == account.id))

    def teach(self, document_id: uuid.UUID, body: dict[str, Any]) -> TeachOut:
        with self.factories.transaction() as db:
            apply_rls_context(db, self.principal())
            return teach_service.teach(
                db, self.principal(), document_id, TeachRequest.model_validate(body), request=None
            )


@pytest.fixture
def env(
    factories: Factories,
    ledger: LedgerFactories,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Env]:
    local = settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )
    monkeypatch.setattr(ingest, "get_settings", lambda: local)
    ingest.default_deps.cache_clear()
    deps = ingest.default_deps()
    assert isinstance(deps.store, LocalFSBlobStore)
    with Actors.open(local, factories) as actors:
        yield Env(
            factories=factories,
            ledger=ledger,
            store=deps.store,
            keyring=deps.keyring,
            owner=factories.user(role="owner", with_totp=True),
            actors=actors,
        )
    ingest.default_deps.cache_clear()


def _month(
    last4: str,
    entries: Sequence[Entry],
    *,
    month: int = 3,
    first_day: int = 1,
    last_day: int = 30,
    source_format: str = "pdf",
) -> dict[str, Any]:
    """One month's statement as a parser stores it (2026; nothing printed to check it)."""
    start, end = date(2026, month, first_day), date(2026, month, last_day)
    return {
        "source_format": source_format,
        "account": {"last4": last4, "currency": "USD"},
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "transactions": [
            {"posted": date(2026, month, day).isoformat(), "amount": amount, "payee": payee}
            for day, amount, payee in entries
        ],
        "confidence": {"period": 1.0},
    }


def _extraction(statements: Sequence[dict[str, Any]], kind: str) -> dict[str, Any]:
    read = ExtractResult.model_validate({"kind": kind, "ok": True, "statements": list(statements)})
    return read.model_dump(mode="json")


def _read(
    env: Env,
    document_id: uuid.UUID,
    *statements: dict[str, Any],
    kind: str = "pdf",
    allow_overlap: Sequence[uuid.UUID] = (),
) -> dict[str, Any]:
    """Store ``statements`` as the document's reading and import it; the run's summary."""
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        document = db.get(Document, document_id)
        assert document is not None
        document.extraction = _extraction(statements, kind)
        db.flush()
        result = stored_result(document.extraction)
        assert result is not None
        outcome = import_extraction(db, document, result, allow_overlap=allow_overlap)
        document.status = outcome.status
        return outcome.summary


def _stored(
    env: Env,
    *statements: dict[str, Any],
    kind: str = "pdf",
    allow_overlap: Sequence[uuid.UUID] = (),
) -> tuple[uuid.UUID, dict[str, Any]]:
    """A document whose first reading holds ``statements``, imported; its id and summary."""
    with env.factories.transaction() as db:
        document = make_document(
            db,
            env.store,
            env.keyring,
            env.household_id,
            data=PDF + uuid.uuid4().hex.encode(),
            mime="application/pdf",
            filename="statement.pdf",
            uploaded_by=env.owner.user.id,
            status=DocumentStatus.NEEDS_REVIEW,
            extraction=_extraction(statements, kind),
        )
        document_id = document.id
    return document_id, _read(env, document_id, *statements, kind=kind, allow_overlap=allow_overlap)


def _by_payee(rows: Sequence[Transaction]) -> dict[str, Transaction]:
    return {row.raw_payee or "": row for row in rows}


def _ids(rows: Sequence[Transaction]) -> set[uuid.UUID]:
    return {row.id for row in rows}


def _first_and_second(env: Env, last4: str) -> tuple[Account, uuid.UUID, Transaction]:
    """An account, a document read with the bogus row, and that row."""
    account = env.ledger.account(env.household_id, number_last4=last4, currency="USD")
    document_id, summary = _stored(env, _month(last4, [*MARCH, BOGUS]))
    assert summary["imported"] == len(MARCH) + 1
    bogus = _by_payee(env.transactions(account))[BOGUS[2]]
    return account, document_id, bogus


def _delete(env: Env, row: Transaction) -> None:
    response = env.client().delete(f"{API}/transactions/{row.id}")
    assert response.status_code == 204, response.text


def _kept_items(env: Env, document_id: uuid.UUID) -> list[dict[str, Any]]:
    return [
        item.payload
        for item in env.open_reviews(document_id)
        if item.payload.get("reason") == "earlier_rows_kept"
    ]


# ---------------------------------------------------------------------------
# Untouched rows go; touched rows stay and are listed
# ---------------------------------------------------------------------------


def test_untouched_rows_the_new_reading_drops_are_removed(env: Env) -> None:
    account, document_id, bogus = _first_and_second(env, "4417")
    before = _ids(env.transactions(account))

    summary = _read(env, document_id, _month("4417", MARCH))

    after = env.transactions(account)
    assert _ids(after) == before - {bogus.id}
    assert (summary["replaced_rows"], summary["kept_rows"], summary["imported"]) == (1, 0, 0)
    assert env.tombstones(account) == []
    assert _kept_items(env, document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    audits: list[AuditLog] = env.query(
        select(AuditLog).where(
            AuditLog.action == "import.replace", AuditLog.target_id == str(document_id)
        )
    )
    assert len(audits) == 1
    assert audits[0].after is not None
    assert audits[0].after["replaced"] == 1
    assert audits[0].after["transaction_ids"] == [str(bogus.id)]
    assert BOGUS[2] not in str(audits[0].after)


def test_an_edited_row_is_kept_and_listed(env: Env) -> None:
    account, document_id, bogus = _first_and_second(env, "4418")
    owner = env.client()
    edited = owner.patch(f"{API}/transactions/{bogus.id}", json={"memo": "Asked the bank"})
    assert edited.status_code == 200, edited.text

    summary = _read(env, document_id, _month("4418", MARCH))

    rows = _by_payee(env.transactions(account))
    assert rows[BOGUS[2]].id == bogus.id
    assert rows[BOGUS[2]].memo == "Asked the bank"
    assert (summary["replaced_rows"], summary["kept_rows"]) == (0, 1)
    assert _kept_items(env, document_id) == [
        {
            "reason": "earlier_rows_kept",
            "account_id": str(account.id),
            "count": 1,
            "transactions": [{"transaction_id": str(bogus.id)}],
        }
    ]
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    # The same re-read asks once, not once per run.
    again = _read(env, document_id, _month("4418", MARCH))
    assert again["kept_rows"] == 1
    assert len(_kept_items(env, document_id)) == 1


def test_a_confirmed_a_categorised_and_a_transfer_row_are_kept(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="4419", currency="USD")
    savings = env.ledger.account(env.household_id, number_last4="4420", currency="USD")
    category = env.ledger.category(env.household_id, name="Odd lines")
    extras: list[Entry] = [
        (24, "-10.01", "Line to confirm"),
        (25, "-20.02", "Line to categorise"),
        (26, "-300.00", "Line moved to savings"),
        (27, "-40.04", "Line with a paper"),
        (28, "-50.05", "Line nobody saw"),
    ]
    document_id, _ = _stored(env, _month("4419", [*MARCH, *extras]))
    rows = _by_payee(env.transactions(account))
    other_leg = env.ledger.transaction(savings, amount="300.00", posted_date=date(2026, 3, 26))
    owner = env.client()
    confirmed = owner.post(
        f"{API}/transactions/bulk",
        json={"action": "confirm", "ids": [str(rows["Line to confirm"].id)]},
    )
    assert confirmed.status_code == 200, confirmed.text
    categorised = owner.patch(
        f"{API}/transactions/{rows['Line to categorise'].id}",
        json={"category_id": str(category.id)},
    )
    assert categorised.status_code == 200, categorised.text
    linked = owner.post(
        f"{API}/transactions/{rows['Line moved to savings'].id}/transfer",
        json={"counterpart_id": str(other_leg.id)},
    )
    assert linked.status_code in (200, 201, 204), linked.text
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        # A paper attached to a row leaves the row's own timestamps alone.
        db.add(
            DocumentLink(
                household_id=env.household_id,
                document_id=document_id,
                target_type=LinkTarget.TRANSACTION,
                target_id=rows["Line with a paper"].id,
            )
        )

    summary = _read(env, document_id, _month("4419", MARCH))

    after = _by_payee(env.transactions(account))
    kept = ["Line to confirm", "Line to categorise", "Line moved to savings", "Line with a paper"]
    assert [name for name in kept if name in after] == kept
    assert "Line nobody saw" not in after
    assert after["Line to confirm"].review_state is ReviewState.CONFIRMED
    assert after["Line to categorise"].category_id == category.id
    assert after["Line moved to savings"].transfer_group_id is not None
    assert (summary["replaced_rows"], summary["kept_rows"]) == (1, 4)
    (item,) = _kept_items(env, document_id)
    assert item["count"] == 4
    assert {entry["transaction_id"] for entry in item["transactions"]} == {
        str(after[name].id) for name in kept
    }


RESPELLED: list[Entry] = [
    (3, "-48.10", "ALDERBANK GROCERS 0303"),
    (9, "-17.25", "Fernhollow Cafe"),
    (14, "2150.00", "Wrenfield Payroll"),
    (21, "-63.40", "Mossgate Hardware"),
]


def test_a_row_read_with_another_description_is_replaced_not_doubled(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="4432", currency="USD")
    document_id, _ = _stored(env, _month("4432", MARCH))
    before = _by_payee(env.transactions(account))

    summary = _read(env, document_id, _month("4432", RESPELLED))

    after = _by_payee(env.transactions(account))
    assert sorted(after) == sorted(payee for _, _, payee in RESPELLED)
    assert after["ALDERBANK GROCERS 0303"].review_state is ReviewState.NONE
    assert after["ALDERBANK GROCERS 0303"].updated_at == after["ALDERBANK GROCERS 0303"].created_at
    assert after["Fernhollow Cafe"].id == before["Fernhollow Cafe"].id
    assert (summary["imported"], summary["replaced_rows"], summary["kept_rows"]) == (1, 1, 0)
    assert summary["possible_duplicates"] == 0
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED


def test_a_respelled_row_beside_a_changed_one_waits_as_a_near_duplicate(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="4433", currency="USD")
    document_id, _ = _stored(env, _month("4433", MARCH))
    old = _by_payee(env.transactions(account))["Alderbank Grocers"]
    edited = env.client().patch(f"{API}/transactions/{old.id}", json={"memo": "Weekly shop"})
    assert edited.status_code == 200, edited.text

    summary = _read(env, document_id, _month("4433", RESPELLED))

    after = _by_payee(env.transactions(account))
    assert after["Alderbank Grocers"].memo == "Weekly shop"
    new = after["ALDERBANK GROCERS 0303"]
    assert new.review_state is ReviewState.PENDING
    assert (summary["replaced_rows"], summary["kept_rows"]) == (0, 1)
    assert summary["possible_duplicates"] == 1
    found = {item.payload["reason"]: item.payload for item in env.open_reviews(document_id)}
    assert sorted(found) == ["earlier_rows_kept", "near_match"]
    assert found["near_match"]["transaction_id"] == str(new.id)
    assert found["near_match"]["candidates"] == [str(old.id)]


def test_a_kept_row_a_person_then_deletes_closes_its_finding(env: Env) -> None:
    account, document_id, bogus = _first_and_second(env, "4434")
    owner = env.client()
    edited = owner.patch(f"{API}/transactions/{bogus.id}", json={"memo": "Asked the bank"})
    assert edited.status_code == 200, edited.text
    _read(env, document_id, _month("4434", MARCH))
    assert len(_kept_items(env, document_id)) == 1
    _delete(env, bogus)

    summary = _read(env, document_id, _month("4434", MARCH))

    assert (summary["replaced_rows"], summary["kept_rows"]) == (0, 0)
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert len(env.transactions(account)) == len(MARCH)


def test_rows_of_another_document_on_the_account_are_untouched(env: Env) -> None:
    account, document_id, bogus = _first_and_second(env, "4421")
    april: list[Entry] = [(4, "-12.00", "Alderbank Grocers"), (18, "-7.70", "Fernhollow Cafe")]
    other_id, _ = _stored(env, _month("4421", april, month=4))
    theirs = _ids(env.query(select(Transaction).where(Transaction.source_document_id == other_id)))
    assert len(theirs) == 2

    summary = _read(env, document_id, _month("4421", MARCH))

    assert summary["replaced_rows"] == 1
    remaining = env.transactions(account)
    assert theirs <= _ids(remaining)
    assert bogus.id not in _ids(remaining)
    assert len(remaining) == len(MARCH) + 2


def test_rows_of_the_same_document_on_another_account_are_untouched(env: Env) -> None:
    first = env.ledger.account(env.household_id, number_last4="4422", currency="USD")
    second = env.ledger.account(env.household_id, number_last4="4423", currency="USD")
    theirs: list[Entry] = [(5, "-91.00", "Thornwick Utilities"), (27, "-311.20", "Month end sweep")]
    document_id, _ = _stored(env, _month("4422", [*MARCH, BOGUS]), _month("4423", theirs))
    before = _ids(env.transactions(second))
    assert len(before) == 2

    # Only the first account's statement reads differently.
    summary = _read(env, document_id, _month("4422", MARCH), _month("4423", theirs))

    assert _ids(env.transactions(second)) == before
    assert BOGUS[2] not in _by_payee(env.transactions(first))
    assert summary["replaced_rows"] == 1


def test_a_row_added_by_hand_for_the_document_is_never_replaced(env: Env) -> None:
    account, document_id, _ = _first_and_second(env, "4424")
    owner = env.client()
    added = owner.post(
        f"{API}/transactions",
        json={
            "account_id": str(account.id),
            "posted_date": "2026-03-29",
            "amount": {"amount": "-5.55", "currency": "USD"},
            "raw_payee": "Line typed in",
            "source_document_id": str(document_id),
        },
    )
    assert added.status_code == 201, added.text

    summary = _read(env, document_id, _month("4424", MARCH))

    rows = _by_payee(env.transactions(account))
    assert rows["Line typed in"].import_hash is None
    assert rows["Line typed in"].source_document_id == document_id
    assert BOGUS[2] not in rows
    assert (summary["replaced_rows"], summary["kept_rows"]) == (1, 0)
    assert _kept_items(env, document_id) == []


def test_nothing_is_replaced_while_a_statement_of_the_account_is_held(env: Env) -> None:
    account, document_id, _ = _first_and_second(env, "4425")
    april: list[Entry] = [(4, "-12.00", "Alderbank Grocers"), (18, "-7.70", "Fernhollow Cafe")]
    _stored(env, _month("4425", april, month=4))
    before = _ids(env.transactions(account))

    # The new reading takes the page for April: it overlaps the other file's
    # statement and is held; the March rows are not an earlier reading yet.
    summary = _read(env, document_id, _month("4425", april, month=4))

    assert summary["statement_overlaps"] == 1
    assert (summary["replaced_rows"], summary["kept_rows"], summary["imported"]) == (0, 0, 0)
    assert _ids(env.transactions(account)) == before
    reasons = [item.payload.get("reason") for item in env.open_reviews(document_id)]
    assert reasons == ["statement_overlap"]
    records: list[StatementRecord] = env.query(
        select(StatementRecord).where(StatementRecord.document_id == document_id)
    )
    assert [r.period_start for r in records] == [date(2026, 3, 1)]


# ---------------------------------------------------------------------------
# A deleted row stays deleted
# ---------------------------------------------------------------------------


def test_a_deleted_row_is_not_imported_again_on_reprocess(env: Env) -> None:
    account, document_id, bogus = _first_and_second(env, "4426")
    _delete(env, bogus)
    assert len(env.tombstones(account)) == 1

    summary = _read(env, document_id, _month("4426", [*MARCH, BOGUS]))

    assert BOGUS[2] not in _by_payee(env.transactions(account))
    assert (summary["tombstoned_rows"], summary["imported"]) == (1, 0)
    assert (summary["replaced_rows"], summary["kept_rows"]) == (0, 0)
    assert len(env.tombstones(account)) == 1
    assert env.open_reviews(document_id) == []


def test_a_deleted_row_is_not_imported_from_another_file(env: Env) -> None:
    account, _, bogus = _first_and_second(env, "4427")
    _delete(env, bogus)

    # The same month downloaded from the bank, the rows spelled the same.
    same = _month("4427", [*MARCH, BOGUS], source_format="ofx")
    _, summary = _stored(env, same, kind="ofx", allow_overlap=[account.id])

    assert (summary["tombstoned_rows"], summary["imported"]) == (1, 0)
    assert summary["duplicates"] == len(MARCH)
    assert BOGUS[2] not in _by_payee(env.transactions(account))

    # Another export spells the line differently: it has another hash, and
    # may be the deleted row or a second one of that amount. It waits.
    respelled = _month(
        "4427", [*MARCH, (BOGUS[0], BOGUS[1], "CARRIED FWD 0327")], source_format="ofx"
    )
    third_id, third = _stored(env, respelled, kind="ofx", allow_overlap=[account.id])

    row = _by_payee(env.transactions(account))["CARRIED FWD 0327"]
    assert row.review_state is ReviewState.PENDING
    assert (third["tombstoned_rows"], third["imported"], third["possible_duplicates"]) == (0, 1, 1)
    items = env.open_reviews(third_id)
    assert [(i.kind, i.payload["reason"]) for i in items] == [
        (ReviewKind.POSSIBLE_DUPLICATE, "near_match")
    ]
    assert items[0].payload["transaction_id"] == str(row.id)
    assert env.document(third_id).status is DocumentStatus.NEEDS_REVIEW


def test_one_of_two_identical_purchases_stays_deleted_through_a_reprocess(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="4428", currency="USD")
    twins: list[Entry] = [(11, "-4.75", "Fernhollow Cafe"), (11, "-4.75", "Fernhollow Cafe")]
    reading = _month("4428", [*MARCH, *twins])
    document_id, _ = _stored(env, reading)
    pair = [r for r in env.transactions(account) if r.amount == Decimal("-4.75")]
    assert len(pair) == 2
    _delete(env, pair[1])

    summary = _read(env, document_id, reading)

    left = [r for r in env.transactions(account) if r.amount == Decimal("-4.75")]
    assert [r.id for r in left] == [pair[0].id]
    assert left[0].review_state is ReviewState.NONE
    assert (summary["tombstoned_rows"], summary["imported"]) == (1, 0)
    assert (summary["possible_duplicates"], summary["replaced_rows"]) == (0, 0)
    assert env.open_reviews(document_id) == []


# ---------------------------------------------------------------------------
# Records and timestamps
# ---------------------------------------------------------------------------


def test_a_changed_period_leaves_one_statement_record(env: Env) -> None:
    account, document_id, _ = _first_and_second(env, "4429")

    _read(env, document_id, _month("4429", MARCH, first_day=2))

    records: list[StatementRecord] = env.query(
        select(StatementRecord).where(StatementRecord.document_id == document_id)
    )
    assert [(r.account_id, r.period_start) for r in records] == [(account.id, date(2026, 3, 2))]


def test_imported_rows_have_equal_timestamps_after_rules_ran(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="4430", currency="USD")
    category = env.ledger.category(env.household_id, name="Ironmongery")
    env.ledger.rule(
        env.household_id,
        conditions={"type": "payee", "op": "contains", "value": "mossgate"},
        actions=[{"type": "set_category", "category_id": str(category.id)}],
    )

    _, summary = _stored(env, _month("4430", MARCH))

    rows = env.transactions(account)
    assert summary["rules_changed"] == 1
    assert _by_payee(rows)["Mossgate Hardware"].category_id == category.id
    assert len(rows) == len(MARCH)
    assert [row.updated_at == row.created_at for row in rows] == [True] * len(rows)


# ---------------------------------------------------------------------------
# The reconcile view settles the kept rows
# ---------------------------------------------------------------------------


def test_kept_rows_are_an_earlier_reading_in_the_view_and_can_be_removed(env: Env) -> None:
    account, document_id, bogus = _first_and_second(env, "4431")
    owner = env.client()
    edited = owner.patch(f"{API}/transactions/{bogus.id}", json={"memo": "Looked at"})
    assert edited.status_code == 200, edited.text
    _read(env, document_id, _month("4431", MARCH))

    got = owner.get(f"{API}/documents/{document_id}/reconciliation")
    assert got.status_code == 200, got.text
    (statement,) = got.json()["statements"]
    assert statement["causes"][0]["kind"] == "earlier_reading"
    assert statement["causes"][0]["transaction_ids"] == [str(bogus.id)]

    removed = owner.post(
        f"{API}/documents/{document_id}/reconciliation/remove-earlier", json={"statement": 0}
    )

    assert removed.status_code == 200, removed.text
    after = removed.json()["statement"]
    assert [c["kind"] for c in after["causes"] if c["kind"] == "earlier_reading"] == []
    assert str(bogus.id) not in {r["transaction_id"] for r in after["rows"]}
    assert BOGUS[2] not in _by_payee(env.transactions(account))
    assert len(env.transactions(account)) == len(MARCH)
    # A person removed it: it stays out of every later reading.
    assert len(env.tombstones(account)) == 1
    assert _kept_items(env, document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    summary = _read(env, document_id, _month("4431", [*MARCH, BOGUS]))
    assert (summary["tombstoned_rows"], summary["imported"]) == (1, 0)


# ---------------------------------------------------------------------------
# Teaching a table on an imported statement
# ---------------------------------------------------------------------------


#: Rows the first sheet prints above its first subtotal line.
SHORT_ROWS = 8


def _words(document: Document) -> list[Word]:
    result = ExtractResult.model_validate(document.extraction)
    return next(p for p in result.pages if p.number == 1).words


def _table(document: Document, *, until: str) -> dict[str, Any]:
    """The first sheet's table, from its header down to the line that starts with ``until``."""
    words = _words(document)
    header = next(w for w in words if w.text == "Booked")
    stop = next(w for w in words if w.text == until)
    subtotal = next(w for w in words if w.text == "Subtotal")
    line = [w for w in words if w.line == subtotal.line]
    ignore = [
        min(w.bbox[0] for w in line) - 1,
        min(w.bbox[1] for w in line) - 1,
        max(w.bbox[2] for w in line) + 1,
        max(w.bbox[3] for w in line) + 1,
    ]
    return {
        "labels": [{"field": "ignore", "page": 1, "bbox": ignore}],
        "tables": [
            {
                "kind": "transactions",
                "page": 1,
                "bbox": [40.0, header.bbox[1] - 2, 580.0, stop.bbox[1] - 4],
                "columns": [
                    {"x0": 40.0, "x1": 140.0, "role": "date"},
                    {"x0": 140.0, "x1": 390.0, "role": "payee"},
                    {"x0": 390.0, "x1": 470.0, "role": "amount"},
                    {"x0": 470.0, "x1": 580.0, "role": "balance"},
                ],
            }
        ],
    }


def _document_rows(
    env: Env, account: Account, document_id: uuid.UUID
) -> list[tuple[date, Decimal, str]]:
    rows: list[Transaction] = env.query(
        select(Transaction).where(
            Transaction.account_id == account.id, Transaction.source_document_id == document_id
        )
    )
    return sorted((t.posted_date, t.amount, t.raw_payee or "") for t in rows)


def test_teaching_a_table_on_an_imported_statement_replaces_its_rows(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="2468", currency="USD")
    stmt = taught.statement_one()
    document_id = env.ingest(taught.pdf(stmt))
    document = env.document(document_id)
    printed = [(d, a.quantize(Decimal("0.000001")), p) for d, a, p in taught.expected(stmt)]

    full = env.teach(
        document_id, {**_table(document, until="Sheet"), "account_id": str(account.id)}
    )
    assert full.rerun == "import"
    assert "rows_already_imported" not in full.warnings
    env.run(APPLY_MAPPING_JOB_TYPE)
    assert _document_rows(env, account, document_id) == sorted(printed)
    before = _ids(env.transactions(account))

    # Taught again with the table drawn shorter: the reading has fewer rows,
    # and the rows only the first reading had go with it.
    again = env.teach(document_id, _table(env.document(document_id), until="Subtotal"))

    assert "rows_already_imported" in again.warnings
    assert again.rerun == "import"
    assert again.job_id is not None
    env.run(APPLY_MAPPING_JOB_TYPE)
    rows = _document_rows(env, account, document_id)
    assert set(printed[:SHORT_ROWS]) <= set(rows) < set(printed)
    assert printed[SHORT_ROWS] not in rows
    assert len(rows) == len(set(rows))
    assert _ids(env.transactions(account)) < before
    assert env.tombstones(account) == []
