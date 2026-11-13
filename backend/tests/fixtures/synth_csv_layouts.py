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
"""

from __future__ import annotations

from collections.abc import Sequence
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


def card_positive_purchases() -> bytes:
    """``Date,Description,Amount``: eight purchases positive, one payment negative."""
    return _file(("Date", "Description", "Amount"), CARD_ROWS)


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
