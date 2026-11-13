# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic delimited exports whose sign, or kind, is the question.

Every payee, ticker and figure here is invented; no layout copies a real
file's rows. Each builder returns the file's bytes (CRLF line ends, UTF-8),
so a test runs it through the real delimited extractor or the upload path.

* :func:`card_positive_purchases` - a card export that lists purchases as
  positive amounts and the payment as a negative one, with nothing else to
  say which way the money went.
* :func:`card_with_type_column` - the same rows beside a ``Type`` of
  ``Purchase``/``Payment``.
* :func:`card_merchant_with_type_column` - those rows under a ``Merchant``
  header (a header that names a card export).
* :func:`aggregator_debit_positive` - an aggregator's export: ``debit`` rows
  positive, ``credit`` rows negative.
* :func:`unsigned_with_indicator_header_unknown` - unsigned amounts beside a
  ``Credit Debit Indicator`` column (a header no indicator word names).
* :func:`lone_inflow_column_of_negatives` - one ``Inflow`` column holding
  negative cells.
* :func:`trades_list` / :func:`fund_transactions` - securities trades.
* :func:`checking_with_running_balance` - signed amounts and a balance that
  reconciles.
* :func:`checking_signed_no_balance` - signed amounts, mostly money out, and
  nothing that proves the sign.
* :func:`aggregator_two_accounts` - an aggregator's export of two accounts
  told apart by an ``Account`` column of names.
* :func:`export_with_unique_account_cells` - an ``Account`` column whose
  cells differ on every row (who the money went to, not whose it is).
* :func:`export_with_payee_and_category` - no account column; a ``Payee``
  and a ``Category`` column of a few repeated values.

Words, shapes and rows that are not transactions:

* :func:`export_dated_under` - the date column under another name
  (``Created``, ``Datetime``, ``Timestamp``, ``Date Processed``), its cells
  timestamps, optionally with a zone offset after a space.
* :func:`payments_gross_fee_net` - a payment service's ``Gross``, ``Fee``
  and ``Net`` columns, with or without a row that carries a fee.
* :func:`credit_debit_amount_columns` - ``Credit Amount`` and ``Debit
  Amount`` columns.
* :func:`wallet_export_with_opening_row` - ``Datetime``, ``Amount (total)``
  and ``Amount (fee)``; the first row holds the holder's name and an opening
  balance of ``$0`` and nothing else.
* :func:`export_with_short_footer` - two transactions under a five-column
  header, then two three-cell footer rows.
* :func:`card_with_pending_rows` - ``Date Processed`` with two rows whose
  date cell says ``Pending``.
* :func:`export_with_long_decimals` - one amount printed with eleven
  decimals (decimal commas).
* :func:`export_with_lettered_currency` - amounts behind a currency written
  in letters and a right-to-left mark.
* :func:`export_with_month_name_dates` - ``07Mar2026 00:00:00`` or
  ``14-Aug-25`` dates.
* :func:`checking_with_daily_balance` - a running balance printed on the
  last row of each day only.

Dates whose day/month order is the question:

* :func:`checking_dated` - a checking export with one row per given date
  cell, signed amounts and a balance that reconciles (nothing else about it
  is in doubt).
* :func:`export_with_two_date_columns` - two date columns beside
  ``Description``, ``Debit`` and ``Credit``.
* :func:`card_positive_purchases` with ``low_days`` - the card export dated
  on nine days of one month, none above the 12th.

Tables without a header row (the first line is the first transaction):

* :func:`headerless_five_quoted_columns` - five quoted cells a row: a date,
  a signed amount, an asterisk, an empty cell and the description.
* :func:`headerless_with_balance` - a date, a check number, a description,
  a debit, a credit and a running balance that reconciles.
* :func:`headerless_card_with_balance_owed` - a date, a description, a
  purchase, a payment and the balance *owed*, which adds up with the
  purchases read as money in.
