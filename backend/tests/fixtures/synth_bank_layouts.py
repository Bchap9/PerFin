# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bank statements whose rows sit a little off their columns.

Each builder draws one PDF the generic statement reader has to read from the
geometry people's statements really have; every institution, payee, number
and figure is invented. Each prints a masked account number, a labelled
period, an opening and a closing balance and rows that reconcile:

* :func:`left_heading_right_figures`: ``Debit | Credit | Balance`` headings
  about 20 points wide at the left of 70-point columns whose figures are
  right-aligned, so no figure overlaps its heading or lies near its centre.
* :func:`tight_date_checking`: the date 2 points before the description, so
  the two are one cell and the date column gets none.
* :func:`tight_date_one_money_column`: the same with ``Date | Description |
  Amount`` and the amount 3 points after the description: the whole row is
  one cell.
* :func:`zero_placeholder_columns`: ``Credits | Debits | Balance`` with a
  figure in both money columns of every row, one of them a zero
  (``$250.00 | -$0.00``).

And statements whose balances are printed where a reader may take them for
something else:

* :func:`summary_table_balances`: a summary box under the header ``Date |
  Description | Amount`` whose two dated lines are the ``Beginning Balance``
  and the ``Ending Balance``, then the transactions under the same header.
* :func:`summary_label_wrapped`: ``Beginning Balance on`` and its date on two
  lines, the figure to their right half-way between the two: three lines.
* :func:`previous_statement_balance_first`: a summary that starts with
  ``Previous Statement Balance`` and ends, three lines on, with ``New
  Balance``.
* :func:`shares_opening_under_deposit`: two shares of one credit union
  member, each ``Beginning Balance`` figure printed under ``Deposit``.

And statements that print words another reader listens for:

* :func:`checking_with_card_note`: a ``Business Checking`` statement whose
  fee note, one sentence of 22 words, mentions a ``business credit card``.
* :func:`other_current_account`: a sterling ``Current Account Statement``
  of a bank no parser is written for (one of the two phrases the Mock
  Savings and Loan parser knows its statements by, and not the other).
* :func:`other_card_ending_in`: a card statement of an issuer no parser is
  written for, which prints ``Card ending in 4410`` (likewise one of Demo
  Card Services' two phrases).
* :func:`card_paid_from_checking`: the same card statement with the lines
  ``AutoPay from Checking ending 7731`` and ``Rewards savings this year
  $12.40``: words of a deposit account on a statement that is not one.

And statements whose amounts print no sign, in tables a title gives the
direction of:

* :func:`sectioned_unsigned_checking`: ``DEPOSITS AND ADDITIONS``,
  ``ELECTRONIC WITHDRAWALS`` and ``FEES``, each over its own ``Date |
  Description | Amount`` table of positive figures with a total line; the
  withdrawals run on to page 2 under the header printed again and the
  title ``ELECTRONIC WITHDRAWALS (continued)``.
* :func:`sectioned_repeat_without_title`: the same, page 2 printing only
  the header again.
* :func:`card_unsigned_payments`: a card statement whose ``Payments and
  Credits`` table prints the payment without a sign and whose
  ``Purchases`` table prints one refund with a minus.

And statements whose lines a reader may drop without a word, or take for rows:

* :func:`date_once_per_day`: ``Date | Description | Paid out | Paid in |
  Balance`` with 14 rows over 5 days, the date printed on the first row of
  each day only and the balance on every row; the last day holds 3 rows.
* :func:`summary_lines_under_table`: an ordinary dated table followed
  directly by ``Interest earned this period`` and ``Average ledger
  balance`` lines whose figures sit in the amount column: no rows.
* :func:`titled_sections_without_headers`: ``DEPOSITS & CREDITS`` over 3
  lines of date, text and figure with no header (counted, never read), then
  ``WITHDRAWALS`` over a ``Date | Description | Amount`` table of 4 rows.

:data:`EXPECTED_ROWS` gives, per builder, the rows a correct reading returns
(``(posted, amount, payee)``, money out negative); :data:`EXPECTED_BALANCES`
the opening and closing balance of the builders that are about balances.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from tests.fixtures.synth_unseen_layouts import _line, _rows

_FOOTER: Final = "Synthetic test document from the PerFin test suite."
_FONT: Final = "Helvetica"
_SIZE: Final = 9

#: ``(posted, amount, payee)``: what a correct reading returns.
ExpectedRow = tuple[date, Decimal, str]


def _d(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _money(value: Decimal) -> str:
    return f"{abs(value):,.2f}"


def _head(
    c: canvas.Canvas, bank: str, last4: str, period: str, opening: Decimal, closing: Decimal
) -> None:
    """The top of most statements here: who, which account, when, the two balances."""
    _top(c, bank, last4, period)
    _rows(
        c,
        700,
        [
            [(40, "Beginning Balance"), (-300, f"${_money(opening)}")],
            [(40, "Ending Balance"), (-300, f"${_money(closing)}")],
        ],
    )


def _top(c: canvas.Canvas, bank: str, last4: str, period: str) -> None:
    """Who, which account and when; the balances are the builder's own business."""
    _line(c, 750, [(40, bank)], size=14, bold=True)
    _line(c, 750, [(380, f"Account Number: ******{last4}")])
    _line(c, 736, [(380, f"Statement Period: {period}")])
    _line(c, 60, [(40, _FOOTER)], size=7)


def _fused(c: canvas.Canvas, y: float, x: float, parts: Sequence[tuple[str, float]]) -> None:
    """``parts`` (``(text, gap after it)``) one after the other from ``x``."""
    c.setFont(_FONT, _SIZE)
    for text, gap in parts:
        c.drawString(x, y, text)
        x += stringWidth(text, _FONT, _SIZE) + gap


def _running(opening: Decimal, rows: Sequence[ExpectedRow]) -> list[Decimal]:
    out: list[Decimal] = []
    balance = opening
    for _, amount, _ in rows:
        balance += amount
        out.append(balance)
    return out


# -- headings at the left of their columns, figures at the right ----------------

_LEFT_OPENING: Final = _d("1,480.25")
_LEFT_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 3, 2), _d("2,150.00"), "Payroll Deposit Fennel Works"),
    (date(2026, 3, 4), _d("-1,275.00"), "Rent Alder Court Apartments"),
    (date(2026, 3, 6), _d("-86.42"), "POS Purchase Pine Street Market"),
    (date(2026, 3, 11), _d("300.00"), "Online Transfer From Savings"),
    (date(2026, 3, 13), _d("-142.17"), "Tidewater Electric Bill Pay"),
    (date(2026, 3, 18), _d("-60.00"), "ATM Withdrawal Canal Street"),
    (date(2026, 3, 24), _d("-218.90"), "POS Purchase Ridgeline Outfitters"),
    (date(2026, 3, 30), _d("45.50"), "Mobile Deposit"),
)
#: Left edge of each 70-point money column (its heading starts there; its
#: figures end 70 points on).
_DEBIT_X: Final = 285.0
_CREDIT_X: Final = 390.0
_BALANCE_X: Final = 495.0
_COLUMN: Final = 70.0


