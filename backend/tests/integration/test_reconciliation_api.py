# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reconcile view: a statement checked against the ledger rows its document produced.

``GET /documents/{id}/reconciliation`` (``app.domain.review.reconciliation``).
Documents go through the real pipeline (upload -> worker -> sandbox ->
parser -> importer) with the production dependencies on a temporary blob
store, as in ``test_pdf_statement_pipeline.py``; the view is then read
through the API as a person would. PDFs and OFX files come from the
synthetic builders (``tests/fixtures``: fictional institutions, invented
numbers).

* The sum is the ledger's: an edited row changes it, a deleted row leaves it
  and is listed as removed, a row that first came from another file counts.
* Balances are shown as printed (a card's balance owed is positive).
* Each statement of a document is checked on its own account, with its own
  rows; a statement on an account the caller cannot see is left out.
* A viewer reads and cannot edit; a document without a statement is ``409``.
* ``read_printed`` reads a balance as a person types it.
* The exits: confirming a statement that balances posts its rows, records
  its balance and closes its findings; accepting one as read posts the rows
  and records no balance; a removed row can be put back. A member who may
  not edit the account is refused before anything changes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, select, update

from app.auth.models import AccountGrant, GrantLevel, HouseholdRole
from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import Keyring
from app.core.errors import ProblemError
from app.core.settings import Settings
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
from app.domain.imports.models import ImportTombstone, StatementRecord
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE
from app.domain.imports.service import import_extraction
from app.domain.review import reconciliation
from app.domain.review.layout import stored_result
from app.domain.review.models import ImportHashAlias
from app.domain.review.reconciliation import read_printed
from app.domain.transactions.models import ReviewState, Transaction
from app.ingestion.intake.upload import receive_upload
from app.ingestion.parsers.template import rebalance
from app.ingestion.sandbox.protocol import ExtractResult
from app.workers.handlers import ingest
from tests.fixtures import synth_neutral_report as neutral
from tests.fixtures import synth_pdf_statements as synth
from tests.fixtures.documents import make_document, make_review_item
from tests.fixtures.factories import AuthedClient, Factories, UserRecord
from tests.fixtures.households import API, Actors, problem_type
from tests.fixtures.imports import run_jobs
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.synth_statements import SynthStatement, ofx_sgml_bytes

pytestmark = pytest.mark.integration

MAX = 10 * 1024 * 1024
PDF = b"%PDF-1.7\n% synthetic placeholder, never parsed by the API\n%%EOF\n"


@dataclass
class Env:
    factories: Factories
    ledger: LedgerFactories
    store: LocalFSBlobStore
    keyring: Keyring
    settings: Settings
    owner: UserRecord
    actors: Actors
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

    def ingest(
        self, data: bytes, *, name: str = "statement.pdf", as_user: UserRecord | None = None
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
                account_id=None,
            )
        assert run_jobs(self.factories, self.household_id, INGEST_JOB_TYPE, done=self.done)
        return received.document_id

    def run(self, job_type: str) -> list[uuid.UUID]:
        return run_jobs(self.factories, self.household_id, job_type, done=self.done)

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

    def account(self, stmt: SynthStatement) -> Account:
        institution = self.ledger.institution(self.household_id, name=stmt.institution.name)
        kind = AccountType.CREDIT_CARD if stmt.account_type == "creditcard" else "checking"
        return self.ledger.account(
            self.household_id,
            number_last4=stmt.last4,
            type=kind,
            currency=stmt.currency,
            institution_id=institution.id,
        )

    def client(self, record: UserRecord | None = None) -> AuthedClient:
        return self.actors.login(record or self.owner)

    def user(self, role: HouseholdRole) -> UserRecord:
        return self.factories.user(role=role, household=self.owner.household, with_totp=True)


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
            settings=local,
            owner=factories.user(role="owner", with_totp=True),
            actors=actors,
        )
    ingest.default_deps.cache_clear()


def view(client: AuthedClient, document_id: uuid.UUID) -> dict[str, Any]:
    response = client.get(f"{API}/documents/{document_id}/reconciliation")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    assert body["document_id"] == str(document_id)
    return body


def one(client: AuthedClient, document_id: uuid.UUID) -> dict[str, Any]:
    (statement,) = view(client, document_id)["statements"]
    found: dict[str, Any] = statement
    return found


def amounts(statement: dict[str, Any]) -> list[tuple[str, Decimal]]:
    return sorted((r["posted_date"], Decimal(r["amount"])) for r in statement["rows"])


def tampered(env: Env, seed: int) -> tuple[SynthStatement, Account, uuid.UUID, Transaction]:
    """A statement with one row its balances do not account for, imported and held."""
    stmt = synth.generic_statement(seed=seed)
    account = env.account(stmt)
    document_id = env.ingest(synth.generic_pdf(stmt, tamper="extra_row"))
    forged = next(r for r in env.transactions(account) if r.amount == synth.FORGED.amount)
    return stmt, account, document_id, forged


# ---------------------------------------------------------------------------
# The sum is the ledger's
# ---------------------------------------------------------------------------


def test_reconciliation_lists_the_ledger_rows_and_the_sum(env: Env) -> None:
    stmt, account, document_id, forged = tampered(env, 61)
    owner = env.client()

    got = one(owner, document_id)

    assert (got["statement"], got["account_id"]) == (0, str(account.id))
    assert (got["account_name"], got["currency"]) == (account.name, stmt.currency)
    assert got["printed_sign"] == "ledger"
    assert got["status"] == "off"
    assert Decimal(got["difference"]) == synth.FORGED.amount
    assert Decimal(got["opening"]) == stmt.opening_balance
    assert Decimal(got["closing"]) == stmt.closing_balance
    assert got["previous_closing"] is None
    ledger_rows = env.transactions(account)
    assert amounts(got) == sorted((r.posted_date.isoformat(), r.amount) for r in ledger_rows)
    assert Decimal(got["total"]) == sum((r.amount for r in ledger_rows), Decimal(0))
    assert Decimal(got["total"]) == stmt.closing_balance - stmt.opening_balance + forged.amount
    assert got["rows_truncated"] is False
    assert got["removed"] == []
    assert got["can_edit"] is True
    assert got["verification"] == "unverified"

    first = got["causes"][0]
    assert first["kind"] == "extra_row"
    assert first["transaction_ids"] == [str(forged.id)]
    assert Decimal(first["amount"]) == synth.FORGED.amount
    assert all(isinstance(r["page"], int) and len(r["bbox"]) == 4 for r in got["rows"])
    assert {(r["origin"], r["in_period"]) for r in got["rows"]} == {("read", True)}
    shown = next(r for r in got["rows"] if r["transaction_id"] == str(forged.id))
    assert (shown["payee"], shown["review_state"]) == (forged.raw_payee, "pending")