* :func:`headerless_with_direction_cell` - a date, a description, a
  category, an amount, ``debit``/``credit`` and the account's name; the
  amounts unsigned, or signed the aggregator's way (money out positive).
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Final

Row = tuple[str, ...]

#: (date, description, amount as printed): eight purchases and one payment.
CARD_ROWS: Final[tuple[tuple[str, str, str], ...]] = (
    ("03/02/2026", "HARBOR LANE GROCERY", "54.18"),
    ("03/04/2026", "TIN ROOF DINER", "23.40"),
    ("03/07/2026", "NORTHGATE FUEL STOP", "41.75"),
    ("03/11/2026", "PAPER KITE BOOKS", "18.99"),
    ("03/14/2026", "PAYMENT RECEIVED THANK YOU", "-350.00"),
    ("03/17/2026", "LANTERN STREET PHARMACY", "12.60"),
    ("03/21/2026", "HARBOR LANE GROCERY", "67.02"),
    ("03/25/2026", "COPPER PINE HARDWARE", "88.30"),
    ("03/28/2026", "SILVER FERN CAFE", "9.25"),
)

#: (date, description, amount as printed, direction word): the aggregator's
#: convention, money out positive.
AGGREGATOR_ROWS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("2026-05-01", "Willow Court Rent", "1210.00", "debit"),
    ("2026-05-03", "Harbor Lane Grocery", "86.41", "debit"),
    ("2026-05-05", "Brightwater Utilities", "64.09", "debit"),
    ("2026-05-08", "Tin Roof Diner", "31.75", "debit"),
    ("2026-05-12", "Northgate Fuel Stop", "47.20", "debit"),
    ("2026-05-15", "Marlow Design Payroll", "-2840.00", "credit"),
    ("2026-05-19", "Paper Kite Books", "22.95", "debit"),
    ("2026-05-23", "Harbor Lane Grocery", "73.18", "debit"),
    ("2026-05-27", "Copper Pine Hardware", "15.60", "debit"),
    ("2026-05-29", "Refund Paper Kite Books", "-22.95", "credit"),
)

#: (date, account, description, amount as printed): a checking account (five
#: rows) and a card (four rows), interleaved, each from its own point of
#: view (money out negative).
TWO_ACCOUNT_ROWS: Final[tuple[tuple[str, str, str, str], ...]] = (
    ("2026-09-01", "Everyday Checking", "Willow Court Rent", "-1210.00"),
    ("2026-09-02", "Rewards Card", "Harbor Lane Grocery", "-62.47"),
    ("2026-09-04", "Everyday Checking", "Brightwater Utilities", "-58.90"),
    ("2026-09-08", "Everyday Checking", "Marlow Design Payroll", "2840.00"),
    ("2026-09-09", "Rewards Card", "Tin Roof Diner", "-27.35"),
    ("2026-09-12", "Everyday Checking", "Northgate Fuel Stop", "-43.15"),
    ("2026-09-15", "Rewards Card", "Paper Kite Books", "-19.80"),
    ("2026-09-19", "Everyday Checking", "Copper Pine Hardware", "-36.20"),
    ("2026-09-23", "Rewards Card", "Payment Received Thank You", "109.62"),
)


def _file(header: Row, rows: Sequence[Row], *, delimiter: str = ",") -> bytes:
    lines = [delimiter.join(cells) for cells in (header, *rows)]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


def card_positive_purchases(*, low_days: bool = False) -> bytes:
    """``Date,Description,Amount``: eight purchases positive, one payment negative.

    ``low_days`` dates the rows ``03/01/2026`` to ``03/09/2026``: one month,
    and no day that only one day/month order reads.
    """
    rows: Sequence[Row] = CARD_ROWS
    if low_days:
        rows = [(f"03/{day:02d}/2026", *row[1:]) for day, row in enumerate(CARD_ROWS, start=1)]
    return _file(("Date", "Description", "Amount"), rows)


