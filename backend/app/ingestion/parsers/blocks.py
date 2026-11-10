# SPDX-License-Identifier: AGPL-3.0-or-later
"""Blocks printed side by side: read one after the other, not line by line.

A statement page often prints two things next to each other: a summary box
left of a table (``Debit Card Summary`` beside ``Debit Card Activity``), two
summary tables (``Core Account Cash Flow`` beside ``Income Summary``), a
table left of a chart and its legend. The extraction's lines run across the
whole page, so a line of such a page holds a piece of each block, a table
header shares its line with the other block's figure (and is no header any
more), and a label is torn from its amounts when the other block's row sits
a few points higher.

:func:`unfold_blocks` puts each block's lines back together. A **block
title** opens a region: a cell the caller knows as a heading
(``titled``), well right of the page's left edge, on a line that holds no
amount and nothing else but titles: two titles side by side, or the right
block's title of two or more words alone on its line (a table's header
cell that happens to be a heading word, ``Description | Contributions``,
opens nothing). From there to the end of the region every line is
cut at the title's left edge, the pieces of each side are joined again by
their own height on the page (so a label and its amounts are one line
whatever the other block printed beside them), and the left block's lines
come first, then the right block's. The region ends at the page's end, at a
line with a cell across the cut (text over the full width) or at a titled
line of the left block alone that stands clear of the lines above it (the
next full-width section: ``Bill Payments`` under the two card blocks).

Pure geometry on :class:`~app.ingestion.parsers.base.ViewLine`: a page
without such a title is returned as it is.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final

from app.ingestion.parsers.base import Cell, ViewLine
from app.ingestion.parsers.text import find_amounts, normalise

#: A block title starts at least this share of the page's text width from its left edge.
MIN_OFFSET: Final = 0.2
#: Cells this close to the cut still belong to the block right of it.
_SLACK: Final = 2.0
#: Pieces whose centres are this share of a line height apart are one line.
_SAME_LINE: Final = 0.45
#: A left-block heading this many line heights under the line above it ends the region.
_CLEAR: Final = 1.5
#: A title cell has at most this many words.
_MAX_TITLE_WORDS: Final = 8

Titled = Callable[[str], bool]


def _has_letter(text: str) -> bool:
    return any(ch.isalpha() for ch in text)


def _title_cut(line: ViewLine, left_edge: float, width: float, titled: Titled) -> float | None:
    """The x a block title on ``line`` cuts the page at, or None (module doc)."""
    if not 1 <= len(line.cells) <= 2 or find_amounts(line.text):
        return None
    if not all(_has_letter(c.text) for c in line.cells):
        return None
    cell = line.cells[-1]
    if cell.x0 - left_edge < MIN_OFFSET * width:
        return None
    words = [len(c.text.split()) for c in line.cells]
    if max(words) > _MAX_TITLE_WORDS or (len(words) == 1 and words[0] < 2):
        return None
    if not all(titled(normalise(c.text)) for c in line.cells):
        return None
    return cell.x0 - _SLACK


def _crosses(line: ViewLine, cut: float) -> bool:
    return any(c.x0 < cut - _SLACK and c.x1 > cut + 2 * _SLACK for c in line.cells)


def _centre(cells: Sequence[Cell]) -> float:
    return sum((c.y0 + c.y1) / 2 for c in cells) / len(cells)


def _join(pieces: list[tuple[ViewLine, list[Cell]]]) -> list[ViewLine]:
    """The pieces of one block as lines: pieces at one height are one line."""
    rows: list[tuple[float, ViewLine, list[Cell]]] = []
    for source, cells in sorted(pieces, key=lambda piece: _centre(piece[1])):
        centre = _centre(cells)
        if rows and abs(centre - rows[-1][0]) <= _SAME_LINE * max(source.height, 1.0):
            rows[-1][2].extend(cells)
            continue
        rows.append((centre, source, list(cells)))
    out: list[ViewLine] = []
    for _, source, cells in rows:
        ordered = tuple(sorted(cells, key=lambda c: c.x0))
        out.append(
            ViewLine(
                page=source.page,
                index=source.index,
                cells=ordered,
                text=" ".join(c.text for c in ordered),
                height=source.height,
            )
        )
    return out


def _unfold_page(lines: Sequence[ViewLine], titled: Titled) -> list[ViewLine]:
    cells = [c for line in lines for c in line.cells]
    if not cells:
        return list(lines)
    left_edge = min(c.x0 for c in cells)
    width = max(c.x1 for c in cells) - left_edge
    out: list[ViewLine] = []
    index = 0
    while index < len(lines):
        cut = _title_cut(lines[index], left_edge, width, titled) if width > 0 else None
        if cut is None:
            out.append(lines[index])
            index += 1
            continue
        left: list[tuple[ViewLine, list[Cell]]] = []
        right: list[tuple[ViewLine, list[Cell]]] = []
        end = index
        bottom = max(c.y1 for c in lines[index].cells)
        while end < len(lines):
            line = lines[end]
            if end > index:
                if _crosses(line, cut):
                    break
                alone = len(line.cells) == 1 and line.cells[0].x1 <= cut
                clear = min(c.y0 for c in line.cells) - bottom >= _CLEAR * max(line.height, 1.0)
                if alone and clear and not find_amounts(line.text) and titled(line.norm):
                    break
            near = [c for c in line.cells if c.x0 < cut]
            far = [c for c in line.cells if c.x0 >= cut]
            if near:
                left.append((line, near))
            if far:
                right.append((line, far))
            bottom = max(bottom, *(c.y1 for c in line.cells))
            end += 1
        if left and right:
            out.extend(_join(left))
            out.extend(_join(right))
        else:
            out.extend(lines[index:end])
        index = end
    return out


def unfold_blocks(lines: Sequence[ViewLine], titled: Titled) -> list[ViewLine]:
    """``lines`` with side-by-side blocks read one after the other (module doc)."""
    out: list[ViewLine] = []
    start = 0
    for stop in range(1, len(lines) + 1):
        if stop == len(lines) or lines[stop].page != lines[start].page:
            out.extend(_unfold_page(lines[start:stop], titled))
            start = stop
    return out
