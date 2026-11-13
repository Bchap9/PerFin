# SPDX-License-Identifier: AGPL-3.0-or-later
"""The header of a statement's transaction table: which line, which columns.

Pure functions on a :class:`~app.ingestion.parsers.base.ViewLine`, used by
:mod:`app.ingestion.parsers.table_statement` (its module doc, "Header",
says what a header is to the reader) and testable on hand-built lines
without a PDF.

* :func:`read_header` gives the columns a line declares (a :class:`Column`
  per heading it knows, with the heading cell's horizontal span), or None
  when the line is no header of a table the reader reads.
* :func:`header_like` says a line is made of heading words only: the header
  of a table the reader does **not** read (a daily-balance table), which
  ends the transaction table above it.
* :func:`stack_headers` is where a header printed over several lines becomes
  one line; today it returns the lines as they are.

The vocabulary (:data:`HEADERS`) maps a normalised heading cell to its
:data:`Role`; a parser may pass its own.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Literal

from app.ingestion.parsers.base import ViewLine
from app.ingestion.parsers.text import find_amounts, normalise

#: What a column holds. ``other`` (a heading that is no column we read) and
#: ``across`` (a table repeated side by side) are declared and not yet
#: produced by any rule.
Role = Literal[
    "date",
    "post_date",
    "description",
    "amount",
    "debit",
    "credit",
    "balance",
    "check",
    "foreign",
    "lead",
    "other",
    "across",
]
NUMERIC_ROLES: Final = frozenset({"amount", "debit", "credit", "balance"})

#: Normalised header cell text -> column role.
HEADERS: Final[Mapping[str, Role]] = {
    "date": "date",
    "trans date": "date",
    "transaction date": "date",
    "posting date": "date",
    "posted": "date",
    "value date": "date",
    "post date": "post_date",
    "description": "description",
    "details": "description",
    "transaction": "description",
    "transaction details": "description",
    "transaction description": "description",
    "payee": "description",
    "particulars": "description",
    "narrative": "description",
    "merchant": "description",
    "amount": "amount",
    "amount usd": "amount",
    "debit": "debit",
    "debits": "debit",
    "withdrawals": "debit",
    "withdrawal": "debit",
    "money out": "debit",
    "paid out": "debit",
    "credit": "credit",
    "credits": "credit",
    "deposits": "credit",
    "deposit": "credit",
    "money in": "credit",
    "paid in": "credit",
    "balance": "balance",
    "running balance": "balance",
    "date paid": "date",
    "check no": "check",
    "check number": "check",
    "check num": "check",
    "check": "check",
    "chk no": "check",
    "foreign spending": "foreign",
    "foreign currency": "foreign",
    "foreign amount": "foreign",
    "foreign currency amount": "foreign",
    "original currency": "foreign",
    "original amount": "foreign",
}
#: Roles whose cells hold the transaction's money (a table needs one).
MONEY_ROLES: Final = frozenset({"amount", "debit", "credit"})
#: Words of a heading that names a transaction table without date or
#: description headings (``New Transactions for JORDAN SAMPLE``).
TABLE_HEADING_WORDS: Final = frozenset({"transactions", "activity"})


@dataclass(frozen=True)
class Column:
    role: Role
    x0: float
    x1: float


def read_header(line: ViewLine, headers: Mapping[str, Role] = HEADERS) -> list[Column] | None:
    """The table columns ``line`` declares, or None when it is no header."""
    found: list[Column] = []
    roles: set[Role] = set()
    for cell in line.cells:
        role = headers.get(normalise(cell.text))
        if role is None or role in roles:
            continue
        roles.add(role)
        found.append(Column(role, cell.x0, cell.x1))
    if "date" in roles and roles & {"description", "check"} and roles & MONEY_ROLES:
        return sorted(found, key=lambda c: c.x0)
    return _headless(line, found, roles)


def _headless(line: ViewLine, found: Sequence[Column], roles: set[Role]) -> list[Column] | None:
    """A heading naming the table over money columns only.

    ``New Transactions for JORDAN SAMPLE | Foreign Spending | Amount $``:
    everything left of the first column heading is the **lead**, read
    row by row as the date then the description.
    """
    if roles & {"date", "post_date", "description", "check"} or not roles & MONEY_ROLES:
        return None
    if len(line.cells) > 5 or find_amounts(line.text):
        return None
    first = min(c.x0 for c in found)
    heading = [c for c in line.cells if c.x1 <= first]
    if not any(TABLE_HEADING_WORDS & set(normalise(c.text).split()) for c in heading):
        return None
    return [Column("lead", 0.0, first), *sorted(found, key=lambda c: c.x0)]


def header_like(line: ViewLine, headers: Mapping[str, Role] = HEADERS) -> bool:
    """Two or more cells, all header words, no amount: a table we do not read."""
    return (
        len(line.cells) >= 2
        and not find_amounts(line.text)
        and all(normalise(c.text) in headers for c in line.cells)
    )


def stack_headers(
    lines: Sequence[ViewLine],
    headers: Mapping[str, Role] = HEADERS,
) -> list[ViewLine]:
    """``lines`` with a header printed over several lines made one line.

    No header is stacked yet: the lines are returned as they are.
    """
    return list(lines)