def left_heading_right_figures() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_LEFT_OPENING, _LEFT_ROWS)[-1]
    _head(c, "Tamarack Savings Bank", "2046", "03/01/2026 - 03/31/2026", _LEFT_OPENING, closing)
    _line(
        c,
        650,
        [
            (40, "Date"),
            (100, "Description"),
            (_DEBIT_X, "Debit"),
            (_CREDIT_X, "Credit"),
            (_BALANCE_X, "Balance"),
        ],
        size=8,
    )
    lines = []
    for (posted, amount, payee), balance in zip(
        _LEFT_ROWS, _running(_LEFT_OPENING, _LEFT_ROWS), strict=True
    ):
        column = _DEBIT_X if amount < 0 else _CREDIT_X
        lines.append(
            [
                (40, posted.strftime("%m/%d/%Y")),
                (100, payee),
                (-(column + _COLUMN), _money(amount)),
                (-(_BALANCE_X + _COLUMN), _money(balance)),
            ]
        )
    _rows(c, 636, lines)
    c.save()
    return buf.getvalue()


# -- the date printed close to the description ----------------------------------

_TIGHT_OPENING: Final = _d("905.60")
_TIGHT_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 5, 1), _d("-720.00"), "Rent Juniper Row Housing"),
    (date(2026, 5, 4), _d("1,640.00"), "Direct Deposit Calloway Freight"),
    (date(2026, 5, 7), _d("-52.38"), "POS Purchase Marigold Grocers"),
    (date(2026, 5, 12), _d("-18.00"), "Transit Pass Reload"),
    (date(2026, 5, 15), _d("-96.14"), "Brightwater Utilities Online Pmt"),
    (date(2026, 5, 21), _d("-40.00"), "ATM Withdrawal Depot Square"),
    (date(2026, 5, 28), _d("125.00"), "Transfer From Savings"),
)


def tight_date_checking() -> bytes:
    """``MM/DD`` two points before the description; amount and balance in their columns."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    balances = _running(_TIGHT_OPENING, _TIGHT_ROWS)
    _head(
        c, "Copperfield Trust Bank", "5583", "05/01/2026 - 05/31/2026", _TIGHT_OPENING, balances[-1]
    )
    _line(
        c,
        650,
        [(40, "Date"), (75, "Description"), (-430, "Amount"), (-520, "Balance")],
        size=8,
    )
    y = 636.0
    for (posted, amount, payee), balance in zip(_TIGHT_ROWS, balances, strict=True):
        _fused(c, y, 40, [(posted.strftime("%m/%d"), 2.0), (payee, 0.0)])
        _line(c, y, [(-430, f"{amount:,.2f}"), (-520, _money(balance))])
        y -= 13
    c.save()
    return buf.getvalue()


_ONE_OPENING: Final = _d("2,310.40")
_ONE_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 7, 2), _d("-64.18"), "POS Purchase Saltmarsh Bakery"),
    (date(2026, 7, 6), _d("1,905.00"), "Direct Deposit Orchard Lane Studio"),
    (date(2026, 7, 9), _d("-1,100.00"), "Rent Wren Hollow Lettings"),
    (date(2026, 7, 14), _d("-37.25"), "Kestrel Mobile Autopay"),
    (date(2026, 7, 20), _d("-12.00"), "Monthly Maintenance Charge"),
    (date(2026, 7, 27), _d("-208.63"), "POS Purchase Foxglove Hardware"),
)


def tight_date_one_money_column() -> bytes:
    """Date, description and a signed amount a few points apart: one cell a row."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_ONE_OPENING, _ONE_ROWS)[-1]
    _head(c, "Larkspur Federal Bank", "9172", "07/01/2026 - 07/31/2026", _ONE_OPENING, closing)
    _line(c, 650, [(40, "Date"), (95, "Description"), (250, "Amount")], size=8)
    y = 636.0
    for posted, amount, payee in _ONE_ROWS:
        _fused(
            c,
            y,
            40,
            [(posted.strftime("%m/%d/%Y"), 2.0), (payee, 3.0), (f"{amount:,.2f}", 0.0)],
        )
        y -= 13
    c.save()
    return buf.getvalue()


# -- a figure in both money columns, one of them a zero -------------------------

_ZERO_OPENING: Final = _d("1,480.25")
_ZERO_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 9, 3), _d("250.00"), "Transfer From External Account"),
    (date(2026, 9, 8), _d("-64.18"), "Withdrawal To Linked Checking"),
    (date(2026, 9, 15), _d("1,200.00"), "Direct Deposit Hawthorn Press"),
    (date(2026, 9, 19), _d("-300.00"), "Online Transfer To Money Market"),
    (date(2026, 9, 25), _d("3.07"), "Savings Yield Paid"),
    (date(2026, 9, 29), _d("-45.00"), "Outgoing Wire Charge"),
)


def zero_placeholder_columns() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    balances = _running(_ZERO_OPENING, _ZERO_ROWS)
    _head(
        c, "Bellwether Online Bank", "3305", "09/01/2026 - 09/30/2026", _ZERO_OPENING, balances[-1]
    )
    _line(
        c,
        650,
        [
            (40, "Date"),
            (110, "Description"),
            (-380, "Credits"),
            (-460, "Debits"),
            (-550, "Balance"),
        ],
        size=8,
    )
    lines = []
    for (posted, amount, payee), balance in zip(_ZERO_ROWS, balances, strict=True):
        credit = amount if amount > 0 else Decimal(0)
        debit = -amount if amount < 0 else Decimal(0)
        lines.append(
            [
                (40, posted.strftime("%m/%d/%Y")),
                (110, payee),
                (-380, f"${_money(credit)}"),
                (-460, f"-${_money(debit)}"),
                (-550, f"${_money(balance)}"),
            ]
        )
    _rows(c, 636, lines)
    c.save()
    return buf.getvalue()


