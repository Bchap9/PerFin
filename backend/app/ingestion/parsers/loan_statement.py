# SPDX-License-Identifier: AGPL-3.0-or-later
"""A generic loan and mortgage statement parser: one payment's split and the balances.

Recognised by its vocabulary on page 1 (``mortgage statement``, ``loan
statement``) or, without it (a loan bill headed ``Account Statement``), by
at least four loan labels (:data:`LABELS`: ``principal balance``,
``escrow``, ``interest rate``, ``payment received``, ``payment
allocation``...) of which one is about the principal
(:data:`PRINCIPAL_LABELS`): a card statement prints a payment due date, a
payment received, an amount past due and an interest rate too, and no
principal. Labels are counted over all pages, each once; a label printed
only inside a longer one (``principal balance`` in ``current principal
balance``) is not counted beside it.

A bill that lists several loans in one table
(:func:`~app.ingestion.parsers.loan_periodic.lists_several_loans`) is not
one loan's statement: nothing is read and the warning is
``loan_bill_several_loans``. Nor is a document of more than
:data:`MAX_LOAN_PAGES` pages (a servicing guide, a report with sample
statements in it): the labels are read over all pages, so a sample that
adds up would be recorded as a payment on a loan whose digits it happens
to print, or that the file was uploaded to. Nothing is read and the
warning is ``loan_document_too_long``.

Two layouts, each with its own reader:

* **The payment receipt** (:func:`_read_lines`): labelled lines ("Interest
  1,276.04"; the amount is the last one on the line):

  * the payment: its date (``Payment received``/``Payment date``/``Date
    received``) and parts: ``Principal``, ``Interest``, ``Escrow`` (taxes
    and insurance), ``Fees``/``Late charges``, and the ``Total payment
    received``;
  * the balances: ``Previous principal balance`` and ``Outstanding``/``New
    principal balance`` (``Principal balance`` alone is the new one);
  * ``Statement date``, ``Next payment due`` (date and amount).

* **The periodic statement**
  (:func:`~app.ingestion.parsers.loan_periodic.is_periodic`): the monthly
  statement that prints the next payment's split, last month's and the
  year's so far. It is read by section and by column
  (:mod:`app.ingestion.parsers.loan_periodic`): the payment is last month's
  column, dated by its row of the activity table; a year-to-date figure,
  the next payment's split and the due date are never the payment. A loan
  bill with a ``Payment Allocation`` block is read by the same reader: the
  block is the payment, dated by its ``date | Payment received | amount``
  row.

Both read ``Interest rate`` (a percentage, stored as a fraction; the tail of
a number OCR split, ``4, 75%``, is no rate), the lender (a ``Lender:`` line,
with its colon, else the first line), the loan number's last four digits
(``ending in 1234``, ``XXXX1234``, or the end of a number of six or more
digits printed in full after its label; never more than four are kept) and
the currency (a ``Currency:`` line or the amounts' symbol).

Checks (SECURITY.md C12, :class:`~app.ingestion.sandbox.protocol.LoanReconciliation`),
each within the reconciliation tolerance: ``parts`` (principal + interest +
escrow + fees = total), ``balance`` (previous - principal = new) and, on a
periodic statement, ``amount_due`` (the next payment's principal + interest
+ escrow = the regular payment, and with the fees = the total amount due,
whichever are printed; the next payment itself is not kept). Field
confidence: 1.0 for figures a check confirmed, :data:`conf.GENERIC` for
dates and text read from labels, :data:`conf.UNCHECKED` for figures no check
could cover and :data:`conf.UNRECONCILED` for every figure of a statement
that does not reconcile. A periodic statement prints its principal balance
once, beside its own label, and nothing to check it against: when the
statement reconciles, it scores :data:`conf.GENERIC`. A statement that
reports no payment in its period has no day paid, and none is scored. On a
scan each field is scaled by the OCR confidence of the cells it was read
from; a figure a check covers on a statement that reconciles keeps full
confidence. The statement is kept in ``extraction.parse.loan``;
``app.domain.loans.statement_import`` records the payment on the matching
loan.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import ClassVar, Final, Literal

from app.core.money import sum_amounts
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import (
    DocumentParser,
    DocumentView,
    ParseOutcome,
    ViewLine,
)
from app.ingestion.parsers.currency import stated_currency
from app.ingestion.parsers.loan_periodic import (
    LoanRead,
    is_periodic,
    lists_several_loans,
    named_lender,
    read_last4,
    read_periodic,
    read_rate,
)
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

#: A loan statement is a few pages (the statement, its coupon, an escrow
#: page); a longer document is about statements, whatever it prints.
MAX_LOAN_PAGES: Final = 6
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
    "outstanding principal",
    "current principal balance",
    "unpaid principal",
    "principal paid",
    "interest paid",
    "payoff",
    "payment due date",
    "payment allocation",
    "past payments breakdown",
    "explanation of amount due",
    "date last payment received",
    "loan seq",
    "int rate",
    "amount past due",
)
#: Labels only a loan prints. Without the vocabulary one of them must be there.
PRINCIPAL_LABELS: Final = (
    "principal balance",
    "outstanding principal",
    "current principal balance",
    "unpaid principal",
    "principal paid",
    "loan seq",
    "escrow",
)
#: Per label, the longer labels it is part of and where it starts in each.
_INSIDE: Final[dict[str, tuple[tuple[str, int], ...]]] = {
    label: tuple(
        (longer, at)
        for longer in LABELS
        if longer != label and (at := f" {longer} ".find(f" {label} ")) >= 0
    )
    for label in LABELS
}
MONEY_FIELDS: Final = ("principal", "interest", "escrow", "fees", "total", "previous", "balance")
_DATE_TOKEN: Final = re.compile(r"\d{1,4}[/.-]\d{1,2}[/.-]\d{2,4}")
#: Labels a loan number follows (normalised).
_NUMBER_LABELS: Final = ("loan number", "loan no", "account number", "account no")

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


def labels_in(text: str) -> set[str]:
    """The :data:`LABELS` printed in ``text`` (normalised), each once.

    An occurrence inside a longer label printed there (``payment received``
    in ``total payment received``) is that longer label's, not its own.
    """
    padded = f" {text} "
    found: set[str] = set()
    for label in LABELS:
        needle = f" {label} "
        start = padded.find(needle)
        while start >= 0:
            if not any(
                start - at >= 0 and padded.startswith(f" {longer} ", start - at)
                for longer, at in _INSIDE[label]
            ):
                found.add(label)
                break
            start = padded.find(needle, start + 1)
    return found


def _last_amount(line: ViewLine) -> tuple[Decimal | None, str | None]:
    amounts = find_amounts(line.text)
    if not amounts:
        return None, None
    found = amounts[-1]
    return abs(found.value), found.currency


class LoanStatementParser(DocumentParser):
    name: ClassVar[str] = "loan_statement_generic"
    version: ClassVar[str] = "2"
    kind: ClassVar[Literal["statement", "payslip", "receipt"]] = "statement"

    def detect(self, view: DocumentView) -> float:
        head = f" {view.head_text()} "
        vocabulary = any(f" {word} " in head for word in VOCABULARY)
        found = labels_in(" ".join(line.norm for line in view.lines))
        labels = len(found)
        if not vocabulary and (labels < 4 or found.isdisjoint(PRINCIPAL_LABELS)):
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
        if view.page_count > MAX_LOAN_PAGES:
            # A guide or a report about loan statements: its sample is nobody's payment.
            return ParseOutcome(warnings=["loan_document_too_long"])
        if lists_several_loans(view):
            # One account with several loans is not one loan's statement.
            return ParseOutcome(warnings=["loan_bill_several_loans"])
        styles = self._styles(view)
        periodic = is_periodic(view)
        read = read_periodic(view, styles) if periodic else _read_lines(view, styles)
        money, dates, cells = read.money, read.dates, read.cells
        lender = read.lender
        first = view.lines[0] if view.lines else None
        if lender is None and first is not None and not any(w in first.norm for w in VOCABULARY):
            lender = first.text.strip()[:200] or None
            cells["lender"] = list(first.cells)
        currency = next(
            (code for line in view.lines if (code := stated_currency(line.text)) is not None),
            None,
        )
        outcome = ParseOutcome()
        outcome.warnings.extend(read.warnings)
        if "principal" not in money and "interest" not in money:
            outcome.warnings.append("no_loan_figures")
            return outcome
        if currency is None and len(read.symbols) == 1:
            currency = next(iter(read.symbols))
        reconciliation = reconcile_loan(money, read.due)
        scores = _scores(
            money,
            dates,
            reconciliation,
            lender=lender,
            last4=read.last4,
            rate=read.rate,
            periodic=periodic,
            no_payment="loan_no_payment_in_period" in read.warnings,
        )
        scores = conf.scaled_fields(
            scores,
            {name: [c.conf for c in found] for name, found in cells.items()},
            confirmed=_confirmed(reconciliation) if reconciliation.status == "ok" else (),
        )
        outcome.loan = LoanStatement(
            lender=lender,
            last4=read.last4,
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
            rate=read.rate,
            next_due_date=dates.get("next_due"),
            next_amount_due=read.next_due_amount,
            reconciliation=reconciliation,
            confidence=scores,
            low_confidence=conf.low_fields(scores, threshold),
        )
        return outcome


def _read_lines(view: DocumentView, styles: tuple[DateStyle, ...]) -> LoanRead:
    """The payment-receipt layout: labelled lines, the last amount on each (module doc)."""
    read = LoanRead()
    money, dates, cells, symbols = read.money, read.dates, read.cells, read.symbols
    for line in view.lines:
        norm = line.norm
        text = line.text
        named = named_lender(text)
        if named is not None:
            read.lender = named
            cells["lender"] = list(line.cells)
        if read.last4 is None and any(label in norm for label in _NUMBER_LABELS):
            last4 = read_last4(text)
            if last4 is not None:
                read.last4 = last4
                cells["last4"] = list(line.cells)
        if "interest rate" in norm and read.rate is None:
            rate = read_rate(text)
            if rate is not None:
                read.rate = rate
                cells["rate"] = list(line.cells)
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
                    cells[key] = list(line.cells)
        if "amount due" in norm and read.next_due_amount is None:
            value, symbol = _last_amount(line)
            if value is not None:
                read.next_due_amount = value
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
            cells[filled] = list(line.cells)
            if symbol:
                symbols.add(symbol)
    return read


def reconcile_loan(
    money: dict[str, Decimal], due: Mapping[str, Decimal] | None = None
) -> LoanReconciliation:
    """The checks over what the statement printed (module doc).

    ``parts`` and ``balance`` on the payment made (``money``); ``amount_due``
    on the next payment (``due``, a periodic statement's): its principal,
    interest and escrow are the regular payment, and with the fees the total
    amount due, whichever of the two are printed.
    """
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
    if due and "principal" in due and "interest" in due and ("regular" in due or "total" in due):
        checks.append("amount_due")
        regular = sum_amounts(due.get(k, Decimal(0)) for k in ("principal", "interest", "escrow"))
        total = regular + due.get("fees", Decimal(0))
        if ("regular" in due and abs(regular - due["regular"]) > TOLERANCE) or (
            "total" in due and abs(total - due["total"]) > TOLERANCE
        ):
            failed.append("amount_due")
    status: Literal["ok", "mismatch", "unavailable"] = (
        "unavailable" if not checks else ("mismatch" if failed else "ok")
    )
    return LoanReconciliation(status=status, checks=checks, failed=failed)


#: The money fields each check covers (``amount_due`` checks the next
#: payment, which is not kept: it covers none).
_COVERED: Final[dict[str, frozenset[str]]] = {
    "parts": frozenset({"principal", "interest", "escrow", "fees", "total"}),
    "balance": frozenset({"principal", "previous", "balance"}),
    "amount_due": frozenset(),
}
#: Figures a periodic statement prints once, beside their own label, that no
#: check can cover.
_LABELLED: Final = frozenset({"previous", "balance"})


def _confirmed(reconciliation: LoanReconciliation) -> set[str]:
    """The money fields the statement's checks cover."""
    return {f for check in reconciliation.checks for f in _COVERED[check]}


def _scores(
    money: dict[str, Decimal],
    dates: dict[str, date],
    reconciliation: LoanReconciliation,
    *,
    lender: str | None,
    last4: str | None,
    rate: Decimal | None,
    periodic: bool = False,
    no_payment: bool = False,
) -> dict[str, float]:
    confirmed = _confirmed(reconciliation)
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
        elif periodic and reconciliation.status == "ok" and name in _LABELLED:
            scores[name] = conf.GENERIC
        else:
            scores[name] = conf.UNCHECKED
    if not no_payment:
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
