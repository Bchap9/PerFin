# SPDX-License-Identifier: AGPL-3.0-or-later
"""Magic-byte sniffing on intake (INGESTION.md "Receive"; SECURITY.md C4, C5)."""

from __future__ import annotations

import io
import zipfile

import pytest

from app.ingestion.intake.sniff import SNIFF_BYTES, FileType, Rejected, RejectReason, sniff
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