def card_with_type_column() -> bytes:
    """:func:`card_positive_purchases` plus ``Type`` (``Purchase``/``Payment``)."""
    rows = [(*row, "Payment" if row[2].startswith("-") else "Purchase") for row in CARD_ROWS]
    return _file(("Date", "Description", "Amount", "Type"), rows)


def card_merchant_with_type_column() -> bytes:
    """:func:`card_with_type_column` under ``Date,Merchant,Amount,Type``."""
    rows = [(*row, "Payment" if row[2].startswith("-") else "Purchase") for row in CARD_ROWS]
    return _file(("Date", "Merchant", "Amount", "Type"), rows)


def aggregator_debit_positive() -> bytes:
    """``Date,Description,Amount,Transaction Type``: debits positive, credits negative."""
    return _file(("Date", "Description", "Amount", "Transaction Type"), AGGREGATOR_ROWS)


def unsigned_with_indicator_header_unknown() -> bytes:
    """Unsigned ``Amount`` beside ``Credit Debit Indicator`` (``Debit``/``Credit``)."""
    rows = [
        ("2026-06-02", "Harbor Lane Grocery", "58.30", "Debit"),
        ("2026-06-04", "Northgate Fuel Stop", "39.14", "Debit"),
        ("2026-06-09", "Tin Roof Diner", "27.65", "Debit"),
        ("2026-06-15", "Marlow Design Payroll", "2840.00", "Credit"),
        ("2026-06-18", "Brightwater Utilities", "61.02", "Debit"),
        ("2026-06-24", "Lantern Street Pharmacy", "14.85", "Debit"),
    ]
    return _file(("Date", "Description", "Amount", "Credit Debit Indicator"), rows)


def lone_inflow_column_of_negatives() -> bytes:
    """``Date;Payee;Inflow`` with every cell negative (decimal commas)."""
    rows = [
        ("2026-04-03", "Harbor Lane Grocery", "-48,12"),
        ("2026-04-06", "Tin Roof Diner", "-19,80"),
        ("2026-04-11", "Northgate Fuel Stop", "-52,45"),
        ("2026-04-16", "Paper Kite Books", "-27,30"),
        ("2026-04-22", "Silver Fern Cafe", "-8,75"),
    ]
    return _file(("Date", "Payee", "Inflow"), rows, delimiter=";")


def trades_list() -> bytes:
    """``date,ticker,qty,price,amount,name``: trades with no action column."""
    rows = [
        ("2026-02-03", "ZQXA", "10", "41.20", "412.00", "Buy Zephyr Quarry A"),
        ("2026-02-10", "MLWD", "25", "12.08", "302.00", "Buy Marlow Wood"),
        ("2026-02-17", "ZQXA", "4", "43.50", "-174.00", "Sell Zephyr Quarry A"),
        ("2026-02-24", "TNRF", "8", "56.25", "450.00", "Buy Tin Roof Holdings"),
        ("2026-03-03", "MLWD", "10", "12.90", "129.00", "Buy Marlow Wood"),
    ]
    return _file(("date", "ticker", "qty", "price", "amount", "name"), rows)


def fund_transactions() -> bytes:
    """``Date,Fund,Transaction Type,Shares,Price,Amount`` with ``Buy``/``Sell``."""
    rows = [
        ("2026-01-15", "Copper Pine Balanced Fund", "Buy", "12.500", "20.00", "250.00"),
        ("2026-02-13", "Copper Pine Balanced Fund", "Buy", "12.255", "20.40", "250.00"),
        ("2026-03-13", "Silver Fern Bond Fund", "Buy", "24.038", "10.40", "250.00"),
        ("2026-04-15", "Copper Pine Balanced Fund", "Sell", "5.000", "21.10", "105.50"),
        ("2026-05-15", "Silver Fern Bond Fund", "Buy", "23.810", "10.50", "250.00"),
    ]
    return _file(("Date", "Fund", "Transaction Type", "Shares", "Price", "Amount"), rows)