def test_an_edited_row_changes_the_sum(env: Env) -> None:
    _, _, document_id, forged = tampered(env, 62)
    owner = env.client()

    changed = owner.patch(
        f"{API}/transactions/{forged.id}", json={"amount": {"amount": "0", "currency": "USD"}}
    )
    assert changed.status_code == 200, changed.text

    got = one(owner, document_id)
    assert got["status"] == "balanced"
    assert Decimal(got["difference"]) == 0
    shown = next(r for r in got["rows"] if r["transaction_id"] == str(forged.id))
    assert shown["edited"] is True
    assert Decimal(shown["amount"]) == 0
    # The row keeps its place on the page although its amount is no longer the printed one.
    assert isinstance(shown["page"], int)
    assert "extra_row" not in [c["kind"] for c in got["causes"]]


def test_a_deleted_row_is_listed_as_removed_and_leaves_the_sum(env: Env) -> None:
    stmt, account, document_id, forged = tampered(env, 63)
    owner = env.client()

    deleted = owner.delete(f"{API}/transactions/{forged.id}")
    assert deleted.status_code == 204, deleted.text

    got = one(owner, document_id)
    assert got["status"] == "balanced"
    assert Decimal(got["total"]) == stmt.closing_balance - stmt.opening_balance
    assert str(forged.id) not in [r["transaction_id"] for r in got["rows"]]
    (removed,) = got["removed"]
    assert Decimal(removed["amount"]) == synth.FORGED.amount
    assert removed["posted_date"] == synth.FORGED.posted.isoformat()
    assert uuid.UUID(removed["tombstone_id"])
    assert len(got["rows"]) == len(env.transactions(account)) == len(stmt.transactions)


def test_card_balances_are_shown_as_printed(env: Env) -> None:
    stmt = synth.demo_card_statement(seed=64)
    account = env.account(stmt)
    document_id = env.ingest(synth.demo_card_pdf(stmt))
    assert stmt.closing_balance < 0  # owed: negative in the ledger

    got = one(env.client(), document_id)

    assert got["printed_sign"] == "owed"
    assert Decimal(got["closing"]) == -stmt.closing_balance > 0
    assert Decimal(got["opening"]) == -stmt.opening_balance
    stored = stored_result(env.document(document_id).extraction)
    assert stored is not None
    balance = stored.statements[0].ledger_balance
    assert balance is not None
    assert balance.amount == stmt.closing_balance
    # The sum and the difference stay in ledger terms: spending is negative.
    assert Decimal(got["total"]) == stmt.closing_balance - stmt.opening_balance < 0
    assert got["status"] == "balanced"
    assert got["verification"] == "reconciled"
    assert len(got["rows"]) == len(env.transactions(account))


def test_a_row_that_came_from_another_file_counts(env: Env) -> None:
    stmt = synth.sample_national_statement(seed=65)
    account = env.account(stmt)
    first = env.ingest(ofx_sgml_bytes(stmt), name="statement.ofx")
    count = len(env.transactions(account))
    assert count == len(stmt.transactions)
    second = env.ingest(synth.sample_national_pdf(stmt))
    (item,) = env.open_reviews(second)
    assert item.payload["reason"] == "statement_overlap"
    owner = env.client()
    resolved = owner.post(
        f"{API}/review/{item.id}/resolve",
        json={"kind": "possible_duplicate", "decision": "reject"},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["resolution"]["import_anyway"] is True
    assert env.run(APPLY_MAPPING_JOB_TYPE)
    rows = env.transactions(account)
    assert len(rows) == count
    assert {r.source_document_id for r in rows} == {first}

    got = one(owner, second)

    assert got["status"] == "balanced"
    assert len(got["rows"]) == count
    assert {r["origin"] for r in got["rows"]} == {"matched"}
    assert Decimal(got["total"]) == stmt.closing_balance - stmt.opening_balance
    assert all(isinstance(r["page"], int) for r in got["rows"])


# ---------------------------------------------------------------------------
# Several statements in one document
# ---------------------------------------------------------------------------


def _neutral(env: Env, uploader: UserRecord | None = None) -> tuple[Account, Account, uuid.UUID]:
    bank = env.ledger.institution(env.household_id, name=neutral.INSTITUTION)
    checking, savings = (
        env.ledger.account(
            env.household_id,
            type=kind,
            number_last4=last4,
            currency="USD",
            institution_id=bank.id,
            name=name,
        )
        for kind, last4, name in (
            (AccountType.CHECKING, "4821", "Checking"),
            (AccountType.SAVINGS, "5530", "Savings"),
        )
    )
    if uploader is not None:
        for account in (checking, savings):
            env.ledger.grant(env.household_id, uploader.user.id, account, GrantLevel.EDIT)
    document_id = env.ingest(
        neutral.report_pdf(neutral.report()), name="combined-statement.pdf", as_user=uploader
    )
    return checking, savings, document_id


def test_rows_of_another_account_do_not_count(env: Env) -> None:
    checking, savings, document_id = _neutral(env)

    statements = view(env.client(), document_id)["statements"]

    by_account = {s["account_id"]: s for s in statements}
    assert set(by_account) == {str(checking.id), str(savings.id)}
    assert len({s["statement"] for s in statements}) == 2
    for account in (checking, savings):
        got = by_account[str(account.id)]
        mine = env.transactions(account)
        assert mine, account.name
        assert sorted(r["transaction_id"] for r in got["rows"]) == sorted(str(r.id) for r in mine)
        assert Decimal(got["total"]) == sum((r.amount for r in mine), Decimal(0))
        assert got["account_name"] == account.name
    assert not {r["transaction_id"] for r in by_account[str(checking.id)]["rows"]} & {
        r["transaction_id"] for r in by_account[str(savings.id)]["rows"]
    }


Entry = tuple[int, str, str]


def _month(
    last4: str, month: int, last_day: int, opening: str, closing: str, entries: list[Entry]
) -> dict[str, Any]:
    """One month's statement as a parser stores it (2026; balances in ledger terms)."""
    start, end = date(2026, month, 1), date(2026, month, last_day)
    return {
        "source_format": "pdf",
        "account": {"last4": last4, "currency": "USD"},
        "period_start": start.isoformat(),
        "period_end": end.isoformat(),
        "opening_balance": {"amount": opening, "as_of": start.isoformat()},
        "ledger_balance": {"amount": closing, "as_of": end.isoformat()},
        "transactions": [
            {"posted": date(2026, month, day).isoformat(), "amount": amount, "payee": payee}
            for day, amount, payee in entries
        ],
        "confidence": {"period": 1.0},
    }


def _stored(env: Env, *statements: dict[str, Any], checked: bool = False) -> uuid.UUID:
    """A PDF document whose stored extraction holds ``statements``, imported.

    ``checked``: each statement carries its check as a document parser
    leaves it; one that does not add up has every row in doubt, so the
    importer holds it.
    """
    read = ExtractResult.model_validate({"kind": "pdf", "ok": True, "statements": list(statements)})
    if checked:
        doubted = [
            s.model_copy(
                update={
                    "transactions": [
                        t.model_copy(update={"low_confidence": ["reconciliation"]})
                        for t in s.transactions
                    ]
                }
            )
            for s in read.statements
        ]
        read = read.model_copy(update={"statements": [rebalance(s) for s in doubted]})
    extraction = read.model_dump(mode="json")
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
            extraction=extraction,
        )
        document_id = document.id
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        fresh = db.get(Document, document_id)
        assert fresh is not None
        result = stored_result(fresh.extraction)
        assert result is not None
        import_extraction(db, fresh, result)
    return document_id


