# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the review tools read from a stored extraction (pure; no database, no file content).

Everything here works on the validated sandbox output kept in
``document.extraction`` (:class:`~app.ingestion.sandbox.protocol.ExtractResult`);
nothing opens or parses the original file (ADR-0006/0009).

* **Layout tables as import tables.** A PDF or image page's tables
  (:class:`LayoutTable`, from ruling lines or aligned columns) become one
  :class:`Table` for the Phase 2 column-mapping model
  (:func:`layout_import_tables`): tables with the same header row on
  several pages are joined (repeated header rows dropped), the largest
  group wins. Cells are formula-neutralised like CSV cells (C7). With
  :func:`with_layout_tables` the stored extraction of a PDF that no parser
  could read offers that table to ``GET /imports/{id}/preview``, the
  mapping editor and the importer; a saved mapping's header fingerprint
  then matches the next PDF of the same layout (a reusable fingerprint).
* **"Remember this layout".** :func:`derive_match` builds the
  :class:`~app.ingestion.classify.fingerprints.MatchSpec` of a household
  fingerprint from the document's header lines and table header: the
  first distinctive header line must hold (``all``), further header lines
  and the column layout corroborate (``any``). Lines with digits runs
  (dates, amounts, account numbers) are never used, so no account number
  ends up in a fingerprint.
* **Regions.** :func:`page_of` and :func:`row_region` find a page and the
  box a parser read a row from, for the review UI's highlight.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError

from app.core.csv_safety import neutralise_formula
from app.ingestion.classify.fingerprints import FingerprintError, MatchSpec, parse_match_rules
from app.ingestion.classify.text import HEADER_LINES, DocumentView, normalise, normalise_cell
from app.ingestion.sandbox.protocol import (
    MAX_CELL_CEILING,
    BBox,
    ExtractedTransaction,
    ExtractResult,
    LayoutTable,
    Page,
    Table,
)

#: Rows of a joined layout table (the importer's table cap is far larger).
MAX_LAYOUT_ROWS: Final = 10_000
#: Header lines used for a remembered layout.
MAX_HEADER_RULES: Final = 4
MIN_HEADER_CHARS: Final = 4
MAX_HEADER_CHARS: Final = 120
#: A header line with a run of this many digits is not layout (dates, amounts, numbers).
_DIGIT_RUN: Final = re.compile(r"\d{2,}")
_LETTERS: Final = re.compile(r"[^\W\d_]{3,}")


def stored_result(extraction: dict[str, Any] | None) -> ExtractResult | None:
    """The validated stored extraction, or None (absent or no longer valid)."""
    if not extraction:
        return None
    try:
        return ExtractResult.model_validate(extraction)
    except ValidationError:
        return None


# ---------------------------------------------------------------------------
# Layout tables -> import tables
# ---------------------------------------------------------------------------


def _clean(cell: str) -> str:
    return neutralise_formula(" ".join(cell.split()))[:MAX_CELL_CEILING]


def _header_key(table: LayoutTable) -> tuple[str, ...]:
    return tuple(normalise_cell(c) for c in table.rows[0]) if table.rows else ()


def layout_import_tables(pages: Sequence[Page]) -> list[Table]:
    """The pages' layout tables as import tables, the largest joined group first."""
    groups: dict[tuple[str, ...], list[list[str]]] = {}
    widths: dict[tuple[str, ...], int] = {}
    order: list[tuple[str, ...]] = []
    for page in pages:
        for layout in page.tables:
            if len(layout.rows) < 2 or layout.column_count < 2:
                continue
            key = _header_key(layout)
            if not any(key):
                continue
            rows = groups.get(key)
            if rows is None:
                rows = groups[key] = [[_clean(c) for c in layout.rows[0]]]
                widths[key] = layout.column_count
                order.append(key)
            for raw in layout.rows[1:]:
                if tuple(normalise_cell(c) for c in raw) == key:
                    continue  # the header repeated on a later page
                if len(rows) >= MAX_LAYOUT_ROWS:
                    break
                rows.append([_clean(c) for c in raw])
            widths[key] = max(widths[key], layout.column_count)
    ranked = sorted(order, key=lambda k: (-len(groups[k]), order.index(k)))
    return [
        Table(
            encoding="utf-8",
            delimiter="\t",
            has_header=True,
            rows=groups[key],
            column_count=widths[key],
            truncated=len(groups[key]) >= MAX_LAYOUT_ROWS,
        )
        for key in ranked
    ]


