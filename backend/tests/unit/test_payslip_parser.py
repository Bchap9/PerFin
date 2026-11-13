# SPDX-License-Identifier: AGPL-3.0-or-later
"""The payslip parser: per-field OCR scaling, and the layouts payroll services print.

A field's confidence follows the cells it was read from
(``confidence.scaled_fields``): a garbled word elsewhere on the page says
nothing about a figure read from clean cells, and figures that add up were
read right whatever Tesseract thought of them.

The layouts of ``tests/fixtures/synth_payslips.py`` (current and year-to-date
columns, pay and deductions side by side under one header line, labels over
their values, no printed total) must give the current figures, never a
year-to-date one, and add up; ``labelled_cells`` is pinned on its own too.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.classify.service import classify_extraction
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine
from app.ingestion.parsers.labelled_cells import (
    amount_of,
    current_cells,
    label_pairs,
    period_columns,
    value_over,
    value_under,
)
from app.ingestion.parsers.payslip import PayslipParser
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.text import read_amount
from app.ingestion.sandbox.protocol import ExtractOptions, Payslip
from tests.fixtures import synth_payslips as payslips
from tests.fixtures import synth_pdf_statements as statements

CLEAN = 0.96


def _line(index: int, *texts: str, ocr: float = CLEAN) -> ViewLine:
    y = 40.0 + 20.0 * index
    cells = tuple(
        Cell(text, 40.0 + 220.0 * at, y, 40.0 + 220.0 * at + 6.0 * len(text), y + 10.0, ocr)
        for at, text in enumerate(texts)
    )
    return ViewLine(page=1, index=index, cells=cells, text=" ".join(texts), height=10.0)


def _view(*, pay_date: float = CLEAN) -> DocumentView:
    lines = (
        _line(0, "Initech Example Ltd"),
        _line(1, "Pay Stub"),
        _line(2, "Pay period: 01/01/2026 to 01/15/2026"),
        _line(3, "Pay date: 01/20/2026", ocr=pay_date),
        _line(4, "Gross pay", "2,000.00"),
        _line(5, "Deductions"),
        _line(6, "Income tax", "300.00"),
        _line(7, "Total deductions", "300.00"),
        _line(8, "Net pay", "1,700.00"),
        _line(9, "Printed by the payroll office", ocr=0.01),
    )
    return DocumentView(lines=lines, source_format="image", truncated=False, page_count=1)


def _payslip(view: DocumentView) -> Payslip:
    outcome = PayslipParser().parse(view, threshold=conf.DEFAULT_THRESHOLD)
    assert outcome.payslip is not None
    return outcome.payslip


def test_scaled_fields() -> None:
    scores = {"a": 1.0, "b": 0.95, "c": 0.5}
    ocr: dict[str, list[float | None]] = {"a": [0.4], "b": [0.9, None, 0.8]}
    assert conf.scaled_fields(scores, ocr, confirmed={"a"}) == {"a": 1.0, "b": 0.76, "c": 0.5}


def test_scaled_fields_leaves_a_text_layer_alone() -> None:
    assert conf.scaled_fields({"a": 0.95, "b": 0.8}, {"a": [None, None], "b": []}) == {
        "a": 0.95,
        "b": 0.8,
    }


def test_a_bad_word_elsewhere_does_not_lower_a_payslip_field() -> None:
    payslip = _payslip(_view())
    assert (payslip.gross, payslip.total_deductions, payslip.net) == (
        Decimal("2000.00"),
        Decimal("300.00"),
        Decimal("1700.00"),
    )
    assert payslip.adds_up is True
    assert payslip.confidence["gross"] == 1.0
    assert payslip.confidence["net"] == 1.0
    assert payslip.confidence["deductions"] == 1.0
    assert payslip.confidence["pay_date"] == pytest.approx(CLEAN)
    assert payslip.confidence["employer"] == pytest.approx(CLEAN)
    assert payslip.low_confidence == []


def test_a_field_read_from_a_bad_cell_is_low() -> None:
    payslip = _payslip(_view(pay_date=0.30))
    assert "pay_date" in payslip.low_confidence
    assert payslip.confidence["pay_date"] == pytest.approx(0.30)
    assert payslip.confidence["gross"] == 1.0


def test_figures_that_do_not_add_up_are_scaled_by_their_cells() -> None:
    lines = list(_view().lines)
    lines[8] = _line(8, "Net pay", "1,900.00")
    view = DocumentView(lines=tuple(lines), source_format="image", truncated=False, page_count=1)
    payslip = _payslip(view)
    assert payslip.adds_up is False
    assert payslip.confidence["net"] == pytest.approx(conf.UNRECONCILED * CLEAN)
    assert {"gross", "net", "deductions"} <= set(payslip.low_confidence)


# ---------------------------------------------------------------------------
# The wordings and columns payroll services print
# ---------------------------------------------------------------------------


def _parse(data: bytes) -> Payslip:
    """Extract, classify (as the pipeline does) and parse with the classifier's hints."""
    result = extract_pdf(data, ExtractOptions(previews=False, ocr=False), "pdf")
    assert result.ok, result.error
    found = classify_extraction(result)
    parsed = parse_extraction(
        result,
        threshold=conf.DEFAULT_THRESHOLD,
        institution=found.fingerprint.key if found.fingerprint else None,
        parser_name=found.parser,
        kind=found.kind,
        date_order=found.fingerprint.spec.date_order if found.fingerprint else None,
    )
    assert parsed.parse is not None
    assert parsed.parse.parser is not None
    assert parsed.parse.parser.name == "payslip_basic"
    assert parsed.parse.payslip is not None
    return parsed.parse.payslip


