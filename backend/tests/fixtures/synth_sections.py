# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statements whose sections are printed with a title and no header row.

Each builder draws one PDF as :mod:`tests.fixtures.synth_bank_layouts` does;
every institution, payee, number and figure is invented. Each prints a
masked account number, a labelled period, an opening and a closing balance
and, unless it says otherwise, rows that reconcile:

* :func:`headerless_sections_checking`: two pages and no header anywhere. A
  summary, then ``DEPOSITS & CREDITS`` over four lines of a date, text and a
  figure and ``Total Deposits & Credits``; ``WITHDRAWALS`` over seven such
  lines, the last three on page 2 under ``WITHDRAWALS (CONTINUED)``, and
  ``Total Withdrawals``. Every figure is printed without a sign.
* :func:`headerless_total_off`: the same with one deposit line left off the
  page; the totals and the balances still count it.
* :func:`headerless_card`: a card with no header anywhere. ``Payments and
  Other Credits`` is the last line of page 1; its one line (two dates, the
  payment printed with a minus) and its total head page 2, then
  ``Purchases and Adjustments`` over five lines and its total.
* :func:`dated_notices_under_a_title`: a headed table that reconciles,
  then ``Fees`` over three sentences that each start with a date and end
  with a figure (more than thirty words each) and ``Deposits`` over five
  lines of a date and a balance with no words. The blocks stand under the
  table, where its columns would read them: none of their lines is a row.

Tables printed across the page, and titles printed with their totals:

* :func:`checks_listed_twice`: ``Date | Check No. | Description | Amount``
  over twelve signed rows, four of them checks; then ``Summary of checks``
  over ``Number | Date | Amount`` printed twice across, listing the same
  four once more.
* :func:`checks_only_across`: the same without the four check rows in the
  first table. The balances are unchanged, so the statement adds up only
  with the four checks of the second table read.
* :func:`titles_with_totals`: ``Deposits / Credits | 3 items totaling
  $...``, ``Withdrawals / Debits | 5 items totaling $...`` and ``Checks |
  4 checks totaling $...``, each over its header, every figure printed
  without a sign. The withdrawals run on to page 2 under ``Withdrawals /
  Debits - continued``; the checks are printed three across, one of them
  with no number.
* :func:`titles_with_totals_one_missing`: the same with one withdrawal left
  off the page; the title's total and the balances still count it.

Accounts told apart by their summaries only:

* :func:`two_accounts_one_number`: a checking and a savings account that
  both print ``Account Number: ******0000``. Page 1 has an overview table
  of the two, ``CHECKING SUMMARY`` with its balances and totals and two
  titled tables of unsigned figures; page 2 has ``SAVINGS SUMMARY`` with
  its own balances over ``Date | Description | Amount | Balance``, whose
  first and last lines are undated ``Beginning Balance`` and ``Ending
  Balance``. Each account adds up by itself (:data:`EXPECTED_ACCOUNTS`).
* :func:`two_accounts_named_above`: the same two accounts; the title of
  each summary is ``Statement period activity summary`` and the product's
  name (``Thistlebeck Everyday Checking``, ``Thistlebeck Premier Savings``)
  is printed over it.
* :func:`two_accounts_unnamed`: the same two accounts under ``PRIMARY
  ACCOUNT SUMMARY`` and ``SECOND ACCOUNT SUMMARY``: no title says which is
  the checking account.
* :func:`summary_printed_twice`: one account whose summary is printed again
  at the head of page 2, with the same figures, over the rest of its rows.

What the first page's summary names:

* :func:`card_named_by_its_summary`: a card that prints no card word at all
  (no ``credit card``, ``card ending``, ``credit limit``, ``minimum
  payment``). Its summary is the card's equation (``Previous Balance``,
  ``Payments and Credits``, ``Purchases``, ``New Balance``) over ``Date |
  Description | Amount``: five purchases printed without a sign and one
  payment with a minus. The figures add up read as a bank statement too,
  with every sign the wrong way round.
* :func:`loan_bill_with_activity`: a lender's bill, no statement of a
  deposit account: ``Total Amount Due``, ``Payment Due Date`` and ``Current
  Principal Balance``, a table ``Date | Activity | Amount`` of two lines
  and a payment coupon. It prints no opening or closing balance.
* :func:`member_statement_with_a_loan_share`: the three shares of
  :func:`tests.fixtures.synth_bank_layouts.credit_union_member_statement`
  drawn again, with a fourth sub-account ``AUTO LOAN ID 0060`` under them
  that prints a ``Principal Balance`` and an ``Amount Due`` and has no
  table. The only deposit labels on the page are the dated ``Beginning
  Balance`` and ``Ending Balance`` lines inside the shares' tables.

Lines of a card's section that print no date:

* :func:`card_interest_lines`: a card whose table holds its sections under
  their titles. ``Fees`` prints one dated fee and ``TOTAL FEES FOR THIS
  PERIOD``; ``Interest Charged`` prints ``Interest Charge on Purchases``
  with a figure and no date, ``Interest Charge on Cash Advances 0.00`` and
  ``TOTAL INTEREST FOR THIS PERIOD``; then a year-to-date block (``Total
  fees charged in 2026``, ``Total interest charged in 2026``) with other
  figures. The new balance counts the interest: the statement adds up only
  with the undated interest line read as a row.

:data:`EXPECTED_ROWS` gives, per builder, the rows a correct reading returns
(``(posted, amount, payee)``, money out negative).
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import letter
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.pdfgen import canvas

from tests.fixtures.synth_bank_layouts import (
    _FOOTER,
    _MEMBER_SHARES,
    _MEMBER_TEXT_X,
    MEMBER_PERIOD,
    ExpectedRow,
    _d,
    _head,
    _money,
    _running,
    _signed_rows,
    _top,
    _total,
    _unsigned_table,
)
from tests.fixtures.synth_bank_layouts import _total_line as _dollar_total_line
from tests.fixtures.synth_unseen_layouts import _line, _rows

