# SPDX-License-Identifier: AGPL-3.0-or-later
"""Loans, their payments, rate changes, schedules and simulations (API.md ``/loans``).

Shared building blocks (used by net worth, statement import and AI views):

* :data:`LOAN_ACCOUNT_TYPES`: the account types a ``loan`` row may describe.
* :func:`visible_loans`: loans of accounts the principal may see.
* :func:`rate_on`: the annual rate (fraction) in force on a day.
* :func:`monthly_rate`: the nominal monthly rate, exact ``Decimal``.
* :func:`terms_of`: a loan as amortisation :class:`~app.domain.loans.amortization.Terms`.
* :func:`sync_balance`: keeps the loan account's balance in step with the
  recorded payments (below).

Who may do what (routers check role and token scope first; every function
here checks again):

* **Read** (any role, ``loans:read``): loans of visible accounts only; a
  loan on a hidden account, another household's or a missing one is ``404
  loan-not-found``. ``POST /loans/{id}/simulate`` stores nothing and is a
  read.
* **Write** (member or above, ``loans:write``): the loan's account must be
  editable (``404`` hidden, ``403`` view-only, as for accounts).

Rules:

* A loan is created on an existing account (``account_id``) or opens its
  own (``account``: a name and a type; the account is created like ``POST
  /accounts`` does, in the principal's currency, opened on the origination
  date, audited as ``account.create``; a token also needs
  ``accounts:write``). Both rows are written in the request's transaction:
  terms that are refused leave no account behind.
* The account must be of type loan, mortgage or line of credit (``422
  invalid-loan-account``), have no loan yet (``409 loan-exists``), and
  amounts are in its currency (``422 currency-mismatch``).
* ``payment_amount`` defaults to the level payment of the terms; terms whose
  payment does not cover a period's interest are ``422
  loan-payment-too-low`` (the schedule is built on every create and change).
* A payment is dated on or after origination and not in the future (``422
  invalid-payment-date``); its parts are ``>= 0`` and not all zero (``422
  invalid-payment``); a linked transaction must be visible to the caller
  (``404 transaction-not-found``).
* A payment given as the **amount paid** alone (above zero, else ``422
  invalid-payment``) is split by :func:`split_amount` the way a lender
  applies it: first the interest of the period (the interest on the
  balance owed the day before at the period's rate, as in the schedule
  check), then the loan's ``escrow_amount``, the rest to principal. A
  period's interest and escrow are taken once: what the payments already
  recorded for the same due date (the nearest one) paid of them is
  deducted, so a second payment in a period goes to principal. The split
  is an estimate from what is recorded at that moment (record payments
  oldest first); it is stored like any other and not recomputed.

**Balance sync**: the loan account's balance is the principal owed (as a
negative balance on a liability account). The service owns the account's
``computed`` balance snapshots: one on the origination date (the original
principal), and one on each day with payments (the balance at the end of
the day before, from the account's best anchors, less the principal paid
that day). They are rebuilt after every change to the loan or its payments;
``statement`` snapshots from loan statements (previous and new principal
balance) and ``manual`` ones beat them on the same day. A ledger
transaction posted to the loan account moves the balance like on any other
account; a payment's transfer posted on the payment day is replaced by the
day's snapshot.

Every write is audited (``loan.create|update|delete``,
``loan.payment_add|payment_delete``, ``loan.rate_change_add|rate_change_delete``)
and emits ``loan.created|updated|deleted`` with ids only (account-scoped).
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, localcontext
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy import Select, delete, select
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import Money, calc_context, minor_units, sum_amounts
from app.domain.accounts import repo as accounts_repo
from app.domain.accounts import service as accounts_service
from app.domain.accounts.balances import Anchor, Movement, balance_on
from app.domain.accounts.models import Account, AccountType, BalanceSnapshot, BalanceSource
from app.domain.accounts.schemas import AccountCreate
from app.domain.accounts.visibility import (
    account_not_found,
    can_edit_account,
    get_visible_account,
    visible_account_filter,
)
from app.domain.loans import amortization as amort
from app.domain.loans.models import Loan, LoanPayment, LoanRateChange, RateType
from app.domain.loans.schemas import (
    LoanCreate,
    LoanOut,
    LoanPage,
    LoanPaymentCreate,
    LoanPaymentList,
    LoanPaymentOut,
    LoanUpdate,
    ProjectionOut,
    RateChangeCreate,
    RateChangeList,
    RateChangeOut,
    ScenarioOut,
    ScheduleCheckOut,
    ScheduleOut,
    ScheduleRowOut,
    ScheduleTotalsOut,
    SimulateIn,
    SimulationOut,
)
from app.domain.transactions.models import Transaction
from app.domain.transactions.service import get_visible_transaction

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

LOAN_ACCOUNT_TYPES: Final = frozenset(
    {AccountType.LOAN, AccountType.MORTGAGE, AccountType.LINE_OF_CREDIT}
)
#: A payment or statement balance this many days before a due date already
#: counts for it (paid early); the projection starts after it.
PAYMENT_GRACE_DAYS: Final = 14
ZERO: Final = Decimal(0)


# ---------------------------------------------------------------------------
# Shared building blocks
# ---------------------------------------------------------------------------


def visible_loans(principal: Principal) -> Select[Loan]:
    if principal.household_id is None:
        raise ValueError("principal has no active household")
    return select(Loan).where(
        Loan.household_id == principal.household_id,
        visible_account_filter(principal, Loan.account_id),
    )


def rate_on(db: Session, loan: Loan, on: date) -> Decimal:
    """The latest ``loan_rate_change`` effective on or before ``on``, else ``loan.rate``."""
    changed = db.execute(
        select(LoanRateChange.new_rate)
        .where(
            LoanRateChange.household_id == loan.household_id,
            LoanRateChange.loan_id == loan.id,
            LoanRateChange.effective_date <= on,
        )
        .order_by(LoanRateChange.effective_date.desc())
        .limit(1)
    ).scalar_one_or_none()
    return loan.rate if changed is None else changed


def monthly_rate(annual: Decimal) -> Decimal:
    """``annual / 12`` in a 34-digit context (not rounded: callers round amounts once)."""
    with localcontext(calc_context()):
        return annual / 12


def loan_for_account(db: Session, household_id: uuid.UUID, account_id: uuid.UUID) -> Loan | None:
    return db.execute(
        select(Loan).where(Loan.household_id == household_id, Loan.account_id == account_id)
    ).scalar_one_or_none()


def rate_changes(db: Session, loan: Loan) -> list[LoanRateChange]:
    return list(
        db.execute(
            select(LoanRateChange)
            .where(
                LoanRateChange.household_id == loan.household_id,
                LoanRateChange.loan_id == loan.id,
            )
            .order_by(LoanRateChange.effective_date)
        ).scalars()
    )


def terms_of(db: Session, loan: Loan) -> amort.Terms:
    return amort.Terms(
        principal=loan.principal_original,
        annual_rate=loan.rate,
        term_months=loan.term_months,
        origination_date=loan.origination_date,
        payment_day=loan.payment_day,
        currency=loan.currency,
        payment=loan.payment_amount,
        variable=loan.rate_type is RateType.VARIABLE,
        escrow=loan.escrow_amount or ZERO,
        rate_changes=tuple(
            amort.RateChange(c.effective_date, c.new_rate) for c in rate_changes(db, loan)
        ),
    )


def payments_of(db: Session, loan: Loan) -> list[LoanPayment]:
    return list(
        db.execute(
            select(LoanPayment)
            .where(LoanPayment.household_id == loan.household_id, LoanPayment.loan_id == loan.id)
            .order_by(LoanPayment.paid_on, LoanPayment.id)
        ).scalars()
    )


# ---------------------------------------------------------------------------
# Balance of the loan account
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountLedger:
    """The loan account's snapshots and transactions, for balances on any day."""

    account: Account
    anchors: list[Anchor]
    movements: list[Movement]

    @property
    def sign(self) -> int:
        return 1 if self.account.is_asset else -1

    def owed(self, on: date) -> Decimal:
        """Principal owed at the end of ``on`` (never negative)."""
        balance = balance_on(self.anchors, self.movements, on, manual=self.account.manual_balance)
        return max(ZERO, balance * self.sign)

    def last_anchor_before(self, on: date, *, recorded_only: bool) -> date | None:
        dates = [
            a.as_of_date
            for a in self.anchors
            if a.as_of_date <= on and (not recorded_only or a.source is not BalanceSource.COMPUTED)
        ]
        return max(dates) if dates else None


