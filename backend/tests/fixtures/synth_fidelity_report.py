# SPDX-License-Identifier: AGPL-3.0-or-later
"""A synthetic three-account "INVESTMENT REPORT" PDF in the Fidelity statement layout.

Nothing here comes from a real statement: the page layout is redrawn by code
(reportlab, through :mod:`tests.fixtures.synth_multi_account`) and every
person, account number, instrument (symbols starting ``ZZ``, CUSIPs starting
``9`` with valid check digits), quantity, price, amount, reference and check
number is invented. Never copy a figure from a real statement into it, not
even one row: invent new ones and re-balance the arithmetic below. Every page
says it is a synthetic test document.

It reproduces the *layout traits* that make the real layout hard to read,
so the multi-account work (period, account segmentation, table reading,
detection) is tested against them:

* **Institution only in metadata.** The PDF's author and title name the
  institution (``Fidelity Investments``, ``institution_metadata=False``
  leaves them out); the page text never prints that name, only a page-1
  footer ``Brokerage services provided by Fidelity Brokerage Services LLC``.
* **Unlabelled header period.** Every page prints ``INVESTMENT REPORT`` and
  ``June 1 <en dash> June 30, 2019`` (the year once) in its header, with no
  label. Page 1 also prints ``Change from Last Period: $...`` (a "period"
  label followed by no period) and bond maturities / security names carry
  dates outside the period (``8/1/28``, ``09/30/2028``). The footnotes on
  pages 1 and 2 speak of "the statement period", as the layout's do (it is
  also what keeps the receipt detector off the report).
* **Accounts.** Page 2 has an *Accounts Included in This Report* index
  (``Page | Account Type/Name | Account Number | Beginning Value | Ending
  Value``) under group lines (``GENERAL INVESTMENTS``, ``PERSONAL
  RETIREMENT``, ``EDUCATION (529) ACCOUNTS``). **Its page numbers are
  wrong**, as the layout's can be: each is off by :data:`INDEX_PAGE_SHIFT`
  and the last one points past the end of the document, so segmentation
  must follow the account headings, never the index's pages. Each
  account's first page prints its number on the same line as a dollar
  amount (``$90,235.56 Account 418-205817`` / ``Account Value: $62,362.06
  Account 418-330642``);
  every other page of the account prints the section title and ``Account
  NNN-NNNNNN`` on the top line and the owner/type line under it. Account
  types: Individual - TOD, Traditional IRA, Education 529 (no core cash).
* **Holdings.** Sections *Core Account* (the heading above its own table,
  the sweep as a row: it is cash, not a position), *Mutual Funds* (with
  sub-headings), *Exchange Traded Products*, *Stocks*, *Bonds* and *Other*;
  two-line headers (``Price`` over ``Per Unit``, ``Ending`` over ``Market
  Value``); names wrapped onto a second line holding the ``(SYMBOL)``;
  bonds priced per 100 of face value with ``CUSIP: ...`` and terms lines
  under the row; an ``ISIN: ...`` line; footnote markers inside numeric
  cells (:attr:`Position.price_mark` / ``cost_mark``: ``147.85A``,
  ``$6,630.82t``, ``$7,184.90 c $3,821.56``); ``Total <group> (n% of account
  holdings)`` subtotals and ``Total Holdings`` (core included). Holdings run
  over several pages with ``Holdings (continued)``.
* **Activity.** *Securities Bought & Sold* (``Settlement Date | Security
  Name | Symbol/CUSIP | Transaction Description | Quantity | Price | Cost
  Basis of Close | Transaction Cost | Transaction Total``, headers over two
  lines, sells with a negative quantity, gain and wash-sale notes under the
  row, on the same line as the wrapped end of the name: ``ETF Wash sale of
  5/14/19: $193.62``), *Trades Pending Settlement* (dates after the period:
  not activity), *Securities Transferred In* / *Out* (between the Individual
  account and the IRA of the same report, both sides printed: a cash row
  with no security, only an internal reference in the name column, and an
  in-kind row whose printed amount is the value moved, not cash; plus an
  in-kind transfer to an account outside the report), *Dividends, Interest
  & Other Income* (a reinvestment, a CUSIP in the symbol column),
  side-by-side *Deposits*,
  *Withdrawals*, *Checking Activity*, *Bill Payments*, *Fees and Charges*
  and *Taxes Withheld* (two tables on one line, the total in the right
  half), *Debit Card Activity* (transaction and post dates),
  *Contributions*/*Distributions* (IRA), and noise tables that are not
  activity (*Daily Additions and Subtractions*, *Estimated Cash Flow*,
  *Core Account Cash Flow*, *Income Summary*, *Top Holdings*). Dates are
  year-less ``M/D`` (``6/1`` and ``6/03`` both occur).
* **Minus signs.** *Debit Card Activity*, *Checking Activity*, *Fees and
  Charges*, *Daily Additions and Subtractions* and the IRA's summary pages
  print negative amounts with U+2212 (:data:`MINUS`, ``\u2212$412.86``);
  every other table uses ``-``.

The figures reconcile: for each account, quantity x price (/ 100 for bonds)
= market value, the positions and the core add up to the ending account
value, the core's opening balance plus every activity row's cash effect is
its closing balance (also printed as *Core Account Cash Flow*), and the
account summary's additions, subtractions and change in investment value
add up. The Year-to-Date columns add up as well and end at the same ending
value or balance as This Period. :func:`expected` /
:func:`expected_activity` / :func:`expected_positions` give what a correct
parser must return per account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Final

from reportlab.pdfgen import canvas

from tests.fixtures.synth_multi_account import (
    BOLD,
    DISCLAIMER,
    FONT,
    ZERO,
    Account,
    Activity,
    Cursor,
    Page,
    Position,
    Report,
    cents,
    check_account,
    cusip,
    expected_activity,
    expected_positions,
    isin,
    md,
    mdy_short,
    money,
    number,
    render,
    wrap,
)

__all__ = [
    "EXPECTED_HEADER_LINES",
    "INDEX_PAGE_SHIFT",
    "INSTITUTION",
    "MINUS",
    "Expected",
    "expected",
    "expected_activity",
    "expected_positions",
    "report",
    "report_pdf",
]

INSTITUTION: Final = "Fidelity Investments"
#: The page-1 footer naming the broker (the only place page text names it).
FOOTER_BROKER: Final = "Fidelity Brokerage Services LLC"
TITLE: Final = "INVESTMENT REPORT"
START: Final = date(2019, 6, 1)
#: How far the index's printed page numbers are off each account's real first
#: page. The layout's index is not reliable: the last one even points past the
#: end of the document. Segmenting must follow the account headings.
INDEX_PAGE_SHIFT: Final = (1, 3, 4)


def _period_text(start: date, end: date) -> str:
    return f"{start:%B} {start.day} \u2013 {end:%B} {end.day}, {end.year}"


#: Header lines every page's extracted text contains (for the default period).
EXPECTED_HEADER_LINES: Final = (TITLE, _period_text(START, date(2019, 6, 30)))


@dataclass(frozen=True)
class Extras:
    """What the report prints besides the expected results (not parser output)."""

    #: Trades dated after the period (*Trades Pending Settlement*).
    pending: dict[str, tuple[Activity, ...]]
    #: Beginning values of positions an account no longer holds at the end.
    closed_begin: dict[str, Decimal]


def _month_end(start: date) -> date:
    following = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return following - timedelta(days=1)


def report(start: date = START) -> tuple[Report, Extras]:
    """The three accounts for the month starting ``start`` (module doc)."""
    assert start.day == 1
    end = _month_end(start)

    def d(day: int) -> date:
        return start.replace(day=day)

    brokerage_bond = cusip("94136RAC")
    muni = cusip("96205LQK")
    tbill = cusip("99318WCD")
    individual_ref = "R57-205817-1"
    ira_ref = "R57-330642-1"
    individual = Account(
        number="418-205817",
        title="Jordan Q. Sample - Individual - TOD",
        kind="investment",
        subtype="brokerage",
        group="GENERAL INVESTMENTS",
        core_name="PLACEHOLDER INSURED DEPOSIT SWEEP NOT COVERED BY SIPC",
        core_symbol="ZZSWP",
        cash_opening=Decimal("9832.47"),
        positions=(
            Position(
                "EXAMPLE TOTAL RETURN BOND FD CL I",
                "Mutual Funds",
                Decimal("864.517"),
                Decimal("10.730"),
                Decimal("9116.40"),
                Decimal("8412.36"),
                symbol="ZZTRB",
                subgroup="Bond Funds",
                notes=("— 30-day yield: 3.96%",),
                cost_mark=" c",
            ),
            Position(
                "SAMPLE INSTL MONEY MARKET PORT CL I",
                "Mutual Funds",
                Decimal("3618.442"),
                Decimal("1.000"),
                Decimal("3618.44"),
                symbol="ZZMXX",
                subgroup="Short-term Funds",
                notes=("— 7-day yield: 0.02%",),
            ),
            Position(
                "MOCK SER TR HIGH YIELD BOND ETF",
                "Exchange Traded Products",
                Decimal("320.00"),
                Decimal("37.84"),
                Decimal("12971.00"),
                Decimal("11262.40"),
                symbol="ZZHY",
                qty_places=2,
                price_places=2,
                cost_mark=" c",
            ),
            Position(
                "DEMO INTL EQUITY INDEX FDS EUROPE ETF",
                "Exchange Traded Products",
                Decimal("75.00"),
                Decimal("46.18"),
                Decimal("3360.00"),
                Decimal("4176.30"),
                symbol="ZZIEQ",
                qty_places=2,
                price_places=2,
            ),
            Position(
                "FICTIONAL APPLIED DEVICES INC",
                "Stocks",
                Decimal("18.00"),
                Decimal("611.47"),
                Decimal("10512.00"),
                Decimal("7184.90"),
                symbol="ZZAPD",
                subgroup="Common Stocks",
                qty_places=2,
                price_places=2,
                cost_mark=" c",
            ),
            Position(
                "SAMPLE HEALTH CARE REIT INC COM USD0.01",
                "Stocks",
                Decimal("410.00"),
                Decimal("12.41"),
                ZERO,
                Decimal("5073.40"),
                symbol="ZZSHR",
                subgroup="Common Stocks",
                qty_places=2,
                price_places=2,
            ),
            Position(
                "PLACEHOLDER GROUP LIMITED COM STK USD 1.00",
                "Stocks",
                Decimal("60.00"),
                Decimal("147.85"),
                ZERO,
                Decimal("7920.60"),
                symbol="ZZPGL",
                subgroup="Common Stocks",
                qty_places=2,
                price_places=2,
                price_mark="A",
            ),
            Position(
                "SAMPLE CORP NT CV",
                "Bonds",
                Decimal("8000.00"),
                Decimal("98.625"),
                Decimal("7860.00"),
                Decimal("7985.00"),
                cusip=brokerage_bond,
                subgroup="Corporate Bonds",
                maturity=date(2028, 8, 1),
                coupon="4.375%",
                per_100=True,
                notes=("Int. Semi-Annually",),
                qty_places=2,
            ),
            Position(
                "EXAMPLE CITY INDL DEV AGY REV",
                "Bonds",
                Decimal("12000.000"),
                Decimal("104.318"),
                Decimal("12462.00"),
                Decimal("12210.00"),
                cusip=muni,
                subgroup="Municipal Bonds",
                maturity=date(2031, 11, 1),
                coupon="3.750%",
                per_100=True,
                notes=("Fixed Coupon; Insured; Int. Semi-Annually",),
                cost_mark=" B",
            ),
            Position(
                "UNITED STATES TREAS BILLS",
                "Bonds",
                Decimal("14000.00"),
                Decimal("99.412"),
                Decimal("13874.00"),
                Decimal("13809.30"),
                cusip=tbill,
                subgroup="US Treasury/Agency Securities",
                maturity=date(2019, 10, 17),
                coupon="0.00%",
                per_100=True,
                notes=("Fixed Coupon",),
                qty_places=2,
            ),
        ),
        activity=(
            # Securities Bought & Sold
            Activity(
                d(11),
                "buy",
                "trades",
                "You bought",
                Decimal("-4280.99"),
                name="MOCK SER TR HIGH YIELD BOND ETF",
                symbol="ZZHY",
                quantity=Decimal(120),
                price=Decimal("35.61700"),
                cost=Decimal("6.95"),
                notes=("Wash sale of 5/14/19: $193.62",),
            ),
            Activity(
                d(11),
                "buy",
                "trades",
                "You bought",
                Decimal("-3336.05"),
                name="SAMPLE HEALTH CARE REIT INC COM USD0.01",
                symbol="ZZSHR",
                quantity=Decimal(270),
                price=Decimal("12.33000"),
                cost=Decimal("6.95"),
            ),
            Activity(
                d(11),
                "buy",
                "trades",
                "You bought",
                Decimal("-1737.35"),
                name="SAMPLE HEALTH CARE REIT INC COM USD0.01",
                symbol="ZZSHR",
                quantity=Decimal(140),
                price=Decimal("12.36000"),
                cost=Decimal("6.95"),
            ),
            Activity(
                d(12),
                "sell",
                "trades",
                "You sold",
                Decimal("6511.57"),
                name="MOCK SER TR HIGH YIELD BOND ETF",
                symbol="ZZHY",
                quantity=Decimal(180),
                price=Decimal("36.21400"),
                cost=Decimal("6.95"),
                cost_basis=Decimal("6402.60"),
                notes=("ST gain: $108.97",),
            ),
            Activity(
                d(18),
                "buy",
                "trades",
                "You bought",
                Decimal("-1484.67"),
                name="MOCK SER TR HIGH YIELD BOND ETF",
                symbol="ZZHY",
                quantity=Decimal(40),
                price=Decimal("36.94300"),
                cost=Decimal("6.95"),
            ),
            # Securities Transferred In: cash (no security) and shares from the
            # IRA of the same report; Out: shares to an account outside it.
            # Cash moved between accounts is a deposit / withdrawal of cash
            # (a transfer_in/out moves units of a security).
            Activity(
                d(28),
                "deposit",
                "transferred_in",
                "Trans. from account 418-330642",
                Decimal("2140.00"),
                name=ira_ref,
            ),
            Activity(
                d(28),
                "transfer_in",
                "transferred_in",
                "Trans. from account 418-330642",
                ZERO,
                name="PLACEHOLDER GROUP LIMITED COM STK USD 1.00",
                symbol="ZZPGL",
                quantity=Decimal("60.000"),
                price=Decimal("143.250"),
                printed=Decimal("8595.00"),
                notes=(ira_ref,),
            ),
            Activity(
                d(28),
                "transfer_out",
                "transferred_out",
                "Trans. to account X81-004410",
                ZERO,
                name="SAMPLE ENERGY PARTNERS LP",
                symbol="ZZSEP",
                quantity=Decimal("210.000"),
                price=Decimal("27.650"),
                printed=Decimal("-5806.50"),
            ),
            # Dividends, Interest & Other Income
            Activity(
                d(3),
                "dividend",
                "income",
                "Dividend received",
                Decimal("155.77"),
                name="EXAMPLE TOTAL RETURN BOND FD CL I",
                symbol="ZZTRB",
            ),
            Activity(
                d(3),
                "reinvest",
                "income",
                "Reinvestment",
                Decimal("-155.77"),
                name="EXAMPLE TOTAL RETURN BOND FD CL I",
                symbol="ZZTRB",
                quantity=Decimal("14.517"),
                price=Decimal("10.730"),
            ),
            Activity(
                d(4),
                "interest",
                "income",
                "Muni exempt interest",
                Decimal("225.00"),
                name="EXAMPLE CITY INDL DEV AGY REV",
                cusip=muni,
            ),
            Activity(
                d(14),
                "dividend",
                "income",
                "Dividend received",
                Decimal("14.04"),
                name="FICTIONAL APPLIED DEVICES INC",
                symbol="ZZAPD",
            ),
            Activity(
                d(28),
                "interest",
                "income",
                "Interest earned",
                Decimal("0.73"),
                name="PLACEHOLDER INSURED DEPOSIT SWEEP",
                symbol="ZZSWP",
            ),
            # Deposits
            Activity(d(1), "deposit", "deposits", "Wire transfer from bank", Decimal("4200.00")),
            Activity(
                d(3),
                "deposit",
                "deposits",
                "Deposit received",
                Decimal("27.50"),
                reference="CHK3381",
            ),
            Activity(d(12), "deposit", "deposits", "Wire transfer from bank", Decimal("1250.00")),
            Activity(
                d(18),
                "deposit",
                "deposits",
                "Electronic funds transfer received",
                Decimal("375.00"),
                reference="EFT2207",
            ),
            Activity(d(25), "deposit", "deposits", "Wire transfer from bank", Decimal("650.00")),
            # Withdrawals
            Activity(
                d(2), "withdrawal", "withdrawals", "Wire transfer to bank", Decimal("-2600.00")
            ),
            Activity(d(8), "withdrawal", "withdrawals", "Wire transfer to bank", Decimal("-40.00")),
            Activity(
                d(17), "withdrawal", "withdrawals", "Wire transfer to bank", Decimal("-1150.00")
            ),
            Activity(
                d(29),
                "withdrawal",
                "withdrawals",
                "Electronic remit Debit Sample Card Co",
                Decimal("-185.00"),
            ),
            # Debit Card Activity
            Activity(
                d(1),
                "withdrawal",
                "debit_card",
                "ATM3171 220 ELM ST, ANYTOWN, ST",
                Decimal("-260.00"),
                post_day=d(4),
                reference="5830947QX2RT8WNM",
            ),
            Activity(
                d(11),
                "withdrawal",
                "debit_card",
                "POS4917 EXAMPLE MART 0001, ANYTOWN, ST",
                Decimal("-87.43"),
                post_day=d(14),
                reference="3302871QX2RT8WPA",
            ),
            Activity(
                d(21),
                "deposit",
                "debit_card",
                "SAMPLE OUTFITTERS #0506, ANYTOWN, ST",
                Decimal("39.12"),
                post_day=d(24),
                reference="20718QXRT48807VKD",
            ),
            # Bill Payments
            Activity(
                d(10),
                "withdrawal",
                "bill_payments",
                "Sample Card Co*",
                Decimal("-1186.40"),
                reference="**********4412",
            ),
            Activity(
                d(15),
                "withdrawal",
                "bill_payments",
                "Example Card Service",
                Decimal("-472.15"),
                reference="**********6630",
            ),
            Activity(
                d(18),
                "withdrawal",
                "bill_payments",
                "Demo Rewards Visa",
                Decimal("-561.09"),
                reference="**********2085",
            ),
            # Checking Activity (the reference is the check number)
            Activity(
                d(30),
                "withdrawal",
                "checking",
                "Check paid",
                Decimal("-412.86"),
                reference="2311",
            ),
            Activity(
                d(5),
                "withdrawal",
                "checking",
                "Check paid",
                Decimal("-1876.30"),
                reference="2318 *",
            ),
            Activity(
                d(23),
                "withdrawal",
                "checking",
                "Check paid",
                Decimal("-793.45"),
                reference="2324 *",
            ),
            Activity(
                d(30),
                "withdrawal",
                "checking",
                "Check paid",
                Decimal("-2240.00"),
                reference="2325",
            ),
            # Fees and Charges
            Activity(d(1), "fee", "fees", "Advisor Fee", Decimal("-39.58")),
            Activity(d(17), "fee", "fees", "ADR fee", Decimal("-1.80")),
            # Taxes Withheld
            Activity(
                d(11),
                "fee",
                "taxes",
                "Foreign tax paid",
                Decimal("-2.11"),
                name="FICTIONAL APPLIED DEVICES INC",
            ),
            Activity(
                d(15),
                "fee",
                "taxes",
                "Federal tax paid",
                Decimal("-31.40"),
                name="MOCK SER TR HIGH YIELD BOND ETF",
            ),
            Activity(
                d(15),
                "fee",
                "taxes",
                "State tax paid",
                Decimal("-9.65"),
                name="MOCK SER TR HIGH YIELD BOND ETF",
            ),
        ),
    )
    ira_bond = cusip("93702MAE")
    ira_note = cusip("97354HBF")
    ira = Account(
        number="418-330642",
        title="Jordan Q. Sample - Traditional IRA",
        kind="investment",
        subtype="ira",
        group="PERSONAL RETIREMENT",
        core_name="FICTIONAL CASH RESERVES",
        core_symbol="ZZCRX",
        cash_opening=Decimal("3486.29"),
        positions=(
            Position(
                "FICTIONAL APPLIED DEVICES INC",
                "Stocks",
                Decimal("22.00"),
                Decimal("611.47"),
                Decimal("12848.00"),
                Decimal("6630.82"),
                symbol="ZZAPD",
                subgroup="COMMON STOCK",
                qty_places=2,
                price_places=2,
                cost_mark="t",
            ),
            Position(
                "EXAMPLE PETROLEUM CORP",
                "Stocks",
                Decimal("64.00"),
                Decimal("79.43"),
                Decimal("4384.80"),
                Decimal("4417.36"),
                symbol="ZZXPC",
                subgroup="COMMON STOCK",
                qty_places=2,
                price_places=2,
                cost_mark="t",
            ),
            Position(
                "SAMPLE MOTORS CO USD0.01",
                "Stocks",
                Decimal("185.00"),
                Decimal("31.27"),
                Decimal("5550.00"),
                symbol="ZZSMC",
                isin=isin("ZZ0000SMC01"),
                subgroup="COMMON STOCK",
                qty_places=2,
                price_places=2,
            ),
            Position(
                "SAMPLE CPI LKD NT FLTG RATE 0.00% 09/30/2028",
                "Stocks",
                Decimal("440.00"),
                Decimal("24.12"),
                Decimal("10560.00"),
                Decimal("8908.24"),
                symbol="ZZSLN",
                subgroup="COMMON STOCK",
                qty_places=2,
                price_places=2,
                cost_mark="t",
            ),
            Position(
                "SAMPLE TECH CORP NT CV",
                "Bonds",
                Decimal("6000.00"),
                Decimal("101.875"),
                Decimal("6090.00"),
                Decimal("6000.00"),
                cusip=ira_bond,
                maturity=date(2026, 2, 15),
                coupon="5.250%",
                per_100=True,
                notes=("Semi-Annual Interest",),
                qty_places=2,
            ),
            Position(
                "EXAMPLE AUTO PARTS JR SB NT",
                "Bonds",
                Decimal("18000.00"),
                Decimal("97.250"),
                Decimal("17460.00"),
                Decimal("17892.00"),
                cusip=ira_note,
                maturity=date(2034, 4, 1),
                coupon="6.125%",
                per_100=True,
                notes=("Fixed Coupon; Quarterly Interest",),
                qty_places=2,
            ),
            Position(
                "SAMPLE MTRS CO WTS EXP 01/22/2027",
                "Other",
                Decimal("240.000"),
                Decimal("9.415"),
                Decimal("2208.00"),
                symbol="ZZWSA",
            ),
        ),
        activity=(
            Activity(
                d(20),
                "buy",
                "trades",
                "You bought",
                Decimal("-803.70"),
                name="EXAMPLE PETROLEUM CORP",
                symbol="ZZXPC",
                quantity=Decimal(10),
                price=Decimal("80.37000"),
            ),
            Activity(
                d(9),
                "dividend",
                "income",
                "Dividend received",
                Decimal("50.76"),
                name="EXAMPLE PETROLEUM CORP",
                symbol="ZZXPC",
            ),
            Activity(
                d(15),
                "interest",
                "income",
                "Interest",
                Decimal("157.50"),
                name="SAMPLE TECH CORP NT CV",
                cusip=ira_bond,
            ),
            Activity(
                d(15),
                "interest",
                "income",
                "Interest",
                Decimal("52.47"),
                name="SAMPLE CPI LKD NT FLTG RATE 0.00% 09/30/2028",
                symbol="ZZSLN",
            ),
            Activity(
                d(28),
                "dividend",
                "income",
                "Dividend received",
                Decimal("0.03"),
                name="FICTIONAL CASH RESERVES",
                symbol="ZZCRX",
            ),
            # The other side of the Individual account's transfers in (the cash
            # leaves as a withdrawal).
            Activity(
                d(28),
                "withdrawal",
                "transferred_out",
                "Trans. to account 418-205817",
                Decimal("-2140.00"),
                name=individual_ref,
            ),
            Activity(
                d(28),
                "transfer_out",
                "transferred_out",
                "Trans. to account 418-205817",
                ZERO,
                name="PLACEHOLDER GROUP LIMITED COM STK USD 1.00",
                symbol="ZZPGL",
                quantity=Decimal("60.000"),
                price=Decimal("143.250"),
                printed=Decimal("-8595.00"),
            ),
            Activity(
                d(14),
                "deposit",
                "contributions",
                f"Contribution for {start.year}",
                Decimal("1650.00"),
            ),
            Activity(
                d(25), "withdrawal", "distributions", "Normal distribution", Decimal("-820.00")
            ),
            Activity(d(25), "fee", "taxes", "Federal tax withheld", Decimal("-82.00")),
        ),
    )
    college = Account(
        number="418-779015",
        title="Jordan Q. Sample - Education Account",
        kind="investment",
        subtype="529",
        group="EDUCATION (529) ACCOUNTS",
        positions=(
            Position(
                "Sample College Portfolio 2031",
                "Holdings",
                Decimal("1046.873"),
                Decimal("18.940"),
                Decimal("19412.00"),
                symbol="ZZC31",
            ),
            Position(
                "Sample Moderate Growth Portfolio",
                "Holdings",
                Decimal("512.664"),
                Decimal("14.270"),
                Decimal("7170.00"),
                symbol="ZZMGP",
            ),
        ),
    )
    accounts = (individual, ira, college)
    for account in accounts:
        check_account(account)
    pending = Activity(
        end + timedelta(days=2),
        "buy",
        "pending",
        "You bought",
        Decimal("-2445.88"),
        name="FICTIONAL APPLIED DEVICES INC",
        symbol="ZZAPD",
        quantity=Decimal(4),
        price=Decimal("611.470"),
    )
    extras = Extras(
        pending={individual.number: (pending,)},
        closed_begin={individual.number: Decimal("5775.00"), ira.number: Decimal("8364.00")},
    )
    stmt = Report(
        INSTITUTION,
        start,
        end,
        accounts,
        owner="Jordan Q. Sample",
        address=("100 Example Avenue", "Anytown, ST 00000"),
    )
    return stmt, extras


@dataclass(frozen=True)
class Expected:
    """What a correct parser returns for one account of the report."""

    last4: str
    subtype: str
    period_start: date
    period_end: date
    positions: list[tuple[str, Decimal, Decimal, Decimal]]
    activity: list[tuple[date, str, str | None, Decimal | None, Decimal]]
    cash_opening: Decimal | None
    cash_closing: Decimal | None
    positions_value: Decimal
    total_value: Decimal


def expected(stmt: Report) -> list[Expected]:
    """Per account, in print order (module doc)."""
    return [
        Expected(
            last4=a.last4,
            subtype=a.subtype,
            period_start=stmt.start,
            period_end=stmt.end,
            positions=expected_positions(a),
            activity=expected_activity(a),
            cash_opening=a.cash_opening,
            cash_closing=a.cash_closing,
            positions_value=a.holdings_value,
            total_value=a.total_value,
        )
        for a in stmt.accounts
    ]


# ---------------------------------------------------------------------------
# Account arithmetic for the summary pages
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Summary:
    begin: Decimal
    deposits: Decimal
    transferred_in: Decimal
    withdrawals: Decimal
    transferred_out: Decimal
    cards: Decimal
    taxes: Decimal
    costs_fees: Decimal
    end: Decimal

    @property
    def additions(self) -> Decimal:
        return self.deposits + self.transferred_in

    @property
    def subtractions(self) -> Decimal:
        return self.withdrawals + self.transferred_out + self.cards + self.taxes

    @property
    def change(self) -> Decimal:
        return self.end - self.begin - self.additions - self.subtractions - self.costs_fees


def _total(rows: list[Activity]) -> Decimal:
    return sum((a.amount for a in rows), ZERO)


def _summary(account: Account, extras: Extras) -> _Summary:
    trade_costs = sum((a.cost for a in account.section("trades")), ZERO)
    return _Summary(
        begin=account.begin_value + extras.closed_begin.get(account.number, ZERO),
        deposits=_total(account.section("deposits", "contributions")),
        transferred_in=sum((a.shown for a in account.section("transferred_in")), ZERO),
        withdrawals=_total(account.section("withdrawals", "distributions")),
        transferred_out=sum((a.shown for a in account.section("transferred_out")), ZERO),
        cards=_total(account.section("checking", "debit_card", "bill_payments")),
        taxes=_total(account.section("taxes")),
        costs_fees=_total(account.section("fees")) - trade_costs,
        end=account.total_value,
    )


def _ytd(value: Decimal, factor: str = "2.35") -> Decimal:
    return cents(value * Decimal(factor))


#: U+2212, the minus sign some of the layout's tables print instead of "-".
MINUS: Final = "\u2212"


def _minus(value: Decimal, *, dollar: bool = False) -> str:
    return money(value, dollar=dollar, minus=MINUS)


def _ytd_column(end: Decimal, flows: list[Decimal]) -> tuple[Decimal, list[Decimal]]:
    """A Year-to-Date column that adds up: (beginning, flows) for ``end``.

    Each flow is scaled by :func:`_ytd`; the beginning is what makes the
    column end at the same ``end`` as This Period (an ending balance is one
    figure, whichever column prints it).
    """
    ytd = [_ytd(v) for v in flows]
    return end - sum(ytd, ZERO), ytd


def _pct(part: Decimal, whole: Decimal) -> str:
    return "0%" if not whole else f"{round(part / whole * 100)}%"


# ---------------------------------------------------------------------------
# Page drawing
# ---------------------------------------------------------------------------


class _Layout:
    def __init__(self, stmt: Report, extras: Extras) -> None:
        self.stmt = stmt
        self.extras = extras
        self.pages: list[Page] = []
        self.first_page: dict[str, int] = {}

    # -- chrome -----------------------------------------------------------------

    def chrome(self, c: canvas.Canvas, page: int, total: int) -> float:
        c.setFont(BOLD, 8)
        c.drawString(40, 772, "*** SYNTHETIC TEST STATEMENT ***")
        c.setFont(FONT, 8)
        c.drawString(40, 760, "For testing purposes only")
        c.setFont(BOLD, 11)
        c.drawRightString(572, 760, TITLE)
        c.setFont(FONT, 9)
        c.drawRightString(572, 747, _period_text(self.stmt.start, self.stmt.end))
        c.setFont(FONT, 6)
        if page == 1:
            c.drawString(
                40,
                52,
                f"Brokerage services provided by {FOOTER_BROKER}, Member SIPC. "
                "Invented figures for software testing.",
            )
        c.drawString(40, 40, DISCLAIMER)
        c.drawRightString(572, 30, f"{page} of {total}")
        return 726.0

    @staticmethod
    def banner(cur: Cursor, account: Account, left: str) -> None:
        cur.line({40: left}, {572: f"Account {account.number}"}, font=BOLD, size=10, gap=12)
        cur.line({}, {572: account.title}, size=8, gap=14)

    # -- plan ---------------------------------------------------------------------

    def add(self, page: Page, account: Account | None = None) -> None:
        self.pages.append(page)
        if account is not None and account.number not in self.first_page:
            self.first_page[account.number] = len(self.pages)

    def build(self) -> list[Page]:
        self.add(self.overview)
        self.add(self.index)
        self.add(self.portfolio_continued)
        individual, ira, college = self.stmt.accounts
        self.add(lambda cur: self.account_summary(cur, individual, style="amount"), individual)
        self.add(lambda cur: self.core_cash_flow(cur, individual))
        self.add(
            lambda cur: self.holdings(
                cur, individual, ("Mutual Funds", "Exchange Traded Products"), first=True
            )
        )
        self.add(lambda cur: self.holdings(cur, individual, ("Stocks",), first=False))
        self.add(lambda cur: self.holdings(cur, individual, ("Bonds",), first=False, total=True))
        self.add(lambda cur: self.activity_trades(cur, individual))
        self.add(lambda cur: self.activity_income_cash(cur, individual))
        self.add(lambda cur: self.activity_banking(cur, individual))
        self.add(lambda cur: self.daily_and_cash_flow(cur, individual))
        self.add(lambda cur: self.account_summary(cur, ira, style="labelled"), ira)
        self.add(lambda cur: self.core_cash_flow(cur, ira))
        self.add(lambda cur: self.holdings(cur, ira, ("Stocks",), first=True))
        self.add(lambda cur: self.holdings(cur, ira, ("Bonds", "Other"), first=False, total=True))
        self.add(lambda cur: self.ira_activity(cur, ira))
        self.add(lambda cur: self.college(cur, college), college)
        self.add(self.endnotes)
        return self.pages

    # -- document-level pages ---------------------------------------------------------

    def overview(self, cur: Cursor) -> None:
        stmt = self.stmt
        sums = [_summary(a, self.extras) for a in stmt.accounts]
        begin = sum((s.begin for s in sums), ZERO)
        end = sum((s.end for s in sums), ZERO)
        additions = sum((s.additions for s in sums), ZERO)
        subtractions = sum((s.subtractions for s in sums), ZERO)
        fees = sum((s.costs_fees for s in sums), ZERO)
        change = sum((s.change for s in sums), ZERO)
        cur.line({40: f"Your Portfolio Value: {money(end, dollar=True)}"}, font=BOLD, size=10)
        cur.line({}, {572: f"Change from Last Period: {money(end - begin, dollar=True)}"}, size=8)
        cur.skip()
        cur.line({40: stmt.owner}, {572: "This Period        Year-to-Date"}, size=8)
        ytd_begin, ytd_flows = _ytd_column(end, [additions, subtractions, fees, change])
        ytd_column = [ytd_begin, *ytd_flows, end]
        for index, (label, value) in enumerate(
            (
                ("Beginning Portfolio Value", begin),
                ("Additions", additions),
                ("Subtractions", subtractions),
                ("Transaction Costs, Fees & Charges", fees),
                ("Change in Investment Value*", change),
                ("Ending Portfolio Value**", end),
            )
        ):
            left = {40: stmt.address[index]} if index < len(stmt.address) else {}
            dollar = index in (0, 5)
            ytd = ytd_column[index]
            cur.line(
                {**left, 300: label},
                {500: money(value, dollar=dollar), 572: money(ytd, dollar=dollar)},
                size=8,
            )
        cur.skip()
        cur.line({40: "* Price moves of your holdings and the income they paid over"})
        cur.line({40: "the statement period."})
        cur.line({40: "** Securities without a price are not included."})
        cur.skip(12)
        cur.heading("Contact Information")
        cur.line({40: "Online"}, {300: "example.test"})
        cur.line({40: "Automated Telephone"}, {300: "(555) 010-0000"})
        cur.skip(12)
        cur.line({40: "Each account and its number is listed on page 2 of this synthetic"})
        cur.line({40: "report."})

    def index(self, cur: Cursor) -> None:
        cur.heading("Portfolio Summary", size=10)
        cur.heading("Accounts Included in This Report")
        cur.line(
            {40: "Page", 66: "Account Type/Name", 300: "Account Number"},
            {470: "Beginning Value", 572: "Ending Value"},
            font=BOLD,
        )
        begin_total = end_total = ZERO
        for index, account in enumerate(self.stmt.accounts):
            s = _summary(account, self.extras)
            begin_total += s.begin
            end_total += s.end
            printed = self.first_page[account.number] + INDEX_PAGE_SHIFT[index]
            cur.line({40: account.group}, font=BOLD)
            cur.line(
                {40: str(printed), 66: account.title, 300: account.number},
                {470: money(s.begin, dollar=index == 0), 572: money(s.end, dollar=index == 0)},
            )
        cur.line(
            {40: "Total Portfolio"},
            {470: money(begin_total, dollar=True), 572: money(end_total, dollar=True)},
            font=BOLD,
        )
        cur.skip(14)
        cur.line({40: "* Transfers between accounts included."})
        cur.line({40: "** Price moves of your holdings and the income they paid over"})
        cur.line({40: "the statement period."})
        cur.skip(14)
        cur.heading("More from your accounts")
        cur.line({40: "This paragraph is promotional filler text of the synthetic statement"})
        cur.line({40: "and offers nothing."})

    def portfolio_continued(self, cur: Cursor) -> None:
        cur.heading("Portfolio Summary (continued)", size=10)
        rows = [a for acc in self.stmt.accounts for a in acc.section("income")]
        dividends = sum((a.amount for a in rows if a.type == "dividend"), ZERO)
        interest = sum((a.amount for a in rows if a.type == "interest"), ZERO)
        cur.line({40: "Income Summary", 340: "Top Holdings"}, font=BOLD, size=8)
        cur.line(
            {340: "Description"},
            {150: "This Period", 220: "Year-to-Date", 520: "Value", 572: "Percent"},
            font=BOLD,
        )
        holdings: dict[str, tuple[str, Decimal]] = {}
        for acc in self.stmt.accounts:
            for p in acc.positions:
                name, value = holdings.get(p.key, (p.name, ZERO))
                holdings[p.key] = (name, value + p.market_value)
        top = sorted(holdings.items(), key=lambda kv: kv[1][1], reverse=True)[:4]
        total = sum((a.total_value for a in self.stmt.accounts), ZERO)
        income = (
            ("Taxable", dividends + interest),
            ("Dividends", dividends),
            ("Interest", interest),
            ("Total", dividends + interest),
        )
        for index, (label, value) in enumerate(income):
            symbol, (name, held) = top[index]
            cur.line(
                {40: label, 340: f"{name.title()[:22].strip()} ({symbol})"},
                {
                    150: money(value, dollar=index in (0, 3)),
                    220: money(_ytd(value), dollar=index in (0, 3)),
                    520: money(held, dollar=index == 0),
                    572: _pct(held, total),
                },
            )

    def endnotes(self, cur: Cursor) -> None:
        cur.heading("Additional Information and Endnotes", size=10)
        for text in (
            "Cost basis, gains and losses on these pages are invented figures and this",
            "text is filler of a synthetic test statement. Income and yield estimates are",
            "made up as well.",
            "No figure here belongs to a real person or account.",
        ):
            cur.line({40: text}, size=8)

    # -- account summary pages ----------------------------------------------------------

    def account_summary(self, cur: Cursor, account: Account, *, style: str) -> None:
        s = _summary(account, self.extras)
        value = money(s.end, dollar=True)
        left = value if style == "amount" else f"Account Value: {value}"
        self.banner(cur, account, left)
        # The IRA's summary prints U+2212 minus signs, the others "-".
        sign = MINUS if account.subtype == "ira" else "-"
        cur.line({40: f"Change in Account Value {money(s.end - s.begin, dollar=True)}"}, size=8)
        cur.skip()
        cur.line({40: "Account Summary", 340: "Account Holdings"}, font=BOLD, size=8)
        cur.line({}, {250: "This Period", 320: "Year-to-Date"}, font=BOLD)
        holdings = self._by_type(account)
        rows: list[tuple[str, Decimal, bool]] = [("Beginning Account Value", s.begin, True)]
        rows.append(("Additions", s.additions, True))
        if account.subtype == "ira":
            rows.append(("Contributions", s.deposits, False))
        else:
            rows.append(("Deposits", s.deposits, False))
        rows.append(("Securities Transferred In", s.transferred_in, False))
        rows.append(("Subtractions", s.subtractions, True))
        if account.subtype == "ira":
            rows.append(("Distributions", s.withdrawals, False))
        else:
            rows.append(("Withdrawals", s.withdrawals, False))
        rows.append(("Securities Transferred Out", s.transferred_out, False))
        rows.append(("Cards, Checking & Bill Payments", s.cards, False))
        rows.append(("Trans. Costs, Fees & Charges", s.costs_fees, False))
        rows.append(("Taxes Withheld", s.taxes, False))
        rows.append(("Change in Investment Value*", s.change, True))
        rows.append(("Ending Account Value", s.end, True))
        # Year-to-Date: the detail rows scaled, the group rows their sums, the
        # beginning whatever ends the column at the same ending value.
        ytd = {label: _ytd(amount) for label, amount, dollar in rows if not dollar}
        details = [label for label, _, dollar in rows if not dollar]
        added = sum((ytd[label] for label in details[:2]), ZERO)
        costs = ytd["Trans. Costs, Fees & Charges"]
        subtracted = sum((ytd[label] for label in details[2:]), ZERO) - costs
        change = _ytd(s.change)
        ytd["Additions"] = added
        ytd["Subtractions"] = subtracted
        ytd["Change in Investment Value*"] = change
        ytd["Ending Account Value"] = s.end
        ytd["Beginning Account Value"] = s.end - added - subtracted - costs - change
        right_rows = [("Holding Type", "Value", "Percent of Account")] + [
            (kind, f"{round(v):,}", _pct(v, s.end)) for kind, v in holdings
        ]
        right_rows.append(("Ending Market Value of Holdings", f"${round(s.end):,}", "100%"))
        for index, (label, amount, dollar) in enumerate(rows):
            right = right_rows[index] if index < len(right_rows) else None
            left_cells = {40 if dollar else 48: label}
            right_cells = {
                250: money(amount, dollar=dollar, minus=sign) if amount else "\u2014",
                320: money(ytd[label], dollar=dollar, minus=sign) if ytd[label] else "\u2014",
            }
            if right is not None:
                left_cells[340] = right[0]
                right_cells[520] = right[1]
                right_cells[572] = right[2]
            cur.line(left_cells, right_cells)
        cur.skip()
        cur.line({40: "* Market value changes of the holdings, with the income they paid in the"})
        cur.line({40: "statement period."})

    def _by_type(self, account: Account) -> list[tuple[str, Decimal]]:
        groups: dict[str, Decimal] = {}
        for p in account.positions:
            groups[p.group] = groups.get(p.group, ZERO) + p.market_value
        if account.cash_closing is not None:
            groups["Core Account"] = account.cash_closing
        return sorted(groups.items(), key=lambda kv: kv[1], reverse=True)

    def core_cash_flow(self, cur: Cursor, account: Account) -> None:
        self.banner(cur, account, "Account Summary (continued)")
        assert account.cash_opening is not None
        assert account.cash_closing is not None
        sign = MINUS if account.subtype == "ira" else "-"
        trades = account.section("trades")
        investment_rows = [
            (
                "Securities Bought",
                sum((a.amount + a.cost for a in trades if a.type == "buy"), ZERO),
            ),
            ("Securities Sold", sum((a.amount + a.cost for a in trades if a.type == "sell"), ZERO)),
            ("Dividends, Interest and Other Income D", _total(account.section("income"))),
            ("Transaction Costs", -sum((a.cost for a in trades), ZERO)),
        ]
        cash: list[tuple[str, Decimal]] = []
        for label, sections in (
            ("Deposits", ("deposits",)),
            ("Contributions", ("contributions",)),
            ("Transfers In", ("transferred_in",)),
            ("Withdrawals", ("withdrawals",)),
            ("Distributions", ("distributions",)),
            ("Transfers Out", ("transferred_out",)),
            ("Taxes Withheld", ("taxes",)),
            ("Checking Activity", ("checking",)),
            ("Debit Card Activity", ("debit_card",)),
            ("Bill Payments", ("bill_payments",)),
            ("Fees & Charges", ("fees",)),
        ):
            total = _total(account.section(*sections))
            if total:  # in-kind transfers move no cash
                cash.append((label, total))
        investment = sum((v for _, v in investment_rows), ZERO)
        management = sum((v for _, v in cash), ZERO)
        assert account.cash_opening + investment + management == account.cash_closing
        # Year-to-Date adds up too, and ends at the same balance as This Period.
        ytd_begin, ytd_flows = _ytd_column(
            account.cash_closing, [v for _, v in investment_rows] + [v for _, v in cash]
        )
        ytd_investment = sum(ytd_flows[: len(investment_rows)], ZERO)
        ytd_management = sum(ytd_flows[len(investment_rows) :], ZERO)
        assert ytd_begin > 0
        dividends = sum((a.amount for a in account.section("income") if a.type == "dividend"), ZERO)
        interest = sum((a.amount for a in account.section("income") if a.type == "interest"), ZERO)
        side = [
            ("Income Summary", None),
            ("Taxable", dividends + interest),
            ("Dividends", dividends),
            ("Interest", interest),
            ("Short-term Capital Gains", None),
            ("Long-term Capital Gains", None),
            ("Total", dividends + interest),
        ]
        rows = iter(ytd_flows)
        lines: list[tuple[str, Decimal | None, Decimal | None, bool]] = [
            ("Core Account Cash Flow", None, None, True),
            (f"Core Account: {account.core_name.title()}", None, None, False),
            ("Beginning Core Account Balance", account.cash_opening, ytd_begin, True),
            ("INVESTMENT ACTIVITY", None, None, True),
            *((label, value, next(rows), False) for label, value in investment_rows),
            ("Total Investment Activity", investment, ytd_investment, True),
            ("CASH MANAGEMENT ACTIVITY", None, None, True),
            *((label, value, next(rows), False) for label, value in cash),
            ("Total Cash Management Activity", management, ytd_management, True),
            ("Ending Core Account Balance", account.cash_closing, account.cash_closing, True),
        ]
        for index, (label, value, ytd, strong) in enumerate(lines):
            left = {40: label}
            right: dict[float, str] = {}
            if value is not None and ytd is not None:
                right = {
                    230: money(value, dollar=strong, minus=sign),
                    300: money(ytd, dollar=strong, minus=sign),
                }
            if index < len(side):
                side_label, side_value = side[index]
                left[340] = side_label
                if side_value is not None:
                    right[520] = money(side_value, dollar=True)
                    right[572] = money(_ytd(side_value), dollar=True)
                elif index:
                    right[520] = "\u2014"
                    right[572] = "\u2014"
            cur.line(left, right, font=BOLD if strong and value is None else FONT)
        cur.line({40: "D Reinvested dividends included."})

    # -- holdings -------------------------------------------------------------------------

    QTY, PRICE, VALUE, COST, GAIN, EAI, LAST = 280, 322, 380, 432, 482, 530, 572
    MATURITY = 192

    def _holdings_header(self, cur: Cursor, *, bond: bool = False, core: bool = False) -> None:
        last = ("Coupon", "Rate") if bond else ("Est. Yield", "(EY)")
        if core:
            first = {self.COST: "Total", self.GAIN: "Unrealized", self.EAI: "Est. Annual"}
            first[self.LAST] = last[0]
            cur.line({}, first, font=BOLD)
            cur.line(
                {40: "Description"},
                {
                    self.QTY: "Quantity",
                    self.PRICE: "Price",
                    self.VALUE: "Ending Value",
                    self.COST: "Cost Basis",
                    self.GAIN: "Gain/Loss",
                    self.EAI: "Income (EAI)",
                    self.LAST: last[1],
                },
                font=BOLD,
            )
            return
        cur.line(
            {},
            {
                self.PRICE: "Price",
                self.VALUE: "Ending",
                self.COST: "Total",
                self.GAIN: "Unrealized",
                self.EAI: "Est. Annual",
                self.LAST: last[0],
            },
            font=BOLD,
        )
        left = {40: "Description"}
        if bond:
            left[self.MATURITY] = "Maturity"
        cur.line(
            left,
            {
                self.QTY: "Quantity",
                self.PRICE: "Per Unit",
                self.VALUE: "Market Value",
                self.COST: "Cost Basis",
                self.GAIN: "Gain/Loss",
                self.EAI: "Income (EAI)",
                self.LAST: last[1],
            },
            font=BOLD,
        )

    def _position_row(self, cur: Cursor, p: Position, *, first: bool) -> None:
        label = p.name if p.symbol is None else f"{p.name} ({p.symbol})"
        name = wrap(label, 24 if p.per_100 else 34)
        cost = "unknown" if p.cost is None else money(p.cost, dollar=first) + p.cost_mark
        gain = "unknown" if p.cost is None else money(p.market_value - p.cost, dollar=first)
        eai = cents(p.market_value * Decimal("0.031"))
        left = {40: name[0]}
        if p.maturity is not None:
            left[self.MATURITY] = mdy_short(p.maturity)
        last = p.coupon or ("\u2014" if p.cost is None else "3.10%")
        cur.line(
            left,
            {
                self.QTY: number(p.quantity, p.qty_places),
                self.PRICE: f"{'$' if first else ''}{number(p.price, p.price_places)}"
                + p.price_mark,
                self.VALUE: money(p.market_value, dollar=first),
                self.COST: cost,
                self.GAIN: gain,
                self.EAI: "\u2014" if p.cost is None else money(eai, dollar=first),
                self.LAST: last,
            },
        )
        for extra in name[1:]:
            cur.line({40: extra})
        if p.cusip is not None:
            cur.line({40: f"CUSIP: {p.cusip}"})
        if p.isin is not None:
            cur.line({40: f"ISIN: {p.isin}"})
        for note in p.notes:
            cur.line({40: note})

    def _group_total(
        self, cur: Cursor, label: str, rows: list[Position], whole: Decimal, *, strong: bool
    ) -> None:
        value = sum((p.market_value for p in rows), ZERO)
        known = [p for p in rows if p.cost is not None]
        right = {self.VALUE: money(value, dollar=True)}
        if known:
            cost = sum((p.cost for p in known if p.cost is not None), ZERO)
            right[self.COST] = money(cost, dollar=True)
            right[self.GAIN] = money(sum((p.market_value for p in known), ZERO) - cost, dollar=True)
        cur.line(
            {40: f"Total {label} ({_pct(value, whole)} of account holdings)"},
            right,
            font=BOLD if strong else FONT,
        )

    def holdings(
        self,
        cur: Cursor,
        account: Account,
        groups: tuple[str, ...],
        *,
        first: bool,
        total: bool = False,
    ) -> None:
        self.banner(cur, account, "Holdings" if first else "Holdings (continued)")
        whole = account.total_value
        if first and account.cash_closing is not None:
            core = account.cash_closing
            assert account.cash_opening is not None
            cur.heading("Core Account", size=8)
            self._holdings_header(cur, core=True)
            name = wrap(f"{account.core_name} ({account.core_symbol})", 34)
            cur.line(
                {40: name[0]},
                {
                    self.QTY: number(core, 3),
                    self.PRICE: "$1.000",
                    self.VALUE: money(core, dollar=True),
                    self.COST: "N/A",
                    self.GAIN: "\u2014",
                    self.EAI: "\u2014",
                    self.LAST: "\u2014",
                },
            )
            for extra in name[1:]:
                cur.line({40: extra})
            cur.line({40: "\u2014 Interest rate: 0.015%"})
            cur.line(
                {40: f"Total Core Account ({_pct(core, whole)} of account holdings)"},
                {self.VALUE: money(core, dollar=True)},
                font=BOLD,
            )
            cur.skip()
        for group in groups:
            rows = [p for p in account.positions if p.group == group]
            heading = {
                "Exchange Traded Products": "Exchange Traded Products E (e.g. ETF, ETN)",
            }.get(group, group)
            cur.heading(heading, size=8)
            self._holdings_header(cur, bond=group == "Bonds")
            subgroups = list(dict.fromkeys(p.subgroup for p in rows))
            for subgroup in subgroups:
                chosen = [p for p in rows if p.subgroup == subgroup]
                if subgroup:
                    cur.line({40: subgroup}, font=BOLD)
                for index, position in enumerate(chosen):
                    self._position_row(cur, position, first=index == 0)
                if subgroup and group == "Bonds" and len(subgroups) > 1:
                    self._group_total(cur, subgroup, chosen, whole, strong=False)
            self._group_total(cur, group, rows, whole, strong=True)
            cur.skip()
        if total:
            cost = sum((p.cost for p in account.positions if p.cost is not None), ZERO)
            cur.line(
                {40: "Total Holdings"},
                {self.VALUE: money(whole, dollar=True), self.COST: money(cost, dollar=True)},
                font=BOLD,
            )
            cur.skip()
            cur.line({40: "Positions are in the cash account unless marked."})

    # -- activity tables ---------------------------------------------------------------------

    T_DATE, T_NAME, T_SYM, T_DESC = 40, 66, 212, 258
    T_QTY, T_PRICE, T_CB, T_TC, T_TOTAL = 372, 412, 456, 498, 572
    X_QTY, X_PRICE, X_AMOUNT = 420, 470, 572

    def _trade_header(self, cur: Cursor, *, cost: bool = True) -> None:
        first = {self.T_CB: "Cost Basis"}
        if cost:
            first[self.T_TC] = "Transaction"
        cur.line({self.T_DATE: "Settlement", self.T_SYM: "Symbol/"}, first, font=BOLD)
        second = {
            self.T_QTY: "Quantity",
            self.T_PRICE: "Price",
            self.T_CB: "of Close",
            self.T_TOTAL: "Transaction Total",
        }
        if cost:
            second[self.T_TC] = "Cost"
        cur.line(
            {
                self.T_DATE: "Date",
                self.T_NAME: "Security Name",
                self.T_SYM: "CUSIP",
                self.T_DESC: "Transaction Description",
            },
            second,
            font=BOLD,
        )

    def _trade_rows(self, cur: Cursor, rows: list[Activity], *, cost: bool = True) -> None:
        for index, a in enumerate(rows):
            assert a.quantity is not None
            assert a.price is not None
            first = index == 0
            name = wrap(a.name, 28)
            quantity = -a.quantity if a.type == "sell" else a.quantity
            right = {
                self.T_QTY: f"{quantity:,}",
                self.T_PRICE: f"{'$' if first else ''}{a.price:,}",
                self.T_TOTAL: money(a.amount, dollar=first),
            }
            if a.cost_basis is not None:
                right[self.T_CB] = money(a.cost_basis, dollar=True)
            if cost:
                right[self.T_TC] = money(-a.cost, dollar=first) if a.cost else "\u2014"
            cur.line(
                {
                    self.T_DATE: md(a.day),
                    self.T_NAME: name[0],
                    self.T_SYM: a.key or "",
                    self.T_DESC: a.description,
                },
                right,
            )
            # A wrapped name and a note under the row share their lines
            # ("ETF Wash sale of ...").
            extras, notes = name[1:], list(a.notes)
            for line in range(max(len(extras), len(notes))):
                cur.line(
                    {
                        self.T_NAME: extras[line] if line < len(extras) else "",
                        self.T_DESC: notes[line] if line < len(notes) else "",
                    }
                )

    def _security_header(self, cur: Cursor, *, amount: str = "Amount") -> None:
        cur.line(
            {self.T_DATE: "Settlement", self.T_SYM: "Symbol/"},
            {self.X_AMOUNT: "Transaction"},
            font=BOLD,
        )
        cur.line(
            {
                self.T_DATE: "Date",
                self.T_NAME: "Security Name",
                self.T_SYM: "CUSIP",
                self.T_DESC: "Transaction Description",
            },
            {self.X_QTY: "Quantity", self.X_PRICE: "Price", self.X_AMOUNT: amount},
            font=BOLD,
        )

    def _security_rows(self, cur: Cursor, rows: list[Activity], *, pad: bool = False) -> None:
        for index, a in enumerate(rows):
            first = index == 0
            name = wrap(a.name, 28)
            right = {self.X_AMOUNT: money(a.shown, dollar=first)}
            if a.quantity is not None:
                quantity = -a.quantity if a.type == "transfer_out" else a.quantity
                right[self.X_QTY] = f"{quantity:,}"
            if a.price is not None:
                right[self.X_PRICE] = f"{'$' if first else ''}{a.price:,}"
            cur.line(
                {
                    self.T_DATE: md(a.day, pad=pad),
                    self.T_NAME: name[0],
                    self.T_SYM: a.key or "",
                    self.T_DESC: a.description,
                },
                right,
            )
            for extra in name[1:]:
                cur.line({self.T_NAME: extra})
            for note in a.notes:
                cur.line({self.T_NAME: note})

    def activity_trades(self, cur: Cursor, account: Account) -> None:
        self.banner(cur, account, "Activity")
        trades = account.section("trades")
        cur.heading("Securities Bought & Sold", size=8)
        self._trade_header(cur)
        self._trade_rows(cur, trades)
        buys = [a for a in trades if a.type == "buy"]
        sells = [a for a in trades if a.type == "sell"]
        cur.line(
            {self.T_DATE: "Total Securities Bought"},
            {
                self.T_TC: money(-sum((a.cost for a in buys), ZERO), dollar=True),
                self.T_TOTAL: money(_total(buys), dollar=True),
            },
        )
        cur.line(
            {self.T_DATE: "Total Securities Sold"},
            {self.T_TOTAL: money(_total(sells), dollar=True)},
        )
        cur.line(
            {self.T_DATE: "Net Securities Bought & Sold"},
            {self.T_TOTAL: money(_total(trades), dollar=True)},
            font=BOLD,
        )
        pending = list(self.extras.pending.get(account.number, ()))
        if pending:
            cur.heading("Trades Pending Settlement", size=8)
            self._trade_header(cur, cost=False)
            self._trade_rows(cur, pending, cost=False)
            cur.line(
                {self.T_DATE: "Total Trades Pending Settlement"},
                {self.T_TOTAL: money(_total(pending), dollar=True)},
            )
        self._transfers(cur, account)

    def _transfers(self, cur: Cursor, account: Account) -> None:
        for section, title in (
            ("transferred_in", "Securities Transferred In"),
            ("transferred_out", "Securities Transferred Out"),
        ):
            rows = account.section(section)
            if not rows:
                continue
            cur.heading(title, size=8)
            self._security_header(cur)
            self._security_rows(cur, rows)
            shown = sum((a.shown for a in rows), ZERO)
            cur.line({self.T_DATE: f"Total {title}"}, {self.X_AMOUNT: money(shown, dollar=True)})

    def _income(self, cur: Cursor, account: Account, *, pad: bool) -> None:
        rows = account.section("income")
        cur.heading("Dividends, Interest & Other Income", size=8)
        cur.line({40: "(Dividend reinvestments included)"})
        self._security_header(cur)
        self._security_rows(cur, rows, pad=pad)
        cur.line(
            {self.T_DATE: "Total Dividends, Interest & Other Income"},
            {self.X_AMOUNT: money(_total(rows), dollar=True)},
        )

    def _side_by_side(
        self,
        cur: Cursor,
        title: str,
        columns: list[tuple[float, str, bool]],
        rows: list[list[str]],
        totals: list[list[str]],
        *,
        header2: list[tuple[float, str, bool]] | None = None,
    ) -> None:
        """Two copies of a table on one line: rows fill the left half, then the right.

        ``columns`` are ``(x in the left half, header, right-aligned)``; the
        right half is the same shifted by 272 points. The first total goes in
        the right half's next free slot when there is one (the layout's
        habit), the others on their own lines.
        """
        shift = 272.0
        cur.heading(title, size=8)

        def cells(
            values: list[str], half: int, spec: list[tuple[float, str, bool]]
        ) -> tuple[dict[float, str], dict[float, str]]:
            left: dict[float, str] = {}
            right: dict[float, str] = {}
            for (x, _, aligned_right), text in zip(spec, values, strict=False):
                (right if aligned_right else left)[x + shift * half] = text
            return left, right

        for spec in ([header2] if header2 else []) + [columns]:
            assert spec is not None
            names = [name for _, name, _ in spec]
            l0, r0 = cells(names, 0, spec)
            l1, r1 = cells(names, 1, spec)
            cur.line({**l0, **l1}, {**r0, **r1}, font=BOLD)
        half = (len(rows) + 1) // 2
        left_rows, right_rows = rows[:half], rows[half:]
        pending_totals = list(totals)
        for index, left_values in enumerate(left_rows):
            l0, r0 = cells(left_values, 0, columns)
            if index < len(right_rows):
                l1, r1 = cells(right_rows[index], 1, columns)
            elif pending_totals and index == len(right_rows):
                l1, r1 = cells(pending_totals.pop(0), 1, columns)
            else:
                l1, r1 = {}, {}
            cur.line({**l0, **l1}, {**r0, **r1})
        for total in pending_totals:
            l0, r0 = cells(total, 0, columns)
            cur.line(l0, r0)

    def _cash_table(self, cur: Cursor, title: str, total_label: str, rows: list[Activity]) -> None:
        columns = [
            (40.0, "Date", False),
            (64.0, "Reference", False),
            (110.0, "Transaction Description", False),
            (300.0, "Transaction Total", True),
        ]
        values = [
            [md(a.day), a.reference, a.description, money(a.amount, dollar=i == 0)]
            for i, a in enumerate(rows)
        ]
        total = ["", "", total_label, money(_total(rows), dollar=True)]
        self._side_by_side(cur, title, columns, values, [total])

    def activity_income_cash(self, cur: Cursor, account: Account) -> None:
        self.banner(cur, account, "Activity (continued)")
        self._income(cur, account, pad=True)
        cur.skip()
        self._cash_table(cur, "Deposits", "Total Deposits & EFT In", account.section("deposits"))
        cur.skip()
        self._cash_table(
            cur, "Withdrawals", "Total Withdrawals & EFT Out", account.section("withdrawals")
        )

    def activity_banking(self, cur: Cursor, account: Account) -> None:
        self.banner(cur, account, "Activity (continued)")
        cards = account.section("debit_card")
        cur.heading("Debit Card Activity", size=8)
        cur.line({40: "SAMPLE PLATINUM CHECK CARD (ACCOUNT ...3176)"}, font=BOLD)
        cur.line(
            {40: "Trans. Date", 84: "Post Date", 128: "Location / Description", 380: "Reference"},
            {572: "Transaction Total"},
            font=BOLD,
        )
        for index, a in enumerate(cards):
            assert a.post_day is not None
            cur.line(
                {40: md(a.day), 84: md(a.post_day), 128: a.description, 380: a.reference},
                {572: _minus(a.amount, dollar=index == 0)},
            )
        cur.line(
            {40: "Sample Platinum Check Card (...3176) Subtotal"},
            {572: _minus(_total(cards), dollar=True)},
        )
        cur.line({40: "Total Debit Card Activity"}, {572: _minus(_total(cards), dollar=True)})
        cur.skip()
        bills = account.section("bill_payments")
        bill_values = [
            [
                md(a.day),
                a.description,
                a.reference,
                money(a.amount, dollar=i == 0),
                money(_ytd(a.amount, "1.95"), dollar=i == 0),
            ]
            for i, a in enumerate(bills)
        ]
        bill_total = _total(bills)
        self._side_by_side(
            cur,
            "Bill Payments",
            [
                (40.0, "Date", False),
                (64.0, "Payee", False),
                (140.0, "Account", False),
                (238.0, "Amount", True),
                (300.0, "YTD Payments", True),
            ],
            bill_values,
            [
                [
                    "",
                    "Total Bill Payments",
                    "",
                    money(bill_total, dollar=True),
                    money(_ytd(bill_total, "1.95"), dollar=True),
                ]
            ],
            header2=[(40.0, "Post", False), (140.0, "Payee", False)],
        )
        cur.line({40: "* Recurring payee"})
        cur.skip()
        checks = account.section("checking")
        self._side_by_side(
            cur,
            "Checking Activity",
            [
                (40.0, "Check Num.", False),
                (84.0, "Post Date", False),
                (124.0, "Code", False),
                (146.0, "Description", False),
                (300.0, "Transaction Total", True),
            ],
            [
                [
                    a.reference,
                    md(a.day, pad=True),
                    "01",
                    a.description,
                    _minus(a.amount, dollar=i == 0),
                ]
                for i, a in enumerate(checks)
            ],
            [["", "", "", "Total Checking Activity", _minus(_total(checks), dollar=True)]],
        )
        cur.line({40: "* Gap in the check number sequence."})
        cur.skip()
        fees = account.section("fees")
        self._side_by_side(
            cur,
            "Fees and Charges",
            [(40.0, "Date", False), (64.0, "Description", False), (300.0, "Amount", True)],
            [
                [md(a.day), a.description, _minus(a.amount, dollar=i == 0)]
                for i, a in enumerate(fees)
            ],
            [["", "Total Fees and Charges", _minus(_total(fees), dollar=True)]],
        )
        cur.skip()
        self._taxes(cur, account)

    def _taxes(self, cur: Cursor, account: Account) -> None:
        taxes = account.section("taxes")
        by_kind: dict[str, Decimal] = {}
        for a in taxes:
            kind = a.description.split()[0]
            by_kind[kind] = by_kind.get(kind, ZERO) + a.amount
        totals = [
            ["", f"Total {kind} Taxes Withheld", "", money(value, dollar=True)]
            for kind, value in by_kind.items()
        ]
        totals.append(["", "Total Taxes Withheld", "", money(_total(taxes), dollar=True)])
        self._side_by_side(
            cur,
            "Taxes Withheld",
            [
                (40.0, "Date", False),
                (64.0, "Security", False),
                (214.0, "Description", False),
                (300.0, "Amount", True),
            ],
            [
                [md(a.day), a.name, a.description, money(a.amount, dollar=i == 0)]
                for i, a in enumerate(taxes)
            ],
            totals,
        )

    def _daily(self, cur: Cursor, account: Account) -> None:
        assert account.cash_opening is not None
        by_day: dict[date, list[Decimal]] = {}
        for a in account.activity:
            if a.amount:
                by_day.setdefault(a.day, []).append(a.amount)
        balance = account.cash_opening
        values: list[list[str]] = []
        for day in sorted(by_day)[:6]:
            amounts = by_day[day]
            added = sum((v for v in amounts if v > 0), ZERO)
            taken = sum((v for v in amounts if v < 0), ZERO)
            balance += added + taken
            values.append(
                [
                    md(day, pad=True),
                    _minus(added) if added else "\u2014",
                    _minus(taken) if taken else "\u2014",
                    _minus(added + taken),
                    _minus(balance),
                ]
            )
        cur.heading("Daily Additions and Subtractions", size=8)
        cur.line({40: f"{account.core_name.title()} @ $1 per share (shown for information)"})
        self._side_by_side(
            cur,
            "",
            [
                (40.0, "Date", False),
                (115.0, "Total Additions", True),
                (182.0, "Total Subtractions", True),
                (236.0, "Net Activity", True),
                (300.0, "Daily Balance", True),
            ],
            values,
            [],
        )

    def daily_and_cash_flow(self, cur: Cursor, account: Account) -> None:
        self.banner(cur, account, "Activity (continued)")
        self._daily(cur, account)
        cur.skip(14)
        cur.line({40: "Estimated Cash Flow"}, font=BOLD, size=9)
        cur.line(
            {40: f"(Rolling as of {self.stmt.end:%B} {self.stmt.end.day}, {self.stmt.end.year})"}
        )
        cur.line({}, {572: "Total Est."}, font=BOLD)
        cur.line(
            {40: "Month"},
            {
                200: "Bond & CD Income",
                290: "Bond & CD Principal",
                370: "Stock Income",
                470: "Mutual Fund Income",
                572: "Cash Flow",
            },
            font=BOLD,
        )
        month = self.stmt.end + timedelta(days=1)
        grand = ZERO
        for index in range(12):
            bond = Decimal(287) if index % 3 == 1 else ZERO
            stock = Decimal(58) if index % 3 == 0 else Decimal(141) if index % 3 == 1 else ZERO
            fund = Decimal(37)
            total = bond + stock + fund
            grand += total
            label = f"{month:%B}" + (f" {month.year}" if index == 0 or month.month == 1 else "")
            cur.line(
                {40: label},
                {
                    200: money(bond) if bond else "\u2014",
                    290: "\u2014",
                    370: money(stock) if stock else "\u2014",
                    470: money(fund),
                    572: money(total),
                },
            )
            month = _month_end(month) + timedelta(days=1)
        cur.line({40: "Total"}, {572: money(grand, dollar=True)}, font=BOLD)
        cur.line({40: "Estimated income only; filler text of the synthetic statement."})

    def ira_activity(self, cur: Cursor, account: Account) -> None:
        self.banner(cur, account, "Activity")
        cur.heading("Securities Bought & Sold", size=8)
        trades = account.section("trades")
        self._trade_header(cur)
        self._trade_rows(cur, trades)
        cur.line(
            {self.T_DATE: "Total Securities Bought"},
            {self.T_TOTAL: money(_total(trades), dollar=True)},
        )
        cur.skip()
        self._transfers(cur, account)
        cur.skip()
        self._income(cur, account, pad=True)
        cur.skip()
        for section, title, total in (
            ("contributions", "Contributions", "Total Contributions"),
            ("distributions", "Distributions", "Total Distributions"),
        ):
            self._cash_table(cur, title, total, account.section(section))
            cur.skip()
        self._taxes(cur, account)
        cur.skip()
        self._daily(cur, account)

    def college(self, cur: Cursor, account: Account) -> None:
        s = _summary(account, self.extras)
        self.banner(cur, account, f"Account Value: {money(s.end, dollar=True)}")
        cur.line(
            {40: f"Change in Account Value {money(s.end - s.begin, dollar=True)}"},
            {572: "College Investment Details"},
            size=8,
        )
        cur.line({}, {250: "This Period", 320: "Year-to-Date", 572: "Year-to-Date"}, font=BOLD)
        cap, contributed = Decimal("400000.00"), Decimal("21915.40")
        details = (
            (f"{self.stmt.end.year} Plan Contribution Cap:", money(cap, dollar=True)),
            ("Total Contributions Over Life of Plan:", money(contributed)),
            (
                f"{self.stmt.end.year} Remaining to Contribute:",
                money(cap - contributed, dollar=True),
            ),
        )
        rows = (
            ("Beginning Account Value", s.begin, True),
            ("Contributions", s.deposits, False),
            ("Distributions", s.withdrawals, False),
            ("Change in Investment Value", s.change, False),
            ("Ending Account Value", s.end, True),
        )
        ytd_begin, ytd_flows = _ytd_column(s.end, [s.deposits, s.withdrawals, s.change])
        ytd_column = [ytd_begin, *ytd_flows, s.end]
        for index, (label, value, dollar) in enumerate(rows):
            left = {40: label}
            ytd = ytd_column[index]
            right = {
                250: money(value, dollar=dollar) if value else "\u2014",
                320: money(ytd, dollar=dollar) if ytd else "\u2014",
            }
            if index < len(details):
                left[360] = details[index][0]
                right[572] = details[index][1]
            cur.line(left, right)
        cur.skip()
        cur.heading("Contribution Elections", size=8)
        cur.line({}, {572: "Percent of"}, font=BOLD)
        cur.line({40: "Description"}, {572: "Contributions"}, font=BOLD)
        for p, share in zip(account.positions, ("75%", "25%"), strict=True):
            cur.line({40: p.name.upper()}, {572: share})
        cur.skip()
        cur.heading("Holdings", size=8)
        cur.line({}, {330: "Percent of"}, font=BOLD)
        cur.line(
            {40: "Description"},
            {330: "Total Value", 400: "Quantity", 480: "Unit Price", 572: "Ending Value"},
            font=BOLD,
        )
        for index, p in enumerate(account.positions):
            cur.line(
                {40: f"{p.name} ({p.symbol})"},
                {
                    330: _pct(p.market_value, s.end),
                    400: number(p.quantity, 3),
                    480: f"{'$' if index == 0 else ''}{number(p.price, 3)}",
                    572: money(p.market_value, dollar=index == 0),
                },
            )
        cur.line({40: "Total Value"}, {572: money(account.holdings_value)}, font=BOLD)


def report_pdf(
    stmt: Report | None = None,
    extras: Extras | None = None,
    *,
    institution_metadata: bool = True,
) -> bytes:
    """The report as PDF bytes (module doc); the default report when none is given."""
    if stmt is None or extras is None:
        stmt, extras = report()
    layout = _Layout(stmt, extras)
    pages = layout.build()
    title = f"Investment Report | {INSTITUTION}" if institution_metadata else "Investment Report"
    return render(
        pages,
        layout.chrome,
        title=title,
        author=INSTITUTION if institution_metadata else "",
        subject="Synthetic test statement",
    )
