# SPDX-License-Identifier: AGPL-3.0-or-later
"""Magic-byte sniffing on intake (INGESTION.md "Receive"; SECURITY.md C4, C5)."""

from __future__ import annotations

import io
import zipfile
from collections.abc import Callable

import pytest

from app.ingestion.extract.common import ExtractFailure, decode_text
from app.ingestion.intake.sniff import SNIFF_BYTES, FileType, Rejected, RejectReason, sniff
from tests.fixtures import synth_sniff_cases as cases
from tests.fixtures.statements import csv_bytes, ofx_sgml, ofx_xml, qif_bytes, synth_transactions

TXNS = synth_transactions(4)


def _xlsx() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("xl/workbook.xml", "<workbook/>")
    return buffer.getvalue()


def _zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a.txt", "synthetic")
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n1 0 obj\n", FileType.PDF),
        (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR", FileType.PNG),
        (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00", FileType.JPEG),
        (b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic", FileType.HEIC),
        (b"II*\x00\x08\x00\x00\x00", FileType.TIFF),
        (b"MM\x00*\x00\x00\x00\x08", FileType.TIFF),
        (_xlsx(), FileType.XLSX),
        (_zip(), FileType.ZIP),
        (ofx_sgml(TXNS), FileType.OFX),
        (ofx_sgml(TXNS, qfx=True), FileType.QFX),
        (ofx_xml(TXNS), FileType.OFX),
        (qif_bytes(TXNS), FileType.QIF),
        (csv_bytes(TXNS), FileType.CSV),
        (csv_bytes(TXNS, bom=True), FileType.CSV),
        (csv_bytes(TXNS, delimiter=";"), FileType.CSV),
        (csv_bytes(TXNS, delimiter="\t"), FileType.TSV),
        (csv_bytes(TXNS, encoding="cp1252").replace(b"Memo", b"M\xe9mo"), FileType.CSV),
        (b"Just some synthetic notes.\nSecond line.\n", FileType.TEXT),
        # A short brokerage export: two rows, then disclaimer notes without commas.
        (
            b"\r\nRun Date,Account,Action,Amount\r\n10/14/2026,Synthetic,BUY,-1.00\r\n"
            b'\r\n"Synthetic disclaimer, with a comma."\r\nDate downloaded 11/02/2026\r\n',
            FileType.CSV,
        ),
        # Prose with a comma in some lines is still text.
        (b"Synthetic notes, first.\nSecond line, here.\nThird line.\n", FileType.TEXT),
        (b"No commas here.\nOne, two, three.\nFour, five, six.\n", FileType.TEXT),
        (
            b"Received: from mx.example.invalid\r\nFrom: a@example.invalid\r\n"
            b"Subject: Synthetic\r\nDate: Mon, 5 Jan 2026 10:00:00 +0000\r\n\r\nBody\r\n",
            FileType.EML,
        ),
        (
            b"From: a@example.invalid\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: text/html\r\n\r\n<html><body>receipt</body></html>\r\n",
            FileType.EML,
        ),
    ],
)
def test_accepted_types(data: bytes, expected: FileType) -> None:
    assert sniff(data) == expected


@pytest.mark.parametrize(
    ("data", "reason"),
    [
        (b"", RejectReason.EMPTY),
        (b"   \n\t ", RejectReason.EMPTY),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>1</script></svg>', RejectReason.MARKUP),
        (
            b'<?xml version="1.0"?><svg xmlns="http://www.w3.org/2000/svg"/>',
            RejectReason.XML_NOT_OFX,
        ),
        (b"\xef\xbb\xbf  <!DOCTYPE html><html><body>x</body></html>", RejectReason.MARKUP),
        (b"<HTML><BODY>hi</BODY></HTML>", RejectReason.MARKUP),
        (b"<script>alert(1)</script>", RejectReason.MARKUP),
        (b"<!-- comment --><p>x</p>", RejectReason.MARKUP),
        (b"<iframe src=x>", RejectReason.MARKUP),
        (b"<foo>bar</foo>", RejectReason.MARKUP),
        (b"%PDF-1.4\n<html><script>alert(1)</script></html>", RejectReason.POLYGLOT),
        (b"\x89PNG\r\n\x1a\n<svg onload=x>", RejectReason.POLYGLOT),
        (b"\xff\xd8\xff\xe0<iframe src='//x'>", RejectReason.POLYGLOT),
        (b"Date,Amount\n2026-01-01,<script>x</script>\n", RejectReason.POLYGLOT),
        (b"Date,Link\n2026-01-01,javascript:alert(1)\n", RejectReason.POLYGLOT),
        (b"\x7fELF\x02\x01\x01\x00\x00\x00", RejectReason.BINARY_UNKNOWN),
        (b"MZ\x90\x00\x03\x00\x00\x00", RejectReason.BINARY_UNKNOWN),
        (b"\xff\xfeD\x00a\x00t\x00e\x00", RejectReason.TEXT_ENCODING),
        (b"GIF89a\x01\x00\x01\x00", RejectReason.BINARY_UNKNOWN),
    ],
)
def test_rejected(data: bytes, reason: RejectReason) -> None:
    assert sniff(data) == Rejected(reason)


def test_declared_type_and_extension_play_no_part() -> None:
    # sniff only ever sees bytes; an HTML page called statement.csv is HTML.
    assert sniff(b"<html><body>Date,Amount</body></html>") == Rejected(RejectReason.MARKUP)


def test_only_the_window_is_examined() -> None:
    data = csv_bytes(synth_transactions(2)) + b"x" * SNIFF_BYTES + b"<script>"
    assert sniff(data) == FileType.CSV


def test_structured_types_map_to_extractor_kinds() -> None:
    assert FileType.CSV.extract_kind == "csv"
    assert FileType.TSV.extract_kind == "tsv"
    assert FileType.OFX.extract_kind == "ofx"
    assert FileType.QFX.extract_kind == "qfx"
    assert FileType.QIF.extract_kind == "qif"
    assert FileType.XLSX.extract_kind == "xlsx"
    assert FileType.PDF.extract_kind == "pdf"
    assert FileType.TEXT.extract_kind is None


@pytest.mark.parametrize("split", range(1, 11))
def test_markup_scanner_finds_patterns_split_across_chunks(split: int) -> None:
    from app.ingestion.intake.sniff import MarkupScanner

    data = b"a" * 50 + b"javascript:" + b"b" * 50
    cut = 50 + split
    scanner = MarkupScanner()
    assert not scanner.feed(data[:cut])
    assert scanner.feed(data[cut:])


def test_markup_scanner_ignores_plain_text() -> None:
    from app.ingestion.intake.sniff import MarkupScanner

    scanner = MarkupScanner()
    for _ in range(100):
        assert not scanner.feed(b"2026-01-02,Synthetic Grocer <grocer>,-12.34\n" * 20)


def test_a_table_under_lines_about_the_account_is_csv() -> None:
    """A bank export that opens with the account's name, number and date range."""
    data = (
        b"Account Name : Everyday Checking\n"
        b"Account Number : ****3377\n"
        b"Date Range : 01/01/2026-01/31/2026\n"
        b"\n"
        b"Posting Date,Effective Date,Transaction Type,Description,Check Number,"
        b"Withdrawals,Deposits,Running Balance\n"
        b'1/5/2026,1/5/2026,Debit,"BLUE OWL GROCERS, SPRINGFIELD",,74.19,,738.25\n'
        b"1/10/2026,1/10/2026,Debit,Transfer To Share 0000,,300.00,,438.25\n"
        b'1/15/2026,1/15/2026,Credit,INITECH PAYROLL,,,"1,610.00","2,048.25"\n'
    )
    assert sniff(data) == FileType.CSV
    assert sniff(data.replace(b",", b";")) == FileType.CSV


@pytest.mark.parametrize(
    "data",
    [
        # Prose with commas, then two lines that happen to share a count.
        b"Dear customer, thank you, as always, for banking with us.\n"
        b"Your statement is ready.\nRegards, the team, your branch, and us\n",
        # A preamble as wide as the table is not a preamble.
        b"a,b,c,d\nnote\n1,2,3,4\n5,6,7,8\n9,10,11,12\n",
        # Too few rows under the header to call it a table.
        b"Account : x\nDate,Description,Debit,Credit\n1/5/2026,Shop,1.00,\n",
    ],
)
def test_lines_that_only_look_like_a_table_stay_text(data: bytes) -> None:
    assert sniff(data) == FileType.TEXT


# ---------------------------------------------------------------------------
# Real-world text tables and oddly headed statement files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("build", "expected"),
    [
        (cases.csv_quoted_decimal_commas, FileType.CSV),
        (cases.csv_multiline_quoted_cell, FileType.CSV),
        (cases.csv_preamble_with_continuation_rows, FileType.CSV),
        (cases.csv_summary_block_then_table, FileType.CSV),
        (cases.ofx_header_keys_before_ofxheader, FileType.OFX),
        (cases.ofx_data_key_header, FileType.OFX),
        (cases.ofx_xml_declaration_without_ofx_pi, FileType.OFX),
        (cases.qif_with_leading_comments, FileType.QIF),
        (cases.qfx_with_trailing_nul, FileType.QFX),
    ],
    ids=lambda value: getattr(value, "__name__", None),
)
def test_real_world_text_file_is_sniffed_as_what_it_is(
    build: Callable[[], bytes], expected: FileType
) -> None:
    assert sniff(build()) == expected


