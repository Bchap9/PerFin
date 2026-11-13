# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic OFX files in the shapes real exporters produce but the spec does not.

Every builder returns ``bytes`` for one checking statement holding the
invented rows of :data:`ROWS`; only the wrapping differs (the declaration, the
encoding it names, SGML leaves under an XML declaration, an escaped payee).
The last three builders print records an extractor used to lose without a
word: leaves with no value (:data:`VALUELESS_ROWS`), a transaction after a
stray end of the list (:data:`STRAY_END_ROWS`), an amount that is not a
number. Nothing here is copied from a real file.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Final

Row = tuple[date, Decimal, str]

#: The invented statement every builder prints: ``(posted, amount, payee)``.
ROWS: Final[list[Row]] = [
    (date(2026, 3, 3), Decimal("-6.25"), "Harbor Lane Bakery"),
    (date(2026, 3, 9), Decimal("1250.00"), "Northfield Payroll"),
    (date(2026, 3, 17), Decimal("-42.18"), "Quill Stationers"),
]

#: ``(posted, amount, memo)`` of :func:`sgml_with_valueless_leaves`: the type,
#: id and name lines of each are printed with no value, so the memo is the text.
VALUELESS_ROWS: Final[list[Row]] = [
    (date(2026, 4, 2), Decimal("-18.40"), "POS purchase Fernhill Grocer"),
    (date(2026, 4, 7), Decimal("-64.00"), "POS purchase Tidewater Fuel"),
    (date(2026, 4, 15), Decimal("980.25"), "Deposit Marlow Street branch"),
    (date(2026, 4, 21), Decimal("-9.99"), "POS purchase Lantern Books"),
]

#: The two rows of :func:`sgml_with_stray_list_end`.
STRAY_END_ROWS: Final[list[Row]] = [
    (date(2026, 5, 4), Decimal("-31.75"), "Copper Kettle Cafe"),
    (date(2026, 5, 11), Decimal("-750.00"), "Transfer to savings"),
]

#: The row :func:`sgml_with_unreadable_amount` prints with ``<TRNAMT>abc``.
UNREADABLE_ROW: Final = 1

ACCOUNT: Final = "000077001234"

XML_DECLARATION: Final = '<?xml version="1.0" encoding="{encoding}" standalone="no"?>\n'
OFX_INSTRUCTION: Final = (
    '<?OFX OFXHEADER="200" VERSION="211" SECURITY="NONE" OLDFILEUID="NONE" NEWFILEUID="NONE"?>\n'
)
SGML_HEADER: Final = (
    "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\n"
    "CHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
)


def _day(day: date) -> str:
    return day.strftime("%Y%m%d")


def _fitid(index: int) -> str:
    return f"SYN-VAR-{index:04d}"


def xml_body(rows: list[Row] | None = None) -> str:
    """An OFX 2 body: every element closed."""
    rows = ROWS if rows is None else rows
    transactions = "".join(
        "<STMTTRN>"
        f"<TRNTYPE>{'CREDIT' if amount > 0 else 'DEBIT'}</TRNTYPE>"
        f"<DTPOSTED>{_day(posted)}</DTPOSTED><TRNAMT>{amount}</TRNAMT>"
        f"<FITID>{_fitid(index)}</FITID><NAME>{payee}</NAME>"
        "</STMTTRN>\n"
        for index, (posted, amount, payee) in enumerate(rows)
    )
    return (
        "<OFX>\n"
        "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0</CODE><SEVERITY>INFO</SEVERITY></STATUS>"
        "<DTSERVER>20260401</DTSERVER><LANGUAGE>ENG</LANGUAGE></SONRS></SIGNONMSGSRSV1>\n"
        "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1</TRNUID><STMTRS>\n"
        "<CURDEF>USD</CURDEF>\n"
        f"<BANKACCTFROM><BANKID>000000000</BANKID><ACCTID>{ACCOUNT}</ACCTID>"
        "<ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>\n"
        f"<BANKTRANLIST><DTSTART>{_day(rows[0][0])}</DTSTART>"
        f"<DTEND>{_day(rows[-1][0])}</DTEND>\n"
        f"{transactions}"
        "</BANKTRANLIST>\n"
        f"<LEDGERBAL><BALAMT>1201.57</BALAMT><DTASOF>{_day(rows[-1][0])}</DTASOF></LEDGERBAL>\n"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n"
        "</OFX>\n"
    )


def sgml_transaction(index: int, row: Row) -> str:
    """One OFX 1 ``STMTTRN``: leaves with no end tag, the aggregate closed."""
    posted, amount, payee = row
    return (
        "<STMTTRN>\n"
        f"<TRNTYPE>{'CREDIT' if amount > 0 else 'DEBIT'}\n"
        f"<DTPOSTED>{_day(posted)}\n<TRNAMT>{amount}\n"
        f"<FITID>{_fitid(index)}\n<NAME>{payee}\n"
        "</STMTTRN>\n"
    )


