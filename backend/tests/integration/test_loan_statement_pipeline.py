# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loan statements end to end through the worker (INGESTION.md "Loan statements").

upload -> worker ``ingest_document`` -> real sandbox (PDF pages) ->
classifier -> loan statement parser -> reconciliation ->
``app.domain.loans.statement_import`` -> ``loan_payment`` with provenance,
statement balance snapshots and the loan account's balance kept in sync.
Statements come from ``tests/fixtures/synth_loans.py`` (an invented lender).

* A statement that reconciles records its payment split; the account's
  balance is the statement's outstanding principal; the next month adds its
  own payment only, and the same file again adds nothing.
* A statement that does not reconcile (C12) writes nothing and waits in
  review with the failed check.
* Matching respects grants (F3): a member's statement does not reach a loan
  on an account they cannot edit; no loan at all is ``loan_unmatched``.
* A printed rate that differs from the loan's raises ``loan_rate_differs``.
* A periodic statement (next payment, last month, year to date) records last
  month's payment on the day its activity row says, never another column.
* A periodic statement that reports no payment in its period is filed: no
  payment, no review item, its balance kept on the statement date (none when
  that date is before the loan began). One whose amount due does not add up
  is still held.
* A loan bill is a loan document, never a receipt: an auto-loan bill records
  its payment; a bill listing several loans, which the parser does not read,
  waits as ``unparsed``/``loan_unreadable`` with the parser's warning.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel, HouseholdRole
from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.accounts.balances import balance_as_of
from app.domain.accounts.models import Account, AccountType, BalanceSnapshot, BalanceSource
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentKind,
    DocumentStatus,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.loans.models import Loan, LoanPayment
from app.ingestion.intake.upload import receive_upload
from app.workers.handlers import ingest
from tests.fixtures import synth_loans as synth
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.imports import run_jobs
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.wealth import make_loan

pytestmark = pytest.mark.integration

MAX = 10 * 1024 * 1024


@dataclass
class Env:
    factories: Factories
    ledger: LedgerFactories
    store: LocalFSBlobStore
    keyring: Keyring
    owner: UserRecord
    done: set[uuid.UUID] = field(default_factory=set)

    @property
    def household_id(self) -> uuid.UUID:
        assert self.owner.household_id is not None
        return self.owner.household_id

    def ingest(self, data: bytes, filename: str, *, uploader: UserRecord | None = None) -> Document:
        record = uploader or self.owner
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

    def reasons(self, document: Document) -> set[str]:
        items: list[ReviewItem] = self.query(
            select(ReviewItem).where(ReviewItem.document_id == document.id)
        )
        return {str(r.payload.get("reason")) for r in items if r.state is ReviewItemState.OPEN}

    def payments(self, loan: Loan) -> list[LoanPayment]:
        return self.query(
            select(LoanPayment).where(LoanPayment.loan_id == loan.id).order_by(LoanPayment.paid_on)
        )

    def balance(self, account: Account, on: date) -> Decimal:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            found = db.get(Account, account.id)
            assert found is not None
            return balance_as_of(db, found, on)

    def loan(self, *, last4: str = synth.LAST4, **kwargs: Any) -> tuple[Account, Loan]:
        account = self.ledger.account(
            self.household_id, type=AccountType.MORTGAGE, number_last4=last4, name="Home"
        )
        terms = synth.TERMS
        fields: dict[str, Any] = {
            "principal_original": terms.principal,
            "rate": terms.annual_rate,
            "term_months": terms.term_months,
            "origination_date": terms.origination_date,
            "payment_amount": "1519.03",
            "payment_day": terms.payment_day,
            "escrow_amount": terms.escrow,
            **kwargs,
        }
        loan = self.ledger.run(make_loan, self.household_id, account=account, **fields)
        return account, loan


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
    yield Env(
        factories=factories,
        ledger=ledger,
        store=deps.store,
        keyring=deps.keyring,
        owner=factories.user(role="owner", with_totp=True),
    )
    ingest.default_deps.cache_clear()


