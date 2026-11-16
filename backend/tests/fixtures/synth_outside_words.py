# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statements with words that are no part of their rows printed beside or over them.

Each builder draws one PDF as :mod:`tests.fixtures.synth_bank_layouts` does;
every institution, payee, number and figure is invented. Each prints a
masked account number, a labelled period, an opening and a closing balance
and rows that reconcile:

* :func:`notice_up_the_margin`: ``Date | Description | Paid out | Paid in |
  Balance`` with a running balance, and a notice printed up the left margin
  of the page, one word above the other (``Regulated``, ``by``, ...). The
  words stand well left of the table; three of them stand beside a row and
  the others beside the lines between and below the rows.
* :func:`flag_left_of_rows`: the same table without the notice; two of its
  rows have a word (:data:`FLAG_WORD`) printed left of the table, where the
  notice stands in the other, and no other line of the page has one. Those
  two rows are not read: a word printed beside rows only may say something
  about them (:data:`FLAGGED_ROWS`).
* :func:`watermark_over_dates`: ``Post Date | Trans Date | Description |
  Amount`` and a large slanted watermark word whose box lies over both dates
  of one row (:data:`WATERMARKED_ROW`); the second page prints a note and
  the watermark at the same place.
* :func:`stamp_over_dates`: its first page alone. A word printed once over
  a row may be a stamp on that row: the row is not read.

:data:`EXPECTED_ROWS` gives, per builder, the rows a correct reading returns
(``(posted, amount, payee)``, money out negative).
"""

from __future__ import annotations

import io
from datetime import date
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from tests.fixtures.synth_bank_layouts import (
    _FOOTER,
    ExpectedRow,
    _d,
    _head,
    _money,
    _running,
)
from tests.fixtures.synth_unseen_layouts import _line

_MARGIN_OPENING: Final = _d("4,180.35")
_MARGIN_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 3, 2), _d("-58.40"), "Card Purchase Yarrowdene Grocer"),
    (date(2026, 3, 4), _d("2,150.00"), "Payroll Deposit Sedgewick Looms"),
    (date(2026, 3, 9), _d("-36.29"), "Card Purchase Hollin Row Outfitters"),
    (date(2026, 3, 12), _d("-940.00"), "Rent Plumtree Court"),
    (date(2026, 3, 17), _d("-22.75"), "Card Purchase Quillfeather Books"),
    (date(2026, 3, 20), _d("120.00"), "Mobile Deposit"),
    (date(2026, 3, 25), _d("-74.10"), "Larkspur Water Bill Pay"),
    (date(2026, 3, 30), _d("-15.00"), "Monthly Service Fee"),
)
#: Where the table starts and how far apart its rows are.
_TABLE_Y: Final = 640.0
_ROW_STEP: Final = 26.0
#: The notice: each word and the y its first letter is printed at, read up the page.
_NOTICE_X: Final = 16.0
_NOTICE_SIZE: Final = 6
#: Words printed beside a row, by the row's number: the first of them is long
#: enough to be taken for a cell of the date column.
_NOTICE_BESIDE_ROWS: Final = {2: "licensed", 4: "Reg.", 6: "Limited"}
#: Words printed between and below the rows.
_NOTICE_ELSEWHERE: Final = ("Sorrelbank", "Deposits", "No.", "0000000", "by", "the", "Board")

#: The word printed left of two rows of :func:`flag_left_of_rows`, and those rows.
FLAG_WORD: Final = "licensed"
_FLAGGED: Final = (2, 6)
FLAGGED_ROWS: Final = tuple(_MARGIN_ROWS[index] for index in _FLAGGED)


def _upright(c: canvas.Canvas, x: float, y: float, text: str, size: float) -> None:
    """``text`` printed up the page from ``(x, y)``."""
    c.saveState()
    c.translate(x, y)
    c.rotate(90)
    c.setFont("Helvetica", size)
    c.drawString(0, 0, text)
    c.restoreState()


def _margin_table(c: canvas.Canvas) -> None:
    balances = _running(_MARGIN_OPENING, _MARGIN_ROWS)
    _head(
        c,
        "Sorrelbank Deposits",
        "2907",
        "03/01/2026 - 03/31/2026",
        _MARGIN_OPENING,
        balances[-1],
    )
    _line(
        c,
        _TABLE_Y,
        [
            (40, "Date"),
            (110, "Description"),
            (-400, "Paid out"),
            (-470, "Paid in"),
            (-550, "Balance"),
        ],
        size=8,
    )
    for index, ((posted, amount, payee), balance) in enumerate(
        zip(_MARGIN_ROWS, balances, strict=True)
    ):
        _line(
            c,
            _TABLE_Y - _ROW_STEP * (index + 1),
            [
                (40, posted.strftime("%m/%d/%Y")),
                (110, payee),
                (-400 if amount < 0 else -470, _money(amount)),
                (-550, _money(balance)),
            ],
        )


def notice_up_the_margin() -> bytes:
    """Eight rows; a notice printed up the left margin beside three of them."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _margin_table(c)
    for index, word in _NOTICE_BESIDE_ROWS.items():
        # Its middle beside the row's line.
        y = _TABLE_Y - _ROW_STEP * (index + 1) + 3
        width = c.stringWidth(word, "Helvetica", _NOTICE_SIZE)
        _upright(c, _NOTICE_X, y - width / 2, word, _NOTICE_SIZE)
    # Between the rows, and under the table.
    between = _TABLE_Y - _ROW_STEP * 1.5
    for position, word in enumerate(_NOTICE_ELSEWHERE):
        if position < 2:
            y = between - _ROW_STEP * 2 * position
        else:
            y = _TABLE_Y - _ROW_STEP * (len(_MARGIN_ROWS) + 1) - 30 * (position - 1)
        _upright(c, _NOTICE_X, y, word, _NOTICE_SIZE)
    c.save()
    return buf.getvalue()