def sgml_statement(transactions: str, rows: list[Row]) -> str:
    """An OFX 1 body around ``transactions`` (the text between the list's tags)."""
    return (
        "<OFX>\n"
        "<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
        "<DTSERVER>20260401<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>\n"
        "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS>\n"
        "<CURDEF>USD\n"
        f"<BANKACCTFROM><BANKID>000000000<ACCTID>{ACCOUNT}<ACCTTYPE>CHECKING</BANKACCTFROM>\n"
        f"<BANKTRANLIST><DTSTART>{_day(rows[0][0])}<DTEND>{_day(rows[-1][0])}\n"
        f"{transactions}"
        "</BANKTRANLIST>\n"
        f"<LEDGERBAL><BALAMT>1201.57<DTASOF>{_day(rows[-1][0])}</LEDGERBAL>\n"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n"
        "</OFX>\n"
    )


def sgml_body(rows: list[Row] | None = None) -> str:
    """An OFX 1 body: leaves are ``<TAG>value`` with no end tag."""
    rows = ROWS if rows is None else rows
    transactions = "".join(sgml_transaction(index, row) for index, row in enumerate(rows))
    return sgml_statement(transactions, rows)


def ofx2_without_xml_declaration() -> bytes:
    """OFX 2 that starts with the ``<?OFX ...?>`` instruction, with no ``<?xml ...?>``."""
    return (OFX_INSTRUCTION + xml_body()).encode("ascii")


def ofx2_unknown_encoding(encoding: str) -> bytes:
    """OFX 2 in plain ASCII bytes whose declaration names ``encoding`` all the same.

    ``USASCII`` (the OFX 1 header word, not an XML encoding name) and
    ``utf-16`` (on bytes that are not UTF-16) are the two seen in the wild.
    """
    declaration = XML_DECLARATION.format(encoding=encoding)
    return (declaration + OFX_INSTRUCTION + xml_body()).encode("ascii")


def ofx2_declared_encoding(encoding: str, payee: str, *, codec: str | None = None) -> bytes:
    """OFX 2 whose declaration names ``encoding``, its first payee ``payee``.

    The bytes are written in ``codec``: the declared encoding itself unless
    another is given (a declaration that is wrong about its own file).
    """
    first, *rest = ROWS
    declaration = XML_DECLARATION.format(encoding=encoding)
    text = declaration + OFX_INSTRUCTION + xml_body([(first[0], first[1], payee), *rest])
    return text.encode(codec or encoding)


def xml_declaration_over_sgml_body() -> bytes:
    """``<?xml ...?>`` and ``<?OFX ...?>`` over an SGML body with unclosed leaves."""
    declaration = XML_DECLARATION.format(encoding="UTF-8")
    return (declaration + OFX_INSTRUCTION + sgml_body()).encode("ascii")


def sgml_double_escaped_payee() -> bytes:
    """OFX 1 whose first payee, ``A <B>``, was escaped twice by the exporter."""
    first, *rest = ROWS
    rows = [(first[0], first[1], "A &amp;lt;B&amp;gt;"), *rest]
    return (SGML_HEADER + sgml_body(rows)).encode("ascii")


def sgml_with_valueless_leaves() -> bytes:
    """OFX 1 whose four transactions print ``<TRNTYPE>``, ``<FITID>`` and ``<NAME>`` empty.

    The empty lines have no end tag either, so nothing but the tag's name says
    they are leaves; the text of each row is in its ``<MEMO>``.
    """
    transactions = "".join(
        "<STMTTRN>\n"
        "<TRNTYPE>\n"
        f"<DTPOSTED>{_day(posted)}\n<TRNAMT>{amount}\n"
        "<FITID>\n<NAME>\n"
        f"<MEMO>{memo}\n"
        "</STMTTRN>\n"
        for posted, amount, memo in VALUELESS_ROWS
    )
    return (SGML_HEADER + sgml_statement(transactions, VALUELESS_ROWS)).encode("ascii")


def sgml_with_stray_list_end() -> bytes:
    """OFX 1 with ``</BANKTRANLIST>`` between its two transactions (and again after them)."""
    first, second = (sgml_transaction(index, row) for index, row in enumerate(STRAY_END_ROWS))
    transactions = f"{first}</BANKTRANLIST>\n{second}"
    return (SGML_HEADER + sgml_statement(transactions, STRAY_END_ROWS)).encode("ascii")


def sgml_with_unreadable_amount() -> bytes:
    """OFX 1 whose three transactions are :data:`ROWS`, the middle one with ``<TRNAMT>abc``."""
    amount = ROWS[UNREADABLE_ROW][1]
    body = sgml_body()
    assert body.count(f"<TRNAMT>{amount}\n") == 1
    return (SGML_HEADER + body.replace(f"<TRNAMT>{amount}\n", "<TRNAMT>abc\n")).encode("ascii")
