# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 5/6 tables: forced RLS, household-consistent references, constraints, services.

Covers ``security``, ``price``, ``investment_txn``, ``lot``, ``holding``,
``loan``, ``loan_payment``, ``loan_rate_change``, ``manual_asset``,
``asset_valuation``, ``net_worth_snapshot``, ``tax_year``,
``tax_checklist_item``, ``tax_category_map`` and ``tax_form_data``
(SECURITY.md F3 for the references, H8 for tax identity boxes).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel, HouseholdRole
from app.auth.principal import Principal
from app.core.errors import ProblemError
from app.db.base import Base, household_scoped_tables
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.assets.models import AssetValuation, ManualAsset, ValuationMethod
from app.domain.assets.service import latest_valuation
from app.domain.investments.models import (
    Holding,
    InvestmentTxn,
    InvestmentTxnType,
    Lot,
    Price,
)
from app.domain.investments.service import (
    dump_lot_links,
    latest_price,
    parse_lot_links,
    rebuild_holdings,
    visible_holdings,
)
from app.domain.loans.models import Loan, LoanPayment, LoanRateChange, RateType
from app.domain.loans.service import rate_on, visible_loans
from app.domain.networth.models import NetWorthSnapshot
from app.domain.networth.service import latest_snapshot, sees_household_totals
from app.domain.tax.models import (
    ChecklistStatus,
    FormDataSource,
    TaxCategoryMap,
    TaxChecklistItem,
    TaxFormData,
    TaxYear,
)
from app.domain.tax.service import check_filing_status, clean_issuer_name, record_form_data
from tests.fixtures.factories import grant_account, make_household, make_user
from tests.fixtures.imports import make_extracted_document
from tests.fixtures.ledger import (
    household_context,
    make_account,
    make_category,
    make_transaction,
)
from tests.fixtures.tax import (
    make_checklist_item,
    make_form_data,
    make_tax_category_map,
    make_tax_year,
    synthetic_w2_boxes,
)
from tests.fixtures.wealth import (
    make_asset_valuation,
    make_brokerage,
    make_holding,
    make_investment_txn,
    make_loan,
    make_loan_payment,
    make_loan_rate_change,
    make_lot,
    make_manual_asset,
    make_net_worth_snapshot,
    make_price,
    make_security,
)

pytestmark = pytest.mark.integration

PHASE56_TABLES = frozenset(
    {
        "security",
        "price",
        "investment_txn",
        "lot",
        "holding",
        "loan",
        "loan_payment",
        "loan_rate_change",
        "manual_asset",
        "asset_valuation",
        "net_worth_snapshot",
        "tax_year",
        "tax_checklist_item",
        "tax_category_map",
        "tax_form_data",
    }
)


def _refused(db: Session, write: Callable[[], object]) -> None:
    def attempt() -> None:
        with db.begin_nested():
            write()
            db.flush()
            db.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))

    with pytest.raises((IntegrityError, DBAPIError)):
        attempt()


def _insert(db: Session, household_id: uuid.UUID, row: object) -> Callable[[], object]:
    def write() -> object:
        set_context(db, household_id)
        db.add(row)
        db.flush()
        return row

    return write


def _exists(db: Session, model: type[Base], row_id: uuid.UUID) -> bool:
    table = model.__table__
    query = select(func.count()).select_from(table).where(table.c.id == row_id)
    return bool(db.execute(query).scalar_one())


def _populate(db: Session, household_id: uuid.UUID) -> None:
    account = make_brokerage(db, household_id)
    security = make_security(db, household_id)
    make_price(db, security)
    txn = make_investment_txn(db, account, security=security)
    make_lot(db, account, security, opening_txn=txn)
    make_holding(db, account, security)
    loan = make_loan(db, household_id)
    make_loan_payment(db, loan)
    make_loan_rate_change(db, loan, effective_date=date(2026, 3, 1), new_rate="0.055")
    asset = make_manual_asset(db, household_id)
    with household_context(db, household_id):
        asset_account = db.get_one(ManualAsset, asset.id).account_id
    make_asset_valuation(db, make_account_ref(db, household_id, asset_account))
    make_net_worth_snapshot(db, household_id)
    year = make_tax_year(db, household_id)
    document = make_extracted_document(db, household_id)
    make_checklist_item(db, year, status=ChecklistStatus.RECEIVED, document=document)
    make_tax_category_map(db, make_category(db, household_id))
    make_form_data(db, document, tax_year_row=year)


