# SPDX-License-Identifier: AGPL-3.0-or-later
"""A generic loan and mortgage statement parser: one payment's split and the balances.

Recognised by its vocabulary (``mortgage statement``, ``loan statement``)
or, without it, by at least four loan labels (``principal balance``,
``escrow``, ``interest rate``, ``payment received``, ``next payment
due``...). Reads labelled lines ("Interest 1,276.04"; the amount is the last
one on the line):

* the payment: its date (``Payment received``/``Payment date``/``Date
  received``) and parts: ``Principal``, ``Interest``, ``Escrow`` (taxes and
  insurance), ``Fees``/``Late charges``, and the ``Total payment received``;
* the balances: ``Previous principal balance`` and ``Outstanding``/``New
  principal balance`` (``Principal balance`` alone is the new one);
* ``Interest rate`` (a percentage, stored as a fraction), ``Statement
  date``, ``Next payment due`` (date and amount), the lender (the first line
  unless a ``Lender:`` line names it), the loan number's last four digits
  (``ending in 1234``, ``XXXX1234``; never more) and the currency (a
  ``Currency:`` line or the amounts' symbol).

Checks (SECURITY.md C12, :class:`~app.ingestion.sandbox.protocol.LoanReconciliation`):
``parts`` (principal + interest + escrow + fees = total) and ``balance``
(previous - principal = new) within the reconciliation tolerance. Field
confidence: 1.0 for figures a check confirmed, :data:`conf.GENERIC` for
dates and text read from labels, :data:`conf.UNCHECKED` for figures no check
could cover and :data:`conf.UNRECONCILED` for every figure of a statement
that does not reconcile; OCR lowers all of them. The statement is kept in
``extraction.parse.loan``; ``app.domain.loans.statement_import`` records
the payment on the matching loan.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import ClassVar, Final, Literal

from app.core.money import sum_amounts
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import DocumentParser, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.text import (
    FULL_STYLES,
    DateStyle,
    find_amounts,
    find_dates,
    guess_numeric_style,
    numeric_parts,
)
from app.ingestion.reconcile import TOLERANCE
from app.ingestion.sandbox.protocol import LoanReconciliation, LoanStatement

VOCABULARY: Final = (
    "mortgage statement",
    "loan statement",
    "home loan statement",
    "auto loan statement",
    "loan billing statement",
)
LABELS: Final = (
    "principal balance",
    "escrow",
    "interest rate",
    "payment received",
    "next payment due",
    "total payment received",
    "loan number",
)
MONEY_FIELDS: Final = ("principal", "interest", "escrow", "fees", "total", "previous", "balance")
_DATE_TOKEN: Final = re.compile(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}")
_CURRENCY: Final = re.compile(r"\bcurrency\s*:?\s*([A-Z]{3})\b", re.IGNORECASE)
_PERCENT: Final = re.compile(r"(\d{1,2}(?:\.\d{1,6})?)\s*%")
_LAST4: Final = re.compile(r"(?:ending(?:\s+in)?\s*:?\s*|[xX*•]{2,}[\s-]?)([0-9]{4})\b(?![0-9])")

# (slot, line prefixes, prefixes that rule the line out), checked in order.
_SLOTS: Final[tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...]] = (
    ("previous", ("previous principal balance", "principal balance before payment"), ()),
    (
        "balance",
        (
            "outstanding principal balance",
            "new principal balance",
            "unpaid principal balance",
            "principal balance",
        ),
        (),
    ),
    ("total", ("total payment received", "total received", "total amount received"), ()),
    ("principal", ("principal",), ("principal paid year to date", "principal ytd")),
    ("interest", ("interest",), ("interest rate", "interest paid year to date", "interest ytd")),
    ("escrow", ("escrow",), ("escrow balance",)),
    ("fees", ("fees", "late charges", "late charge", "other charges"), ()),
)


def _starts(norm: str, prefixes: tuple[str, ...]) -> bool:
    return any(norm == p or norm.startswith(f"{p} ") for p in prefixes)


def _last_amount(line: ViewLine) -> tuple[Decimal | None, str | None]:
    amounts = find_amounts(line.text)
    if not amounts:
        return None, None
    found = amounts[-1]
    return abs(found.value), found.currency


class LoanStatementParser(DocumentParser):
    name: ClassVar[str] = "loan_statement_generic"
    version: ClassVar[str] = "1"
    kind: ClassVar[Literal["statement", "payslip", "receipt"]] = "statement"

    def detect(self, view: DocumentView) -> float:
        head = f" {view.head_text()} "
        vocabulary = any(f" {word} " in head for word in VOCABULARY)
        labels = sum(1 for label in LABELS if f" {label} " in head)
        if not vocabulary and labels < 4:
            return 0.0
        return round(min(1.0, (0.6 if vocabulary else 0.2) + 0.1 * labels), 4)

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
        money: dict[str, Decimal] = {}
        #: Currencies of the amounts' symbols ($ is USD).
        symbols: set[str] = set()
        dates: dict[str, date] = {}
        lender: str | None = None
        last4: str | None = None
        currency: str | None = None
        rate: Decimal | None = None
        next_due_amount: Decimal | None = None
        for line in view.lines:
            norm = line.norm
            text = line.text
            if lender is None and line.index == 0 and not any(w in norm for w in VOCABULARY):
                lender = text.strip()[:200] or None
            if norm.startswith("lender "):
                lender = text.split(":", 1)[-1].strip()[:200] or lender
            match = _CURRENCY.search(text)
            if match is not None and currency is None:
                currency = match[1].upper()
            if last4 is None and ("loan number" in norm or "account number" in norm):
                found = _LAST4.search(text)
                if found is not None:
                    last4 = found[1]
            if "interest rate" in norm and rate is None:
                percent = _PERCENT.search(text)
                if percent is not None:
                    rate = Decimal(percent[1]) / 100
                continue
            for key, labels in (
                ("statement_date", ("statement date",)),
                ("paid_on", ("payment received on", "payment received", "payment date")),
                ("paid_on", ("date received",)),
                ("next_due", ("next payment due", "payment due date")),
            ):
                for label in labels:
                    at = norm.find(label)
                    if at < 0 or key in dates:
                        continue
                    found_dates = find_dates(text[text.lower().find(label.split()[0]) :], styles)
                    if found_dates:
                        dates[key] = found_dates[0]
            if "amount due" in norm and next_due_amount is None:
                value, symbol = _last_amount(line)
                if value is not None:
                    next_due_amount = value
                    if symbol:
                        symbols.add(symbol)
                continue
            filled = next(
                (
                    name
                    for name, prefixes, excluded in _SLOTS
                    if _starts(norm, prefixes) and not _starts(norm, excluded)
                ),
                None,
            )
            if filled is None or filled in money:
                continue
            value, symbol = _last_amount(line)
            if value is not None:
                money[filled] = value
                if symbol:
                    symbols.add(symbol)
        outcome = ParseOutcome()
        if "principal" not in money and "interest" not in money:
            outcome.warnings.append("no_loan_figures")
            return outcome
        if currency is None and len(symbols) == 1:
            currency = symbols.pop()
        reconciliation = reconcile_loan(money)
        scores = _scores(money, dates, reconciliation, lender=lender, last4=last4, rate=rate)
        ocr = [c for line in view.lines for c in [line.conf] if c is not None]
        if ocr:
            scores = {k: conf.scaled(v, min(ocr)) for k, v in scores.items()}
        outcome.loan = LoanStatement(
            lender=lender,
            last4=last4,
            currency=currency,
            statement_date=dates.get("statement_date"),
            paid_on=dates.get("paid_on"),
            principal=money.get("principal"),
            interest=money.get("interest"),
            escrow=money.get("escrow"),
            fees=money.get("fees"),
            total=money.get("total"),
            previous_balance=money.get("previous"),
            balance=money.get("balance"),
            rate=rate,
            next_due_date=dates.get("next_due"),
            next_amount_due=next_due_amount,
            reconciliation=reconciliation,
            confidence=scores,
            low_confidence=conf.low_fields(scores, threshold),
        )
        return outcome


def reconcile_loan(money: dict[str, Decimal]) -> LoanReconciliation:
    """The ``parts`` and ``balance`` checks over what the statement printed."""
    checks: list[str] = []
    failed: list[str] = []
    principal = money.get("principal")
    if principal is not None and "interest" in money and "total" in money:
        checks.append("parts")
        parts = sum_amounts(
            money.get(k, Decimal(0)) for k in ("principal", "interest", "escrow", "fees")
        )
        if abs(parts - money["total"]) > TOLERANCE:
            failed.append("parts")
    if principal is not None and "previous" in money and "balance" in money:
        checks.append("balance")
        if abs(money["previous"] - principal - money["balance"]) > TOLERANCE:
            failed.append("balance")
    status: Literal["ok", "mismatch", "unavailable"] = (
        "unavailable" if not checks else ("mismatch" if failed else "ok")
    )
    return LoanReconciliation(status=status, checks=checks, failed=failed)


def _scores(
    money: dict[str, Decimal],
    dates: dict[str, date],
    reconciliation: LoanReconciliation,
    *,
    lender: str | None,
    last4: str | None,
    rate: Decimal | None,
) -> dict[str, float]:
    covered = {
        "parts": {"principal", "interest", "escrow", "fees", "total"},
        "balance": {"principal", "previous", "balance"},
    }
    confirmed = {f for check in reconciliation.checks for f in covered[check]}
    scores: dict[str, float] = {}
    for name in MONEY_FIELDS:
        if name not in money:
            if name in ("principal", "interest"):
                scores[name] = conf.MISSING
            continue
        if reconciliation.status == "mismatch":
            scores[name] = conf.UNRECONCILED
        elif name in confirmed:
            scores[name] = 1.0
        else:
            scores[name] = conf.UNCHECKED
    scores["paid_on"] = conf.GENERIC if "paid_on" in dates else conf.MISSING
    if "statement_date" in dates:
        scores["statement_date"] = conf.GENERIC
    if rate is not None:
        scores["rate"] = conf.GENERIC
    if lender:
        scores["lender"] = conf.GENERIC
    if last4:
        scores["last4"] = conf.GENERIC
    return scores
