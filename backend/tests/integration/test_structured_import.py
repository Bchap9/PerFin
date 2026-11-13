# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured imports end to end: upload -> worker ``ingest_document`` -> sandbox
-> structured importer -> ledger, for every format (INGESTION.md).

Files come from ``tests/fixtures/synth_statements.py`` (invented institutions,
merchants and numbers). Each test uploads through the real intake
(``receive_upload``: encrypted quarantine blob plus the queued job), then runs
the queued jobs through their registered handlers with the production
pipeline dependencies (``app.workers.handlers.ingest.default_deps``: real
sandbox, real structured importer) pointed at a temporary blob store.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel, HouseholdRole
from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.models import AuditLog, Event, Household, Job
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType, BalanceSnapshot, BalanceSource
from app.domain.accounts.schemas import AccountCreate
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentStatus,
    IngestJob,
    IngestStage,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports import proposals
from app.domain.imports import service as imports_service
from app.domain.imports.models import ImportMapping, StatementRecord
from app.domain.imports.schemas import (
    APPLY_MAPPING_JOB_TYPE,
    AccountChoice,
    DetectedAccount,
    DocumentAccountsApply,
    MappingSpec,
)
from app.domain.transactions.models import ReviewState, Transaction, TransactionSplit
from app.domain.transactions.schemas import AUTO_PAIR_JOB_TYPE
from app.ingestion import pipeline
from app.ingestion.extract.delimited import extract_delimited
from app.ingestion.intake.upload import receive_upload
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from app.workers.handlers import ingest
from tests.fixtures import synth_csv_layouts as layouts
from tests.fixtures import synth_ofx_variants as ofx_variants
from tests.fixtures.factories import Factories, UserRecord, without_ids
from tests.fixtures.households import Actors, problem_type
from tests.fixtures.imports import (
    make_extracted_document,
    make_import_mapping,
    run_jobs,
    table_result,
)
from tests.fixtures.ledger import LedgerFactories, make_category, make_rule
from tests.fixtures.synth_activity_csv import (
    LEDGER_HEADER,
    ActivityAccount,
    ActivityRow,
    activity_accounts,
    activity_csv_bytes,
    activity_rows,
)
from tests.fixtures.synth_activity_csv import (
    SPEC as ACTIVITY_SPEC,
)
from tests.fixtures.synth_statements import (
    INSTITUTIONS,
    LAYOUTS,
    SynthStatement,
    csv_bytes,
    layout_statement,
    ofx_sgml_bytes,
    ofx_xml_bytes,
    qif_bytes,
    sample_statement,
    statement,
    synth_ledger,
)

pytestmark = pytest.mark.integration

API = "/api/v1"
MAX = 10 * 1024 * 1024


