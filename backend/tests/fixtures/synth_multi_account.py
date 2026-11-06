# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared model and drawing helpers for the synthetic multi-account statement PDFs.

Two generators build on this module, each redrawing a statement *layout*
(never a real statement) with invented people, account numbers, instruments
and amounts:

* ``tests/fixtures/synth_fidelity_report.py``: a three-account brokerage
  "INVESTMENT REPORT" in the layout traits of a Fidelity statement.
* ``tests/fixtures/synth_neutral_report.py``: a provider-neutral combined
  statement (checking, savings and brokerage under one invented bank).

The model is what a correct parser must return, per account:

* :class:`Position`: one holding at the period end. ``per_100`` marks a bond
  priced per 100 of face value (``market_value = quantity x price / 100``).
* :class:`Activity`: one activity row. ``amount`` is the signed cash effect
  (the :class:`app.ingestion.sandbox.protocol.InvestmentActivity`
  convention); an in-kind securities transfer has no cash effect
  (``amount == 0``) and keeps the value the statement prints in
  ``printed``. ``quantity`` is in units, positive. ``cost`` is a
  transaction cost already included in ``amount``. ``day`` is the first
  date the row prints (the trade or transaction date), ``post_day`` a second
  one (a post date) when the row has it.
* :class:`Account`: ``kind="investment"`` (positions, activity, the core /
  sweep cash as ``cash_opening``/``cash_closing``) or ``kind="bank"``
  (activity only; ``cash_opening``/``cash_closing`` are the opening and
  closing balances). ``cash_closing`` is always ``cash_opening`` plus the
  activity, so a figure that does not add up is a fixture bug
  (:func:`check_account` asserts it).
* :class:`Report`: the statement period and the accounts in print order.

