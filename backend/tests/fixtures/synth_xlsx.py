# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic XLSX workbooks (openpyxl, or hand-built parts for hostile variants).

:func:`workbook` writes rows with openpyxl; :func:`raw_xlsx` assembles the
minimal set of OOXML parts around a given worksheet XML, so tests can put
in what openpyxl would never write: cached formula values, DTDs, entity
bombs, absurd dimensions.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from decimal import Decimal

_PKG = "http://schemas.openxmlformats.org/package/2006"
_DOC = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_SML = "application/vnd.openxmlformats-officedocument.spreadsheetml"
CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<Types xmlns="{_PKG}/content-types">'
    '<Default Extension="rels" '
    'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    f'<Override PartName="/xl/workbook.xml" ContentType="{_SML}.sheet.main+xml"/>'
    f'<Override PartName="/xl/worksheets/sheet1.xml" ContentType="{_SML}.worksheet+xml"/>'
    "</Types>"
).encode()
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<Relationships xmlns="{_PKG}/relationships">'
    f'<Relationship Id="rId1" Type="{_DOC}/officeDocument" Target="xl/workbook.xml"/>'
    "</Relationships>"
).encode()
WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
    f'xmlns:r="{_DOC}">'
    '<sheets><sheet name="Sheet1" sheetId="1" r:id="rId1"/></sheets></workbook>'
).encode()
WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
    f'<Relationships xmlns="{_PKG}/relationships">'
    f'<Relationship Id="rId1" Type="{_DOC}/worksheet" Target="worksheets/sheet1.xml"/>'
    "</Relationships>"
).encode()
SHEET_NS = b'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'

Cell = str | int | float | Decimal | date | bool | None


def workbook(rows: Iterable[Sequence[Cell]], *, sheets: int = 1) -> bytes:
    """A workbook written by openpyxl with ``rows`` on each of ``sheets`` sheets."""
    from openpyxl import Workbook

    book = Workbook()
    data = [list(r) for r in rows]
    for index in range(sheets):
        sheet = book.active if index == 0 else book.create_sheet(f"Sheet{index + 1}")
        for row in data:
            sheet.append(row)
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def sheet_xml(body: bytes, *, prolog: bytes = b"", dimension: str | None = None) -> bytes:
    dim = f'<dimension ref="{dimension}"/>'.encode() if dimension else b""
    return (
        b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        + prolog
        + b"<worksheet "
        + SHEET_NS
        + b">"
        + dim
        + b"<sheetData>"
        + body
        + b"</sheetData></worksheet>"
    )


def raw_xlsx(
    sheet: bytes,
    *,
    extra: Mapping[str, bytes] | None = None,
    compression: int = zipfile.ZIP_DEFLATED,
) -> bytes:
    """A workbook of the minimal parts around ``sheet`` (worksheet XML)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("xl/workbook.xml", WORKBOOK)
        archive.writestr("xl/_rels/workbook.xml.rels", WORKBOOK_RELS)
        archive.writestr("xl/worksheets/sheet1.xml", sheet)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def inline_row(index: int, *cells: str) -> bytes:
    """A ``<row>`` of inline-string cells (columns A, B, ...)."""
    out = [f'<row r="{index}">'.encode()]
    for col, text in enumerate(cells):
        ref = f"{chr(ord('A') + col)}{index}"
        out.append(f'<c r="{ref}" t="inlineStr"><is><t>{text}</t></is></c>'.encode())
    out.append(b"</row>")
    return b"".join(out)
