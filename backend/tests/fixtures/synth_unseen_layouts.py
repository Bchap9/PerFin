# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statements in layouts no fingerprint or institution parser knows.

Each generator draws one PDF the generic readers have to read without help,
the way a household's first statement from a new bank arrives. They are
shaped like the layouts people actually receive and every name, number and
figure in them is invented:

* :func:`summary_block_checking`: a summary block, ``MM/DD`` rows without a
  year, a running balance, the period printed without a label and the
  account number in one long run of digits
  (:func:`summary_block_checking_next` is the same account a month on).
* :func:`sectioned_checking`: deposits, withdrawals and checks in separate
  tables, and the account number printed in **groups** (``4410 0092
  6654``): its last four digits are those of the last group.
* :func:`card_statement`: charges and payments in separate tables, a
  closing date instead of a period, ``Account Ending 4-71009``.
* :func:`sterling_current_account`: day-first rows (``12 Jan``), ``Money
  out`` / ``Money in`` columns, amounts in pounds, a sort code beside the
  account number, and a balance **brought forward** at the top of the
  second page (the same account continuing, not another one).
* :func:`credit_union_shares`: two shares of one member, each under its own
  title with its own opening and closing balance row and no account number
  of its own (``PRIMARY SAVINGS - ID 0000``, ``FREE CHECKING - ID 0010``);
  the member number is printed once for both.