# -- balances printed where a reader may take them for something else ------------

_BOX_OPENING: Final = _d("1,204.50")
_BOX_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 2, 3), _d("-58.12"), "POS Purchase Thistle Lane Grocery"),
    (date(2026, 2, 6), _d("1,730.00"), "Direct Deposit Harrow Mill Works"),
    (date(2026, 2, 10), _d("-945.00"), "Rent Osprey Terrace Homes"),
    (date(2026, 2, 17), _d("-71.36"), "Glenmoor Water Online Pmt"),
    (date(2026, 2, 20), _d("-25.00"), "ATM Withdrawal Station Road"),
    (date(2026, 2, 26), _d("210.00"), "Mobile Deposit"),
)
_SIGNED_HEADER: Final = [(40, "Date"), (110, "Description"), (-500, "Amount")]


def _signed_rows(c: canvas.Canvas, y: float, rows: Sequence[ExpectedRow]) -> None:
    """A ``Date | Description | Amount`` table: its header at ``y``, signed amounts."""
    _line(c, y, _SIGNED_HEADER, size=8)
    _rows(
        c,
        y - 14,
        [
            [(40, posted.strftime("%m/%d/%Y")), (110, payee), (-500, f"{amount:,.2f}")]
            for posted, amount, payee in rows
        ],
    )


def summary_table_balances() -> bytes:
    """The two balances as dated lines of a summary box with the table's own header."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_BOX_OPENING, _BOX_ROWS)[-1]
    _top(c, "Hollowbrook Community Bank", "7418", "02/01/2026 - 02/28/2026")
    _line(c, 706, [(40, "Account Summary")], size=11, bold=True)
    _line(c, 690, _SIGNED_HEADER, size=8)
    _rows(
        c,
        676,
        [
            [(40, "02/01/2026"), (110, "Beginning Balance"), (-500, f"${_money(_BOX_OPENING)}")],
            [(40, "02/28/2026"), (110, "Ending Balance"), (-500, f"${_money(closing)}")],
        ],
    )
    _line(c, 630, [(40, "Account Activity")], size=11, bold=True)
    _signed_rows(c, 614, _BOX_ROWS)
    c.save()
    return buf.getvalue()


_WRAP_OPENING: Final = _d("3,061.75")
_WRAP_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 8, 4), _d("-112.40"), "POS Purchase Birchgate Pharmacy"),
    (date(2026, 8, 7), _d("2,015.00"), "Direct Deposit Lowmoor Textiles"),
    (date(2026, 8, 12), _d("-1,320.00"), "Rent Fernhill Court Lettings"),
    (date(2026, 8, 19), _d("-48.95"), "Ashdown Broadband Autopay"),
    (date(2026, 8, 27), _d("-80.00"), "ATM Withdrawal Market Cross"),
)


def summary_label_wrapped() -> bytes:
    """``Beginning Balance on`` / its figure / its date as three lines 5 points apart."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_WRAP_OPENING, _WRAP_ROWS)[-1]
    _top(c, "Stonecrop Mutual Bank", "6027", "08/01/2026 - 08/31/2026")
    _line(c, 705, [(40, "Beginning Balance on")])
    _line(c, 700, [(-300, f"${_money(_WRAP_OPENING)}")])
    _line(c, 695, [(40, "August 1, 2026")])
    _line(c, 672, [(40, "Ending Balance"), (-300, f"${_money(closing)}")])
    _signed_rows(c, 640, _WRAP_ROWS)
    c.save()
    return buf.getvalue()


_PREVIOUS_OPENING: Final = _d("640.15")
_PREVIOUS_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 10, 2), _d("1,480.00"), "Direct Deposit Wexcombe Joinery"),
    (date(2026, 10, 6), _d("-610.00"), "Rent Sorrel Bank Cottages"),
    (date(2026, 10, 13), _d("-93.27"), "POS Purchase Dunmere Farm Shop"),
    (date(2026, 10, 21), _d("75.00"), "Transfer From Savings"),
    (date(2026, 10, 28), _d("-134.60"), "Pellbrook Gas Online Pmt"),
)


