# SPDX-License-Identifier: AGPL-3.0-or-later
"""A basic payslip parser: employer, period, pay date, gross, deductions, net.

Recognised by its vocabulary (``payslip``, ``pay slip``, ``pay stub``,
``earnings statement``) or three of its labels. Page 1 only. What it reads,
in the wordings payroll services print:

* **Labelled figures** (``Gross pay 4,250.00``): gross (:data:`GROSS_LABELS`),
  net (:data:`NET_LABELS`) and the deductions total (:data:`TOTAL_LABELS`,
  or the word ``Deductions`` with an amount after it). A label naming the
  year so far (``YTD gross pay``) is never read.
* **Current and year-to-date columns**: under a header line naming them
  (``Current | YTD``, ``This Period | YTD``, in either order) a figure is
  the one in the period's column, never the year-to-date one
  (:mod:`app.ingestion.parsers.labelled_cells`); a label whose figures all
  stand outside the period's column is left unread. With no header naming
  the columns the first figure after the label is taken, as before.
* **Sections**: the lines under a *Deductions* (*Taxes*, *Withholdings*)
  heading are the deduction lines, those under *Earnings* (*Payments*,
  *Pay*) the earnings, down to the labelled total. A heading may be one
  cell of a wider line (``PAY | Hours | Rate | Current | YTD | DEDUCTIONS |
  Current | YTD``): the lines under it are split into blocks at each
  heading. A line starting with ``Total`` is never a line item. With no
  printed total the deductions total is the sum of the deduction lines.
* **Labels over values**: a line of two or more labels with no amount takes
  each value from the cell under its label.
* **Period**: ``Pay period <date> - <date>``, else a start and an end label
  (:data:`PERIOD_LABELS`), each with its date after it; one without the
  other is no period. **Pay date**: after ``Pay date``. Dates are numeric
  (order from the values, else ``MM/DD``) or with month names.
* **Employer**: the cell under an ``EMPLOYER`` heading, else what follows
  the word on its line (never another label: ``EMPLOYER | PAY PERIOD``),
  else the first line of the page.

Checks: ``gross - total deductions = net`` (and the deduction lines add up
to their total) within the reconciliation tolerance; a payslip that does not
add up has every amount field below the threshold. On a scan each field is
scaled by the OCR confidence of the cells it was read from; gross,
deductions and net that add up keep full confidence. Payslips are kept on the
document (``extraction.parse.payslip``, ``kind = payslip``, period); they
create no ledger rows (the salary arrives as the bank statement's deposit).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import ClassVar, Final, Literal

from app.core.money import sum_amounts
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import (
    Cell,
    DocumentParser,
    DocumentView,
    ParseOutcome,
    ViewLine,
)
from app.ingestion.parsers.labelled_cells import (
    LabelPair,
    PeriodColumn,
    amount_of,
    current_cells,
    has_letters,
    is_year_to_date,
    label_pairs,
    period_columns,
    value_under,
)
from app.ingestion.parsers.table_statement import label_end
from app.ingestion.parsers.text import (
    FULL_STYLES,
    DateStyle,
    after_label,
    find_amounts,
    find_dates,
    guess_numeric_style,
    normalise,
    numeric_parts,
    separator_sign,
)
from app.ingestion.reconcile import TOLERANCE
from app.ingestion.sandbox.protocol import ParserRef, Payslip, PayslipLine

VOCABULARY: Final = ("payslip", "pay slip", "pay stub", "earnings statement", "pay advice")
#: Labels counted by :meth:`PayslipParser.detect` (three of them make a payslip).
LABELS: Final = (
    "gross pay",
    "net pay",
    "total deductions",
    "pay period",
    "pay date",
    "gross earnings",
    "total earnings",
    "period ending",
    "taxes and deductions",
)
GROSS_LABELS: Final = (
    "gross pay",
    "total pay",
    "gross earnings",
    "total gross",
    "total earnings",
    "gross wages",
)
NET_LABELS: Final = ("net pay", "net amount", "take home pay")
#: The deductions total; the bare word ``deductions`` counts only as a whole
#: label with an amount after it (:func:`_slot`).
TOTAL_LABELS: Final = ("total deductions", "total taxes and deductions", "taxes and deductions")
#: A cell that is one of these, on a line without amounts, starts a section.
DEDUCTION_HEADINGS: Final = frozenset(
    {"deductions", "taxes", "taxes deductions", "taxes and deductions", "withholdings"}
)
EARNING_HEADINGS: Final = frozenset({"earnings", "payments", "pay"})
#: The first and the last day of the period, each under its own label.
PERIOD_LABELS: Final = (
    ("period beginning", "period ending"),
    ("period start", "period end"),
    ("pay begin date", "pay end date"),
    ("pay period start", "pay period end"),
)
#: Text starting with one of these is a label or a heading, never an employer's name.
_NOT_A_NAME: Final = (
    "employer",
    "employee",
    "pay period",
    "pay date",
    "period",
    "pay begin date",
    "pay end date",
    *GROSS_LABELS,
    *NET_LABELS,
    *TOTAL_LABELS,
    *DEDUCTION_HEADINGS,
    *EARNING_HEADINGS,
)
_DATE_TOKEN: Final = re.compile(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}")
_CURRENCY: Final = re.compile(r"\bcurrency\s*:?\s*([A-Z]{3})\b", re.IGNORECASE)
_EVERYWHERE: Final = float("inf")

Slot = Literal["gross", "net", "deductions"]
Section = Literal["earnings", "deductions"]


#: The labelled figures and their wordings, the total first (``total taxes and
#: deductions`` is no gross).
_SLOT_LABELS: Final[tuple[tuple[Slot, tuple[str, ...]], ...]] = (
    ("deductions", TOTAL_LABELS),
    ("gross", GROSS_LABELS),
    ("net", NET_LABELS),
)


def _amount(line: ViewLine, label: str) -> Decimal | None:
    end = label_end(line.text, label)
    if end is None:
        return None
    amounts = after_label(line.text[end:], [cell.text for cell in line.cells])
    return amounts[0].value if amounts else None


def _slot(text: str, *, bare: bool) -> Slot | None:
    """The figure the label ``text`` names, else None.

    ``bare``: the word ``Deductions`` alone names the total (an amount
    follows it, or stands under it). A label of the year so far names
    nothing a payslip keeps.
    """
    if is_year_to_date(text):
        return None
    if any(label_end(text, label) is not None for label in TOTAL_LABELS):
        return "deductions"
    if bare and normalise(text) == "deductions":
        return "deductions"
    if any(label_end(text, label) is not None for label in GROSS_LABELS):
        return "gross"
    if any(label_end(text, label) is not None for label in NET_LABELS):
        return "net"
    return None


def _is_name(text: str) -> bool:
    """``text`` may be an employer's name: words, and no label or heading."""
    norm = normalise(text)
    if not has_letters(text) or find_amounts(text):
        return False
    return not any(norm == label or norm.startswith(f"{label} ") for label in _NOT_A_NAME)