# -- a checking statement with no header anywhere --------------------------------

_CHECKING_OPENING: Final = _d("2,640.15")
_CHECKING_DEPOSITS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 10, 2), _d("1,815.00"), "Payroll Hazelrigg Cooperage"),
    (date(2026, 10, 9), _d("120.00"), "Mobile Deposit"),
    (date(2026, 10, 16), _d("1,815.00"), "Payroll Hazelrigg Cooperage"),
    (date(2026, 10, 27), _d("42.75"), "Refund Sedgewick Lane Books"),
)
_CHECKING_WITHDRAWALS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 10, 1), _d("-1,340.00"), "Rent Pennywell Terrace"),
    (date(2026, 10, 5), _d("-64.18"), "Card Purchase Barleymow Grocer"),
    (date(2026, 10, 8), _d("-118.40"), "Bill Pay Tarnside Electric"),
    (date(2026, 10, 13), _d("-60.00"), "ATM Cash Ropewalk Square"),
    (date(2026, 10, 19), _d("-37.25"), "Card Purchase Fernhollow Cafe"),
    (date(2026, 10, 23), _d("-210.90"), "Bill Pay Ousebridge Insurance"),
    (date(2026, 10, 29), _d("-15.00"), "Monthly Service Charge"),
)
#: The withdrawals printed on page 1; the rest are under the title printed again.
_CHECKING_FIRST_PAGE: Final = 4
_CHECKING_BANK: Final = "Dunnockshaw Savings Bank"
_CHECKING_PERIOD: Final = "10/01/2026 - 10/31/2026"


def _led_lines(rows: Sequence[ExpectedRow]) -> list[list[tuple[float, str]]]:
    """``MM/DD``, the text and the figure with no sign, one line per row."""
    return [
        [(40, posted.strftime("%m/%d")), (110, payee), (-560, _money(amount))]
        for posted, amount, payee in rows
    ]


def _total_line(c: canvas.Canvas, y: float, label: str, rows: Sequence[ExpectedRow]) -> None:
    _line(c, y, [(40, label), (-560, _money(_total(rows)))], bold=True)


def _headerless_checking(*, omit: ExpectedRow | None = None) -> bytes:
    """``omit`` is a deposit left off the page; the totals and balances count it."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    everything = (*_CHECKING_DEPOSITS, *_CHECKING_WITHDRAWALS)
    closing = _running(_CHECKING_OPENING, everything)[-1]
    _top(c, _CHECKING_BANK, "2946", _CHECKING_PERIOD)
    _rows(
        c,
        700,
        [
            [(40, "Beginning Balance"), (-300, f"${_money(_CHECKING_OPENING)}")],
            [(40, "Deposits & Credits"), (-300, _money(_total(_CHECKING_DEPOSITS)))],
            [(40, "Withdrawals"), (-300, _money(_total(_CHECKING_WITHDRAWALS)))],
            [(40, "Ending Balance"), (-300, f"${_money(closing)}")],
        ],
    )
    _line(c, 630, [(40, "DEPOSITS & CREDITS")], size=10, bold=True)
    y = _rows(c, 614, _led_lines([row for row in _CHECKING_DEPOSITS if row != omit]))
    _total_line(c, y, "Total Deposits & Credits", _CHECKING_DEPOSITS)
    y -= 30
    _line(c, y, [(40, "WITHDRAWALS")], size=10, bold=True)
    _rows(c, y - 16, _led_lines(_CHECKING_WITHDRAWALS[:_CHECKING_FIRST_PAGE]))
    c.showPage()
    _top(c, _CHECKING_BANK, "2946", _CHECKING_PERIOD)
    _line(c, 700, [(40, "WITHDRAWALS (CONTINUED)")], size=10, bold=True)
    y = _rows(c, 684, _led_lines(_CHECKING_WITHDRAWALS[_CHECKING_FIRST_PAGE:]))
    _total_line(c, y, "Total Withdrawals", _CHECKING_WITHDRAWALS)
    c.save()
    return buf.getvalue()


def headerless_sections_checking() -> bytes:
    """Four deposits and seven withdrawals under their titles, no header row."""
    return _headerless_checking()


#: The deposit :func:`headerless_total_off` leaves off the page.
TOTAL_OFF_OMITTED: Final = _CHECKING_DEPOSITS[1]


def headerless_total_off() -> bytes:
    """One deposit is missing: the deposits are not their printed total."""
    return _headerless_checking(omit=TOTAL_OFF_OMITTED)


# -- a card with no header anywhere ----------------------------------------------

_CARD_PREVIOUS: Final = _d("812.40")
#: The payment: its two reference numbers are printed after the text.
_CARD_PAYMENT: Final[ExpectedRow] = (
    date(2026, 8, 14),
    _d("240.00"),
    "PAYMENT RECEIVED - THANK YOU 5521 0098",
)
_CARD_PURCHASES: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 8, 3), _d("-58.12"), "MARLPIT HARDWARE 4410 7702"),
    (date(2026, 8, 9), _d("-23.80"), "COWSLIP LANE BAKERY 4410 7719"),
    (date(2026, 8, 17), _d("-142.65"), "STANEGATE OUTFITTERS 4410 7733"),
    (date(2026, 8, 22), _d("-9.99"), "TALLOWBY STREAMING 4410 7748"),
    (date(2026, 8, 28), _d("-76.30"), "WHINFELL FUEL STOP 4410 7760"),
)
_CARD_ISSUER: Final = "Gorsebank Card Services"
_CARD_PERIOD: Final = "08/01/2026 - 08/31/2026"


def _card_line(row: ExpectedRow, *, minus: bool) -> list[tuple[float, str]]:
    """The transaction date, the posting date a day later, the text, the figure."""
    posted, amount, payee = row
    after = posted.replace(day=posted.day + 1)
    figure = f"-{_money(amount)}" if minus else _money(amount)
    return [
        (40, posted.strftime("%m/%d")),
        (80, after.strftime("%m/%d")),
        (125, payee),
        (-560, figure),
    ]


def headerless_card() -> bytes:
    """One payment (title on page 1, line on page 2) and five purchases."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    purchases = _total(_CARD_PURCHASES)
    new_balance = _CARD_PREVIOUS - _CARD_PAYMENT[1] + purchases
    _top(c, _CARD_ISSUER, "4417", _CARD_PERIOD)
    _rows(
        c,
        700,
        [
            [(40, "Credit Limit"), (-300, "$6,000.00")],
            [(40, "Previous Balance"), (-300, f"${_money(_CARD_PREVIOUS)}")],
            [(40, "Payments and Other Credits"), (-300, f"-${_money(_CARD_PAYMENT[1])}")],
            [(40, "Purchases and Adjustments"), (-300, f"${_money(purchases)}")],
            [(40, "New Balance"), (-300, f"${_money(new_balance)}")],
        ],
    )
    _line(c, 90, [(40, "Payments and Other Credits")], size=10, bold=True)
    c.showPage()
    _top(c, _CARD_ISSUER, "4417", _CARD_PERIOD)
    _rows(c, 700, [_card_line(_CARD_PAYMENT, minus=True)])
    _line(
        c,
        687,
        [
            (40, "TOTAL PAYMENTS AND OTHER CREDITS FOR THIS PERIOD"),
            (-560, f"-${_money(_CARD_PAYMENT[1])}"),
        ],
        bold=True,
    )
    _line(c, 657, [(40, "Purchases and Adjustments")], size=10, bold=True)
    y = _rows(c, 641, [_card_line(row, minus=False) for row in _CARD_PURCHASES])
    _line(
        c,
        y,
        [
            (40, "TOTAL PURCHASES AND ADJUSTMENTS FOR THIS PERIOD"),
            (-560, f"${_money(purchases)}"),
        ],
        bold=True,
    )
    c.save()
    return buf.getvalue()