def previous_statement_balance_first() -> bytes:
    """``Previous Statement Balance`` first, ``New Balance`` three lines later."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_PREVIOUS_OPENING, _PREVIOUS_ROWS)[-1]
    money_in = sum((a for _, a, _ in _PREVIOUS_ROWS if a > 0), Decimal(0))
    money_out = sum((a for _, a, _ in _PREVIOUS_ROWS if a < 0), Decimal(0))
    _top(c, "Sedgewick Savings Association", "1953", "10/01/2026 - 10/31/2026")
    _rows(
        c,
        700,
        [
            [(40, "Previous Statement Balance"), (-300, f"${_money(_PREVIOUS_OPENING)}")],
            [(40, "Total Deposits"), (-300, f"${_money(money_in)}")],
            [(40, "Total Withdrawals"), (-300, f"${_money(money_out)}")],
            [(40, "New Balance"), (-300, f"${_money(closing)}")],
        ],
    )
    _signed_rows(c, 630, _PREVIOUS_ROWS)
    c.save()
    return buf.getvalue()


#: ``(title, last four digits, opening, rows)`` of each share of the member.
_SHARES: Final[tuple[tuple[str, str, Decimal, tuple[ExpectedRow, ...]], ...]] = (
    (
        "REGULAR SAVINGS - ID 0001",
        "0001",
        _d("3,200.00"),
        (
            (date(2026, 6, 9), _d("150.00"), "Transfer From Share 0009"),
            (date(2026, 6, 30), _d("0.71"), "Dividend Paid"),
        ),
    ),
    (
        "EVERYDAY CHECKING - ID 0009",
        "0009",
        _d("968.30"),
        (
            (date(2026, 6, 4), _d("-61.05"), "POS Debit AMBERLEY GREENGROCER"),
            (date(2026, 6, 9), _d("-150.00"), "Transfer To Share 0001"),
            (date(2026, 6, 16), _d("1,405.00"), "ACH Deposit TALLOWFIELD PAYROLL"),
        ),
    ),
)


def shares_opening_under_deposit() -> bytes:
    """Two shares of one member; each opening figure sits under ``Deposit``."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _line(c, 750, [(40, "Meadowlark Members Credit Union")], size=14, bold=True)
    _line(c, 750, [(380, "Statement Period: 06/01/2026 - 06/30/2026")])
    _line(c, 738, [(380, "Member Number: ******8834")])
    _line(c, 60, [(40, _FOOTER)], size=7)
    y = 700.0
    for title, _, opening, rows in _SHARES:
        balances = _running(opening, rows)
        _line(c, y, [(40, title)], size=11, bold=True)
        _line(
            c,
            y - 16,
            [
                (40, "Trans Date"),
                (110, "Transaction Description"),
                (-400, "Withdrawal"),
                (-470, "Deposit"),
                (-550, "Balance"),
            ],
            size=8,
        )
        y = _rows(
            c,
            y - 29,
            [
                [(40, "06/01"), (110, "Beginning Balance"), (-470, _money(opening))],
                *(
                    [
                        (40, posted.strftime("%m/%d")),
                        (110, payee),
                        (-400 if amount < 0 else -470, _money(amount)),
                        (-550, _money(balance)),
                    ]
                    for (posted, amount, payee), balance in zip(rows, balances, strict=True)
                ),
                [(40, "06/30"), (110, "Ending Balance"), (-550, _money(balances[-1]))],
            ],
        )
        y -= 17
    c.save()
    return buf.getvalue()


# -- words another reader listens for --------------------------------------------

_NOTE_OPENING: Final = _d("4,820.10")
_NOTE_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 4, 2), _d("-1,450.00"), "Lease Payment Quarry Lane Units"),
    (date(2026, 4, 6), _d("3,275.40"), "Merchant Settlement Batch 0418"),
    (date(2026, 4, 9), _d("-312.66"), "ACH Debit Windrush Packaging"),
    (date(2026, 4, 15), _d("-1,980.00"), "Payroll Transfer Out"),
    (date(2026, 4, 21), _d("2,140.00"), "Invoice 2207 Elmstead Cafe"),
    (date(2026, 4, 28), _d("-18.00"), "Monthly Service Charge"),
)
#: One sentence of 22 words, printed on one line above the table.
CARD_NOTE: Final = (
    "The monthly service charge is waived when you keep a Garnet Ridge business "
    "credit card open and make one purchase each cycle."
)


def checking_with_card_note() -> bytes:
    """A checking statement; its fee note says ``business credit card``."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    balances = _running(_NOTE_OPENING, _NOTE_ROWS)
    _head(c, "Garnet Ridge Bank", "6641", "04/01/2026 - 04/30/2026", _NOTE_OPENING, balances[-1])
    _line(c, 722, [(40, "Business Checking")], size=11, bold=True)
    _line(c, 668, [(40, CARD_NOTE)], size=8)
    _line(
        c,
        650,
        [(40, "Date"), (110, "Description"), (-430, "Amount"), (-520, "Balance")],
        size=8,
    )
    _rows(
        c,
        636,
        [
            [
                (40, posted.strftime("%m/%d/%Y")),
                (110, payee),
                (-430, f"{amount:,.2f}"),
                (-520, _money(balance)),
            ]
            for (posted, amount, payee), balance in zip(_NOTE_ROWS, balances, strict=True)
        ],
    )
    c.save()
    return buf.getvalue()


_CURRENT_OPENING: Final = _d("2,315.40")
_CURRENT_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 2, 3), _d("-46.10"), "WATER RATES ELMBRIDGE"),
    (date(2026, 2, 6), _d("-72.35"), "CORNMARKET GROCERS 118"),
    (date(2026, 2, 13), _d("1,890.00"), "LOWFIELD ARCHIVES SALARY"),
    (date(2026, 2, 17), _d("-825.00"), "RENT T ASHCOMBE"),
    (date(2026, 2, 24), _d("-60.00"), "CASH MACHINE BRIDGE ST"),
)


def other_current_account() -> bytes:
    """Sterling, day first, ``Money out | Money in | Balance``; not Mock Savings and Loan."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    balances = _running(_CURRENT_OPENING, _CURRENT_ROWS)
    _line(c, 800, [(40, "Harrowgate & Fold Bank")], size=14, bold=True)
    _line(c, 800, [(380, "Account Number: ****5527")])
    _line(c, 786, [(380, "Statement Period: 01/02/2026 - 28/02/2026")])
    _line(c, 782, [(40, "Current Account Statement")])
    _line(c, 60, [(40, _FOOTER)], size=7)
    _rows(
        c,
        750,
        [
            [(40, "Opening Balance"), (-300, f"£{_money(_CURRENT_OPENING)}")],
            [(40, "Closing Balance"), (-300, f"£{_money(balances[-1])}")],
        ],
    )
    _line(
        c,
        700,
        [
            (40, "Date"),
            (110, "Description"),
            (-400, "Money out"),
            (-470, "Money in"),
            (-550, "Balance"),
        ],
        size=8,
    )
    _rows(
        c,
        686,
        [
            [
                (40, posted.strftime("%d/%m/%Y")),
                (110, payee),
                (-400 if amount < 0 else -470, _money(amount)),
                (-550, _money(balance)),
            ]
            for (posted, amount, payee), balance in zip(_CURRENT_ROWS, balances, strict=True)
        ],
    )
    c.save()
    return buf.getvalue()


#: As the ledger keeps them: a charge is money out, the payment money in.
_CARD_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 5, 4), _d("512.30"), "PAYMENT RECEIVED - THANK YOU"),
    (date(2026, 5, 6), _d("-41.18"), "OAKHAVEN BOOKSELLERS"),
    (date(2026, 5, 11), _d("-17.60"), "BRINDLE STREET CAFE"),
    (date(2026, 5, 19), _d("-96.00"), "NORTHGATE RAIL TICKETS"),
    (date(2026, 5, 26), _d("-63.47"), "FELLSIDE GARDEN SUPPLY"),
)
_CARD_PREVIOUS: Final = _d("512.30")


