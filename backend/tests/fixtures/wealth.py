# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic investments, loans, manual assets and net worth rows for tests (Phase 5).

Flush-only ``make_*`` builders in the style of ``tests/fixtures/ledger.py``:
each runs in the household's RLS context and puts the caller's back. Use
``ledger.run(make_x, ...)`` (the committing ``ledger`` fixture) when a
``TestClient`` app must see the rows. Symbols, names and amounts are
invented (``SYN1A2B``, ``Security-3f9a1c``, ``Lender-...``); nothing is a
real instrument or institution.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.domain.accounts.models import Account, AccountType
from app.domain.assets.models import (
    AssetValuation,
    ManualAsset,
    ValuationMethod,
    ValuationSource,
)
from app.domain.investments.models import (
    AssetClass,
    Holding,
    InvestmentTxn,
    InvestmentTxnType,
    Lot,
    Price,
    Security,
)
from app.domain.loans.models import Loan, LoanPayment, LoanRateChange, RateType
from app.domain.networth.models import NetWorthSnapshot
from app.domain.transactions.models import Transaction
from tests.fixtures.factories import unique_name
from tests.fixtures.ledger import DEFAULT_DATE, household_context, make_account


def _dec(value: Decimal | str | int) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _add[T](db: Session, household_id: uuid.UUID, row: T) -> T:
    with household_context(db, household_id):
        db.add(row)
        db.flush()
    return row


def synthetic_symbol() -> str:
    return f"SYN{secrets.token_hex(3).upper()}"


# ---------------------------------------------------------------------------
# Investments
# ---------------------------------------------------------------------------


def make_brokerage(db: Session, household_id: uuid.UUID, **fields: Any) -> Account:
    return make_account(db, household_id, type=AccountType.BROKERAGE, **fields)


def make_security(
    db: Session,
    household_id: uuid.UUID,
    *,
    symbol: str | None = None,
    name: str | None = None,
    asset_class: AssetClass | str = AssetClass.EQUITY,
    currency: str = "USD",
    **fields: Any,
) -> Security:
    return _add(
        db,
        household_id,
        Security(
            household_id=household_id,
            symbol=symbol or synthetic_symbol(),
            name=name or unique_name("Security"),
            asset_class=AssetClass(asset_class),
            currency=currency,
            **fields,
        ),
    )


def make_price(
    db: Session,
    security: Security,
    *,
    close: Decimal | str = "100.00",
    price_date: date = DEFAULT_DATE,
    source: str = "manual",
) -> Price:
    return _add(
        db,
        security.household_id,
        Price(
            household_id=security.household_id,
            security_id=security.id,
            price_date=price_date,
            close=_dec(close),
            currency=security.currency,
            source=source,
        ),
    )


def make_investment_txn(
    db: Session,
    account: Account,
    *,
    security: Security | None = None,
    type: InvestmentTxnType | str = InvestmentTxnType.BUY,
    trade_date: date = DEFAULT_DATE,
    quantity: Decimal | str | None = "10",
    price: Decimal | str | None = "100.00",
    amount: Decimal | str | None = None,
    fees: Decimal | str = "0",
    lot_links: list[dict[str, Any]] | None = None,
    **fields: Any,
) -> InvestmentTxn:
    """A brokerage row; ``amount`` defaults to ``-(quantity * price) - fees`` for a buy."""
    kind = InvestmentTxnType(type)
    qty = _dec(quantity) if quantity is not None else None
    unit = _dec(price) if price is not None else None
    fee = _dec(fees)
    if amount is None:
        gross = qty * unit if qty is not None and unit is not None else Decimal(0)
        amount = -gross - fee if kind is InvestmentTxnType.BUY else gross - fee
    return _add(
        db,
        account.household_id,
        InvestmentTxn(
            household_id=account.household_id,
            account_id=account.id,
            security_id=security.id if security is not None else None,
            type=kind,
            trade_date=trade_date,
            quantity=qty,
            price=unit,
            amount=_dec(amount).quantize(Decimal("0.000001")),
            fees=fee,
            currency=account.currency,
            lot_links=lot_links or [],
            **fields,
        ),
    )


def make_lot(
    db: Session,
    account: Account,
    security: Security,
    *,
    quantity: Decimal | str = "10",
    remaining: Decimal | str | None = None,
    cost_per_unit: Decimal | str = "100.00",
    open_date: date = DEFAULT_DATE,
    closed_date: date | None = None,
    opening_txn: InvestmentTxn | None = None,
) -> Lot:
    qty = _dec(quantity)
    left = qty if remaining is None else _dec(remaining)
    if left == 0 and closed_date is None:
        closed_date = open_date
    return _add(
        db,
        account.household_id,
        Lot(
            household_id=account.household_id,
            account_id=account.id,
            security_id=security.id,
            open_date=open_date,
            quantity=qty,
            remaining_quantity=left,
            cost_per_unit=_dec(cost_per_unit),
            currency=account.currency,
            closed_date=closed_date,
            opening_txn_id=opening_txn.id if opening_txn is not None else None,
        ),
    )