# -- dated lines under a title that are no rows ----------------------------------

_NOTICE_OPENING: Final = _d("1,906.32")
_NOTICE_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 11, 3), _d("1,480.00"), "Payroll Deposit Lintmill Weavers"),
    (date(2026, 11, 6), _d("-915.00"), "Rent Coldwell Row"),
    (date(2026, 11, 12), _d("-48.66"), "POS Purchase Ravensgill Market"),
    (date(2026, 11, 20), _d("-72.10"), "Skerryvore Mobile Online Pmt"),
    (date(2026, 11, 27), _d("0.41"), "Interest Credit"),
)
#: Sentences that start with a date and end with a figure: notices, not rows.
NOTICE_SENTENCES: Final[tuple[str, ...]] = (
    "11/01 a monthly charge of the kind set out in the schedule we sent to you in the spring"
    " may be taken from an account that is kept below the floor we agreed with you of 12.00",
    "11/15 a charge for paper copies of a statement that was first sent to you more than two"
    " years ago is taken on the day we post it to you and is now for each copy 5.00",
    "11/30 a charge for a payment that we send abroad for you on a working day before noon and"
    " that is paid in the currency of the account it leaves is from now on 18.50",
)
#: ``(day, balance)``: balances by day, a date and a figure with no words.
_NOTICE_BALANCES: Final[tuple[tuple[str, str], ...]] = (
    ("11/03", "3,386.32"),
    ("11/06", "2,471.32"),
    ("11/12", "2,422.66"),
    ("11/20", "2,350.56"),
    ("11/27", "2,350.97"),
)


def _notice_blocks(c: canvas.Canvas, y: float) -> float:
    """``Fees`` over the sentences, ``Deposits`` over the balances by day; the next y."""
    _line(c, y, [(40, "Fees")], size=10, bold=True)
    y -= 16
    for sentence in NOTICE_SENTENCES:
        _line(c, y, [(40, sentence)], size=6)
        y -= 13
    y -= 13
    _line(c, y, [(40, "Deposits")], size=10, bold=True)
    return _rows(c, y - 16, [[(40, day), (-560, balance)] for day, balance in _NOTICE_BALANCES])