def checking_with_running_balance() -> bytes:
    """``Date,Description,Amount,Balance``: signed amounts, each balance the sum so far."""
    rows = [
        ("2026-07-01", "Willow Court Rent", "-1210.00", "1630.00"),
        ("2026-07-03", "Harbor Lane Grocery", "-74.60", "1555.40"),
        ("2026-07-08", "Northgate Fuel Stop", "-45.10", "1510.30"),
        ("2026-07-15", "Marlow Design Payroll", "2840.00", "4350.30"),
        ("2026-07-19", "Tin Roof Diner", "-33.25", "4317.05"),
        ("2026-07-26", "Brightwater Utilities", "-66.40", "4250.65"),
    ]
    return _file(("Date", "Description", "Amount", "Balance"), rows)


def checking_signed_no_balance() -> bytes:
    """``Date,Description,Amount``: mostly money out, one deposit, no other column."""
    rows = [
        ("2026-08-01", "Willow Court Rent", "-1210.00"),
        ("2026-08-04", "Harbor Lane Grocery", "-81.35"),
        ("2026-08-09", "Northgate Fuel Stop", "-44.70"),
        ("2026-08-14", "Marlow Design Payroll", "2840.00"),
        ("2026-08-20", "Tin Roof Diner", "-29.15"),
        ("2026-08-27", "Brightwater Utilities", "-63.80"),
    ]
    return _file(("Date", "Description", "Amount"), rows)


def aggregator_two_accounts(header: str = "Account") -> bytes:
    """``Date,Account,Description,Amount``: ``Everyday Checking`` and ``Rewards Card`` rows.

    ``header`` renames the account column (``Account Name``, ``Source Account``).
    """
    return _file(("Date", header, "Description", "Amount"), TWO_ACCOUNT_ROWS)


def export_with_unique_account_cells() -> bytes:
    """``Date,Account,Description,Amount``: a different ``Account`` cell on every row."""
    rows = [
        ("2026-10-01", "Willow Court Lettings", "Rent October", "-1210.00"),
        ("2026-10-03", "Harbor Lane Grocery", "Weekly shop", "-71.25"),
        ("2026-10-06", "Brightwater Utilities", "Water and power", "-60.40"),
        ("2026-10-10", "Marlow Design", "Payroll", "2840.00"),
        ("2026-10-14", "Northgate Fuel Stop", "Fuel", "-46.85"),
        ("2026-10-21", "Tin Roof Diner", "Dinner", "-34.10"),
    ]
    return _file(("Date", "Account", "Description", "Amount"), rows)


def export_with_payee_and_category() -> bytes:
    """``Date,Payee,Category,Amount``: three payees and three categories, each repeated."""
    rows = [
        ("2026-11-02", "Harbor Lane Grocery", "Groceries", "-58.30"),
        ("2026-11-04", "Northgate Fuel Stop", "Transport", "-41.90"),
        ("2026-11-06", "Marlow Design", "Income", "1420.00"),
        ("2026-11-09", "Harbor Lane Grocery", "Groceries", "-63.75"),
        ("2026-11-13", "Northgate Fuel Stop", "Transport", "-39.20"),
        ("2026-11-16", "Harbor Lane Grocery", "Groceries", "-22.10"),
        ("2026-11-20", "Marlow Design", "Income", "1420.00"),
        ("2026-11-24", "Northgate Fuel Stop", "Transport", "-44.65"),
    ]
    return _file(("Date", "Payee", "Category", "Amount"), rows)


#: (timestamp, description, amount as printed): mostly money out, one deposit.
STAMPED_ROWS: Final[tuple[tuple[str, str, str], ...]] = (
    ("2026-02-03 09:12:44", "Harbor Lane Grocery", "-61.30"),
    ("2026-02-06 18:40:02", "Tin Roof Diner", "-27.85"),
    ("2026-02-11 07:55:19", "Northgate Fuel Stop", "-43.10"),
    ("2026-02-14 11:03:27", "Marlow Design Payroll", "2840.00"),
    ("2026-02-20 16:21:37", "Paper Kite Books", "-17.45"),
    ("2026-02-25 20:48:09", "Brightwater Utilities", "-59.20"),
)