@dataclass
class _Block:
    """The part of the page under one heading cell: its section and its columns."""

    x0: float
    x1: float
    section: Section | None
    columns: list[PeriodColumn]

    @property
    def whole(self) -> bool:
        return self.x0 == -_EVERYWHERE and self.x1 == _EVERYWHERE

    def holds(self, cell: Cell) -> bool:
        return self.x0 <= cell.x0 < self.x1


def _blocks(line: ViewLine, columns: list[PeriodColumn]) -> list[_Block]:
    """The blocks a heading line starts: one per heading cell, split at its left edge."""
    headings: list[tuple[Cell, Section]] = []
    for cell in line.cells:
        norm = normalise(cell.text)
        if norm in DEDUCTION_HEADINGS:
            headings.append((cell, "deductions"))
        elif norm in EARNING_HEADINGS:
            headings.append((cell, "earnings"))
    headings.sort(key=lambda found: found[0].x0)
    blocks: list[_Block] = []
    for at, (cell, section) in enumerate(headings):
        x0 = -_EVERYWHERE if at == 0 else cell.x0
        x1 = headings[at + 1][0].x0 if at + 1 < len(headings) else _EVERYWHERE
        inside = [c for c in columns if x0 <= c.x0 < x1]
        blocks.append(_Block(x0, x1, section, inside))
    return blocks


def _close(blocks: list[_Block], slot: Slot) -> None:
    """A labelled figure ends the section it totals (net pay ends every one)."""
    ends = {"gross": "earnings", "deductions": "deductions"}.get(slot)
    for block in blocks:
        if slot == "net" or block.whole or block.section == ends:
            block.section = None


def _figure(pair: LabelPair, line: ViewLine, blocks: list[_Block]) -> tuple[Cell, Decimal] | None:
    """The period's figure of a labelled pair: its cell and its value.

    Several amounts under a header naming the columns: the one in the
    period's column, or nothing. One amount, or no header: the first. A
    dash between the label and the first figure is the label's separator
    (:func:`app.ingestion.parsers.text.separator_sign`).
    """
    if not pair.amounts:
        return None
    columns = next((b.columns for b in blocks if b.holds(pair.label)), [])
    chosen = pair.amounts[0]
    if len(pair.amounts) > 1 and columns:
        current = current_cells(pair.amounts, columns)
        if not current:
            return None
        chosen = current[0]
    amount = amount_of(chosen)
    if amount is None:  # pragma: no cover - label_pairs only pairs amounts
        return None
    value = amount.value
    if chosen is pair.amounts[0] and separator_sign(amount, [c.text for c in line.cells]):
        value = abs(value)
    return chosen, value