def dated_notices_under_a_title() -> bytes:
    """A headed table, and under it two titled blocks of dated lines that are no rows."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_NOTICE_OPENING, _NOTICE_ROWS)[-1]
    _head(c, "Brackenthwaite Bank", "5068", "11/01/2026 - 11/30/2026", _NOTICE_OPENING, closing)
    _signed_rows(c, 650, _NOTICE_ROWS)
    # The table's header and its rows are 14 points apart.
    _notice_blocks(c, 650 - 14 * (len(_NOTICE_ROWS) + 1) - 26)
    c.save()
    return buf.getvalue()


# -- a second listing of the checks, printed across the page ---------------------

_LISTED_OPENING: Final = _d("3,512.08")
#: ``(row, check number)``: the first table's rows, the checks with their number.
_LISTED_ROWS: Final[tuple[tuple[ExpectedRow, str], ...]] = (
    ((date(2026, 9, 1), _d("1,960.00"), "Payroll Withycombe Saddlery"), ""),
    ((date(2026, 9, 2), _d("-1,125.00"), "Rent Hobbleton Mews"), ""),
    ((date(2026, 9, 4), _d("-66.00"), "Check"), "4181"),
    ((date(2026, 9, 8), _d("-43.27"), "Card Purchase Yarrowfield Grocer"), ""),
    ((date(2026, 9, 9), _d("-249.00"), "Check"), "4182"),
    ((date(2026, 9, 11), _d("-97.15"), "Bill Pay Brindlemoor Gas"), ""),
    ((date(2026, 9, 15), _d("1,960.00"), "Payroll Withycombe Saddlery"), ""),
    ((date(2026, 9, 16), _d("-80.00"), "ATM Cash Tolcarne Street"), ""),
    ((date(2026, 9, 18), _d("-312.50"), "Check"), "4184"),
    ((date(2026, 9, 22), _d("-28.90"), "Card Purchase Skelwith Bakery"), ""),
    ((date(2026, 9, 25), _d("-75.25"), "Check"), "4185"),
    ((date(2026, 9, 29), _d("0.36"), "Interest Credit"), ""),
)
#: The four checks as a reader of the second table alone returns them.
LISTED_CHECKS: Final[tuple[ExpectedRow, ...]] = tuple(
    (posted, amount, f"CHECK {number}") for (posted, amount, _), number in _LISTED_ROWS if number
)
#: Where the number and the date of each group start and where its amount ends.
_LISTED_GROUPS: Final = ((40.0, 95.0, 215.0), (300.0, 355.0, 475.0))


def _checks_across(
    c: canvas.Canvas,
    y: float,
    checks: Sequence[tuple[str, date, Decimal]],
    groups: Sequence[tuple[float, float, float]],
) -> float:
    """``Number | Date | Amount`` once per group and the checks under it,
    one per group from the left, with no sign; the next y."""
    header: list[tuple[float, str]] = []
    for number_x, date_x, amount_x in groups:
        header += [(number_x, "Number"), (date_x, "Date"), (-amount_x, "Amount")]
    _line(c, y, header, size=8)
    lines: list[list[tuple[float, str]]] = []
    for start in range(0, len(checks), len(groups)):
        cells: list[tuple[float, str]] = []
        for (number, paid, amount), (number_x, date_x, amount_x) in zip(
            checks[start : start + len(groups)], groups, strict=False
        ):
            if number:
                cells.append((number_x, number))
            cells += [(date_x, paid.strftime("%m/%d/%Y")), (-amount_x, _money(amount))]
        lines.append(cells)
    return _rows(c, y - 14, lines)


def _listed(*, in_first_table: bool) -> bytes:
    """``in_first_table``: the first table holds its four check rows."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_LISTED_OPENING, [row for row, _ in _LISTED_ROWS])[-1]
    _head(c, "Quernmore Vale Bank", "6093", "09/01/2026 - 09/30/2026", _LISTED_OPENING, closing)
    _line(
        c,
        650,
        [(40, "Date"), (110, "Check No."), (180, "Description"), (-560, "Amount")],
        size=8,
    )
    y = _rows(
        c,
        636,
        [
            [
                (40, posted.strftime("%m/%d/%Y")),
                (110, number),
                (180, payee),
                (-560, f"{amount:,.2f}"),
            ]
            for (posted, amount, payee), number in _LISTED_ROWS
            if in_first_table or not number
        ],
    )
    y -= 15
    _line(c, y, [(40, "Summary of checks")], size=10, bold=True)
    checks = [(number, posted, amount) for (posted, amount, _), number in _LISTED_ROWS if number]
    _checks_across(c, y - 16, checks, _LISTED_GROUPS)
    c.save()
    return buf.getvalue()


def checks_listed_twice() -> bytes:
    """Twelve rows, four of them checks, and the four listed again two across."""
    return _listed(in_first_table=True)


def checks_only_across() -> bytes:
    """Eight rows, and four checks that only their own table prints."""
    return _listed(in_first_table=False)


# -- titles printed with their count and total -----------------------------------

_TITLED_OPENING: Final = _d("2,204.60")
_TITLED_DEPOSITS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 7, 1), _d("1,250.00"), "Payroll Appletreewick Forge"),
    (date(2026, 7, 14), _d("480.00"), "Transfer From Savings"),
    (date(2026, 7, 28), _d("140.00"), "Mobile Deposit"),
)
_TITLED_WITHDRAWALS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 7, 2), _d("-610.00"), "Rent Cuddington Close"),
    (date(2026, 7, 7), _d("-84.15"), "Card Purchase Mossgiel Grocer"),
    (date(2026, 7, 13), _d("-126.00"), "Bill Pay Farleton Water"),
    (date(2026, 7, 20), _d("-52.25"), "Card Purchase Tansley Hardware"),
    (date(2026, 7, 27), _d("-40.00"), "ATM Cash Wetherlam Road"),
)
#: The withdrawals printed on page 1; the rest are under the title printed again.
_TITLED_FIRST_PAGE: Final = 3
#: ``(number, paid, amount)``: three to a line; the second has no number printed.
_TITLED_CHECKS: Final[tuple[tuple[str, date, Decimal], ...]] = (
    ("5207", date(2026, 7, 6), _d("215.00")),
    ("", date(2026, 7, 9), _d("96.40")),
    ("5209", date(2026, 7, 17), _d("180.60")),
    ("5210", date(2026, 7, 24), _d("148.00")),
)
_TITLED_CHECK_ROWS: Final[tuple[ExpectedRow, ...]] = tuple(
    (paid, -amount, f"CHECK {number}".strip()) for number, paid, amount in _TITLED_CHECKS
)
_TITLED_GROUPS: Final = ((40.0, 95.0, 215.0), (240.0, 295.0, 415.0), (440.0, 495.0, 575.0))
_TITLED_BANK: Final = "Ellerbeck Trust Bank"
_TITLED_PERIOD: Final = "07/01/2026 - 07/31/2026"
#: The withdrawal :func:`titles_with_totals_one_missing` leaves off the page.
TITLED_OMITTED: Final = _TITLED_WITHDRAWALS[1]


def _titled_line(c: canvas.Canvas, y: float, title: str, count: str, total: Decimal) -> None:
    """A title with its count and total beside it, as one line of two cells."""
    _line(c, y, [(40, title), (230, f"{count} totaling ${_money(total)}")], size=10, bold=True)


