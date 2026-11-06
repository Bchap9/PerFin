# SPDX-License-Identifier: AGPL-3.0-or-later
"""Import as read (``app.domain.investments.statement_import``, step 3).

Which held statements a person may import as printed, what the review item
says did not add up, and the resolution body's shape.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.domain.investments.statement_import import acceptable_as_read, differences
from app.domain.review.schemas import ResolveLowConfidence
from app.ingestion.parsers.brokerage import reconcile_investment
from app.ingestion.sandbox.protocol import (
    Balance,
    InvestmentActivity,
    InvestmentPosition,
    InvestmentStatement,
    SecurityRef,
)

ON = date(2026, 1, 31)


@pytest.mark.parametrize(
    ("payload", "acceptable"),
    [
        ({"reason": "investment_reconciliation_failed", "failed": ["cash"]}, True),
        ({"reason": "investment_low_confidence", "fields": ["reconciliation"]}, True),
        # A guessed date or an unread figure is never imported on a click.
        (
            {"reason": "investment_low_confidence", "fields": ["reconciliation", "trade_date"]},
            False,
        ),
        ({"reason": "investment_low_confidence", "fields": ["price"]}, False),
        ({"reason": "investment_low_confidence"}, False),
        ({"reason": "investment_date_out_of_range"}, False),
        ({"reason": "reconciliation_failed"}, False),
        ({}, False),
    ],
)
def test_which_held_statements_may_be_imported_as_read(
    payload: dict[str, Any], acceptable: bool
) -> None:
    assert acceptable_as_read(payload) is acceptable


def _statement(**overrides: Any) -> InvestmentStatement:
    values: dict[str, Any] = {
        "source_format": "pdf",
        "period_start": date(2026, 1, 1),
        "period_end": ON,
        "as_of": ON,
        "positions": [
            InvestmentPosition(
                security=SecurityRef(symbol="ZZAA"),
                quantity=Decimal(10),
                unit_price=Decimal("12.50"),
                market_value=Decimal("125.00"),
            ),
            InvestmentPosition(
                security=SecurityRef(symbol="ZZBB"),
                quantity=Decimal(4),
                unit_price=Decimal("20.00"),
                market_value=Decimal("90.00"),
            ),
        ],
        "activity": [
            InvestmentActivity(trade_date=date(2026, 1, 9), type="dividend", amount=Decimal("3.10"))
        ],
        "cash_opening": Balance(amount=Decimal("100.00"), as_of=date(2026, 1, 1)),
        "cash_closing": Balance(amount=Decimal("103.40"), as_of=ON),
        "positions_value": Balance(amount=Decimal("200.00"), as_of=ON),
        "total_value": Balance(amount=Decimal("320.00"), as_of=ON),
    }
    values.update(overrides)
    return InvestmentStatement(**values)


def test_differences_name_each_failed_check_with_its_figures() -> None:
    statement = _statement()
    check = reconcile_investment(statement)
    assert check.failed == ["positions", "holdings_total", "cash", "total_value"]
    found = {d["check"]: d for d in differences(statement, check)}
    # 4 x 20.00 is 80.00, printed 90.00.
    assert found["positions"] == {
        "check": "positions",
        "printed": "90.000000",
        "computed": "80.000000",
        "difference": "-10.000000",
        "count": 1,
    }
    # The rows' 215.00 against the printed 200.00 holdings.
    assert (found["holdings_total"]["printed"], found["holdings_total"]["computed"]) == (
        "200.000000",
        "215.000000",
    )
    assert found["holdings_total"]["difference"] == "15.000000"
    # 100.00 + 3.10 is 103.10, printed 103.40.
    assert found["cash"]["difference"] == "-0.300000"
    # 215.00 + 103.40 is 318.40, printed 320.00.
    assert found["total_value"]["difference"] == "-1.600000"


def test_a_statement_that_adds_up_has_no_differences() -> None:
    statement = _statement(
        positions=[
            InvestmentPosition(
                security=SecurityRef(symbol="ZZAA"),
                quantity=Decimal(10),
                unit_price=Decimal("12.50"),
                market_value=Decimal("125.00"),
            )
        ],
        positions_value=Balance(amount=Decimal("125.00"), as_of=ON),
        cash_closing=Balance(amount=Decimal("103.10"), as_of=ON),
        total_value=Balance(amount=Decimal("228.10"), as_of=ON),
    )
    check = reconcile_investment(statement)
    assert check.status == "ok"
    assert differences(statement, check) == []


def test_import_as_read_takes_no_row_corrections() -> None:
    body = ResolveLowConfidence(kind="low_confidence_field", import_as_read=True)
    assert body.import_as_read
    assert body.transactions == []
    with pytest.raises(ValidationError):
        ResolveLowConfidence.model_validate(
            {
                "kind": "low_confidence_field",
                "import_as_read": True,
                "transactions": [
                    {"transaction_id": "00000000-0000-7000-8000-000000000001", "memo": "x"}
                ],
            }
        )