def _hand_view(lines: list[ViewLine]) -> DocumentView:
    return DocumentView(lines=tuple(lines), source_format="image", truncated=False, page_count=1)


def _year_to_date_figures() -> set[Decimal]:
    return {
        payslips.SUMMARY_GROSS[1],
        payslips.SUMMARY_TOTAL[1],
        payslips.SUMMARY_NET[1],
        *(ytd for _, _, ytd in payslips.SUMMARY_DEDUCTIONS),
    }


def _assert_summary_layout(payslip: Payslip) -> None:
    assert payslip.employer == "Initech Example Ltd"
    assert (payslip.period_start, payslip.period_end) == (date(2026, 2, 1), date(2026, 2, 15))
    assert payslip.pay_date == date(2026, 2, 20)
    assert payslip.gross == Decimal("2400.00")
    assert payslip.total_deductions == Decimal("591.60")
    assert payslip.net == Decimal("1808.40")
    assert [(d.label, d.amount) for d in payslip.deductions] == [
        ("Federal tax", Decimal("288.00")),
        ("Social insurance", Decimal("183.60")),
        ("Retirement plan", Decimal("120.00")),
    ]
    assert [(e.label, e.amount) for e in payslip.earnings] == [("Regular", Decimal("2400.00"))]
    assert payslip.adds_up is True
    assert payslip.low_confidence == []
    read = {payslip.gross, payslip.total_deductions, payslip.net}
    read |= {line.amount for line in (*payslip.deductions, *payslip.earnings)}
    assert not read & _year_to_date_figures()


def test_summary_layout_reads_the_current_column() -> None:
    _assert_summary_layout(_parse(payslips.summary_columns_pdf()))


def test_a_heading_is_never_the_employer() -> None:
    payslip = _parse(payslips.summary_columns_pdf())
    assert payslip.employer is not None
    assert "PAY PERIOD" not in payslip.employer.upper()
    assert "EMPLOYER" not in payslip.employer.upper()


def test_ytd_first_columns_are_still_read_by_header() -> None:
    _assert_summary_layout(_parse(payslips.summary_columns_pdf(ytd_first=True)))


def test_deductions_are_summed_when_no_total_is_printed() -> None:
    payslip = _parse(payslips.this_period_pdf())
    assert payslip.employer == "Initech Example Ltd"
    assert (payslip.period_start, payslip.period_end) == (date(2026, 3, 1), date(2026, 3, 15))
    assert (payslip.gross, payslip.net) == (Decimal("1750.00"), Decimal("1425.75"))
    assert [(d.label, d.amount) for d in payslip.deductions] == [
        (label, current) for label, _, current, _ in payslips.PERIOD_DEDUCTIONS
    ]
    assert payslip.total_deductions == Decimal("324.25")
    assert payslip.adds_up is True
    assert payslip.low_confidence == []