:data:`EXPECTED` gives, per generator, what a correct reading returns.
"""

from __future__ import annotations

import io
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from reportlab.lib.pagesizes import A4, letter
from reportlab.pdfgen import canvas

#: ``(x, text)`` drawn from the left, or ``(-x, text)`` drawn up to ``x``.
Cell = tuple[float, str]
Line = Sequence[Cell]


def _line(c: canvas.Canvas, y: float, cells: Line, *, size: float = 9, bold: bool = False) -> None:
    c.setFont("Helvetica-Bold" if bold else "Helvetica", size)
    for x, text in cells:
        if x < 0:
            c.drawRightString(-x, y, text)
        else:
            c.drawString(x, y, text)


def _rows(c: canvas.Canvas, y: float, rows: Sequence[Line], *, step: float = 13) -> float:
    for cells in rows:
        _line(c, y, cells)
        y -= step
    return y


_CARD: Final = "Springfield IL Card 7731"
#: ``(date, description, amount, balance)`` rows of the January statement.
_JANUARY_ROWS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("01/15", f"Card Purchase 01/14 Corner Grocer #204 {_CARD}", "-64.18", "2,354.59"),
    ("01/16", "Gusto Pay 123456 PPD ID: 9138864007", "1,825.00", "4,179.59"),
    ("01/20", f"Card Purchase With Pin 01/19 Lantern Fuel {_CARD}", "-41.07", "4,138.52"),
    ("01/22", "Oakfield Rentals Web Pmts 7Q2K1X Web ID: 1234567890", "-1,450.00", "2,688.52"),
    ("01/27", f"ATM Withdrawal 01/27 100 Main St {_CARD}", "-80.00", "2,608.52"),
    ("01/30", "Gusto Pay 123456 PPD ID: 9138864007", "1,825.00", "4,433.52"),
    ("02/03", "City Power & Light Bill Pay PPD ID: 4455667788", "-179.99", "4,253.53"),
    ("02/09", f"Card Purchase 02/08 Quiet Bean Coffee {_CARD}", "-27.21", "4,226.32"),
    ("02/12", "Monthly Service Fee", "-12.00", "4,214.32"),
)
#: The statement after it: the same layout, the same account, a month on.
_FEBRUARY_ROWS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("02/13", "Gusto Pay 123456 PPD ID: 9138864007", "1,825.00", "6,039.32"),
    ("02/20", "Oakfield Rentals Web Pmts 8R3L2Y Web ID: 1234567890", "-1,450.00", "4,589.32"),
    ("03/02", f"Card Purchase 03/01 Corner Grocer #204 {_CARD}", "-58.40", "4,530.92"),
)


def _summary_block(
    period: str, opening: str, rows: Sequence[tuple[str, str, str, str]], closing: str
) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _line(c, 750, [(40, "Northwind Bank, N.A.")], size=14, bold=True)
    _line(c, 750, [(380, period)])
    _line(c, 735, [(40, "P O Box 182051, Columbus, OH 43218 - 2051")])
    _line(c, 738, [(380, "Account Number:  000000482913377")])
    _rows(c, 700, [[(40, "JORDAN A SAMPLE")], [(40, "1420 ELM ST APT 3")]], step=12)
    _line(c, 640, [(40, "CHECKING SUMMARY")], size=11, bold=True)
    _line(c, 622, [(60, "Total Checking"), (340, "INSTANCES"), (-500, "AMOUNT")], size=8)
    _rows(
        c,
        608,
        [
            [(60, "Beginning Balance"), (-500, f"${opening}")],
            [(60, "Ending Balance"), (360, str(len(rows))), (-500, f"${closing}")],
        ],
    )
    _line(c, 510, [(40, "TRANSACTION DETAIL")], size=11, bold=True)
    _line(c, 492, [(60, "DATE"), (110, "DESCRIPTION"), (-430, "AMOUNT"), (-520, "BALANCE")], size=8)
    y = _rows(
        c,
        478,
        [
            [(110, "Beginning Balance"), (-520, f"${opening}")],
            *(
                [(60, day), (110, text), (-430, amount), (-520, balance)]
                for day, text, amount, balance in rows
            ),
        ],
    )
    _line(c, y, [(110, "Ending Balance"), (-520, f"${closing}")], bold=True)
    _line(c, 60, [(40, "Synthetic test document from the PerFin test suite.")], size=7)
    c.save()
    return buf.getvalue()


def summary_block_checking() -> bytes:
    return _summary_block(
        "January 14, 2026 through February 12, 2026", "2,418.77", _JANUARY_ROWS, "4,214.32"
    )


def summary_block_checking_next() -> bytes:
    """The next month's statement of :func:`summary_block_checking`'s account."""
    return _summary_block(
        "February 13, 2026 through March 12, 2026", "4,214.32", _FEBRUARY_ROWS, "4,530.92"
    )


def sectioned_checking() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _line(c, 750, [(40, "Harborline Community Bank")], size=14, bold=True)
    _line(c, 730, [(40, "Your Advantage Banking"), (380, "Account number: 4410 0092 6654")])
    _line(c, 716, [(40, "for March 1, 2026 to March 31, 2026")])
    _line(c, 680, [(40, "Account summary")], size=11, bold=True)
    y = _rows(
        c,
        664,
        [
            [(40, "Beginning balance on March 1, 2026"), (-330, "$1,902.15")],
            [(40, "Deposits and other additions"), (-330, "2,480.00")],
            [(40, "Withdrawals and other subtractions"), (-330, "-1,311.40")],
            [(40, "Checks"), (-330, "-250.00")],
            [(40, "Service fees"), (-330, "-0.00")],
            [(40, "Ending balance on March 31, 2026"), (-330, "$2,820.75")],
        ],
    )
    header: Line = [(40, "Date"), (100, "Description"), (-560, "Amount")]
    payroll = "INITECH DES:PAYROLL ID:XXXXX4471 INDN:SAMPLE JORDAN CO ID:XXXXX1178 PPD"
    y -= 14
    _line(c, y, [(40, "Deposits and other additions")], size=11, bold=True)
    _line(c, y - 16, header, size=8)
    y = _rows(
        c,
        y - 29,
        [
            [(40, "03/13/26"), (100, payroll), (-560, "1,240.00")],
            [(40, "03/27/26"), (100, payroll), (-560, "1,240.00")],
        ],
    )
    _line(c, y, [(40, "Total deposits and other additions"), (-560, "$2,480.00")], bold=True)
    y -= 26
    _line(c, y, [(40, "Withdrawals and other subtractions")], size=11, bold=True)
    _line(c, y - 16, header, size=8)
    y = _rows(
        c,
        y - 29,
        [
            [
                (40, "03/02/26"),
                (100, "OAKFIELD RENTALS DES:RENT ID:88123 INDN:JORDAN SAMPLE"),
                (-560, "-950.00"),
            ],
            [
                (40, "03/05/26"),
                (100, "CHECKCARD 0304 BLUE OWL GROCERS SPRINGFIELD IL 2443106"),
                (-560, "-88.12"),
            ],
            [
                (40, "03/11/26"),
                (100, "CHECKCARD 0310 MAPLE HARDWARE SPRINGFIELD IL 2416407"),
                (-560, "-43.28"),
            ],
            [
                (40, "03/19/26"),
                (100, "CITY WATER DES:UTILITY ID:5512 INDN:JORDAN SAMPLE"),
                (-560, "-130.00"),
            ],
            [
                (40, "03/24/26"),
                (100, "CHECKCARD 0323 JUNIPER CAFE SPRINGFIELD IL 5543286"),
                (-560, "-100.00"),
            ],
        ],
    )
    _line(c, y, [(40, "Total withdrawals and other subtractions"), (-560, "-$1,311.40")], bold=True)
    y -= 26
    _line(c, y, [(40, "Checks")], size=11, bold=True)
    _line(c, y - 16, [(40, "Date"), (100, "Check #"), (-560, "Amount")], size=8)
    _line(c, y - 29, [(40, "03/17/26"), (100, "1042"), (-560, "-250.00")])
    _line(c, y - 42, [(40, "Total checks"), (-560, "-$250.00")], bold=True)
    c.save()
    return buf.getvalue()


def card_statement() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _line(c, 750, [(40, "Meridian Rewards Visa Signature")], size=14, bold=True)
    _line(c, 750, [(380, "Account Ending 4-71009")])
    _line(c, 734, [(40, "Issued by Meridian Card Bank")])
    _line(c, 738, [(380, "Closing Date 02/26/26")])
    _line(c, 726, [(380, "Payment Due Date 03/23/26")])
    y = _rows(
        c,
        690,
        [
            [(40, "Previous Balance"), (-300, "$1,204.33")],
            [(40, "Payments/Credits"), (-300, "-$1,204.33")],
            [(40, "New Charges"), (-300, "+$641.92")],
            [(40, "Fees"), (-300, "+$0.00")],
            [(40, "Interest Charged"), (-300, "+$0.00")],
            [(40, "New Balance"), (-300, "$641.92")],
            [(40, "Minimum Payment Due"), (-300, "$35.00")],
            [(40, "Credit Limit"), (-300, "$12,000.00")],
            [(40, "Available Credit"), (-300, "$11,358.08")],
        ],
    )
    header: Line = [(40, "Date"), (110, "Description"), (-560, "Amount")]
    y -= 14
    _line(c, y, [(40, "Payments and Credits")], size=11, bold=True)
    _line(c, y - 16, header, size=8)
    _line(c, y - 29, [(40, "02/14/26*"), (110, "ONLINE PAYMENT - THANK YOU"), (-560, "-$1,204.33")])
    y -= 55
    _line(c, y, [(40, "New Charges")], size=11, bold=True)
    _line(c, y - 16, header, size=8)
    _rows(
        c,
        y - 29,
        [
            [
                (40, "01/29/26"),
                (110, "JUNIPER BOOKS 000012"),
                (360, "SPRINGFIELD IL"),
                (-560, "$38.40"),
            ],
            [
                (40, "02/02/26"),
                (110, "AplPay QUARTZ GROCERS"),
                (360, "SPRINGFIELD IL"),
                (-560, "$126.07"),
            ],
            [
                (40, "02/07/26"),
                (110, "SKYLINE AIR 0062381190"),
                (360, "CHICAGO IL"),
                (-560, "$318.20"),
            ],
            [
                (40, "02/15/26"),
                (110, "HARBOR PHARMACY #88"),
                (360, "SPRINGFIELD IL"),
                (-560, "$22.75"),
            ],
            [(40, "02/21/26"), (110, "LANTERN FUEL 5521"), (360, "DECATUR IL"), (-560, "$52.50")],
            [
                (40, "02/24/26"),
                (110, "STREAMBOX *MONTHLY"),
                (360, "888-555-0100 CA"),
                (-560, "$84.00"),
            ],
        ],
    )
    c.save()
    return buf.getvalue()


_STERLING_HEADER: Final[Line] = [
    (40, "Date"),
    (90, "Type"),
    (130, "Description"),
    (-400, "Money out"),
    (-470, "Money in"),
    (-550, "Balance"),
]


def sterling_current_account() -> bytes:
    """Two pages; the second opens with the balance brought forward from the first."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    head: Line = [(40, "Thistle & Wren Building Society"), (380, "1 January to 31 January 2026")]
    _line(c, 800, [head[0]], size=14, bold=True)
    _line(c, 800, [(380, "Sort code 40-11-62")])
    _line(c, 788, [(380, "Account number 31926819")])
    _line(c, 782, [(40, "Current Account Statement")])
    _line(c, 776, [head[1]])
    _rows(
        c,
        750,
        [
            [(40, "Balance on 1 January 2026"), (-300, "£1,250.00")],
            [(40, "Money in"), (-300, "£2,100.00")],
            [(40, "Money out"), (-300, "£1,003.45")],
            [(40, "Balance on 31 January 2026"), (-300, "£2,346.55")],
        ],
    )
    _line(c, 680, _STERLING_HEADER, size=8)
    _rows(
        c,
        666,
        [
            [(130, "BALANCE BROUGHT FORWARD"), (-550, "1,250.00")],
            [
                (40, "02 Jan"),
                (90, "DD"),
                (130, "COUNCIL TAX"),
                (-400, "142.00"),
                (-550, "1,108.00"),
            ],
            [
                (40, "05 Jan"),
                (90, "DEB"),
                (130, "TESBURY STORES 3321"),
                (-400, "61.20"),
                (-550, "1,046.80"),
            ],
            [
                (40, "12 Jan"),
                (90, "SO"),
                (130, "FLAT RENT M HOLLIS"),
                (-400, "700.00"),
                (-550, "346.80"),
            ],
            [(130, "BALANCE CARRIED FORWARD"), (-550, "346.80")],
        ],
        step=14,
    )
    c.showPage()
    _line(c, 800, [head[0]], size=14, bold=True)
    _line(c, 776, [head[1]])
    _line(c, 740, _STERLING_HEADER, size=8)
    _rows(
        c,
        726,
        [
            [(130, "BALANCE BROUGHT FORWARD"), (-550, "346.80")],
            [
                (40, "15 Jan"),
                (90, "BGC"),
                (130, "ACME WIDGETS LTD SALARY"),
                (-470, "2,100.00"),
                (-550, "2,446.80"),
            ],
            [
                (40, "21 Jan"),
                (90, "DEB"),
                (130, "RAILCARD TICKETS"),
                (-400, "48.25"),
                (-550, "2,398.55"),
            ],
            [
                (40, "28 Jan"),
                (90, "CPT"),
                (130, "CASH MACHINE HIGH ST"),
                (-400, "52.00"),
                (-550, "2,346.55"),
            ],
            [(130, "BALANCE CARRIED FORWARD"), (-550, "2,346.55")],
        ],
        step=14,
    )
    c.save()
    return buf.getvalue()


