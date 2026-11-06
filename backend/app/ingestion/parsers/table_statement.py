# SPDX-License-Identifier: AGPL-3.0-or-later
"""The generic table statement parser for bank and card statements (PDF, scans).

Works on the words and boxes the extractor stored (``Page.words``/``lines``;
text layer or OCR), so ruled tables, aligned columns and multi-page tables
are read the same way:

1. **Header.** A line whose cells name a date, a description and an amount
   (one signed *Amount* column, or *Debit*/*Credit* style columns), with an
   optional running *Balance* and posting date, starts (or, on a later page,
   continues) the table. The header cells' horizontal spans are the columns.
2. **Rows.** Every later line is split into cells; each cell goes to the
   column it overlaps most. A line with a date in the date column and an
   amount is a transaction; a dated line whose description is an opening or
   closing label is the table's own balance row; a following line with only
   description text continues the previous row's memo (wrapped text).
   Section headings set the row type (fees, interest) or start a *pending*
   section whose rows are counted but not imported (they are not posted and
   not part of the balances). Totals, page footers and "continued" lines are
   skipped. A dated line without a readable amount is counted as skipped.
3. **Summary.** Outside the rows: the statement period (``Statement period``,
   ``Billing period``...), the account's last four digits (a masked number
   or "ending in"), the currency, and the opening/closing balances by label.
   Every labelled line is a period candidate (a label alone on its line
   offers the next line), tried in order until one reads, so a label that
   matches something else first ("Change from last period") blocks
   nothing; a range's start borrows the end's year (``Jan 1 - Jan 31,
   2026``). Without a labelled period, the **header band**
   (:func:`app.ingestion.classify.dates.header_band_range`: a range printed
   in the top lines of at least half the pages) is the period
   (:func:`read_period`). Yearless row dates (``MM/DD``) take their year
   from the period.
4. **Signs.** ``bank``: a signed amount as printed, *Debit* negative and
   *Credit* positive, ``CR``/``DR`` markers honoured. ``card``: charges are
   printed positive and credits negative or with ``CR``; both the amounts and
   the printed balances (amounts owed) are turned into the ledger convention
   (a purchase is negative, the balance owed is negative).
5. **Checks and confidence.** The running balance and the totals are
   reconciled (:mod:`app.ingestion.reconcile`) and every field is scored
   (:mod:`app.ingestion.parsers.confidence`).

Institution parsers (:mod:`app.ingestion.parsers.institutions`) subclass it
and pin what the generic one has to guess: date format, sign convention,
currency, fingerprint phrases and the institution's name.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from functools import lru_cache
from typing import ClassVar, Final, Literal

from app.ingestion.classify.dates import HEADER_BAND_LINES as BAND_LINES
from app.ingestion.classify.dates import DateOrder, find_ranges, header_band_range
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.base import Cell, DocumentParser, DocumentView, ParseOutcome, ViewLine
from app.ingestion.parsers.text import (
    FULL_STYLES,
    MAX_LINE,
    NUMERIC_DM,
    Amount,
    DateStyle,
    find_amounts,
    find_dates,
    guess_numeric_style,
    normalise,
    numeric_parts,
    read_amount,
    read_date,
)
from app.ingestion.reconcile import Row, reconcile
from app.ingestion.sandbox.protocol import (
    Balance,
    ExtractedTransaction,
    ParserRef,
    Statement,
    StatementAccount,
)

Role = Literal["date", "post_date", "description", "amount", "debit", "credit", "balance"]
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
}

_LAST4: Final = re.compile(
    r"(?<![A-Za-z0-9])[*xX•]{2}[*xX•\s-]{0,30}?(\d{4})\b"
    r"|\bending(?:\s+in)?\s*:?\s*(\d{4})\b"
    r"|\baccount(?:\s+(?:number|no\.?))?\s*:?\s*\d{0,26}?(\d{4})\b",
    re.IGNORECASE,
)
_CURRENCY: Final = re.compile(r"\bcurrency\s*:?\s*([A-Z]{3})\b", re.IGNORECASE)
#: Line separators between the two dates of a period.
_RANGE: Final = re.compile(r"\s(?:to|through|until|-|\u2013)\s", re.IGNORECASE)


@lru_cache(maxsize=256)
def _label_pattern(label: str) -> re.Pattern[str]:
    words = (re.escape(word) for word in label.split())
    return re.compile(r"\b" + r"[^\w]{1,3}".join(words) + r"\b", re.IGNORECASE)


def label_end(text: str, label: str) -> int | None:
    """Where ``label`` (words, any punctuation between them) ends in ``text``."""
    match = _label_pattern(label).search(text[:MAX_LINE])
    return match.end() if match else None


#: Period candidates kept per statement (labelled lines, in order).
MAX_PERIOD_CANDIDATES: Final = 20


@dataclass
class PeriodCandidates:
    """Lines a period label was found on (the text after the label), in order.

    A label with nothing after it on its line offers the next line instead
    (``Statement period`` above ``01/01/2026 - 01/31/2026``).
    """

    texts: list[str] = field(default_factory=list)
    next_line: bool = False

    def scan(self, text: str, labels: Sequence[str]) -> None:
        if self.next_line:
            self.next_line = False
            self.add(text)
        for label in labels:
            end = label_end(text, label)
            if end is None:
                continue
            rest = text[end:]
            if any(ch.isdigit() for ch in rest):
                self.add(rest)
            else:
                self.next_line = True
            break

    def add(self, text: str) -> None:
        if len(self.texts) < MAX_PERIOD_CANDIDATES:
            self.texts.append(text)


def page_heads(view: DocumentView, lines: int = BAND_LINES) -> list[list[str]]:
    """The top ``lines`` lines of each page (the header band)."""
    heads: dict[int, list[str]] = {}
    for line in view.lines:
        head = heads.setdefault(line.page, [])
        if len(head) < lines:
            head.append(line.text)
    return [heads[page] for page in sorted(heads)]


def full_style(style: DateStyle) -> DateStyle:
    """The numeric style with a year a yearless one (``md``/``dm``) stands for."""
    if style == "md":
        return "mdy"
    if style == "dm":
        return "dmy"
    return style


def period_order(styles: Iterable[DateStyle]) -> DateOrder | None:
    """The day/month order the first numeric style of ``styles`` stands for."""
    for style in styles:
        if style in ("mdy", "md"):
            return "mdy"
        if style in ("dmy", "dm"):
            return "dmy"
    return None


def read_period(
    candidates: Sequence[str], ordered: Sequence[DateStyle], view: DocumentView
) -> tuple[date, date] | None:
    """The first candidate that reads as a period, else the header band (module doc)."""
    order = period_order(ordered)
    for text in candidates:
        parts = _RANGE.split(text, maxsplit=1)
        if len(parts) == 2:
            start = find_dates(parts[0], ordered)
            end = find_dates(parts[1], ordered)
            if start and end and start[-1] <= end[0]:
                return start[-1], end[0]
        found = find_dates(text, ordered)
        if len(found) >= 2 and found[0] <= found[1]:
            return found[0], found[1]
        ranges = find_ranges(text, order=order)
        if ranges:
            return ranges[0].start, ranges[0].end
    band = header_band_range(page_heads(view), order=order)
    return (band.start, band.end) if band is not None else None


@dataclass(frozen=True)
class Column:
    role: Role
    x0: float
    x1: float


@dataclass
class _Row:
    page: int
    date_text: str
    description: list[str]
    amount: Amount | None
    debit: Amount | None
    credit: Amount | None
    balance: Amount | None
    ocr: float | None
    bbox: tuple[float, float, float, float]
    section_type: str | None
    memo: list[str] = field(default_factory=list)


@dataclass
class _Summary:
    opening: Amount | None = None
    closing: Amount | None = None
    opening_in_table: bool = False
    closing_in_table: bool = False
    period: PeriodCandidates = field(default_factory=PeriodCandidates)
    last4: str | None = None
    currency: str | None = None


def _min(*values: float | None) -> float | None:
    present = [v for v in values if v is not None]
    return min(present) if present else None


class TableStatementParser(DocumentParser):
    """Generic: any statement with a recognisable transaction table (module doc)."""

    name: ClassVar[str] = "statement_generic"
    version: ClassVar[str] = "1"
    kind: ClassVar[Literal["statement", "payslip"]] = "statement"
    #: The institution's name as accounts know it (account matching).
    display_name: ClassVar[str | None] = None
    #: Normalised phrases on the first page that identify the institution.
    fingerprints: ClassVar[tuple[str, ...]] = ()
    account_type: ClassVar[str | None] = None
    default_currency: ClassVar[str | None] = None
    #: Date formats of the rows; empty: guessed from the values.
    date_styles: ClassVar[tuple[DateStyle, ...]] = ()
    period_styles: ClassVar[tuple[DateStyle, ...]] = FULL_STYLES
    sign: ClassVar[Literal["bank", "card"]] = "bank"
    #: Confidence of a value read in the parser's declared format.
    base: ClassVar[float] = conf.GENERIC
    headers: ClassVar[Mapping[str, Role]] = HEADERS
    opening_labels: ClassVar[tuple[str, ...]] = (
        "beginning balance",
        "opening balance",
        "previous balance",
        "starting balance",
        "balance brought forward",
        "balance forward",
    )
    closing_labels: ClassVar[tuple[str, ...]] = (
        "ending balance",
        "closing balance",
        "new balance",
        "statement balance",
        "balance carried forward",
    )
    period_labels: ClassVar[tuple[str, ...]] = (
        "statement period",
        "billing period",
        "statement dates",
        "for the period",
        "statement from",
        "period",
    )
    pending_markers: ClassVar[tuple[str, ...]] = ("pending",)
    #: Section headings (normalised prefix) -> transaction type (None: plain).
    sections: ClassVar[Mapping[str, str | None]] = {
        "fees charged": "fee",
        "fees": "fee",
        "interest charged": "int",
        "interest": "int",
        "purchases": None,
        "payments and other credits": None,
        "payments and credits": None,
        "deposits and other credits": None,
        "withdrawals and other debits": None,
        "transactions": None,
    }
    skip_prefixes: ClassVar[tuple[str, ...]] = ("total", "subtotal", "page ", "continued")
    #: Words in a description that type a row when its section does not.
    type_keywords: ClassVar[tuple[tuple[str, str], ...]] = (
        ("fee", "fee"),
        ("interest", "int"),
        ("dividend", "div"),
    )

    # -- detection --------------------------------------------------------------

    def columns(self, line: ViewLine) -> list[Column] | None:
        """The table columns ``line`` declares, or None when it is no header."""
        found: list[Column] = []
        roles: set[Role] = set()
        for cell in line.cells:
            role = self.headers.get(normalise(cell.text))
            if role is None or role in roles:
                continue
            roles.add(role)
            found.append(Column(role, cell.x0, cell.x1))
        if not {"date", "description"} <= roles:
            return None
        if not roles & {"amount", "debit", "credit"}:
            return None
        return sorted(found, key=lambda c: c.x0)

    def has_table(self, view: DocumentView) -> bool:
        return any(self.columns(line) is not None for line in view.lines)

    def fingerprint_score(self, view: DocumentView) -> float:
        if not self.fingerprints:
            return 0.0
        head = f" {view.head_text()} "
        hits = sum(1 for phrase in self.fingerprints if f" {normalise(phrase)} " in head)
        return hits / len(self.fingerprints)

    def detect(self, view: DocumentView) -> float:
        table = self.has_table(view)
        if not self.fingerprints:
            return 0.4 if table else 0.0
        share = self.fingerprint_score(view)
        if share == 0:
            return 0.0
        score = 0.5 + 0.4 * share
        return round(score + 0.1 if table else score * 0.6, 4)

    # -- reading ----------------------------------------------------------------

    def _assign(self, line: ViewLine, columns: Sequence[Column]) -> dict[Role, list[Cell]]:
        out: dict[Role, list[Cell]] = {}
        for cell in line.cells:
            best: Column | None = None
            best_overlap = 0.0
            for column in columns:
                overlap = cell.overlap(column.x0, column.x1)
                if overlap > best_overlap:
                    best, best_overlap = column, overlap
            if best is None:
                nearest = min(
                    columns, key=lambda c: abs((c.x0 + c.x1) / 2 - cell.centre), default=None
                )
                if nearest is not None and abs((nearest.x0 + nearest.x1) / 2 - cell.centre) < max(
                    3 * line.height, 30.0
                ):
                    best = nearest
            if best is not None:
                out.setdefault(best.role, []).append(cell)
        return out

    def _date_shaped(self, text: str) -> bool:
        if numeric_parts(text) is not None:
            return True
        return any(read_date(text, style) is not None for style in ("d_mon_y", "mon_d_y", "ymd"))

    def _label_amount(self, line: ViewLine, labels: Sequence[str]) -> Amount | None:
        for label in labels:
            end = label_end(line.text, label)
            if end is None:
                continue
            amounts = find_amounts(line.text[end:])
            if amounts:
                return amounts[0]
        return None

    def _scan_summary(self, line: ViewLine, summary: _Summary) -> None:
        if summary.opening is None:
            summary.opening = self._label_amount(line, self.opening_labels)
        closing = self._label_amount(line, self.closing_labels)
        if closing is not None and summary.closing is None:
            summary.closing = closing
        summary.period.scan(line.text, self.period_labels)
        if summary.last4 is None:
            match = _LAST4.search(line.text)
            if match is not None:
                summary.last4 = next(g for g in match.groups() if g)
        if summary.currency is None:
            match = _CURRENCY.search(line.text)
            if match is not None:
                summary.currency = match[1].upper()

    def _section(self, norm: str) -> tuple[bool, str | None] | None:
        """``(pending, type)`` when ``norm`` is a section heading."""
        for marker in self.pending_markers:
            if norm.startswith(marker):
                return True, None
        for heading, kind in self.sections.items():
            if norm == heading or norm.startswith(f"{heading} "):
                return False, kind
        return None

    def _collect(self, view: DocumentView) -> tuple[list[_Row], _Summary, int, int, bool]:
        rows: list[_Row] = []
        summary = _Summary()
        pending_items = skipped = 0
        columns: list[Column] | None = None
        pending = False
        section_type: str | None = None
        last: _Row | None = None
        found_header = False
        for line in view.lines:
            header = self.columns(line)
            if header is not None:
                columns, pending, section_type, last = header, False, None, None
                found_header = True
                continue
            norm = line.norm
            if columns is None:
                self._scan_summary(line, summary)
                continue
            cells = self._assign(line, columns)
            date_text = " ".join(c.text for c in cells.get("date", []))
            if not date_text or not self._date_shaped(date_text):
                last = self._not_a_row(line, norm, cells, summary, last)
                section = self._section(norm) if last is None else None
                if section is not None:
                    pending, section_type = section
                continue
            description = [c.text for c in cells.get("description", [])]
            desc_norm = normalise(" ".join(description))
            balance = self._amount(cells.get("balance"))
            amount = self._amount(cells.get("amount"))
            debit = self._amount(cells.get("debit"))
            credit = self._amount(cells.get("credit"))
            if amount is None and debit is None and credit is None:
                if any(desc_norm.startswith(lbl) for lbl in self.opening_labels):
                    if balance is not None and summary.opening is None:
                        summary.opening, summary.opening_in_table = balance, True
                    continue
                if any(desc_norm.startswith(lbl) for lbl in self.closing_labels):
                    if balance is not None:
                        summary.closing, summary.closing_in_table = balance, True
                    continue
                if pending:
                    continue
                skipped += 1
                last = None
                continue
            if pending:
                pending_items += 1
                last = None
                continue
            used = [c for role_cells in cells.values() for c in role_cells]
            last = _Row(
                page=line.page,
                date_text=date_text,
                description=description,
                amount=amount,
                debit=debit,
                credit=credit,
                balance=balance,
                ocr=_min(*(c.conf for c in used)),
                bbox=line.bbox,
                section_type=section_type,
            )
            rows.append(last)
        return rows, summary, pending_items, skipped, found_header

    def _not_a_row(
        self,
        line: ViewLine,
        norm: str,
        cells: dict[Role, list[Cell]],
        summary: _Summary,
        last: _Row | None,
    ) -> _Row | None:
        """Handle an undated line in the table; return the row a wrap may extend."""
        if any(norm.startswith(prefix.strip()) for prefix in self.skip_prefixes):
            return None
        only_text = set(cells) <= {"description"} and "description" in cells
        if (
            last is not None
            and only_text
            and line.page == last.page
            and line.bbox[1] - last.bbox[3] < 1.5 * line.height
            and self._section(norm) is None
            and not find_amounts(line.text)
        ):
            last.memo.extend(c.text for c in cells["description"])
            last.bbox = (
                min(last.bbox[0], line.bbox[0]),
                last.bbox[1],
                max(last.bbox[2], line.bbox[2]),
                line.bbox[3],
            )
            return last
        self._scan_summary(line, summary)
        return None

    @staticmethod
    def _amount(cells: list[Cell] | None) -> Amount | None:
        if not cells:
            return None
        return read_amount(" ".join(c.text for c in cells))

    def _signed(self, row: _Row) -> Decimal | None:
        if row.amount is not None:
            value = row.amount
            if value.credit is True:
                return abs(value.value)
            if value.credit is False:
                return -abs(value.value)
            if self.sign == "card":
                return abs(value.value) if value.negative_marked else -abs(value.value)
            return value.value
        if row.debit is not None and row.credit is None:
            return -abs(row.debit.value)
        if row.credit is not None and row.debit is None:
            return abs(row.credit.value)
        return None

    def _ledger_balance(self, amount: Amount | None) -> Decimal | None:
        if amount is None:
            return None
        value = amount.value
        if amount.credit is not None:
            # "CR": the institution owes the holder (positive in the ledger);
            # "DR": the holder owes (an overdraft, a card balance).
            return abs(value) if amount.credit else -abs(value)
        return -value if self.sign == "card" else value

    # -- dates ------------------------------------------------------------------

    def _row_styles(
        self, rows: Sequence[_Row], hint: str | None = None
    ) -> tuple[tuple[DateStyle, ...], bool]:
        if self.date_styles:
            return self.date_styles, True
        texts = [r.date_text for r in rows]
        numeric = [t for t in texts if numeric_parts(t) is not None]
        if numeric:
            style, certain = guess_numeric_style(numeric)
            if hint in NUMERIC_DM and (not certain or style == hint):
                # The classifier's fingerprint knows the institution's order.
                style, certain = hint, True
            yearless = all((numeric_parts(t) or (0, 0, 0))[2] is None for t in numeric)
            if style is None:  # pragma: no cover - numeric values always give a style
                style = "mdy"
            if yearless:
                style = "md" if style == "mdy" else "dm"
            return (style, "ymd", "d_mon_y", "mon_d_y"), certain
        return ("d_mon_y", "mon_d_y", "ymd"), True

    def _period(
        self, summary: _Summary, styles: tuple[DateStyle, ...], view: DocumentView
    ) -> tuple[date, date] | None:
        ordered: list[DateStyle] = [s for s in self.period_styles if s in FULL_STYLES]
        for style in styles:
            numeric = full_style(style)
            if numeric in NUMERIC_DM and numeric in ordered:
                ordered.remove(numeric)
                ordered.insert(0, numeric)
        return read_period(summary.period.texts, ordered, view)

    # -- the statement ----------------------------------------------------------

    def _type(self, row: _Row, description: str) -> str | None:
        if row.section_type:
            return row.section_type
        words = set(normalise(description).split())
        for keyword, kind in self.type_keywords:
            if keyword in words:
                return kind
        return None

    def parse(
        self, view: DocumentView, *, threshold: float, date_order: str | None = None
    ) -> ParseOutcome:
        rows, summary, pending_items, skipped, found_header = self._collect(view)
        outcome = ParseOutcome(pending_items=pending_items, skipped_lines=skipped)
        if not found_header:
            outcome.warnings.append("no_table_header")
            return outcome
        styles, certain = self._row_styles(rows, date_order)
        period = self._period(summary, styles, view)
        transactions: list[ExtractedTransaction] = []
        fields: list[dict[str, float]] = []
        ledger_rows: list[Row] = []
        for row in rows:
            posted: date | None = None
            for style in styles:
                posted = read_date(row.date_text, style, window=period)
                if posted is not None:
                    break
            amount = self._signed(row)
            if posted is None or amount is None:
                outcome.skipped_lines += 1
                continue
            date_conf = self.base if certain else conf.GUESSED
            if period is not None and not (period[0] <= posted <= period[1]):
                date_conf = min(date_conf, conf.INFERRED)
            payee = " ".join(row.description).strip() or None
            score = {
                "posted_date": conf.scaled(date_conf, row.ocr),
                "amount": conf.scaled(self.base, row.ocr),
                "payee": conf.scaled(self.base if payee else conf.MISSING, row.ocr),
            }
            memo = " ".join(row.memo).strip() or None
            fields.append(score)
            ledger_rows.append(Row(amount, self._ledger_balance(row.balance)))
            transactions.append(
                ExtractedTransaction(
                    posted=posted,
                    amount=amount,
                    payee=payee[:4096] if payee else None,
                    memo=memo[:4096] if memo else None,
                    type=self._type(row, " ".join(row.description)),
                    page=row.page,
                    bbox=row.bbox,
                )
            )
        statement_conf: dict[str, float] = {}
        opening = self._ledger_balance(summary.opening)
        closing = self._ledger_balance(summary.closing)
        statement_conf["opening_balance"] = self.base if opening is not None else conf.MISSING
        statement_conf["closing_balance"] = self.base if closing is not None else conf.MISSING
        if ledger_rows and opening is None and ledger_rows[0].balance is not None:
            opening = ledger_rows[0].balance - ledger_rows[0].amount
            statement_conf["opening_balance"] = conf.DERIVED
        if ledger_rows and closing is None and ledger_rows[-1].balance is not None:
            closing = ledger_rows[-1].balance
            statement_conf["closing_balance"] = conf.DERIVED
        result = reconcile(opening, closing, ledger_rows)
        for index in result.running_mismatches:
            fields[index]["amount"] = min(fields[index]["amount"], conf.CONTRADICTED)
        if result.status == "ok" and any(r.balance is not None for r in ledger_rows):
            # Each amount agrees with the printed running balance: confirmed.
            for index, checked in enumerate(ledger_rows):
                if checked.balance is not None and index not in result.running_mismatches:
                    fields[index]["amount"] = 1.0
        cap = {"mismatch": conf.UNRECONCILED, "unavailable": conf.UNCHECKED}.get(result.status)
        if cap is not None:
            for score in fields:
                score["reconciliation"] = cap
        if period is not None:
            statement_conf["period"] = self.base
        elif transactions:
            period = (min(t.posted for t in transactions), max(t.posted for t in transactions))
            # Inferred from the rows: no surer than their dates.
            statement_conf["period"] = min(
                [conf.INFERRED, *(score["posted_date"] for score in fields)]
            )
        statement_conf["account_number"] = self.base if summary.last4 else conf.MISSING
        currency = summary.currency or self.default_currency
        statement_conf["currency"] = self.base if currency else conf.MISSING
        statement_conf["reconciliation"] = (
            1.0 if result.status == "ok" else (cap if cap is not None else 1.0)
        )
        transactions = [
            txn.model_copy(
                update={
                    "confidence": min(score.values()),
                    "low_confidence": conf.low_fields(score, threshold),
                }
            )
            for txn, score in zip(transactions, fields, strict=True)
        ]
        if view.truncated:
            outcome.warnings.append("truncated")
        if not certain:
            outcome.warnings.append("date_order_guessed")
        date_order = styles[0] if styles[0] in ("mdy", "dmy", "ymd") else None
        if styles[0] == "md":
            date_order = "mdy"
        elif styles[0] == "dm":
            date_order = "dmy"
        outcome.statements.append(
            Statement(
                source_format=view.source_format,
                account=StatementAccount(
                    account_type=self.account_type,
                    last4=summary.last4,
                    currency=currency,
                    institution=self.display_name,
                ),
                period_start=period[0] if period else None,
                period_end=period[1] if period else None,
                opening_balance=(
                    Balance(amount=opening, as_of=period[0] if period else None)
                    if opening is not None
                    else None
                ),
                ledger_balance=(
                    Balance(amount=closing, as_of=period[1] if period else None)
                    if closing is not None
                    else None
                ),
                transactions=transactions,
                date_order=date_order,  # type: ignore[arg-type]
                truncated=view.truncated,
                parser=ParserRef(
                    name=self.name,
                    version=self.version,
                    institution=self.institution,
                    kind="statement",
                ),
                confidence={k: round(v, 4) for k, v in statement_conf.items()},
                reconciliation=result,
            )
        )
        return outcome


#: Words on a card statement's first page (the generic card parser's cue).
CARD_VOCABULARY: Final = (
    "card ending",
    "credit card",
    "card statement",
    "minimum payment",
    "credit limit",
)


class CardStatementParser(TableStatementParser):
    """Generic credit card statement: charges positive, credits ``CR`` or negative."""

    name: ClassVar[str] = "card_statement_generic"
    sign: ClassVar[Literal["bank", "card"]] = "card"
    account_type: ClassVar[str | None] = "creditcard"

    def detect(self, view: DocumentView) -> float:
        if self.fingerprints:
            return super().detect(view)
        head = f" {view.head_text()} "
        if not any(f" {word} " in head for word in CARD_VOCABULARY):
            return 0.0
        # Above the bank-statement generic parser for the same table.
        return 0.45 if self.has_table(view) else 0.0