:func:`expected_activity` / :func:`expected_positions` give the tuples
parser tests compare against (the shape ``tests/unit/test_fidelity_parser.py``
uses for ``synth_fidelity``).
"""

from __future__ import annotations

import io
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Final, Literal

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

CENT: Final = Decimal("0.01")
ZERO: Final = Decimal(0)
FONT: Final = "Helvetica"
BOLD: Final = "Helvetica-Bold"
#: The line every synthetic page prints, so nobody mistakes one for a statement.
DISCLAIMER: Final = "Synthetic test document from the PerFin test suite. Not a real statement."

ActivityType = Literal[
    "buy",
    "sell",
    "dividend",
    "interest",
    "fee",
    "reinvest",
    "deposit",
    "withdrawal",
    "transfer_in",
    "transfer_out",
]
AccountKind = Literal["investment", "bank"]


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_EVEN)


def _luhn_value(char: str) -> int:
    return int(char) if char.isdigit() else ord(char) - ord("A") + 10


def cusip(base: str) -> str:
    """``base`` (8 characters, at least one digit) with its CUSIP check digit."""
    assert len(base) == 8, base
    assert any(c.isdigit() for c in base), base
    total = 0
    for index, char in enumerate(base):
        value = _luhn_value(char)
        if index % 2:
            value *= 2
        total += value // 10 + value % 10
    return f"{base}{(10 - total % 10) % 10}"


def isin(base: str) -> str:
    """``base`` (11 characters, two letters first) with its ISIN check digit (Luhn)."""
    assert len(base) == 11, base
    assert base[:2].isalpha(), base
    digits = "".join(str(_luhn_value(c)) for c in base)
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2 == 0:
            value *= 2
        total += value // 10 + value % 10
    return f"{base}{(10 - total % 10) % 10}"


@dataclass(frozen=True)
class Position:
    name: str
    group: str
    quantity: Decimal
    price: Decimal
    begin_value: Decimal
    cost: Decimal | None = None
    symbol: str | None = None
    cusip: str | None = None
    isin: str | None = None
    subgroup: str = ""
    maturity: date | None = None
    coupon: str = ""
    per_100: bool = False
    #: Lines printed under the row (bond terms, a yield note).
    notes: tuple[str, ...] = ()
    qty_places: int = 3
    price_places: int = 3
    #: Footnote markers printed after the price / after the cost basis, part of
    #: the printed cell (``"A"`` gives ``203.15A``, ``" c"`` gives ``$7.10 c``).
    price_mark: str = ""
    cost_mark: str = ""

    @property
    def market_value(self) -> Decimal:
        value = self.quantity * self.price
        return cents(value / 100 if self.per_100 else value)

    @property
    def key(self) -> str:
        key = self.symbol or self.cusip
        assert key is not None
        return key


@dataclass(frozen=True)
class Activity:
    day: date
    type: ActivityType
    section: str
    description: str
    amount: Decimal
    name: str = ""
    symbol: str | None = None
    cusip: str | None = None
    quantity: Decimal | None = None
    price: Decimal | None = None
    cost: Decimal = ZERO
    cost_basis: Decimal | None = None
    printed: Decimal | None = None
    post_day: date | None = None
    reference: str = ""
    #: Lines printed under the row (a wash-sale or gain note, an account ref).
    notes: tuple[str, ...] = ()

    @property
    def key(self) -> str | None:
        return self.symbol or self.cusip

    @property
    def shown(self) -> Decimal:
        """The total the statement prints for the row."""
        return self.amount if self.printed is None else self.printed


@dataclass(frozen=True)
class Account:
    number: str
    title: str
    kind: AccountKind
    subtype: str
    positions: tuple[Position, ...] = ()
    activity: tuple[Activity, ...] = ()
    cash_opening: Decimal | None = None
    #: The index group the account is listed under ("GENERAL INVESTMENTS").
    group: str = ""
    #: The sweep / core position's name and symbol (investment accounts with cash).
    core_name: str = ""
    core_symbol: str | None = None

    @property
    def last4(self) -> str:
        return "".join(c for c in self.number if c.isdigit())[-4:]

    @property
    def cash_closing(self) -> Decimal | None:
        if self.cash_opening is None:
            return None
        return self.cash_opening + sum((a.amount for a in self.activity), ZERO)

    @property
    def holdings_value(self) -> Decimal:
        return sum((p.market_value for p in self.positions), ZERO)

    @property
    def total_value(self) -> Decimal:
        return self.holdings_value + (self.cash_closing or ZERO)

    @property
    def begin_value(self) -> Decimal:
        return sum((p.begin_value for p in self.positions), ZERO) + (self.cash_opening or ZERO)

    def section(self, *names: str) -> list[Activity]:
        return [a for a in self.activity if a.section in names]


@dataclass(frozen=True)
class Report:
    institution: str
    start: date
    end: date
    accounts: tuple[Account, ...]
    owner: str = ""
    address: tuple[str, ...] = field(default_factory=tuple)


def check_account(account: Account) -> None:
    """Assert the invariants a correct parser's reconciliation relies on."""
    if account.cash_opening is None:
        assert not account.activity or all(a.amount == 0 for a in account.activity)
    for row in account.activity:
        if row.type in ("buy", "fee", "withdrawal", "reinvest"):
            assert row.amount < 0, row
        if row.type in ("sell", "dividend", "interest", "deposit"):
            assert row.amount > 0, row
        if row.type in ("transfer_in", "transfer_out") and row.quantity is not None:
            assert row.amount == 0, row
        if row.type in ("buy", "sell") and row.quantity is not None and row.price is not None:
            gross = cents(row.quantity * row.price)
            sign = -1 if row.type == "buy" else 1
            assert row.amount == sign * gross - row.cost, row


def expected_activity(
    account: Account,
) -> list[tuple[date, str, str | None, Decimal | None, Decimal]]:
    """``(date, type, symbol or CUSIP, quantity, cash amount)`` per row, sorted."""
    rows = [(a.day, a.type, a.key, a.quantity, a.amount) for a in account.activity]
    return sorted(rows, key=lambda r: (r[0], r[1], r[2] or "", r[4]))