def make_account_ref(db: Session, household_id: uuid.UUID, account_id: uuid.UUID) -> Account:
    with household_context(db, household_id):
        return db.get_one(Account, account_id)


# ---------------------------------------------------------------------------
# RLS
# ---------------------------------------------------------------------------


def test_every_table_is_household_scoped_with_forced_rls(db: Session) -> None:
    assert set(household_scoped_tables()) >= PHASE56_TABLES
    rows = db.execute(
        text(
            "SELECT relname, relrowsecurity, relforcerowsecurity FROM pg_class "
            "WHERE relkind = 'r' AND relname = ANY(:names)"
        ),
        {"names": sorted(PHASE56_TABLES)},
    ).all()
    assert {r[0] for r in rows} == PHASE56_TABLES
    assert all(r[1] and r[2] for r in rows), rows


def test_rows_are_invisible_to_other_households(db: Session) -> None:
    a = make_household(db)
    b = make_household(db)
    _populate(db, a.id)
    tables = sorted(PHASE56_TABLES)
    selects = [f"SELECT '{t}' AS t, count(*) AS n FROM \"{t}\"" for t in tables]  # noqa: S608
    count_sql = " UNION ALL ".join(selects)
    set_context(db, a.id)
    mine: dict[str, int] = dict(db.execute(text(count_sql)).all())
    assert all(mine[t] >= 1 for t in tables), mine
    set_context(db, b.id)
    assert set(dict(db.execute(text(count_sql)).all()).values()) == {0}
    set_context(db, None)
    assert set(dict(db.execute(text(count_sql)).all()).values()) == {0}


def test_rows_cannot_be_written_into_another_household(db: Session) -> None:
    a = make_household(db)
    b = make_household(db)
    _refused(db, _insert(db, b.id, TaxYear(household_id=a.id, year=2025, jurisdiction="US")))


# ---------------------------------------------------------------------------
# Household-consistent references (F3)
# ---------------------------------------------------------------------------


def test_cross_household_parents_are_refused(db: Session) -> None:
    a = make_household(db)
    b = make_household(db)
    account_a = make_brokerage(db, a.id)
    account_b = make_brokerage(db, b.id)
    security_a = make_security(db, a.id)
    security_b = make_security(db, b.id)
    loan_b = make_loan(db, b.id)
    year_b = make_tax_year(db, b.id)
    category_b = make_category(db, b.id)
    document_b = make_extracted_document(db, b.id)
    day = date(2026, 1, 2)
    one = Decimal(1)
    cases: list[object] = [
        Price(
            household_id=a.id, security_id=security_b.id, price_date=day, close=one, currency="USD"
        ),
        InvestmentTxn(
            household_id=a.id,
            account_id=account_b.id,
            security_id=security_a.id,
            type=InvestmentTxnType.BUY,
            trade_date=day,
            quantity=one,
            price=one,
            amount=-one,
            currency="USD",
        ),
        InvestmentTxn(
            household_id=a.id,
            account_id=account_a.id,
            security_id=security_b.id,
            type=InvestmentTxnType.BUY,
            trade_date=day,
            quantity=one,
            price=one,
            amount=-one,
            currency="USD",
        ),
        Lot(
            household_id=a.id,
            account_id=account_a.id,
            security_id=security_b.id,
            open_date=day,
            quantity=one,
            remaining_quantity=one,
            cost_per_unit=one,
            currency="USD",
        ),
        Holding(
            household_id=a.id,
            account_id=account_b.id,
            security_id=security_a.id,
            quantity=one,
            cost_basis=one,
            currency="USD",
            as_of=day,
        ),
        Loan(
            household_id=a.id,
            account_id=account_b.id,
            principal_original=one,
            currency="USD",
            rate=Decimal("0.05"),
            rate_type=RateType.FIXED,
            term_months=12,
            origination_date=day,
            payment_amount=one,
            payment_day=1,
        ),
        LoanPayment(household_id=a.id, loan_id=loan_b.id, paid_on=day, currency="USD"),
        LoanRateChange(
            household_id=a.id, loan_id=loan_b.id, effective_date=day, new_rate=Decimal("0.01")
        ),
        ManualAsset(
            household_id=a.id, account_id=account_b.id, valuation_method=ValuationMethod.MANUAL
        ),
        AssetValuation(
            household_id=a.id,
            account_id=account_b.id,
            valued_on=day,
            value=one,
            currency="USD",
            source="manual",
        ),
        TaxChecklistItem(household_id=a.id, tax_year_id=year_b.id, expected_form="W-2"),
        TaxCategoryMap(
            household_id=a.id, category_id=category_b.id, jurisdiction="US", schedule="Schedule A"
        ),
        TaxFormData(
            household_id=a.id,
            document_id=document_b.id,
            tax_year=2025,
            jurisdiction="US",
            form_type="W-2",
            source=FormDataSource.PARSED,
        ),
    ]
    for row in cases:
        _refused(db, _insert(db, a.id, row))