def test_a_sentence_total_is_the_total_and_no_deduction_line() -> None:
    payslip = _parse(payslips.this_period_pdf(sentence=True))
    assert len(payslip.deductions) == len(payslips.PERIOD_DEDUCTIONS)
    assert payslip.total_deductions == Decimal("324.25")
    assert payslip.adds_up is True
    assert payslip.low_confidence == []


def test_labels_over_values() -> None:
    payslip = _payslip(_hand_view(payslips.labels_over_values_lines()))
    assert (payslip.gross, payslip.total_deductions, payslip.net) == (
        Decimal("3100.00"),
        Decimal("930.00"),
        Decimal("2170.00"),
    )
    assert payslip.adds_up is True
    assert [payslip.confidence[name] for name in ("gross", "deductions", "net")] == [1.0, 1.0, 1.0]
    assert payslip.deductions == []


def test_existing_payslip_reads_as_before() -> None:
    payslip = _parse(statements.payslip_pdf())
    assert payslip.employer == "Initech Example Ltd"
    assert (payslip.period_start, payslip.period_end) == (date(2026, 1, 1), date(2026, 1, 31))
    assert payslip.pay_date == date(2026, 1, 30)
    assert (payslip.gross, payslip.net) == (Decimal("4250.00"), Decimal("2847.50"))
    assert payslip.total_deductions == Decimal("1402.50")
    assert [(d.label, d.amount) for d in payslip.deductions] == list(statements.DEDUCTIONS)
    assert [(e.label, e.amount) for e in payslip.earnings] == list(statements.EARNINGS)
    assert payslip.adds_up is True
    assert payslip.low_confidence == []


# Negative fixtures: what the wider rules must still leave alone.


def test_a_year_to_date_label_is_never_the_current_figure() -> None:
    lines = [
        _line(0, "Initech Example Ltd"),
        _line(1, "Pay Stub"),
        _line(2, "Gross pay", "2,000.00"),
        _line(3, "YTD gross pay", "9,000.00"),
        _line(4, "Net pay", "1,700.00"),
        _line(5, "Net pay year to date", "7,650.00"),
    ]
    payslip = _payslip(_hand_view(lines))
    assert (payslip.gross, payslip.net) == (Decimal("2000.00"), Decimal("1700.00"))


def test_two_payslip_labels_do_not_make_a_payslip() -> None:
    lines = [
        _line(0, "Sample Savings Society"),
        _line(1, "Statement period ending 02/15/2026"),
        _line(2, "Total earnings", "12.40"),
    ]
    assert PayslipParser().detect(_hand_view(lines)) == 0.0
    lines.append(_line(3, "Pay date: 02/20/2026"))
    assert PayslipParser().detect(_hand_view(lines)) > 0.0


def test_a_total_line_in_a_section_is_no_line_item() -> None:
    lines = [
        _line(0, "Initech Example Ltd"),
        _line(1, "Pay Stub"),
        _line(2, "Gross pay", "2,000.00"),
        _line(3, "Deductions"),
        _line(4, "Income tax", "250.00"),
        _line(5, "Union dues", "50.00"),
        _line(6, "Total", "300.00"),
        _line(7, "Net pay", "1,700.00"),
    ]
    payslip = _payslip(_hand_view(lines))
    assert [d.label for d in payslip.deductions] == ["Income tax", "Union dues"]
    assert payslip.total_deductions == Decimal("300.00")
    assert payslip.adds_up is True


def test_a_period_end_alone_is_no_period() -> None:
    lines = [
        _line(0, "Initech Example Ltd"),
        _line(1, "Pay Stub"),
        _line(2, "Period Ending:", "02/15/2026"),
        _line(3, "Gross pay", "2,000.00"),
        _line(4, "Net pay", "2,000.00"),
    ]
    payslip = _payslip(_hand_view(lines))
    assert (payslip.period_start, payslip.period_end) == (None, None)
    assert "period" in payslip.low_confidence