#: Lines a card statement prints about the bank account it is paid from and
#: about what its rewards saved: each holds a word a deposit account is named
#: by, and neither names the account the statement is of.
CARD_AUTOPAY_LINE: Final = "AutoPay from Checking ending 7731"
CARD_REWARDS_LINE: Final = "Rewards savings this year $12.40"


def other_card_ending_in() -> bytes:
    """A card statement (charges positive) that prints ``Card ending in 4410``."""
    return _card_ending_in(autopay=False)


def card_paid_from_checking() -> bytes:
    """:func:`other_card_ending_in` with :data:`CARD_AUTOPAY_LINE` under the
    card's number and :data:`CARD_REWARDS_LINE` under the summary."""
    return _card_ending_in(autopay=True)


def _card_ending_in(*, autopay: bool) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    charges = -sum((a for _, a, _ in _CARD_ROWS if a < 0), Decimal(0))
    credits = sum((a for _, a, _ in _CARD_ROWS if a > 0), Decimal(0))
    owed = _CARD_PREVIOUS - credits + charges
    _line(c, 750, [(40, "Brackenfell Rewards Mastercard")], size=14, bold=True)
    _line(c, 734, [(40, "Card ending in 4410")])
    _line(c, 750, [(380, "Billing Period: 05/01/2026 - 05/31/2026")])
    _line(c, 60, [(40, _FOOTER)], size=7)
    _rows(
        c,
        700,
        [
            [(40, "Previous Balance"), (-300, f"${_money(_CARD_PREVIOUS)}")],
            [(40, "Payments and Credits"), (-300, f"-${_money(credits)}")],
            [(40, "New Charges"), (-300, f"+${_money(charges)}")],
            [(40, "New Balance"), (-300, f"${_money(owed)}")],
            [(40, "Minimum Payment Due"), (-300, "$25.00")],
        ],
    )
    if autopay:
        _line(c, 718, [(40, CARD_AUTOPAY_LINE)])
        _line(c, 636, [(40, CARD_REWARDS_LINE)])
    _line(c, 618, [(40, "Date"), (110, "Description"), (-560, "Amount")], size=8)
    _rows(
        c,
        604,
        [
            [
                (40, posted.strftime("%m/%d/%y")),
                (110, payee),
                (-560, f"${_money(amount)}" if amount < 0 else f"-${_money(amount)}"),
            ]
            for posted, amount, payee in _CARD_ROWS
        ],
    )
    c.save()
    return buf.getvalue()


# -- unsigned amounts under a title that says which way the money went ----------

_SECTION_OPENING: Final = _d("2,640.18")
_SECTION_DEPOSITS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 4, 3), _d("1,875.00"), "Payroll Thistleyard Joinery"),
    (date(2026, 4, 14), _d("220.00"), "Mobile Deposit"),
    (date(2026, 4, 24), _d("64.50"), "Refund Corbel Lane Cycles"),
)
_SECTION_WITHDRAWALS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 4, 2), _d("-980.00"), "Rent Sorrelgate Lettings"),
    (date(2026, 4, 7), _d("-71.36"), "Card Purchase Greenfinch Market"),
    (date(2026, 4, 12), _d("-118.02"), "Bill Pay Eastmere Power"),
    (date(2026, 4, 19), _d("-45.00"), "ATM Cash Mill Lane"),
    (date(2026, 4, 27), _d("-203.77"), "Card Purchase Hazelbank Tools"),
)
#: No word of it types the row: the ``FEES`` title does.
_SECTION_FEES: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 4, 30), _d("-12.00"), "Monthly Maintenance Charge"),
)
_SECTION_ROWS: Final = (*_SECTION_DEPOSITS, *_SECTION_WITHDRAWALS, *_SECTION_FEES)
#: Withdrawals printed on page 1; the rest are on page 2.
_SECTION_SPLIT: Final = 3
_UNSIGNED_HEADER: Final = [(40, "Date"), (110, "Description"), (-560, "Amount")]


def _total(rows: Sequence[ExpectedRow]) -> Decimal:
    return abs(sum((amount for _, amount, _ in rows), Decimal(0)))


def _unsigned_table(
    c: canvas.Canvas, y: float, title: str | None, rows: Sequence[ExpectedRow]
) -> float:
    """``title`` (when given), the header and ``rows`` with positive figures; the next y."""
    if title is not None:
        _line(c, y, [(40, title)], size=10, bold=True)
        y -= 16
    _line(c, y, _UNSIGNED_HEADER, size=8)
    return _rows(
        c,
        y - 13,
        [
            [(40, posted.strftime("%m/%d/%Y")), (110, payee), (-560, _money(amount))]
            for posted, amount, payee in rows
        ],
    )


def _total_line(c: canvas.Canvas, y: float, label: str, rows: Sequence[ExpectedRow]) -> float:
    _line(c, y, [(40, label), (-560, f"${_money(_total(rows))}")], bold=True)
    return y - 26


