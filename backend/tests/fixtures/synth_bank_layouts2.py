# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bank statements, round two: layouts the first round read wrongly.

Each builder draws one PDF as :mod:`tests.fixtures.synth_bank_layouts` does;
every institution, payee, number and figure is invented. Each prints a
masked account number, a labelled period, an opening and a closing balance
and rows that reconcile:

* :func:`marker_column_checking`: ``Date | Description | Reference | Amount |
  DC | Balance``: every amount is printed without a sign and the column
  ``DC`` says which way it went (``DB`` on five rows, ``CR`` on three); a
  running balance on every row.

:data:`EXPECTED_ROWS` gives, per builder, the rows a correct reading returns
(``(posted, amount, payee)``, money out negative).
"""

from __future__ import annotations

import io
from datetime import date
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from tests.fixtures.synth_bank_layouts import ExpectedRow, _d, _head, _money, _running
from tests.fixtures.synth_unseen_layouts import _line, _rows

# -- a credit or debit marker in a column of its own ----------------------------

_MARKER_OPENING: Final = _d("3,264.80")
#: ``(row, reference)``: the reference is printed between description and amount.
_MARKER_ROWS: Final[tuple[tuple[ExpectedRow, str], ...]] = (
    ((date(2026, 8, 3), _d("-412.60"), "Card Purchase Thistledown Garage"), "7719"),
    ((date(2026, 8, 5), _d("1,930.00"), "Payroll Deposit Brackenhurst Mills"), "4402"),
    ((date(2026, 8, 7), _d("-58.25"), "Card Purchase Oatfield Bakery"), "7720"),
    ((date(2026, 8, 12), _d("-1,180.00"), "Rent Ploverfield Mews"), "3051"),
    ((date(2026, 8, 14), _d("75.00"), "Mobile Deposit"), "4403"),
    ((date(2026, 8, 19), _d("-96.40"), "Wrenlow Water Bill Pay"), "3052"),
    ((date(2026, 8, 25), _d("-240.00"), "ATM Withdrawal Saltmarsh Row"), "7721"),
    ((date(2026, 8, 28), _d("0.52"), "Interest Paid"), "4404"),
)


def marker_column_checking() -> bytes:
    """Eight unsigned amounts; ``DB`` or ``CR`` in the column beside them."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    rows = [row for row, _ in _MARKER_ROWS]
    balances = _running(_MARKER_OPENING, rows)
    _head(
        c,
        "Kestrelmoor Savings Bank",
        "6183",
        "08/01/2026 - 08/31/2026",
        _MARKER_OPENING,
        balances[-1],
    )
    _line(
        c,
        650,
        [
            (40, "Date"),
            (105, "Description"),
            (310, "Reference"),
            (-430, "Amount"),
            (445, "DC"),
            (-550, "Balance"),
        ],
        size=8,
    )
    lines: list[list[tuple[float, str]]] = []
    for ((posted, amount, payee), reference), balance in zip(_MARKER_ROWS, balances, strict=True):
        lines.append(
            [
                (40, posted.strftime("%m/%d/%Y")),
                (105, payee),
                (310, reference),
                (-430, _money(amount)),
                (445, "DB" if amount < 0 else "CR"),
                (-550, _money(balance)),
            ]
        )
    _rows(c, 636, lines)
    c.save()
    return buf.getvalue()


EXPECTED_ROWS: Final[dict[str, list[ExpectedRow]]] = {
    "marker_column_checking": [row for row, _ in _MARKER_ROWS],
}
