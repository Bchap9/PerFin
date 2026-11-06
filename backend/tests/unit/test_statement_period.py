# SPDX-License-Identifier: AGPL-3.0-or-later
"""The statement period: header band, year borrowing, label candidates, yearless rows.

INGESTION.md "Time window detection". Covers
:func:`app.ingestion.classify.dates.find_ranges` /
:func:`~app.ingestion.classify.dates.header_band_range`, the classifier's
:func:`~app.ingestion.classify.period.detect_period` on the synthetic
multi-account fixtures (``tests/fixtures/synth_fidelity_report.py``,
``tests/fixtures/synth_neutral_report.py``), and the statement parsers'
period readers (``parsers.table_statement.read_period`` and the brokerage
parser) on small synthetic PDFs. Everything is invented.
"""

from __future__ import annotations

import io
from collections.abc import Callable
from datetime import date
from decimal import Decimal
from typing import cast

import pytest
from reportlab.pdfgen import canvas

from app.domain.documents.models import Document
from app.domain.imports.statement_records import period_stated
from app.ingestion.classify.dates import find_ranges, header_band_range
from app.ingestion.classify.period import detect_period
from app.ingestion.classify.service import apply_classification, classify_extraction
from app.ingestion.classify.text import DocumentView as ClassifyView
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import DocumentView
from app.ingestion.parsers.brokerage import BrokerageStatementParser
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.table_statement import (
    PeriodCandidates,
    TableStatementParser,
    read_period,
)
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from tests.fixtures import synth_fidelity_report as fidr
from tests.fixtures import synth_neutral_report as neutral

OPTIONS = ExtractOptions(previews=False, ocr=False)
JULY = (date(2015, 7, 1), date(2015, 7, 31))

Cells = list[tuple[float, str]]


