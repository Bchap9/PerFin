# SPDX-License-Identifier: AGPL-3.0-or-later
"""Layout from positioned words: lines, tables from aligned columns, text quality.

Shared by the PDF text layer and OCR (run only inside the sandbox child).
Coordinates are page units with the origin top left; every algorithm here is
linear or ``n log n`` in the number of words, with the per-page word count
capped by the caller (:data:`app.ingestion.sandbox.protocol.MAX_WORDS_PER_PAGE`).

* :func:`group_lines` puts words whose vertical centres fall inside the
  current line's band on one line, left to right.
* :func:`column_tables` finds tables without ruling lines: runs of at least
  :data:`MIN_TABLE_ROWS` consecutive lines with two or more cells that share
  at least one vertical gutter (whitespace that crosses every line of the
  run). The gutters become column boundaries; cells are the words between
  them. Formula characters in cells are neutralised (C7) like CSV cells.
* :func:`text_quality` measures how much of a text layer is real text
  (letters, digits, common punctuation) as opposed to replacement
  characters, private-use glyphs and control bytes, which is what a broken
  font encoding produces; the PDF extractor uses it with the text density to
  decide whether a page needs OCR.
"""

from __future__ import annotations

import itertools
import unicodedata
from dataclasses import dataclass, field
from statistics import median
from typing import Final

from app.core.csv_safety import neutralise_formula
from app.ingestion.sandbox.protocol import LayoutTable, Line, Word

MIN_TABLE_ROWS: Final = 3
#: A gap wider than this many median word heights separates two cells.
CELL_GAP_FACTOR: Final = 1.2
#: A gutter must be at least this many median word heights wide.
GUTTER_FACTOR: Final = 0.6
#: A vertical gap larger than this many line heights ends a table.
ROW_GAP_FACTOR: Final = 2.5


@dataclass
class RawWord:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    conf: float | None = None

    @property
    def yc(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 0.0)


@dataclass
class RawLine:
    words: list[RawWord] = field(default_factory=list)

    @property
    def x0(self) -> float:
        return min(w.x0 for w in self.words)

    @property
    def x1(self) -> float:
        return max(w.x1 for w in self.words)

    @property
    def y0(self) -> float:
        return min(w.y0 for w in self.words)

    @property
    def y1(self) -> float:
        return max(w.y1 for w in self.words)

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


def clean_word(text: str) -> str:
    """Drop control and format characters, collapse whitespace; may return ''."""
    out = []
    for ch in text:
        category = unicodedata.category(ch)
        if category in ("Cc", "Cf", "Cs"):
            continue
        out.append(" " if ch.isspace() else ch)
    return " ".join("".join(out).split())[:512]


def group_lines(words: list[RawWord]) -> list[RawLine]:
    """Words grouped into lines (top to bottom), each line left to right."""
    ordered = sorted(words, key=lambda w: (w.yc, w.x0))
    lines: list[RawLine] = []
    band_top = band_bottom = 0.0
    for word in ordered:
        if lines and band_top <= word.yc <= band_bottom:
            lines[-1].words.append(word)
            # Keep the band to the line's first word plus a little slack.
            continue
        lines.append(RawLine([word]))
        half = max(word.height, 1e-6) / 2
        band_top, band_bottom = word.yc - half * 0.8, word.yc + half * 0.8
    for line in lines:
        line.words.sort(key=lambda w: w.x0)
    return lines


def to_models(lines: list[RawLine]) -> tuple[list[Word], list[Line]]:
    """Protocol words and lines (word ``line`` indices refer to the line list)."""
    words: list[Word] = []
    out_lines: list[Line] = []
    for index, line in enumerate(lines):
        first = len(words)
        for raw in line.words:
            words.append(
                Word(
                    text=raw.text,
                    bbox=(raw.x0, raw.y0, raw.x1, raw.y1),
                    line=index,
                    conf=raw.conf,
                )
            )
        out_lines.append(
            Line(
                text=line.text,
                bbox=(line.x0, line.y0, line.x1, line.y1),
                first_word=first,
                word_count=len(line.words),
            )
        )
    return words, out_lines


