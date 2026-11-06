# SPDX-License-Identifier: AGPL-3.0-or-later
"""Securities a statement prints by their name alone (no symbol, CUSIP or ISIN).

``app.domain.investments.statement_import`` step 4: such a security is
matched by its exact normalised name, else created without a symbol; a later
statement that prints a symbol or CUSIP for the same name fills it in; two
different names are never merged. The API and the price feed handle a
security without a symbol. Everything is synthetic.
"""

from __future__ import annotations

import calendar
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.accounts.models import Account
from app.domain.investments.models import Holding, Security, security_name_key
from app.domain.investments.statement_import import (
    InvestmentImport,
    import_investment_statements,
)
from app.ingestion.sandbox.protocol import (
    InvestmentPosition,
    InvestmentStatement,
    SecurityRef,
)
from app.integrations.prices.store import household_symbols
from tests.fixtures.factories import make_household
from tests.fixtures.imports import make_extracted_document
from tests.fixtures.ledger import household_context
from tests.fixtures.wealth import make_brokerage, make_security

pytestmark = pytest.mark.integration

PLAN = "Example Plan Portfolio 2040 (Example Funds)"
PARTNERSHIP = "EXAMPLE ROYALTY PARTNERS LP IX-Z"


def _position(ref: SecurityRef, quantity: str, price: str) -> InvestmentPosition:
    units, unit = Decimal(quantity), Decimal(price)
    return InvestmentPosition(
        security=ref, quantity=units, unit_price=unit, market_value=units * unit
    )


def _statement(month: int, *positions: InvestmentPosition) -> InvestmentStatement:
    end = date(2024, month, calendar.monthrange(2024, month)[1])
    return InvestmentStatement(
        source_format="ofx",
        period_start=date(2024, month, 1),
        period_end=end,
        as_of=end,
        positions=list(positions),
    )


def _import(
    db: Session, account: Account, statement: InvestmentStatement
) -> tuple[InvestmentImport, list[dict[str, Any]]]:
    document = make_extracted_document(db, account.household_id, account=account)
    reviews: list[dict[str, Any]] = []
    with household_context(db, account.household_id):
        result = import_investment_statements(
            db,
            document,
            [statement],
            allowed=None,
            review=lambda _kind, detail: reviews.append(detail),
            commit_cash=lambda _statement, _account: None,
        )
        db.flush()
    return result, reviews


def _catalogue(db: Session, account: Account) -> list[tuple[str | None, str | None, str]]:
    with household_context(db, account.household_id):
        rows = db.execute(select(Security).order_by(Security.created_at, Security.id)).scalars()
        return [(s.symbol, s.cusip, s.name) for s in rows]


def _holdings(db: Session, account: Account) -> dict[str, Decimal]:
    with household_context(db, account.household_id):
        securities = {s.id: s for s in db.execute(select(Security)).scalars()}
        rows = db.execute(select(Holding).where(Holding.account_id == account.id)).scalars()
        return {securities[h.security_id].name: h.quantity for h in rows if h.quantity}


def test_a_name_alone_names_one_security_and_later_identifiers_fill_it_in(db: Session) -> None:
    hh = make_household(db)
    account = make_brokerage(db, hh.id)
    first, reviews = _import(
        db,
        account,
        _statement(
            1,
            _position(SecurityRef(name=PLAN), "10", "25"),
            _position(SecurityRef(name=PARTNERSHIP), "4", "125"),
        ),
    )
    assert (first.statements, first.securities_created, reviews) == (1, 2, [])
    # Created without a symbol (nor any identifier), under the printed name.
    assert _catalogue(db, account) == [(None, None, PLAN), (None, None, PARTNERSHIP)]
    assert _holdings(db, account) == {PLAN: Decimal(10), PARTNERSHIP: Decimal(4)}

    # The same names, case and spacing aside: the same securities.
    second, _ = _import(
        db,
        account,
        _statement(
            2,
            _position(SecurityRef(name="EXAMPLE PLAN PORTFOLIO  2040 (example funds)"), "12", "25"),
            _position(SecurityRef(name=PARTNERSHIP), "4", "130"),
        ),
    )
    assert (second.statements, second.securities_created) == (1, 0)
    assert _holdings(db, account) == {PLAN: Decimal(12), PARTNERSHIP: Decimal(4)}

    # A later statement prints the plan's symbol and CUSIP: they are filled in.
    third, _ = _import(
        db,
        account,
        _statement(
            3,
            _position(SecurityRef(symbol="ZZPLN", cusip="999999999", name=PLAN), "12", "26"),
            _position(SecurityRef(name=PARTNERSHIP), "4", "130"),
        ),
    )
    assert (third.statements, third.securities_created) == (1, 0)
    assert _catalogue(db, account) == [("ZZPLN", "999999999", PLAN), (None, None, PARTNERSHIP)]
    # Only a real symbol is ever sent to a price feed.
    with household_context(db, hh.id):
        assert household_symbols(db, hh.id) == {"ZZPLN"}


