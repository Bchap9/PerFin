# SPDX-License-Identifier: AGPL-3.0-or-later
"""XLSX extraction: worksheets as text tables (INGESTION.md "Extract"; SECURITY.md C2, C6, C7).

Runs only inside the sandbox child, on the workbook's bytes in memory.

* **Container checks first** (C2): the ZIP's central directory is read with
  :mod:`zipfile` before openpyxl sees anything: at most
  :data:`MAX_MEMBERS` entries, the declared uncompressed total at most
  ``options.max_children_total_bytes``, no member above 1 MiB expanding more
  than ``options.max_zip_ratio`` times, no encrypted members (``zip_bomb``,
  ``xlsx_too_large``, ``too_many_members``, ``encrypted``).
* **No DTDs or entities** (C6): every XML part is checked for ``<!DOCTYPE``
  and ``<!ENTITY`` before parsing (``xml_dtd``), and openpyxl must be using
  defusedxml (it does when installed and ``OPENPYXL_DEFUSEDXML`` is not
  ``False``; the child's environment is empty), else the extractor refuses
  to run (``unsafe_parser``).
* **Values only, formulas never evaluated**: ``load_workbook(read_only=True,
  data_only=True, keep_links=False, keep_vba=False)``: a formula cell gives
  the value the authoring program cached, or nothing; external links, macros
  and pivot caches are not loaded.
* **Caps** (C2): at most :data:`MAX_SHEETS` worksheets, ``max_columns``
  columns (wider sheets are cut, warned), ``max_rows`` non-empty rows per
  sheet (``truncated``), ``max_cell_chars`` per cell; at most four times
  ``max_rows`` row positions are walked, so a sheet that declares a million
  empty rows costs nothing.
* **Formula neutralisation** (C7): every text cell goes through
  :func:`app.core.csv_safety.neutralise_formula`, also the cached value of
  a formula (``=HYPERLINK(...)`` stored as text stays text).

Cells become text: numbers in plain decimal notation, dates ISO 8601
(``2026-03-01``, or with the time when it is not midnight), booleans
``TRUE``/``FALSE``. Each non-empty worksheet is one :class:`Table`
(``delimiter`` is ``\\t``: the cells were never delimited, the field keeps the
model shared with CSV/TSV).
"""

from __future__ import annotations

import io
import re
import struct
import zipfile
import zlib
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Final

# Imported with the module, i.e. before the child hardens itself: openpyxl's
# import reads /etc/mime.types (``mimetypes``), which Landlock later denies.
import openpyxl

from app.core.csv_safety import neutralise_formula
from app.ingestion.extract.common import ExtractFailure
from app.ingestion.extract.delimited import _has_header
from app.ingestion.extract.registry import register
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Table

VERSION: Final = "xlsx/1"
MAX_SHEETS: Final = 50
MAX_MEMBERS: Final = 10_000
RATIO_FLOOR: Final = 1024 * 1024
_DTD: Final = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_XML_SUFFIXES: Final = (".xml", ".rels", ".vml")
_CHUNK: Final = 64 * 1024
_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


#: What :mod:`zipfile` raises for damaged archives (offsets, headers, streams).
_ZIP_ERRORS: Final = (
    zipfile.BadZipFile,
    zlib.error,
    NotImplementedError,
    OSError,
    EOFError,
    RuntimeError,
    ValueError,
    struct.error,
)


def _check_container(data: bytes, options: ExtractOptions) -> None:
    try:
        _check_members(data, options)
    except ExtractFailure:
        raise
    except _ZIP_ERRORS:
        raise ExtractFailure("malformed_xlsx", "not a readable ZIP container") from None


def _check_members(data: bytes, options: ExtractOptions) -> None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_MEMBERS:
            raise ExtractFailure("too_many_members", f"more than {MAX_MEMBERS} parts")
        total = 0
        for info in infos:
            if info.flag_bits & 0x1:
                raise ExtractFailure("encrypted", "encrypted workbook")
            total += info.file_size
            if total > options.max_children_total_bytes:
                raise ExtractFailure("xlsx_too_large", "workbook expands beyond the cap")
            if (
                info.file_size > RATIO_FLOOR
                and info.file_size > max(info.compress_size, 1) * options.max_zip_ratio
            ):
                raise ExtractFailure("zip_bomb", "a part expands beyond the ratio cap")
        for info in infos:
            if not info.filename.lower().endswith(_XML_SUFFIXES) or info.is_dir():
                continue
            if _has_dtd(archive, info):
                raise ExtractFailure("xml_dtd", "DTDs and entities are not allowed")