def _account(db: Session, loan: Loan, *, lock: bool = False) -> Account:
    account = accounts_repo.get_account(db, loan.household_id, loan.account_id, lock=lock)
    if account is None:  # pragma: no cover - the foreign key cascades
        raise account_not_found()
    return account


def account_ledger(db: Session, account: Account) -> AccountLedger:
    anchors = [
        Anchor(d, b, s)
        for d, b, s in db.execute(
            select(BalanceSnapshot.as_of_date, BalanceSnapshot.balance, BalanceSnapshot.source)
            .where(
                BalanceSnapshot.household_id == account.household_id,
                BalanceSnapshot.account_id == account.id,
            )
            .order_by(BalanceSnapshot.as_of_date)
        )
    ]
    movements = [
        Movement(d, a)
        for d, a in db.execute(
            select(Transaction.posted_date, Transaction.amount).where(
                Transaction.household_id == account.household_id,
                Transaction.account_id == account.id,
            )
        )
    ]
    return AccountLedger(account, anchors, movements)


def sync_balance(db: Session, loan: Loan) -> None:
    """Rebuild the loan account's ``computed`` snapshots (module doc, "Balance sync")."""
    account = _account(db, loan, lock=True)
    db.execute(
        delete(BalanceSnapshot).where(
            BalanceSnapshot.household_id == account.household_id,
            BalanceSnapshot.account_id == account.id,
            BalanceSnapshot.source == BalanceSource.COMPUTED,
        )
    )
    db.flush()
    ledger = account_ledger(db, account)
    sign = ledger.sign
    computed: dict[date, Decimal] = {loan.origination_date: sign * loan.principal_original}
    ledger.anchors.append(
        Anchor(loan.origination_date, computed[loan.origination_date], BalanceSource.COMPUTED)
    )
    per_day: dict[date, Decimal] = defaultdict(lambda: ZERO)
    for payment in payments_of(db, loan):
        per_day[payment.paid_on] += payment.principal
    for day in sorted(per_day):
        before = ledger.owed(day - timedelta(days=1))
        after = max(ZERO, before - per_day[day])
        computed[day] = sign * after
        ledger.anchors.append(Anchor(day, computed[day], BalanceSource.COMPUTED))
    for day, balance in sorted(computed.items()):
        accounts_repo.upsert_snapshot(db, account, day, balance, BalanceSource.COMPUTED)