def _cells(line: RawLine, gap: float) -> list[tuple[float, float]]:
    spans: list[tuple[float, float]] = []
    for word in line.words:
        if spans and word.x0 - spans[-1][1] <= gap:
            spans[-1] = (spans[-1][0], max(spans[-1][1], word.x1))
        else:
            spans.append((word.x0, word.x1))
    return spans


def _free(spans: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """The gaps between consecutive cell spans of one line."""
    return [(a[1], b[0]) for a, b in itertools.pairwise(spans) if b[0] > a[1]]


def _intersect(
    gutters: list[tuple[float, float]], spans: list[tuple[float, float]], minimum: float
) -> list[tuple[float, float]]:
    """``gutters`` narrowed to the space ``spans`` (one line's cells) leave free."""
    result: list[tuple[float, float]] = []
    for g0, g1 in gutters:
        pieces = [(g0, g1)]
        for s0, s1 in spans:
            next_pieces: list[tuple[float, float]] = []
            for p0, p1 in pieces:
                if s1 <= p0 or s0 >= p1:
                    next_pieces.append((p0, p1))
                    continue
                if s0 > p0:
                    next_pieces.append((p0, s0))
                if s1 < p1:
                    next_pieces.append((s1, p1))
            pieces = next_pieces
        result.extend(p for p in pieces if p[1] - p[0] >= minimum)
    return result


def _table(block: list[RawLine], gutters: list[tuple[float, float]]) -> LayoutTable:
    bounds = sorted((g0 + g1) / 2 for g0, g1 in gutters)
    rows: list[list[str]] = []
    for line in block:
        cells: list[list[str]] = [[] for _ in range(len(bounds) + 1)]
        for word in line.words:
            centre = (word.x0 + word.x1) / 2
            column = sum(1 for bound in bounds if centre > bound)
            cells[column].append(word.text)
        rows.append([neutralise_formula(" ".join(cell)) for cell in cells])
    return LayoutTable(
        source="columns",
        bbox=(
            min(line.x0 for line in block),
            min(line.y0 for line in block),
            max(line.x1 for line in block),
            max(line.y1 for line in block),
        ),
        rows=rows,
        column_count=len(bounds) + 1,
        column_bounds=bounds,
    )


def column_tables(lines: list[RawLine], *, min_rows: int = MIN_TABLE_ROWS) -> list[LayoutTable]:
    """Tables of aligned columns (module doc), top to bottom."""
    heights = [w.height for line in lines for w in line.words if w.height > 0]
    if not heights:
        return []
    unit = median(heights)
    gap, minimum = unit * CELL_GAP_FACTOR, unit * GUTTER_FACTOR
    tables: list[LayoutTable] = []
    index = 0
    while index < len(lines):
        spans = _cells(lines[index], gap)
        if len(spans) < 2:
            index += 1
            continue
        block = [lines[index]]
        gutters = [g for g in _free(spans) if g[1] - g[0] >= minimum]
        cursor = index + 1
        while cursor < len(lines) and gutters:
            line = lines[cursor]
            line_spans = _cells(line, gap)
            if len(line_spans) < 2:
                break
            if line.y0 - block[-1].y1 > unit * ROW_GAP_FACTOR:
                break
            narrowed = _intersect(gutters, line_spans, minimum)
            if not narrowed:
                break
            gutters = narrowed
            block.append(line)
            cursor += 1
        if len(block) >= min_rows and gutters:
            tables.append(_table(block, gutters))
            index = cursor
        else:
            index += 1
    return tables


_COMMON_PUNCT: Final = frozenset(".,:;-+/\\()[]{}$%&*#@'\"!?=_<>|~^`€£¥")


def text_quality(text: str) -> float:
    """Share of non-space characters that look like real text (0..1; 1 for no text)."""
    total = good = 0
    for ch in text:
        if ch.isspace():
            continue
        total += 1
        if (ch.isalnum() and unicodedata.category(ch)[0] in ("L", "N")) or ch in _COMMON_PUNCT:
            good += 1
    return good / total if total else 1.0


def lines_text(lines: list[RawLine]) -> str:
    return "\n".join(line.text for line in lines)