def test_statements_record_payments_and_keep_the_balance(env: Env) -> None:
    account, loan = env.loan()
    first, second = synth.loan_series(2, extra={2: Decimal("1000.00")})
    document = env.ingest(synth.loan_statement_pdf(first), "mortgage-2024-04.pdf")
    assert document.status is DocumentStatus.PROCESSED, env.reasons(document)
    assert document.kind is DocumentKind.STATEMENT
    assert document.account_id == account.id
    [payment] = env.payments(loan)
    assert (payment.paid_on, payment.principal, payment.interest, payment.escrow) == (
        first.paid_on,
        first.principal,
        first.interest,
        first.escrow,
    )
    assert payment.source_document_id == document.id
    assert env.balance(account, first.paid_on) == -first.balance
    assert env.balance(account, first.paid_on - timedelta(days=1)) == -first.previous_balance
    snapshots: list[BalanceSnapshot] = env.query(
        select(BalanceSnapshot).where(
            BalanceSnapshot.account_id == account.id,
            BalanceSnapshot.source == BalanceSource.STATEMENT,
        )
    )
    assert {s.source_document_id for s in snapshots} == {document.id}

    later = env.ingest(synth.loan_statement_pdf(second, layout="compact"), "mortgage-2024-05.pdf")
    assert later.status is DocumentStatus.PROCESSED, env.reasons(later)
    assert [p.principal for p in env.payments(loan)] == [first.principal, second.principal]
    assert env.balance(account, date(2024, 6, 30)) == -second.balance

    # The same statement again (another copy of the file) adds nothing.
    again = env.ingest(synth.loan_statement_pdf(first) + b"\n%copy\n", "copy.pdf")
    assert again.status is DocumentStatus.PROCESSED
    assert len(env.payments(loan)) == 2


@pytest.mark.parametrize("tamper", ["parts", "balance"])
def test_statement_that_does_not_reconcile_is_held(env: Env, tamper: synth.Tamper) -> None:
    account, loan = env.loan()
    stmt = synth.loan_series(1)[0]
    document = env.ingest(synth.loan_statement_pdf(stmt, tamper=tamper), f"{tamper}.pdf")
    assert document.status is DocumentStatus.NEEDS_REVIEW
    assert env.reasons(document) == {"loan_reconciliation_failed"}
    assert env.payments(loan) == []
    written: list[BalanceSnapshot] = env.query(
        select(BalanceSnapshot).where(BalanceSnapshot.account_id == account.id)
    )
    assert written == []


def test_matching_respects_grants_and_needs_a_loan(env: Env) -> None:
    stmt = synth.loan_series(1)[0]
    orphan = env.ingest(synth.loan_statement_pdf(stmt), "no-loan.pdf")
    assert orphan.status is DocumentStatus.NEEDS_REVIEW
    assert env.reasons(orphan) == {"loan_unmatched"}

    account, loan = env.loan()
    member = env.factories.user(role=HouseholdRole.MEMBER, household=env.owner.household)
    viewed = env.ingest(synth.loan_statement_pdf(stmt), "member.pdf", uploader=member)
    assert env.reasons(viewed) == {"loan_unmatched"}
    assert env.payments(loan) == []
    env.ledger.grant(env.household_id, member.id, account, GrantLevel.EDIT)
    granted = env.ingest(
        synth.loan_statement_pdf(stmt, layout="compact"), "m2.pdf", uploader=member
    )
    assert granted.status is DocumentStatus.PROCESSED, env.reasons(granted)
    assert len(env.payments(loan)) == 1


def test_rate_that_differs_is_flagged(env: Env) -> None:
    _account, loan = env.loan(rate="0.055", rate_type="variable")
    stmt = synth.loan_series(1)[0]
    document = env.ingest(synth.loan_statement_pdf(stmt), "rate.pdf")
    assert document.status is DocumentStatus.NEEDS_REVIEW
    assert env.reasons(document) == {"loan_rate_differs"}
    assert len(env.payments(loan)) == 1


def test_periodic_statement_records_last_months_payment(env: Env) -> None:
    account, loan = env.loan(
        last4=synth.PERIODIC_LAST4,
        principal_original="200000.00",
        rate="0.05",
        origination_date=date(2021, 6, 1),
        payment_amount="1172.25",
        escrow_amount="301.50",
    )
    document = env.ingest(synth.periodic_statement_pdf(), "mortgage-2026-03.pdf")
    assert env.reasons(document) == set()
    assert document.status is DocumentStatus.PROCESSED
    assert document.account_id == account.id
    [payment] = env.payments(loan)
    assert (payment.paid_on, payment.principal, payment.interest, payment.escrow, payment.fees) == (
        date(2026, 3, 2),
        Decimal("410.37"),
        Decimal("761.88"),
        Decimal("301.50"),
        Decimal("0.00"),
    )
    assert payment.source_document_id == document.id
    assert env.balance(account, date(2026, 3, 2)) == Decimal("-182440.17")
    statement: list[BalanceSnapshot] = env.query(
        select(BalanceSnapshot).where(
            BalanceSnapshot.account_id == account.id,
            BalanceSnapshot.source == BalanceSource.STATEMENT,
        )
    )
    assert [(s.as_of_date, s.balance) for s in statement] == [
        (date(2026, 3, 2), Decimal("-182440.17"))
    ]


