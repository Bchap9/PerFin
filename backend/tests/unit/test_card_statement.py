# SPDX-License-Identifier: AGPL-3.0-or-later
"""Card statements with a statement date and no period (``synth_card_statement``).

The synthetic "Statement of Account" (invented issuer, cardholder and
merchants) is extracted, classified and read by the generic card parser:
the period comes from the statement date and the rows, month-named yearless
dates take the year that keeps them on or before the statement date (the
December rows are the year before), credits marked ``CR`` on the same or
the next line are money in, the foreign-spending column only goes to the
memo, the payment stub is no part of the statement, and the boxed
equation summary (previous - credits + charges = new) reconciles.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.ingestion.classify.service import classify_extraction
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.table_statement import (
    CardStatementParser,
    TableStatementParser,
    anchored_period,
    month_before,
)
from app.ingestion.parsers.text import infer_year, month_day, read_date
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Statement
from tests.fixtures import synth_card_statement as synth

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _extract(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return result


def _parse(data: bytes, *, hinted: bool = True) -> ExtractResult:
    result = _extract(data)
    if not hinted:
        return parse_extraction(result)
    found = classify_extraction(result)
    return parse_extraction(
        result,
        parser_name=found.parser,
        institution=found.fingerprint.key if found.fingerprint else None,
        kind=found.kind,
        date_order=found.fingerprint.spec.date_order if found.fingerprint else None,
    )


def _only(result: ExtractResult) -> Statement:
    assert len(result.statements) == 1, result.statements
    return result.statements[0]


@pytest.mark.parametrize("hinted", [True, False], ids=["classified", "detected"])
def test_statement_of_account_reads_exactly_and_reconciles(hinted: bool) -> None:
    stmt = synth.card_statement()
    result = _parse(synth.card_pdf(stmt), hinted=hinted)
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "card_statement_generic"
    # The stub's dated "Amount Due" line is neither a row nor a skipped line.
    assert result.parse.skipped_lines == 0
    assert result.parse.warnings == []
    statement = _only(result)
    assert len(statement.transactions) == len(stmt.rows) == 24
    assert sorted((t.posted, t.amount, t.payee) for t in statement.transactions) == (
        synth.expected(stmt)
    )
    # The masked number of the header, not the stub's digits.
    assert statement.account.account_type == "creditcard"
    assert statement.account.last4 == synth.LAST4
    # Period: the statement date ends it, the earliest row (older than the
    # day after the same date a month before) starts it.
    assert stmt.rows[0].day < stmt.cycle_start
    assert (statement.period_start, statement.period_end) == (
        date(2024, 12, 13),
        date(2025, 1, 14),
    )
    assert statement.confidence["period"] >= 0.9
    # Balances owed are negative in the ledger; previous + rows = new.
    assert statement.opening_balance is not None
    assert statement.ledger_balance is not None
    assert statement.opening_balance.amount == stmt.opening_balance == Decimal("-7412.06")
    assert statement.ledger_balance.amount == stmt.closing_balance
    assert statement.ledger_balance.as_of == stmt.statement_date
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.reconciliation.difference == 0
    assert all(
        t.confidence is not None and t.confidence >= 0.9 and not t.low_confidence
        for t in statement.transactions
    )
    assert ExtractResult.model_validate(result.model_dump(mode="json")) == result


def test_signs_years_credits_and_memos() -> None:
    stmt = synth.card_statement()
    statement = _only(_parse(synth.card_pdf(stmt)))
    rows = sorted(statement.transactions, key=lambda t: (t.posted, t.amount))
    # Charges are money out, credits (CR on the same line or under it) money in.
    credits = [t for t in rows if t.amount > 0]
    assert [(t.posted, t.amount) for t in credits] == [
        (date(2024, 12, 30), Decimal("18.45")),
        (date(2025, 1, 7), Decimal("75.00")),
        (date(2025, 1, 11), Decimal("60.00")),
    ]
    assert sum(t.amount for t in credits) == stmt.credits
    assert -sum(t.amount for t in rows if t.amount < 0) == stmt.charges
    # December is the year before the January statement date; nothing after it.
    assert {t.posted.year for t in rows if t.posted.month == 12} == {2024}
    assert {t.posted.year for t in rows if t.posted.month == 1} == {2025}
    assert max(t.posted for t in rows) == stmt.statement_date
    # The category line is the memo; the foreign amount is kept there, not booked.
    tidewater = next(t for t in rows if t.payee and t.payee.startswith("TIDEWATER"))
    assert tidewater.amount == Decimal("-289.47")
    assert tidewater.memo == "Goods or Services; foreign amount 201.00 US Dollar"
    rail = next(t for t in rows if t.payee == "ALPENGLOW RAIL ZURICH")
    assert rail.memo == "Travel - Rail; foreign amount 131.50 Swiss Franc"
    books = next(t for t in rows if t.posted == date(2024, 12, 13))
    assert books.payee == "NORTHWIND BOOKS ONLINE NWBOOKS.EXAMPLE"
    assert books.memo == "INTERNET - ELEC DELIVERY"
    # A "*" after the date (a processing date) does not stop the row.
    assert any(t.posted == date(2025, 1, 9) and t.amount == Decimal("-19.99") for t in rows)
    assert all(t.page is not None and t.bbox is not None for t in rows)
    assert {t.page for t in rows} == {1, 2, 3}


def test_period_starts_with_the_cycle_when_no_row_is_older() -> None:
    stmt = synth.later_rows(synth.card_statement())
    assert stmt.rows
    assert min(r.day for r in stmt.rows) > stmt.cycle_start
    statement = _only(_parse(synth.card_pdf(stmt)))
    assert (statement.period_start, statement.period_end) == (
        date(2024, 12, 15),
        date(2025, 1, 14),
    )
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_a_printed_previous_statement_date_starts_the_period() -> None:
    stmt = synth.card_statement()
    stmt = synth.CardStatement(
        statement_date=stmt.statement_date,
        previous_balance=stmt.previous_balance,
        rows=stmt.rows,
        previous_statement=date(2024, 12, 12),
    )
    statement = _only(_parse(synth.card_pdf(stmt)))
    assert (statement.period_start, statement.period_end) == (
        date(2024, 12, 13),
        date(2025, 1, 14),
    )
    assert stmt.period_start == statement.period_start


def test_summary_totals_that_do_not_match_the_rows_do_not_reconcile() -> None:
    base = synth.card_statement()
    stmt = synth.CardStatement(
        statement_date=base.statement_date,
        previous_balance=base.previous_balance,
        rows=base.rows,
        summary_shift=Decimal("100.00"),
    )
    result = _parse(synth.card_pdf(stmt))
    statement = _only(result)
    assert statement.reconciliation is not None
    # Previous + rows = new still holds; the equation's own totals do not.
    assert statement.reconciliation.difference == 0
    assert statement.reconciliation.status == "mismatch"
    assert result.parse is not None
    assert "summary_totals_mismatch" in result.parse.warnings
    assert all(t.low_confidence for t in statement.transactions)


def test_the_stub_is_left_out_but_a_table_after_it_is_read() -> None:
    stmt = synth.card_statement()
    view = DocumentView.from_result(_extract(synth.card_pdf(stmt)))
    parser = CardStatementParser()
    kept = parser.without_stubs(view.lines)
    texts = {line.text for line in kept}
    assert not any("detach" in t.lower() for t in texts)
    assert not any(synth.STUB_NUMBER in t for t in texts)
    assert sum(1 for line in kept if line.page == 1) < sum(
        1 for line in view.lines if line.page == 1
    )
    # Pages after the stub's are untouched.
    assert [line for line in kept if line.page > 1] == [
        line for line in view.lines if line.page > 1
    ]
    # A table header after a stub marker on the same page resumes reading.
    marker = next(line for line in view.lines if "detach" in line.text.lower())
    header = next(line for line in view.lines if parser.columns(line) is not None)
    moved = header.__class__(
        page=marker.page,
        index=marker.index + 1,
        cells=header.cells,
        text=header.text,
        height=header.height,
    )
    assert parser.without_stubs([marker, moved]) == [moved]


def test_headless_header_needs_a_table_heading_and_a_money_column() -> None:
    view = DocumentView.from_result(_extract(synth.card_pdf(synth.card_statement())))
    parser = CardStatementParser()
    headers = [line for line in view.lines if parser.columns(line) is not None]
    assert len(headers) == 3  # page 1 and both continuation pages
    columns = parser.columns(headers[0])
    assert columns is not None
    assert [c.role for c in columns] == ["lead", "foreign", "amount"]
    # Labels without a table heading are no header ("Payment Due Date |
    # Amount Due $ | Amount Paid $" on the stub, the summary's labels).
    for line in view.lines:
        if line.text.startswith(("Payment Due Date", "Previous Balance", "Prepared For")):
            assert parser.columns(line) is None


def test_month_named_yearless_dates() -> None:
    assert month_day("December 16", "mon_d") == (12, 16)
    assert month_day("Sept 5", "mon_d") == (9, 5)
    assert month_day("16 Dec", "d_mon") == (12, 16)
    assert month_day("Smarch 5", "mon_d") is None
    assert month_day("December 32", "mon_d") is None
    assert month_day("December 16", "mdy") is None
    end = date(2021, 1, 16)
    window = (end - timedelta(days=364), end)
    # Strict: the latest date not after the statement date.
    assert read_date("December 16", "mon_d", window=window, strict=True) == date(2020, 12, 16)
    assert read_date("January 5", "mon_d", window=window, strict=True) == date(2021, 1, 5)
    assert read_date("January 20", "mon_d", window=window, strict=True) == date(2020, 1, 20)
    assert read_date("12/16", "md", window=window, strict=True) == date(2020, 12, 16)
    # Not strict: a week of slack after the period, as before.
    assert infer_year(1, 20, (date(2020, 12, 17), end)) == date(2021, 1, 20)
    assert read_date("December 16", "mon_d") is None  # yearless needs a window


def test_anchored_period() -> None:
    end = date(2025, 3, 31)
    assert month_before(end) == date(2025, 2, 28)
    assert month_before(date(2025, 1, 14)) == date(2024, 12, 14)
    assert anchored_period(end, None, []) == (date(2025, 3, 1), end)
    assert anchored_period(end, None, [date(2025, 2, 20), date(2025, 3, 2)]) == (
        date(2025, 2, 20),
        end,
    )
    assert anchored_period(end, date(2025, 2, 26), [date(2025, 2, 20)]) == (
        date(2025, 2, 27),
        end,
    )


def _view(rows: list[list[tuple[str, float]]]) -> DocumentView:
    """One page of lines, each a list of ``(text, x0)`` cells, 14 points apart."""
    lines = []
    for index, cells in enumerate(rows):
        y = 100.0 + 14 * index
        built = tuple(Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0) for text, x0 in cells)
        lines.append(
            ViewLine(
                page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
            )
        )
    return DocumentView(lines=tuple(lines), source_format="pdf", truncated=False, page_count=1)


HEADER = [("Date", 62.0), ("Description", 150.0), ("Amount", 500.0)]


def test_a_dated_row_after_the_statement_date_drops_it() -> None:
    view = _view(
        [
            [("Prepared For", 62.0), ("Date", 400.0)],
            [("JORDAN Q SAMPLE", 62.0), ("January 14, 2025", 400.0)],
            HEADER,
            [("01/10/2025", 62.0), ("Coffee", 150.0), ("4.50", 500.0)],
            [("01/20/2025", 62.0), ("Tea", 150.0), ("3.00", 500.0)],
        ]
    )
    (statement,) = TableStatementParser().parse(view, threshold=0.85).statements
    # Not a statement date after all: the period is the rows' span, inferred.
    assert (statement.period_start, statement.period_end) == (date(2025, 1, 10), date(2025, 1, 20))
    assert statement.confidence["period"] < 0.85


def test_date_over_a_balance_column_is_no_statement_date() -> None:
    view = _view(
        [
            HEADER,
            [("01/05/2025", 62.0), ("Coffee", 150.0), ("4.50", 500.0)],
            [("01/07/2025", 62.0), ("Tea", 150.0), ("3.00", 500.0)],
            [("Date", 62.0), ("Balance", 150.0)],
            [("01/06/2025", 62.0), ("1,000.00", 150.0)],
        ]
    )
    (statement,) = TableStatementParser().parse(view, threshold=0.85).statements
    assert (statement.period_start, statement.period_end) == (date(2025, 1, 5), date(2025, 1, 7))
    assert statement.confidence["period"] < 0.85


def test_a_labelled_statement_date_on_its_line_and_numeric_yearless_rows() -> None:
    view = _view(
        [
            [("Statement Date: 01/14/2025", 62.0)],
            [("Previous Balance", 62.0), ("New Balance", 300.0)],
            [("100.00", 62.0), ("110.00", 300.0)],
            HEADER,
            [("12/30", 62.0), ("Refund", 150.0), ("5.00 CR", 500.0)],
            [("01/02", 62.0), ("Lunch", 150.0), ("15.00", 500.0)],
        ]
    )
    (statement,) = CardStatementParser().parse(view, threshold=0.85).statements
    assert sorted((t.posted, t.amount) for t in statement.transactions) == [
        (date(2024, 12, 30), Decimal("5.00")),
        (date(2025, 1, 2), Decimal("-15.00")),
    ]
    assert (statement.period_start, statement.period_end) == (date(2024, 12, 15), date(2025, 1, 14))
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_an_opening_closing_date_range_is_the_period_not_a_statement_date() -> None:
    view = _view(
        [
            [("Opening/Closing Date 12/15/24 - 01/14/25", 62.0)],
            HEADER,
            [("12/20", 62.0), ("Lunch", 150.0), ("15.00", 500.0)],
        ]
    )
    (statement,) = CardStatementParser().parse(view, threshold=0.85).statements
    assert (statement.period_start, statement.period_end) == (date(2024, 12, 15), date(2025, 1, 14))
    assert [t.posted for t in statement.transactions] == [date(2024, 12, 20)]