def test_two_statements_of_one_document_are_separate(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="7306", name="Everyday")
    document_id = _stored(
        env,
        _month(
            "7306",
            4,
            30,
            "100.00",
            "80.50",
            [(3, "-12.25", "Lamplight Books"), (9, "-7.25", "Corner Bakery")],
        ),
        # 80.50 - 7.25 + 40.00 is 113.25: the printed closing is 9.00 short.
        _month(
            "7306",
            5,
            31,
            "80.50",
            "104.25",
            [(9, "-7.25", "Corner Bakery"), (20, "40.00", "Refund Desk")],
        ),
    )
    assert len(env.transactions(account)) == 4
    owner = env.client()

    april, may = view(owner, document_id)["statements"]

    assert (april["statement"], may["statement"]) == (0, 1)
    assert april["account_id"] == may["account_id"] == str(account.id)
    assert amounts(april) == [("2026-04-03", Decimal("-12.25")), ("2026-04-09", Decimal("-7.25"))]
    assert amounts(may) == [("2026-05-09", Decimal("-7.25")), ("2026-05-20", Decimal("40.00"))]
    assert (april["status"], Decimal(april["difference"])) == ("balanced", Decimal(0))
    assert (may["status"], Decimal(may["difference"])) == ("off", Decimal("9.00"))
    assert may["causes"][0]["kind"] == "transposed_digits"
    assert (april["period_start"], may["period_end"]) == ("2026-04-01", "2026-05-31")
    # The previous statement is one of another document: April of this file is
    # not held against May's opening balance.
    assert [r.period_start for r in records(env, document_id)] == [
        date(2026, 4, 1),
        date(2026, 5, 1),
    ]
    assert (april["previous_closing"], may["previous_closing"]) == (None, None)

    # A row a person adds for the document has no hash: its date says which statement.
    added = owner.post(
        f"{API}/transactions",
        json={
            "account_id": str(account.id),
            "posted_date": "2026-05-22",
            "amount": {"amount": "-9.00", "currency": "USD"},
            "raw_payee": "Ferry Kiosk",
            "source_document_id": str(document_id),
        },
    )
    assert added.status_code == 201, added.text
    april, may = view(owner, document_id)["statements"]
    assert len(april["rows"]) == 2
    assert may["status"] == "balanced"
    shown = next(r for r in may["rows"] if r["transaction_id"] == added.json()["id"])
    assert (shown["origin"], shown["page"], shown["bbox"]) == ("added", None, None)


def test_the_previous_closing_is_the_accounts_last_statement_as_printed(env: Env) -> None:
    card = env.ledger.account(
        env.household_id, number_last4="7411", name="Travel card", type=AccountType.CREDIT_CARD
    )
    april = _stored(
        env,
        _month(
            "7411",
            4,
            30,
            "0.00",
            "-19.50",
            [(3, "-12.25", "Lamplight Books"), (9, "-7.25", "Corner Bakery")],
        ),
    )
    # May opens ten more owed than April closed.
    may = _stored(env, _month("7411", 5, 31, "-29.50", "-36.75", [(9, "-7.25", "Corner Bakery")]))
    assert len(env.transactions(card)) == 3
    owner = env.client()

    first, second = one(owner, april), one(owner, may)

    assert (first["previous_closing"], first["status"]) == (None, "balanced")
    assert first["causes"] == []
    assert second["printed_sign"] == "owed"
    assert (second["opening"], second["closing"], second["previous_closing"]) == (
        "29.50",
        "36.75",
        "19.50",
    )
    assert (second["total"], second["difference"], second["status"]) == (
        "-7.25",
        "0.00",
        "balanced",
    )
    assert second["causes"] == [
        {
            "kind": "opening_differs_from_previous",
            "transaction_ids": [],
            "amount": "19.50",
            "page": None,
            "bbox": None,
        }
    ]


# ---------------------------------------------------------------------------
# Who sees what
# ---------------------------------------------------------------------------


def test_viewer_reads_and_cannot_edit(env: Env) -> None:
    _, account, document_id, forged = tampered(env, 66)
    viewer = env.user(HouseholdRole.VIEWER)
    env.ledger.grant(env.household_id, viewer.user.id, account, GrantLevel.VIEW)

    got = one(env.client(viewer), document_id)

    assert got["can_edit"] is False
    assert got["status"] == "off"
    assert str(forged.id) in [r["transaction_id"] for r in got["rows"]]
    assert one(env.client(), document_id)["can_edit"] is True


def test_a_member_who_may_only_view_the_account_cannot_edit(env: Env) -> None:
    _, account, document_id, _ = tampered(env, 67)
    member = env.user(HouseholdRole.MEMBER)
    env.ledger.grant(env.household_id, member.user.id, account, GrantLevel.VIEW)

    assert one(env.client(member), document_id)["can_edit"] is False


