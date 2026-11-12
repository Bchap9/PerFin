# SPDX-License-Identifier: AGPL-3.0-or-later
"""Text files the sniffer has to call what they are (INGESTION.md "Receive").

Each builder returns the bytes of one export shaped like files households
really receive, in a layout the plain delimiter count gets wrong. Every
name, number and figure is invented.

Tables:

* :func:`csv_quoted_decimal_commas`: a six-column header, rows whose amounts
  use a decimal comma (``"-8,78"``) and whose descriptions hold commas inside
  quotes, so the commas per physical line run from five to eight.
* :func:`csv_multiline_quoted_cell`: a semicolon file with a quoted cell
  that runs over three physical lines.
* :func:`csv_preamble_with_continuation_rows`: four lines about the account,
  then a table in which every transaction row is followed by a row that
  only carries the rest of the description.
* :func:`csv_summary_block_then_table`: five ``label,value`` summary lines
  (the values quoted, with thousands separators), a blank line, then the
  transaction table.

Statement formats:

* :func:`ofx_header_keys_before_ofxheader`: an OFX 1.x header whose first key
  is ``ENCODING``, not ``OFXHEADER``.
* :func:`ofx_data_key_header`: an OFX 1.x header of ``DATA`` and ``ENCODING``
  only.
* :func:`ofx_xml_declaration_without_ofx_pi`: an XML declaration followed
  directly by the ``<OFX>`` root, with no ``<?OFX ...?>`` instruction.
* :func:`qif_with_leading_comments`: ``#`` comment lines before ``!Type:``.
* :func:`qfx_with_trailing_nul`: a QFX whose last byte is a NUL.
"""

from __future__ import annotations

_CRLF = "\r\n"