def _extract(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return result


def _pdf(pages: list[list[Cells]]) -> bytes:
    """A PDF whose pages print the given lines, each a list of ``(x, text)`` cells."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(612, 792))
    c.setFont("Helvetica", 9)
    for page in pages:
        y = 760.0
        for line in page:
            for x, text in line:
                c.drawString(x, y, text)
            y -= 14
        c.drawString(40, 30, "Synthetic test document from the PerFin test suite.")
        c.showPage()
    c.save()
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Ranges in a line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("July 1 \u2013 July 31, 2015", JULY),
        ("July 1 \u2014 July 31, 2015", JULY),
        ("Jul 1 - Jul 31, 2015", JULY),
        ("July 1 to July 31, 2015", JULY),
        ("07/01/2015 - 07/31/2015", JULY),
        ("07/01/2015 through 07/31/2015", JULY),
        ("2015-07-01 to 2015-07-31", JULY),
        ("1 July 2015 \u2013 31 July 2015", JULY),
        ("01.07.2015 \u2013 31.07.2015", JULY),
        ("Dec 15 - Jan 14, 2016", (date(2015, 12, 15), date(2016, 1, 14))),
        ("Statement Period: 06/01/2019 - 06/30/2019", (date(2019, 6, 1), date(2019, 6, 30))),
    ],
)
def test_ranges_borrow_the_year_and_settle_the_order(
    line: str, expected: tuple[date, date]
) -> None:
    (found,) = find_ranges(line)
    assert (found.start, found.end) == expected
    assert not found.ambiguous


@pytest.mark.parametrize(
    "line",
    [
        "07/01/2015 07/31/2015",  # two dates, no separator: a table row
        "Change from Last Period: \u25b2 $21,000.37",
        "Maturity 03/15/2017 Coupon 4.50%",
        "July 31, 2015 - July 1, 2015",  # ends before it starts
        "Jan 1, 2014 - Jan 31, 2016",  # longer than a statement
    ],
)
def test_no_range(line: str) -> None:
    assert find_ranges(line) == []


def test_an_ambiguous_range_stays_ambiguous() -> None:
    (found,) = find_ranges("03/04/2026 - 04/04/2026")
    assert found.ambiguous
    (eu,) = find_ranges("03/04/2026 - 04/04/2026", order="dmy")
    assert (eu.start, eu.end) == (date(2026, 4, 3), date(2026, 4, 4))


# ---------------------------------------------------------------------------
# The header band
# ---------------------------------------------------------------------------


def _pages(
    count: int, *, every: int = 1, line: str = "July 1 \u2013 July 31, 2015"
) -> list[list[str]]:
    return [
        ["SYNTHETIC REPORT", line if index % every == 0 else "Continued", "Holdings"]
        for index in range(count)
    ]


def test_header_band_needs_half_the_pages() -> None:
    band = header_band_range(_pages(6))
    assert band is not None
    assert (band.start, band.end, band.pages, band.total) == (*JULY, 6, 6)
    assert header_band_range(_pages(6, every=2)) is not None  # three of six
    assert header_band_range(_pages(6, every=3)) is None  # two of six


def test_header_band_only_reads_the_top_lines() -> None:
    low = [["Title", *(f"line {n}" for n in range(12)), "July 1 \u2013 July 31, 2015"]] * 3
    assert header_band_range(low) is None


def test_header_band_prefers_the_range_on_most_pages() -> None:
    pages = _pages(4)
    pages[0].append("June 1 \u2013 June 30, 2015")  # last month's figures on page 1
    band = header_band_range(pages)
    assert band is not None
    assert (band.start, band.end) == JULY


def test_classifier_reads_the_unlabelled_header_period() -> None:
    view = ClassifyView(
        lines=tuple(line for page in _pages(5) for line in page),
        header=tuple(_pages(1)[0]),
        page_heads=tuple(tuple(page) for page in _pages(5)),
    )
    period = detect_period(view)
    assert (period.start, period.end, period.source, period.inferred) == (*JULY, "header", False)
    assert period.confidence == 0.9
    single = ClassifyView(lines=(), header=(), page_heads=(tuple(_pages(1)[0]),))
    assert detect_period(single).confidence == 0.75


def test_fixture_report_periods() -> None:
    fid = classify_extraction(_extract(fidr.report_pdf()))
    assert (fid.period.start, fid.period.end, fid.period.source) == (
        date(2019, 6, 1),
        date(2019, 6, 30),
        "header",
    )
    bare = classify_extraction(_extract(fidr.report_pdf(institution_metadata=False)))
    assert (bare.period.start, bare.period.end) == (date(2019, 6, 1), date(2019, 6, 30))
    other = classify_extraction(_extract(fidr.report_pdf(*fidr.report(date(2021, 7, 1)))))
    assert (other.period.start, other.period.end) == (date(2021, 7, 1), date(2021, 7, 31))
    bank = classify_extraction(_extract(neutral.report_pdf()))
    assert (bank.period.start, bank.period.end, bank.period.source) == (
        neutral.START,
        neutral.END,
        "label",
    )
    # The month-first order is proven by "03/31/2024".
    assert bank.period.confidence == 0.95


def test_fixture_reports_parse_with_the_header_period() -> None:
    # The header band (on every page) is believed as far as the classifier
    # believes it (0.9: stated); a labelled period gets the parser's own.
    for data, period, believed in (
        (fidr.report_pdf(), (date(2019, 6, 1), date(2019, 6, 30)), 0.9),
        (neutral.report_pdf(), (neutral.START, neutral.END), 0.95),
    ):
        result = _extract(data)
        parsed = parse_extraction(result, kind=classify_extraction(result).kind)
        found = [
            *((s.period_start, s.period_end, s.confidence["period"]) for s in parsed.statements),
            *((i.period_start, i.period_end, i.confidence["period"]) for i in parsed.investments),
        ]
        assert found
        for start, end, confidence in found:
            assert (start, end) == period
            assert confidence >= believed


def test_an_inferred_period_is_a_hint_only() -> None:
    text = "Riverbend Test Bank\nStatement\n01/03/2026 COFFEE 3.00\n03/15/2026 MATURITY 9.00"
    found = classify_extraction(ExtractResult(kind="txt", ok=True, text=text))
    assert found.kind == "statement"
    assert found.period.inferred
    assert found.period.source == "line_dates"
    document = Document()
    apply_classification(cast("object", None), document, found)  # type: ignore[arg-type]
    assert (document.period_start, document.period_end) == (None, None)
    assert document.confidence is not None
    assert document.confidence["period"] == {
        "source": "line_dates",
        "inferred": True,
        "start": "2026-01-03",
        "end": "2026-03-15",
    }


# ---------------------------------------------------------------------------
# The parsers' period readers
# ---------------------------------------------------------------------------


def test_label_candidates_do_not_latch_on_the_first_match() -> None:
    labels = TableStatementParser.period_labels
    candidates = PeriodCandidates()
    for line in (
        "Change from Last Period: $21,000.37",
        "Statement Period",
        "July 1 \u2013 July 31, 2015",
    ):
        candidates.scan(line, labels)
    assert candidates.texts == [": $21,000.37", "July 1 \u2013 July 31, 2015"]
    view = DocumentView(lines=(), source_format="pdf", truncated=False, page_count=0)
    found = read_period(candidates.texts, ("mdy", "mon_d_y"), view)
    assert found is not None
    assert (found.span, found.source) == (JULY, "label")


_HEADER: Cells = [(50, "Date"), (130, "Description"), (400, "Amount"), (500, "Balance")]


def _bank_pages(period_line: str | None) -> list[list[Cells]]:
    head: list[Cells] = [[(50, "RIVERBEND TEST BANK")]]
    if period_line is not None:
        head.append([(50, period_line)])
    first: list[Cells] = [
        *head,
        [(50, "Change from last period: +$40.00")],
        [(50, "Account ending in 4821")],
        [(50, "Beginning balance"), (400, "$1,000.00")],
        [(50, "Ending balance"), (400, "$1,040.00")],
        _HEADER,
        [(50, "07/02"), (130, "Payroll deposit"), (400, "100.00"), (500, "1,100.00")],
        [(50, "07/15"), (130, "Grocery store"), (400, "-60.00"), (500, "1,040.00")],
    ]
    second: list[Cells] = [*head, [(50, "Notices")], [(50, "Thank you for banking with us.")]]
    return [first, second]


def test_bank_statement_period_from_the_header_band_dates_yearless_rows() -> None:
    result = _extract(_pdf(_bank_pages("July 1 \u2013 July 31, 2015")))
    outcome = TableStatementParser().parse(DocumentView.from_result(result), threshold=0.85)
    (statement,) = outcome.statements
    assert (statement.period_start, statement.period_end) == JULY
    assert [(t.posted, t.amount) for t in statement.transactions] == [
        (date(2015, 7, 2), Decimal("100.00")),
        (date(2015, 7, 15), Decimal("-60.00")),
    ]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_bank_statement_label_alone_on_its_line() -> None:
    pages = _bank_pages(None)
    pages[0].insert(1, [(50, "Statement period")])
    pages[0].insert(2, [(50, "07/01/2015 - 07/31/2015")])
    result = _extract(_pdf(pages))
    outcome = TableStatementParser().parse(DocumentView.from_result(result), threshold=0.85)
    (statement,) = outcome.statements
    assert (statement.period_start, statement.period_end) == JULY
    assert len(statement.transactions) == 2


def test_without_a_period_yearless_rows_are_not_guessed() -> None:
    result = _extract(_pdf(_bank_pages(None)))
    outcome = TableStatementParser().parse(DocumentView.from_result(result), threshold=0.85)
    assert outcome.statements == [] or not outcome.statements[0].transactions


def test_generic_brokerage_parser_reads_yearless_activity_through_the_period() -> None:
    page: list[Cells] = [
        [(50, "SYNTHETIC BROKERAGE REPORT")],
        [(50, "July 1 \u2013 July 31, 2015")],
        [(50, "Account 999-123456")],
        [(50, "Activity")],
        [(50, "Date"), (110, "Activity"), (200, "Description"), (420, "Quantity"), (500, "Amount")],
        [(50, "7/06"), (110, "Dividend"), (200, "EXMPL Example Index Fund"), (500, "12.34")],
        [(50, "7/20"), (110, "Interest"), (200, "Core cash"), (500, "1.05")],
    ]
    result = _extract(_pdf([page, page[:2]]))
    outcome = BrokerageStatementParser().parse(DocumentView.from_result(result), threshold=0.85)
    (statement,) = outcome.investments
    assert (statement.period_start, statement.period_end) == JULY
    assert [(a.trade_date, a.type, a.amount) for a in statement.activity] == [
        (date(2015, 7, 6), "dividend", Decimal("12.34")),
        (date(2015, 7, 20), "interest", Decimal("1.05")),
    ]


# ---------------------------------------------------------------------------
# Day-first periods, day-only ranges, how far a band is believed
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("July 1-31, 2015", JULY),
        ("Statement Period: July 1 - 31, 2015", JULY),
        ("Jul 1st \u2013 31st, 2015", JULY),
        ("1-31 July 2015", JULY),
        ("1 \u2013 31 Jul 2015", JULY),
        ("Feb 1 to 28, 2026", (date(2026, 2, 1), date(2026, 2, 28))),
    ],
)
def test_ranges_whose_end_is_only_a_day(line: str, expected: tuple[date, date]) -> None:
    (found,) = find_ranges(line)
    assert (found.start, found.end) == expected
    assert not found.ambiguous


@pytest.mark.parametrize(
    "line",
    [
        "July 31-1, 2015",  # ends before it starts
        "Feb 1 - 30, 2026",  # no such day
        "Page 3 of 12 2015",
    ],
)
def test_no_day_only_range(line: str) -> None:
    assert find_ranges(line) == []


def test_the_values_beat_the_order_hint() -> None:
    # Day-first: 31 is no month. A month-first hint (a default) does not
    # turn the start into January 7.
    for order in (None, "mdy", "dmy"):
        (found,) = find_ranges("01/07/2015 - 31/07/2015", order=order)
        assert (found.start, found.end, found.order, found.ambiguous) == (*JULY, "dmy", False)
    (us,) = find_ranges("07/01/2015 - 07/31/2015", order="dmy")
    assert (us.start, us.end, us.order) == (*JULY, "mdy")
    band = header_band_range([["SYNTHETIC BANK", "01/07/2015 - 31/07/2015"]] * 3, order="mdy")
    assert band is not None
    assert (band.start, band.end, band.order) == (*JULY, "dmy")


def test_classifier_reads_day_first_and_day_only_periods() -> None:
    for lines in (
        ("Synthetic Bank", "Statement period: 01/07/2015 - 31/07/2015"),
        ("Synthetic Bank", "Statement Period: July 1-31, 2015"),
        ("Synthetic Bank", "Statement period:", "1 - 31 July 2015"),
    ):
        view = ClassifyView(lines=lines, header=lines, page_heads=(lines,))
        period = detect_period(view, order="mdy")
        assert (period.start, period.end, period.source) == (*JULY, "label"), lines
        assert period.confidence == 0.95


def _day_first_pages(band: str, rows: list[Cells]) -> list[list[Cells]]:
    head: list[Cells] = [[(50, "RIVERBEND TEST BANK")], [(50, band)]]
    first: list[Cells] = [
        *head,
        [(50, "Account ending in 4821")],
        [(50, "Beginning balance"), (400, "1,000.00")],
        [(50, "Ending balance"), (400, "1,040.00")],
        _HEADER,
        *rows,
    ]
    return [first, [*head, [(50, "Notices")]]]


@pytest.mark.parametrize(
    "row_date",
    [
        # Named-month rows say nothing about the numeric order.
        lambda day, month: f"{day} {('Jul', 'Aug')[month - 7]} 2015",
        # Day-first rows that fit both orders: the period proves day-first.
        lambda day, month: f"{day:02d}/{month:02d}/2015",
    ],
    ids=["named-rows", "ambiguous-rows"],
)
def test_bank_statement_day_first_header_band(row_date: Callable[[int, int], str]) -> None:
    periods = []
    for month in (7, 8):
        band = f"01/{month:02d}/2015 - 31/{month:02d}/2015"
        rows: list[Cells] = [
            [(50, row_date(2, month)), (130, "Payroll"), (400, "100.00"), (500, "1,100.00")],
            [(50, row_date(11, month)), (130, "Grocery"), (400, "-60.00"), (500, "1,040.00")],
        ]
        result = _extract(_pdf(_day_first_pages(band, rows)))
        outcome = TableStatementParser().parse(DocumentView.from_result(result), threshold=0.85)
        (statement,) = outcome.statements
        periods.append((statement.period_start, statement.period_end))
        assert statement.confidence["period"] == 0.9
        assert "date_order_guessed" not in outcome.warnings
        assert [t.posted for t in statement.transactions] == [
            date(2015, month, 2),
            date(2015, month, 11),
        ]
    # Consecutive months, not one month read twice (the duplicate guard's input).
    assert periods == [JULY, (date(2015, 8, 1), date(2015, 8, 31))]


def test_bank_statement_day_first_label_with_ambiguous_rows() -> None:
    rows: list[Cells] = [
        [(50, "02/07/2015"), (130, "Payroll"), (400, "100.00"), (500, "1,100.00")],
        [(50, "11/07/2015"), (130, "Grocery"), (400, "-60.00"), (500, "1,040.00")],
    ]
    pages = _day_first_pages("Statement period: 01/07/2015 - 31/07/2015", rows)
    result = _extract(_pdf(pages))
    outcome = TableStatementParser().parse(DocumentView.from_result(result), threshold=0.85)
    (statement,) = outcome.statements
    assert (statement.period_start, statement.period_end) == JULY
    assert statement.confidence["period"] == 0.95
    assert [t.posted for t in statement.transactions] == [date(2015, 7, 2), date(2015, 7, 11)]


def test_brokerage_day_first_header_band_dates_the_activity() -> None:
    page: list[Cells] = [
        [(50, "SYNTHETIC BROKERAGE REPORT")],
        [(50, "01/07/2015 - 31/07/2015")],
        [(50, "Account 999-123456")],
        [(50, "Activity")],
        [(50, "Date"), (110, "Activity"), (200, "Description"), (420, "Quantity"), (500, "Amount")],
        [(50, "06/07/2015"), (110, "Dividend"), (200, "EXMPL Example Index Fund"), (500, "12.34")],
        [(50, "10/07/2015"), (110, "Interest"), (200, "Core cash"), (500, "1.05")],
    ]
    result = _extract(_pdf([page, page[:2]]))
    outcome = BrokerageStatementParser().parse(DocumentView.from_result(result), threshold=0.85)
    (statement,) = outcome.investments
    assert (statement.period_start, statement.period_end) == JULY
    assert statement.confidence["period"] == 0.9
    assert [a.trade_date for a in statement.activity] == [date(2015, 7, 6), date(2015, 7, 10)]


def test_a_one_page_header_band_is_not_a_stated_period() -> None:
    (page, _) = _bank_pages("July 1 \u2013 July 31, 2015")
    result = _extract(_pdf([page]))
    outcome = TableStatementParser().parse(DocumentView.from_result(result), threshold=0.85)
    (statement,) = outcome.statements
    assert (statement.period_start, statement.period_end) == JULY
    # As far as the classifier believes a one-page band: below "stated".
    assert statement.confidence["period"] == 0.75
    assert not period_stated("pdf", *JULY, statement.confidence)