def test_cross_household_optional_references_are_refused(db: Session) -> None:
    a = make_household(db)
    b = make_household(db)
    account_a = make_brokerage(db, a.id)
    security_a = make_security(db, a.id)
    txn_b = make_investment_txn(db, make_brokerage(db, b.id), security=make_security(db, b.id))
    document_a = make_extracted_document(db, a.id)
    document_b = make_extracted_document(db, b.id)
    loan_a = make_loan(db, a.id)
    ledger_txn_b = make_transaction(db, make_account(db, b.id))
    year_a = make_tax_year(db, a.id)
    year_b = make_tax_year(db, b.id)
    day = date(2026, 1, 2)
    one = Decimal(1)
    cases: list[object] = [
        Lot(
            household_id=a.id,
            account_id=account_a.id,
            security_id=security_a.id,
            open_date=day,
            quantity=one,
            remaining_quantity=one,
            cost_per_unit=one,
            currency="USD",
            opening_txn_id=txn_b.id,
        ),
        InvestmentTxn(
            household_id=a.id,
            account_id=account_a.id,
            type=InvestmentTxnType.FEE,
            trade_date=day,
            amount=-one,
            currency="USD",
            source_document_id=document_b.id,
        ),
        LoanPayment(
            household_id=a.id,
            loan_id=loan_a.id,
            paid_on=day,
            currency="USD",
            transaction_id=ledger_txn_b.id,
        ),
        LoanPayment(
            household_id=a.id,
            loan_id=loan_a.id,
            paid_on=day,
            currency="USD",
            source_document_id=document_b.id,
        ),
        TaxChecklistItem(
            household_id=a.id,
            tax_year_id=year_a.id,
            expected_form="1099-INT",
            status=ChecklistStatus.RECEIVED,
            document_id=document_b.id,
        ),
        TaxFormData(
            household_id=a.id,
            document_id=document_a.id,
            tax_year=2025,
            tax_year_id=year_b.id,
            jurisdiction="US",
            form_type="W-2",
            source=FormDataSource.PARSED,
        ),
    ]
    for row in cases:
        _refused(db, _insert(db, a.id, row))


def test_currency_follows_account_and_security(db: Session) -> None:
    hh = make_household(db)
    account = make_brokerage(db, hh.id, currency="USD")
    security = make_security(db, hh.id, currency="EUR")
    day = date(2026, 1, 2)
    _refused(
        db,
        _insert(
            db,
            hh.id,
            Price(
                household_id=hh.id,
                security_id=security.id,
                price_date=day,
                close=Decimal(1),
                currency="USD",
            ),
        ),
    )
    _refused(
        db,
        _insert(
            db,
            hh.id,
            Holding(
                household_id=hh.id,
                account_id=account.id,
                security_id=security.id,
                quantity=Decimal(1),
                cost_basis=Decimal(1),
                currency="EUR",
                as_of=day,
            ),
        ),
    )


# ---------------------------------------------------------------------------
# Constraints
# ---------------------------------------------------------------------------


