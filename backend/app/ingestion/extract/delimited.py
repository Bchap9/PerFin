# SPDX-License-Identifier: AGPL-3.0-or-later
"""CSV and TSV extraction (INGESTION.md "Extract"; SECURITY.md C2, C7).

* **Encoding**: UTF-8 (with or without BOM), else Windows-1252, else Latin-1
  (:func:`app.ingestion.extract.common.decode_text`).
* **Dialect**: TSV is tab-separated. For CSV the delimiter is chosen among
  ``,``, ``;``, ``|`` and TAB by parsing a sample of the file with each
  candidate in ``strict`` mode: the candidate that gives the most rows with the
  same (at least two) column count wins. Quoting is RFC 4180 (``"`` doubled
  inside quotes); a malformed quote fails the whole file instead of being
  guessed around. Two departures real exports make are read:

  * blanks between a closing quote and the delimiter or the line end
    (``"12.50" ``) are dropped before the strict parse (:func:`_tidy`);
    anything else after a closing quote still fails the file;
  * a file whose cells are mostly written ``'like this'`` is read with ``'``
    as its quote character (:func:`quote_char`). An apostrophe inside a cell,
    or at the start of a few cells, is text.
* **Caps**: at most ``max_columns`` columns (more is an error), cells cut at
  ``max_cell_chars`` (warning), at most ``max_rows`` rows (the table is
  marked ``truncated``). The csv module's own field limit stops a single
  unterminated quote from swallowing the file.
* **Formula neutralisation** (C7): every cell that a spreadsheet would read as
  a formula gets a leading ``'`` (:mod:`app.core.csv_safety`). The guard runs
  on the cell values the reader returns, so on what a single-quoted cell holds
  once its quotes are off.

The output is the table of text cells; mapping columns to dates and amounts
is the importer's job (with the column-mapping review when needed).
"""

from __future__ import annotations

import csv
import functools
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
DOUBLE_QUOTE: Final = '"'
SINGLE_QUOTE: Final = "'"

_NUMERIC_OR_DATE: Final = re.compile(
    r"[+-]?[$€£¥]?\s?\(?\d[\d,.' ]*\)?|\d{1,4}[/.-]\d{1,2}[/.-]\d{1,4}"
)


def _sample(text: str) -> str:
    if len(text) <= SAMPLE_CHARS:
        return text
    cut = text.rfind("\n", 0, SAMPLE_CHARS)
    return text[: cut if cut > 0 else SAMPLE_CHARS]


def _reader(
    text: str, delimiter: str, *, skip_space: bool = False, quote: str = '"'
) -> _csv.Reader:
    return csv.reader(
        io.StringIO(text, newline=""),
        delimiter=delimiter,
        quotechar=quote,
        doublequote=True,
        skipinitialspace=skip_space,
        strict=True,
    )


def spaced_quotes(sample: str, delimiter: str, quote: str = '"') -> bool:
    """Whether fields are written ``a, "b, c"``: a space between the delimiter and a quote.

    CSV readers only treat a quote as quoting at the start of a field, so
    such a file splits inside its quoted values unless the space after each
    delimiter is skipped. Files without that pattern are read as before
    (their cells keep any leading spaces).
    """
    return sample.count(f"{delimiter} {quote}") > sample.count(f"{delimiter}{quote}")


@functools.cache
def _fields(delimiter: str, quote: str, skip_space: bool) -> re.Pattern[str]:
    """One match per piece of a delimited text, in order: a field, a delimiter or a line end.

    Group 1 is a quoted field as the csv module reads one (it opens at the
    start of a field, after the spaces the reader skips when ``skip_space``,
    and closes at the first quote that is not doubled); the blanks between
    its closing quote and the delimiter or line end are matched outside the
    group. Group 2 is an unquoted field, group 3 a delimiter or a line end.
    Matching left to right from the start of the text keeps the pattern in
    step with the reader, so a quote inside a field is never taken for one
    that opens or closes a field.
    """
    d, q = re.escape(delimiter), re.escape(quote)
    blank = "[ ]" if delimiter == "\t" else "[ \t]"
    lead = "[ ]*" if skip_space else ""
    return re.compile(
        rf"({lead}{q}[^{q}]*(?:{q}{q}[^{q}]*)*{q})(?:{blank}+(?=[{d}\r\n]|\Z))?"
        rf"|([^{d}\r\n]+)|([{d}\r\n])"
    )


def _tidy(text: str, delimiter: str, quote: str, *, skip_space: bool) -> str:
    """``text`` without the blanks between a closing quote and the delimiter or line end.

    The strict reader refuses a whole file for one such blank (``"12.50" ``
    on a last line). Only spaces and tabs in exactly that place go (never a
    tab that is the delimiter); blanks inside quotes or in unquoted fields
    are content and stay, and other text after a closing quote is left for
    the reader to refuse.
    """
    blank = "[ ]" if delimiter == "\t" else "[ \t]"
    if re.search(rf"{re.escape(quote)}{blank}+(?:[{re.escape(delimiter)}\r\n]|\Z)", text) is None:
        return text  # nothing that could be such a blank: most files
    return _fields(delimiter, quote, skip_space).sub(r"\1\2\3", text)


def _reads(text: str, delimiter: str, quote: str) -> bool:
    skip_space = spaced_quotes(text, delimiter, quote)
    reader = _reader(
        _tidy(text, delimiter, quote, skip_space=skip_space),
        delimiter,
        skip_space=skip_space,
        quote=quote,
    )
    try:
        for _ in reader:
            pass
    except csv.Error:
        return False
    return True


def quote_char(sample: str, delimiter: str) -> str:
    """The quote character of the file: ``"``, or ``'`` when most cells are single-quoted.

    ``'`` only when more of the sample's non-empty cells are written
    ``'like this'`` than not *and* the sample reads cleanly that way. An
    apostrophe inside a cell (``O'Brien``), or opening a few cells of an
    otherwise unquoted file, therefore stays text.
    """
    if SINGLE_QUOTE not in sample:
        return DOUBLE_QUOTE
    quoted = plain = 0
    fields = _fields(delimiter, SINGLE_QUOTE, spaced_quotes(sample, delimiter, SINGLE_QUOTE))
    for match in fields.finditer(sample):
        if match.group(1) is not None:
            quoted += 1
        elif match.group(2) is not None and match.group(2).strip():
            plain += 1
    if quoted > plain and _reads(sample, delimiter, SINGLE_QUOTE):
        return SINGLE_QUOTE
    return DOUBLE_QUOTE


def _score(sample: str, delimiter: str) -> tuple[float, int] | None:
    counts: Counter[int] = Counter()
    quote = quote_char(sample, delimiter)
    skip_space = spaced_quotes(sample, delimiter, quote)
    try:
        reader = _reader(
            _tidy(sample, delimiter, quote, skip_space=skip_space),
            delimiter,
            skip_space=skip_space,
            quote=quote,
        )
        for index, row in enumerate(reader):
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
    sample = _sample(text)
    quote = quote_char(sample, delimiter)
    skip_space = spaced_quotes(sample, delimiter, quote)
    reader = _reader(
        _tidy(text, delimiter, quote, skip_space=skip_space),
        delimiter,
        skip_space=skip_space,
        quote=quote,
    )
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