def test_window_cut_inside_a_quoted_cell_is_still_csv() -> None:
    """A long export is judged on the records the window holds whole."""
    row = b'2026-04-03;Juniper Row Books;"Order 000-17\ntwo paperbacks\ngift wrapped";-24.80\n'
    data = b"Date;Payee;Memo;Amount\n" + row * 200
    assert len(data) > SNIFF_BYTES
    assert sniff(data) == FileType.CSV


@pytest.mark.parametrize(
    "data",
    [
        b"a,b\x00,c\n1,2,3\n",
        b"\x00Date,Amount\n2026-01-01,1.00\n",
        b"Date,Amount\n2026-01-01,1.00\x00\n",
        cases.qfx_with_trailing_nul() + b"\n<STMTTRN>",
        cases.qfx_with_trailing_nul().replace(b"<OFX>", b"<OFX>\x00", 1),
        # Nothing but padding is not text.
        b"\x00",
        b"  \n\x00\x00",
    ],
)
def test_nul_inside_text_is_still_binary(data: bytes) -> None:
    """Only NUL padding at the very end of the file is ignored."""
    assert sniff(data) == Rejected(RejectReason.BINARY_UNKNOWN)


def test_nul_at_the_end_of_a_full_window_is_not_padding() -> None:
    """The window cannot tell padding from a NUL in the middle of a longer file."""
    line = b"2026-01-01,Synthetic,1.00\n"
    head = (line * (SNIFF_BYTES // len(line) + 1))[: SNIFF_BYTES - 2] + b"\x00\x00"
    assert sniff(head + line) == Rejected(RejectReason.BINARY_UNKNOWN)
    assert sniff(head) == Rejected(RejectReason.BINARY_UNKNOWN)


def test_trailing_nul_padding_is_dropped_on_decode() -> None:
    text, _ = decode_text(b"Date,Amount\n2026-01-01,1.00\n\x00\x00")
    assert text == "Date,Amount\n2026-01-01,1.00\n"
    for data in (b"a,b\x00,c\n", b"a,b\n\x00\n", b"\x00a,b\n"):
        with pytest.raises(ExtractFailure) as caught:
            decode_text(data)
        assert caught.value.code == "binary_content"


PROSE = (
    b"Dear member, thank you for staying with us this year and for your patience.\n"
    b"We have moved the branch to a new address.\n"
    b"\n"
    b'Our teller said, "the new counters open on Monday, the old ones close on Friday," and\n'
    b"we hope the change is an easy one.\n"
    b"Statements are posted on the first working day.\n"
    b"\n"
    b"Fees, rates, limits and opening hours are unchanged, as are your account numbers.\n"
    b"Kind regards\n"
    b"The branch team\n"
)


@pytest.mark.parametrize(
    "data",
    [
        PROSE,
        PROSE.replace(b"\n", b"\r\n"),
        # A quotation left open runs to the end of the letter: no table.
        PROSE.replace(b'Friday,"', b"Friday,"),
        # ... and one that only closes pages later is no cell either.
        PROSE.replace(b'Friday,"', b"Friday,") * 3,
        # A quotation that opens a line and closes two lines on.
        b"Synthetic notes, first.\n"
        b'"A quoted remark, begun here\n'
        b"and carried on, over a line\n"
        b'and closed here."\n'
        b"Third line.\n",
        # A list with a comma here and there.
        b"Things to bring:\n- passport, or an identity card\n- proof of address\n- the form\n",
    ],
)
def test_prose_with_commas_is_still_plain_text(data: bytes) -> None:
    assert sniff(data) == FileType.TEXT


@pytest.mark.parametrize(
    "data",
    [
        b'<?xml version="1.0" encoding="UTF-8"?><Document><Stmt/></Document>',
        b'<?xml version="1.0"?>\n<Document xmlns="urn:example:invalid"><OFX></OFX></Document>',
        # The root must follow the declaration: a DOCTYPE or a comment in between is refused.
        b'<?xml version="1.0"?>\n<!DOCTYPE OFX [<!ENTITY a "b">]>\n<OFX></OFX>',
        b'<?xml version="1.0"?>\n<OFXS><OFX></OFX></OFXS>',
    ],
)
def test_xml_that_is_not_ofx_is_still_refused(data: bytes) -> None:
    assert sniff(data) == Rejected(RejectReason.XML_NOT_OFX)


@pytest.mark.parametrize(
    ("data", "verdict"),
    [
        # Lines before <OFX> that are not KEY:VALUE headers.
        (b"Synthetic notes about a file\n<OFX>\n</OFX>\n", FileType.TEXT),
        (b"DATA:OFXSGML\nsome words, and more\n<OFX>\n</OFX>\n", FileType.TEXT),
        (b"DATA:<b>\n<OFX>\n</OFX>\n", FileType.TEXT),
        # A comment line does not make any text a QIF.
        (b"# Synthetic notes\nSecond line.\n", FileType.TEXT),
        (b"# a,b,c\n1,2,3\n4,5,6\n", FileType.CSV),
    ],
)
def test_headers_that_only_look_like_ofx_or_qif(data: bytes, verdict: FileType) -> None:
    assert sniff(data) == verdict


@pytest.mark.parametrize(
    "build",
    [
        cases.csv_quoted_decimal_commas,
        cases.csv_multiline_quoted_cell,
        cases.csv_preamble_with_continuation_rows,
        cases.csv_summary_block_then_table,
        cases.ofx_header_keys_before_ofxheader,
        cases.ofx_data_key_header,
        cases.ofx_xml_declaration_without_ofx_pi,
        cases.qif_with_leading_comments,
        cases.qfx_with_trailing_nul,
    ],
    ids=lambda value: getattr(value, "__name__", None),
)
def test_markup_in_accepted_file_is_still_refused(build: Callable[[], bytes]) -> None:
    """Every newly accepted shape is a polyglot as soon as it carries active markup."""
    data = build()
    for old in (b"Larkspur", b"Juniper", b"MOSSY", b"HOLLOW", b"Mossy"):
        if old in data:
            assert sniff(data.replace(old, b"<script>x</script>", 1)) == Rejected(
                RejectReason.POLYGLOT
            )
            assert sniff(data.replace(old, b"javascript:x", 1)) == Rejected(RejectReason.POLYGLOT)
            break
    else:  # pragma: no cover - every builder names one of the words above
        raise AssertionError("no cell to replace")


def test_markup_in_accepted_csv_is_still_refused() -> None:
    data = cases.csv_quoted_decimal_commas().replace(
        b"Marrow Lane Books 22", b"<script>alert(1)</script>"
    )
    assert b"<script>" in data
    assert sniff(data) == Rejected(RejectReason.POLYGLOT)
    # Past the window the whole-file scan of the upload intake finds it.
    from app.ingestion.intake.sniff import MarkupScanner

    scanner = MarkupScanner()
    assert scanner.feed(data)


@pytest.mark.parametrize(
    ("counts", "table"),
    [
        # Header and first row together, then a shorter row after each row.
        ([1, 1, 1, 0, 4, 4, 2, 4, 2, 4, 2], True),
        ([4, 4, 0, 4, 0, 4], True),
        # A shorter line right under the header: not a continuation.
        ([4, 0, 4, 0, 4, 0, 4], False),
        # Two shorter lines running: the table is over.
        ([1, 4, 4, 2, 2, 4, 2, 4], False),
        # Fewer than three rows of the table's shape.
        ([1, 4, 4, 2], False),
        # A preamble as wide as the table is still not a preamble.
        ([2, 3, 3, 1, 3, 1, 3], False),
    ],
)
def test_rows_after_preamble_allows_one_shorter_row_between_rows(
    counts: list[int], table: bool
) -> None:
    from app.ingestion.intake.sniff import _rows_after_preamble

    assert _rows_after_preamble(counts) is table