def test_investment_constraints(db: Session) -> None:
    hh = make_household(db)
    account = make_brokerage(db, hh.id)
    security = make_security(db, hh.id)
    day = date(2026, 1, 2)

    def txn(**fields: object) -> InvestmentTxn:
        base: dict[str, object] = {
            "household_id": hh.id,
            "account_id": account.id,
            "security_id": security.id,
            "type": InvestmentTxnType.BUY,
            "trade_date": day,
            "quantity": Decimal(1),
            "price": Decimal(1),
            "amount": Decimal(-1),
            "currency": "USD",
        }
        base.update(fields)
        return InvestmentTxn(**base)

    _refused(db, _insert(db, hh.id, txn(security_id=None)))
    _refused(db, _insert(db, hh.id, txn(quantity=None)))
    _refused(db, _insert(db, hh.id, txn(quantity=Decimal(0))))
    _refused(db, _insert(db, hh.id, txn(fees=Decimal(-1))))
    _refused(db, _insert(db, hh.id, txn(lot_links={"lot": 1})))
    _refused(db, _insert(db, hh.id, txn(settle_date=date(2026, 1, 1))))
    _refused(db, _insert(db, hh.id, txn(import_hash="not-a-hash")))
    # An interest row needs neither a security nor a quantity.
    _insert(db, hh.id, txn(type=InvestmentTxnType.INTEREST, security_id=None, quantity=None))()
    lot = {
        "household_id": hh.id,
        "account_id": account.id,
        "security_id": security.id,
        "open_date": day,
        "quantity": Decimal(10),
        "cost_per_unit": Decimal(1),
        "currency": "USD",
    }
    _refused(db, _insert(db, hh.id, Lot(**lot, remaining_quantity=Decimal(11))))
    _refused(db, _insert(db, hh.id, Lot(**lot, remaining_quantity=Decimal(0))))  # not closed
    _refused(
        db,
        _insert(db, hh.id, Lot(**lot, remaining_quantity=Decimal(5), closed_date=day)),
    )
    _refused(db, _insert(db, hh.id, Lot(**{**lot, "quantity": Decimal(0)}, remaining_quantity=0)))
    # Symbols are unique per exchange, NULL exchange included.
    make_security(db, hh.id, symbol="SYNAAA")
    _refused(db, lambda: make_security(db, hh.id, symbol="SYNAAA"))
    make_security(db, hh.id, symbol="SYNAAA", exchange="XSYN")
    _refused(db, lambda: make_security(db, hh.id, isin="US123"))
    # A security with activity cannot be deleted.
    _insert(db, hh.id, txn())()
    set_context(db, hh.id)
    delete_security = text("DELETE FROM security WHERE id = :id")
    with pytest.raises(IntegrityError), db.begin_nested():
        db.execute(delete_security, {"id": security.id})


def test_loan_asset_and_networth_constraints(db: Session) -> None:
    hh = make_household(db)
    loan = make_loan(db, hh.id)
    _refused(db, lambda: make_loan(db, hh.id, rate="1.5"))
    _refused(db, lambda: make_loan(db, hh.id, payment_day=32))
    _refused(db, lambda: make_loan(db, hh.id, term_months=0))
    _refused(db, lambda: make_loan_payment(db, loan, interest="-1"))
    make_loan_rate_change(db, loan, effective_date=date(2026, 1, 1), new_rate="0.05")
    _refused(
        db,
        lambda: make_loan_rate_change(db, loan, effective_date=date(2026, 1, 1), new_rate="0.04"),
    )
    _refused(db, lambda: make_loan(db, hh.id, account=make_account_ref(db, hh.id, loan.account_id)))
    _refused(db, lambda: make_net_worth_snapshot(db, hh.id, assets="1", liabilities="-2"))
    with household_context(db, hh.id):
        bad = NetWorthSnapshot(
            household_id=hh.id,
            as_of_date=date(2026, 2, 1),
            currency="USD",
            assets=Decimal(5),
            liabilities=Decimal(1),
            net=Decimal(3),
        )
    _refused(db, _insert(db, hh.id, bad))
    asset = make_manual_asset(db, hh.id)
    account = make_account_ref(db, hh.id, asset.account_id)
    _refused(db, lambda: make_asset_valuation(db, account, value="-1"))


def test_tax_constraints(db: Session) -> None:
    hh = make_household(db)
    year = make_tax_year(db, hh.id)
    _refused(db, lambda: make_tax_year(db, hh.id))  # one per year
    _refused(db, lambda: make_tax_year(db, hh.id, year=1800))
    _refused(db, lambda: make_tax_year(db, hh.id, year=2024, jurisdiction="usa"))
    make_checklist_item(db, year, expected_form="1098", issuer="Lender-x")
    _refused(db, lambda: make_checklist_item(db, year, expected_form="1098", issuer="Lender-x"))

    def no_issuer() -> TaxChecklistItem:
        return TaxChecklistItem(household_id=hh.id, tax_year_id=year.id, expected_form="1098-E")

    _insert(db, hh.id, no_issuer())()
    _refused(db, _insert(db, hh.id, no_issuer()))  # NULL issuers are not distinct
    _refused(db, lambda: make_checklist_item(db, year, expected_form="w 2"))
    document = make_extracted_document(db, hh.id)
    _refused(db, lambda: make_checklist_item(db, year, document=document))  # not received

    def raw_form(boxes: object) -> TaxFormData:
        return TaxFormData(
            household_id=hh.id,
            document_id=document.id,
            tax_year=2025,
            jurisdiction="US",
            form_type="W-2",
            form_index=7,
            source=FormDataSource.MANUAL,
            boxes=boxes,
        )

    # The database refuses anything in boxes but box ids -> decimal strings,
    # even when the service is bypassed (an SSN as text, a name, a number).
    for bad in (
        {"a": "123-45-6789"},
        {"e": "Pat Example"},
        {"1": 1000},
        {"1": "1e5"},
        {"1": "1.1234567"},
        {"bad key": "1.00"},
        {"1": {"x": "1"}},
        ["1.00"],
        {f"k{i}": "1" for i in range(129)},
    ):
        _refused(db, _insert(db, hh.id, raw_form(bad)))
    _insert(db, hh.id, raw_form({"1": "-1000.50", "12_D": "5"}))()


