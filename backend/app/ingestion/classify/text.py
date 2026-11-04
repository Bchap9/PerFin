# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the classifier looks at: a bounded, normalised view of an extraction.

:class:`DocumentView` is built from the validated sandbox output
(:class:`~app.ingestion.sandbox.protocol.ExtractResult`) in the worker; it
never opens file content itself. Everything is capped: at most
:data:`MAX_LINES` lines of at most :data:`MAX_LINE_CHARS` characters, the
"header" is the first :data:`HEADER_LINES` lines of the first page (or the
mail subject and the first body lines), and table headers are the first row
of each table. The view holds only what the extraction already holds (last
four digits of account numbers at most; masked numbers stay masked).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Final

from app.ingestion.sandbox.protocol import ExtractResult

MAX_LINES: Final = 4000
MAX_LINE_CHARS: Final = 300
HEADER_LINES: Final = 15
MAX_TABLE_HEADERS: Final = 50
MAX_TOKENS: Final = 20_000

_SPACE: Final = re.compile(r"\s+")
_TOKEN: Final = re.compile(r"[a-z]+|\d+")
_CELL: Final = re.compile(r"[^a-z0-9]+")


def normalise(text: str) -> str:
    """NFKC, case-folded, whitespace collapsed (used for every string comparison)."""
    return _SPACE.sub(" ", unicodedata.normalize("NFKC", text).casefold()).strip()


def normalise_cell(text: str) -> str:
    """A table header cell for layout comparison: letters and digits only, single spaces."""
    return _CELL.sub(" ", normalise(text)).strip()


def tokens(text: str) -> list[str]:
    """Feature tokens: lower-case words of two or more letters; digits by shape (``#4``)."""
    out: list[str] = []
    for match in _TOKEN.finditer(normalise(text)):
        tok = match.group(0)
        if tok[0].isdigit():
            out.append(f"#{min(len(tok), 6)}")
        elif len(tok) >= 2:
            out.append(tok)
        if len(out) >= MAX_TOKENS:
            break
    return out


def _clip(lines: Iterable[str]) -> list[str]:
    out: list[str] = []
    for raw in lines:
        line = _SPACE.sub(" ", raw).strip()
        if line:
            out.append(line[:MAX_LINE_CHARS])
            if len(out) >= MAX_LINES:
                break
    return out


@dataclass(frozen=True)
class DocumentView:
    """Bounded text, header lines, table headers and statement facts of one document."""

    lines: tuple[str, ...]
    header: tuple[str, ...]
    table_headers: tuple[tuple[str, ...], ...] = ()
    #: Transaction dates the extractor already parsed (OFX/QIF statements).
    transaction_dates: tuple[date, ...] = ()
    #: Last four digits reported by a structured statement.
    statement_last4: tuple[str, ...] = ()
    #: Institution names reported by a structured statement (OFX ``FI/ORG``).
    statement_institutions: tuple[str, ...] = ()
    #: Periods reported by a structured statement.
    statement_periods: tuple[tuple[date | None, date | None], ...] = ()
    source: str = "text"
    normalised: tuple[str, ...] = field(default=(), compare=False)

    def __post_init__(self) -> None:
        if not self.normalised:
            object.__setattr__(self, "normalised", tuple(normalise(line) for line in self.lines))

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @classmethod
    def from_text(cls, text: str, *, table_headers: Iterable[Iterable[str]] = ()) -> DocumentView:
        lines = _clip(text.splitlines())
        headers = tuple(
            tuple(normalise_cell(c) for c in row) for row in list(table_headers)[:MAX_TABLE_HEADERS]
        )
        return cls(lines=tuple(lines), header=tuple(lines[:HEADER_LINES]), table_headers=headers)

    @classmethod
    def from_extraction(cls, result: ExtractResult) -> DocumentView:
        raw: list[str] = []
        header: list[str] = []
        source = "text"
        if result.pages:
            source = "pages"
            for index, page in enumerate(result.pages):
                page_lines = [line.text for line in page.lines] or page.text.splitlines()
                clipped = _clip(page_lines)
                if index == 0:
                    header = clipped[:HEADER_LINES]
                raw.extend(clipped)
                if len(raw) >= MAX_LINES:
                    break
        elif result.email is not None:
            source = "email"
            subject = result.email.headers.subject or ""
            body = _clip(result.email.body_text.splitlines())
            raw = _clip([subject, *body])
            header = raw[:HEADER_LINES]
        else:
            raw = _clip(result.text.splitlines())
            header = raw[:HEADER_LINES]
        table_headers: list[tuple[str, ...]] = []
        for table in result.tables:
            if table.rows:
                table_headers.append(tuple(normalise_cell(c) for c in table.rows[0]))
        for page in result.pages:
            for layout in page.tables:
                if layout.rows:
                    table_headers.append(tuple(normalise_cell(c) for c in layout.rows[0]))
        dates: list[date] = []
        last4: list[str] = []
        institutions: list[str] = []
        periods: list[tuple[date | None, date | None]] = []
        for statement in result.statements:
            dates.extend(t.posted for t in statement.transactions)
            if statement.account.last4:
                last4.append(statement.account.last4)
            if statement.account.institution:
                institutions.append(statement.account.institution)
            periods.append((statement.period_start, statement.period_end))
        lines = raw[:MAX_LINES]
        return cls(
            lines=tuple(lines),
            header=tuple(header),
            table_headers=tuple(table_headers[:MAX_TABLE_HEADERS]),
            transaction_dates=tuple(dates),
            statement_last4=tuple(dict.fromkeys(last4)),
            statement_institutions=tuple(dict.fromkeys(institutions)),
            statement_periods=tuple(periods),
            source=source,
        )
