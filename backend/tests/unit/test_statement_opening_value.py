# SPDX-License-Identifier: AGPL-3.0-or-later
"""The value a brokerage statement opens with, as a balance of the day before.

Taken from the statement's own account-value summary, and only when that
summary is checked twice: it closes with the value the import records for
the period's end, and it adds up from its opening line to its closing one.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.domain.investments.statement_import import opening_value
from app.ingestion.sandbox.protocol import InvestmentStatement, StatementFigure

TOLERANCE = Decimal("0.01")


def _statement(
    *lines: tuple[str, str, str], start: date | None = date(2026, 10, 1)
) -> InvestmentStatement:
    return InvestmentStatement(
        source_format="pdf",
        period_start=start,
        period_end=date(2026, 10, 31),
        as_of=date(2026, 10, 31),
        figures=[
            StatementFigure(group="account_value", label=label, role=role, amount=Decimal(amount))  # type: ignore[arg-type]
            for label, role, amount in lines
        ],
    )


SUMMARY = (
    ("Beginning Account Value", "opening", "1000.00"),
    ("Additions", "item", "250.00"),
    ("Subtractions", "item", "-100.00"),
    ("Change in Investment Value", "item", "50.00"),
    ("Ending Account Value", "closing", "1200.00"),
)


def test_the_opening_value_is_the_day_before_the_period() -> None:
    found = opening_value(_statement(*SUMMARY), Decimal("1200.00"), TOLERANCE)
    assert found == (date(2026, 9, 30), Decimal("1000.00"))


def test_a_summary_that_is_not_the_recorded_value_gives_none() -> None:
    # The summary closes with another figure than the import records.
    assert opening_value(_statement(*SUMMARY), Decimal("1300.00"), TOLERANCE) is None


def test_a_summary_that_does_not_add_up_gives_none() -> None:
    lines = [("Beginning Account Value", "opening", "900.00"), *SUMMARY[1:]]
    assert opening_value(_statement(*lines), Decimal("1200.00"), TOLERANCE) is None


def test_nothing_to_check_the_opening_line_with_gives_none() -> None:
    # No line between the opening and the closing one: nothing vouches for it.
    lines = (SUMMARY[0], SUMMARY[-1])
    assert opening_value(_statement(*lines), Decimal("1200.00"), TOLERANCE) is None
    # No period: no day to record it on.
    assert opening_value(_statement(*SUMMARY, start=None), Decimal("1200.00"), TOLERANCE) is None
    # A breakdown line is not added again.
    detailed = _statement(*SUMMARY)
    detailed.figures.insert(
        2,
        StatementFigure(
            group="account_value",
            label="Deposits",
            role="item",
            detail=True,
            amount=Decimal("250.00"),
        ),
    )
    assert opening_value(detailed, Decimal("1200.00"), TOLERANCE) == (
        date(2026, 9, 30),
        Decimal("1000.00"),
    )