def export_dated_under(header: str, *, zone: str = "") -> bytes:
    """``<header>,Description,Amount``: timestamps under ``header``.

    ``zone`` is appended to every timestamp (``" +0000"``: an offset after a space).
    """
    rows = [(f"{stamp}{zone}", payee, amount) for stamp, payee, amount in STAMPED_ROWS]
    return _file((header, "Description", "Amount"), rows)


def payments_gross_fee_net(*, fees: bool = False) -> bytes:
    """``Date,Time,Name,Status,Currency,Gross,Fee,Net``: a payment service's export.

    Without ``fees`` every fee is zero, so gross and net are the same figure;
    with ``fees`` the one sale carries a fee and they differ on that row.
    """
    sale = ("120.00", "-3.78", "116.22") if fees else ("120.00", "0.00", "120.00")
    rows = [
        ("2026-03-03", "08:14:10", "Harbor Lane Grocery", "Completed", "USD", "-48.20", "0.00"),
        ("2026-03-06", "19:02:41", "Tin Roof Diner", "Completed", "USD", "-26.75", "0.00"),
        ("2026-03-12", "10:30:05", "Juniper Row Books", "Completed", "USD", *sale[:2]),
        ("2026-03-17", "13:47:52", "Northgate Fuel Stop", "Completed", "USD", "-39.90", "0.00"),
        ("2026-03-24", "21:15:33", "Silver Fern Cafe", "Completed", "USD", "-8.40", "0.00"),
    ]
    nets = ["-48.20", "-26.75", sale[2], "-39.90", "-8.40"]
    header = ("Date", "Time", "Name", "Status", "Currency", "Gross", "Fee", "Net")
    return _file(header, [(*row, net) for row, net in zip(rows, nets, strict=True)])


def credit_debit_amount_columns() -> bytes:
    """``Date,Description,Credit Amount,Debit Amount,Currency``: one figure per row."""
    rows = [
        ("2026-04-02", "Marlow Design invoice 14", "1850.00", "", "USD"),
        ("2026-04-05", "Card load", "", "200.00", "USD"),
        ("2026-04-09", "Withdrawal to bank", "", "1500.00", "USD"),
        ("2026-04-16", "Marlow Design invoice 15", "920.00", "", "USD"),
        ("2026-04-23", "Service charge", "", "3.00", "USD"),
    ]
    return _file(("Date", "Description", "Credit Amount", "Debit Amount", "Currency"), rows)


def wallet_export_with_opening_row() -> bytes:
    """A wallet's statement: the first row is the holder's name and ``$0`` only.

    ``Holder,ID,Datetime,Description,From,To,Amount (total),Amount (fee),Opening
    Balance``; amounts are printed ``- $14.50`` / ``+ $40.00``.
    """
    header = (
        "Holder",
        "ID",
        "Datetime",
        "Description",
        "From",
        "To",
        "Amount (total)",
        "Amount (fee)",
        "Opening Balance",
    )
    activity = [
        ("7001", "2026-04-02T09:14:05", "Lunch", "Ada Finch", "Noor Vale", "- $14.50"),
        ("7002", "2026-04-05T20:31:48", "Tickets", "Ada Finch", "Ines Okoro", "- $36.00"),
        ("7003", "2026-04-09T12:02:17", "Rent share", "Noor Vale", "Ada Finch", "+ $40.00"),
        ("7004", "2026-04-15T08:45:30", "Coffee", "Ada Finch", "Noor Vale", "- $4.75"),
        ("7005", "2026-04-21T17:20:56", "Groceries", "Ada Finch", "Ines Okoro", "- $22.10"),
    ]
    rows: list[Row] = [("harborfinch", "", "", "", "", "", "", "", "$0")]
    rows += [("", *cells, "$0.00", "") for cells in activity]
    return _file(header, rows)