def _sectioned_unsigned(*, continued_title: bool, omit: ExpectedRow | None = None) -> bytes:
    """``omit`` is a withdrawal left off the page; the totals and balances count it."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_SECTION_OPENING, _SECTION_ROWS)[-1]
    bank = "Wexcombe River Bank"
    _head(c, bank, "7318", "04/01/2026 - 04/30/2026", _SECTION_OPENING, closing)
    y = _rows(
        c,
        674,
        [
            [(40, "Deposits and Additions"), (-300, _money(_total(_SECTION_DEPOSITS)))],
            [(40, "Electronic Withdrawals"), (-300, _money(_total(_SECTION_WITHDRAWALS)))],
            [(40, "Fees"), (-300, _money(_total(_SECTION_FEES)))],
        ],
    )
    y = _unsigned_table(c, y - 15, "DEPOSITS AND ADDITIONS", _SECTION_DEPOSITS)
    y = _total_line(c, y, "Total Deposits and Additions", _SECTION_DEPOSITS)
    first = [row for row in _SECTION_WITHDRAWALS[:_SECTION_SPLIT] if row != omit]
    _unsigned_table(c, y, "ELECTRONIC WITHDRAWALS", first)
    c.showPage()
    _line(c, 750, [(40, bank)], size=14, bold=True)
    _line(c, 750, [(380, "Page 2 of 2")])
    _line(c, 60, [(40, _FOOTER)], size=7)
    y = _unsigned_table(
        c,
        716,
        "ELECTRONIC WITHDRAWALS (continued)" if continued_title else None,
        [row for row in _SECTION_WITHDRAWALS[_SECTION_SPLIT:] if row != omit],
    )
    y = _total_line(c, y, "Total Electronic Withdrawals", _SECTION_WITHDRAWALS)
    y = _unsigned_table(c, y, "FEES", _SECTION_FEES)
    _total_line(c, y, "Total Fees", _SECTION_FEES)
    c.save()
    return buf.getvalue()


def sectioned_unsigned_checking() -> bytes:
    """Three titled tables of positive figures; the second continues, titled, on page 2."""
    return _sectioned_unsigned(continued_title=True)


def sectioned_repeat_without_title() -> bytes:
    """The same; page 2 starts with the header alone."""
    return _sectioned_unsigned(continued_title=False)


#: The withdrawal :func:`section_total_off` does not print.
SECTION_ROW_OFF: Final = _SECTION_WITHDRAWALS[1]


def section_total_off() -> bytes:
    """:func:`sectioned_unsigned_checking` without :data:`SECTION_ROW_OFF`:
    the withdrawals printed no longer add up to their total, nor the
    statement to its closing balance."""
    return _sectioned_unsigned(continued_title=True, omit=SECTION_ROW_OFF)


#: As the ledger keeps them: a purchase is money out, the payment and the refund money in.
_UNSIGNED_CARD_PAYMENTS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 6, 5), _d("640.25"), "PAYMENT RECEIVED - THANK YOU"),
)
_UNSIGNED_CARD_PURCHASES: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 6, 3), _d("-58.20"), "LINDENWALK STATIONERS"),
    (date(2026, 6, 9), _d("-34.75"), "COPPERGATE BISTRO"),
    (date(2026, 6, 15), _d("19.99"), "RETURN LINDENWALK STATIONERS"),
    (date(2026, 6, 22), _d("-47.10"), "MOORSIDE FUEL STOP"),
)
_UNSIGNED_CARD_ROWS: Final = (*_UNSIGNED_CARD_PAYMENTS, *_UNSIGNED_CARD_PURCHASES)
_UNSIGNED_CARD_PREVIOUS: Final = _d("640.25")


def card_unsigned_payments() -> bytes:
    """A card: the payment printed without a sign under ``Payments and Credits``."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    charges = -sum((a for _, a, _ in _UNSIGNED_CARD_ROWS if a < 0), Decimal(0))
    credits = sum((a for _, a, _ in _UNSIGNED_CARD_ROWS if a > 0), Decimal(0))
    owed = _UNSIGNED_CARD_PREVIOUS - credits + charges
    _line(c, 750, [(40, "Pellwick Rewards Visa")], size=14, bold=True)
    _line(c, 750, [(380, "Account Number: ******6093")])
    _line(c, 736, [(380, "Billing Period: 06/01/2026 - 06/30/2026")])
    _line(c, 60, [(40, _FOOTER)], size=7)
    y = _rows(
        c,
        700,
        [
            [(40, "Previous Balance"), (-300, f"${_money(_UNSIGNED_CARD_PREVIOUS)}")],
            [(40, "Payments and Credits"), (-300, f"-${_money(credits)}")],
            [(40, "New Charges"), (-300, f"+${_money(charges)}")],
            [(40, "New Balance"), (-300, f"${_money(owed)}")],
            [(40, "Minimum Payment Due"), (-300, "$25.00")],
            [(40, "Credit Limit"), (-300, "$4,000.00")],
        ],
    )
    y -= 15
    for title, rows in (
        ("Payments and Credits", _UNSIGNED_CARD_PAYMENTS),
        ("Purchases", _UNSIGNED_CARD_PURCHASES),
    ):
        _line(c, y, [(40, title)], size=10, bold=True)
        _line(c, y - 16, _UNSIGNED_HEADER, size=8)
        y = _rows(
            c,
            y - 29,
            [
                [
                    (40, posted.strftime("%m/%d/%y")),
                    (110, payee),
                    # A purchase and the payment print no sign; the refund a minus.
                    (-560, f"-${_money(amount)}" if payee.startswith("RETURN") else _money(amount)),
                ]
                for posted, amount, payee in rows
            ],
        )
        y -= 13
    c.save()
    return buf.getvalue()


# -- lines found and not read, and dates printed once per day -------------------

_DAILY_OPENING: Final = _d("1,932.44")
_DAILY_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 5, 4), _d("-42.18"), "Card Purchase Larchmere Bakery"),
    (date(2026, 5, 4), _d("-310.00"), "Standing Order Pennywell Lettings"),
    (date(2026, 5, 4), _d("1,640.00"), "Salary Brackenholt Glassworks"),
    (date(2026, 5, 8), _d("-18.75"), "Card Purchase Otterburn Books"),
    (date(2026, 5, 8), _d("-64.20"), "Direct Debit Kestrelside Water"),
    (date(2026, 5, 8), _d("-25.00"), "Cash Machine Tanner Row"),
    (date(2026, 5, 13), _d("85.00"), "Transfer From Saver"),
    (date(2026, 5, 13), _d("-131.46"), "Card Purchase Windlestone Grocers"),
    (date(2026, 5, 13), _d("-9.99"), "Direct Debit Lapwing Mobile"),
    (date(2026, 5, 21), _d("-57.30"), "Card Purchase Fernhollow Garage"),
    (date(2026, 5, 21), _d("240.00"), "Cheque Paid In"),
    (date(2026, 5, 28), _d("-72.64"), "Direct Debit Marlpit Energy"),
    (date(2026, 5, 28), _d("-14.50"), "Card Purchase Sedgewick Cafe"),
    (date(2026, 5, 28), _d("-200.00"), "Transfer To Saver"),
)