def csv_quoted_decimal_commas() -> bytes:
    """Six columns; decimal commas and commas inside quoted descriptions."""
    lines = [
        '"Date","Amount","Account","Counterparty","Name","Description"',
        '"2026-03-02","-8,78","XX00SYNT0000000001","","Larkspur Hosting",'
        '"Larkspur Hosting 0100, Dublin, IE 9.95 USD"',
        '"2026-03-03","-41,20","XX00SYNT0000000001","","Pebble Lane Grocer",'
        '"Pebble Lane Grocer, Utrecht, card 0000"',
        '"2026-03-05","1.250,00","XX00SYNT0000000001","XX00SYNT0000000002","Quillfeather BV",'
        '"Salary March"',
        '"2026-03-06","-12,00","XX00SYNT0000000001","","Tern Street Cafe",'
        '"Tern Street Cafe, Utrecht, card 0000"',
        '"2026-03-09","-5,49","XX00SYNT0000000001","","Marrow Lane Books","Marrow Lane Books 22"',
        '"2026-03-10","-63,10","XX00SYNT0000000001","","Ashgrove Energy",'
        '"Ashgrove Energy, invoice 000123, March"',
        '"2026-03-12","-7,25","XX00SYNT0000000001","","Fennel Yard","Fennel Yard Market"',
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def csv_multiline_quoted_cell() -> bytes:
    """A semicolon file; the first row's memo is one quoted cell over three lines."""
    text = (
        "Date;Payee;Memo;Amount\n"
        '2026-04-03;Juniper Row Books;"Order 000-17\n'
        "two paperbacks\n"
        'gift wrapped";-24.80\n'
        '2026-04-05;Tern Street Cafe;"Lunch";-9.30\n'
    )
    return text.encode("utf-8")


def csv_preamble_with_continuation_rows() -> bytes:
    """Four lines about the account; each row is followed by the rest of its description."""
    lines = [
        "Account details for:,Everyday Savings 000-0-000000",
        "Available balance,1204.18",
        "Ledger balance,1204.18",
        "Transaction history",
        "",
        "Transaction date,Value date,Description,Withdrawals,Deposits",
        "02/03/2026,02/03/2026,DEBIT PURCHASE,18.40,",
        ",,01/03/26 xx-0000 MOSSY OAK DELI",
        "03/03/2026,03/03/2026,DEBIT PURCHASE,6.75,",
        ",,02/03/26 xx-0000 TERN STREET CAFE",
        "05/03/2026,05/03/2026,FUND TRANSFER,,250.00",
        ",,from 000-0-000001 QUILLFEATHER",
        "06/03/2026,06/03/2026,DEBIT PURCHASE,32.10,",
        ",,05/03/26 xx-0000 PEBBLE LANE GROCER",
    ]
    return (_CRLF.join(lines) + _CRLF).encode("utf-8")


def csv_summary_block_then_table() -> bytes:
    """A five-line summary of label,value pairs, a blank line, then the table."""
    lines = [
        "Description,Summary Amt.",
        'Beginning balance as of 01/01/2026,"1,204.18"',
        'Total credits,"2,310.00"',
        'Total debits,"-1,877.42"',
        'Ending balance as of 01/31/2026,"1,636.76"',
        "",
        "Date,Description,Amount,Running Bal.",
        '01/01/2026,Beginning balance as of 01/01/2026,,"1,204.18"',
        '01/03/2026,"HOLLOW PINE MARKET, PURCHASE 0103",-54.12,"1,150.06"',
        '01/15/2026,"QUILLFEATHER PAYROLL, ID 000017","2,310.00","3,460.06"',
        '01/20/2026,"ASHGROVE ENERGY, BILL PAYMENT","-1,823.30","1,636.76"',
    ]
    return (_CRLF.join(lines) + _CRLF).encode("utf-8")


def _sgml_body(*, qfx: bool = False) -> str:
    intu = "<INTU.BID>00000\n" if qfx else ""
    return (
        "<OFX>\n<SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>\n"
        f"<DTSERVER>20260301<LANGUAGE>ENG\n{intu}</SONRS></SIGNONMSGSRSV1>\n"
        "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STMTRS>\n"
        "<CURDEF>USD\n<BANKACCTFROM><BANKID>000000000<ACCTID>000000004410"
        "<ACCTTYPE>CHECKING</BANKACCTFROM>\n"
        "<BANKTRANLIST><DTSTART>20260201<DTEND>20260228\n"
        "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260203<TRNAMT>-18.40<FITID>SYN0001"
        "<NAME>MOSSY OAK DELI</STMTTRN>\n"
        "<STMTTRN><TRNTYPE>CREDIT<DTPOSTED>20260214<TRNAMT>250.00<FITID>SYN0002"
        "<NAME>QUILLFEATHER PAYROLL</STMTTRN>\n"
        "</BANKTRANLIST>\n"
        "<LEDGERBAL><BALAMT>1204.18<DTASOF>20260228</LEDGERBAL>\n"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n</OFX>\n"
    )


def ofx_header_keys_before_ofxheader() -> bytes:
    """An OFX 1.x file whose header opens with ``ENCODING``, then ``OFXHEADER``."""
    header = (
        "ENCODING:UTF-8\nOFXHEADER:100\nDATA:OFXSGML\nVERSION:211\nSECURITY:NONE\n"
        "CHARSET:NONE\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
    )
    return (header + _sgml_body()).encode("utf-8")


def ofx_data_key_header() -> bytes:
    """An OFX 1.x file whose whole header is ``DATA:OFXSGML`` and ``ENCODING:UTF-8``."""
    return ("DATA:OFXSGML\nENCODING:UTF-8\n" + _sgml_body()).encode("utf-8")


def ofx_xml_declaration_without_ofx_pi() -> bytes:
    """An XML declaration, then the ``<OFX>`` root: no ``<?OFX ...?>`` instruction."""
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<OFX>\n<BANKMSGSRSV1><STMTTRNRS><TRNUID>1</TRNUID><STMTRS>\n"
        "<CURDEF>EUR</CURDEF>\n"
        "<BANKACCTFROM><BANKID>000000000</BANKID><ACCTID>000000004410</ACCTID>"
        "<ACCTTYPE>CHECKING</ACCTTYPE></BANKACCTFROM>\n"
        "<BANKTRANLIST><DTSTART>20260201</DTSTART><DTEND>20260228</DTEND>\n"
        "<STMTTRN><TRNTYPE>DEBIT</TRNTYPE><DTPOSTED>20260203</DTPOSTED>"
        "<TRNAMT>-18.40</TRNAMT><FITID>SYN0001</FITID><NAME>MOSSY OAK DELI</NAME></STMTTRN>\n"
        "<STMTTRN><TRNTYPE>CREDIT</TRNTYPE><DTPOSTED>20260214</DTPOSTED>"
        "<TRNAMT>250.00</TRNAMT><FITID>SYN0002</FITID><NAME>QUILLFEATHER PAYROLL</NAME>"
        "</STMTTRN>\n"
        "</BANKTRANLIST>\n"
        "<LEDGERBAL><BALAMT>1204.18</BALAMT><DTASOF>20260228</DTASOF></LEDGERBAL>\n"
        "</STMTRS></STMTTRNRS></BANKMSGSRSV1>\n</OFX>\n"
    )
    return text.encode("utf-8")


def qif_with_leading_comments() -> bytes:
    """A QIF whose first lines are ``#`` comments, before the ``!Type:`` header."""
    lines = [
        "### Synthetic export",
        "# written for the sniffer tests",
        "!Type:Bank",
        "D02/03/2026",
        "T-18.40",
        "PMossy Oak Deli",
        "^",
        "D02/14/2026",
        "T250.00",
        "PQuillfeather Payroll",
        "^",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def qfx_with_trailing_nul() -> bytes:
    """A QFX (``<INTU.BID>``) whose last byte, after ``</OFX>``, is a NUL."""
    header = (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\n"
        "CHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
    )
    return (header + _sgml_body(qfx=True)).rstrip("\n").encode("ascii") + b"\x00"
