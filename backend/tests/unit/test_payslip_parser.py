# SPDX-License-Identifier: AGPL-3.0-or-later
"""The payslip parser on views built by hand, and the per-field OCR scaling.

A field's confidence follows the cells it was read from
(``confidence.scaled_fields``): a garbled word elsewhere on the page says
nothing about a figure read from clean cells, and figures that add up were
read right whatever Tesseract thought of them.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine
from app.ingestion.parsers.payslip import PayslipParser
from app.ingestion.sandbox.protocol import Payslip

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
