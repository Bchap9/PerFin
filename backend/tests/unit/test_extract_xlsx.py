# SPDX-License-Identifier: AGPL-3.0-or-later
"""XLSX extractor (``app.ingestion.extract.xlsx``; SECURITY.md C2, C6, C7)."""

from __future__ import annotations

import io
import time
import zipfile
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.ingestion.extract.common import ExtractFailure
from app.ingestion.extract.xlsx import cell_text, extract_xlsx
from app.ingestion.intake.sniff import FileType, sniff
from app.ingestion.sandbox.protocol import ExtractOptions
from app.ingestion.sandbox.runner import SandboxConfig, run_extraction
from tests.fixtures.synth_xlsx import Cell, inline_row, raw_xlsx, sheet_xml, workbook

OPTS = ExtractOptions()

ROWS: list[list[Cell]] = [
    ["Date", "Description", "Amount"],
    [date(2026, 3, 1), "Corner Grocer", Decimal("-12.34")],
    [date(2026, 3, 2), "Payroll Inc", 2500],
    [date(2026, 3, 3), '=HYPERLINK("http://example.invalid")', -5.5],
]


def test_workbook_rows_become_a_table_of_text() -> None:
    data = workbook(ROWS)
    assert sniff(data) == FileType.XLSX
    assert FileType.XLSX.extract_kind == "xlsx"
    result = extract_xlsx(data, OPTS, "xlsx")
    assert result.ok
    (table,) = result.tables
    assert table.has_header
    assert table.column_count == 3
    assert table.rows[0] == ["Date", "Description", "Amount"]
    assert table.rows[1] == ["2026-03-01", "Corner Grocer", "-12.34"]
    assert table.rows[2] == ["2026-03-02", "Payroll Inc", "2500"]
    # A formula-looking string is neutralised (C7); openpyxl stored it as a
    # formula with no cached value, so data_only gives nothing at all.
    assert table.rows[3][0] == "2026-03-03"
    assert table.rows[3][1] in ("", '\'=HYPERLINK("http://example.invalid")')
    assert table.rows[3][2] == "-5.5"


def test_formulas_are_never_evaluated_only_cached_values_kept() -> None:
    body = (
        inline_row(1, "Label", "Value")
        + b'<row r="2"><c r="A2" t="inlineStr"><is><t>cached</t></is></c>'
        b'<c r="B2"><f>1+1</f><v>7</v></c></row>'
        b'<row r="3"><c r="A3" t="inlineStr"><is><t>uncached</t></is></c>'
        b'<c r="B3"><f>SUM(1,2)</f></c></row>'
        b'<row r="4"><c r="A4" t="str"><f>A1</f><v>=cmd|\' /C calc\'!A0</v></c></row>'
    )
    result = extract_xlsx(raw_xlsx(sheet_xml(body)), OPTS, "xlsx")
    rows = result.tables[0].rows
    assert rows[1] == ["cached", "7"]  # the cached value, not 2
    assert rows[2] == ["uncached", ""]
    assert rows[3][0] == "'=cmd|' /C calc'!A0"  # cached text neutralised (C7)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ""),
        (True, "TRUE"),
        (0.1, "0.1"),
        (1e-7, "0.0000001"),
        (1e20, "100000000000000000000"),
        (float("nan"), ""),
        (float("inf"), ""),
        (Decimal("12.500"), "12.5"),
        (datetime(2026, 3, 1, 0, 0), "2026-03-01"),
        (datetime(2026, 3, 1, 13, 5), "2026-03-01 13:05:00"),
        ("@SUM(A1)", "'@SUM(A1)"),
        ("-12.34", "-12.34"),
        ("a\x00b\x07c", "a b c"),
    ],
)
def test_cell_text(value: object, expected: str) -> None:
    assert cell_text(value, 100)[0] == expected


def test_caps_rows_columns_cells() -> None:
    options = ExtractOptions(max_rows=5, max_columns=2, max_cell_chars=16)
    rows = [["a" * 40, "b", "c"] for _ in range(10)]
    result = extract_xlsx(workbook(rows), options, "xlsx")
    (table,) = result.tables
    assert len(table.rows) == 5
    assert table.truncated
    assert result.truncated
    assert table.column_count == 2
    assert all(len(cell) <= 16 for row in table.rows for cell in row)
    assert {"rows_truncated:5", "columns_truncated:2"} <= set(result.warnings)
    assert any(w.startswith("cells_truncated:") for w in result.warnings)


