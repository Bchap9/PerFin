# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSV and TSV extraction (INGESTION.md "Extract"; SECURITY.md C2, C7).

* **Encoding**: UTF-8 (with or without BOM), else Windows-1252, else Latin-1
  (:func:`app.ingestion.extract.common.decode_text`).
* **Dialect**: TSV is tab-separated. For CSV the delimiter is chosen among
  ``,``, ``;``, ``|`` and TAB by parsing a sample of the file with each
  candidate in ``strict`` mode: the candidate that gives the most rows with the
  same (at least two) column count wins. Quoting is RFC 4180 (``"`` doubled
  inside quotes); a malformed quote fails the whole file instead of being
  guessed around.
* **Caps**: at most ``max_columns`` columns (more is an error), cells cut at
  ``max_cell_chars`` (warning), at most ``max_rows`` rows (the table is
  marked ``truncated``). The csv module's own field limit stops a single
  unterminated quote from swallowing the file.
* **Formula neutralisation** (C7): every cell that a spreadsheet would read as
  a formula gets a leading ``'`` (:mod:`app.core.csv_safety`).

The output is the table of text cells; mapping columns to dates and amounts
is the importer's job (with the column-mapping review when needed).
"""

from __future__ import annotations

import csv
import io
import re
from collections import Counter
from typing import TYPE_CHECKING, Final

from app.core.csv_safety import neutralise_formula
from app.ingestion.extract.common import ExtractFailure, decode_text
from app.ingestion.extract.registry import register
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Table

if TYPE_CHECKING:
    import _csv

VERSION: Final = "delimited/1"
CSV_DELIMITERS: Final = (",", ";", "|", "\t")
SAMPLE_CHARS: Final = 64 * 1024
SAMPLE_ROWS: Final = 50

_NUMERIC_OR_DATE: Final = re.compile(
    r"[+-]?[$€£¥]?\s?\(?\d[\d,.' ]*\)?|\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}"
)


def _sample(text: str) -> str:
    if len(text) <= SAMPLE_CHARS:
        return text
    cut = text.rfind("\n", 0, SAMPLE_CHARS)
    return text[: cut if cut > 0 else SAMPLE_CHARS]


def _reader(text: str, delimiter: str) -> _csv.Reader:
    return csv.reader(
        io.StringIO(text, newline=""),
        delimiter=delimiter,
        quotechar='"',
        doublequote=True,
        skipinitialspace=False,
        strict=True,
    )


def _score(sample: str, delimiter: str) -> tuple[float, int] | None:
    counts: Counter[int] = Counter()
    try:
        for index, row in enumerate(_reader(sample, delimiter)):
            if index >= SAMPLE_ROWS:
                break
            if any(cell.strip() for cell in row):
                counts[len(row)] += 1
    except csv.Error:
        return None
    if not counts:
        return None
    columns, rows = counts.most_common(1)[0]
    if columns < 2:
        return None
    return rows / sum(counts.values()), columns


def detect_delimiter(text: str) -> str:
    """The CSV delimiter (module doc); no consistent candidate is an error."""
    sample = _sample(text)
    best: tuple[tuple[float, int], str] | None = None
    for delimiter in CSV_DELIMITERS:
        score = _score(sample, delimiter)
        if score is not None and (best is None or score > best[0]):
            best = (score, delimiter)
    if best is None:
        raise ExtractFailure("no_delimiter", "no consistent delimiter found")
    return best[1]


def _looks_like_value(cell: str) -> bool:
    return bool(_NUMERIC_OR_DATE.fullmatch(cell.strip()))


def _has_header(rows: list[list[str]]) -> bool:
    if len(rows) < 2:
        return False
    first, second = rows[0], rows[1]
    if not all(cell.strip() for cell in first):
        return False
    return not any(_looks_like_value(c) for c in first) and any(
        _looks_like_value(c) for c in second
    )


def extract_delimited(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    text, encoding = decode_text(data)
    delimiter = "\t" if kind == "tsv" else detect_delimiter(text)
    csv.field_size_limit(max(options.max_cell_chars * 4, 1024))
    rows: list[list[str]] = []
    warnings: list[str] = []
    truncated = False
    cut_cells = 0
    reader = _reader(text, delimiter)
    try:
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            if len(row) > options.max_columns:
                raise ExtractFailure("too_many_columns", f"more than {options.max_columns} columns")
            if len(rows) >= options.max_rows:
                truncated = True
                break
            cells = []
            for cell in row:
                if len(cell) > options.max_cell_chars:
                    cell = cell[: options.max_cell_chars]
                    cut_cells += 1
                cells.append(neutralise_formula(cell))
            rows.append(cells)
    except csv.Error as exc:
        raise ExtractFailure("malformed_csv", f"line {reader.line_num}: {exc}") from None
    if not rows:
        raise ExtractFailure("empty_table", "no rows")
    if cut_cells:
        warnings.append(f"cells_truncated:{cut_cells}")
    if truncated:
        warnings.append(f"rows_truncated:{options.max_rows}")
    table = Table(
        encoding=encoding,
        delimiter=delimiter,
        has_header=_has_header(rows),
        rows=rows,
        column_count=max(len(r) for r in rows),
        truncated=truncated,
    )
    return ExtractResult(
        kind=kind, ok=True, extractor_version=VERSION, tables=[table], warnings=warnings
    )


register("csv", extract_delimited)
register("tsv", extract_delimited)