def test_an_employer_label_with_its_name_beside_it_is_still_read() -> None:
    lines = [
        _line(0, "Pay Stub"),
        _line(1, "Employer", "Initech Example Ltd"),
        _line(2, "Gross pay", "2,000.00"),
        _line(3, "Net pay", "2,000.00"),
    ]
    assert _payslip(_hand_view(lines)).employer == "Initech Example Ltd"


def test_a_column_with_only_a_year_to_date_figure_is_no_deduction() -> None:
    def row(index: int, *cells: tuple[str, float]) -> ViewLine:
        y = 40.0 + 20.0 * index
        built = tuple(Cell(text, x, y, x + 6.0 * len(text), y + 10.0, None) for text, x in cells)
        return ViewLine(
            page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=10.0
        )

    lines = [
        row(0, ("Initech Example Ltd", 40.0)),
        row(1, ("Pay Stub", 40.0)),
        row(2, ("Gross pay", 40.0), ("2,000.00", 300.0)),
        row(3, ("Deductions", 40.0), ("Current", 300.0), ("YTD", 420.0)),
        row(4, ("Income tax", 40.0), ("300.00", 300.0), ("1,200.00", 420.0)),
        row(5, ("Relocation loan", 40.0), ("450.00", 420.0)),
        row(6, ("Net pay", 40.0), ("1,700.00", 300.0)),
    ]
    payslip = _payslip(_hand_view(lines))
    assert [(d.label, d.amount) for d in payslip.deductions] == [("Income tax", Decimal("300.00"))]
    assert payslip.total_deductions == Decimal("300.00")
    assert payslip.adds_up is True


# labelled_cells on its own


def test_label_pairs_split_a_label_and_its_amount_in_one_cell() -> None:
    (pair,) = label_pairs(_line(0, "Net Pay 1,808.40"))
    assert (pair.label.text, [a.text for a in pair.amounts]) == ("Net Pay", ["1,808.40"])
    assert pair.amounts[0].x0 > pair.label.x1 - 1.0
    first, second = label_pairs(_line(0, "Federal tax", "288.00", "1,152.00", "Type"))
    assert (first.label.text, [a.text for a in first.amounts]) == (
        "Federal tax",
        ["288.00", "1,152.00"],
    )
    assert (second.label.text, second.amounts) == ("Type", ())
    (dashed,) = label_pairs(_line(0, "Net Pay - $1,840.25"))
    assert [a.text for a in dashed.amounts] == ["- $1,840.25"]
    (plain,) = label_pairs(_line(0, "Subtotal 900.00 - 150.00"))
    assert [amount_of(a) for a in plain.amounts] == [
        read_amount("900.00"),
        read_amount("150.00"),
    ]
    assert label_pairs(_line(0, "2,000.00", "300.00")) == []


def test_period_columns_need_a_year_to_date_or_two_headers() -> None:
    assert period_columns(_line(0, "Description", "Amount")) == []
    assert period_columns(_line(0, "Current")) == []
    kinds = [c.kind for c in period_columns(_line(0, "Pay", "This Period", "YTD"))]
    assert kinds == ["current", "ytd"]
    kinds = [c.kind for c in period_columns(_line(0, "Paid Since 02/20/2026", "Paid Year to Date"))]
    assert kinds == ["current", "ytd"]
    merged = period_columns(_line(0, "Deductions", "Current YTD"))
    assert [c.kind for c in merged] == ["current", "ytd"]
    assert merged[0].x1 <= merged[1].x0
    assert [c.kind for c in period_columns(_line(0, "Item", "Year-to-date"))] == ["ytd"]


