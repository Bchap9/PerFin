# SPDX-License-Identifier: AGPL-3.0-or-later
"""A basic payslip parser: employer, period, pay date, gross, deductions, net.

Recognised by its vocabulary (``payslip``, ``pay slip``, ``pay stub``,
``earnings statement`` and the gross/net pay labels). Reads labelled lines
("Gross pay 4,250.00"); the lines between a *Deductions* heading and the
total (or the net pay) are the deduction lines, those between *Earnings* and
the gross pay the earnings. The employer is the first line of the first
page unless an ``Employer:`` line names it. Dates: the ``Pay period`` range
and the ``Pay date``, numeric (order from the values, else ``MM/DD``) or
with month names.

Checks: ``gross - total deductions = net`` (and the deduction lines add up
to their total) within the reconciliation tolerance; a payslip that does not
add up has every amount field below the threshold. Payslips are kept on the
document (``extraction.parse.payslip``, ``kind = payslip``, period); they
create no ledger rows (the salary arrives as the bank statement's deposit).
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import ClassVar, Final, Literal

from app.core.money import sum_amounts
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import DocumentParser, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.table_statement import label_end
from app.ingestion.parsers.text import (
    FULL_STYLES,
    DateStyle,
    find_amounts,
    find_dates,
    guess_numeric_style,
    numeric_parts,
)
from app.ingestion.reconcile import TOLERANCE
from app.ingestion.sandbox.protocol import ParserRef, Payslip, PayslipLine

VOCABULARY: Final = ("payslip", "pay slip", "pay stub", "earnings statement", "pay advice")
LABELS: Final = ("gross pay", "net pay", "total deductions", "pay period", "pay date")
_DATE_TOKEN: Final = re.compile(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}")
_CURRENCY: Final = re.compile(r"\bcurrency\s*:?\s*([A-Z]{3})\b", re.IGNORECASE)


def _amount(line: ViewLine, label: str) -> Decimal | None:
    end = label_end(line.text, label)
    if end is None:
        return None
    amounts = find_amounts(line.text[end:])
    return amounts[0].value if amounts else None


class PayslipParser(DocumentParser):
    name: ClassVar[str] = "payslip_basic"
    version: ClassVar[str] = "1"
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
        gross = net = total = None
        period: tuple[date, date] | None = None
        pay_date: date | None = None
        currency: str | None = None
        earnings: list[PayslipLine] = []
        deductions: list[PayslipLine] = []
        section: str | None = None
        for line in view.lines:
            if line.page > 1:
                break
            norm = line.norm
            if employer is None and line.index == 0 and not any(w in norm for w in VOCABULARY):
                employer = line.text.strip()[:200] or None
            end = label_end(line.text, "employer")
            if end is not None and line.text[end:].strip(" :"):
                employer = line.text[end:].strip(" :")[:200]
            match = _CURRENCY.search(line.text)
            if match is not None and currency is None:
                currency = match[1].upper()
            end = label_end(line.text, "pay period")
            if end is not None and period is None:
                found = find_dates(line.text[end:], styles)
                if len(found) >= 2 and found[0] <= found[1]:
                    period = (found[0], found[1])
            end = label_end(line.text, "pay date")
            if end is not None and pay_date is None:
                found = find_dates(line.text[end:], styles)
                pay_date = found[0] if found else None
            for label, slot in (("gross pay", "gross"), ("net pay", "net")):
                value = _amount(line, label)
                if value is not None:
                    if slot == "gross":
                        gross = value
                    else:
                        net = value
                    section = None
            value = _amount(line, "total deductions")
            if value is not None:
                total, section = value, None
                continue
            if norm in ("earnings", "payments"):
                section = "earnings"
                continue
            if norm == "deductions":
                section = "deductions"
                continue
            if section is not None and len(line.cells) >= 2:
                amounts = find_amounts(line.cells[-1].text)
                label = " ".join(c.text for c in line.cells[:-1]).strip()
                if amounts and label:
                    target = earnings if section == "earnings" else deductions
                    target.append(PayslipLine(label=label[:200], amount=abs(amounts[0].value)))
        if total is None and deductions:
            total = sum_amounts(d.amount for d in deductions)
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
        ocr = [c for line in view.lines for c in [line.conf] if c is not None]
        if ocr:
            scores = {k: conf.scaled(v, min(ocr)) for k, v in scores.items()}
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