def _has_dtd(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bool:
    tail = b""
    read = 0
    with archive.open(info) as handle:
        while chunk := handle.read(_CHUNK):
            read += len(chunk)
            if read > info.file_size:
                raise ExtractFailure("malformed_xlsx", "a part is longer than declared")
            if _DTD.search(tail + chunk):
                return True
            tail = chunk[-16:]
    return False


def _number(value: float | int | Decimal) -> str:
    if isinstance(value, int):
        return str(value)
    try:
        number = Decimal(repr(value)) if isinstance(value, float) else value
    except InvalidOperation:  # pragma: no cover - repr of a float is always a number
        return ""
    if not number.is_finite():
        return ""
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def cell_text(value: Any, limit: int) -> tuple[str, bool]:
    """``(text, cut)`` for one cell value (module doc)."""
    if value is None:
        return "", False
    if isinstance(value, bool):
        text = "TRUE" if value else "FALSE"
    elif isinstance(value, int | float | Decimal):
        text = _number(value)
    elif isinstance(value, datetime):
        text = (
            value.date().isoformat()
            if value.time() == time(0)
            else value.replace(tzinfo=None).isoformat(sep=" ")
        )
    elif isinstance(value, date | time):
        text = value.isoformat()
    else:
        text = _CONTROL.sub(" ", str(value))
    cut = len(text) > limit
    if cut:
        text = text[:limit]
    return neutralise_formula(text), cut


def _sheet_rows(sheet: Any, options: ExtractOptions) -> tuple[list[list[str]], bool, int, bool]:
    rows: list[list[str]] = []
    truncated = False
    wide = False
    cut_cells = 0
    walked = 0
    for raw in sheet.iter_rows(values_only=True, max_col=options.max_columns + 1):
        walked += 1
        if walked > options.max_rows * 4:
            truncated = True
            break
        values = list(raw)
        if len(values) > options.max_columns:
            if any(v is not None for v in values[options.max_columns :]):
                wide = True
            values = values[: options.max_columns]
        cells: list[str] = []
        for value in values:
            text, cut = cell_text(value, options.max_cell_chars)
            cut_cells += cut
            cells.append(text)
        while cells and not cells[-1].strip():
            cells.pop()
        if not cells:
            continue
        if len(rows) >= options.max_rows:
            truncated = True
            break
        rows.append(cells)
    return rows, truncated, cut_cells, wide


def extract_xlsx(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    _check_container(data, options)
    if not openpyxl.DEFUSEDXML:
        raise ExtractFailure("unsafe_parser", "openpyxl is not using defusedxml")
    try:
        workbook = openpyxl.load_workbook(
            io.BytesIO(data), read_only=True, data_only=True, keep_links=False, keep_vba=False
        )
    except ExtractFailure:
        raise
    except Exception:  # noqa: BLE001 - openpyxl raises many types for malformed input
        raise ExtractFailure("malformed_xlsx", "the workbook cannot be read") from None
    tables: list[Table] = []
    warnings: list[str] = []
    cut_total = 0
    try:
        sheets = workbook.worksheets
        if len(sheets) > MAX_SHEETS:
            warnings.append(f"sheets_truncated:{MAX_SHEETS}")
            sheets = sheets[:MAX_SHEETS]
        for sheet in sheets:
            if not hasattr(sheet, "iter_rows"):
                continue  # a chartsheet
            try:
                rows, truncated, cut, wide = _sheet_rows(sheet, options)
            except ExtractFailure:
                raise
            except Exception:  # noqa: BLE001 - malformed sheet XML
                raise ExtractFailure("malformed_xlsx", "a worksheet cannot be read") from None
            cut_total += cut
            if wide:
                warnings.append(f"columns_truncated:{options.max_columns}")
            if truncated:
                warnings.append(f"rows_truncated:{options.max_rows}")
            if not rows:
                continue
            width = max(len(r) for r in rows)
            rows = [r + [""] * (width - len(r)) for r in rows]
            tables.append(
                Table(
                    encoding="utf-8",
                    delimiter="\t",
                    has_header=_has_header(rows),
                    rows=rows,
                    column_count=width,
                    truncated=truncated,
                )
            )
    finally:
        workbook.close()
    if not tables:
        raise ExtractFailure("empty_table", "no rows")
    if cut_total:
        warnings.append(f"cells_truncated:{cut_total}")
    return ExtractResult(
        kind=kind,
        ok=True,
        extractor_version=VERSION,
        tables=tables,
        truncated=any(t.truncated for t in tables),
        warnings=warnings[:100],
    )


register("xlsx", extract_xlsx)