def test_different_names_are_never_merged(db: Session) -> None:
    hh = make_household(db)
    account = make_brokerage(db, hh.id)
    result, _ = _import(
        db,
        account,
        _statement(
            1,
            _position(SecurityRef(name=PLAN), "10", "25"),
            _position(SecurityRef(name=PLAN.replace("2040", "2041")), "5", "20"),
            _position(SecurityRef(name=f"{PLAN} II"), "2", "10"),
        ),
    )
    assert result.securities_created == 3
    assert len(_holdings(db, account)) == 3


def test_a_name_matches_an_identified_security_only_when_one_carries_it(db: Session) -> None:
    hh = make_household(db)
    account = make_brokerage(db, hh.id)
    with household_context(db, hh.id):
        listed = make_security(db, hh.id, symbol="ZZLST", name="Example Listed Fund")
        make_security(db, hh.id, symbol="ZZSH1", name="Example Shared Name Fund")
        make_security(db, hh.id, symbol="ZZSH2", name="Example Shared Name Fund")
    result, _ = _import(
        db,
        account,
        _statement(
            1,
            # One security of the household carries the name: that one.
            _position(SecurityRef(name="example listed fund"), "3", "10"),
            # Two carry it: neither is chosen, a security without a symbol is made.
            _position(SecurityRef(name="Example Shared Name Fund"), "2", "10"),
            # A symbol printed with a name another listed security carries: by
            # name it takes only a security without identifiers, so a new one.
            _position(SecurityRef(symbol="ZZNEW", name="Example Listed Fund"), "1", "10"),
        ),
    )
    assert result.securities_created == 2
    with household_context(db, hh.id):
        rows = db.execute(select(Holding).where(Holding.account_id == account.id)).scalars()
        held = {
            (s.symbol, s.name): h.quantity
            for h in rows
            for s in [db.get(Security, h.security_id)]
            if s is not None
        }
    assert held == {
        (listed.symbol, listed.name): Decimal(3),
        (None, "Example Shared Name Fund"): Decimal(2),
        ("ZZNEW", "Example Listed Fund"): Decimal(1),
    }


def test_a_symbol_another_security_has_is_not_filled_in(db: Session) -> None:
    hh = make_household(db)
    account = make_brokerage(db, hh.id)
    _import(db, account, _statement(1, _position(SecurityRef(name=PLAN), "10", "25")))
    # Its CUSIP is printed: filled in (found by the name).
    _import(
        db,
        account,
        _statement(2, _position(SecurityRef(cusip="999999997", name=PLAN), "10", "25")),
    )
    assert _catalogue(db, account) == [(None, "999999997", PLAN)]
    with household_context(db, hh.id):
        make_security(db, hh.id, symbol="ZZTKN", name="Example Other Fund")
    # Found by its CUSIP, with a symbol another security already has: the
    # symbol stays unset (the catalogue keeps one security per symbol).
    result, _ = _import(
        db,
        account,
        _statement(
            3, _position(SecurityRef(symbol="ZZTKN", cusip="999999997", name=PLAN), "10", "25")
        ),
    )
    assert result.statements == 1
    assert _catalogue(db, account)[0] == (None, "999999997", PLAN)


def test_the_name_key_is_exact_apart_from_case_spacing_and_width() -> None:
    assert security_name_key("  Example\u00a0Fund  LP ") == security_name_key("EXAMPLE FUND LP")
    assert security_name_key("\uff25xample Fund") == security_name_key("Example Fund")
    assert security_name_key("Example Fund LP") != security_name_key("Example Fund, LP")
    assert security_name_key("Example Fund") != security_name_key("Example Funds")