def _item(line: ViewLine, block: _Block) -> tuple[str, Decimal, list[Cell]] | None:
    """The line item ``line`` prints inside ``block``: label, amount, the cells read."""
    inside = tuple(cell for cell in line.cells if block.holds(cell))
    if not inside:
        return None
    part = ViewLine(line.page, line.index, inside, " ".join(c.text for c in inside), line.height)
    if part.norm.split()[:1] == ["total"] or is_year_to_date(part.text):
        return None
    pairs = label_pairs(part)
    amounts = [cell for pair in pairs for cell in pair.amounts]
    if not pairs or not amounts:
        return None
    if block.columns:
        current = current_cells(amounts, block.columns)
        if not current:
            return None
        chosen, label = current[0], pairs[0].label.text
    else:
        chosen, label = amounts[-1], " ".join(pair.label.text for pair in pairs)
    amount = amount_of(chosen)
    label = label.strip()
    if amount is None or not label:
        return None
    return label[:200], abs(amount.value), [pairs[0].label, chosen]


class PayslipParser(DocumentParser):
    name: ClassVar[str] = "payslip_basic"
    version: ClassVar[str] = "2"
    kind: ClassVar[Literal["statement", "payslip"]] = "payslip"

    def detect(self, view: DocumentView) -> float:
        head = f" {view.head_text()} "
        vocabulary = any(f" {word} " in head for word in VOCABULARY)
        labels = sum(1 for label in LABELS if f" {label} " in head)
        if not vocabulary and labels < 3:
            return 0.0
        return round(min(1.0, (0.5 if vocabulary else 0.2) + 0.1 * labels), 4)

    def _styles(self, view: DocumentView) -> tuple[DateStyle, ...]:
        tokens = [
            m.group(0)
            for line in view.lines
            for m in _DATE_TOKEN.finditer(line.text)
            if numeric_parts(m.group(0)) is not None
        ]
        style, _ = guess_numeric_style(tokens)
        ordered = list(FULL_STYLES)
        if style is not None:
            ordered.remove(style)
            ordered.insert(0, style)
        return tuple(ordered)

    def parse(
        self, view: DocumentView, *, threshold: float, date_order: str | None = None
    ) -> ParseOutcome:
        styles = self._styles(view)
        employer: str | None = None
        figures: dict[Slot, Decimal] = {}
        period: tuple[date, date] | None = None
        first_day: tuple[date, list[Cell]] | None = None
        last_day: tuple[date, list[Cell]] | None = None
        pay_date: date | None = None
        currency: str | None = None
        earnings: list[PayslipLine] = []
        deductions: list[PayslipLine] = []
        blocks: list[_Block] = []
        #: Per field, the cells its label and value were read from.
        cells: dict[str, list[Cell]] = {}
        deduction_cells: list[Cell] = []
        lines = [line for line in view.lines if line.page <= 1]
        values: set[int] = set()  # lines read as the values under a line of labels
        for at, line in enumerate(lines):
            if at in values:
                continue
            norm = line.norm
            heading = next((c for c in line.cells if normalise(c.text) == "employer"), None)
            end = label_end(line.text, "employer")
            if heading is not None or end is not None:
                beside = line.text[end:].strip(" :") if end is not None else ""
                under = value_under(lines, at, heading, text=True) if heading is not None else None
                below = under.text.strip() if under is not None else ""
                # ``Employer: <name>`` names it on the line; a heading over a column, under it.
                colon = heading is not None and heading.text.rstrip().endswith(":")
                tries = [(beside, None), (below, under)]
                for text, cell in tries if colon or heading is None else reversed(tries):
                    if _is_name(text):
                        employer = text[:200]
                        cells["employer"] = (
                            [heading, cell] if heading and cell else list(line.cells)
                        )
                        break
            elif employer is None and line.index == 0 and not any(w in norm for w in VOCABULARY):
                employer = line.text.strip()[:200] or None
                cells["employer"] = list(line.cells)
            match = _CURRENCY.search(line.text)
            if match is not None and currency is None:
                currency = match[1].upper()
            end = label_end(line.text, "pay period")
            if end is not None and period is None:
                found = find_dates(line.text[end:], styles)
                if len(found) >= 2 and found[0] <= found[1]:
                    period = (found[0], found[1])
                    cells["period"] = list(line.cells)
            for start_label, end_label in PERIOD_LABELS:
                end = label_end(line.text, start_label)
                found = find_dates(line.text[end:], styles) if end is not None else []
                if found and first_day is None:
                    first_day = (found[0], list(line.cells))
                end = label_end(line.text, end_label)
                found = find_dates(line.text[end:], styles) if end is not None else []
                if found and last_day is None:
                    last_day = (found[0], list(line.cells))
            end = label_end(line.text, "pay date")
            if end is not None and pay_date is None:
                found = find_dates(line.text[end:], styles)
                pay_date = found[0] if found else None
                if pay_date is not None:
                    cells["pay_date"] = list(line.cells)
            columns = period_columns(line)
            if not find_amounts(line.text):
                labels = [cell for cell in line.cells if has_letters(cell.text)]
                over = False
                if len(labels) >= 2 and not columns:
                    for cell in labels:
                        slot = _slot(cell.text, bare=True)
                        under = value_under(lines, at, cell) if slot is not None else None
                        amount = amount_of(under) if under is not None else None
                        if slot is None or under is None or amount is None:
                            continue
                        figures[slot] = abs(amount.value) if slot == "deductions" else amount.value
                        cells[slot] = [cell, under]
                        _close(blocks, slot)
                        over = True
                if over:
                    values.add(at + 1)
                    continue
                started = _blocks(line, columns)
                if started:
                    blocks = started
                elif columns and not blocks:
                    blocks = [_Block(-_EVERYWHERE, _EVERYWHERE, None, columns)]
                elif columns:
                    for block in blocks:
                        block.columns = [c for c in columns if block.x0 <= c.x0 < block.x1]
                continue
            matched = False
            for pair in label_pairs(line):
                slot = _slot(pair.label.text, bare=bool(pair.amounts))
                figure = _figure(pair, line, blocks) if slot is not None else None
                if slot is None or figure is None:
                    continue
                figures[slot] = abs(figure[1]) if slot == "deductions" else figure[1]
                cells[slot] = [pair.label, figure[0]]
                _close(blocks, slot)
                matched = True
            if not matched and not any(block.columns for block in blocks):
                # A word between the label and its figure (``Gross pay USD 4,250.00``).
                for slot, wordings in _SLOT_LABELS:
                    if is_year_to_date(line.text):
                        break
                    value = next(
                        (v for v in (_amount(line, label) for label in wordings) if v is not None),
                        None,
                    )
                    if value is not None:
                        figures[slot] = abs(value) if slot == "deductions" else value
                        cells[slot] = list(line.cells)
                        _close(blocks, slot)
                        matched = True
            if matched:
                continue
            for block in blocks:
                item = _item(line, block) if block.section is not None else None
                if item is None:
                    continue
                target = earnings if block.section == "earnings" else deductions
                target.append(PayslipLine(label=item[0], amount=item[1]))
                if block.section == "deductions":
                    deduction_cells.extend(item[2])
        if (
            period is None
            and first_day is not None
            and last_day is not None
            and first_day[0] <= last_day[0]
        ):
            period = (first_day[0], last_day[0])
            cells["period"] = [*first_day[1], *last_day[1]]
        gross, net, total = figures.get("gross"), figures.get("net"), figures.get("deductions")
        if total is None and deductions:
            total = sum_amounts(d.amount for d in deductions)
            cells["deductions"] = deduction_cells
        adds_up: bool | None = None
        if gross is not None and net is not None and total is not None:
            adds_up = abs(gross - total - net) <= TOLERANCE
            if deductions:
                adds_up = adds_up and (
                    abs(sum_amounts(d.amount for d in deductions) - total) <= TOLERANCE
                )
        outcome = ParseOutcome()
        if gross is None and net is None:
            outcome.warnings.append("no_payslip_figures")
            return outcome
        money = 1.0 if adds_up else (conf.UNRECONCILED if adds_up is False else conf.UNCHECKED)
        scores = {
            "employer": 1.0 if employer else conf.MISSING,
            "period": 1.0 if period else conf.MISSING,
            "pay_date": 1.0 if pay_date else conf.MISSING,
            "gross": money if gross is not None else conf.MISSING,
            "net": money if net is not None else conf.MISSING,
            "deductions": money if total is not None else conf.MISSING,
        }
        scores = conf.scaled_fields(
            scores,
            {name: [c.conf for c in read] for name, read in cells.items()},
            confirmed=("gross", "net", "deductions") if adds_up else (),
        )
        outcome.payslip = Payslip(
            employer=employer,
            period_start=period[0] if period else None,
            period_end=period[1] if period else None,
            pay_date=pay_date,
            currency=currency,
            gross=gross,
            net=net,
            earnings=earnings[:100],
            deductions=deductions[:100],
            total_deductions=total,
            adds_up=adds_up,
            confidence=scores,
            low_confidence=conf.low_fields(scores, threshold),
        )
        return outcome

    def ref(self) -> ParserRef:
        return ParserRef(name=self.name, version=self.version, kind="payslip")