def test_current_cells() -> None:
    header = _line(0, "Item", "Current", "YTD")
    columns = period_columns(header)
    row = _line(1, "Income tax", "300.00", "1,200.00")
    assert [c.text for c in current_cells(row.cells[1:], columns)] == ["300.00"]
    # Only a year-to-date figure: nothing for the current column.
    only = _line(1, "Loan", "", "450.00")
    assert current_cells(only.cells[2:], columns) == ()
    # No current column named: what is not under the year-to-date one.
    ytd_only = period_columns(_line(0, "Item", "Paid", "Year to date"))
    assert [c.text for c in current_cells(row.cells[1:], ytd_only)] == ["300.00"]
    # No columns: every amount.
    assert current_cells(row.cells[1:], []) == row.cells[1:]
    # Amounts printed off their columns are matched in the header's order.
    far = tuple(
        Cell(text, x, 60.0, x + 40.0, 70.0) for text, x in (("300.00", 900.0), ("1,200.00", 960.0))
    )
    assert [c.text for c in current_cells(far, columns)] == ["300.00"]
    swapped = period_columns(_line(0, "Item", "YTD", "Current"))
    assert [c.text for c in current_cells(far, swapped)] == ["1,200.00"]


def test_value_under_and_over() -> None:
    lines = [
        _line(0, "02/15/2026", "Sample"),
        _line(1, "Period ending", "Net pay"),
        _line(2, "02/15/2026", "$2,170.00"),
        _line(3, "Thank you", "for your work"),
    ]
    label, other = lines[1].cells
    under = value_under(lines, 1, other)
    assert under is not None
    assert under.text == "$2,170.00"
    over = value_over(lines, 1, label)
    assert over is not None
    assert over.text == "02/15/2026"
    # Words are no value, unless the caller asks for text (a name under a heading).
    assert value_over(lines, 1, other) is None
    assert value_under(lines, 2, label) is None
    named = value_under(lines, 2, label, text=True)
    assert named is not None
    assert named.text == "Thank you"
    assert value_under(lines, 3, label) is None
    other_page = [lines[1], ViewLine(2, 2, lines[2].cells, lines[2].text, 10.0)]
    assert value_under(other_page, 0, other) is None


# Review findings: year-to-date figures under, after or beside the period's.


def _row(index: int, *cells: tuple[str, float]) -> ViewLine:
    y = 40.0 + 20.0 * index
    built = tuple(Cell(text, x, y, x + 6.0 * len(text), y + 10.0, None) for text, x in cells)
    return ViewLine(
        page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=10.0
    )


_OVER = (("Gross pay", 140.0), ("Total deductions", 260.0), ("Net pay", 420.0))
_CURRENT_ROW = (("Current", 40.0), ("$2,400.00", 140.0), ("$591.60", 260.0), ("$1,808.40", 420.0))
_YTD_ROW = (("YTD", 40.0), ("$9,600.00", 140.0), ("$2,366.40", 260.0), ("$7,233.60", 420.0))


@pytest.mark.parametrize("ytd_first", [False, True])
def test_labels_over_a_current_and_a_year_to_date_row(ytd_first: bool) -> None:
    rows = (_YTD_ROW, _CURRENT_ROW) if ytd_first else (_CURRENT_ROW, _YTD_ROW)
    lines = [
        _row(0, ("Initech Example Ltd", 40.0)),
        _row(1, ("Pay Stub", 40.0)),
        _row(2, *_OVER),
        _row(3, *rows[0]),
        _row(4, *rows[1]),
    ]
    payslip = _payslip(_hand_view(lines))
    assert (payslip.gross, payslip.total_deductions, payslip.net) == (
        Decimal("2400.00"),
        Decimal("591.60"),
        Decimal("1808.40"),
    )
    assert payslip.adds_up is True


def test_labels_over_a_year_to_date_row_alone_are_not_read() -> None:
    lines = [
        _row(0, ("Initech Example Ltd", 40.0)),
        _row(1, ("Pay Stub", 40.0)),
        _row(2, *_OVER),
        _row(3, *_YTD_ROW),
        # Not the period's row: nothing says what these figures are.
        _row(4, ("Hours", 40.0), ("80.00", 140.0), ("0.00", 260.0), ("80.00", 420.0)),
    ]
    outcome = PayslipParser().parse(_hand_view(lines), threshold=conf.DEFAULT_THRESHOLD)
    assert outcome.payslip is None
    assert outcome.warnings == ["no_payslip_figures"]