def test_statement_on_an_invisible_account_is_left_out(env: Env) -> None:
    member = env.user(HouseholdRole.MEMBER)
    checking, savings, document_id = _neutral(env, uploader=member)
    reader = env.client(member)
    assert {s["account_id"] for s in view(reader, document_id)["statements"]} == {
        str(checking.id),
        str(savings.id),
    }
    # The member's access to the savings account is taken away.
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        db.execute(
            delete(AccountGrant).where(
                AccountGrant.user_id == member.user.id, AccountGrant.account_id == savings.id
            )
        )

    (left,) = view(reader, document_id)["statements"]

    assert left["account_id"] == str(checking.id)
    savings_rows = {str(r.id) for r in env.transactions(savings)}
    assert savings_rows
    assert not savings_rows & {r["transaction_id"] for r in left["rows"]}
    assert len(view(env.client(), document_id)["statements"]) == 2


def test_a_hidden_document_is_404(env: Env) -> None:
    _, _, document_id, _ = tampered(env, 68)
    stranger = env.user(HouseholdRole.MEMBER)

    refused = env.client(stranger).get(f"{API}/documents/{document_id}/reconciliation")

    assert refused.status_code == 404, refused.text


def test_document_without_a_statement_is_409(env: Env) -> None:
    document_id = env.ingest(synth.payslip_pdf(), name="payslip.pdf")

    refused = env.client().get(f"{API}/documents/{document_id}/reconciliation")

    assert refused.status_code == 409, refused.text
    assert problem_type(refused) == "statement-unavailable"


# ---------------------------------------------------------------------------
# The exits: confirm, accept, put back
# ---------------------------------------------------------------------------


def act(
    client: AuthedClient, document_id: uuid.UUID, action: str, **body: Any
) -> tuple[int, dict[str, Any]]:
    response = client.post(f"{API}/documents/{document_id}/reconciliation/{action}", json=body)
    found: dict[str, Any] = response.json()
    return response.status_code, found


def snapshots(env: Env, account: Account) -> list[BalanceSnapshot]:
    return env.query(
        select(BalanceSnapshot)
        .where(BalanceSnapshot.account_id == account.id)
        .order_by(BalanceSnapshot.as_of_date, BalanceSnapshot.id)
    )


def records(env: Env, document_id: uuid.UUID) -> list[StatementRecord]:
    return env.query(
        select(StatementRecord)
        .where(StatementRecord.document_id == document_id)
        .order_by(StatementRecord.period_start, StatementRecord.id)
    )


def items(env: Env, document_id: uuid.UUID) -> list[ReviewItem]:
    return env.query(
        select(ReviewItem)
        .where(ReviewItem.document_id == document_id)
        .order_by(ReviewItem.created_at, ReviewItem.id)
    )


def states(env: Env, account: Account) -> list[tuple[uuid.UUID, ReviewState]]:
    return [(r.id, r.review_state) for r in env.transactions(account)]


def test_confirm_writes_the_snapshot_and_closes_the_findings(env: Env) -> None:
    stmt, account, document_id, forged = tampered(env, 71)
    owner = env.client()
    assert owner.delete(f"{API}/transactions/{forged.id}").status_code == 204
    pending = [r for r in env.transactions(account) if r.review_state is ReviewState.PENDING]
    assert len(env.transactions(account)) == len(stmt.transactions)
    assert env.open_reviews(document_id)
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert snapshots(env, account) == []

    status, got = act(owner, document_id, "confirm", statement=0)

    assert status == 200, got
    assert got["confirmed"] == len(pending)
    assert got["snapshot"] is True
    assert got["items_closed"] >= 1
    assert (got["statement"]["status"], got["statement"]["verification"]) == (
        "balanced",
        "reconciled",
    )
    assert ReviewState.PENDING not in {state for _, state in states(env, account)}
    assert "pending" not in {r["review_state"] for r in got["statement"]["rows"]}
    (snapshot,) = snapshots(env, account)
    assert snapshot.balance == stmt.closing_balance
    assert snapshot.source is BalanceSource.STATEMENT
    assert snapshot.source_document_id == document_id
    assert snapshot.as_of_date == got_date(got["statement"]["period_end"])
    (record,) = records(env, document_id)
    assert record.verification == "reconciled"
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    closed = [i for i in items(env, document_id) if i.state is ReviewItemState.RESOLVED]
    assert len(closed) == got["items_closed"]
    for item in closed:
        assert item.resolution is not None
        assert item.resolution["reconciled"] is True
        assert item.resolution["account_id"] == str(account.id)
        assert item.resolution["statement"] == 0


def got_date(text: str) -> date:
    return date.fromisoformat(text)


def test_confirm_refuses_while_off(env: Env) -> None:
    stmt, account, document_id, _ = tampered(env, 72)
    owner = env.client()
    before = states(env, account)
    stored = env.document(document_id).extraction
    open_before = [i.id for i in env.open_reviews(document_id)]
    assert open_before

    status, got = act(owner, document_id, "confirm", statement=0)

    assert status == 409, got
    assert got["type"].endswith("statement-does-not-reconcile")
    assert "777.77" in got["detail"]
    # A typed balance that still leaves a difference is not kept either.
    status, got = act(owner, document_id, "confirm", statement=0, closing_balance="1.00")
    assert status == 409, got
    assert got["type"].endswith("statement-does-not-reconcile")
    status, got = act(owner, document_id, "confirm", statement=0, closing_balance="1,20")
    assert (status, got["type"].rsplit(":", 1)[-1]) == (422, "invalid-amount")
    status, got = act(owner, document_id, "confirm", statement=3)
    assert status == 422, got
    assert got["type"].endswith("statement-not-found")

    assert states(env, account) == before
    assert ReviewState.PENDING in {state for _, state in before}
    assert snapshots(env, account) == []
    assert [r.verification for r in records(env, document_id)] == ["unverified"]
    assert [i.id for i in env.open_reviews(document_id)] == open_before
    assert env.document(document_id).extraction == stored
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert Decimal(one(owner, document_id)["closing"]) == stmt.closing_balance