def with_layout_tables(result: ExtractResult) -> ExtractResult:
    """``result`` with its page tables as import tables when it has no records otherwise.

    Only for documents with pages (PDF, images) whose parsers produced no
    statement and that carry no structured table; anything else is
    returned unchanged.
    """
    if result.statements or result.tables or not result.pages:
        return result
    if result.parse is not None and (
        result.parse.receipt is not None or result.parse.payslip is not None
    ):
        return result  # a receipt's or payslip's lines are not ledger rows
    tables = layout_import_tables(result.pages)
    if not tables:
        return result
    return result.model_copy(update={"tables": tables[:1]})


# ---------------------------------------------------------------------------
# "Remember this layout"
# ---------------------------------------------------------------------------


def _layout_line(line: str) -> str | None:
    text = " ".join(line.split())
    if not MIN_HEADER_CHARS <= len(text) <= MAX_HEADER_CHARS:
        return None
    if _DIGIT_RUN.search(text) or not _LETTERS.search(text):
        return None
    return text


def header_candidates(view: DocumentView) -> list[str]:
    """Distinctive header lines (no digit runs), first page top first, unique."""
    out: list[str] = []
    seen: set[str] = set()
    for line in view.header[:HEADER_LINES]:
        text = _layout_line(line)
        if text is None or normalise(text) in seen:
            continue
        seen.add(normalise(text))
        out.append(text)
    return out


def derive_match(result: ExtractResult) -> MatchSpec | None:
    """A household fingerprint's rules for the layout of ``result`` (None: nothing distinctive)."""
    view = DocumentView.from_extraction(result)
    lines = header_candidates(view)
    if not lines:
        return None
    rules: list[dict[str, Any]] = [{"header": lines[0]}]
    corroborating: list[dict[str, Any]] = [{"header": text} for text in lines[1:MAX_HEADER_RULES]]
    columns = [c for c in (view.table_headers[0] if view.table_headers else ()) if c][:20]
    if len(columns) >= 2 and all(1 <= len(c) <= 64 for c in columns):
        corroborating.append({"columns": list(columns)})
    data: dict[str, Any] = {"all": rules}
    if corroborating:
        data["any"] = corroborating
        data["min_any"] = 1
    try:
        return parse_match_rules(data)
    except FingerprintError:
        return None


# ---------------------------------------------------------------------------
# Regions
# ---------------------------------------------------------------------------


def page_of(result: ExtractResult, number: int) -> Page | None:
    return next((p for p in result.pages if p.number == number), None)


@dataclass(frozen=True)
class RowRegion:
    page: int
    bbox: BBox
    text: str


def _line_text(page: Page, bbox: BBox) -> str:
    """The words inside ``bbox`` (vertical centre inside the box), left to right."""
    x0, y0, x1, y1 = bbox
    words = [
        w
        for w in page.words
        if y0 - 1 <= (w.bbox[1] + w.bbox[3]) / 2 <= y1 + 1 and w.bbox[2] >= x0 and w.bbox[0] <= x1
    ]
    words.sort(key=lambda w: (w.line, w.bbox[0]))
    return " ".join(w.text for w in words)[:1000]


def _same(row: ExtractedTransaction, posted: date, amount: Decimal) -> bool:
    return row.posted == posted and row.amount == amount


def row_region(
    result: ExtractResult, *, page: int | None, posted: date, amount: Decimal
) -> RowRegion | None:
    """Where a parser read the row ``(posted, amount)`` (on ``page`` when given)."""
    for statement in result.statements:
        for row in statement.transactions:
            if row.page is None or row.bbox is None:
                continue
            if page is not None and row.page != page:
                continue
            if _same(row, posted, amount):
                found = page_of(result, row.page)
                text = _line_text(found, row.bbox) if found is not None else ""
                return RowRegion(row.page, row.bbox, text)
    return None


def words_matching(page: Page, needles: Iterable[str]) -> list[BBox]:
    """Boxes of the words ending with any of ``needles`` (e.g. last four digits)."""
    wanted = tuple(n for n in needles if n)
    if not wanted:
        return []
    return [w.bbox for w in page.words if w.text.rstrip(".,;:)").endswith(wanted)][:10]
