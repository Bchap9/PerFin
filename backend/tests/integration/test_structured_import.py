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
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel
from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.models import AuditLog, Event, Household, Job
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType, BalanceSnapshot, BalanceSource
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentStatus,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE
from app.domain.transactions.models import ReviewState, Transaction, TransactionSplit
from app.domain.transactions.schemas import AUTO_PAIR_JOB_TYPE
from app.ingestion import pipeline
from app.ingestion.intake.upload import receive_upload
from app.workers.handlers import ingest
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.households import Actors, problem_type
from tests.fixtures.imports import make_import_mapping, run_jobs
from tests.fixtures.ledger import LedgerFactories, make_category, make_rule
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
        assert stmt.account_number[-4:] not in str(imported.after)
        assert "Initech" not in str(imported.after)  # no statement text in the trail
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
def test_csv_layouts_import_with_a_saved_mapping(env: Env, layout: str) -> None:
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
# Deduplication
# ---------------------------------------------------------------------------


def test_reimport_is_idempotent(env: Env) -> None:
    stmt = sample_statement(27)
    account = env.checking(stmt)
    first = env.ingest(ofx_sgml_bytes(stmt))
    count = len(env.transactions(account))
    # The same statement again, as different bytes (OFX 2 instead of 1).
    second = env.ingest(ofx_xml_bytes(stmt))
    assert env.document(second).status is DocumentStatus.PROCESSED
    assert len(env.transactions(account)) == count
    # Re-running the first document's import changes nothing either.
    with Actors.open(env.settings, env.factories) as actors:
        owner = actors.login(env.owner)
        assert owner.post(f"{API}/imports/{first}/apply-mapping", json={}).status_code == 202
    assert env.run(APPLY_MAPPING_JOB_TYPE)
    assert len(env.transactions(account)) == count
    assert env.document(first).status is DocumentStatus.PROCESSED
    for db in env.db():
        logs: list[dict[str, Any]] = [
            a.after or {}
            for a in db.execute(
                select(AuditLog)
                .where(AuditLog.action.in_(["document.ingest", "document.import"]))
                .order_by(AuditLog.seq)
            ).scalars()
        ]
    assert [(log["imported"], log["duplicates"]) for log in logs] == [
        (count, 0),
        (0, count),
        (0, count),
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