def export_with_short_footer() -> bytes:
    """Two transactions under a five-column header, then two three-cell footer rows."""
    rows: list[Row] = [
        ("2026-10-14", "Harbor Lane Grocery", "R-1001", "-52.40", "1147.60"),
        ("2026-10-28", "Tin Roof Diner", "R-1002", "-31.15", "1116.45"),
        ("Overdraft limit agreed", "2026-10-31", "+0.00"),
        ("Interest charged this period", "2026-10-31", "+0.00"),
    ]
    return _file(("Date", "Description", "Reference", "Amount", "Balance"), rows)


def card_with_pending_rows() -> bytes:
    """``Date Processed,Description,Amount``: two rows not yet processed (``Pending``)."""
    rows = [
        ("Pending", "SILVER FERN CAFE", "-7.80"),
        ("Pending", "NORTHGATE FUEL STOP", "-38.25"),
        ("27/10/2026", "HARBOR LANE GROCERY", "-45.10"),
        ("25/10/2026", "PAYMENT RECEIVED THANK YOU", "300.00"),
        ("21/10/2026", "TIN ROOF DINER", "-29.60"),
        ("16/10/2026", "PAPER KITE BOOKS", "-13.99"),
        ("14/10/2026", "COPPER PINE HARDWARE", "-64.35"),
    ]
    return _file(("Date Processed", "Description", "Amount"), rows)


def export_with_long_decimals() -> bytes:
    """``Date;Payee;Amount`` (decimal commas): one amount has eleven decimals."""
    rows = [
        ("2026-06-02", "Harbor Lane Grocery", "-58,30"),
        ("2026-06-05", "Copper Pine Fund distribution", "318,40277777778"),
        ("2026-06-09", "Tin Roof Diner", "-27,65"),
        ("2026-06-18", "Brightwater Utilities", "-61,02"),
        ("2026-06-24", "Lantern Street Pharmacy", "-14,85"),
    ]
    return _file(("Date", "Payee", "Amount"), rows, delimiter=";")


#: A currency written in letters with a right-to-left mark before the number.
LETTERED_CURRENCY: Final = "\u062f.\u0625.\u200f"


def export_with_lettered_currency() -> bytes:
    """``Date,Payee,Outflow,Inflow``: every amount behind :data:`LETTERED_CURRENCY`."""
    figures = [
        ("2026-05-03", "Harbor Lane Grocery", "18.250", "0.000"),
        ("2026-05-07", "Tin Roof Diner", "4.500", "0.000"),
        ("2026-05-15", "Marlow Design Payroll", "0.000", "1250.000"),
        ("2026-05-22", "Northgate Fuel Stop", "9.750", "0.000"),
    ]
    rows = [
        (day, payee, f"{LETTERED_CURRENCY}{out}", f"{LETTERED_CURRENCY}{into}")
        for day, payee, out, into in figures
    ]
    return _file(("Date", "Payee", "Outflow", "Inflow"), rows)


def export_with_month_name_dates(*, short_year: bool = False) -> bytes:
    """``Date,Description,Amount``: ``07Mar2026 00:00:00`` dates, or ``14-Aug-25`` ones."""
    days = (
        ("14-Aug-25", "19-Aug-25", "02-Sep-25", "23-Sep-25")
        if short_year
        else (
            "07Mar2026 00:00:00",
            "15Mar2026 00:00:00",
            "02Apr2026 00:00:00",
            "21Apr2026 00:00:00",
        )
    )
    rows = [
        (days[0], "Harbor Lane Grocery", "-47.35"),
        (days[1], "Marlow Design Payroll", "2840.00"),
        (days[2], "Tin Roof Diner", "-25.10"),
        (days[3], "Northgate Fuel Stop", "-41.80"),
    ]
    return _file(("Date", "Description", "Amount"), rows)