def date_once_per_day() -> bytes:
    """Fourteen rows over five days; only the first row of a day prints its date."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    balances = _running(_DAILY_OPENING, _DAILY_ROWS)
    _head(
        c, "Pellbrook Savings Bank", "5127", "05/01/2026 - 05/31/2026", _DAILY_OPENING, balances[-1]
    )
    _line(
        c,
        650,
        [
            (40, "Date"),
            (110, "Description"),
            (-400, "Paid out"),
            (-470, "Paid in"),
            (-550, "Balance"),
        ],
        size=8,
    )
    lines: list[list[tuple[float, str]]] = []
    printed: date | None = None
    for (posted, amount, payee), balance in zip(_DAILY_ROWS, balances, strict=True):
        cells: list[tuple[float, str]] = []
        if posted != printed:
            cells.append((40, posted.strftime("%m/%d/%Y")))
            printed = posted
        cells += [
            (110, payee),
            (-400 if amount < 0 else -470, _money(amount)),
            (-550, _money(balance)),
        ]
        lines.append(cells)
    _rows(c, 636, lines)
    c.save()
    return buf.getvalue()


_UNDER_OPENING: Final = _d("2,418.06")
_UNDER_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 7, 2), _d("-88.14"), "POS Purchase Dunmallow Hardware"),
    (date(2026, 7, 9), _d("1,512.00"), "Direct Deposit Quarrybank Textiles"),
    (date(2026, 7, 15), _d("-760.00"), "Rent Heronsgate Court"),
    (date(2026, 7, 22), _d("-39.95"), "Skylark Broadband Online Pmt"),
    (date(2026, 7, 31), _d("0.38"), "Interest Credit"),
)


def summary_lines_under_table() -> bytes:
    """An ordinary table; two summary lines right under it have a figure under ``Amount``."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_UNDER_OPENING, _UNDER_ROWS)[-1]
    _head(c, "Yarrowfield Bank", "3390", "07/01/2026 - 07/31/2026", _UNDER_OPENING, closing)
    _signed_rows(c, 650, _UNDER_ROWS)
    _rows(
        c,
        636 - 13 * len(_UNDER_ROWS),
        [
            [(110, "Interest earned this period"), (-500, "0.38")],
            [(110, "Average ledger balance"), (-500, "2,611.47")],
        ],
    )
    c.save()
    return buf.getvalue()


_HEADERLESS_OPENING: Final = _d("1,275.90")
#: Printed under ``DEPOSITS & CREDITS`` with no header: counted, never read.
_HEADERLESS_DEPOSITS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 9, 3), _d("1,420.00"), "Payroll Coldharbour Printworks"),
    (date(2026, 9, 12), _d("75.00"), "Mobile Deposit"),
    (date(2026, 9, 25), _d("31.20"), "Refund Ashlar Street Cobblers"),
)
_HEADERLESS_WITHDRAWALS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 9, 1), _d("-890.00"), "Rent Wrenfield Estates"),
    (date(2026, 9, 8), _d("-52.61"), "Card Purchase Tolbooth Market"),
    (date(2026, 9, 17), _d("-104.33"), "Bill Pay Northmoor Gas"),
    (date(2026, 9, 26), _d("-40.00"), "ATM Cash Chandlers Wharf"),
)


def titled_sections_without_headers() -> bytes:
    """Deposits under a title alone; withdrawals under a title and a header."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    everything = (*_HEADERLESS_DEPOSITS, *_HEADERLESS_WITHDRAWALS)
    closing = _running(_HEADERLESS_OPENING, everything)[-1]
    _head(
        c, "Corriedale Trust Bank", "6052", "09/01/2026 - 09/30/2026", _HEADERLESS_OPENING, closing
    )
    _line(c, 660, [(40, "DEPOSITS & CREDITS")], size=10, bold=True)
    y = _rows(
        c,
        644,
        [
            [(40, posted.strftime("%m/%d/%Y")), (110, payee), (-560, _money(amount))]
            for posted, amount, payee in _HEADERLESS_DEPOSITS
        ],
    )
    _unsigned_table(c, y - 15, "WITHDRAWALS", _HEADERLESS_WITHDRAWALS)
    c.save()
    return buf.getvalue()


# -- totals of sections, totals of the year, and rows printed newest first ------

_NEWEST_OPENING: Final = _d("3,206.71")
#: Oldest first, as a correct reading returns them; the page prints them reversed.
_NEWEST_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 8, 3), _d("-1,120.00"), "Rent Fallowmead Housing"),
    (date(2026, 8, 3), _d("-36.48"), "Card Purchase Brindlewood Grocer"),
    (date(2026, 8, 4), _d("1,980.00"), "Payroll Osierbank Ceramics"),
    (date(2026, 8, 5), _d("-22.10"), "Card Purchase Tallowgate Cafe"),
    (date(2026, 8, 6), _d("-84.63"), "Bill Pay Harrowdene Water"),
    (date(2026, 8, 7), _d("-50.00"), "ATM Cash Wickerby Square"),
    (date(2026, 8, 10), _d("-17.95"), "Card Purchase Plumtree News"),
    (date(2026, 8, 11), _d("140.00"), "Mobile Deposit"),
    (date(2026, 8, 12), _d("-63.27"), "Card Purchase Elmscott Pharmacy"),
    (date(2026, 8, 13), _d("-129.40"), "Bill Pay Ravensmoor Electric"),
    (date(2026, 8, 14), _d("-41.86"), "Card Purchase Brindlewood Grocer"),
    (date(2026, 8, 17), _d("-250.00"), "Transfer To Savings 2290"),
    (date(2026, 8, 18), _d("1,980.00"), "Payroll Osierbank Ceramics"),
    (date(2026, 8, 19), _d("-58.12"), "Card Purchase Stonechat Fuel"),
    (date(2026, 8, 20), _d("-33.00"), "Card Purchase Kiln Lane Bistro"),
    (date(2026, 8, 21), _d("-95.74"), "Bill Pay Marrowfield Mobile"),
    (date(2026, 8, 24), _d("27.30"), "Refund Elmscott Pharmacy"),
    (date(2026, 8, 25), _d("-12.99"), "Card Purchase Plumtree News"),
    (date(2026, 8, 27), _d("-76.05"), "Card Purchase Brindlewood Grocer"),
    (date(2026, 8, 31), _d("-9.00"), "Monthly Service Charge"),
)


def newest_first_checking() -> bytes:
    """Twenty rows with their running balance, the last day at the top."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    balances = _running(_NEWEST_OPENING, _NEWEST_ROWS)
    _head(
        c,
        "Skelwith Dale Bank",
        "8846",
        "08/01/2026 - 08/31/2026",
        _NEWEST_OPENING,
        balances[-1],
    )
    _line(
        c,
        650,
        [(40, "Date"), (110, "Description"), (-470, "Amount"), (-550, "Balance")],
        size=8,
    )
    printed = list(zip(_NEWEST_ROWS, balances, strict=True))[::-1]
    _rows(
        c,
        636,
        [
            [
                (40, posted.strftime("%m/%d/%Y")),
                (110, payee),
                (-470, f"{amount:,.2f}"),
                (-550, _money(balance)),
            ]
            for (posted, amount, payee), balance in printed
        ],
    )
    c.save()
    return buf.getvalue()