def _unsigned(c: canvas.Canvas, y: float, rows: Sequence[ExpectedRow]) -> float:
    """``Date | Description | Amount`` and ``rows`` with no sign; the next y."""
    _line(c, y, [(40, "Date"), (110, "Description"), (-560, "Amount")], size=8)
    return _rows(
        c,
        y - 13,
        [
            [(40, posted.strftime("%m/%d/%Y")), (110, payee), (-560, _money(amount))]
            for posted, amount, payee in rows
        ],
    )


def _titled(*, omit: ExpectedRow | None = None) -> bytes:
    """``omit`` is a withdrawal left off the page; totals and balances count it."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    everything = (*_TITLED_DEPOSITS, *_TITLED_WITHDRAWALS, *_TITLED_CHECK_ROWS)
    closing = _running(_TITLED_OPENING, everything)[-1]
    _head(c, _TITLED_BANK, "3381", _TITLED_PERIOD, _TITLED_OPENING, closing)
    _titled_line(c, 650, "Deposits / Credits", "3 items", _total(_TITLED_DEPOSITS))
    y = _unsigned(c, 634, _TITLED_DEPOSITS)
    y -= 17
    _titled_line(c, y, "Withdrawals / Debits", "5 items", _total(_TITLED_WITHDRAWALS))
    first = [row for row in _TITLED_WITHDRAWALS[:_TITLED_FIRST_PAGE] if row != omit]
    _unsigned(c, y - 16, first)
    c.showPage()
    _top(c, _TITLED_BANK, "3381", _TITLED_PERIOD)
    _line(c, 700, [(40, "Withdrawals / Debits - continued")], size=10, bold=True)
    y = _unsigned(c, 684, _TITLED_WITHDRAWALS[_TITLED_FIRST_PAGE:])
    y -= 17
    _titled_line(c, y, "Checks", "4 checks", _total(_TITLED_CHECK_ROWS))
    _checks_across(c, y - 16, _TITLED_CHECKS, _TITLED_GROUPS)
    c.save()
    return buf.getvalue()


def titles_with_totals() -> bytes:
    """Three deposits, five withdrawals and four checks under titles with totals."""
    return _titled()


def titles_with_totals_one_missing() -> bytes:
    """One withdrawal is missing: the withdrawals are not their title's total."""
    return _titled(omit=TITLED_OMITTED)


# -- two accounts under one masked number ----------------------------------------

_PAIR_BANK: Final = "Thistlebeck Mutual Bank"
_PAIR_PERIOD: Final = "11/01/2026 - 11/30/2026"
_PAIR_CHECKING_OPENING: Final = _d("3,412.60")
_PAIR_DEPOSITS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 11, 3), _d("1,640.00"), "Payroll Garsdale Joinery"),
    (date(2026, 11, 10), _d("85.00"), "Mobile Deposit"),
    (date(2026, 11, 17), _d("1,640.00"), "Payroll Garsdale Joinery"),
    (date(2026, 11, 24), _d("31.45"), "Refund Lowther Street Books"),
)
_PAIR_WITHDRAWALS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 11, 2), _d("-1,180.00"), "Rent Skelwith Court"),
    (date(2026, 11, 5), _d("-400.00"), "Transfer To Savings"),
    (date(2026, 11, 9), _d("-96.32"), "Bill Pay Brantfell Electric"),
    (date(2026, 11, 18), _d("-58.76"), "Card Purchase Yewdale Grocer"),
    (date(2026, 11, 27), _d("-22.50"), "Card Purchase Nab Scar Cafe"),
)
_PAIR_CHECKING_ROWS: Final = (*_PAIR_DEPOSITS, *_PAIR_WITHDRAWALS)
_PAIR_SAVINGS_OPENING: Final = _d("18,250.00")
_PAIR_SAVINGS_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 11, 5), _d("400.00"), "Transfer From Checking"),
    (date(2026, 11, 17), _d("-150.00"), "Withdrawal Branch Teller"),
    (date(2026, 11, 28), _d("3.18"), "Interest Paid"),
)
#: Per account of :func:`two_accounts_one_number`: opening, closing, its rows.
EXPECTED_ACCOUNTS: Final[tuple[tuple[Decimal, Decimal, list[ExpectedRow]], ...]] = (
    (
        _PAIR_CHECKING_OPENING,
        _running(_PAIR_CHECKING_OPENING, _PAIR_CHECKING_ROWS)[-1],
        list(_PAIR_CHECKING_ROWS),
    ),
    (
        _PAIR_SAVINGS_OPENING,
        _running(_PAIR_SAVINGS_OPENING, _PAIR_SAVINGS_ROWS)[-1],
        list(_PAIR_SAVINGS_ROWS),
    ),
)


def two_accounts_one_number() -> bytes:
    """A checking and a savings account, each with its summary, one masked number."""
    return _two_accounts(("CHECKING SUMMARY",), ("SAVINGS SUMMARY",))


def two_accounts_named_above() -> bytes:
    """The same two accounts; each summary's own title names no kind of
    account and the product's name is printed over it."""
    summary = "Statement period activity summary"
    return _two_accounts(
        ("Thistlebeck Everyday Checking", summary), ("Thistlebeck Premier Savings", summary)
    )


def two_accounts_unnamed() -> bytes:
    """The same two accounts under titles that name no kind of account."""
    return _two_accounts(("PRIMARY ACCOUNT SUMMARY",), ("SECOND ACCOUNT SUMMARY",))


def _titles(c: canvas.Canvas, y: float, titles: Sequence[str]) -> None:
    """``titles`` one over the other, the last of them at ``y``."""
    for position, title in enumerate(reversed(titles)):
        _line(c, y + 14 * position, [(40, title)], size=10, bold=True)


