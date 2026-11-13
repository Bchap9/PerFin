# SPDX-License-Identifier: AGPL-3.0-or-later
"""Header recognition of the generic statement reader, on hand-built lines.

:mod:`app.ingestion.parsers.statement_header` holds pure functions on a
:class:`ViewLine`, so a header is tested here without a PDF: every line is
built from ``(text, x0)`` cells and nothing but column headings is printed.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import ClassVar

from app.ingestion.parsers import statement_header, table_statement
from app.ingestion.parsers.base import Cell, ViewLine
from app.ingestion.parsers.statement_header import (
    Column,
    Role,
    header_like,
    read_header,
    stack_headers,
)


def _line(cells: list[tuple[str, float]], index: int = 0) -> ViewLine:
    """One line of ``(text, x0)`` cells, each five points wide per character."""
    y = 100.0 + 14 * index
    built = tuple(Cell(text, x0, y, x0 + 5.0 * len(text), y + 9.0) for text, x0 in cells)
    return ViewLine(
        page=1, index=index, cells=built, text=" ".join(t for t, _ in cells), height=9.0
    )


def test_one_line_header_is_read() -> None:
    line = _line([("Date", 62.0), ("Description", 150.0), ("Amount", 420.0), ("Balance", 500.0)])
    assert read_header(line) == [
        Column("date", 62.0, 82.0),
        Column("description", 150.0, 205.0),
        Column("amount", 420.0, 450.0),
        Column("balance", 500.0, 535.0),
    ]
    assert not header_like(_line([("Date", 62.0)]))


def test_headless_heading_gives_a_lead_column() -> None:
    line = _line(
        [("New Transactions for the card", 62.0), ("Foreign Spending", 330.0), ("Amount", 500.0)]
    )
    assert read_header(line) == [
        Column("lead", 0.0, 330.0),
        Column("foreign", 330.0, 410.0),
        Column("amount", 500.0, 530.0),
    ]
    # No heading that names a table: money columns alone are no header.
    assert read_header(_line([("Fees for the card", 62.0), ("Amount", 500.0)])) is None
    # A figure on the line: a row or a total, never a header.
    assert read_header(_line([("New Transactions", 62.0), ("Amount 12.00", 500.0)])) is None


def test_a_line_of_headings_we_do_not_read_is_header_like() -> None:
    line = _line([("Date", 62.0), ("Balance", 150.0), ("Date", 300.0), ("Balance", 390.0)])
    assert header_like(line)
    assert read_header(line) is None
    # A word that is no heading, or a figure, and the line is not header-like.
    assert not header_like(_line([("Date", 62.0), ("Notes", 150.0)]))
    assert not header_like(_line([("Date", 62.0), ("Balance 10.00", 150.0)]))


def test_headers_can_be_replaced() -> None:
    line = _line([("When", 62.0), ("What", 150.0), ("Sum", 420.0)])
    assert read_header(line) is None
    own: Mapping[str, Role] = {"when": "date", "what": "description", "sum": "amount"}
    assert [c.role for c in read_header(line, own) or []] == ["date", "description", "amount"]
    assert header_like(line, own)
    assert not header_like(line)


def test_stack_headers_returns_the_lines_unchanged() -> None:
    lines = (
        _line([("Date", 62.0), ("Description", 150.0)], 0),
        _line([("Posted", 62.0), ("Amount", 420.0)], 1),
    )
    stacked = stack_headers(lines)
    assert stacked == list(lines)
    assert all(a is b for a, b in zip(stacked, lines, strict=True))


def test_moved_names_still_import_from_table_statement() -> None:
    for name in ("HEADERS", "Column", "MONEY_ROLES", "NUMERIC_ROLES", "TABLE_HEADING_WORDS"):
        assert getattr(table_statement, name) is getattr(statement_header, name), name
    assert table_statement.Role == statement_header.Role


def test_the_parser_delegates_with_its_own_headers() -> None:
    class Own(table_statement.TableStatementParser):
        headers: ClassVar[Mapping[str, Role]] = {
            "when": "date",
            "what": "description",
            "sum": "amount",
        }

    line = _line([("When", 62.0), ("What", 150.0), ("Sum", 420.0)])
    assert table_statement.TableStatementParser().columns(line) is None
    assert [c.role for c in Own().columns(line) or []] == ["date", "description", "amount"]
    assert Own()._header_like(line)
    assert Own().prepare([line]) == [line]
