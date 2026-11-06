# SPDX-License-Identifier: AGPL-3.0-or-later
"""An activity row's price is stored as one unit's (a bond trade's per-100 price too).

``investment_txn.price`` feeds the valuation's price book (a trade newer
than the stored price is the close), so a bond trade priced per 100 of face
value must never be stored at 100 times one unit's price.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, cast

from app.domain.accounts.models import Account
from app.domain.investments.statement_import import _txn
from app.ingestion.sandbox.protocol import InvestmentActivity, SecurityRef


def _stored(activity: InvestmentActivity) -> Decimal | None:
    household = uuid.uuid4()
    document = SimpleNamespace(household_id=household, id=uuid.uuid4())
    ctx = cast(Any, SimpleNamespace(household_id=household, document=document))
    account = Account(id=uuid.uuid4(), currency="USD")
    return _txn(ctx, account, activity, uuid.uuid4(), "0" * 64).price


def test_a_bond_trade_priced_per_100_is_stored_per_unit() -> None:
    bond = InvestmentActivity(
        type="buy",
        trade_date=date(2024, 3, 5),
        security=SecurityRef(symbol="ZZBND"),
        quantity=Decimal(2000),
        unit_price=Decimal("99.00"),
        price_per_100=True,
        amount=Decimal("-1980.00"),
    )
    assert bond.price_per_unit == Decimal("0.99")
    assert _stored(bond) == Decimal("0.99")
    share = bond.model_copy(update={"price_per_100": False, "unit_price": Decimal("12.50")})
    assert _stored(share) == Decimal("12.50")