#: As the ledger keeps them: purchases and the fee are money out, the payment money in.
_YTD_PAYMENTS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 10, 6), _d("412.60"), "PAYMENT RECEIVED - THANK YOU"),
)
_YTD_PURCHASES: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 10, 4), _d("-73.15"), "ASHGROVE GARDEN CENTRE"),
    (date(2026, 10, 12), _d("-28.40"), "FERNDOWN BOOKSHOP"),
    (date(2026, 10, 21), _d("-116.92"), "TARNSIDE OUTDOOR SUPPLY"),
)
_YTD_FEES: Final[tuple[ExpectedRow, ...]] = ((date(2026, 10, 9), _d("-29.00"), "LATE PAYMENT FEE"),)
_YTD_ROWS: Final = (*_YTD_PAYMENTS, *_YTD_PURCHASES, *_YTD_FEES)
_YTD_PREVIOUS: Final = _d("441.60")
#: The year's figures under the fee table: neither is the period's.
YTD_TOTAL_LINES: Final = (
    ("Total fees charged in 2026", "$87.00"),
    ("Total interest charged in 2026", "$18.44"),
)


def year_to_date_totals() -> bytes:
    """A card statement: the fee table is followed by the totals of the year."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    charges = -sum((a for _, a, _ in _YTD_ROWS if a < 0), Decimal(0))
    credits = sum((a for _, a, _ in _YTD_ROWS if a > 0), Decimal(0))
    owed = _YTD_PREVIOUS - credits + charges
    _line(c, 750, [(40, "Garthmoor Platinum Visa")], size=14, bold=True)
    _line(c, 750, [(380, "Account Number: ******2754")])
    _line(c, 736, [(380, "Billing Period: 10/01/2026 - 10/31/2026")])
    _line(c, 60, [(40, _FOOTER)], size=7)
    y = _rows(
        c,
        700,
        [
            [(40, "Previous Balance"), (-300, f"${_money(_YTD_PREVIOUS)}")],
            [(40, "Payments and Credits"), (-300, f"-${_money(credits)}")],
            [(40, "New Charges"), (-300, f"+${_money(charges)}")],
            [(40, "New Balance"), (-300, f"${_money(owed)}")],
            [(40, "Minimum Payment Due"), (-300, "$25.00")],
            [(40, "Credit Limit"), (-300, "$5,500.00")],
        ],
    )
    y -= 15
    for title, rows in (
        ("Payments and Credits", _YTD_PAYMENTS),
        ("Purchases", _YTD_PURCHASES),
        ("Fees Charged", _YTD_FEES),
    ):
        y = _unsigned_table(c, y, title, rows) - 13
    _rows(c, y + 13, [[(40, label), (-560, figure)] for label, figure in YTD_TOTAL_LINES])
    c.save()
    return buf.getvalue()


EXPECTED_ROWS: Final[dict[str, list[ExpectedRow]]] = {
    "left_heading_right_figures": list(_LEFT_ROWS),
    "tight_date_checking": list(_TIGHT_ROWS),
    "tight_date_one_money_column": list(_ONE_ROWS),
    "zero_placeholder_columns": list(_ZERO_ROWS),
    "summary_table_balances": list(_BOX_ROWS),
    "summary_label_wrapped": list(_WRAP_ROWS),
    "previous_statement_balance_first": list(_PREVIOUS_ROWS),
    "checking_with_card_note": list(_NOTE_ROWS),
    "other_current_account": list(_CURRENT_ROWS),
    "other_card_ending_in": list(_CARD_ROWS),
    "card_paid_from_checking": list(_CARD_ROWS),
    "sectioned_unsigned_checking": list(_SECTION_ROWS),
    "sectioned_repeat_without_title": list(_SECTION_ROWS),
    "card_unsigned_payments": list(_UNSIGNED_CARD_ROWS),
    "date_once_per_day": list(_DAILY_ROWS),
    "summary_lines_under_table": list(_UNDER_ROWS),
    "titled_sections_without_headers": list(_HEADERLESS_WITHDRAWALS),
    "section_total_off": [row for row in _SECTION_ROWS if row != SECTION_ROW_OFF],
    "newest_first_checking": list(_NEWEST_ROWS),
    "year_to_date_totals": list(_YTD_ROWS),
}

#: ``(opening, closing)`` of the builders that are about balances.
EXPECTED_BALANCES: Final[dict[str, tuple[Decimal, Decimal]]] = {
    "summary_table_balances": (_BOX_OPENING, _running(_BOX_OPENING, _BOX_ROWS)[-1]),
    "summary_label_wrapped": (_WRAP_OPENING, _running(_WRAP_OPENING, _WRAP_ROWS)[-1]),
    "previous_statement_balance_first": (
        _PREVIOUS_OPENING,
        _running(_PREVIOUS_OPENING, _PREVIOUS_ROWS)[-1],
    ),
    "sectioned_unsigned_checking": (
        _SECTION_OPENING,
        _running(_SECTION_OPENING, _SECTION_ROWS)[-1],
    ),
    "newest_first_checking": (_NEWEST_OPENING, _running(_NEWEST_OPENING, _NEWEST_ROWS)[-1]),
}

#: Per share of :func:`shares_opening_under_deposit`: ``(last4, opening, closing, rows)``.
EXPECTED_SHARES: Final[list[tuple[str, Decimal, Decimal, list[ExpectedRow]]]] = [
    (last4, opening, _running(opening, rows)[-1], list(rows)) for _, last4, opening, rows in _SHARES
]