def test_confirm_takes_card_balances_as_printed(env: Env) -> None:
    card = env.ledger.account(
        env.household_id, number_last4="7523", name="Daily card", type=AccountType.CREDIT_CARD
    )
    # 1,000.00 owed, 19.50 spent: 1,019.50 is owed at the end. The reading has 1,091.50.
    document_id = _stored(
        env,
        _month(
            "7523",
            4,
            30,
            "-1000.00",
            "-1091.50",
            [(3, "-12.25", "Lamplight Books"), (9, "-7.25", "Corner Bakery")],
        ),
        checked=True,
    )
    assert {state for _, state in states(env, card)} == {ReviewState.PENDING}
    assert snapshots(env, card) == []
    owner = env.client()
    held = one(owner, document_id)
    assert (held["printed_sign"], held["closing"], held["status"]) == ("owed", "1091.50", "off")
    assert Decimal(held["difference"]) == Decimal("72.00")

    status, got = act(owner, document_id, "confirm", statement=0, closing_balance="$1,019.50")

    assert status == 200, got
    assert (got["statement"]["closing"], got["statement"]["status"]) == ("1019.50", "balanced")
    assert (got["confirmed"], got["snapshot"]) == (2, True)
    (snapshot,) = snapshots(env, card)
    assert (snapshot.balance, snapshot.as_of_date) == (Decimal("-1019.50"), date(2026, 4, 30))
    (record,) = records(env, document_id)
    assert (record.verification, record.closing_balance) == ("reconciled", Decimal("-1019.50"))
    stored = stored_result(env.document(document_id).extraction)
    assert stored is not None
    balance = stored.statements[0].ledger_balance
    assert balance is not None
    assert balance.amount == Decimal("-1019.50")
    assert {state for _, state in states(env, card)} == {ReviewState.CONFIRMED}
    assert env.open_reviews(document_id) == []


def test_accept_posts_the_rows_and_writes_no_snapshot(env: Env) -> None:
    stmt, account, document_id, forged = tampered(env, 73)
    owner = env.client()
    assert len(env.transactions(account)) == len(stmt.transactions) + 1
    count = sum(1 for _, state in states(env, account) if state is ReviewState.PENDING)
    assert count >= 1

    status, got = act(owner, document_id, "accept", statement=0)

    assert status == 200, got
    assert (got["confirmed"], got["snapshot"]) == (count, False)
    assert got["items_closed"] >= 1
    assert (got["statement"]["status"], got["statement"]["verification"]) == ("off", "accepted")
    assert Decimal(got["statement"]["difference"]) == forged.amount
    assert [r.verification for r in records(env, document_id)] == ["accepted"]
    assert snapshots(env, account) == []
    assert (
        env.query(select(BalanceSnapshot).where(BalanceSnapshot.source_document_id == document_id))
        == []
    )
    assert ReviewState.PENDING not in {state for _, state in states(env, account)}
    assert ReviewState.CONFIRMED in {state for _, state in states(env, account)}
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    closed = [i for i in items(env, document_id) if i.state is ReviewItemState.RESOLVED]
    assert len(closed) == got["items_closed"]
    for item in closed:
        assert item.resolution is not None
        assert item.resolution["accepted"] is True
        assert item.resolution["account_id"] == str(account.id)
        assert "reconciled" not in item.resolution


def test_accept_on_a_balanced_statement_is_refused(env: Env) -> None:
    _, account, document_id, forged = tampered(env, 74)
    owner = env.client()
    assert owner.delete(f"{API}/transactions/{forged.id}").status_code == 204
    before = states(env, account)
    open_before = [i.id for i in env.open_reviews(document_id)]

    status, got = act(owner, document_id, "accept", statement=0)

    assert status == 409, got
    assert got["type"].endswith("statement-balanced")
    assert states(env, account) == before
    assert [r.verification for r in records(env, document_id)] == ["unverified"]
    assert [i.id for i in env.open_reviews(document_id)] == open_before


def test_restore_puts_a_deleted_row_back_pending(env: Env) -> None:
    stmt, account, document_id, forged = tampered(env, 75)
    owner = env.client()
    assert owner.delete(f"{API}/transactions/{forged.id}").status_code == 204
    (removed,) = one(owner, document_id)["removed"]

    status, got = act(owner, document_id, "restore", tombstone_id=removed["tombstone_id"])

    assert status == 200, got
    assert (got["confirmed"], got["snapshot"], got["items_closed"]) == (0, False, 0)
    assert got["statement"]["removed"] == []
    assert got["statement"]["status"] == "off"
    assert Decimal(got["statement"]["difference"]) == forged.amount
    rows = env.transactions(account)
    assert len(rows) == len(stmt.transactions) + 1
    (back,) = [r for r in rows if r.amount == forged.amount]
    assert back.id != forged.id
    assert (back.posted_date, back.raw_payee) == (forged.posted_date, forged.raw_payee)
    assert (back.review_state, back.source_document_id) == (ReviewState.PENDING, document_id)
    assert back.import_hash == forged.import_hash
    assert env.query(select(ImportTombstone).where(ImportTombstone.account_id == account.id)) == []
    shown = next(r for r in got["statement"]["rows"] if r["transaction_id"] == str(back.id))
    assert (shown["origin"], shown["review_state"]) == ("read", "pending")
    # Taken back once: there is no such tombstone a second time.
    status, again = act(owner, document_id, "restore", tombstone_id=removed["tombstone_id"])
    assert (status, again["type"].rsplit(":", 1)[-1]) == (404, "tombstone-not-found")
    assert len(env.transactions(account)) == len(rows)


def test_a_row_put_back_into_a_confirmed_statement_unchecks_it(env: Env) -> None:
    _, account, document_id, forged = tampered(env, 79)
    owner = env.client()
    assert owner.delete(f"{API}/transactions/{forged.id}").status_code == 204
    status, got = act(owner, document_id, "confirm", statement=0)
    assert (status, got["statement"]["verification"]) == (200, "reconciled")
    (removed,) = got["statement"]["removed"]

    status, got = act(owner, document_id, "restore", tombstone_id=removed["tombstone_id"])

    assert status == 200, got
    assert (got["statement"]["status"], got["statement"]["verification"]) == ("off", "unverified")
    assert [r.verification for r in records(env, document_id)] == ["unverified"]
    waiting = [i for i, state in states(env, account) if state is ReviewState.PENDING]
    assert len(waiting) == 1