# ---------------------------------------------------------------------------
# Problems and helpers
# ---------------------------------------------------------------------------


def loan_not_found() -> ProblemError:
    return ProblemError(404, "loan-not-found", "Loan not found")


def payment_not_found() -> ProblemError:
    return ProblemError(404, "loan-payment-not-found", "Loan payment not found")


def rate_change_not_found() -> ProblemError:
    return ProblemError(404, "loan-rate-change-not-found", "Rate change not found")


def _currency_mismatch(currency: str) -> ProblemError:
    return ProblemError(
        422,
        "currency-mismatch",
        "Currency mismatch",
        f"Amounts of this loan are in {currency}.",
        extra={"currency": currency},
    )


def _schedule_problem(error: amort.ScheduleError) -> ProblemError:
    return ProblemError(422, error.code, "The loan terms do not amortise", str(error))


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def today() -> date:
    return clock.utcnow().date()


def _rate_text(rate: Decimal) -> str:
    return f"{rate.normalize():f}" if rate != 0 else "0"


def _money(value: Decimal, currency: str) -> Money:
    return Money.of(value, currency)


def _state(loan: Loan) -> dict[str, Any]:
    # The lender's name is not identity data, but audit rows keep to terms.
    return {
        "account_id": str(loan.account_id),
        "principal_original": str(loan.principal_original),
        "currency": loan.currency,
        "rate": str(loan.rate),
        "rate_type": loan.rate_type.value,
        "term_months": loan.term_months,
        "origination_date": loan.origination_date.isoformat(),
        "payment_amount": str(loan.payment_amount),
        "payment_day": loan.payment_day,
        "escrow_amount": str(loan.escrow_amount) if loan.escrow_amount is not None else None,
    }


