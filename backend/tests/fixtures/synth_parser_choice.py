# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pages on the border between a statement and a receipt (parser choice).

Two one-page PDFs that print the same table header, ``Date | Description |
Amount``, and the same closing lines (subtotal, tax, total). Every name and
figure is invented:

* :func:`activity_page_with_totals`: a page of account activity, six dated
  rows, then ``Subtotal``, ``Tax`` and ``Total`` lines and "Thank you". It
  prints none of the phrases the receipt parser takes for a statement
  (``receipt._STATEMENT_WORDS``: no period label, no opening or closing
  balance), which is what this fixture is about: only its table says what
  it is.
* :func:`receipt_with_item_table`: a shop's receipt whose items sit under
  the same header, with a subtotal, tax, a total and a card tender line.

:data:`ACTIVITY_ROWS`, :data:`RECEIPT_ITEMS` and the totals give what a
correct reading returns.
"""

from __future__ import annotations

import io
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from tests.fixtures.synth_unseen_layouts import _line, _rows

FOOTER: Final = "Synthetic test document. Not a real account or purchase."

#: ``(date, description, amount)`` of the activity page.
ACTIVITY_ROWS: Final[tuple[tuple[str, str, str], ...]] = (
    ("04/02/2026", "Parcel locker rental", "18.00"),
    ("04/06/2026", "Bicycle storage, bay 12", "27.50"),
    ("04/11/2026", "Guest parking permit", "9.25"),
    ("04/15/2026", "Laundry room top-up", "40.00"),
    ("04/21/2026", "Key fob replacement", "15.75"),
    ("04/27/2026", "Meeting room booking", "32.00"),
)
ACTIVITY_SUBTOTAL: Final = Decimal("142.50")
ACTIVITY_TAX: Final = Decimal("11.40")
ACTIVITY_TOTAL: Final = Decimal("153.90")

#: ``(date, description, amount)`` of the receipt's items.
RECEIPT_ITEMS: Final[tuple[tuple[str, str, str], ...]] = (
    ("05/09/2026", "Potting soil 20 L", "12.40"),
    ("05/09/2026", "Terracotta pot", "8.95"),
    ("05/09/2026", "Tomato seedlings", "6.60"),
)
RECEIPT_SUBTOTAL: Final = Decimal("27.95")
RECEIPT_TAX: Final = Decimal("2.24")
RECEIPT_TOTAL: Final = Decimal("30.19")
RECEIPT_LAST4: Final = "6038"

_HEADER: Final = ((54, "Date"), (130, "Description"), (-540, "Amount"))


def _table(c: canvas.Canvas, y: float, rows: tuple[tuple[str, str, str], ...]) -> float:
    _line(c, y, _HEADER, bold=True)
    return _rows(
        c, y - 15, [((54, day), (130, text), (-540, amount)) for day, text, amount in rows]
    )


def _totals(c: canvas.Canvas, y: float, subtotal: Decimal, tax: Decimal, total: Decimal) -> float:
    return _rows(
        c,
        y - 8,
        [
            ((380, "Subtotal"), (-540, f"{subtotal:,.2f}")),
            ((380, "Tax"), (-540, f"{tax:,.2f}")),
            ((380, "Total"), (-540, f"{total:,.2f}")),
        ],
    )


def _finish(c: canvas.Canvas, out: io.BytesIO) -> bytes:
    _line(c, 40, ((54, FOOTER),), size=7)
    c.showPage()
    c.save()
    return out.getvalue()


def activity_page_with_totals() -> bytes:
    """One page of account activity that ends in subtotal, tax and total lines."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=letter, invariant=1)
    c.setTitle("Account activity")
    _line(c, 740, ((54, "Larchmere Commons Residents Fund"),), size=14, bold=True)
    _line(c, 720, ((54, "Account activity"),), size=11)
    _line(c, 704, ((54, "Account number ending in 4417"),))
    y = _table(c, 670, ACTIVITY_ROWS)
    y = _totals(c, y, ACTIVITY_SUBTOTAL, ACTIVITY_TAX, ACTIVITY_TOTAL)
    _line(c, y - 20, ((54, "Thank you"),))
    return _finish(c, out)


def receipt_with_item_table() -> bytes:
    """A shop receipt whose items are printed as a dated table."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=letter, invariant=1)
    c.setTitle("Receipt")
    _line(c, 740, ((54, "Pebblebrook Garden Supply"),), size=14, bold=True)
    _line(c, 722, ((54, "18 Wren Lane"),))
    _line(c, 709, ((54, "Receipt no. R-004471"),))
    y = _table(c, 676, RECEIPT_ITEMS)
    y = _totals(c, y, RECEIPT_SUBTOTAL, RECEIPT_TAX, RECEIPT_TOTAL)
    _line(
        c, y - 14, ((54, f"Paid with Visa ending in {RECEIPT_LAST4}"), (-540, f"{RECEIPT_TOTAL}"))
    )
    _line(c, y - 34, ((54, "Thank you for shopping with us"),))
    return _finish(c, out)