def credit_union_shares() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _line(c, 750, [(40, "Prairie Lakes Credit Union")], size=14, bold=True)
    _line(c, 750, [(380, "Statement Period: 04/01/2026 - 04/30/2026")])
    _line(c, 738, [(380, "Member Number: ******5120")])
    header: Line = [
        (40, "Trans Date"),
        (110, "Transaction Description"),
        (-400, "Withdrawal"),
        (-470, "Deposit"),
        (-550, "Balance"),
    ]

    def share(y: float, title: str, opening: str, rows: Sequence[Line], closing: str) -> float:
        _line(c, y, [(40, title)], size=11, bold=True)
        _line(c, y - 16, header, size=8)
        y = _rows(
            c,
            y - 29,
            [
                [(40, "04/01"), (110, "Beginning Balance"), (-550, opening)],
                *rows,
                [(40, "04/30"), (110, "Ending Balance"), (-550, closing)],
            ],
        )
        return y - 17

    y = share(
        700,
        "PRIMARY SAVINGS - ID 0000",
        "5,000.00",
        [
            [
                (40, "04/10"),
                (110, "Transfer From Share 0010"),
                (-470, "300.00"),
                (-550, "5,300.00"),
            ],
            [(40, "04/30"), (110, "Dividend 0.250% APY"), (-470, "1.09"), (-550, "5,301.09")],
        ],
        "5,301.09",
    )
    share(
        y,
        "FREE CHECKING - ID 0010",
        "812.44",
        [
            [(40, "04/03"), (110, "POS Debit BLUE OWL GROCERS"), (-400, "74.19"), (-550, "738.25")],
            [(40, "04/10"), (110, "Transfer To Share 0000"), (-400, "300.00"), (-550, "438.25")],
            [
                (40, "04/15"),
                (110, "ACH Deposit INITECH PAYROLL"),
                (-470, "1,610.00"),
                (-550, "2,048.25"),
            ],
            [(40, "04/22"), (110, "Draft 2051"), (-400, "125.00"), (-550, "1,923.25")],
        ],
        "1,923.25",
    )
    c.save()
    return buf.getvalue()