def checking_with_daily_balance() -> bytes:
    """``Date,Description,Amount,Balance``: the balance on the last row of each day only."""
    rows = [
        ("2026-07-01", "Willow Court Rent", "-1210.00", ""),
        ("2026-07-01", "Harbor Lane Grocery", "-74.60", "1555.40"),
        ("2026-07-03", "Northgate Fuel Stop", "-45.10", ""),
        ("2026-07-03", "Tin Roof Diner", "-33.25", ""),
        ("2026-07-03", "Marlow Design Payroll", "2840.00", "4317.05"),
        ("2026-07-08", "Brightwater Utilities", "-66.40", "4250.65"),
    ]
    return _file(("Date", "Description", "Amount", "Balance"), rows)


#: (description, amount as printed) of :func:`checking_dated`, repeated as
#: often as there are dates: money out negative, a salary every sixth row.
DATED_ROWS: Final[tuple[tuple[str, str], ...]] = (
    ("Willow Court Rent", "-1210.00"),
    ("Harbor Lane Grocery", "-68.45"),
    ("Northgate Fuel Stop", "-42.90"),
    ("Tin Roof Diner", "-27.15"),
    ("Brightwater Utilities", "-61.30"),
    ("Marlow Design Payroll", "2840.00"),
)
#: The balance before the first row of :func:`checking_dated`.
DATED_OPENING: Final = Decimal("2975.20")


def checking_dated(dates: Sequence[str]) -> bytes:
    """``Date,Description,Amount,Balance``: one row per cell of ``dates``, as printed.

    The amounts are signed and every balance is the sum so far, so the sign
    is proven and the date column is the only thing left to doubt.
    """
    balance = DATED_OPENING
    rows = []
    for index, cell in enumerate(dates):
        payee, amount = DATED_ROWS[index % len(DATED_ROWS)]
        balance += Decimal(amount)
        rows.append((cell, payee, amount, str(balance)))
    return _file(("Date", "Description", "Amount", "Balance"), rows)


def export_with_two_date_columns(
    dates: Sequence[tuple[str, str]],
    *,
    headers: tuple[str, str] = ("Transaction Date", "Post Date"),
) -> bytes:
    """``<headers>,Description,Debit,Credit``: one row per pair of date cells, as printed.

    The debit and credit columns prove the sign, whatever the header says
    about a card.
    """
    rows = []
    for index, (first, second) in enumerate(dates):
        payee, amount = DATED_ROWS[index % len(DATED_ROWS)]
        debit, credit = (amount[1:], "") if amount.startswith("-") else ("", amount)
        rows.append((first, second, payee, debit, credit))
    return _file((*headers, "Description", "Debit", "Credit"), rows)


# ---------------------------------------------------------------------------
# Tables without a header row
# ---------------------------------------------------------------------------


def _rows(rows: Sequence[Row], *, quoted: bool = False) -> bytes:
    """``rows`` as a file with no header line (every cell in double quotes when ``quoted``)."""
    lines = [",".join(f'"{cell}"' if quoted else cell for cell in cells) for cells in rows]
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


#: (date, amount as printed, description): seven rows of money out, one of money in.
HEADERLESS_SIGNED_ROWS: Final[tuple[tuple[str, str, str], ...]] = (
    ("09/02/2026", "-1210.00", "WILLOW COURT RENT ONLINE TRANSFER REF 0041"),
    ("09/04/2026", "-48.15", "PURCHASE AUTHORIZED ON 09/03 HARBOR LANE GROCERY CARD 0000"),
    ("09/09/2026", "-39.60", "PURCHASE AUTHORIZED ON 09/08 NORTHGATE FUEL STOP CARD 0000"),
    ("09/15/2026", "2840.00", "MARLOW DESIGN PAYROLL DIRECT DEPOSIT"),
    ("09/17/2026", "-27.35", "PURCHASE AUTHORIZED ON 09/16 TIN ROOF DINER CARD 0000"),
    ("09/21/2026", "-61.02", "BRIGHTWATER UTILITIES BILL PAYMENT"),
    ("09/24/2026", "-18.99", "PURCHASE AUTHORIZED ON 09/23 PAPER KITE BOOKS CARD 0000"),
    ("09/28/2026", "-14.85", "PURCHASE AUTHORIZED ON 09/27 LANTERN STREET PHARMACY CARD 0000"),
)