# ---------------------------------------------------------------------------
# Cascades
# ---------------------------------------------------------------------------


def test_deletes_cascade_or_clear(db: Session) -> None:
    hh = make_household(db)
    year = make_tax_year(db, hh.id)
    document = make_extracted_document(db, hh.id)
    item = make_checklist_item(db, year, status=ChecklistStatus.RECEIVED, document=document)
    form = make_form_data(db, document, tax_year_row=year)
    account = make_brokerage(db, hh.id)
    security = make_security(db, hh.id)
    txn = make_investment_txn(db, account, security=security, source_document_id=document.id)
    lot = make_lot(db, account, security, opening_txn=txn)
    loan = make_loan(db, hh.id)
    payment = make_loan_payment(db, loan, source_document_id=document.id)
    form_id, item_id, loan_id, payment_id = form.id, item.id, loan.id, payment.id
    set_context(db, hh.id)
    db.execute(text("DELETE FROM document WHERE id = :id"), {"id": document.id})
    db.execute(text("DELETE FROM investment_txn WHERE id = :id"), {"id": txn.id})
    db.expire_all()
    assert not _exists(db, TaxFormData, form_id)
    assert db.get_one(TaxChecklistItem, item.id).document_id is None
    assert db.get_one(Lot, lot.id).opening_txn_id is None
    assert db.get_one(LoanPayment, payment.id).source_document_id is None
    db.execute(text("DELETE FROM tax_year WHERE id = :id"), {"id": year.id})
    db.execute(text("DELETE FROM account WHERE id = :id"), {"id": loan.account_id})
    assert not _exists(db, TaxChecklistItem, item_id)
    assert not _exists(db, Loan, loan_id)
    assert not _exists(db, LoanPayment, payment_id)


# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------


def test_holdings_rebuild_from_lots_and_respect_grants(db: Session) -> None:
    hh = make_household(db)
    account = make_brokerage(db, hh.id)
    hidden = make_brokerage(db, hh.id)
    s1 = make_security(db, hh.id)
    s2 = make_security(db, hh.id)
    make_lot(db, account, s1, quantity="10", cost_per_unit="100.123456789")
    make_lot(db, account, s1, quantity="2.5", remaining="0.0000000001", cost_per_unit="3")
    make_lot(db, account, s2, quantity="5", remaining="0", cost_per_unit="7")
    make_holding(db, account, s2, quantity="5")
    with household_context(db, hh.id):
        rows = rebuild_holdings(db, account, date(2026, 2, 1))
    assert [(r.security_id, r.quantity) for r in rows] == [(s1.id, Decimal("10.0000000001"))]
    # 10 * 100.123456789 + 0.0000000001 * 3 = 1001.2345678903 -> 1001.234568
    assert rows[0].cost_basis == Decimal("1001.234568")
    make_holding(db, hidden, s1)
    member = make_user(db, role="member", household=hh)
    grant_account(db, hh.id, member.user.id, account.id, GrantLevel.VIEW)
    principal = Principal(
        user=member.user, household_id=hh.id, role=HouseholdRole.MEMBER, auth_method="session"
    )
    set_context(db, hh.id)
    seen = {h.account_id for h in db.execute(visible_holdings(principal)).scalars()}
    assert seen == {account.id}
    assert not sees_household_totals(db, principal)
    owner = Principal(
        user=member.user, household_id=hh.id, role=HouseholdRole.OWNER, auth_method="session"
    )
    assert sees_household_totals(db, owner)
    grant_account(db, hh.id, member.user.id, hidden.id, GrantLevel.VIEW)
    set_context(db, hh.id)
    assert sees_household_totals(db, principal)
    loan = make_loan(db, hh.id)
    set_context(db, hh.id)
    assert loan.id not in set(db.execute(visible_loans(principal)).scalars())