def _two_accounts(first: Sequence[str], second: Sequence[str]) -> bytes:
    """Two accounts under one masked number; ``first`` and ``second`` are the
    lines printed over the summary of each."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    (checking_open, checking_close, _), (savings_open, savings_close, _) = EXPECTED_ACCOUNTS
    _top(c, _PAIR_BANK, "0000", _PAIR_PERIOD)
    _line(c, 712, [(40, "Your accounts at a glance")], size=10, bold=True)
    _line(c, 698, [(40, "Account"), (-420, "Beginning Balance"), (-560, "Ending Balance")], size=8)
    _rows(
        c,
        685,
        [
            [
                (40, "Everyday Checking"),
                (-420, _money(checking_open)),
                (-560, _money(checking_close)),
            ],
            [(40, "Premier Savings"), (-420, _money(savings_open)), (-560, _money(savings_close))],
        ],
    )
    _titles(c, 640, first)
    y = _rows(
        c,
        626,
        [
            [(40, "Beginning Balance"), (-300, f"${_money(checking_open)}")],
            [(40, "Deposits and Additions"), (-300, _money(_total(_PAIR_DEPOSITS)))],
            [(40, "Electronic Withdrawals"), (-300, _money(_total(_PAIR_WITHDRAWALS)))],
            [(40, "Ending Balance"), (-300, f"${_money(checking_close)}")],
        ],
    )
    y = _unsigned_table(c, y - 15, "DEPOSITS AND ADDITIONS", _PAIR_DEPOSITS)
    y = _dollar_total_line(c, y, "Total Deposits and Additions", _PAIR_DEPOSITS)
    y = _unsigned_table(c, y, "ELECTRONIC WITHDRAWALS", _PAIR_WITHDRAWALS)
    _dollar_total_line(c, y, "Total Electronic Withdrawals", _PAIR_WITHDRAWALS)
    c.showPage()
    _top(c, _PAIR_BANK, "0000", _PAIR_PERIOD)
    _titles(c, 700, second)
    _rows(
        c,
        686,
        [
            [(40, "Beginning Balance"), (-300, f"${_money(savings_open)}")],
            [(40, "Ending Balance"), (-300, f"${_money(savings_close)}")],
        ],
    )
    _line(
        c,
        640,
        [(40, "Date"), (110, "Description"), (-460, "Amount"), (-560, "Balance")],
        size=8,
    )
    balances = _running(savings_open, _PAIR_SAVINGS_ROWS)
    _rows(
        c,
        626,
        [
            [(110, "Beginning Balance"), (-560, _money(savings_open))],
            *(
                [
                    (40, posted.strftime("%m/%d/%Y")),
                    (110, payee),
                    (-460, f"{amount:,.2f}"),
                    (-560, _money(balance)),
                ]
                for (posted, amount, payee), balance in zip(
                    _PAIR_SAVINGS_ROWS, balances, strict=True
                )
            ),
            [(110, "Ending Balance"), (-560, _money(savings_close))],
        ],
    )
    c.save()
    return buf.getvalue()


_TWICE_OPENING: Final = _d("1,905.40")
_TWICE_ROWS: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 12, 1), _d("-725.00"), "Rent Holmrook Gardens"),
    (date(2026, 12, 4), _d("1,310.00"), "Payroll Eskmeals Pottery"),
    (date(2026, 12, 8), _d("-48.90"), "Card Purchase Ravenglass Grocer"),
    (date(2026, 12, 11), _d("-112.35"), "Bill Pay Muncaster Water"),
    (date(2026, 12, 18), _d("1,310.00"), "Payroll Eskmeals Pottery"),
    (date(2026, 12, 22), _d("-80.00"), "ATM Cash Drigg Road"),
    (date(2026, 12, 29), _d("-19.75"), "Card Purchase Seascale Cafe"),
)
#: The rows printed on page 1; the rest stand under the summary printed again.
_TWICE_FIRST_PAGE: Final = 4


def summary_printed_twice() -> bytes:
    """One account; page 2 starts with its summary once more, the same figures."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    closing = _running(_TWICE_OPENING, _TWICE_ROWS)[-1]
    for page, rows in enumerate((_TWICE_ROWS[:_TWICE_FIRST_PAGE], _TWICE_ROWS[_TWICE_FIRST_PAGE:])):
        if page:
            c.showPage()
        _top(c, "Calderbridge Savings Bank", "5127", "12/01/2026 - 12/31/2026")
        _line(c, 712, [(40, "CHECKING SUMMARY")], size=10, bold=True)
        _rows(
            c,
            698,
            [
                [(40, "Beginning Balance"), (-300, f"${_money(_TWICE_OPENING)}")],
                [(40, "Ending Balance"), (-300, f"${_money(closing)}")],
            ],
        )
        _signed_rows(c, 650, rows)
    c.save()
    return buf.getvalue()


# -- what the first page's summary names ------------------------------------------

_EQUATION_PREVIOUS: Final = _d("412.60")
_EQUATION_PAYMENT: Final[ExpectedRow] = (
    date(2026, 9, 12),
    _d("300.00"),
    "PAYMENT RECEIVED - THANK YOU",
)
_EQUATION_PURCHASES: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 9, 3), _d("-46.25"), "FELLFOOT IRONMONGER"),
    (date(2026, 9, 8), _d("-18.90"), "DRYBECK DAIRY"),
    (date(2026, 9, 15), _d("-132.40"), "CRAGSIDE CYCLES"),
    (date(2026, 9, 21), _d("-7.49"), "MOSSDALE PARKING"),
    (date(2026, 9, 27), _d("-61.36"), "HAWTHWAITE GROCER"),
)
#: The rows in the order printed: by date, the payment among the purchases.
_EQUATION_ROWS: Final = tuple(sorted((*_EQUATION_PURCHASES, _EQUATION_PAYMENT)))
#: What the card owes at the end of :func:`card_named_by_its_summary`.
EQUATION_NEW_BALANCE: Final = (
    _EQUATION_PREVIOUS - _EQUATION_PAYMENT[1] + _total(_EQUATION_PURCHASES)
)


