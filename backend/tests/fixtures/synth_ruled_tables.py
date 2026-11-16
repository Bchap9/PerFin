# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statements printed line-printer style: tables two across with a rule between.

Each builder draws one PDF as :mod:`tests.fixtures.synth_bank_layouts` does;
every institution, payee, number and figure is invented. Each prints a
masked account number, a labelled period, an opening and a closing balance
and rows that reconcile:

* :func:`two_across_with_rules`: two sections, each under a title set in
  dashes (``------ DEPOSITS/CREDITS ------``) and a header ``DATE |
  DESCRIPTION | AMOUNT`` printed twice side by side. A vertical bar stands
  between the two halves on the header line, on a line of its own under it
  and on every row line, also where the right half is empty. Amounts are
  printed without a sign; the titles say which way they went.
* :func:`two_across_left_half_only`: the same statement with every entry on
  a line of its own in the left half, the bar after it and nothing right
  of the bar.

:data:`EXPECTED_ROWS` gives, per builder, the rows a correct reading returns
(``(posted, amount, payee)``, money out negative): each line left to right.
"""

from __future__ import annotations

import io
from datetime import date
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from tests.fixtures.synth_bank_layouts import ExpectedRow, _d, _head, _money, _running
from tests.fixtures.synth_unseen_layouts import _line

#: The lines of a section: the left entry and, where printed, the right one.
Lines = tuple[tuple[ExpectedRow, ...], ...]

_OPENING: Final = _d("1,840.20")
_DEPOSIT_LINES: Final[Lines] = (
    ((date(2026, 2, 2), _d("700.00"), "TELLER LODGEMENT"),),
    (
        (date(2026, 2, 9), _d("1,250.40"), "SALARY HOLLOWAY"),
        (date(2026, 2, 17), _d("86.15"), "REFUND FENWICK"),
    ),
)
_WITHDRAWAL_LINES: Final[Lines] = (
    (
        (date(2026, 2, 3), _d("-412.75"), "RENT ORCHARD ROW"),
        (date(2026, 2, 11), _d("-58.30"), "WATER BOARD"),
    ),
    ((date(2026, 2, 24), _d("-125.00"), "CASH MACHINE 0412"),),
)
#: The row printed alone on its line with the bar after it.
ALONE_BESIDE_A_RULE: Final = _DEPOSIT_LINES[0][0]

_RULE_X: Final = 294.0
#: Where the date, the description and the right edge of the amount of each half stand.
_HALVES: Final = ((40.0, 110.0, 270.0), (308.0, 378.0, 560.0))


def _section(c: canvas.Canvas, y: float, title: str, lines: Lines) -> float:
    """The title, the header twice with the bar between, a bar alone, the rows."""
    _line(c, y, [(40, f"{'-' * 30} {title} {'-' * 30}")])
    y -= 18
    header: list[tuple[float, str]] = [(_RULE_X, "|")]
    for date_x, text_x, amount_x in _HALVES:
        header += [(date_x, "DATE"), (text_x, "DESCRIPTION"), (-amount_x, "AMOUNT")]
    _line(c, y, header)
    y -= 11
    _line(c, y, [(_RULE_X, "|")])
    y -= 11
    for entries in lines:
        cells: list[tuple[float, str]] = [(_RULE_X, "|")]
        for (posted, amount, payee), (date_x, text_x, amount_x) in zip(
            entries, _HALVES, strict=False
        ):
            cells += [
                (date_x, posted.strftime("%m/%d/%Y")),
                (text_x, payee),
                (-amount_x, _money(amount)),
            ]
        _line(c, y, cells)
        y -= 11
    return y - 14


def _statement(deposits: Lines, withdrawals: Lines) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    rows = [row for lines in (deposits, withdrawals) for entries in lines for row in entries]
    closing = _running(_OPENING, rows)[-1]
    _head(c, "Nettlecombe Commerce Bank", "4471", "02/01/2026 - 02/28/2026", _OPENING, closing)
    y = _section(c, 650, "DEPOSITS/CREDITS", deposits)
    _section(c, y, "WITHDRAWALS/DEBITS", withdrawals)
    c.save()
    return buf.getvalue()


def two_across_with_rules() -> bytes:
    """Three deposits and three withdrawals, two across, a bar between the halves."""
    return _statement(_DEPOSIT_LINES, _WITHDRAWAL_LINES)


def two_across_left_half_only() -> bytes:
    """The same header; every entry stands alone on its line, in the left half."""
    return _statement(_alone(_DEPOSIT_LINES), _alone(_WITHDRAWAL_LINES))


def _alone(lines: Lines) -> Lines:
    return tuple((row,) for entries in lines for row in entries)


_ROWS: Final = [
    row for lines in (_DEPOSIT_LINES, _WITHDRAWAL_LINES) for entries in lines for row in entries
]
EXPECTED_ROWS: Final[dict[str, list[ExpectedRow]]] = {
    "two_across_with_rules": _ROWS,
    "two_across_left_half_only": _ROWS,
}