def _periodic_loan(env: Env) -> tuple[Account, Loan]:
    return env.loan(
        last4=synth.PERIODIC_LAST4,
        principal_original="200000.00",
        rate="0.05",
        origination_date=date(2021, 6, 1),
        payment_amount="1172.25",
        escrow_amount="301.50",
    )


def _statement_snapshots(env: Env, account: Account) -> list[tuple[date, Decimal]]:
    found: list[BalanceSnapshot] = env.query(
        select(BalanceSnapshot).where(
            BalanceSnapshot.account_id == account.id,
            BalanceSnapshot.source == BalanceSource.STATEMENT,
        )
    )
    return [(s.as_of_date, s.balance) for s in found]


def test_statement_without_a_payment_files_the_balance(env: Env) -> None:
    account, loan = _periodic_loan(env)
    document = env.ingest(synth.periodic_statement_pdf(variant="no_payment"), "no-payment.pdf")
    assert env.reasons(document) == set()
    assert document.status is DocumentStatus.PROCESSED
    assert document.kind is DocumentKind.STATEMENT
    assert document.account_id == account.id
    assert env.payments(loan) == []
    assert _statement_snapshots(env, account) == [(date(2026, 3, 20), Decimal("-182440.17"))]
    assert env.balance(account, date(2026, 3, 20)) == Decimal("-182440.17")


def test_statement_without_a_payment_that_does_not_add_up_is_held(env: Env) -> None:
    account, loan = _periodic_loan(env)
    data = synth.periodic_statement_pdf(variant="no_payment", tamper="due")
    document = env.ingest(data, "no-payment-tampered.pdf")
    assert document.status is DocumentStatus.NEEDS_REVIEW
    assert env.reasons(document) == {"loan_reconciliation_failed"}
    assert env.payments(loan) == []
    assert _statement_snapshots(env, account) == []


def test_statement_without_a_payment_dated_before_the_loan_keeps_no_balance(env: Env) -> None:
    # The statement is dated 2026-03-20; a loan that began later has no balance on that day.
    account, loan = env.loan(
        last4=synth.PERIODIC_LAST4,
        principal_original="200000.00",
        rate="0.05",
        origination_date=date(2026, 6, 1),
        payment_amount="1172.25",
        escrow_amount="301.50",
    )
    document = env.ingest(synth.periodic_statement_pdf(variant="no_payment"), "before-loan.pdf")
    assert env.reasons(document) == set()
    assert document.status is DocumentStatus.PROCESSED
    assert document.account_id == account.id
    assert env.payments(loan) == []
    assert _statement_snapshots(env, account) == []


def _open_items(env: Env, document: Document) -> list[ReviewItem]:
    items: list[ReviewItem] = env.query(
        select(ReviewItem).where(ReviewItem.document_id == document.id)
    )
    return [item for item in items if item.state is ReviewItemState.OPEN]


def test_bill_of_several_loans_says_so(env: Env) -> None:
    document = env.ingest(synth.student_loan_bill_pdf(), "student-bill.pdf")
    assert document.status is DocumentStatus.NEEDS_REVIEW
    [item] = _open_items(env, document)
    assert item.kind is ReviewKind.UNPARSED
    assert item.payload["reason"] == "loan_unreadable"
    assert item.payload["parser"] == "loan_statement_generic"
    assert "loan_bill_several_loans" in item.payload["warnings"]
    assert env.query(select(LoanPayment)) == []


def test_auto_loan_bill_is_a_loan_document_not_a_receipt(env: Env) -> None:
    account, loan = env.loan(
        last4=synth.AUTO_LAST4,
        principal_original="24000.00",
        rate="0.0549",
        term_months=60,
        origination_date=date(2024, 9, 15),
        payment_amount="455.20",
        payment_day=15,
        escrow_amount=None,
    )
    document = env.ingest(synth.auto_loan_bill_pdf(), "auto-bill.pdf")
    assert (document.parser_version or "").startswith("loan_statement_generic/")
    assert document.kind is DocumentKind.STATEMENT
    assert document.account_id == account.id
    [payment] = env.payments(loan)
    assert (payment.paid_on, payment.principal, payment.interest) == (
        date(2026, 3, 14),
        Decimal("371.85"),
        Decimal("83.35"),
    )
    assert all(item.kind is not ReviewKind.RECEIPT_MATCH for item in _open_items(env, document))
    assert (document.extraction or {}).get("parse", {}).get("receipt") is None