def card_named_by_its_summary() -> bytes:
    """A card known by its summary alone: purchases unsigned, the payment minus."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    purchases = _total(_EQUATION_PURCHASES)
    _top(c, "Hobbleshaw Finance", "3085", "09/01/2026 - 09/30/2026")
    _rows(
        c,
        700,
        [
            [(40, "Previous Balance"), (-300, f"${_money(_EQUATION_PREVIOUS)}")],
            [(40, "Payments and Credits"), (-300, f"-${_money(_EQUATION_PAYMENT[1])}")],
            [(40, "Purchases"), (-300, f"${_money(purchases)}")],
            [(40, "New Balance"), (-300, f"${_money(EQUATION_NEW_BALANCE)}")],
        ],
    )
    _line(c, 620, [(40, "Date"), (110, "Description"), (-500, "Amount")], size=8)
    _rows(
        c,
        606,
        [
            [
                (40, posted.strftime("%m/%d/%Y")),
                (110, payee),
                # A purchase is printed bare, money that came off with a minus.
                (-500, _money(amount) if amount < 0 else f"-{_money(amount)}"),
            ]
            for posted, amount, payee in _EQUATION_ROWS
        ],
    )
    c.save()
    return buf.getvalue()


def loan_bill_with_activity() -> bytes:
    """A lender's bill with a short activity table and a payment coupon."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _line(c, 750, [(40, "Skelmergh Motor Finance")], size=14, bold=True)
    _line(c, 750, [(380, "Loan Number: ******7714")])
    _line(c, 736, [(380, "Statement Date: 10/20/2026")])
    _line(c, 60, [(40, _FOOTER)], size=7)
    _rows(
        c,
        700,
        [
            [(40, "Total Amount Due"), (-300, "$318.42")],
            [(40, "Payment Due Date"), (-300, "11/15/2026")],
            [(40, "Current Principal Balance"), (-300, "$9,640.18")],
        ],
    )
    _line(c, 630, [(40, "Date"), (110, "Activity"), (-500, "Amount")], size=8)
    _rows(
        c,
        616,
        [
            [(40, "10/05/2026"), (110, "Payment Received - Thank You"), (-500, "318.42")],
            [(40, "10/12/2026"), (110, "Insurance Adjustment"), (-500, "12.50")],
        ],
    )
    _line(c, 300, [(40, "Detach and return this coupon with your payment")], size=8)
    _rows(
        c,
        282,
        [
            [(40, "Loan Number"), (-300, "******7714")],
            [(40, "Total Amount Due"), (-300, "$318.42")],
            [(40, "Amount Enclosed"), (-300, "$ __________")],
        ],
    )
    c.save()
    return buf.getvalue()


def member_statement_with_a_loan_share() -> bytes:
    """Three shares, each a table with dated balance lines, and a loan's two figures."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _line(c, 750, [(40, "Rushmere Lakes Credit Union")], size=14, bold=True)
    _line(c, 750, [(380, "Statement For: 04/01/2026 - 04/30/2026")])
    _line(c, 738, [(380, "Member Number: ******6172")])
    _line(c, 60, [(40, _FOOTER)], size=7)
    start, end = (day.strftime("%m/%d") for day in MEMBER_PERIOD)
    y = 700.0
    for title, _, _, opening, rows in _MEMBER_SHARES:
        balances = _running(opening, rows)
        _line(c, y, [(40, title[0])], size=11, bold=True)
        if len(title) > 1:
            _line(c, y, [(330, title[1]), (-550, title[2])])
        _line(
            c,
            y - 16,
            [
                # 4 points apart: the two headings are one cell.
                (40, "Date"),
                (40 + stringWidth("Date", "Helvetica", 8) + 4, "Transaction Description"),
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
                [(40, start), (_MEMBER_TEXT_X, "Beginning Balance"), (-550, _money(opening))],
                *(
                    [
                        (40, posted.strftime("%m/%d")),
                        (_MEMBER_TEXT_X, payee),
                        (-400 if amount < 0 else -470, _money(amount)),
                        (-550, _money(balance)),
                    ]
                    for (posted, amount, payee), balance in zip(rows, balances, strict=True)
                ),
                [(40, end), (_MEMBER_TEXT_X, "Ending Balance"), (-550, _money(balances[-1]))],
            ],
        )
        y -= 17
    _line(c, y, [(40, "AUTO LOAN ID 0060")], size=11, bold=True)
    _rows(
        c,
        y - 16,
        [
            [(40, "Principal Balance"), (-300, "8,412.60")],
            [(40, "Amount Due"), (-300, "265.00")],
        ],
    )
    c.save()
    return buf.getvalue()


# -- a card whose interest is printed without a date -----------------------------

_INTEREST_PREVIOUS: Final = _d("1,204.75")
_INTEREST_PAYMENT: Final[ExpectedRow] = (
    date(2026, 6, 11),
    _d("450.00"),
    "PAYMENT RECEIVED - THANK YOU",
)
_INTEREST_PURCHASES: Final[tuple[ExpectedRow, ...]] = (
    (date(2026, 6, 4), _d("-37.60"), "ELTERBURN GARDEN SUPPLY"),
    (date(2026, 6, 9), _d("-112.05"), "KESTLEWICK TYRES"),
    (date(2026, 6, 18), _d("-14.99"), "ROWANFOLD NEWSAGENT"),
    (date(2026, 6, 25), _d("-68.44"), "SWINSTY FARM SHOP"),
)
_INTEREST_FEE: Final[ExpectedRow] = (date(2026, 6, 22), _d("-25.00"), "LATE FEE")
#: The period of :func:`card_interest_lines`; its last day dates the interest.
INTEREST_PERIOD: Final = (date(2026, 6, 1), date(2026, 6, 30))
#: The interest line printed without a date, as a correct reading returns it.
INTEREST_ROW: Final[ExpectedRow] = (
    INTEREST_PERIOD[1],
    _d("-6.20"),
    "Interest Charge on Purchases",
)
#: The line that charges nothing: never a row.
INTEREST_ZERO_LINE: Final = "Interest Charge on Cash Advances"
#: The year-to-date figures: totals of another span of time, never rows.
_INTEREST_YEAR_FEES: Final = _d("75.00")
_INTEREST_YEAR_INTEREST: Final = _d("41.30")
#: What the card owes at the end of :func:`card_interest_lines`.
INTEREST_NEW_BALANCE: Final = (
    _INTEREST_PREVIOUS
    - _INTEREST_PAYMENT[1]
    + _total(_INTEREST_PURCHASES)
    + abs(_INTEREST_FEE[1])
    + abs(INTEREST_ROW[1])
)


def _card_table_rows(rows: Sequence[ExpectedRow], *, minus: bool) -> list[list[tuple[float, str]]]:
    """Dated lines of a card's table: the figure bare, or with a minus (money in)."""
    return [
        [
            (40, posted.strftime("%m/%d/%Y")),
            (110, payee),
            (-500, f"-{_money(amount)}" if minus else _money(amount)),
        ]
        for posted, amount, payee in rows
    ]


