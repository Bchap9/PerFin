# SPDX-License-Identifier: AGPL-3.0-or-later
"""Header and balance rows of a table are not transactions (synthetic files).

A row that repeats the header row is a note row, not an unreadable one; a
dated row described as a balance is a note row too, and an opening one gives
the statement's opening balance.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.domain.imports import proposals
from app.domain.imports.schemas import MappingSpec
from app.ingestion.parsers.structured import ParsedStatement, from_table
from app.ingestion.sandbox.protocol import Table

# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


def _table(rows: list[list[str]]) -> Table:
    return Table(
        encoding="utf-8",
        delimiter=",",
        has_header=True,
        rows=rows,
        column_count=max(len(r) for r in rows),
    )


SPEC = MappingSpec(
    date_column=0, date_format="YYYY-MM-DD", payee_column=1, amount_column=2, balance_column=3
)


def test_table_row_repeating_the_header_is_a_note_row() -> None:
    table = _table(
        [
            ["Date", "Description", "Amount", "Balance"],
            ["2026-03-02", "Orchard Row Market", "-23.80", "976.20"],
            [" date ", "DESCRIPTION", "Amount ", "balance"],
            ["2026-03-09", "Stipend", "410.00", "1386.20"],
        ]
    )
    parsed = from_table(table, SPEC)
    assert [t.row for t in parsed.transactions] == [1, 3]
    assert parsed.skipped_rows == []
    assert parsed.note_rows == [2]
    assert parsed.balance_mismatches == []


def test_table_row_repeating_the_header_after_the_last_row_is_a_note_row() -> None:
    table = _table(
        [
            ["Date", "Description", "Amount", "Balance"],
            ["2026-03-02", "Orchard Row Market", "-23.80", "976.20"],
            ["Date", "Description", "Amount", "Balance"],
        ]
    )
    parsed = from_table(table, SPEC)
    assert parsed.skipped_rows == []
    assert parsed.footer_rows == []
    assert parsed.note_rows == [2]


@pytest.mark.parametrize(
    "label", ["Opening Balance", "Balance brought forward", "PREVIOUS BALANCE"]
)
def test_table_opening_balance_row_is_the_opening_balance(label: str) -> None:
    table = _table(
        [
            ["Date", "Description", "Amount", "Balance"],
            ["2026-03-01", label, "", "1000.00"],
            ["2026-03-02", "Orchard Row Market", "-23.80", "976.20"],
            ["2026-03-09", "Stipend", "410.00", "1386.20"],
        ]
    )
    parsed = from_table(table, SPEC)
    assert [t.row for t in parsed.transactions] == [2, 3]
    assert parsed.skipped_rows == []
    assert parsed.note_rows == [1]
    assert parsed.opening_balance == Decimal("1000.00")
    assert parsed.closing_balance is not None
    assert parsed.closing_balance.amount == Decimal("1386.20")


def test_table_opening_balance_in_the_amount_column_is_no_transaction() -> None:
    table = _table(
        [
            ["Date", "Description", "Amount"],
            ["2026-03-01", "Opening balance", "1000.00"],
            ["2026-03-02", "Orchard Row Market", "-23.80"],
        ]
    )
    parsed = from_table(
        table, MappingSpec(date_column=0, date_format="YYYY-MM-DD", payee_column=1, amount_column=2)
    )
    assert [t.row for t in parsed.transactions] == [2]
    assert parsed.note_rows == [1]
    assert parsed.opening_balance == Decimal("1000.00")


def test_table_closing_balance_row_is_a_note_row() -> None:
    table = _table(
        [
            ["Date", "Description", "Amount", "Balance"],
            ["2026-03-02", "Orchard Row Market", "-23.80", "976.20"],
            ["2026-03-31", "Closing Balance", "", "976.20"],
        ]
    )
    parsed = from_table(table, SPEC)
    assert [t.row for t in parsed.transactions] == [1]
    assert parsed.skipped_rows == []
    assert parsed.footer_rows == []
    assert parsed.note_rows == [2]
    assert parsed.opening_balance is None


def test_table_balance_wording_with_more_text_stays_a_transaction() -> None:
    table = _table(
        [
            ["Date", "Description", "Amount", "Balance"],
            ["2026-03-02", "Opening balance fee", "-5.00", "995.00"],
        ]
    )
    parsed = from_table(table, SPEC)
    assert [t.row for t in parsed.transactions] == [1]
    assert parsed.opening_balance is None


# ---------------------------------------------------------------------------
# The opening balance's date
# ---------------------------------------------------------------------------


def _proposed_opening(parsed: ParsedStatement) -> tuple[str | None, date | None]:
    proposal = proposals._proposal(proposals._parsed("table", 0, parsed), None, "USD")
    return proposal.opening_balance, proposal.opening_balance_date


def test_table_opening_balance_is_proposed_as_of_the_day_before_its_row() -> None:
    # A snapshot is the balance at the end of its day: dated on the opening
    # row's own day it would leave out a transaction of that day.
    table = _table(
        [
            ["Date", "Description", "Amount", "Balance"],
            ["2026-03-02", "Opening Balance", "", "1000.00"],
            ["2026-03-02", "Orchard Row Market", "-23.80", "976.20"],
            ["2026-03-09", "Stipend", "410.00", "1386.20"],
        ]
    )
    assert _proposed_opening(from_table(table, SPEC)) == ("1000.00", date(2026, 3, 1))


def test_table_opening_balance_a_day_before_the_first_row_is_proposed_before_it() -> None:
    table = _table(
        [
            ["Date", "Description", "Amount", "Balance"],
            ["2026-03-01", "Opening Balance", "", "1000.00"],
            ["2026-03-02", "Orchard Row Market", "-23.80", "976.20"],
        ]
    )
    assert _proposed_opening(from_table(table, SPEC)) == ("1000.00", date(2026, 2, 28))


def test_table_opening_balance_row_without_a_date_is_proposed_before_every_row() -> None:
    table = _table(
        [
            ["Date", "Description", "Amount", "Balance"],
            ["", "Opening Balance", "", "1000.00"],
            ["2026-03-02", "Orchard Row Market", "-23.80", "976.20"],
            ["2026-03-09", "Stipend", "410.00", "1386.20"],
        ]
    )
    assert _proposed_opening(from_table(table, SPEC)) == ("1000.00", date(2026, 3, 1))
