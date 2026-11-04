# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every client-supplied business date of Phases 5-6 has a lower bound (SECURITY.md F8).

Security review P56-SR-R1-01: a trade dated year 1 made ``range=max``
performance walk 740 000 days. Each input schema must refuse a date before
``MIN_BUSINESS_DATE`` and accept the bound itself.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app.core.dates import MIN_BUSINESS_DATE, in_business_range, min_date_check
from app.domain.assets.schemas import AssetCreate, ValuationCreate
from app.domain.fx.schemas import FxRateSet
from app.domain.investments.schemas import InvestmentTxnCreate, InvestmentTxnUpdate, PriceSet
from app.domain.loans.schemas import (
    LoanCreate,
    LoanPaymentCreate,
    LoanUpdate,
    OneOffExtraIn,
    RateChangeCreate,
    RecurringExtraIn,
)
from app.domain.recurring.schemas import ScheduleCreate, ScheduleUpdate

TOO_OLD = MIN_BUSINESS_DATE - timedelta(days=1)
USD = {"amount": "10.00", "currency": "USD"}
ACCOUNT = str(uuid.uuid4())

#: (schema, valid payload without the date, the date field)
CASES: list[tuple[type[BaseModel], dict[str, Any], str]] = [
    (
        InvestmentTxnCreate,
        {"account_id": ACCOUNT, "type": "dividend", "amount": USD},
        "trade_date",
    ),
    (
        InvestmentTxnCreate,
        {"account_id": ACCOUNT, "type": "dividend", "amount": USD, "trade_date": "2026-01-02"},
        "settle_date",
    ),
    (InvestmentTxnUpdate, {}, "trade_date"),
    (InvestmentTxnUpdate, {}, "settle_date"),
    (PriceSet, {"close": "12.5"}, "price_date"),
    (
        LoanCreate,
        {"account_id": ACCOUNT, "principal_original": USD, "rate": "0.05", "term_months": 12},
        "origination_date",
    ),
    (LoanUpdate, {}, "origination_date"),
    (LoanPaymentCreate, {"principal": USD, "interest": USD}, "paid_on"),
    (RateChangeCreate, {"new_rate": "0.04"}, "effective_date"),
    (OneOffExtraIn, {"amount": USD}, "on"),
    (RecurringExtraIn, {"amount": USD}, "start"),
    (RecurringExtraIn, {"amount": USD}, "end"),
    (AssetCreate, {"account_id": ACCOUNT, "value": USD}, "valued_on"),
    (ValuationCreate, {"value": USD}, "valued_on"),
    (FxRateSet, {"base": "EUR", "quote": "USD", "rate": "1.1"}, "rate_date"),
    (
        ScheduleCreate,
        {
            "account_id": ACCOUNT,
            "name": "Synthetic rent",
            "cadence": "monthly",
            "amount_min": USD,
            "amount_max": USD,
        },
        "next_due",
    ),
    (ScheduleUpdate, {}, "next_due"),
]


@pytest.mark.parametrize(
    ("schema", "payload", "field"), CASES, ids=[f"{c[0].__name__}.{c[2]}" for c in CASES]
)
def test_dates_before_the_bound_are_refused(
    schema: type[BaseModel], payload: dict[str, Any], field: str
) -> None:
    for ancient in (TOO_OLD, date(1, 1, 2), date(1000, 1, 2)):
        with pytest.raises(ValidationError) as caught:
            schema.model_validate({**payload, field: ancient.isoformat()})
        assert any(err["loc"][-1] == field for err in caught.value.errors())
    accepted = schema.model_validate({**payload, field: MIN_BUSINESS_DATE.isoformat()})
    assert getattr(accepted, field) == MIN_BUSINESS_DATE


def test_bound_helpers() -> None:
    assert date(1900, 1, 1) == MIN_BUSINESS_DATE
    assert in_business_range(MIN_BUSINESS_DATE)
    assert not in_business_range(TOO_OLD)
    assert min_date_check("trade_date") == "trade_date >= '1900-01-01'::date"