def test_a_sheet_declaring_a_million_rows_costs_nothing() -> None:
    body = inline_row(1, "Date", "Amount") + inline_row(1048576, "2026-01-01", "1")
    data = raw_xlsx(sheet_xml(body, dimension="A1:XFD1048576"))
    options = ExtractOptions(max_rows=100)
    started = time.monotonic()
    result = extract_xlsx(data, options, "xlsx")
    assert time.monotonic() - started < 5
    assert result.tables[0].rows == [["Date", "Amount"]]
    assert result.truncated


@pytest.mark.parametrize(
    "prolog",
    [
        pytest.param(b'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>', id="xxe"),
        pytest.param(
            b'<!DOCTYPE x [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
            b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;"><!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;">]>',
            id="billion-laughs",
        ),
    ],
)
def test_dtds_and_entities_are_refused(prolog: bytes) -> None:
    data = raw_xlsx(sheet_xml(inline_row(1, "a", "b"), prolog=prolog))
    with pytest.raises(ExtractFailure) as caught:
        extract_xlsx(data, OPTS, "xlsx")
    assert caught.value.code == "xml_dtd"


def test_dtd_in_shared_strings_is_refused() -> None:
    strings = b'<?xml version="1.0"?><!DOCTYPE sst [<!ENTITY e "x">]><sst/>'
    data = raw_xlsx(sheet_xml(inline_row(1, "a")), extra={"xl/sharedStrings.xml": strings})
    with pytest.raises(ExtractFailure) as caught:
        extract_xlsx(data, OPTS, "xlsx")
    assert caught.value.code == "xml_dtd"


def test_openpyxl_uses_defusedxml() -> None:
    import openpyxl

    assert openpyxl.DEFUSEDXML is True


def test_unsafe_parser_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    import openpyxl

    monkeypatch.setattr(openpyxl, "DEFUSEDXML", False)
    with pytest.raises(ExtractFailure) as caught:
        extract_xlsx(workbook(ROWS), OPTS, "xlsx")
    assert caught.value.code == "unsafe_parser"


def test_decompression_bomb_is_refused_before_parsing() -> None:
    bomb = sheet_xml(b'<row r="1"></row>' + b" " * (30 * 1024 * 1024))
    data = raw_xlsx(bomb)
    assert len(data) < 1024 * 1024
    with pytest.raises(ExtractFailure) as caught:
        extract_xlsx(data, OPTS, "xlsx")
    assert caught.value.code == "zip_bomb"
    with pytest.raises(ExtractFailure) as total:
        extract_xlsx(data, ExtractOptions(max_children_total_bytes=1024 * 1024), "xlsx")
    assert total.value.code == "xlsx_too_large"


@pytest.mark.parametrize(
    "data",
    [
        pytest.param(b"PK\x03\x04" + b"\x00" * 64, id="truncated-zip"),
        pytest.param(raw_xlsx(b"<worksheet><sheetData><row"), id="broken-sheet-xml"),
        pytest.param(
            raw_xlsx(sheet_xml(b""), extra={}).replace(b"xl/workbook.xml", b"xl/workbook.xmX"),
            id="missing-workbook",
        ),
    ],
)
def test_malformed_workbooks_fail_cleanly(data: bytes) -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_xlsx(data, OPTS, "xlsx")
    assert caught.value.code in {"malformed_xlsx", "empty_table"}


def test_empty_workbook_is_an_error() -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_xlsx(workbook([]), OPTS, "xlsx")
    assert caught.value.code == "empty_table"


def test_every_sheet_is_a_table() -> None:
    result = extract_xlsx(workbook(ROWS, sheets=3), OPTS, "xlsx")
    assert len(result.tables) == 3


def test_encrypted_parts_are_refused() -> None:
    data = bytearray(workbook(ROWS))
    # Set the "encrypted" flag on every central directory entry.
    archive = zipfile.ZipFile(io.BytesIO(bytes(data)))
    offset = data.find(b"PK\x01\x02")
    while offset != -1:
        data[offset + 8] |= 0x1
        offset = data.find(b"PK\x01\x02", offset + 4)
    archive.close()
    with pytest.raises(ExtractFailure) as caught:
        extract_xlsx(bytes(data), OPTS, "xlsx")
    assert caught.value.code == "encrypted"


def test_xlsx_through_the_sandbox() -> None:
    result = run_extraction("xlsx", workbook(ROWS), config=SandboxConfig())
    assert result.ok
    assert result.tables[0].rows[1] == ["2026-03-01", "Corner Grocer", "-12.34"]
    xxe = raw_xlsx(
        sheet_xml(
            inline_row(1, "&xxe;"),
            prolog=b'<!DOCTYPE x [<!ENTITY xxe SYSTEM "file:///etc/hostname">]>',
        )
    )
    refused = run_extraction("xlsx", xxe, config=SandboxConfig())
    assert not refused.ok
    assert refused.error is not None
    assert refused.error.code == "xml_dtd"