def test_price_rate_valuation_and_snapshot_lookups(db: Session) -> None:
    hh = make_household(db)
    security = make_security(db, hh.id)
    make_price(db, security, price_date=date(2026, 1, 1), close="10")
    make_price(db, security, price_date=date(2026, 1, 5), close="12.5")
    loan = make_loan(db, hh.id, rate="0.06")
    make_loan_rate_change(db, loan, effective_date=date(2026, 2, 1), new_rate="0.07")
    asset = make_manual_asset(db, hh.id)
    account = make_account_ref(db, hh.id, asset.account_id)
    make_asset_valuation(db, account, valued_on=date(2026, 1, 1), value="100", source="provider")
    make_asset_valuation(db, account, valued_on=date(2026, 1, 1), value="90")
    make_net_worth_snapshot(db, hh.id, as_of_date=date(2026, 1, 3))
    set_context(db, hh.id)
    assert latest_price(db, hh.id, security.id, date(2025, 12, 31)) is None
    found = latest_price(db, hh.id, security.id, date(2026, 1, 4))
    assert found is not None
    assert found.close == Decimal("10")
    assert rate_on(db, loan, date(2026, 1, 31)) == Decimal("0.06")
    assert rate_on(db, loan, date(2026, 2, 1)) == Decimal("0.07")
    valuation = latest_valuation(db, hh.id, account.id, date(2026, 6, 1))
    assert valuation is not None
    assert valuation.value == Decimal("90")
    assert latest_snapshot(db, hh.id, date(2026, 1, 2)) is None
    assert latest_snapshot(db, hh.id, date(2026, 1, 9)) is not None


def test_lot_links_round_trip() -> None:
    lot_id = uuid.uuid4()
    links = parse_lot_links([{"lot_id": str(lot_id), "quantity": "1.5"}])
    assert dump_lot_links(links) == [{"lot_id": str(lot_id), "quantity": "1.5"}]
    for bad in (
        [{"lot_id": str(lot_id), "quantity": "0"}],
        [{"lot_id": str(lot_id), "quantity": "1", "extra": 1}],
        [{"lot_id": str(lot_id), "quantity": "1"}] * 2,
        {"lot_id": str(lot_id)},
    ):
        with pytest.raises(ValueError, match=r"validation error|linked twice"):
            parse_lot_links(bad)


def test_form_data_never_stores_identity(db: Session) -> None:
    hh = make_household(db)
    document = make_extracted_document(db, hh.id)
    raw = synthetic_w2_boxes(seed=3)
    form = make_form_data(db, document, boxes=raw, issuer_name="  Synthetic\x00 Employer\n Inc ")
    set_context(db, hh.id)
    stored = db.execute(
        text("SELECT boxes::text, issuer_name FROM tax_form_data WHERE id = :id"), {"id": form.id}
    ).one()
    for key in ("a", "b", "c", "d", "e", "f", "14", "15"):
        assert str(raw[key]) not in stored[0]
    assert set(form.boxes) == {"1", "2", "3", "4", "5", "6", "12_D", "16", "17"}
    assert stored[1] == "Synthetic Employer Inc"
    with pytest.raises(ProblemError) as caught, household_context(db, hh.id):
        record_form_data(
            db,
            hh.id,
            document_id=document.id,
            jurisdiction="US",
            form_type="W-2",
            tax_year=2025,
            boxes={"1": "900-12-3456"},
            source=FormDataSource.PARSED,
            form_index=1,
        )
    assert caught.value.status_code == 422
    assert "900-12-3456" not in str(caught.value.detail)
    with pytest.raises(ProblemError):
        check_filing_status("US", "jointly")
    with pytest.raises(ProblemError):
        record_form_data(
            db,
            hh.id,
            document_id=document.id,
            jurisdiction="US",
            form_type="W-9",
            tax_year=2025,
            boxes={},
            source=FormDataSource.MANUAL,
        )
    assert clean_issuer_name(" \t ") is None
    assert clean_issuer_name("x" * 300) == "x" * 200


def test_new_loan_accounts_default_to_liabilities(db: Session) -> None:
    hh = make_household(db)
    loan = make_loan(db, hh.id, account_type=AccountType.LOAN)
    assert make_account_ref(db, hh.id, loan.account_id).is_asset is False