def test_a_year_to_date_block_after_the_summary_is_not_read() -> None:
    lines = [
        _row(0, ("Initech Example Ltd", 40.0)),
        _row(1, ("Pay Stub", 40.0)),
        _row(2, ("Gross Pay", 40.0), ("2,400.00", 200.0)),
        _row(3, ("Total Deductions", 40.0), ("591.60", 200.0)),
        _row(4, ("Net Pay", 40.0), ("1,808.40", 200.0)),
        _row(5, ("Year to Date", 40.0)),
        _row(6, ("Gross Pay", 40.0), ("9,600.00", 200.0)),
        _row(7, ("Total Deductions", 40.0), ("2,366.40", 200.0)),
        _row(8, ("Net Pay", 40.0), ("7,233.60", 200.0)),
    ]
    payslip = _payslip(_hand_view(lines))
    assert (payslip.gross, payslip.total_deductions, payslip.net) == (
        Decimal("2400.00"),
        Decimal("591.60"),
        Decimal("1808.40"),
    )
    assert payslip.adds_up is True
    # The year-to-date block alone gives nothing.
    alone = PayslipParser().parse(
        _hand_view([*lines[:2], *lines[5:]]), threshold=conf.DEFAULT_THRESHOLD
    )
    assert alone.payslip is None


def test_the_first_reading_of_a_figure_stands() -> None:
    lines = [
        _row(0, ("Initech Example Ltd", 40.0)),
        _row(1, ("Pay Stub", 40.0)),
        _row(2, ("Gross Pay", 40.0), ("2,400.00", 200.0)),
        _row(3, ("Net Pay", 40.0), ("1,808.40", 200.0)),
        _row(4, ("Summary of the year", 40.0)),
        _row(5, ("Gross Pay", 40.0), ("9,600.00", 200.0)),
        _row(6, ("Net Pay", 40.0), ("7,233.60", 200.0)),
    ]
    payslip = _payslip(_hand_view(lines))
    assert (payslip.gross, payslip.net) == (Decimal("2400.00"), Decimal("1808.40"))


def test_a_rate_is_not_the_current_earnings_when_the_current_cell_is_blank() -> None:
    lines = [
        _row(0, ("Initech Example Ltd", 40.0)),
        _row(1, ("Pay Stub", 40.0)),
        _row(
            2,
            ("Earnings", 40.0),
            ("Hours", 160.0),
            ("Rate", 230.0),
            ("Current", 300.0),
            ("YTD", 400.0),
        ),
        _row(
            3,
            ("Regular", 40.0),
            ("80.00", 160.0),
            ("30.00", 230.0),
            ("2,400.00", 300.0),
            ("9,600.00", 400.0),
        ),
        _row(4, ("Overtime", 40.0), ("0.00", 160.0), ("45.00", 230.0), ("900.00", 400.0)),
        _row(5, ("Gross Pay", 40.0), ("2,400.00", 300.0), ("10,500.00", 400.0)),
        _row(6, ("Net Pay", 40.0), ("2,400.00", 300.0), ("10,500.00", 400.0)),
    ]
    payslip = _payslip(_hand_view(lines))
    assert [(e.label, e.amount) for e in payslip.earnings] == [("Regular", Decimal("2400.00"))]
    assert payslip.gross == Decimal("2400.00")


def test_a_heading_starting_with_employer_is_no_employer() -> None:
    lines = [
        _row(0, ("Initech Example Ltd", 40.0)),
        _row(1, ("Earnings Statement", 40.0)),
        _row(2, ("Gross Pay", 40.0), ("2,400.00", 300.0)),
        _row(3, ("Employer Paid Benefits", 40.0)),
        _row(4, ("Net Pay", 40.0), ("2,400.00", 300.0)),
    ]
    assert _payslip(_hand_view(lines)).employer == "Initech Example Ltd"
    lines[3] = _row(3, ("Employer: Sample Works Co", 40.0))
    assert _payslip(_hand_view(lines)).employer == "Sample Works Co"