def test_restore_refuses_a_tombstone_of_another_document(env: Env) -> None:
    _, account, document_id, _ = tampered(env, 76)
    other = synth.sample_national_statement(seed=77)
    other_account = env.account(other)
    other_document = env.ingest(synth.sample_national_pdf(other, tamper="extra_row"))
    other_forged = next(
        r for r in env.transactions(other_account) if r.amount == synth.FORGED.amount
    )
    owner = env.client()
    assert owner.delete(f"{API}/transactions/{other_forged.id}").status_code == 204
    (removed,) = one(owner, other_document)["removed"]
    before = states(env, account), states(env, other_account)

    status, got = act(owner, document_id, "restore", tombstone_id=removed["tombstone_id"])

    assert (status, got["type"].rsplit(":", 1)[-1]) == (404, "tombstone-not-found")
    status, got = act(owner, document_id, "restore", tombstone_id=str(uuid.uuid4()))
    assert (status, got["type"].rsplit(":", 1)[-1]) == (404, "tombstone-not-found")
    assert (states(env, account), states(env, other_account)) == before
    assert [r["tombstone_id"] for r in one(owner, other_document)["removed"]] == [
        removed["tombstone_id"]
    ]


def test_remove_earlier_changes_nothing_yet(env: Env) -> None:
    _, account, document_id, _ = tampered(env, 78)
    before = states(env, account)

    status, got = act(env.client(), document_id, "remove-earlier", statement=0)

    assert status == 200, got
    assert (got["confirmed"], got["snapshot"], got["items_closed"]) == (0, False, 0)
    assert got["statement"]["status"] == "off"
    assert states(env, account) == before


def test_member_without_edit_on_the_account_is_refused(env: Env) -> None:
    member = env.user(HouseholdRole.MEMBER)
    checking, savings, document_id = _neutral(env, uploader=member)
    owner = env.client()
    victim = env.transactions(savings)[0]
    assert owner.delete(f"{API}/transactions/{victim.id}").status_code == 204
    # The member uploaded the file and may still change it, but may only view the savings account.
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        db.execute(
            update(AccountGrant)
            .where(AccountGrant.user_id == member.user.id, AccountGrant.account_id == savings.id)
            .values(level=GrantLevel.VIEW)
        )
    asker = env.client(member)
    shown = {s["account_id"]: s for s in view(asker, document_id)["statements"]}
    assert shown[str(checking.id)]["can_edit"] is True
    mine = shown[str(savings.id)]
    assert mine["can_edit"] is False
    (removed,) = mine["removed"]
    before = (
        states(env, savings),
        [(r.account_id, r.verification) for r in records(env, document_id)],
        [(i.id, i.state) for i in items(env, document_id)],
        [(s.id, s.balance) for s in snapshots(env, savings)],
        env.document(document_id).extraction,
    )

    answers = [
        act(asker, document_id, "confirm", statement=mine["statement"], closing_balance="5.00"),
        act(asker, document_id, "accept", statement=mine["statement"]),
        act(asker, document_id, "restore", tombstone_id=removed["tombstone_id"]),
        act(asker, document_id, "remove-earlier", statement=mine["statement"]),
    ]

    # What ``get_visible_account(need="edit")`` answers: the account is not theirs to change.
    assert [(status, body["type"].rsplit(":", 1)[-1]) for status, body in answers] == [
        (404, "account-not-found")
    ] * 4
    assert before == (
        states(env, savings),
        [(r.account_id, r.verification) for r in records(env, document_id)],
        [(i.id, i.state) for i in items(env, document_id)],
        [(s.id, s.balance) for s in snapshots(env, savings)],
        env.document(document_id).extraction,
    )
    assert len(one_of(owner, document_id, savings)["removed"]) == 1
    # A viewer, and a member who cannot see the file, never get that far.
    viewer = env.user(HouseholdRole.VIEWER)
    env.ledger.grant(env.household_id, viewer.user.id, savings, GrantLevel.VIEW)
    status, _ = act(env.client(viewer), document_id, "accept", statement=mine["statement"])
    assert status == 403
    stranger = env.user(HouseholdRole.MEMBER)
    status, _ = act(env.client(stranger), document_id, "accept", statement=mine["statement"])
    assert status == 404


def one_of(client: AuthedClient, document_id: uuid.UUID, account: Account) -> dict[str, Any]:
    (found,) = [
        s for s in view(client, document_id)["statements"] if s["account_id"] == str(account.id)
    ]
    statement: dict[str, Any] = found
    return statement


def test_other_findings_of_the_document_stay_open(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="7306", name="Everyday")
    document_id = _stored(
        env,
        # 100.00 - 19.50 is 80.50: the reading has 85.50.
        _month(
            "7306",
            4,
            30,
            "100.00",
            "85.50",
            [(3, "-12.25", "Lamplight Books"), (9, "-7.25", "Corner Bakery")],
        ),
        _month("9914", 4, 30, "50.00", "45.00", [(5, "-5.00", "Ferry Kiosk")]),
        checked=True,
    )
    open_before = env.open_reviews(document_id)
    unknown = [i for i in open_before if i.kind.value == "unknown_account"]
    assert len(unknown) == 1
    assert any(i.payload["reason"] == "reconciliation_failed" for i in open_before)
    owner = env.client()

    status, got = act(owner, document_id, "confirm", statement=0, closing_balance="80.50")

    assert status == 200, got
    assert (got["confirmed"], got["snapshot"]) == (2, True)
    assert got["items_closed"] == len(open_before) - 1
    assert [i.id for i in env.open_reviews(document_id)] == [unknown[0].id]
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (snapshot,) = snapshots(env, account)
    assert snapshot.balance == Decimal("80.50")


