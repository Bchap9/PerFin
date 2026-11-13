# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pages on the border between two kinds of document (parser choice).

Two one-page PDFs that print the same table header, ``Date | Description |
Amount``, and the same closing lines (subtotal, tax, total), and pages with
one holding on them that are, or are not, a brokerage statement. Every name
and figure is invented:

* :func:`activity_page_with_totals`: a page of account activity, six dated
  rows, then ``Subtotal``, ``Tax`` and ``Total`` lines and "Thank you". It
  prints none of the phrases the receipt parser takes for a statement
  (``receipt._STATEMENT_WORDS``: no period label, no opening or closing
  balance), which is what this fixture is about: only its table says what
  it is.
  The generic brokerage parser scores a little above the generic statement
  parser here ("Account activity"; plan finding D4), is tried first and
  reads nothing, so the statement parser reads the page as the fallback
  (warning ``parser_fallback:brokerage_statement_generic``).
* :func:`receipt_with_item_table`: a shop's receipt whose items sit under
  the same header, with a subtotal, tax, a total and a card tender line.

* :func:`tax_form_explainer_with_a_holding`: two pages that explain a
  year-end tax form of a health savings account. They name a form in a
  sentence (so the tax form parser sees a form and reads no box), print no
  statement period and no total, and show the account's one fund in a table
  ``Investment | Shares | Price | Market Value`` under "as of" a day: a
  holdings table to the generic brokerage parser, whose one row multiplies
  out. Nothing on the pages says what the account holds as a whole
  (``total``: a ``Total`` line under the row, which only repeats it).
* :func:`one_holding_statement`: a brokerage statement of one fund, with a
  period and an account number. ``totals`` prints the holdings total, the
  cash balances and the account value under it; without them the page
  prints the period, the account and the table only.

:data:`ACTIVITY_ROWS`, :data:`RECEIPT_ITEMS` and the totals give what a
correct reading returns; :data:`HOLDING` is the one fund of the last two.
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

#: ``(name, shares, share price, market value)``: the one fund held.
HOLDING: Final = ("Larkspur Balanced Fund", "41.250", "26.40", "1,089.00")
HOLDING_VALUE: Final = Decimal("1089.00")
HOLDING_CASH: Final = Decimal("210.35")
HOLDING_LAST4: Final = "5529"

_HEADER: Final = ((54, "Date"), (130, "Description"), (-540, "Amount"))
_HOLDINGS_HEADER: Final = (
    (54, "Investment"),
    (-330, "Shares"),
    (-430, "Price"),
    (-540, "Market Value"),
)


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


def _holding(c: canvas.Canvas, y: float) -> float:
    name, shares, price, value = HOLDING
    _line(c, y, _HOLDINGS_HEADER, bold=True)
    return _rows(c, y - 15, [((54, name), (-330, shares), (-430, price), (-540, value))])


def tax_form_explainer_with_a_holding(*, total: bool = False) -> bytes:
    """Two pages about a year-end tax form, with the account's one fund in a table.

    ``total``: a ``Total`` line under the one row repeats its value.
    """
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=letter, invariant=1)
    c.setTitle("Understanding your year-end tax form")
    _line(c, 740, ((54, "Understanding your 2025 year-end tax form"),), size=14, bold=True)
    _line(c, 720, ((54, "Health savings account ending in " + HOLDING_LAST4),), size=11)
    y = _rows(
        c,
        694,
        [
            ((54, "This tax form reports the contributions made to your health savings"),),
            ((54, "account for the tax year. It is for your information and is not"),),
            ((54, "required to file your income tax return. Keep it with your records."),),
            ((54, "Money you took out is reported separately on Form 1099-SA, which is"),),
            ((54, "mailed in January."),),
        ],
    )
    y = _rows(
        c,
        y - 12,
        [
            ((54, "Box 2. Total contributions made in 2025"), (-540, "$3,100.00")),
            ((54, "Box 3. Total contributions made in 2026 for 2025"), (-540, "$0.00")),
            ((54, "Box 5. Fair market value of the account"), (-540, "$1,299.35")),
        ],
    )
    _line(c, y - 14, ((54, "Your investments as of 12/31/2025"),), size=11, bold=True)
    y = _holding(c, y - 34)
    if total:
        _line(c, y, ((54, "Total"), (-540, HOLDING[3])))
    _line(c, 40, ((54, FOOTER),), size=7)
    c.showPage()
    _line(c, 740, ((54, "Questions and answers"),), size=14, bold=True)
    _rows(
        c,
        714,
        [
            ((54, "Why did I receive this tax form? Every account holder receives one"),),
            ((54, "each year, whether or not a contribution was made."),),
            ((54, "Do I need to send it to anyone? No. Give the figures to whoever"),),
            ((54, "prepares your income tax return."),),
        ],
    )
    return _finish(c, out)


def one_holding_statement(*, totals: bool = True) -> bytes:
    """A brokerage statement of one fund (module doc)."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=letter, invariant=1)
    c.setTitle("Statement")
    _line(c, 740, ((54, "Larkspur Hollow Securities"),), size=14, bold=True)
    _line(c, 722, ((54, "Investment Account Statement"),), size=11)
    _line(
        c,
        706,
        (
            (54, f"Account XXXX-{HOLDING_LAST4}"),
            (300, "Statement period: 03/01/2026 - 03/31/2026"),
        ),
    )
    y = 680.0
    if totals:
        y = _rows(
            c,
            y,
            [
                ((54, "Beginning cash balance"), (-330, f"${HOLDING_CASH:,.2f}")),
                ((54, "Ending cash balance"), (-330, f"${HOLDING_CASH:,.2f}")),
                ((54, "Total holdings value"), (-330, f"${HOLDING_VALUE:,.2f}")),
                ((54, "Total account value"), (-330, f"${HOLDING_VALUE + HOLDING_CASH:,.2f}")),
            ],
            step=14,
        )
    _line(c, y - 10, ((54, "Holdings as of 03/31/2026"),), size=11, bold=True)
    y = _holding(c, y - 28)
    if totals:
        _line(c, y, ((54, "Total holdings"), (-540, f"{HOLDING_VALUE:,.2f}")))
    return _finish(c, out)
