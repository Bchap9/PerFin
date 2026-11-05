# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recording a parsed loan statement on its loan (INGESTION.md "Loan statements").

Called by the ingest pipeline for ``extraction.parse.loan`` (written by
``app.ingestion.parsers.loan_statement``) in the worker's transaction and
RLS context. Per statement:

1. **Loan**: the document's account when it has a loan, else the loan
   account with the statement's last four digits (the lender's name breaks
   ties), among the accounts the uploader may edit (``importable_account_ids``,
   SECURITY.md F3). None: an ``unparsed`` item (``loan_unmatched``) and
   nothing is written; describe the loan (``POST /loans``) and reprocess the
   document.
2. **Hold** (SECURITY.md C12): a statement whose figures do not reconcile
   (parts vs total, previous balance - principal vs new balance), with no
   check that could run, or with any field below the confidence threshold
   writes **nothing** and raises a ``low_confidence_field`` item
   (``loan_reconciliation_failed`` / ``loan_low_confidence``) naming the
   fields. So does a statement in another currency (``loan_currency_mismatch``)
   or paid outside the loan's life (``loan_payment_out_of_range``).
3. **Payment**: a ``loan_payment`` with the split and ``source_document_id``.
   Reprocessing the same document replaces its payment; a payment already
   recorded for the loan on the same day with the same principal and interest
   (typed in, or another copy of the statement) is not added twice.
4. **Balances**: the previous principal balance on the day before the
   payment and the new one on the payment day become ``statement`` balance
   snapshots of the loan account (with the document as source); the
   account's ``computed`` snapshots are then rebuilt
   (``app.domain.loans.service.sync_balance``).
5. **Rate**: a printed rate other than the rate in force for the loan
   raises a ``low_confidence_field`` item (``loan_rate_differs``) so a
   person can add the rate change; the payment is still recorded.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.audit import audit
from app.core.events import emit
from app.domain.accounts import repo as accounts_repo
from app.domain.accounts.models import Account, BalanceSource
from app.domain.documents import repo as documents_repo
from app.domain.documents.models import Document, DocumentKind, ReviewKind
from app.domain.loans.models import Loan, LoanPayment
from app.domain.loans.service import LOAN_ACCOUNT_TYPES, payment_state, rate_on, sync_balance
from app.ingestion.match.accounts import importable_account_ids, match_account
from app.ingestion.sandbox.protocol import LoanStatement

ZERO = Decimal(0)


@dataclass
class LoanImport:
    """What happened: whether a payment was recorded and review items were raised."""

    recorded: bool = False
    review: bool = False
    summary: dict[str, Any] = field(default_factory=dict)


def _review(db: Session, document: Document, kind: ReviewKind, payload: dict[str, Any]) -> None:
    documents_repo.add_review_item(
        db, document.household_id, kind, payload, document_id=document.id
    )


def _find_loan(db: Session, document: Document, stmt: LoanStatement) -> Loan | None:
    allowed = importable_account_ids(db, document.household_id, document.uploaded_by)
    loan_accounts = set(
        db.execute(
            select(Loan.account_id)
            .join(
                Account,
                (Account.household_id == Loan.household_id) & (Account.id == Loan.account_id),
            )
            .where(Loan.household_id == document.household_id, Account.type.in_(LOAN_ACCOUNT_TYPES))
        ).scalars()
    )
    if allowed is not None:
        loan_accounts &= set(allowed)
    account_id = document.account_id if document.account_id in loan_accounts else None
    if account_id is None and stmt.last4:
        match = match_account(
            db,
            document.household_id,
            last4=stmt.last4,
            institution=stmt.lender,
            allowed=loan_accounts,
        )
        account_id = match.account_id
    if account_id is None:
        return None
    return db.execute(
        select(Loan).where(
            Loan.household_id == document.household_id, Loan.account_id == account_id
        )
    ).scalar_one_or_none()