def test_confirming_one_statement_leaves_the_other_of_the_account(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="7308", name="Everyday")
    document_id = _stored(
        env,
        _month(
            "7308",
            4,
            30,
            "100.00",
            "80.50",
            [(3, "-12.25", "Lamplight Books"), (9, "-7.25", "Corner Bakery")],
        ),
        # 80.50 - 7.25 + 40.00 is 113.25: the printed closing is 9.00 short.
        _month(
            "7308",
            5,
            31,
            "80.50",
            "104.25",
            [(9, "-7.25", "Corner Bakery"), (20, "40.00", "Refund Desk")],
        ),
        checked=True,
    )
    open_before = [i.id for i in env.open_reviews(document_id)]
    assert open_before
    owner = env.client()

    status, got = act(owner, document_id, "confirm", statement=0)

    assert status == 200, got
    assert got["items_closed"] == 0
    assert [i.id for i in env.open_reviews(document_id)] == open_before
    may_rows = {r["transaction_id"] for r in view(owner, document_id)["statements"][1]["rows"]}
    pending = {str(i) for i, state in states(env, account) if state is ReviewState.PENDING}
    assert pending == may_rows
    assert [s.as_of_date for s in snapshots(env, account)] == [date(2026, 4, 30)]

    # May is accepted as read: its rows are posted, its balance is not recorded.
    status, got = act(owner, document_id, "accept", statement=1)
    assert status == 200, got
    assert (got["confirmed"], got["snapshot"], got["items_closed"]) == (2, False, 1)
    assert env.open_reviews(document_id) == []
    assert [r.verification for r in records(env, document_id)] == ["reconciled", "accepted"]
    assert [s.as_of_date for s in snapshots(env, account)] == [date(2026, 4, 30)]


def finding(
    env: Env, document_id: uuid.UUID, kind: ReviewKind, payload: dict[str, Any]
) -> uuid.UUID:
    """An open review item of the document, as the importer or the pipeline raises it."""
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        document = db.get(Document, document_id)
        assert document is not None
        return make_review_item(db, document, kind, payload).id


def test_a_finding_about_rows_of_the_other_statement_stays_open(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="7309", name="Everyday")
    document_id = _stored(
        env,
        _month(
            "7309",
            4,
            30,
            "100.00",
            "80.50",
            [(3, "-12.25", "Lamplight Books"), (9, "-7.25", "Corner Bakery")],
        ),
        _month(
            "7309",
            5,
            31,
            "80.50",
            "113.25",
            [(9, "-7.25", "Corner Bakery"), (20, "40.00", "Refund Desk")],
        ),
    )
    owner = env.client()
    april, may = view(owner, document_id)["statements"]
    assert (april["status"], may["status"]) == ("balanced", "balanced")
    # A finding that names no statement, only rows: they are May's.
    doubt = finding(
        env,
        document_id,
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {
            "reason": "low_confidence",
            "account_id": str(account.id),
            "transactions": [{"transaction_id": r["transaction_id"]} for r in may["rows"]],
        },
    )
    open_before = [i.id for i in env.open_reviews(document_id)]
    assert doubt in open_before

    status, got = act(owner, document_id, "confirm", statement=0)

    assert status == 200, got
    assert got["items_closed"] == 0
    assert [i.id for i in env.open_reviews(document_id)] == open_before

    # Confirming May answers it.
    status, got = act(owner, document_id, "confirm", statement=1)
    assert status == 200, got
    assert got["items_closed"] >= 1
    assert doubt not in [i.id for i in env.open_reviews(document_id)]


def test_lines_nobody_could_read_close_with_the_last_question(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="7310", name="Everyday")
    document_id = _stored(
        env,
        # 100.00 - 19.50 is 80.50: the reading has 85.50.
        _month(
            "7310",
            4,
            30,
            "100.00",
            "85.50",
            [(3, "-12.25", "Lamplight Books"), (9, "-7.25", "Corner Bakery")],
        ),
        checked=True,
    )
    # The pipeline's notice is about the whole file: it names no account.
    skipped = finding(
        env, document_id, ReviewKind.LOW_CONFIDENCE_FIELD, {"reason": "lines_skipped", "count": 2}
    )
    twin = finding(
        env,
        document_id,
        ReviewKind.POSSIBLE_DUPLICATE,
        {"reason": "near_matches", "account_id": str(account.id), "rows": [], "count": 0},
    )
    owner = env.client()

    status, got = act(owner, document_id, "confirm", statement=0, closing_balance="80.50")

    assert status == 200, got
    assert got["items_closed"] >= 1
    # Another question is still open: the notice waits with it.
    assert sorted(i.id for i in env.open_reviews(document_id)) == sorted([skipped, twin])
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW

    dismissed = owner.post(f"{API}/review/{twin}/dismiss")
    assert dismissed.status_code == 200, dismissed.text
    assert [i.id for i in env.open_reviews(document_id)] == [skipped]
    status, got = act(owner, document_id, "confirm", statement=0)

    assert status == 200, got
    assert got["items_closed"] == 1
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    assert len(snapshots(env, account)) == 1


def test_a_row_known_by_an_alias_counts(env: Env) -> None:
    """A statement row confirmed to be a row the ledger already had under another hash."""
    account = env.ledger.account(env.household_id, number_last4="7311", name="Everyday")
    document_id = _stored(
        env,
        _month(
            "7311",
            4,
            30,
            "100.00",
            "80.50",
            [(3, "-12.25", "Lamplight Books"), (9, "-7.25", "Corner Bakery")],
        ),
    )
    kept = next(r for r in env.transactions(account) if r.amount == Decimal("-12.25"))
    printed_hash = kept.import_hash
    assert printed_hash is not None
    # The kept row came from elsewhere, a day late, with a hash of its own;
    # the statement's hash for it is an alias.
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        db.execute(
            update(Transaction)
            .where(Transaction.id == kept.id)
            .values(
                import_hash=uuid.uuid4().hex + uuid.uuid4().hex,
                source_document_id=None,
                posted_date=date(2026, 4, 4),
            )
        )
        db.add(
            ImportHashAlias(
                household_id=env.household_id,
                account_id=account.id,
                transaction_id=kept.id,
                import_hash=printed_hash,
            )
        )

    got = one(env.client(), document_id)

    assert got["status"] == "balanced"
    assert Decimal(got["total"]) == Decimal("-19.50")
    assert len(got["rows"]) == 2
    shown = next(r for r in got["rows"] if r["transaction_id"] == str(kept.id))
    assert (shown["origin"], shown["posted_date"]) == ("matched", "2026-04-04")