def flag_left_of_rows() -> bytes:
    """The same eight rows; a word printed left of two of them and nowhere else."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _margin_table(c)
    for index in _FLAGGED:
        y = _TABLE_Y - _ROW_STEP * (index + 1) + 3
        width = c.stringWidth(FLAG_WORD, "Helvetica", _NOTICE_SIZE)
        _upright(c, _NOTICE_X, y - width / 2, FLAG_WORD, _NOTICE_SIZE)
    c.save()
    return buf.getvalue()


# -- a watermark over the dates of a row -------------------------------------------

_WATERMARK_OPENING: Final = _d("1,260.90")
_WATERMARK_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 5, 4), _d("-31.56"), "FENWORTHY CYCLES"),
    (date(2026, 5, 8), _d("-124.43"), "OTTERBURN MARKET"),
    (date(2026, 5, 13), _d("640.00"), "DEPOSIT BRANCH 7"),
    (date(2026, 5, 19), _d("-42.22"), "CLOVERLEY DRAPERS"),
    (date(2026, 5, 22), _d("-9.08"), "PIPIT LANE COFFEE"),
    (date(2026, 5, 27), _d("-65.49"), "ASHGROVE CHEMIST"),
)
#: The row whose two dates the watermark's box lies over.
_WATERMARKED: Final = 3
WATERMARKED_ROW: Final = _WATERMARK_ROWS[_WATERMARKED]
WATERMARK_WORD: Final = "SPECIMEN"
_WATERMARK_STEP: Final = 30.0
_WATERMARK_Y: Final = 640.0


def _watermark(c: canvas.Canvas) -> None:
    """A pale word slanted up the page: its box starts left of the table and
    reaches over both dates of one row and nothing else of the table."""
    row_y = _WATERMARK_Y - _WATERMARK_STEP * (_WATERMARKED + 1)
    c.saveState()
    c.setFillGray(0.85)
    c.translate(33, row_y - 14)
    c.rotate(12)
    c.setFont("Helvetica-Bold", 30)
    c.drawString(0, 0, WATERMARK_WORD)
    c.restoreState()


def stamp_over_dates() -> bytes:
    """As :func:`watermark_over_dates` on one page: the word is printed once."""
    return _watermarked(pages=1)


def watermark_over_dates() -> bytes:
    """Six rows with a posting and a transaction date; a watermark over one
    row's dates, printed on the second page as well."""
    return _watermarked(pages=2)


def _watermarked(*, pages: int) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_WATERMARK_OPENING, _WATERMARK_ROWS)[-1]
    _head(
        c,
        "Hawksmoor Vale Bank",
        "5514",
        "05/01/2026 - 05/31/2026",
        _WATERMARK_OPENING,
        closing,
    )
    _line(
        c,
        _WATERMARK_Y,
        [(40, "Post Date"), (133, "Trans Date"), (226, "Description"), (-520, "Amount")],
        size=8,
    )
    for index, (posted, amount, payee) in enumerate(_WATERMARK_ROWS):
        y = _WATERMARK_Y - _WATERMARK_STEP * (index + 1)
        _line(
            c,
            y,
            [
                (40, posted.replace(day=posted.day + 1).strftime("%m/%d/%Y")),
                (133, posted.strftime("%m/%d/%Y")),
                (226, payee),
                (-520, f"-{_money(amount)}" if amount < 0 else _money(amount)),
            ],
        )
    _watermark(c)
    if pages > 1:
        c.showPage()
        # The second page: a note, and the watermark at the same place.
        _line(c, 750, [(40, "Hawksmoor Vale Bank")], size=14, bold=True)
        _line(c, 700, [(40, "Please tell us within sixty days if a line here is wrong.")])
        _line(c, 60, [(40, _FOOTER)], size=7)
        _watermark(c)
    c.save()
    return buf.getvalue()


EXPECTED_ROWS: Final[dict[str, list[ExpectedRow]]] = {
    "notice_up_the_margin": list(_MARGIN_ROWS),
    "flag_left_of_rows": [row for row in _MARGIN_ROWS if row not in FLAGGED_ROWS],
    "watermark_over_dates": list(_WATERMARK_ROWS),
    "stamp_over_dates": [row for row in _WATERMARK_ROWS if row != WATERMARKED_ROW],
}