def make_holding(
    db: Session,
    account: Account,
    security: Security,
    *,
    quantity: Decimal | str = "10",
    cost_basis: Decimal | str = "1000.00",
    as_of: date = DEFAULT_DATE,
) -> Holding:
    return _add(
        db,
        account.household_id,
        Holding(
            household_id=account.household_id,
            account_id=account.id,
            security_id=security.id,
            quantity=_dec(quantity),
            cost_basis=_dec(cost_basis),
            currency=account.currency,
            as_of=as_of,
        ),
    )


# ---------------------------------------------------------------------------
# Loans
# ---------------------------------------------------------------------------


def make_loan(
    db: Session,
    household_id: uuid.UUID,
    *,
    account: Account | None = None,
    account_type: AccountType | str = AccountType.MORTGAGE,
    principal_original: Decimal | str = "250000.00",
    rate: Decimal | str = "0.06125",
    rate_type: RateType | str = RateType.FIXED,
    term_months: int = 360,
    origination_date: date = date(2024, 3, 1),
    payment_amount: Decimal | str = "1519.03",
    payment_day: int = 1,
    escrow_amount: Decimal | str | None = None,
    lender: str | None = None,
) -> Loan:
    """A loan on ``account`` (a new account of ``account_type`` unless given)."""
    if account is None:
        account = make_account(db, household_id, type=account_type)
    return _add(
        db,
        household_id,
        Loan(
            household_id=household_id,
            account_id=account.id,
            principal_original=_dec(principal_original),
            currency=account.currency,
            rate=_dec(rate),
            rate_type=RateType(rate_type),
            term_months=term_months,
            origination_date=origination_date,
            payment_amount=_dec(payment_amount),
            payment_day=payment_day,
            escrow_amount=_dec(escrow_amount) if escrow_amount is not None else None,
            lender=lender or unique_name("Lender"),
        ),
    )


def make_loan_payment(
    db: Session,
    loan: Loan,
    *,
    paid_on: date = DEFAULT_DATE,
    principal: Decimal | str = "243.00",
    interest: Decimal | str = "1276.03",
    escrow: Decimal | str = "0",
    fees: Decimal | str = "0",
    transaction: Transaction | None = None,
    source_document_id: uuid.UUID | None = None,
) -> LoanPayment:
    return _add(
        db,
        loan.household_id,
        LoanPayment(
            household_id=loan.household_id,
            loan_id=loan.id,
            paid_on=paid_on,
            principal=_dec(principal),
            interest=_dec(interest),
            escrow=_dec(escrow),
            fees=_dec(fees),
            currency=loan.currency,
            transaction_id=transaction.id if transaction is not None else None,
            source_document_id=source_document_id,
        ),
    )


def make_loan_rate_change(
    db: Session, loan: Loan, *, effective_date: date, new_rate: Decimal | str
) -> LoanRateChange:
    return _add(
        db,
        loan.household_id,
        LoanRateChange(
            household_id=loan.household_id,
            loan_id=loan.id,
            effective_date=effective_date,
            new_rate=_dec(new_rate),
        ),
    )


# ---------------------------------------------------------------------------
# Manual assets and net worth
# ---------------------------------------------------------------------------


def make_manual_asset(
    db: Session,
    household_id: uuid.UUID,
    *,
    account: Account | None = None,
    asset_kind: str = "property",
    description: str | None = None,
    valuation_method: ValuationMethod | str = ValuationMethod.MANUAL,
) -> ManualAsset:
    if account is None:
        account = make_account(
            db, household_id, type=AccountType.MANUAL_ASSET, asset_kind=asset_kind
        )
    return _add(
        db,
        household_id,
        ManualAsset(
            household_id=household_id,
            account_id=account.id,
            description=description,
            valuation_method=ValuationMethod(valuation_method),
        ),
    )


def make_asset_valuation(
    db: Session,
    account: Account,
    *,
    value: Decimal | str = "350000.00",
    valued_on: date = DEFAULT_DATE,
    source: ValuationSource | str = ValuationSource.MANUAL,
) -> AssetValuation:
    return _add(
        db,
        account.household_id,
        AssetValuation(
            household_id=account.household_id,
            account_id=account.id,
            valued_on=valued_on,
            value=_dec(value),
            currency=account.currency,
            source=ValuationSource(source),
        ),
    )


def make_net_worth_snapshot(
    db: Session,
    household_id: uuid.UUID,
    *,
    as_of_date: date = DEFAULT_DATE,
    assets: Decimal | str = "500000.00",
    liabilities: Decimal | str = "200000.00",
    currency: str = "USD",
    breakdown: dict[str, Any] | None = None,
) -> NetWorthSnapshot:
    a, li = _dec(assets), _dec(liabilities)
    return _add(
        db,
        household_id,
        NetWorthSnapshot(
            household_id=household_id,
            as_of_date=as_of_date,
            currency=currency,
            assets=a,
            liabilities=li,
            net=a - li,
            breakdown=breakdown if breakdown is not None else {"accounts": []},
        ),
    )