@dataclass(frozen=True)
class ExpectedStatement:
    last4: str | None
    opening: Decimal
    closing: Decimal
    rows: int
    name: str | None = None
    account_type: str | None = None
    currency: str | None = None


@dataclass(frozen=True)
class Expected:
    make: Callable[[], bytes]
    institution: str
    statements: tuple[ExpectedStatement, ...]


def _d(text: str) -> Decimal:
    return Decimal(text)


EXPECTED: Final[dict[str, Expected]] = {
    "summary_block_checking": Expected(
        summary_block_checking,
        "Northwind Bank, N.A.",
        (ExpectedStatement("3377", _d("2418.77"), _d("4214.32"), 9),),
    ),
    "sectioned_checking": Expected(
        sectioned_checking,
        "Harborline Community Bank",
        (ExpectedStatement("6654", _d("1902.15"), _d("2820.75"), 8),),
    ),
    "card_statement": Expected(
        card_statement,
        "Meridian Card Bank",
        (ExpectedStatement("1009", _d("-1204.33"), _d("-641.92"), 7, account_type="creditcard"),),
    ),
    "sterling_current_account": Expected(
        sterling_current_account,
        "Thistle & Wren Building Society",
        (ExpectedStatement("6819", _d("1250.00"), _d("2346.55"), 6, currency="GBP"),),
    ),
    "credit_union_shares": Expected(
        credit_union_shares,
        "Prairie Lakes Credit Union",
        (
            ExpectedStatement(
                "0000", _d("5000.00"), _d("5301.09"), 2, "PRIMARY SAVINGS", "savings"
            ),
            ExpectedStatement("0010", _d("812.44"), _d("1923.25"), 4, "FREE CHECKING", "checking"),
        ),
    ),
}