def import_loan_statement(db: Session, document: Document, stmt: LoanStatement) -> LoanImport:
    """Record ``stmt`` on its loan (module doc)."""
    document.kind = DocumentKind.STATEMENT
    days = [d for d in (stmt.paid_on, stmt.statement_date) if d is not None]
    if days:
        document.period_start, document.period_end = min(days), max(days)
    outcome = LoanImport(summary={"loan_statement": True})
    loan = _find_loan(db, document, stmt)
    if loan is None:
        _review(
            db,
            document,
            ReviewKind.UNPARSED,
            {"reason": "loan_unmatched", "last4_read": stmt.last4 is not None},
        )
        outcome.review = True
        outcome.summary["loan"] = None
        return outcome
    if document.account_id is None:
        document.account_id = loan.account_id
    outcome.summary["loan"] = str(loan.id)
    reconciliation = stmt.reconciliation
    hold: dict[str, Any] | None = None
    if stmt.currency is not None and stmt.currency != loan.currency:
        hold = {"reason": "loan_currency_mismatch", "currency": stmt.currency}
    elif reconciliation is None or reconciliation.status != "ok":
        hold = {
            "reason": "loan_reconciliation_failed",
            "status": reconciliation.status if reconciliation else "unavailable",
            "failed": list(reconciliation.failed) if reconciliation else [],
            "fields": list(stmt.low_confidence),
        }
    elif (
        stmt.low_confidence
        or stmt.paid_on is None
        or stmt.principal is None
        or stmt.interest is None
    ):
        hold = {"reason": "loan_low_confidence", "fields": list(stmt.low_confidence)}
    elif stmt.paid_on < loan.origination_date or stmt.paid_on > clock.utcnow().date():
        hold = {"reason": "loan_payment_out_of_range"}
    if hold is not None or stmt.paid_on is None or stmt.principal is None or stmt.interest is None:
        hold = hold or {"reason": "loan_low_confidence", "fields": list(stmt.low_confidence)}
        _review(db, document, ReviewKind.LOW_CONFIDENCE_FIELD, hold)
        outcome.review = True
        outcome.summary["held"] = hold["reason"]
        return outcome
    _record(db, document, loan, stmt, (stmt.paid_on, stmt.principal, stmt.interest), outcome)
    return outcome


def _record(
    db: Session,
    document: Document,
    loan: Loan,
    stmt: LoanStatement,
    split: tuple[date, Decimal, Decimal],
    outcome: LoanImport,
) -> None:
    paid_on, principal, interest = split
    db.execute(
        delete(LoanPayment).where(
            LoanPayment.household_id == loan.household_id,
            LoanPayment.loan_id == loan.id,
            LoanPayment.source_document_id == document.id,
        )
    )
    duplicate = db.execute(
        select(LoanPayment.id).where(
            LoanPayment.household_id == loan.household_id,
            LoanPayment.loan_id == loan.id,
            LoanPayment.paid_on == paid_on,
            LoanPayment.principal == principal,
            LoanPayment.interest == interest,
        )
    ).first()
    account = accounts_repo.get_account(db, loan.household_id, loan.account_id, lock=True)
    if account is None:  # pragma: no cover - the foreign key cascades
        return
    sign = 1 if account.is_asset else -1
    for day, value in (
        (paid_on - timedelta(days=1), stmt.previous_balance),
        (paid_on, stmt.balance),
    ):
        if value is not None:
            accounts_repo.upsert_snapshot(
                db,
                account,
                day,
                sign * value,
                BalanceSource.STATEMENT,
                source_document_id=document.id,
            )
    if duplicate is not None:
        outcome.summary["duplicate"] = True
    else:
        payment = LoanPayment(
            household_id=loan.household_id,
            loan_id=loan.id,
            paid_on=paid_on,
            principal=principal,
            interest=interest,
            escrow=stmt.escrow or ZERO,
            fees=stmt.fees or ZERO,
            currency=loan.currency,
            source_document_id=document.id,
        )
        db.add(payment)
        db.flush()
        outcome.recorded = True
        audit(
            db,
            action="loan.payment_import",
            target_type="loan_payment",
            target_id=payment.id,
            after=payment_state(payment),
            household_id=loan.household_id,
            user_id=document.uploaded_by,
        )
    sync_balance(db, loan)
    emit(
        db,
        loan.household_id,
        "loan.updated",
        {"loan_id": str(loan.id), "account_id": str(loan.account_id)},
    )
    outcome.summary["recorded"] = outcome.recorded
    if stmt.rate is not None and stmt.rate != rate_on(db, loan, paid_on):
        _review(
            db,
            document,
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {"reason": "loan_rate_differs", "fields": ["rate"]},
        )
        outcome.review = True
        outcome.summary["rate_differs"] = True