def card_interest_lines() -> bytes:
    """A card that adds up only with its undated interest line read as a row."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    purchases = _total(_INTEREST_PURCHASES)
    start, end = (day.strftime("%m/%d/%Y") for day in INTEREST_PERIOD)
    _top(c, "Yewdale Card Company", "5126", f"{start} - {end}")
    _rows(
        c,
        700,
        [
            [(40, "Credit Limit"), (-300, "$4,500.00")],
            [(40, "Previous Balance"), (-300, f"${_money(_INTEREST_PREVIOUS)}")],
            [(40, "Payments and Other Credits"), (-300, f"-${_money(_INTEREST_PAYMENT[1])}")],
            [(40, "Purchases and Adjustments"), (-300, f"${_money(purchases)}")],
            [(40, "Fees Charged"), (-300, f"${_money(_INTEREST_FEE[1])}")],
            [(40, "Interest Charged"), (-300, f"${_money(INTEREST_ROW[1])}")],
            [(40, "New Balance"), (-300, f"${_money(INTEREST_NEW_BALANCE)}")],
        ],
    )
    _line(c, 596, [(40, "Date"), (110, "Description"), (-500, "Amount")], size=8)
    y = 582.0
    _line(c, y, [(40, "Payments and Other Credits")], size=10, bold=True)
    y = _rows(c, y - 14, _card_table_rows([_INTEREST_PAYMENT], minus=True))
    _line(c, y - 4, [(40, "Purchases and Adjustments")], size=10, bold=True)
    y = _rows(c, y - 18, _card_table_rows(_INTEREST_PURCHASES, minus=False))
    _line(c, y - 4, [(40, "Fees")], size=10, bold=True)
    y = _rows(c, y - 18, _card_table_rows([_INTEREST_FEE], minus=False))
    _line(
        c,
        y,
        [(110, "TOTAL FEES FOR THIS PERIOD"), (-500, _money(_INTEREST_FEE[1]))],
        bold=True,
    )
    y -= 17
    _line(c, y, [(40, "Interest Charged")], size=10, bold=True)
    y = _rows(
        c,
        y - 14,
        [
            [(110, INTEREST_ROW[2]), (-500, _money(INTEREST_ROW[1]))],
            [(110, INTEREST_ZERO_LINE), (-500, "0.00")],
        ],
    )
    _line(
        c,
        y,
        [(110, "TOTAL INTEREST FOR THIS PERIOD"), (-500, _money(INTEREST_ROW[1]))],
        bold=True,
    )
    y -= 17
    _line(c, y, [(40, "2026 Totals Year-to-Date")], size=10, bold=True)
    _rows(
        c,
        y - 14,
        [
            [(110, "Total fees charged in 2026"), (-500, _money(_INTEREST_YEAR_FEES))],
            [(110, "Total interest charged in 2026"), (-500, _money(_INTEREST_YEAR_INTEREST))],
        ],
    )
    c.save()
    return buf.getvalue()


EXPECTED_ROWS: Final[dict[str, list[ExpectedRow]]] = {
    "headerless_sections_checking": [*_CHECKING_DEPOSITS, *_CHECKING_WITHDRAWALS],
    "headerless_total_off": list(_CHECKING_WITHDRAWALS),
    "headerless_card": [_CARD_PAYMENT, *_CARD_PURCHASES],
    "dated_notices_under_a_title": list(_NOTICE_ROWS),
    "checks_listed_twice": [row for row, _ in _LISTED_ROWS],
    "checks_only_across": [*(row for row, number in _LISTED_ROWS if not number), *LISTED_CHECKS],
    "titles_with_totals": [*_TITLED_DEPOSITS, *_TITLED_WITHDRAWALS, *_TITLED_CHECK_ROWS],
    "titles_with_totals_one_missing": [
        *_TITLED_DEPOSITS,
        *(row for row in _TITLED_WITHDRAWALS if row != TITLED_OMITTED),
        *_TITLED_CHECK_ROWS,
    ],
    "summary_printed_twice": list(_TWICE_ROWS),
    "card_named_by_its_summary": list(_EQUATION_ROWS),
    "card_interest_lines": [
        _INTEREST_PAYMENT,
        *_INTEREST_PURCHASES,
        _INTEREST_FEE,
        INTEREST_ROW,
    ],
}