def test_restore_adds_no_second_row_when_the_ledger_holds_it(env: Env) -> None:
    stmt, account, document_id, forged = tampered(env, 81)
    owner = env.client()
    assert owner.delete(f"{API}/transactions/{forged.id}").status_code == 204
    (removed,) = one(owner, document_id)["removed"]
    # The row is in the ledger again (another run wrote it) while its tombstone is still there.
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        db.add(
            Transaction(
                household_id=env.household_id,
                account_id=account.id,
                posted_date=forged.posted_date,
                amount=forged.amount,
                currency=forged.currency,
                raw_payee=forged.raw_payee,
                status=forged.status,
                source_document_id=document_id,
                import_hash=forged.import_hash,
                review_state=ReviewState.PENDING,
            )
        )
    before = states(env, account)
    assert len(before) == len(stmt.transactions) + 1

    status, got = act(owner, document_id, "restore", tombstone_id=removed["tombstone_id"])

    assert status == 200, got
    assert got["statement"]["removed"] == []
    assert states(env, account) == before
    assert env.query(select(ImportTombstone).where(ImportTombstone.account_id == account.id)) == []
    assert len(got["statement"]["rows"]) == len(before)


def test_a_row_in_another_currency_does_not_shift_the_rows_after_it(env: Env) -> None:
    """The importer leaves a foreign-currency row out before it numbers equal rows."""
    account = env.ledger.account(env.household_id, number_last4="7312", name="Everyday")
    month = _month(
        "7312",
        4,
        30,
        "100.00",
        "91.00",
        [
            (5, "-4.50", "Corner Bakery"),
            (5, "-4.50", "Corner Bakery"),
            (5, "-4.50", "Corner Bakery"),
        ],
    )
    boxes = [[40.0, 100.0, 520.0, 110.0], [40.0, 120.0, 520.0, 130.0], [40.0, 140.0, 520.0, 150.0]]
    for row, box in zip(month["transactions"], boxes, strict=True):
        row.update(page=1, bbox=box)
    month["transactions"][0]["currency"] = "EUR"
    document_id = _stored(env, month)
    assert [r.amount for r in env.transactions(account)] == [Decimal("-4.50")] * 2

    got = one(env.client(), document_id)

    assert got["status"] == "balanced"
    # Each ledger row is shown where the statement prints it, not one line up.
    assert sorted(r["bbox"] for r in got["rows"]) == boxes[1:]
    assert {r["page"] for r in got["rows"]} == {1}


def test_only_the_first_rows_are_listed_and_all_are_summed(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, account, document_id, _ = tampered(env, 82)
    ledger_rows = env.transactions(account)
    assert len(ledger_rows) > 2
    owner = env.client()
    assert one(owner, document_id)["rows_truncated"] is False
    monkeypatch.setattr(reconciliation, "ROWS_MAX", 2)

    got = one(owner, document_id)

    assert got["rows_truncated"] is True
    assert [r["transaction_id"] for r in got["rows"]] == [str(r.id) for r in ledger_rows[:2]]
    assert Decimal(got["total"]) == sum((r.amount for r in ledger_rows), Decimal(0))
    assert got["status"] == "off"


def test_a_replayed_action_answers_as_the_first_did(env: Env) -> None:
    _, _, document_id, _ = tampered(env, 83)
    owner = env.client()
    url = f"{API}/documents/{document_id}/reconciliation/accept"
    key = {"Idempotency-Key": uuid.uuid4().hex}

    first = owner.post(url, json={"statement": 0}, headers=key)
    again = owner.post(url, json={"statement": 0}, headers=key)

    assert (first.status_code, again.status_code) == (200, 200), again.text
    assert again.json() == first.json()
    assert first.json()["statement"]["verification"] == "accepted"


def test_a_copy_that_imported_nothing_cannot_be_confirmed_or_accepted(env: Env) -> None:
    account = env.ledger.account(env.household_id, number_last4="7313", name="Everyday")
    month = _month(
        "7313",
        6,
        30,
        "100.00",
        "75.50",
        [(3, "-4.50", "Corner Bakery"), (9, "-20.00", "Lamplight Books")],
    )
    first = _stored(env, month, checked=True)
    (snapshot,) = snapshots(env, account)
    assert (snapshot.balance, snapshot.source_document_id) == (Decimal("75.50"), first)
    # The same statement as another file: held as an overlap, nothing imported from it.
    copy = _stored(env, month, checked=True)
    assert records(env, copy) == []
    assert {r.source_document_id for r in env.transactions(account)} == {first}
    open_before = [i.id for i in env.open_reviews(copy)]
    assert open_before
    owner = env.client()
    assert one(owner, copy)["status"] == "balanced"

    answers = [
        act(owner, copy, "confirm", statement=0),
        act(owner, copy, "confirm", statement=0, closing_balance="70.00"),
        act(owner, copy, "accept", statement=0),
    ]

    assert [(status, body["type"].rsplit(":", 1)[-1]) for status, body in answers] == [
        (409, "statement-unavailable")
    ] * 3
    (snapshot,) = snapshots(env, account)
    assert (snapshot.balance, snapshot.source_document_id) == (Decimal("75.50"), first)
    assert [i.id for i in env.open_reviews(copy)] == open_before
    stored = stored_result(env.document(copy).extraction)
    assert stored is not None
    balance = stored.statements[0].ledger_balance
    assert balance is not None
    assert balance.amount == Decimal("75.50")
    # The first file's statement is still the one that is reconciled.
    assert one(owner, first)["verification"] == "reconciled"
    status, got = act(owner, first, "confirm", statement=0)
    assert (status, got["snapshot"]) == (200, True)


# ---------------------------------------------------------------------------
# A balance as a person types it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1,204.18", "1204.18"),
        ("$1,204.18", "1204.18"),
        ("(12.00)", "-12.00"),
        ("-12", "-12"),
        (" 3 ", "3"),
        ("1,204,180", "1204180"),
        ("1204.18 €", "1204.18"),
        ("-$5.50", "-5.50"),
        ("£ 0.07", "0.07"),
        ("($1,000.00)", "-1000.00"),
    ],
)
def test_read_printed(text: str, value: str) -> None:
    assert read_printed(text) == Decimal(value)


@pytest.mark.parametrize(
    "text",
    [
        "1,20",
        "12.3.4",
        "abc",
        "1e5",
        "",
        "   ",
        "12,34.00",
        "$$5",
        "$5$",
        "(5",
        "-(5)",
        "--5",
        "1 000",
        "5.",
        "NaN",
        "\uff11\uff12",  # full-width digits are not what a statement prints
        "999999999999999.00",  # one digit more than the ledger holds
        "0.1234567",
    ],
)
def test_read_printed_refuses(text: str) -> None:
    with pytest.raises(ProblemError) as caught:
        read_printed(text)
    assert (caught.value.status_code, caught.value.slug) == (422, "invalid-amount")