def expected_positions(account: Account) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    """``(symbol or CUSIP, quantity, printed price, market value)`` per position, sorted.

    The core / sweep cash is not a position (it is ``cash_closing``). A bond's
    price is the printed price per 100 of face value (:attr:`Position.per_100`).
    """
    return sorted((p.key, p.quantity, p.price, p.market_value) for p in account.positions)


def expected_bank_rows(account: Account) -> list[tuple[date, Decimal, str]]:
    """``(date, signed amount, description)`` of a bank account's rows, in print order."""
    return [(a.day, a.amount, a.description) for a in account.activity]


# ---------------------------------------------------------------------------
# Text formatting
# ---------------------------------------------------------------------------


def money(value: Decimal, *, dollar: bool = False, plus: bool = False, minus: str = "-") -> str:
    """``-$1,234.56`` (``minus`` is the sign printed for a negative value)."""
    sign = minus if value < 0 else ("+" if plus and value > 0 else "")
    return f"{sign}{'$' if dollar else ''}{abs(value):,.2f}"


def number(value: Decimal, places: int) -> str:
    return f"{value:,.{places}f}"


def md(day: date, *, pad: bool = False) -> str:
    """A year-less ``M/D`` date (``6/1``; ``6/01`` with ``pad``)."""
    return f"{day.month}/{day.day:02d}" if pad else f"{day.month}/{day.day}"


def mdy_short(day: date) -> str:
    """``M/D/YY`` (a bond maturity)."""
    return f"{day.month}/{day.day}/{day:%y}"


def wrap(text: str, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > width:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(current)
    return lines


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------

ROW: Final = 10.0
TOP: Final = 700.0
#: Cells of one printed line: x (points) -> text.
Cells = Mapping[int, str] | Mapping[float, str]


class Cursor:
    """Lines drawn top to bottom on one page; cells are anchored left or right."""

    def __init__(self, c: canvas.Canvas, y: float = TOP) -> None:
        self.c = c
        self.y = y

    def line(
        self,
        left: Cells | None = None,
        right: Cells | None = None,
        *,
        font: str = FONT,
        size: float = 7,
        gap: float = ROW,
    ) -> None:
        self.c.setFont(font, size)
        for x, text in (left or {}).items():
            if text:
                self.c.drawString(x, self.y, text)
        for x, text in (right or {}).items():
            if text:
                self.c.drawRightString(x, self.y, text)
        self.y -= gap
        assert self.y > 50, "page overflow: the fixture page plan needs another page"

    def heading(self, text: str, *, size: float = 9, right: Cells | None = None) -> None:
        self.y -= 3
        self.line({40: text}, right, font=BOLD, size=size, gap=ROW + 3)

    def skip(self, points: float = 6) -> None:
        self.y -= points


Page = Callable[[Cursor], None]
Chrome = Callable[[canvas.Canvas, int, int], float]


def render(
    pages: Sequence[Page],
    chrome: Chrome,
    *,
    title: str = "",
    author: str = "",
    subject: str = "",
) -> bytes:
    """Draw ``pages`` (each gets a :class:`Cursor` below what ``chrome`` drew).

    ``chrome(canvas, page_number, page_count)`` draws the page header and
    footer and returns the y where the page body starts. ``title``/``author``
    go into the PDF metadata only.
    """
    buffer = io.BytesIO()
    c = canvas.Canvas(buffer, pagesize=letter, invariant=True)
    c.setTitle(title)
    c.setAuthor(author)
    c.setSubject(subject)
    c.setCreator("PerFin test suite")
    total = len(pages)
    for number_, draw in enumerate(pages, start=1):
        top = chrome(c, number_, total)
        draw(Cursor(c, top))
        c.showPage()
    c.save()
    return buffer.getvalue()
