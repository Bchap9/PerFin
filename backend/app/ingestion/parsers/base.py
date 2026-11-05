# SPDX-License-Identifier: AGPL-3.0-or-later
"""The document parser contract (INGESTION.md "Parse", "Parser contribution model").

Document parsers run **in the worker** on the stored, schema-checked output
of the sandbox (``document.extraction``: pages with words and boxes, from a
PDF's text layer or from OCR). They never see file bytes, so re-running a
newer parser version needs neither the original file nor a new extraction,
and nothing here can be reached by a parser library exploit (ADR-0006).

A parser is a subclass of :class:`DocumentParser` with a unique ``name``, a
``version`` (bumped whenever its output for the same pages may change), the
``kind`` it reads and, for an institution-specific one, the institution
``key`` it is written for (the same key as the classifier's fingerprints and
``institution.builtin_key``). :meth:`DocumentParser.detect` scores how well
the pages fit (0..1); :meth:`DocumentParser.parse` returns a
:class:`ParseOutcome`. Every field a parser produces carries a confidence in
0..1 (:mod:`app.ingestion.parsers.confidence`).

:class:`DocumentView` is the parsers' read-only view of the pages: lines in
reading order, each split into **cells** (runs of words separated by less
than a column gap), with the page, box and the lowest OCR word confidence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from statistics import median
from typing import ClassVar, Final, Literal

from app.ingestion.parsers.text import MAX_LINE, normalise
from app.ingestion.sandbox.protocol import (
    IMAGE_KINDS,
    ExtractResult,
    InvestmentStatement,
    LoanStatement,
    Page,
    Payslip,
    Receipt,
    Statement,
    TaxFormRead,
)

#: Words closer than this many word heights belong to the same cell.
CELL_GAP_FACTOR: Final = 0.9
#: Lines a parser reads at most (bounded work on any extraction).
MAX_LINES: Final = 200_000

ParserKind = Literal["statement", "payslip", "receipt", "tax_form"]


@dataclass(frozen=True)
class Cell:
    text: str
    x0: float
    y0: float
    x1: float
    y1: float
    #: Lowest OCR confidence of the cell's words (0..1); None for a text layer.
    conf: float | None = None

    @property
    def centre(self) -> float:
        return (self.x0 + self.x1) / 2

    def overlap(self, x0: float, x1: float) -> float:
        return max(0.0, min(self.x1, x1) - max(self.x0, x0))


@dataclass(frozen=True)
class ViewLine:
    page: int
    index: int
    cells: tuple[Cell, ...]
    #: The line's words joined by single spaces.
    text: str
    height: float

    @property
    def norm(self) -> str:
        return normalise(self.text)

    @property
    def bbox(self) -> tuple[float, float, float, float]:
        return (
            min(c.x0 for c in self.cells),
            min(c.y0 for c in self.cells),
            max(c.x1 for c in self.cells),
            max(c.y1 for c in self.cells),
        )

    @property
    def conf(self) -> float | None:
        values = [c.conf for c in self.cells if c.conf is not None]
        return min(values) if values else None


def _cells(page: Page, first: int, count: int, gap: float) -> tuple[Cell, ...]:
    words = page.words[first : first + count]
    cells: list[Cell] = []
    texts: list[str] = []
    confs: list[float] = []
    box: list[float] = []

    def close() -> None:
        if texts:
            cells.append(
                Cell(" ".join(texts), box[0], box[1], box[2], box[3], min(confs) if confs else None)
            )

    for word in words:
        x0, y0, x1, y1 = word.bbox
        if texts and x0 - box[2] <= gap:
            texts.append(word.text)
            box = [box[0], min(box[1], y0), max(box[2], x1), max(box[3], y1)]
        else:
            close()
            texts, confs, box = [word.text], [], [x0, y0, x1, y1]
        if word.conf is not None:
            confs.append(word.conf / 100)
    close()
    return tuple(cells)


@dataclass(frozen=True)
class DocumentView:
    """The pages of one extraction as parser input (module doc)."""

    lines: tuple[ViewLine, ...]
    source_format: Literal["pdf", "image"]
    truncated: bool
    page_count: int

    @classmethod
    def from_result(cls, result: ExtractResult) -> DocumentView:
        lines: list[ViewLine] = []
        for page in result.pages:
            heights = [w.bbox[3] - w.bbox[1] for w in page.words if w.bbox[3] > w.bbox[1]]
            unit = median(heights) if heights else 10.0
            for line in page.lines:
                if len(lines) >= MAX_LINES:
                    break
                cells = _cells(page, line.first_word, line.word_count, unit * CELL_GAP_FACTOR)
                if not cells:
                    continue
                text = " ".join(c.text for c in cells)[:MAX_LINE]
                lines.append(
                    ViewLine(
                        page=page.number,
                        index=len(lines),
                        cells=cells,
                        text=text,
                        height=line.bbox[3] - line.bbox[1] or unit,
                    )
                )
        return cls(
            lines=tuple(lines),
            source_format="image" if result.kind in IMAGE_KINDS else "pdf",
            truncated=result.truncated,
            page_count=len(result.pages),
        )

    def head_text(self, pages: int = 1) -> str:
        """Normalised text of the first ``pages`` pages (for fingerprints)."""
        return " ".join(line.norm for line in self.lines if line.page <= pages)


@dataclass
class ParseOutcome:
    statements: list[Statement] = field(default_factory=list)
    #: Brokerage statements (``app.ingestion.parsers.brokerage``).
    investments: list[InvestmentStatement] = field(default_factory=list)
    payslip: Payslip | None = None
    receipt: Receipt | None = None
    #: A loan statement (``app.ingestion.parsers.loan_statement``).
    loan: LoanStatement | None = None
    #: Tax forms (``app.ingestion.parsers.tax_forms``; amounts only).
    tax_forms: list[TaxFormRead] = field(default_factory=list)
    pending_items: int = 0
    skipped_lines: int = 0
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return (
            not self.payslip
            and not self.receipt
            and not self.loan
            and not self.tax_forms
            and not any(s.transactions for s in self.statements)
            and not any(i.positions or i.activity for i in self.investments)
        )


class DocumentParser(ABC):
    """One parser (module doc)."""

    name: ClassVar[str]
    version: ClassVar[str]
    kind: ClassVar[ParserKind]
    #: Institution key (``institution.builtin_key``); None for a generic parser.
    institution: ClassVar[str | None] = None

    @abstractmethod
    def detect(self, view: DocumentView) -> float:
        """How well the pages fit this parser (0..1)."""

    @abstractmethod
    def parse(
        self, view: DocumentView, *, threshold: float, date_order: str | None = None
    ) -> ParseOutcome:
        """Read the pages; fields below ``threshold`` are listed as low confidence.

        ``date_order`` (``mdy``/``dmy``/``ymd``) is the classifier fingerprint's
        hint for a generic parser; a parser with a declared format ignores it.
        """