def _record(
    db: Session,
    principal: Principal,
    loan: Loan,
    action: str,
    *,
    event: str = "updated",
    target_type: str = "loan",
    target_id: uuid.UUID | None = None,
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=f"loan.{action}",
        target_type=target_type,
        target_id=target_id or loan.id,
        before=before,
        after=after,
        household_id=loan.household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(
        db,
        loan.household_id,
        f"loan.{event}",
        {"loan_id": str(loan.id), "account_id": str(loan.account_id)},
    )


def _get(db: Session, principal: Principal, loan_id: uuid.UUID, *, lock: bool = False) -> Loan:
    household_id = _household(principal, HouseholdRole.VIEWER)
    stmt = visible_loans(principal).where(Loan.household_id == household_id, Loan.id == loan_id)
    if lock:
        stmt = stmt.with_for_update()
    loan = db.execute(stmt).scalar_one_or_none()
    if loan is None:
        raise loan_not_found()
    return loan


def get_loan_row(db: Session, principal: Principal, loan_id: uuid.UUID) -> Loan:
    """A loan the caller may read, or ``404 loan-not-found``."""
    return _get(db, principal, loan_id)


def require_editable(db: Session, principal: Principal, loan_id: uuid.UUID) -> Loan:
    """A loan the caller may change: hidden ``404``, view-only ``403`` (locked)."""
    _household(principal, HouseholdRole.MEMBER)
    loan = _get(db, principal, loan_id)
    if not can_edit_account(db, principal, loan.account_id):
        raise forbidden("You can view this loan but not change it.")
    return _get(db, principal, loan_id, lock=True)


def _check_terms(terms: amort.Terms) -> None:
    try:
        amort.schedule(terms)
    except amort.ScheduleError as exc:
        raise _schedule_problem(exc) from exc


# ---------------------------------------------------------------------------
# Projection and output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Position:
    """Where a loan stands on ``as_of``: what a projection starts from."""

    as_of: date
    start: amort.Start
    source: Literal["recorded", "scheduled"]


def position(db: Session, loan: Loan, terms: amort.Terms, on: date) -> Position:
    """The balance owed at the end of ``on`` and the next payment to make.

    With payments or statement/manual balances recorded on or before ``on``:
    the loan account's balance, and the first payment due more than
    :data:`PAYMENT_GRACE_DAYS` after the latest of them. With none: the
    original schedule, assuming every payment due by ``on`` was made.
    """
    ledger = account_ledger(db, _account(db, loan))
    paid = [p.paid_on for p in payments_of(db, loan) if p.paid_on <= on]
    recorded = ledger.last_anchor_before(on, recorded_only=True)
    latest = max([*paid, *([recorded] if recorded is not None else [])], default=None)
    if latest is not None:
        number = amort.first_due_after(terms, latest + timedelta(days=PAYMENT_GRACE_DAYS))
        return Position(on, amort.Start(number, ledger.owed(on)), "recorded")
    number = amort.first_due_after(terms, on)
    balance = terms.principal
    if number > 1:
        original = amort.schedule(terms)
        done = [r for r in original.rows if r.number < number]
        balance = done[-1].balance if done else terms.principal
    return Position(on, amort.Start(number, balance), "scheduled")


def _projection(db: Session, loan: Loan, terms: amort.Terms, on: date) -> ProjectionOut:
    where = position(db, loan, terms, on)
    currency = loan.currency
    try:
        plan = amort.project(terms, where.start)
        payment = amort.payment_at(terms, where.start.number)
    except amort.ScheduleError:
        plan = None
        payment = loan.payment_amount
    rate = amort.rate_in_force(terms, amort.due_date(terms, where.start.number - 1))
    return ProjectionOut(
        as_of=on,
        balance=_money(where.start.balance, currency),
        balance_source=where.source,
        rate=_rate_text(rate),
        scheduled_payment=_money(payment, currency),
        next_due_date=plan.next_due_date if plan else None,
        payoff_date=plan.payoff_date if plan else None,
        remaining_payments=plan.remaining_payments if plan else 0,
        remaining_interest=_money(plan.remaining_interest if plan else ZERO, currency),
    )


def loan_out(db: Session, loan: Loan) -> LoanOut:
    terms = terms_of(db, loan)
    currency = loan.currency
    return LoanOut(
        id=loan.id,
        account_id=loan.account_id,
        principal_original=_money(loan.principal_original, currency),
        rate=_rate_text(loan.rate),
        rate_type=loan.rate_type,
        term_months=loan.term_months,
        origination_date=loan.origination_date,
        payment_amount=_money(loan.payment_amount, currency),
        payment_day=loan.payment_day,
        escrow_amount=(
            _money(loan.escrow_amount, currency) if loan.escrow_amount is not None else None
        ),
        lender=loan.lender,
        maturity_date=amort.due_date(terms, loan.term_months),
        projection=_projection(db, loan, terms, today()),
        created_at=loan.created_at,
        updated_at=loan.updated_at,
    )


def _row_out(row: amort.Row, currency: str) -> ScheduleRowOut:
    return ScheduleRowOut(
        number=row.number,
        due_date=row.due_date,
        rate=_rate_text(row.rate),
        payment=_money(row.payment, currency),
        principal=_money(row.principal, currency),
        interest=_money(row.interest, currency),
        extra=_money(row.extra, currency),
        escrow=_money(row.escrow, currency),
        total=_money(row.total, currency),
        balance=_money(row.balance, currency),
    )


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------


def list_loans(
    db: Session, principal: Principal, *, cursor: uuid.UUID | None, limit: int
) -> LoanPage:
    _household(principal, HouseholdRole.VIEWER)
    stmt = visible_loans(principal).order_by(Loan.id).limit(limit + 1)
    if cursor is not None:
        stmt = stmt.where(Loan.id > cursor)
    rows = list(db.execute(stmt).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return LoanPage(
        items=[loan_out(db, loan) for loan in rows],
        next_cursor=rows[-1].id if more and rows else None,
    )


def get_loan(db: Session, principal: Principal, loan_id: uuid.UUID) -> LoanOut:
    return loan_out(db, _get(db, principal, loan_id))


def create_loan(
    db: Session, principal: Principal, data: LoanCreate, *, request: HTTPConnection | None
) -> LoanOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    account_id = data.account_id or _open_account(db, principal, data, request=request)
    get_visible_account(db, principal, account_id)
    if not can_edit_account(db, principal, account_id):
        raise forbidden("You can view this account but not change it.")
    account = get_visible_account(db, principal, account_id, need="edit", lock=True)
    if account.type not in LOAN_ACCOUNT_TYPES:
        raise _not_a_loan_account()
    if loan_for_account(db, household_id, account.id) is not None:
        raise ProblemError(409, "loan-exists", "Loan exists", "This account already has a loan.")
    for money in (data.principal_original, data.payment_amount, data.escrow_amount):
        if money is not None and money.currency != account.currency:
            raise _currency_mismatch(account.currency)
    if data.principal_original.amount <= 0:
        raise ProblemError(
            422, "invalid-loan", "Invalid loan", "principal_original must be above zero."
        )
    _check_parts(data.payment_amount, data.escrow_amount)
    loan = Loan(
        household_id=household_id,
        account_id=account.id,
        principal_original=data.principal_original.amount,
        currency=account.currency,
        rate=data.rate,
        rate_type=data.rate_type,
        term_months=data.term_months,
        origination_date=data.origination_date,
        payment_amount=ZERO,
        payment_day=data.payment_day or data.origination_date.day,
        escrow_amount=data.escrow_amount.amount if data.escrow_amount else None,
        lender=data.lender,
    )
    loan.payment_amount = (
        data.payment_amount.amount
        if data.payment_amount is not None and data.payment_amount.amount > 0
        else _level(loan)
    )
    _check_terms(_terms_no_changes(loan))
    db.add(loan)
    db.flush()
    sync_balance(db, loan)
    _record(db, principal, loan, "create", event="created", after=_state(loan), request=request)
    return loan_out(db, loan)


def _not_a_loan_account() -> ProblemError:
    return ProblemError(
        422,
        "invalid-loan-account",
        "Not a loan account",
        "A loan describes an account of type loan, mortgage or line_of_credit.",
    )


def _open_account(
    db: Session, principal: Principal, data: LoanCreate, *, request: HTTPConnection | None
) -> uuid.UUID:
    """Open the account of a new loan (module doc, "Rules"); the caller adds the terms."""
    if data.account is None:  # pragma: no cover - the schema requires one of the two
        raise account_not_found()
    if not principal.has_scope("accounts:write"):
        raise ProblemError(
            403,
            "insufficient-scope",
            "Insufficient scope",
            "This token lacks the 'accounts:write' scope.",
        )
    if data.account.type not in LOAN_ACCOUNT_TYPES:
        raise _not_a_loan_account()
    opened = accounts_service.create_account(
        db,
        principal,
        AccountCreate(
            type=data.account.type,
            name=data.account.name,
            currency=data.principal_original.currency,
            opened_on=data.origination_date,
        ),
        request=request,
    )
    return opened.id


def _level(loan: Loan) -> Decimal:
    try:
        return amort.level_payment(
            loan.principal_original, loan.rate, loan.term_months, loan.currency
        )
    except amort.ScheduleError as exc:  # pragma: no cover - term validated by the schema
        raise _schedule_problem(exc) from exc


def _terms_no_changes(loan: Loan) -> amort.Terms:
    return amort.Terms(
        principal=loan.principal_original,
        annual_rate=loan.rate,
        term_months=loan.term_months,
        origination_date=loan.origination_date,
        payment_day=loan.payment_day,
        currency=loan.currency,
        payment=loan.payment_amount,
        variable=loan.rate_type is RateType.VARIABLE,
        escrow=loan.escrow_amount or ZERO,
    )


def _check_parts(payment: Any, escrow: Any) -> None:
    if payment is not None and payment.amount < 0:
        raise ProblemError(
            422, "invalid-loan", "Invalid loan", "payment_amount must not be negative."
        )
    if escrow is not None and escrow.amount < 0:
        raise ProblemError(
            422, "invalid-loan", "Invalid loan", "escrow_amount must not be negative."
        )


def update_loan(
    db: Session,
    principal: Principal,
    loan_id: uuid.UUID,
    data: LoanUpdate,
    *,
    request: HTTPConnection | None,
) -> LoanOut:
    loan = require_editable(db, principal, loan_id)
    before = _state(loan)
    fields = data.model_fields_set
    for money in (data.principal_original, data.payment_amount, data.escrow_amount):
        if money is not None and money.currency != loan.currency:
            raise _currency_mismatch(loan.currency)
    _check_parts(data.payment_amount, data.escrow_amount)
    if data.principal_original is not None:
        if data.principal_original.amount <= 0:
            raise ProblemError(
                422, "invalid-loan", "Invalid loan", "principal_original must be above zero."
            )
        loan.principal_original = data.principal_original.amount
    if data.rate is not None:
        loan.rate = data.rate
    if data.rate_type is not None:
        loan.rate_type = data.rate_type
    if data.term_months is not None:
        loan.term_months = data.term_months
    if data.origination_date is not None:
        loan.origination_date = data.origination_date
    if data.payment_day is not None:
        loan.payment_day = data.payment_day
    if data.payment_amount is not None:
        loan.payment_amount = (
            data.payment_amount.amount if data.payment_amount.amount > 0 else _level(loan)
        )
    if "escrow_amount" in fields:
        loan.escrow_amount = data.escrow_amount.amount if data.escrow_amount else None
    if "lender" in fields:
        loan.lender = data.lender
    first = db.execute(
        select(LoanPayment.paid_on)
        .where(LoanPayment.household_id == loan.household_id, LoanPayment.loan_id == loan.id)
        .order_by(LoanPayment.paid_on)
        .limit(1)
    ).scalar_one_or_none()
    if first is not None and first < loan.origination_date:
        raise ProblemError(
            422,
            "invalid-payment-date",
            "Payment before origination",
            "The loan has payments before this origination date.",
        )
    _check_terms(terms_of(db, loan))
    db.flush()
    sync_balance(db, loan)
    _record(db, principal, loan, "update", before=before, after=_state(loan), request=request)
    return loan_out(db, loan)


def delete_loan(
    db: Session, principal: Principal, loan_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    """Delete the loan, its payments and rate changes; the account and its other balances stay."""
    loan = require_editable(db, principal, loan_id)
    before = _state(loan)
    account = _account(db, loan, lock=True)
    db.execute(
        delete(BalanceSnapshot).where(
            BalanceSnapshot.household_id == account.household_id,
            BalanceSnapshot.account_id == account.id,
            BalanceSnapshot.source == BalanceSource.COMPUTED,
        )
    )
    for model in (LoanPayment, LoanRateChange):
        db.execute(
            delete(model).where(model.household_id == loan.household_id, model.loan_id == loan.id)
        )
    _record(db, principal, loan, "delete", event="deleted", before=before, request=request)
    db.delete(loan)
    db.flush()


# ---------------------------------------------------------------------------
# Schedules and simulation
# ---------------------------------------------------------------------------


def _totals(plan: amort.Schedule) -> ScheduleTotalsOut:
    currency = plan.currency
    return ScheduleTotalsOut(
        payments=len(plan.rows),
        principal=_money(plan.total_principal, currency),
        interest=_money(plan.total_interest, currency),
        extra=_money(plan.total_extra, currency),
        escrow=_money(plan.total_escrow, currency),
        paid=_money(plan.total_paid, currency),
        payoff_date=plan.payoff_date,
    )


def get_schedule(
    db: Session,
    principal: Principal,
    loan_id: uuid.UUID,
    *,
    basis: Literal["original", "projected"],
) -> ScheduleOut:
    loan = _get(db, principal, loan_id)
    terms = terms_of(db, loan)
    try:
        if basis == "original":
            plan = amort.schedule(terms)
        else:
            plan = amort.project(terms, position(db, loan, terms, today()).start).schedule
    except amort.ScheduleError as exc:
        raise _schedule_problem(exc) from exc
    return ScheduleOut(
        loan_id=loan.id,
        basis=basis,
        start_balance=_money(plan.start_balance, loan.currency),
        rows=[_row_out(r, loan.currency) for r in plan.rows],
        totals=_totals(plan),
    )


def _scenario(projection: amort.Projection) -> ScenarioOut:
    plan = projection.schedule
    return ScenarioOut(
        payoff_date=projection.payoff_date,
        payments=projection.remaining_payments,
        total_interest=_money(plan.total_interest, plan.currency),
        total_extra=_money(plan.total_extra, plan.currency),
    )


def simulate(
    db: Session, principal: Principal, loan_id: uuid.UUID, data: SimulateIn
) -> SimulationOut:
    """The projection from today with and without extra principal payments (stores nothing)."""
    loan = _get(db, principal, loan_id)
    terms = terms_of(db, loan)
    on = today()
    where = position(db, loan, terms, on)
    currency = loan.currency
    for money in [e.amount for e in data.one_off] + (
        [data.recurring.amount] if data.recurring else []
    ):
        if money.currency != currency:
            raise _currency_mismatch(currency)
        if money.amount <= 0:
            raise ProblemError(
                422, "invalid-extra-payment", "Invalid extra payment", "Extras must be above zero."
            )
    extras = amort.Extras(
        one_off=tuple(amort.OneOffExtra(e.on, e.amount.amount) for e in data.one_off),
        recurring=(
            (
                amort.RecurringExtra(
                    data.recurring.amount.amount,
                    data.recurring.start or on,
                    data.recurring.end,
                ),
            )
            if data.recurring
            else ()
        ),
    )
    try:
        result = amort.simulate(terms, where.start, extras)
    except amort.ScheduleError as exc:
        raise _schedule_problem(exc) from exc
    return SimulationOut(
        loan_id=loan.id,
        as_of=on,
        balance=_money(where.start.balance, currency),
        baseline=_scenario(result.baseline),
        with_extras=_scenario(result.scenario),
        interest_saved=_money(result.interest_saved, currency),
        payments_saved=result.payments_saved,
        schedule=(
            [_row_out(r, currency) for r in result.scenario.schedule.rows]
            if data.include_schedule
            else None
        ),
    )


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


def _check(
    terms: amort.Terms, ledger: AccountLedger, payment: LoanPayment, already_paid: Decimal
) -> tuple[ScheduleCheckOut, Decimal | None]:
    """The payment's interest against the interest still due for its period.

    ``already_paid``: the interest of the period's earlier payments (a
    period's interest is due once: an extra payment after it carries none).
    """
    currency = payment.currency
    before = ledger.owed(payment.paid_on - timedelta(days=1))
    after = ledger.owed(payment.paid_on)
    if before <= 0:
        return ScheduleCheckOut(status="unchecked", expected_interest=None, difference=None), after
    number = amort.nearest_due_number(terms, payment.paid_on)
    rate = amort.rate_in_force(terms, amort.due_date(terms, number - 1))
    expected = max(ZERO, amort.period_interest(before, rate, currency) - already_paid)
    difference = payment.interest - expected
    tolerance = Decimal(1).scaleb(-minor_units(currency))
    return (
        ScheduleCheckOut(
            status="ok" if abs(difference) <= tolerance else "differs",
            expected_interest=_money(expected, currency),
            difference=_money(difference, currency),
        ),
        after,
    )


def _payment_out(
    payment: LoanPayment, check: ScheduleCheckOut, after: Decimal | None
) -> LoanPaymentOut:
    currency = payment.currency
    return LoanPaymentOut(
        id=payment.id,
        loan_id=payment.loan_id,
        paid_on=payment.paid_on,
        principal=_money(payment.principal, currency),
        interest=_money(payment.interest, currency),
        escrow=_money(payment.escrow, currency),
        fees=_money(payment.fees, currency),
        total=_money(
            sum_amounts((payment.principal, payment.interest, payment.escrow, payment.fees)),
            currency,
        ),
        balance_after=_money(after, currency) if after is not None else None,
        transaction_id=payment.transaction_id,
        source_document_id=payment.source_document_id,
        schedule_check=check,
        created_at=payment.created_at,
    )


def payments_out(db: Session, loan: Loan, payments: Sequence[LoanPayment]) -> list[LoanPaymentOut]:
    terms = terms_of(db, loan)
    ledger = account_ledger(db, _account(db, loan))
    # The interest each payment's period had already been paid before it
    # (payments in date order; a period is the nearest due date).
    already_paid: dict[uuid.UUID, Decimal] = {}
    per_period: dict[int, Decimal] = defaultdict(lambda: ZERO)
    for earlier in payments_of(db, loan):
        number = amort.nearest_due_number(terms, earlier.paid_on)
        already_paid[earlier.id] = per_period[number]
        per_period[number] += earlier.interest
    result = []
    for payment in payments:
        check, after = _check(terms, ledger, payment, already_paid.get(payment.id, ZERO))
        result.append(_payment_out(payment, check, after))
    return result


def list_payments(
    db: Session,
    principal: Principal,
    loan_id: uuid.UUID,
    *,
    start: date | None,
    end: date | None,
    limit: int,
) -> LoanPaymentList:
    loan = _get(db, principal, loan_id)
    stmt = select(LoanPayment).where(
        LoanPayment.household_id == loan.household_id, LoanPayment.loan_id == loan.id
    )
    if start is not None:
        stmt = stmt.where(LoanPayment.paid_on >= start)
    if end is not None:
        stmt = stmt.where(LoanPayment.paid_on <= end)
    rows = list(
        db.execute(stmt.order_by(LoanPayment.paid_on, LoanPayment.id).limit(limit)).scalars()
    )
    return LoanPaymentList(items=payments_out(db, loan, rows))


def check_payment_date(loan: Loan, paid_on: date) -> None:
    if paid_on < loan.origination_date or paid_on > today():
        raise ProblemError(
            422,
            "invalid-payment-date",
            "Invalid payment date",
            "A payment is dated between the loan's origination and today.",
        )


def split_amount(
    db: Session, loan: Loan, paid_on: date, amount: Decimal
) -> tuple[Decimal, Decimal, Decimal]:
    """``(principal, interest, escrow)`` of ``amount`` paid on ``paid_on`` (module doc).

    Call before the payment is stored: the balance and the period's earlier
    payments are read from what is recorded.
    """
    terms = terms_of(db, loan)
    owed = account_ledger(db, _account(db, loan)).owed(paid_on - timedelta(days=1))
    number = amort.nearest_due_number(terms, paid_on)
    same_period = [
        p for p in payments_of(db, loan) if amort.nearest_due_number(terms, p.paid_on) == number
    ]
    rate = amort.rate_in_force(terms, amort.due_date(terms, number - 1))
    interest_due = amort.period_interest(owed, rate, loan.currency) if owed > 0 else ZERO
    interest_due = max(ZERO, interest_due - sum_amounts(p.interest for p in same_period))
    escrow_paid = sum_amounts(p.escrow for p in same_period)
    escrow_due = max(ZERO, (loan.escrow_amount or ZERO) - escrow_paid)
    interest = min(amount, interest_due)
    escrow = min(amount - interest, escrow_due)
    return amount - interest - escrow, interest, escrow


def add_payment(
    db: Session,
    principal: Principal,
    loan_id: uuid.UUID,
    data: LoanPaymentCreate,
    *,
    request: HTTPConnection | None,
) -> LoanPaymentOut:
    loan = require_editable(db, principal, loan_id)
    parts = [data.principal, data.interest, data.escrow, data.fees]
    for money in (data.amount, *parts):
        if money is not None and money.currency != loan.currency:
            raise _currency_mismatch(loan.currency)
    amounts = [m.amount if m is not None else ZERO for m in parts]
    if data.amount is not None and data.amount.amount <= 0:
        raise ProblemError(
            422, "invalid-payment", "Invalid payment", "The amount paid is above zero."
        )
    if data.amount is None and (any(a < 0 for a in amounts) or all(a == 0 for a in amounts)):
        raise ProblemError(
            422,
            "invalid-payment",
            "Invalid payment",
            "Payment parts are zero or more and not all zero.",
        )
    check_payment_date(loan, data.paid_on)
    if data.transaction_id is not None:
        get_visible_transaction(db, principal, data.transaction_id)
    if data.amount is not None:
        amounts = [*split_amount(db, loan, data.paid_on, data.amount.amount), ZERO]
    payment = LoanPayment(
        household_id=loan.household_id,
        loan_id=loan.id,
        paid_on=data.paid_on,
        principal=amounts[0],
        interest=amounts[1],
        escrow=amounts[2],
        fees=amounts[3],
        currency=loan.currency,
        transaction_id=data.transaction_id,
    )
    db.add(payment)
    db.flush()
    sync_balance(db, loan)
    _record(
        db,
        principal,
        loan,
        "payment_add",
        target_type="loan_payment",
        target_id=payment.id,
        after=payment_state(payment),
        request=request,
    )
    return payments_out(db, loan, [payment])[0]


def payment_state(payment: LoanPayment) -> dict[str, Any]:
    return {
        "loan_id": str(payment.loan_id),
        "paid_on": payment.paid_on.isoformat(),
        "principal": str(payment.principal),
        "interest": str(payment.interest),
        "escrow": str(payment.escrow),
        "fees": str(payment.fees),
        "source_document_id": (
            str(payment.source_document_id) if payment.source_document_id else None
        ),
    }


def delete_payment(
    db: Session,
    principal: Principal,
    loan_id: uuid.UUID,
    payment_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> None:
    loan = require_editable(db, principal, loan_id)
    payment = db.execute(
        select(LoanPayment).where(
            LoanPayment.household_id == loan.household_id,
            LoanPayment.loan_id == loan.id,
            LoanPayment.id == payment_id,
        )
    ).scalar_one_or_none()
    if payment is None:
        raise payment_not_found()
    before = payment_state(payment)
    db.delete(payment)
    db.flush()
    sync_balance(db, loan)
    _record(
        db,
        principal,
        loan,
        "payment_delete",
        target_type="loan_payment",
        target_id=payment_id,
        before=before,
        request=request,
    )


# ---------------------------------------------------------------------------
# Rate changes
# ---------------------------------------------------------------------------


def _rate_change_out(change: LoanRateChange) -> RateChangeOut:
    return RateChangeOut(
        id=change.id,
        loan_id=change.loan_id,
        effective_date=change.effective_date,
        new_rate=_rate_text(change.new_rate),
        created_at=change.created_at,
    )


def list_rate_changes(db: Session, principal: Principal, loan_id: uuid.UUID) -> RateChangeList:
    loan = _get(db, principal, loan_id)
    return RateChangeList(items=[_rate_change_out(c) for c in rate_changes(db, loan)])


def add_rate_change(
    db: Session,
    principal: Principal,
    loan_id: uuid.UUID,
    data: RateChangeCreate,
    *,
    request: HTTPConnection | None,
) -> RateChangeOut:
    loan = require_editable(db, principal, loan_id)
    if data.effective_date <= loan.origination_date:
        raise ProblemError(
            422,
            "invalid-rate-change",
            "Invalid rate change",
            "A rate change takes effect after the origination date (change the loan's rate).",
        )
    exists = db.execute(
        select(LoanRateChange.id).where(
            LoanRateChange.household_id == loan.household_id,
            LoanRateChange.loan_id == loan.id,
            LoanRateChange.effective_date == data.effective_date,
        )
    ).scalar_one_or_none()
    if exists is not None:
        raise ProblemError(
            409,
            "rate-change-exists",
            "Rate change exists",
            "The loan already has a rate change on this date.",
        )
    change = LoanRateChange(
        household_id=loan.household_id,
        loan_id=loan.id,
        effective_date=data.effective_date,
        new_rate=data.new_rate,
    )
    db.add(change)
    db.flush()
    _check_terms(terms_of(db, loan))
    _record(
        db,
        principal,
        loan,
        "rate_change_add",
        target_type="loan_rate_change",
        target_id=change.id,
        after={"effective_date": data.effective_date.isoformat(), "new_rate": str(data.new_rate)},
        request=request,
    )
    return _rate_change_out(change)


def delete_rate_change(
    db: Session,
    principal: Principal,
    loan_id: uuid.UUID,
    change_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> None:
    loan = require_editable(db, principal, loan_id)
    change = db.execute(
        select(LoanRateChange).where(
            LoanRateChange.household_id == loan.household_id,
            LoanRateChange.loan_id == loan.id,
            LoanRateChange.id == change_id,
        )
    ).scalar_one_or_none()
    if change is None:
        raise rate_change_not_found()
    before = {
        "effective_date": change.effective_date.isoformat(),
        "new_rate": str(change.new_rate),
    }
    db.delete(change)
    db.flush()
    _record(
        db,
        principal,
        loan,
        "rate_change_delete",
        target_type="loan_rate_change",
        target_id=change_id,
        before=before,
        request=request,
    )