def headerless_five_quoted_columns() -> bytes:
    """No header; ``"date","amount","*","","description"`` with every cell quoted."""
    return _rows(
        [(day, amount, "*", "", text) for day, amount, text in HEADERLESS_SIGNED_ROWS],
        quoted=True,
    )


#: (date, check number, description, debit, credit): six rows of money out
#: (two of them checks) and two of money in.
HEADERLESS_BALANCE_ROWS: Final[tuple[tuple[str, str, str, str, str], ...]] = (
    ("09/01/2026", "", "MARLOW DESIGN PAYROLL", "", "2840.00"),
    ("09/02/2026", "1041", "CHECK WILLOW COURT RENT", "1210.00", ""),
    ("09/04/2026", "", "HARBOR LANE GROCERY", "74.60", ""),
    ("09/09/2026", "", "NORTHGATE FUEL STOP", "45.10", ""),
    ("09/14/2026", "1042", "CHECK BRIGHTWATER UTILITIES", "66.40", ""),
    ("09/15/2026", "", "MARLOW DESIGN PAYROLL", "", "2840.00"),
    ("09/19/2026", "", "TIN ROOF DINER", "33.25", ""),
    ("09/26/2026", "", "LANTERN STREET PHARMACY", "14.85", ""),
)
#: The balance before the first row of :func:`headerless_with_balance`.
HEADERLESS_OPENING: Final = Decimal("280.55")


def headerless_with_balance() -> bytes:
    """No header; ``date, check no, description, debit, credit, balance`` that reconciles."""
    balance = HEADERLESS_OPENING
    rows = []
    for day, check, text, debit, credit in HEADERLESS_BALANCE_ROWS:
        balance += Decimal(credit or "0") - Decimal(debit or "0")
        rows.append((day, check, text, debit, credit, str(balance)))
    return _rows(rows)


#: What was owed before the first row of :func:`headerless_card_with_balance_owed`.
HEADERLESS_OWED: Final = Decimal("412.90")


def headerless_card_with_balance_owed() -> bytes:
    """No header; ``date, description, purchase, payment, balance owed`` (:data:`CARD_ROWS`).

    The balance is what the holder owes: it goes up with a purchase and
    down with a payment, the mirror image of an account's balance.
    """
    owed = HEADERLESS_OWED
    rows = []
    for day, text, amount in CARD_ROWS:
        owed += Decimal(amount)
        purchase, payment = ("", amount[1:]) if amount.startswith("-") else (amount, "")
        rows.append((day, text, purchase, payment, str(owed)))
    return _rows(rows)


#: The category beside each row of :data:`AGGREGATOR_ROWS`.
HEADERLESS_CATEGORIES: Final = (
    "Housing",
    "Groceries",
    "Utilities",
    "Dining",
    "Transport",
    "Income",
    "Shopping",
    "Groceries",
    "Home",
    "Shopping",
)


def headerless_with_direction_cell(*, signed: bool = False) -> bytes:
    """No header; ``date, description, category, amount, debit|credit, account``.

    The amounts carry no sign (the word beside them says which way the
    money went). ``signed`` prints them the aggregator's way instead:
    ``debit`` rows positive, ``credit`` rows negative.
    """
    account = "Everyday Checking"
    rows = [
        (day, text, category, amount if signed else amount.removeprefix("-"), word, account)
        for (day, text, amount, word), category in zip(
            AGGREGATOR_ROWS, HEADERLESS_CATEGORIES, strict=True
        )
    ]
    return _rows(rows)