@dataclass
class Env:
    """One household's import world: blob store, keyring, the job runner."""

    factories: Factories
    ledger: LedgerFactories
    store: LocalFSBlobStore
    keyring: Keyring
    settings: Settings
    owner: UserRecord
    done: set[uuid.UUID] = field(default_factory=set)

    @property
    def household_id(self) -> uuid.UUID:
        assert self.owner.household_id is not None
        return self.owner.household_id

    def principal(self, record: UserRecord | None = None) -> Principal:
        record = record or self.owner
        return Principal(
            user=record.user,
            household_id=record.household_id,
            role=record.role,
            auth_method="session",
        )

    def upload(
        self,
        data: bytes,
        *,
        account: Account | None = None,
        as_user: UserRecord | None = None,
        name: str = "statement.dat",
    ) -> uuid.UUID:
        principal = self.principal(as_user)
        with self.factories.transaction() as db:
            apply_rls_context(db, principal)
            received = receive_upload(
                db,
                principal,
                store=self.store,
                keyring=self.keyring,
                chunks=iter([data]),
                max_bytes=MAX,
                filename=name,
                account_id=account.id if account is not None else None,
            )
            return received.document_id

    def run(self, job_type: str) -> list[uuid.UUID]:
        return run_jobs(self.factories, self.household_id, job_type, done=self.done)

    def ingest(self, data: bytes, **kwargs: Any) -> uuid.UUID:
        document_id = self.upload(data, **kwargs)
        assert self.run(INGEST_JOB_TYPE), "the upload queued no ingest job"
        return document_id

    def db(self) -> Iterator[Session]:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            yield db

    def document(self, document_id: uuid.UUID) -> Document:
        for db in self.db():
            found = db.get(Document, document_id)
            assert found is not None
            db.expunge(found)
            return found
        raise AssertionError  # pragma: no cover

    def transactions(self, account: Account) -> list[Transaction]:
        for db in self.db():
            rows = list(
                db.execute(
                    select(Transaction)
                    .where(Transaction.account_id == account.id)
                    .order_by(Transaction.posted_date, Transaction.id)
                ).scalars()
            )
            db.expunge_all()
            return rows
        raise AssertionError  # pragma: no cover

    def reviews(self, document_id: uuid.UUID) -> list[ReviewItem]:
        for db in self.db():
            rows = list(
                db.execute(
                    select(ReviewItem)
                    .where(ReviewItem.document_id == document_id)
                    .order_by(ReviewItem.created_at, ReviewItem.id)
                ).scalars()
            )
            db.expunge_all()
            return rows
        raise AssertionError  # pragma: no cover

    def open_reviews(self, document_id: uuid.UUID) -> list[ReviewItem]:
        return [r for r in self.reviews(document_id) if r.state is ReviewItemState.OPEN]

    def snapshots(self, account: Account) -> list[BalanceSnapshot]:
        for db in self.db():
            rows = list(
                db.execute(
                    select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id)
                ).scalars()
            )
            db.expunge_all()
            return rows
        raise AssertionError  # pragma: no cover

    def checking(self, stmt: SynthStatement, **kwargs: Any) -> Account:
        institution = self.ledger.institution(self.household_id, name=stmt.institution.name)
        kwargs.setdefault(
            "type", AccountType.CREDIT_CARD if stmt.account_type == "creditcard" else "checking"
        )
        kwargs.setdefault("currency", stmt.currency)
        return self.ledger.account(
            self.household_id,
            number_last4=stmt.last4,
            institution_id=institution.id,
            **kwargs,
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
    # The production dependencies (sandbox and importer included), on tmp dirs.
    monkeypatch.setattr(ingest, "get_settings", lambda: local)
    ingest.default_deps.cache_clear()
    deps = ingest.default_deps()
    assert deps.importer is pipeline.structured_import
    assert isinstance(deps.store, LocalFSBlobStore)
    yield Env(
        factories=factories,
        ledger=ledger,
        store=deps.store,
        keyring=deps.keyring,
        settings=local,
        owner=factories.user(role="owner", with_totp=True),
    )
    ingest.default_deps.cache_clear()


def _amounts(rows: list[Transaction]) -> list[tuple[date, Decimal]]:
    return sorted((t.posted_date, t.amount) for t in rows)


def _expected(stmt: SynthStatement) -> list[tuple[date, Decimal]]:
    return sorted((t.posted, t.amount) for t in stmt.transactions)


# ---------------------------------------------------------------------------
# Every format through the worker
# ---------------------------------------------------------------------------


def test_ofx_import_writes_transactions_snapshot_rules_audit_and_events(env: Env) -> None:
    stmt = sample_statement(21)
    account = env.checking(stmt)
    salary = env.ledger.run(make_category, env.household_id, name="Salary (synthetic)")
    env.ledger.run(
        make_rule,
        env.household_id,
        conditions={"type": "payee", "op": "contains", "value": "payroll"},
        actions=[{"type": "set_category", "category_id": str(salary.id)}],
    )
    document_id = env.ingest(ofx_sgml_bytes(stmt))

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED
    assert document.account_id == account.id
    assert document.institution_id == account.institution_id
    rows = env.transactions(account)
    assert _amounts(rows) == _expected(stmt)
    assert all(r.source_document_id == document_id for r in rows)
    assert all(r.import_hash and len(r.import_hash) == 64 for r in rows)
    assert len({r.import_hash for r in rows}) == len(rows)  # two coffees, two hashes
    assert all(r.review_state is ReviewState.NONE for r in rows)
    assert {r.category_id for r in rows if r.raw_payee == "Initech Payroll"} == {salary.id}
    (snapshot,) = env.snapshots(account)
    assert snapshot.source is BalanceSource.STATEMENT
    assert (snapshot.as_of_date, snapshot.balance) == (stmt.end, stmt.closing_balance)
    assert snapshot.source_document_id == document_id
    assert env.open_reviews(document_id) == []
    for db in env.db():
        events = {e.type for e in db.execute(select(Event)).scalars()}
        actions = {a.action for a in db.execute(select(AuditLog)).scalars()}
        jobs = {j.type for j in db.execute(select(Job)).scalars()}
        imported = db.execute(
            select(AuditLog).where(AuditLog.action == "transaction.import")
        ).scalar_one()
        # The trail lists the new transactions' ids (masked: UUID hex can hold
        # any four digits).
        trail = without_ids(imported.after)
        assert stmt.account_number[-4:] not in trail
        assert "Initech" not in trail  # no statement text in the trail
    assert {"transaction.created", "balance.updated", "document.updated"} <= events
    assert {"transaction.import", "document.ingest", "rule.apply"} <= actions
    assert AUTO_PAIR_JOB_TYPE in jobs


@pytest.mark.parametrize(
    ("fmt", "credit_card"),
    [("ofx-xml", False), ("ofx-xml", True), ("qfx", True), ("ofx", True)],
)
def test_ofx_family_imports(env: Env, fmt: str, credit_card: bool) -> None:
    stmt = sample_statement(22, credit_card=credit_card)
    account = env.checking(stmt)
    data = ofx_xml_bytes(stmt) if fmt == "ofx-xml" else ofx_sgml_bytes(stmt, qfx=fmt == "qfx")
    document_id = env.ingest(data)
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert _amounts(env.transactions(account)) == _expected(stmt)
    (snapshot,) = env.snapshots(account)
    assert snapshot.balance == stmt.closing_balance
    if credit_card:
        assert snapshot.balance < 0  # a card's balance is owed


def test_qif_import_uses_the_upload_account_categories_and_splits(env: Env) -> None:
    stmt = sample_statement(23)
    account = env.ledger.account(env.household_id)  # QIF names no account number
    housing = env.ledger.run(make_category, env.household_id, name="Housing")
    rent = env.ledger.run(make_category, env.household_id, name="Rent", parent=housing)
    coffee_parent = env.ledger.run(make_category, env.household_id, name="Food")
    coffee = env.ledger.run(make_category, env.household_id, name="Coffee", parent=coffee_parent)
    fees = env.ledger.run(make_category, env.household_id, name="Bank Fees")
    # A rule that would re-categorise everything must not override the file's categories.
    other = env.ledger.run(make_category, env.household_id, name="Other (rule)")
    env.ledger.run(
        make_rule,
        env.household_id,
        conditions={"type": "all", "conditions": []},
        actions=[{"type": "set_category", "category_id": str(other.id)}],
    )
    document_id = env.ingest(qif_bytes(stmt), account=account)
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    rows = env.transactions(account)
    assert _amounts(rows) == _expected(stmt)
    by_payee = {r.raw_payee: r for r in rows}
    assert by_payee["Quiet Bean Coffee"].category_id == coffee.id
    assert (
        by_payee["Initech Payroll"].category_id == other.id
    )  # its file category is unknown here: rules ran
    rent_row = by_payee["Oakfield Rentals"]
    assert rent_row.category_id is None
    for db in env.db():
        splits = list(
            db.execute(
                select(TransactionSplit).where(TransactionSplit.transaction_id == rent_row.id)
            ).scalars()
        )
        assert sorted((s.category_id, s.amount) for s in splits) == sorted(
            [(rent.id, Decimal("-1400.00")), (fees.id, Decimal("-50.00"))]
        )


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_csv_layouts_import_with_a_saved_mapping(
    env: Env, layout: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The mapping-editor path: importing confident suggestions on their own
    # is switched off here (tested below).
    monkeypatch.setattr(imports_service, "AUTO_MAPPING_KINDS", frozenset())
    history = synth_ledger(24, days=59)
    stmt = layout_statement(
        statement(history, date(2026, 1, 1), date(2026, 1, 31), seed=24), layout
    )
    account = env.ledger.account(env.household_id, currency=stmt.currency)
    data = csv_bytes(stmt, layout)
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        # 1. No mapping yet: the document waits for one.
        first = env.ingest(data, account=account, name=f"{layout}.csv")
        assert env.document(first).status is DocumentStatus.NEEDS_REVIEW
        (item,) = env.open_reviews(first)
        assert item.kind is ReviewKind.UNPARSED
        assert item.payload["reason"] == "mapping_required"
        assert env.transactions(account) == []
        # 2. The mapping editor reads the stored table and saves the mapping.
        preview = owner.get(f"{API}/imports/{first}/preview").json()
        (table,) = preview["tables"]
        assert table["rows"][LAYOUTS[layout].spec.get("skip_rows", 0)] == list(
            LAYOUTS[layout].header
        )
        saved = owner.post(
            f"{API}/imports/mappings",
            json={
                "name": f"Synthetic {layout}",
                "spec": LAYOUTS[layout].spec,
                "document_id": str(first),
            },
        )
        assert saved.status_code == 201, saved.text
        mapping_id = saved.json()["id"]
        # 3. Re-run: imported, the review item answered.
        applied = owner.post(
            f"{API}/imports/{first}/apply-mapping", json={"mapping_id": mapping_id}
        )
        assert applied.status_code == 202, applied.text
        assert env.run(APPLY_MAPPING_JOB_TYPE)
        assert env.document(first).status is DocumentStatus.PROCESSED
        assert [r.state for r in env.reviews(first)] == [ReviewItemState.RESOLVED]
        assert _amounts(env.transactions(account)) == _expected(stmt)
        assert owner.get(f"{API}/imports/{first}/preview").json()["last_import"]["imported"] == len(
            stmt.transactions
        )
        if LAYOUTS[layout].spec.get("balance_column") is not None:
            (snapshot,) = env.snapshots(account)
            assert snapshot.balance == stmt.closing_balance
        # 4. The next file of this layout is imported without anyone (fingerprint).
        later = layout_statement(
            statement(history, date(2026, 2, 1), date(2026, 2, 28), seed=24), layout
        )
        second = env.ingest(csv_bytes(later, layout), account=account)
        assert env.document(second).status is DocumentStatus.PROCESSED
        assert len(env.transactions(account)) == len(stmt.transactions) + len(later.transactions)
        assert owner.get(f"{API}/imports/mappings/{mapping_id}").json()["use_count"] == 2


def test_mapping_with_an_account_routes_files_without_one(env: Env) -> None:
    stmt = sample_statement(26)
    account = env.ledger.account(env.household_id)
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        make_import_mapping(
            db,
            env.household_id,
            spec=LAYOUTS["us_signed"].spec,
            rows=[list(LAYOUTS["us_signed"].header)],
            account=db.get(Account, account.id),
        )
    document_id = env.ingest(csv_bytes(stmt, "us_signed"))
    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED
    assert document.account_id == account.id
    assert _amounts(env.transactions(account)) == _expected(stmt)


# ---------------------------------------------------------------------------
# A statement that lost records says so
# ---------------------------------------------------------------------------


def _variant_account(env: Env) -> Account:
    """The checking account the files of ``synth_ofx_variants`` name."""
    return env.ledger.account(env.household_id, number_last4=ofx_variants.ACCOUNT[-4:])


def _skipped_items(env: Env, document_id: uuid.UUID) -> list[ReviewItem]:
    return [r for r in env.open_reviews(document_id) if r.payload.get("reason") == "lines_skipped"]


def test_ofx_with_skipped_records_raises_lines_skipped(env: Env) -> None:
    account = _variant_account(env)
    document_id = env.ingest(ofx_variants.sgml_with_unreadable_amount())

    read = [r for i, r in enumerate(ofx_variants.ROWS) if i != ofx_variants.UNREADABLE_ROW]
    assert _amounts(env.transactions(account)) == sorted((day, amount) for day, amount, _ in read)
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.LOW_CONFIDENCE_FIELD
    assert item.payload == {"reason": "lines_skipped", "count": 1, "source": "extraction"}


def test_statement_with_transactions_and_no_rows_is_not_processed(env: Env) -> None:
    account = _variant_account(env)
    body = ofx_variants.sgml_body()
    for _, amount, _ in ofx_variants.ROWS:
        body = body.replace(f"<TRNAMT>{amount}\n", "<TRNAMT>n/a\n")
    document_id = env.ingest((ofx_variants.SGML_HEADER + body).encode("ascii"))

    assert env.transactions(account) == []
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = _skipped_items(env, document_id)
    assert item.payload["count"] == len(ofx_variants.ROWS)
    assert item.payload["source"] == "extraction"


def test_each_statement_of_a_file_adds_its_skipped_records_to_one_item(env: Env) -> None:
    """Two statements losing one record each: one item for the document, of two."""
    account = _variant_account(env)
    body = ofx_variants.sgml_with_unreadable_amount().decode("ascii")
    body = body.removeprefix(ofx_variants.SGML_HEADER)
    end = "</BANKMSGSRSV1>\n"
    start, stop = body.index("<BANKMSGSRSV1>"), body.index(end) + len(end)
    block = body[start:stop]
    # The second statement is a later month, so its rows are not the first's again.
    twice = body[:stop] + block.replace("202603", "202604") + body[stop:]
    document_id = env.ingest((ofx_variants.SGML_HEADER + twice).encode("ascii"))

    (item,) = _skipped_items(env, document_id)
    assert item.payload == {"reason": "lines_skipped", "count": 2, "source": "extraction"}
    assert len(env.transactions(account)) == 4
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW


def test_qif_with_a_record_without_an_amount_raises_lines_skipped(env: Env) -> None:
    account = env.ledger.account(env.household_id)  # QIF names no account number
    data = (
        "!Type:Bank\n"
        "D03/04/2026\nT-12.40\nPHarbor Lane Bakery\n^\n"
        "D03/11/2026\nPQuire Stationers\n^\n"
        "D03/19/2026\nT300.00\nPNorthfield Payroll\n^\n"
    ).encode("ascii")
    document_id = env.ingest(data, account=account, name="export.qif")

    assert len(env.transactions(account)) == 2
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert item.payload == {"reason": "lines_skipped", "count": 1, "source": "extraction"}


def test_rerunning_a_statement_that_lost_records_does_not_repeat_the_item(env: Env) -> None:
    account = _variant_account(env)
    document_id = env.ingest(ofx_variants.sgml_with_unreadable_amount())
    for db in env.db():
        document = db.get(Document, document_id)
        assert document is not None
        result = ExtractResult.model_validate(document.extraction)
        outcome = imports_service.import_extraction(db, document, result)
        assert outcome.status is DocumentStatus.NEEDS_REVIEW
    assert len(_skipped_items(env, document_id)) == 1
    assert len(env.transactions(account)) == 2


def test_clean_ofx_raises_nothing(env: Env) -> None:
    stmt = sample_statement(38)
    account = env.checking(stmt)
    document_id = env.ingest(ofx_sgml_bytes(stmt))
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.reviews(document_id) == []
    assert _amounts(env.transactions(account)) == _expected(stmt)

    variant = _variant_account(env)
    clean = (ofx_variants.SGML_HEADER + ofx_variants.sgml_body()).encode("ascii")
    document_id = env.ingest(clean)
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.reviews(document_id) == []
    assert len(env.transactions(variant)) == len(ofx_variants.ROWS)


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def test_reimport_is_idempotent(env: Env) -> None:
    stmt = sample_statement(27)
    account = env.checking(stmt)
    first = env.ingest(ofx_sgml_bytes(stmt))
    count = len(env.transactions(account))
    # The same statement again, as different bytes (OFX 2 instead of 1): the
    # statement guard holds it (statement_overlap) and imports nothing.
    second = env.ingest(ofx_xml_bytes(stmt))
    assert env.document(second).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(second)
    assert (item.kind, item.payload["reason"]) == (
        ReviewKind.POSSIBLE_DUPLICATE,
        "statement_overlap",
    )
    assert item.payload["other_document_id"] == str(first)
    assert (item.payload["period_start"], item.payload["period_end"]) == (
        stmt.start.isoformat(),
        stmt.end.isoformat(),
    )
    assert len(env.transactions(account)) == count
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        # "Same statement": skipped, the document is done.
        resolved = owner.post(
            f"{API}/review/{item.id}/resolve",
            json={"kind": "possible_duplicate", "decision": "confirm"},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["resolution"]["skipped"] is True
        assert env.document(second).status is DocumentStatus.PROCESSED
        # Re-running either document's import changes nothing and asks nothing.
        for document_id in (first, second):
            rerun = owner.post(f"{API}/imports/{document_id}/apply-mapping", json={})
            assert rerun.status_code == 202
    assert len(env.run(APPLY_MAPPING_JOB_TYPE)) == 2
    assert len(env.transactions(account)) == count
    assert env.document(first).status is DocumentStatus.PROCESSED
    assert env.document(second).status is DocumentStatus.PROCESSED
    assert env.open_reviews(second) == []
    for db in env.db():
        logs: list[dict[str, Any]] = [
            a.after or {}
            for a in db.execute(
                select(AuditLog)
                .where(AuditLog.action.in_(["document.ingest", "document.import"]))
                .order_by(AuditLog.seq)
            ).scalars()
        ]
    assert [(log["imported"], log["duplicates"], log["statement_overlaps"]) for log in logs] == [
        (count, 0, 0),
        (0, 0, 1),
        (0, count, 0),
        (0, 0, 1),
    ]


def test_overlapping_statements_are_handled_by_hash(env: Env) -> None:
    ledger = synth_ledger(28, days=75)
    jan = statement(ledger, date(2026, 1, 1), date(2026, 1, 31), seed=28)
    overlap = statement(ledger, date(2026, 1, 20), date(2026, 3, 10), seed=28)
    account = env.checking(jan)
    env.ingest(ofx_sgml_bytes(jan))
    second = env.ingest(ofx_sgml_bytes(overlap))
    assert env.document(second).status is DocumentStatus.PROCESSED
    union = sorted(set(jan.transactions) | set(overlap.transactions), key=lambda t: t.fitid)
    assert _amounts(env.transactions(account)) == sorted((t.posted, t.amount) for t in union)
    # Two snapshots, one per statement end.
    assert sorted((s.as_of_date, s.balance) for s in env.snapshots(account)) == [
        (jan.end, jan.closing_balance),
        (overlap.end, overlap.closing_balance),
    ]


def _records(env: Env, account: Account) -> list[StatementRecord]:
    for db in env.db():
        rows = list(
            db.execute(
                select(StatementRecord)
                .where(StatementRecord.account_id == account.id)
                .order_by(StatementRecord.created_at, StatementRecord.id)
            ).scalars()
        )
        db.expunge_all()
        return rows
    raise AssertionError  # pragma: no cover


def test_overlapping_statement_imported_anyway(env: Env) -> None:
    """``statement_overlap`` -> "import anyway": imported, row hashes still dedupe."""
    stmt = sample_statement(31)
    account = env.checking(stmt)
    first = env.ingest(ofx_sgml_bytes(stmt))
    count = len(env.transactions(account))
    (record,) = _records(env, account)
    assert (record.document_id, record.kind, record.outcome) == (first, "bank", "imported")
    assert (record.period_start, record.period_end, record.period_stated) == (
        stmt.start,
        stmt.end,
        True,
    )
    assert (record.source_format, record.currency) == ("ofx", stmt.currency)
    assert record.closing_balance == stmt.closing_balance

    second = env.ingest(ofx_xml_bytes(stmt))
    (item,) = env.open_reviews(second)
    assert item.payload["reason"] == "statement_overlap"
    assert item.payload["statement"] == 0
    assert item.payload["investment"] is False
    assert [o["document_id"] for o in item.payload["others"]] == [str(first)]
    assert len(env.transactions(account)) == count
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        resolved = owner.post(
            f"{API}/review/{item.id}/resolve",
            json={"kind": "possible_duplicate", "decision": "reject"},
        )
        assert resolved.status_code == 200, resolved.text
        resolution = resolved.json()["resolution"]
        assert resolution["import_anyway"] is True
        # The same shape as "import as read" (app.domain.review.service).
        assert (resolution["account_id"], resolution["target"], resolution["statement"]) == (
            str(account.id),
            "statement",
            0,
        )
    assert env.run(APPLY_MAPPING_JOB_TYPE)
    assert env.document(second).status is DocumentStatus.PROCESSED
    assert env.open_reviews(second) == []
    # The rows were already recorded: nothing doubled.
    assert len(env.transactions(account)) == count
    assert [(r.document_id, r.outcome) for r in _records(env, account)] == [
        (first, "imported"),
        (second, "imported"),
    ]


def test_statement_overlap_needs_edit_on_the_account(env: Env) -> None:
    stmt = sample_statement(32)
    account = env.checking(stmt)
    env.ingest(ofx_sgml_bytes(stmt))
    second = env.ingest(ofx_xml_bytes(stmt))
    (item,) = env.open_reviews(second)
    # A member who may only view the account (and did not upload the files).
    member = env.factories.user(
        role=HouseholdRole.MEMBER, household=env.owner.household, with_totp=True
    )
    env.ledger.grant(env.household_id, member.user.id, account, GrantLevel.VIEW)
    with Actors.open(env.settings, env.factories) as actors:
        reader = actors.login(member)
        refused = reader.post(
            f"{API}/review/{item.id}/resolve",
            json={"kind": "possible_duplicate", "decision": "confirm"},
        )
        assert refused.status_code in (403, 404), refused.text
    assert [r.state for r in env.reviews(second)] == [ReviewItemState.OPEN]
    assert len(_records(env, account)) == 1


def test_statements_sharing_only_their_edges_are_not_held(env: Env) -> None:
    """Consecutive statements that share a closing day, exports that overlap: hashes only."""
    ledger = synth_ledger(33, days=75)
    jan = statement(ledger, date(2026, 1, 1), date(2026, 1, 31), seed=33)
    feb = statement(ledger, date(2026, 1, 31), date(2026, 2, 28), seed=33)
    account = env.checking(jan)
    env.ingest(ofx_sgml_bytes(jan))
    second = env.ingest(ofx_sgml_bytes(feb))
    assert env.document(second).status is DocumentStatus.PROCESSED, env.open_reviews(second)
    assert len(_records(env, account)) == 2


def test_rolling_ofx_download_windows_are_not_held(env: Env) -> None:
    """A bank's "last 90 days" OFX downloads overlap by design: the row hashes handle them."""
    ledger = synth_ledger(36, days=125)
    first = statement(ledger, date(2026, 1, 1), date(2026, 3, 31), seed=36)
    later = statement(ledger, date(2026, 2, 1), date(2026, 4, 30), seed=36)
    account = env.checking(first)
    env.ingest(ofx_sgml_bytes(first))
    second = env.ingest(ofx_sgml_bytes(later))
    assert env.document(second).status is DocumentStatus.PROCESSED, env.open_reviews(second)
    union = sorted(set(first.transactions) | set(later.transactions), key=lambda t: t.fitid)
    assert _amounts(env.transactions(account)) == sorted((t.posted, t.amount) for t in union)
    assert [r.outcome for r in _records(env, account)] == ["imported", "imported"]


def test_same_statement_answer_can_be_taken_back(env: Env) -> None:
    """``allow_overlap`` imports a statement recorded as skipped ("same statement")."""
    stmt = sample_statement(37)
    account = env.checking(stmt)
    first = env.ingest(ofx_sgml_bytes(stmt))
    count = len(env.transactions(account))
    second = env.ingest(ofx_xml_bytes(stmt))
    (item,) = env.open_reviews(second)
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        resolved = owner.post(
            f"{API}/review/{item.id}/resolve",
            json={"kind": "possible_duplicate", "decision": "confirm"},
        )
        assert resolved.status_code == 200, resolved.text
        assert [(r.document_id, r.outcome) for r in _records(env, account)] == [
            (first, "imported"),
            (second, "skipped"),
        ]
        rerun = owner.post(
            f"{API}/imports/{second}/apply-mapping", json={"allow_overlap": [str(account.id)]}
        )
        assert rerun.status_code == 202, rerun.text
    assert env.run(APPLY_MAPPING_JOB_TYPE)
    assert env.document(second).status is DocumentStatus.PROCESSED
    assert env.open_reviews(second) == []
    assert [(r.document_id, r.outcome) for r in _records(env, account)] == [
        (first, "imported"),
        (second, "imported"),
    ]
    # The rows were already recorded: nothing doubled.
    assert len(env.transactions(account)) == count


def test_overlapping_csv_exports_are_recorded_not_held(env: Env) -> None:
    history = synth_ledger(34, days=70)
    january = statement(history, date(2026, 1, 1), date(2026, 1, 31), seed=34)
    window = statement(history, date(2026, 1, 10), date(2026, 2, 20), seed=34)
    account = env.ledger.account(env.household_id, currency=january.currency)
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        make_import_mapping(
            db,
            env.household_id,
            spec=LAYOUTS["us_signed"].spec,
            rows=[list(LAYOUTS["us_signed"].header)],
            account=db.get(Account, account.id),
        )
    env.ingest(csv_bytes(january, "us_signed"))
    second = env.ingest(csv_bytes(window, "us_signed"))
    assert env.document(second).status is DocumentStatus.PROCESSED, env.open_reviews(second)
    records = _records(env, account)
    assert [(r.source_format, r.period_stated) for r in records] == [
        ("csv", False),
        ("csv", False),
    ]
    union = set(january.transactions) | set(window.transactions)
    assert _amounts(env.transactions(account)) == sorted((t.posted, t.amount) for t in union)


def test_near_duplicates_go_to_review(env: Env) -> None:
    stmt = sample_statement(29)
    account = env.checking(stmt)
    target = next(t for t in stmt.transactions if t.payee == "Initech Payroll")
    manual = env.ledger.transaction(
        account, amount=target.amount, posted_date=target.posted, raw_payee="Salary (typed by hand)"
    )
    document_id = env.ingest(ofx_sgml_bytes(stmt))
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.POSSIBLE_DUPLICATE
    assert item.payload["candidates"] == [str(manual.id)]
    rows = env.transactions(account)
    assert len(rows) == len(stmt.transactions) + 1
    flagged = [r for r in rows if r.review_state is ReviewState.PENDING]
    assert [str(r.id) for r in flagged] == [item.payload["transaction_id"]]
    assert flagged[0].raw_payee == "Initech Payroll"


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def test_unknown_account_waits_for_a_choice(env: Env) -> None:
    stmt = sample_statement(30)
    account = env.ledger.account(env.household_id)  # no last four digits on file
    document_id = env.ingest(ofx_sgml_bytes(stmt))
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.UNKNOWN_ACCOUNT
    assert item.payload["reason"] == "account_not_found"
    assert stmt.last4 not in str(item.payload)
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        response = owner.post(
            f"{API}/imports/{document_id}/apply-mapping", json={"account_id": str(account.id)}
        )
        assert response.status_code == 202, response.text
    env.run(APPLY_MAPPING_JOB_TYPE)
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert env.document(document_id).account_id == account.id
    assert _amounts(env.transactions(account)) == _expected(stmt)
    assert [r.state for r in env.reviews(document_id)] == [ReviewItemState.RESOLVED]
    assert env.reviews(document_id)[0].resolved_by == env.owner.user.id
    # The re-run queued the transfer matcher too.
    assert env.run(AUTO_PAIR_JOB_TYPE)


def test_institution_breaks_a_last4_tie(env: Env) -> None:
    stmt = sample_statement(31)
    right = env.checking(stmt)
    other_bank = env.ledger.institution(env.household_id, name=INSTITUTIONS[1].name)
    env.ledger.account(env.household_id, number_last4=stmt.last4, institution_id=other_bank.id)
    document_id = env.ingest(ofx_sgml_bytes(stmt))
    assert env.document(document_id).account_id == right.id
    assert _amounts(env.transactions(right)) == _expected(stmt)


def test_same_last4_without_institution_is_ambiguous(env: Env) -> None:
    stmt = sample_statement(32)
    a = env.ledger.account(env.household_id, number_last4=stmt.last4)
    b = env.ledger.account(env.household_id, number_last4=stmt.last4)
    document_id = env.ingest(ofx_sgml_bytes(stmt))
    (item,) = env.open_reviews(document_id)
    assert item.payload["reason"] == "account_ambiguous"
    assert sorted(item.payload["candidates"]) == sorted([str(a.id), str(b.id)])
    assert env.transactions(a) == env.transactions(b) == []


def test_member_upload_never_lands_on_a_hidden_account(env: Env) -> None:
    stmt = sample_statement(33)
    hidden = env.checking(stmt)
    member = env.factories.user(role="member", with_totp=True, household=_household(env))
    mine = env.ledger.account(env.household_id)
    env.ledger.grant(env.household_id, member.user.id, mine, GrantLevel.EDIT)
    document_id = env.ingest(ofx_sgml_bytes(stmt), as_user=member)
    document = env.document(document_id)
    assert document.account_id is None
    assert document.status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.UNKNOWN_ACCOUNT
    assert item.payload["candidates"] == []  # the hidden account is not even named
    assert env.transactions(hidden) == []
    with Actors.open(env.settings, env.factories) as actors:
        client = actors.login(member)
        refused = client.post(
            f"{API}/imports/{document_id}/apply-mapping", json={"account_id": str(hidden.id)}
        )
        assert refused.status_code == 404
        assert problem_type(refused) == "account-not-found"
        accepted = client.post(
            f"{API}/imports/{document_id}/apply-mapping", json={"account_id": str(mine.id)}
        )
        assert accepted.status_code == 202
    env.run(APPLY_MAPPING_JOB_TYPE)
    assert _amounts(env.transactions(mine)) == _expected(stmt)
    assert env.transactions(hidden) == []


def test_statement_in_another_currency_is_not_imported(env: Env) -> None:
    stmt = replace(sample_statement(34), currency="EUR")
    account = env.checking(replace(stmt, currency="USD"))
    document_id = env.ingest(ofx_sgml_bytes(stmt))
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.UNKNOWN_ACCOUNT
    assert item.payload["reason"] == "currency_mismatch"
    assert env.transactions(account) == []
    assert env.snapshots(account) == []


# ---------------------------------------------------------------------------
# Transfers
# ---------------------------------------------------------------------------


def test_transfers_are_paired_after_import(env: Env) -> None:
    stmt = sample_statement(35)
    checking = env.checking(stmt)
    savings = env.ledger.account(env.household_id, type="savings")
    leg = next(t for t in stmt.transactions if t.payee == "Transfer to savings")
    incoming = env.ledger.transaction(
        savings, amount=-leg.amount, posted_date=leg.posted, raw_payee="Transfer from checking"
    )
    env.ingest(ofx_sgml_bytes(stmt))
    assert env.run(AUTO_PAIR_JOB_TYPE)
    out = next(t for t in env.transactions(checking) if t.raw_payee == "Transfer to savings")
    (back,) = [t for t in env.transactions(savings) if t.id == incoming.id]
    assert out.transfer_group_id is not None
    assert out.transfer_group_id == back.transfer_group_id


def _household(env: Env) -> Household:
    household = env.owner.household
    assert household is not None
    return household


# ---------------------------------------------------------------------------
# Never-seen layouts: confident suggestions, multi-account files
# ---------------------------------------------------------------------------


def _mappings(env: Env) -> list[ImportMapping]:
    for db in env.db():
        rows = list(db.execute(select(ImportMapping).order_by(ImportMapping.created_at)).scalars())
        db.expunge_all()
        return rows
    raise AssertionError  # pragma: no cover


def _trail(env: Env) -> str:
    """Every audit row, event and ingest log of the household, as text."""
    for db in env.db():
        audits = [without_ids(a.after) for a in db.execute(select(AuditLog)).scalars()]
        events = [without_ids(e.payload) for e in db.execute(select(Event)).scalars()]
        logs = [
            without_ids(j.log) for j in db.execute(select(IngestJob)).scalars() if j.log is not None
        ]
        return str([audits, events, logs])
    raise AssertionError  # pragma: no cover


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_confident_layouts_import_on_their_own_and_save_the_mapping(env: Env, layout: str) -> None:
    history = synth_ledger(25, days=59)
    stmt = layout_statement(
        statement(history, date(2026, 1, 1), date(2026, 1, 31), seed=25), layout
    )
    # Purchases-positive card exports have nothing that proves the sign: the
    # suggestion waits for a person (whatever the account).
    card = layout == "card_positive_purchases"
    account = env.ledger.account(
        env.household_id,
        currency=stmt.currency,
        type=AccountType.CREDIT_CARD if card else AccountType.CHECKING,
    )
    first = env.ingest(csv_bytes(stmt, layout), account=account, name=f"{layout}.csv")
    document = env.document(first)
    if card:
        assert document.status is DocumentStatus.NEEDS_REVIEW
        (item,) = env.open_reviews(first)
        assert item.payload["reason"] == "mapping_required"
        assert item.payload["auto_import"] == "sign_unverified"
        assert item.payload["suggestion"]["confident"] is False
        assert item.payload["suggestion"]["doubts"] == ["sign_unverified"]
        assert item.payload["suggestion"]["sign_evidence"] is None
        assert item.payload["suggested_spec"]["date_column"] == 1  # Post Date
        assert env.transactions(account) == []
        assert _mappings(env) == []
        return
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(first)
    assert _amounts(env.transactions(account)) == _expected(stmt)
    (mapping,) = _mappings(env)
    assert mapping.created_by == env.owner.user.id
    assert mapping.name.startswith(LAYOUTS[layout].header[0])
    assert mapping.spec == {
        **json_spec(LAYOUTS[layout].spec),
        "account_column": None,
    }
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        last = owner.get(f"{API}/imports/{first}/preview").json()["last_import"]
        assert last["mapping_suggested"] is True
        assert last["mapping_id"] == str(mapping.id)
    for db in env.db():
        created = db.execute(
            select(AuditLog).where(AuditLog.action == "import_mapping.create")
        ).scalar_one()
        assert created.after is not None
        assert created.after["origin"] == "suggested"
        assert created.user_id == env.owner.user.id
        assert "name" not in created.after  # names are not in the trail
    # The next file of the layout is recognised by its saved mapping.
    later = layout_statement(
        statement(history, date(2026, 2, 1), date(2026, 2, 28), seed=25), layout
    )
    second = env.ingest(csv_bytes(later, layout), account=account)
    assert env.document(second).status is DocumentStatus.PROCESSED
    assert len(_mappings(env)) == 1
    for db in env.db():
        log = db.execute(
            select(IngestJob.log).where(
                IngestJob.document_id == second, IngestJob.stage == IngestStage.PARSE
            )
        ).scalar_one()
        assert log["mapping_suggested"] is False
        assert log["mapping_id"] == str(mapping.id)


def json_spec(fields: dict[str, Any]) -> dict[str, Any]:
    return MappingSpec.model_validate(fields).model_dump(mode="json")


#: ``LEDGER_HEADER`` (no action column: not a brokerage activity table, which
#: goes to the investments importer, test_activity_table_import.py) without
#: the names of a trade's columns. A header that names a security, a quantity
#: and a price is never a confident cash ledger (``investment_columns``);
#: these tests are about a multi-account cash export.
CASH_HEADER = tuple(
    {
        "Symbol": "Code",
        "Price": "Rate",
        "Quantity": "Count",
        "Exchange Quantity": "Exchange Count",
    }.get(cell, cell)
    for cell in LEDGER_HEADER
)


def _activity(
    env: Env, seed: int, accounts: tuple[ActivityAccount, ...], **kwargs: Any
) -> tuple[list[ActivityRow], bytes]:
    rows = activity_rows(seed, accounts, **kwargs)
    return rows, activity_csv_bytes(rows, header=CASH_HEADER)


def _cash(rows: list[ActivityRow], account: ActivityAccount) -> list[tuple[date, Decimal]]:
    return sorted(
        (r.run_date, r.amount) for r in rows if r.account == account and r.amount is not None
    )


def test_two_account_activity_csv_lands_on_each_account(env: Env) -> None:
    individual, roth = activity_accounts(41)
    rows, data = _activity(env, 41, (individual, roth))
    brokerage = env.ledger.account(
        env.household_id, type=AccountType.BROKERAGE, number_last4=individual.last4
    )
    retirement = env.ledger.account(
        env.household_id, type=AccountType.RETIREMENT, number_last4=roth.last4
    )
    document_id = env.ingest(data, name="activity.csv")
    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert document.account_id is None  # two accounts: the document names neither
    assert _amounts(env.transactions(brokerage)) == _cash(rows, individual)
    assert _amounts(env.transactions(retirement)) == _cash(rows, roth)
    payees = {t.raw_payee for t in env.transactions(brokerage)}
    assert "SYNTHETIC ADVISORY FEE" in payees or "No Description" in payees
    (mapping,) = _mappings(env)
    assert mapping.spec["account_column"] == ACTIVITY_SPEC["account_column"]
    assert mapping.spec["currency_column"] == ACTIVITY_SPEC["currency_column"]
    assert mapping.spec["date_column"] == ACTIVITY_SPEC["date_column"]
    # The disclaimers and "Date downloaded" line are note rows: not imported,
    # not a review item either.
    assert env.open_reviews(document_id) == []
    # Account numbers never reach the trail, not even the last four digits.
    trail = _trail(env)
    for account in (individual, roth):
        assert account.number not in trail
        assert account.last4 not in trail
    # A second export with newer rows goes through the saved mapping.
    newer, data = _activity(env, 42, (individual, roth), start=date(2026, 10, 31), days=12)
    second = env.ingest(data, name="activity-2.csv")
    assert env.document(second).status is DocumentStatus.PROCESSED
    assert len(_mappings(env)) == 1
    assert _amounts(env.transactions(brokerage)) == sorted(
        _cash(rows, individual) + _cash(newer, individual)
    )


def test_unknown_account_of_an_activity_csv_waits_per_group(env: Env) -> None:
    individual, roth = activity_accounts(43)
    rows, data = _activity(env, 43, (individual, roth))
    brokerage = env.ledger.account(
        env.household_id, type=AccountType.BROKERAGE, number_last4=individual.last4
    )
    retirement = env.ledger.account(env.household_id, type=AccountType.RETIREMENT)
    document_id = env.ingest(data, name="activity.csv")
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert _amounts(env.transactions(brokerage)) == _cash(rows, individual)
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.UNKNOWN_ACCOUNT
    assert item.payload["reason"] == "account_not_found"
    first_group = 0 if rows[0].account == roth else 1
    assert item.payload["group"] == first_group
    assert item.payload["transactions"] == len(_cash(rows, roth))
    assert roth.last4 not in str(without_ids(item.payload))
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        detail = owner.get(f"{API}/review/{item.id}").json()
        ranked = [a["value"] for a in detail["alternatives"] if a["field"] == "account_id"]
        assert retirement.id is not None
        assert str(retirement.id) in ranked
        resolved = owner.post(
            f"{API}/review/{item.id}/resolve",
            json={"kind": "unknown_account", "account_id": str(retirement.id), "remember": True},
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["resolution"]["remembered"] == ["last4"]
    for db in env.db():
        remembered = db.get(Account, retirement.id)
        assert remembered is not None
        assert remembered.number_last4 == roth.last4
        (mapping,) = db.execute(select(ImportMapping)).scalars()
        assert mapping.account_id is None  # the mapping serves both accounts
    env.run(APPLY_MAPPING_JOB_TYPE)
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert _amounts(env.transactions(retirement)) == _cash(rows, roth)
    assert _amounts(env.transactions(brokerage)) == _cash(rows, individual)  # not twice


def test_member_activity_csv_only_reaches_accounts_they_edit(env: Env) -> None:
    individual, roth = activity_accounts(48)
    rows, data = _activity(env, 48, (individual, roth))
    member = env.factories.user(role="member", with_totp=True, household=_household(env))
    mine = env.ledger.account(
        env.household_id, type=AccountType.BROKERAGE, number_last4=individual.last4
    )
    hidden = env.ledger.account(
        env.household_id, type=AccountType.RETIREMENT, number_last4=roth.last4
    )
    env.ledger.grant(env.household_id, member.user.id, mine, GrantLevel.EDIT)
    document_id = env.ingest(data, as_user=member, name="activity.csv")
    assert _amounts(env.transactions(mine)) == _cash(rows, individual)
    assert env.transactions(hidden) == []
    (item,) = env.open_reviews(document_id)
    assert item.payload["reason"] == "account_not_found"
    assert item.payload["candidates"] == []  # the hidden account is not named
    (mapping,) = _mappings(env)
    assert mapping.created_by == member.user.id


def test_unsure_suggestion_waits_with_the_suggestion(env: Env) -> None:
    individual, roth = activity_accounts(45)
    rows = activity_rows(45, (individual, roth))
    # A row in the middle without a cash amount (a share transfer): the
    # suggestion cannot read every row, so a person decides.
    middle = len(rows) // 2
    rows[middle] = ActivityRow(
        rows[middle].run_date,
        rows[middle].account,
        "TRANSFERRED FROM SYNTHETIC GROWTH INDEX ETF (SGRX) (Cash)",
        "SGRX",
        "SYNTHETIC GROWTH INDEX ETF",
        None,
        Decimal(3),
        None,
        None,
    )
    env.ledger.account(env.household_id, type=AccountType.BROKERAGE, number_last4=individual.last4)
    document_id = env.ingest(activity_csv_bytes(rows, header=CASH_HEADER), name="activity.csv")
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.UNPARSED
    assert item.payload["reason"] == "mapping_required"
    assert item.payload["auto_import"] == "not_confident"
    assert item.payload["suggestion"]["doubts"] == ["rows_unreadable"]
    assert item.payload["suggestion"]["failed_rows"] == 1
    assert _mappings(env) == []
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        preview = owner.get(f"{API}/imports/{document_id}/preview").json()
    # The review item and the preview carry the same suggestion.
    assert preview["suggested_spec"] == item.payload["suggested_spec"]
    assert preview["suggestion"]["spec"] == item.payload["suggested_spec"]
    assert preview["suggestion"]["confident"] is False
    assert preview["suggestion"]["doubts"] == ["rows_unreadable"]
    assert preview["suggested_spec"]["account_column"] == ACTIVITY_SPEC["account_column"]
    roles = {r["role"]: r["column"] for r in preview["suggestion"]["roles"]}
    assert roles["account"] == ACTIVITY_SPEC["account_column"]
    assert "indicator" not in roles


def test_mapping_stored_before_account_column_still_imports(env: Env) -> None:
    stmt = sample_statement(46)
    account = env.ledger.account(env.household_id)
    old_spec = dict(LAYOUTS["us_signed"].spec)  # no "account_column" key at all
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        mapping = make_import_mapping(
            db, env.household_id, spec=old_spec, rows=[list(LAYOUTS["us_signed"].header)]
        )
        mapping.spec = {**json_spec(old_spec)}
        del mapping.spec["account_column"]
        db.flush()
        mapping_id = mapping.id
    document_id = env.ingest(csv_bytes(stmt, "us_signed"), account=account)
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert _amounts(env.transactions(account)) == _expected(stmt)
    assert [m.id for m in _mappings(env)] == [mapping_id]
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        shown = owner.get(f"{API}/imports/mappings/{mapping_id}").json()
        assert shown["spec"]["account_column"] is None
        assert shown["use_count"] == 1


@pytest.mark.parametrize(
    ("kind", "uploader", "held"),
    [("csv", False, "no_member"), ("pdf", True, "not_delimited"), ("csv", True, None)],
)
def test_who_and_what_may_be_mapped_without_a_person(
    env: Env, kind: str, uploader: bool, held: str | None
) -> None:
    """Intake without a person (drop folder, mail) and PDF page tables always wait."""
    individual, roth = activity_accounts(50)
    rows = activity_rows(50, (individual, roth))
    data = activity_csv_bytes(rows, header=CASH_HEADER)
    cells = extract_delimited(data, ExtractOptions(), "csv").tables[0].rows
    result = table_result(cells, kind=kind)
    brokerage = env.ledger.account(
        env.household_id, type=AccountType.BROKERAGE, number_last4=individual.last4
    )
    for db in env.db():
        document = make_extracted_document(
            db,
            env.household_id,
            result=result,
            uploaded_by=env.owner.user.id if uploader else None,
        )
        outcome = imports_service.import_extraction(db, document, result)
        assert outcome.summary["mapping_suggested"] is (held is None)
        items = list(
            db.execute(select(ReviewItem).where(ReviewItem.document_id == document.id)).scalars()
        )
        if held is None:
            assert outcome.status is DocumentStatus.NEEDS_REVIEW  # the unknown Roth IRA
            assert [i.payload["reason"] for i in items] == ["account_not_found"]
        else:
            (item,) = items
            assert item.payload["reason"] == "mapping_required"
            assert item.payload["auto_import"] == held
            assert item.payload["suggestion"]["confident"] is True
    expected = _cash(rows, individual) if held is None else []
    assert _amounts(env.transactions(brokerage)) == expected
    assert len(_mappings(env)) == (1 if held is None else 0)


# ---------------------------------------------------------------------------
# Review fixes: choices that must not reach further than the item they answer
# ---------------------------------------------------------------------------


def _resolve(env: Env, item: ReviewItem, account: Account, *, remember: bool) -> Any:
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        return owner.post(
            f"{API}/review/{item.id}/resolve",
            json={"kind": "unknown_account", "account_id": str(account.id), "remember": remember},
        )


@pytest.mark.parametrize("remember", [True, False])
def test_an_account_chosen_for_one_group_takes_only_that_group(env: Env, remember: bool) -> None:
    known, first, second = activity_accounts(51, 3)
    rows, data = _activity(env, 51, (known, first, second))
    brokerage = env.ledger.account(
        env.household_id, type=AccountType.BROKERAGE, number_last4=known.last4
    )
    chosen = env.ledger.account(env.household_id, type=AccountType.RETIREMENT)
    document_id = env.ingest(data, name="activity.csv")
    items = env.open_reviews(document_id)
    assert [i.payload["reason"] for i in items] == ["account_not_found"] * 2
    assert all(isinstance(i.payload["spec_digest"], str) for i in items)
    groups = {i.payload["group"]: i for i in items}
    order = [r.account for r in rows if r.account != known and r.amount is not None]
    # The item of the group of ``first`` (groups are numbered by first appearance).
    numbered = list(dict.fromkeys(r.account for r in rows if r.amount is not None))
    item = groups[numbered.index(first)]
    assert first in order
    assert second in order
    resolved = _resolve(env, item, chosen, remember=remember)
    assert resolved.status_code == 200, resolved.text
    env.run(APPLY_MAPPING_JOB_TYPE)
    assert _amounts(env.transactions(chosen)) == _cash(rows, first)
    assert _amounts(env.transactions(brokerage)) == _cash(rows, known)
    # The other unmatched group still waits for its own choice.
    (left,) = env.open_reviews(document_id)
    assert left.kind is ReviewKind.UNKNOWN_ACCOUNT
    assert left.payload["group"] == numbered.index(second)
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW


def test_apply_with_an_account_and_no_group_takes_only_its_own_digits(env: Env) -> None:
    known, first, second = activity_accounts(52, 3)
    rows, data = _activity(env, 52, (known, first, second))
    env.ledger.account(env.household_id, type=AccountType.BROKERAGE, number_last4=known.last4)
    document_id = env.ingest(data, name="activity.csv")
    assert len(env.open_reviews(document_id)) == 2

    def apply(account: Account) -> None:
        with Actors.open(env.settings, env.factories) as actors:
            owner = actors.login(env.owner)
            accepted = owner.post(
                f"{API}/imports/{document_id}/apply-mapping",
                json={"account_id": str(account.id)},
            )
            assert accepted.status_code == 202, accepted.text
        env.run(APPLY_MAPPING_JOB_TYPE)

    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        refused = owner.post(f"{API}/imports/{document_id}/apply-mapping", json={"group": 0})
        assert refused.status_code == 422
    # Two groups match nothing: neither lands on an account without their number.
    unnumbered = env.ledger.account(env.household_id, type=AccountType.RETIREMENT)
    apply(unnumbered)
    assert env.transactions(unnumbered) == []
    assert len(env.open_reviews(document_id)) == 2
    # An account with one group's digits takes that group only.
    numbered = env.ledger.account(
        env.household_id, type=AccountType.RETIREMENT, number_last4=second.last4
    )
    apply(numbered)
    assert _amounts(env.transactions(numbered)) == _cash(rows, second)
    (left,) = env.open_reviews(document_id)
    assert left.payload["reason"] == "account_not_found"
    # Now one group is left unmatched: the chosen account takes it.
    apply(unnumbered)
    assert _amounts(env.transactions(unnumbered)) == _cash(rows, first)
    assert env.open_reviews(document_id) == []


def test_a_group_item_raised_under_another_spec_is_stale(env: Env) -> None:
    known, other = activity_accounts(53)
    _, data = _activity(env, 53, (known, other))
    env.ledger.account(env.household_id, type=AccountType.BROKERAGE, number_last4=known.last4)
    chosen = env.ledger.account(env.household_id, type=AccountType.RETIREMENT)
    document_id = env.ingest(data, name="activity.csv")
    (item,) = env.open_reviews(document_id)
    (mapping,) = _mappings(env)
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        # Someone edits the mapping before the item is answered.
        changed = owner.patch(
            f"{API}/imports/mappings/{mapping.id}",
            json={"spec": {**mapping.spec, "payee_column": None}},
        )
        assert changed.status_code == 200, changed.text
    for db in env.db():
        document = db.get(Document, document_id)
        assert document is not None
        assert imports_service.stored_group_last4(db, document, item.payload) is None
        assert imports_service.group_item_current(db, document, item.payload) is False
    response = _resolve(env, item, chosen, remember=True)
    assert response.status_code == 409
    assert problem_type(response) == "review-item-stale"
    for db in env.db():
        account = db.get(Account, chosen.id)
        assert account is not None
        assert account.number_last4 is None  # nothing remembered
    assert env.transactions(chosen) == []


#: Signed amounts, mostly money out, and nothing that proves the sign: the
#: suggestion is confident and its sign is on trust.
TRUSTED_LAYOUT = "preamble"


def _card_upload(env: Env) -> tuple[SynthStatement, Account, uuid.UUID]:
    history = synth_ledger(25, days=59)
    stmt = layout_statement(
        statement(history, date(2026, 1, 1), date(2026, 1, 31), seed=25),
        TRUSTED_LAYOUT,
    )
    card = env.ledger.account(
        env.household_id, currency=stmt.currency, type=AccountType.CREDIT_CARD
    )
    # No account named: the suggestion is saved, the account is asked for.
    document_id = env.ingest(csv_bytes(stmt, TRUSTED_LAYOUT), name="card.csv")
    (item,) = env.open_reviews(document_id)
    assert item.payload["reason"] == "account_required"
    (mapping,) = _mappings(env)
    assert mapping.sign_unverified is True
    return stmt, card, document_id


@pytest.mark.parametrize("remember", [True, False])
def test_a_sign_read_on_trust_never_reaches_a_card_through_the_saved_mapping(
    env: Env, remember: bool
) -> None:
    stmt, card, document_id = _card_upload(env)
    (item,) = env.open_reviews(document_id)
    resolved = _resolve(env, item, card, remember=remember)
    assert resolved.status_code == 200, resolved.text
    env.run(APPLY_MAPPING_JOB_TYPE)
    assert env.transactions(card) == []
    (held,) = env.open_reviews(document_id)
    assert held.kind is ReviewKind.UNPARSED
    assert held.payload["reason"] == "mapping_required"
    assert held.payload["auto_import"] == "sign_unverified"
    (mapping,) = _mappings(env)
    assert mapping.account_id == (card.id if remember else None)
    # Next month's file to the card waits the same way.
    february = layout_statement(
        statement(synth_ledger(25, days=59), date(2026, 2, 1), date(2026, 2, 28), seed=25),
        TRUSTED_LAYOUT,
    )
    later = env.ingest(csv_bytes(february, TRUSTED_LAYOUT), account=card, name="b.csv")
    (again,) = env.open_reviews(later)
    assert again.payload["auto_import"] == "sign_unverified"
    # The person confirms the sign in the editor: the mapping is theirs now.
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        fixed = owner.patch(
            f"{API}/imports/mappings/{mapping.id}",
            json={"spec": {**mapping.spec, "sign": "inflow_positive"}},
        )
        assert fixed.status_code == 200, fixed.text
        applied = owner.post(
            f"{API}/review/{held.id}/resolve", json={"kind": "unparsed", "action": "apply_mapping"}
        )
        assert applied.status_code == 200, applied.text
    env.run(APPLY_MAPPING_JOB_TYPE)
    assert _amounts(env.transactions(card)) == _expected(stmt)
    (mapping,) = _mappings(env)
    assert mapping.sign_unverified is False


def test_choosing_the_trust_only_mapping_explicitly_imports_with_it(env: Env) -> None:
    stmt, card, document_id = _card_upload(env)
    (mapping,) = _mappings(env)
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        accepted = owner.post(
            f"{API}/imports/{document_id}/apply-mapping",
            json={"mapping_id": str(mapping.id), "account_id": str(card.id)},
        )
        assert accepted.status_code == 202, accepted.text
    env.run(APPLY_MAPPING_JOB_TYPE)
    # The person's choice: the mapping as it is, the file's own signs.
    assert _amounts(env.transactions(card)) == _expected(stmt)


def test_a_mapping_on_trust_is_not_used_for_a_file_whose_sign_is_doubted(env: Env) -> None:
    """A mapping saved from a card export before its sign was doubted: next month's file waits."""
    layout = "card_positive_purchases"
    history = synth_ledger(25, days=59)
    stmt = layout_statement(
        statement(history, date(2026, 2, 1), date(2026, 2, 28), seed=25), layout
    )
    # The account such a file was given: the checking account once proposed for it.
    account = env.ledger.account(env.household_id, currency=stmt.currency)
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        saved = make_import_mapping(
            db,
            env.household_id,
            spec={**LAYOUTS[layout].spec, "sign": "inflow_positive"},
            rows=[list(LAYOUTS[layout].header)],
        )
        saved.sign_unverified = True
        mapping_id = saved.id
    document_id = env.ingest(csv_bytes(stmt, layout), account=account, name="card.csv")
    assert env.transactions(account) == []
    (held,) = env.open_reviews(document_id)
    assert held.payload["reason"] == "mapping_required"
    assert held.payload["auto_import"] == "sign_unverified"
    assert held.payload["suggestion"]["doubts"] == ["sign_unverified"]
    # The person says purchases are positive: the mapping is theirs, the file imports.
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        fixed = owner.patch(
            f"{API}/imports/mappings/{mapping_id}",
            json={"spec": json_spec(LAYOUTS[layout].spec)},
        )
        assert fixed.status_code == 200, fixed.text
        applied = owner.post(
            f"{API}/review/{held.id}/resolve", json={"kind": "unparsed", "action": "apply_mapping"}
        )
        assert applied.status_code == 200, applied.text
    env.run(APPLY_MAPPING_JOB_TYPE)
    assert _amounts(env.transactions(account)) == _expected(stmt)
    (mapping,) = _mappings(env)
    assert mapping.sign_unverified is False


def test_an_account_bound_mapping_wins_over_a_members_household_one(env: Env) -> None:
    stmt = sample_statement(54)
    owners = env.ledger.account(env.household_id, currency=stmt.currency)
    member = env.factories.user(role="member", with_totp=True, household=_household(env))
    mine = env.ledger.account(env.household_id, currency=stmt.currency)
    env.ledger.grant(env.household_id, member.user.id, mine, GrantLevel.EDIT)
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        bound = make_import_mapping(
            db,
            env.household_id,
            spec=LAYOUTS["us_signed"].spec,
            rows=[list(LAYOUTS["us_signed"].header)],
            account=db.get(Account, owners.id),
        )
        bound_id = bound.id
    # The member cannot see the owner's mapping: the suggestion is saved for them.
    theirs = env.ingest(csv_bytes(stmt, "us_signed"), account=mine, as_user=member)
    assert env.document(theirs).status is DocumentStatus.PROCESSED
    assert len(_mappings(env)) == 2
    # The owner's next file without an account still goes to the owner's account.
    later = sample_statement(55)
    document_id = env.ingest(csv_bytes(later, "us_signed"))
    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, env.open_reviews(document_id)
    assert document.account_id == owners.id
    assert _amounts(env.transactions(owners)) == _expected(later)
    for db in env.db():
        log = db.execute(
            select(IngestJob.log).where(
                IngestJob.document_id == document_id, IngestJob.stage == IngestStage.PARSE
            )
        ).scalar_one()
        assert log["mapping_id"] == str(bound_id)


# ---------------------------------------------------------------------------
# The sign of a file is proven, or the file waits
# ---------------------------------------------------------------------------


def _detected(env: Env, document_id: uuid.UUID) -> list[DetectedAccount]:
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        return proposals.detect_accounts(db, env.principal(), document_id).accounts


def _accept_proposals(env: Env, document_id: uuid.UUID) -> list[uuid.UUID]:
    """Create every account the document proposes, as proposed, and import into it.

    A proposal that names no type is created as a checking account, which is
    what a person who clicks through gets.
    """
    choices = [
        AccountChoice(
            target=found.target,
            index=found.index,
            create=AccountCreate(
                type=found.proposal.type or AccountType.CHECKING,
                name=found.proposal.name,
                currency=found.proposal.currency or "USD",
            ),
        )
        for found in _detected(env, document_id)
        if found.proposal is not None
    ]
    if not choices:
        return []
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        applied = proposals.apply_accounts(
            db,
            env.principal(),
            document_id,
            DocumentAccountsApply(items=choices),
            request=None,
        )
    env.run(APPLY_MAPPING_JOB_TYPE)
    return applied.created


def _ledger_rows(env: Env) -> int:
    for db in env.db():
        return db.execute(select(func.count()).select_from(Transaction)).scalar_one()
    raise AssertionError  # pragma: no cover


def test_card_export_is_held_not_imported_as_income(env: Env) -> None:
    """Purchases printed positive, into an empty household: never income on a guess."""
    data = layouts.card_positive_purchases()
    document_id = env.ingest(data, name="card.csv")
    assert _accept_proposals(env, document_id) == []  # no account is proposed for a held file
    assert _ledger_rows(env) == 0
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.UNPARSED
    assert item.payload["reason"] == "mapping_required"
    assert item.payload["auto_import"] == "sign_unverified"
    assert item.payload["suggestion"]["doubts"] == ["sign_unverified"]
    assert item.payload["suggestion"]["confident"] is False
    assert item.payload["suggestion"]["sign_evidence"] is None
    assert _mappings(env) == []
    # The mapping editor is the answer: the person says purchases are positive.
    card = env.ledger.account(env.household_id, type=AccountType.CREDIT_CARD)
    cells = extract_delimited(data, ExtractOptions(), "csv").tables[0].rows
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        make_import_mapping(
            db,
            env.household_id,
            spec={**item.payload["suggested_spec"], "sign": "outflow_positive"},
            rows=cells,
            account=db.get(Account, card.id),
        )
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        applied = owner.post(
            f"{API}/review/{item.id}/resolve", json={"kind": "unparsed", "action": "apply_mapping"}
        )
        assert applied.status_code == 200, applied.text
    env.run(APPLY_MAPPING_JOB_TYPE)
    amounts = [t.amount for t in env.transactions(card)]
    assert len(amounts) == len(layouts.CARD_ROWS)
    assert sorted(amounts)[-1] == Decimal("350.00")  # the payment is the only money in
    assert sum(1 for amount in amounts if amount < 0) == len(layouts.CARD_ROWS) - 1


def test_proposed_account_type_is_not_checking_when_sign_unproven(env: Env) -> None:
    # Signed amounts nothing proves: the mapping is saved on trust, the
    # account is asked for, and the proposal does not say what kind it is.
    trusted = env.ingest(layouts.checking_signed_no_balance(), name="export.csv")
    (mapping,) = _mappings(env)
    assert mapping.sign_unverified is True
    (found,) = _detected(env, trusted)
    assert found.status == "new"
    assert found.proposal is not None
    assert found.proposal.type is None
    assert found.proposal.name == "Account"
    # A header that names a card, the sign proven by its Type words.
    card = env.ingest(layouts.card_merchant_with_type_column(), name="card.csv")
    (found,) = _detected(env, card)
    assert found.proposal is not None
    assert found.proposal.type is AccountType.CREDIT_CARD
    # A sign the file proves and no card word: a checking account, as before.
    proven = env.ingest(layouts.checking_with_running_balance(), name="checking.csv")
    (found,) = _detected(env, proven)
    assert found.proposal is not None
    assert found.proposal.type is AccountType.CHECKING
    # The card's purchases are spending on the account created from its proposal.
    (created,) = _accept_proposals(env, card)
    for db in env.db():
        account = db.get(Account, created)
        assert account is not None
        assert account.type is AccountType.CREDIT_CARD
        amounts = [
            t.amount
            for t in db.execute(
                select(Transaction).where(Transaction.account_id == account.id)
            ).scalars()
        ]
    assert sum(1 for amount in amounts if amount < 0) == len(layouts.CARD_ROWS) - 1
    assert max(amounts) == Decimal("350.00")


# ---------------------------------------------------------------------------
# Payees inside a bank's description, and their default category
# ---------------------------------------------------------------------------

_PAYEE_CSV = (
    b"Date,Description,Amount\n"
    b"2026-03-02,OAKFIELD RENTALS DES:RENT ID:88123 INDN:JORDAN SAMPLE,-950.00\n"
    b"2026-03-05,CHECKCARD 0304 BLUE OWL GROCERS SPRINGFIELD IL 2443106,-88.12\n"
    b"2026-03-11,POS QB COFFEE 0042,-4.50\n"
    b"2026-03-13,INITECH DES:PAYROLL ID:XXXXX4471,1240.00\n"
    b"2026-03-19,TRANSFER FROM OAKFIELD RENTALS TO BLUE OWL GROCERS,-1.00\n"
    b"2026-03-24,MAPLE HARDWARE SPRINGFIELD IL,-43.28\n"
)


def test_rows_take_the_payee_they_name_and_its_default_category(env: Env) -> None:
    household = env.household_id
    rent = env.ledger.category(household, name="Rent")
    groceries = env.ledger.category(household, name="Groceries")
    coffee = env.ledger.category(household, name="Coffee")
    eating_out = env.ledger.category(household, name="Eating out")
    landlord = env.ledger.payee(household, name="Oakfield Rentals", default_category=rent)
    grocer = env.ledger.payee(household, name="Blue Owl Grocers", default_category=groceries)
    cafe = env.ledger.payee(
        household, name="Quiet Bean Coffee", aliases=["QB Coffee"], default_category=coffee
    )
    employer = env.ledger.payee(household, name="Initech")  # no default category
    # A rule decides before a payee's default does.
    env.ledger.rule(
        household,
        conditions={"type": "payee", "op": "contains", "value": "qb coffee"},
        actions=[{"type": "set_category", "category_id": str(eating_out.id)}],
    )
    account = env.ledger.account(household, currency="USD")
    document_id = env.ingest(_PAYEE_CSV, account=account, name="march.csv")
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    rows = {t.posted_date.day: t for t in env.transactions(account)}
    assert (rows[2].payee_id, rows[2].category_id) == (landlord.id, rent.id)
    assert (rows[5].payee_id, rows[5].category_id) == (grocer.id, groceries.id)
    assert (rows[11].payee_id, rows[11].category_id) == (cafe.id, eating_out.id)
    assert (rows[13].payee_id, rows[13].category_id) == (employer.id, None)
    # Two payees side by side, and a payee nobody named: left for a person.
    assert (rows[19].payee_id, rows[19].category_id) == (None, None)
    assert (rows[24].payee_id, rows[24].category_id) == (None, None)
    # The description is kept as printed.
    assert rows[2].raw_payee == "OAKFIELD RENTALS DES:RENT ID:88123 INDN:JORDAN SAMPLE"


# ---------------------------------------------------------------------------
# An account column of names splits the file
# ---------------------------------------------------------------------------


def _named_rows(name: str) -> list[tuple[date, Decimal]]:
    return sorted(
        (date.fromisoformat(row[0]), Decimal(row[3]))
        for row in layouts.TWO_ACCOUNT_ROWS
        if row[1] == name
    )


def test_aggregator_export_asks_for_each_account(env: Env) -> None:
    """A checking account and a card in one ``Account`` column: split, never merged."""
    document_id = env.ingest(layouts.aggregator_two_accounts(), name="all-accounts.csv")
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert _ledger_rows(env) == 0  # nothing lands before each account is answered
    items = sorted(env.open_reviews(document_id), key=lambda item: item.payload["group"])
    assert [item.kind for item in items] == [ReviewKind.UNKNOWN_ACCOUNT] * 2
    assert [item.payload["reason"] for item in items] == ["account_not_found"] * 2
    assert [item.payload["group"] for item in items] == [0, 1]
    assert [item.payload["transactions"] for item in items] == [5, 4]
    (mapping,) = _mappings(env)
    assert mapping.spec["account_column"] == 1
    # The names stay in the file: neither the items nor the trail carry them.
    assert "Everyday" not in str([item.payload for item in items])
    assert "Everyday" not in _trail(env)
    assert "Rewards" not in _trail(env)
    # The detected accounts tell the two questions apart.
    detected = _detected(env, document_id)
    assert [(found.target, found.index, found.status) for found in detected] == [
        ("group", 0, "new"),
        ("group", 1, "new"),
    ]
    assert [found.last4 for found in detected] == [None, None]
    assert [found.name for found in detected] == ["Everyday Checking", "Rewards Card"]
    assert [found.proposal.name for found in detected if found.proposal is not None] == [
        "Everyday Checking",
        "Rewards Card",
    ]
    assert [found.review_item_id for found in detected] == [item.id for item in items]
    # Both proposals accepted: each name's rows land on its own account.
    created = _accept_proposals(env, document_id)
    assert len(created) == 2
    for db in env.db():
        accounts = {
            account.name: account
            for account in db.execute(select(Account).where(Account.id.in_(created))).scalars()
        }
        db.expunge_all()
    assert sorted(accounts) == ["Everyday Checking", "Rewards Card"]
    for name, account in accounts.items():
        assert _amounts(env.transactions(account)) == _named_rows(name)
    assert _ledger_rows(env) == len(layouts.TWO_ACCOUNT_ROWS)
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED


def test_an_account_chosen_for_one_name_takes_only_that_names_rows(env: Env) -> None:
    document_id = env.ingest(layouts.aggregator_two_accounts("Source Account"), name="all.csv")
    items = {item.payload["group"]: item for item in env.open_reviews(document_id)}
    assert sorted(items) == [0, 1]
    checking = env.ledger.account(env.household_id, type=AccountType.CHECKING)
    resolved = _resolve(env, items[0], checking, remember=False)
    assert resolved.status_code == 200, resolved.text
    env.run(APPLY_MAPPING_JOB_TYPE)
    assert _amounts(env.transactions(checking)) == _named_rows("Everyday Checking")
    assert _ledger_rows(env) == 5
    # The other name still waits for its own choice.
    (left,) = env.open_reviews(document_id)
    assert left.kind is ReviewKind.UNKNOWN_ACCOUNT
    assert (left.payload["group"], left.payload["transactions"]) == (1, 4)
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW


# ---------------------------------------------------------------------------
# The day a file arrived and its day/month order
# ---------------------------------------------------------------------------

#: Month first: 3 and 9 February, 4 and 11 March. Day first: 2 March,
#: 2 September, 3 April, 3 November.
TIED_DATES = ("02/03/2026", "02/09/2026", "03/04/2026", "03/11/2026")


@pytest.mark.parametrize("remember", [False, True])
def test_names_answered_one_after_the_other_finish_the_document(env: Env, remember: bool) -> None:
    """Each name is asked about once: an answer is kept when the next one is given."""
    document_id = env.ingest(layouts.aggregator_two_accounts(), name="all-accounts.csv")
    accounts = [
        env.ledger.account(env.household_id, type=AccountType.CHECKING),
        env.ledger.account(env.household_id, type=AccountType.SAVINGS),
    ]
    for group, account in enumerate(accounts):
        items = {item.payload["group"]: item for item in env.open_reviews(document_id)}
        assert sorted(items) == [0, 1][group:]  # the group answered before is not asked again
        resolved = _resolve(env, items[group], account, remember=remember)
        assert resolved.status_code == 200, resolved.text
        env.run(APPLY_MAPPING_JOB_TYPE)
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert _amounts(env.transactions(accounts[0])) == _named_rows("Everyday Checking")
    assert _amounts(env.transactions(accounts[1])) == _named_rows("Rewards Card")
    assert _ledger_rows(env) == len(layouts.TWO_ACCOUNT_ROWS)
    # The names stay in the file: the answers are kept by a digest, not by the name.
    resolutions = str([item.resolution for item in env.reviews(document_id)])
    assert "Everyday" not in resolutions
    assert "Rewards" not in resolutions
    # Importing the document again asks nothing.
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        again = owner.post(f"{API}/imports/{document_id}/apply-mapping", json={})
        assert again.status_code == 202, again.text
    env.run(APPLY_MAPPING_JOB_TYPE)
    assert env.open_reviews(document_id) == []
    assert _ledger_rows(env) == len(layouts.TWO_ACCOUNT_ROWS)


def test_digits_inside_an_account_name_never_pick_an_account(env: Env) -> None:
    """``Pot 12 of 34`` is a name: an unrelated account ending 1234 gets none of its rows."""
    other = env.ledger.account(env.household_id, type=AccountType.CHECKING, number_last4="1234")
    names = ["Pot 12 of 34", "Everyday Checking"] * 3
    document_id = env.ingest(layouts.aggregator_named_accounts(names), name="all-accounts.csv")
    assert env.transactions(other) == []
    assert _ledger_rows(env) == 0
    items = sorted(env.open_reviews(document_id), key=lambda item: item.payload["group"])
    assert [(item.kind, item.payload["reason"]) for item in items] == [
        (ReviewKind.UNKNOWN_ACCOUNT, "account_not_found")
    ] * 2
    assert [found.name for found in _detected(env, document_id)] == names[:2]


def _received(env: Env, data: bytes, account: Account, on: date) -> uuid.UUID:
    """Upload ``data`` for ``account`` as if it had arrived ``on`` that day, then ingest it."""
    document_id = env.upload(data, account=account, name="export.csv")
    for db in env.db():
        db.execute(
            update(Document)
            .where(Document.id == document_id)
            .values(created_at=datetime(on.year, on.month, on.day, 9, 30, tzinfo=UTC))
        )
    assert env.run(INGEST_JOB_TYPE), "the upload queued no ingest job"
    return document_id


def test_a_date_order_that_dates_rows_after_the_upload_is_not_the_files(env: Env) -> None:
    """Uploaded on 20 March: the rows cannot be of April, September and November."""
    account = env.ledger.account(env.household_id, type=AccountType.CHECKING)
    document_id = _received(env, layouts.checking_dated(TIED_DATES), account, date(2026, 3, 20))
    assert env.document(document_id).status is DocumentStatus.PROCESSED, env.open_reviews(
        document_id
    )
    assert [t.posted_date for t in env.transactions(account)] == [
        date(2026, 2, 3),
        date(2026, 2, 9),
        date(2026, 3, 4),
        date(2026, 3, 11),
    ]
    (mapping,) = _mappings(env)
    assert mapping.spec["date_format"] == "MM/DD/YYYY"
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        preview = owner.get(f"{API}/imports/{document_id}/preview").json()
    assert preview["suggestion"]["doubts"] == []  # the editor is told the same


def test_a_date_order_the_upload_day_cannot_settle_waits_for_a_person(env: Env) -> None:
    """Uploaded in December, both orders are in the past: the order is still a guess."""
    account = env.ledger.account(env.household_id, type=AccountType.CHECKING)
    document_id = _received(env, layouts.checking_dated(TIED_DATES), account, date(2026, 12, 20))
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert item.payload["reason"] == "mapping_required"
    assert item.payload["auto_import"] == "not_confident"
    assert item.payload["suggestion"]["doubts"] == ["date_order_guessed"]
    assert env.transactions(account) == []
    assert _mappings(env) == []


# ---------------------------------------------------------------------------
# A table without a header gets a suggestion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "data",
    [
        b"Date,Description,Amount,Balance\r\n",
        b"Date,Description,Amount,Balance",
        b"Posted Date;Payee;Money Out;Money In\r\n\r\n\r\n",
    ],
    ids=["header", "no_newline", "blank_lines_after"],
)
def test_header_only_export_says_no_rows(env: Env, data: bytes) -> None:
    """A header and nothing under it: nothing to import, and nothing to map either."""
    account = env.ledger.account(env.household_id, type=AccountType.CHECKING)
    document_id = env.ingest(data, account=account, name="export.csv")
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.UNPARSED
    assert item.payload == {"reason": "no_rows", "source": "table", "table": 0}
    assert env.transactions(account) == []
    assert _mappings(env) == []
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        (shown,) = owner.get(f"{API}/review?document={document_id}").json()["items"]
    # The mapping editor has no row to read: it is not offered.
    assert shown["reason"] == "no_rows"
    assert shown["next_step"] == "reprocess"


def test_a_single_row_without_a_header_is_not_called_empty(env: Env) -> None:
    """One transaction and no header is a row, not a header: the columns are asked for."""
    account = env.ledger.account(env.household_id, type=AccountType.CHECKING)
    document_id = env.ingest(b"01/05/2026,Corner Bakery,-4.50\r\n", account=account, name="one.csv")
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.UNPARSED
    assert item.payload["reason"] == "mapping_required"
    assert item.payload["row_count"] == 1


def test_headerless_export_opens_a_prefilled_mapping(env: Env) -> None:
    """Signed amounts and no header: the columns are offered, never imported on a guess."""
    data = layouts.headerless_five_quoted_columns()
    account = env.ledger.account(env.household_id, type=AccountType.CHECKING)
    document_id = env.ingest(data, account=account, name="export.csv")
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.UNPARSED
    assert item.payload["reason"] == "mapping_required"
    assert item.payload["auto_import"] == "not_confident"
    assert item.payload["suggestion"]["confident"] is False
    assert item.payload["suggestion"]["doubts"] == ["no_header"]
    assert item.payload["suggestion"]["parsed_rows"] == len(layouts.HEADERLESS_SIGNED_ROWS)
    suggested = item.payload["suggested_spec"]
    assert suggested["has_header"] is False
    assert (suggested["date_column"], suggested["amount_column"], suggested["payee_column"]) == (
        0,
        1,
        4,
    )
    assert env.transactions(account) == []
    assert _mappings(env) == []
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        preview = owner.get(f"{API}/imports/{document_id}/preview").json()
        # The mapping editor opens with the same reading.
        assert preview["suggested_spec"] == suggested
        assert preview["suggestion"]["doubts"] == ["no_header"]
        assert {r["match"] for r in preview["suggestion"]["roles"]} == {"content"}
        # The person saves it as it is; the file is imported with it.
        cells = extract_delimited(data, ExtractOptions(), "csv").tables[0].rows
        with env.factories.transaction() as db:
            set_context(db, env.household_id)
            make_import_mapping(db, env.household_id, spec=suggested, rows=cells)
        applied = owner.post(
            f"{API}/review/{item.id}/resolve", json={"kind": "unparsed", "action": "apply_mapping"}
        )
        assert applied.status_code == 200, applied.text
    env.run(APPLY_MAPPING_JOB_TYPE)
    assert _amounts(env.transactions(account)) == sorted(
        (date(int(day[6:]), int(day[:2]), int(day[3:5])), Decimal(amount))
        for day, amount, _ in layouts.HEADERLESS_SIGNED_ROWS
    )


def test_headerless_export_with_a_reconciling_balance_imports_on_its_own(env: Env) -> None:
    data = layouts.headerless_with_balance()
    account = env.ledger.account(env.household_id, type=AccountType.CHECKING)
    document_id = env.ingest(data, account=account, name="export.csv")
    assert env.document(document_id).status is DocumentStatus.PROCESSED, env.open_reviews(
        document_id
    )
    rows = env.transactions(account)
    assert len(rows) == len(layouts.HEADERLESS_BALANCE_ROWS)
    assert sum(t.amount for t in rows) == Decimal("4516.35") - layouts.HEADERLESS_OPENING
    assert sum(1 for t in rows if t.amount < 0) == 6
    (mapping,) = _mappings(env)
    assert mapping.spec["has_header"] is False
    assert (mapping.spec["debit_column"], mapping.spec["credit_column"]) == (3, 4)
    assert mapping.spec["balance_column"] == 5
    assert mapping.sign_unverified is False
    # A mapping saved without a header is not named after the first transaction.
    assert mapping.name == "No header, 6 columns"
    assert "PAYROLL" not in _trail(env)
    # The next file of the same shape is read with the saved mapping.
    second = env.ingest(data.replace(b"/2026", b"/2025"), account=account, name="older.csv")
    assert env.document(second).status is DocumentStatus.PROCESSED, env.open_reviews(second)
    assert len(_mappings(env)) == 1
    assert len(env.transactions(account)) == 2 * len(layouts.HEADERLESS_BALANCE_ROWS)


def test_headerless_card_export_is_never_imported_on_its_balance_owed(env: Env) -> None:
    """Into an empty household: purchases are not income because the balance owed adds up."""
    document_id = env.ingest(layouts.headerless_card_with_balance_owed(), name="card.csv")
    assert _accept_proposals(env, document_id) == []
    assert _ledger_rows(env) == 0
    (item,) = env.open_reviews(document_id)
    assert item.payload["reason"] == "mapping_required"
    assert "sign_unverified" in item.payload["suggestion"]["doubts"]
    assert _mappings(env) == []
