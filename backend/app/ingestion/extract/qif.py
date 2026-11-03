# SPDX-License-Identifier: AGPL-3.0-or-later
"""QIF extraction (Quicken Interchange Format).

* Sections start with ``!Type:<name>``; transaction sections (``Bank``,
  ``Cash``, ``CCard``, ``Oth A``, ``Oth L``) become one statement each, with
  the account name from a preceding ``!Account`` block when there is one.
  Investment, category, class and memorised sections are skipped (warning).
* Records end with ``^``. Fields: ``D`` date, ``T``/``U`` amount, ``P``
  payee, ``M`` memo, ``N`` check number, ``C`` cleared flag, ``L`` category,
  and ``S``/``E``/``$`` split lines.
* **Dates** carry no format marker. All dates of the file are looked at
  together: a first component above 12 means day-month-year, a second
  component above 12 means month-day-year; both is an error; neither keeps
  the Quicken default month-day-year and says so (``date_order:
  "mdy_assumed"`` and a warning). Four-digit first components are
  year-month-day. Two-digit years: ``'`` (Quicken's 2000s marker) or a year
  below 70 is 20xx, otherwise 19xx.
* Text fields are whitespace-collapsed, cut and formula-neutralised (C7).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Final, Literal

from app.ingestion.extract.common import ExtractFailure, clean_text, decode_text, parse_amount
from app.ingestion.extract.registry import register
from app.ingestion.sandbox.protocol import (
    ExtractedTransaction,
    ExtractOptions,
    ExtractResult,
    SplitLine,
    Statement,
    StatementAccount,
)

VERSION: Final = "qif/1"
TRANSACTION_TYPES: Final = {
    "bank": "bank",
    "cash": "cash",
    "ccard": "creditcard",
    "oth a": "other_asset",
    "oth l": "other_liability",
}
_DATE_PARTS: Final = re.compile(r"^\s*(\d{1,4})\s*[/.\-]\s*(\d{1,2})\s*([/.\-']\s*)(\d{1,4})\s*$")
_CLEARED: Final = {"*": "cleared", "c": "cleared", "x": "reconciled", "r": "reconciled"}

DateOrder = Literal["ymd", "mdy", "dmy", "mdy_assumed"]


@dataclass
class _Record:
    date_raw: str | None = None
    amount_raw: str | None = None
    payee: str | None = None
    memo: str | None = None
    number: str | None = None
    cleared: str | None = None
    category: str | None = None
    splits: list[dict[str, str]] = field(default_factory=list)


@dataclass
class _Section:
    account_type: str
    account_name: str | None
    records: list[_Record] = field(default_factory=list)
    truncated: bool = False


@dataclass(frozen=True)
class _DateParts:
    a: int
    b: int
    year: int


def _date_parts(raw: str) -> _DateParts | None:
    match = _DATE_PARTS.match(raw)
    if match is None:
        return None
    first, second, separator, last = match.groups()
    if len(first) == 4:  # year-month-day
        return _DateParts(int(second), int(last), -int(first))
    year = int(last)
    if len(last) <= 2:
        year += 2000 if ("'" in separator or year < 70) else 1900
    return _DateParts(int(first), int(second), year)


def _date_order(parts: list[_DateParts]) -> DateOrder:
    if parts and all(p.year < 0 for p in parts):
        return "ymd"
    if any(p.year < 0 for p in parts):
        raise ExtractFailure("inconsistent_dates", "mixed date formats")
    day_first = any(p.a > 12 for p in parts)
    month_first = any(p.b > 12 for p in parts)
    if day_first and month_first:
        raise ExtractFailure("inconsistent_dates", "dates cannot all be read with one format")
    if day_first:
        return "dmy"
    if month_first:
        return "mdy"
    return "mdy_assumed"


def _to_date(parts: _DateParts, order: DateOrder) -> date | None:
    try:
        if order == "ymd":
            return date(-parts.year, parts.a, parts.b)
        if order == "dmy":
            return date(parts.year, parts.b, parts.a)
        return date(parts.year, parts.a, parts.b)
    except ValueError:
        return None


def _sections(text: str, options: ExtractOptions, warnings: set[str]) -> list[_Section]:
    sections: list[_Section] = []
    current: _Section | None = None
    record = _Record()
    in_account = False
    account_name: str | None = None
    budget = options.max_transactions
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.strip():
            continue
        if line.startswith("!"):
            header = line[1:].strip()
            lowered = header.lower()
            if lowered == "account":
                in_account, current = True, None
                account_name = None
            elif lowered.startswith("type:"):
                in_account = False
                kind = lowered[5:].strip()
                if kind in TRANSACTION_TYPES:
                    current = _Section(TRANSACTION_TYPES[kind], account_name)
                    sections.append(current)
                else:
                    current = None
                    warnings.add(f"section_skipped:{re.sub(r'[^a-z0-9_]', '_', kind)[:32]}")
            elif lowered.startswith(("option:", "clear:")):
                continue
            else:
                current = None
            record = _Record()
            continue
        code, value = line[0], line[1:]
        if in_account:
            if code == "N":
                account_name = value
            elif code == "^":
                in_account = False
            continue
        if current is None:
            continue
        if code == "^":
            if record != _Record():
                if budget <= 0:
                    current.truncated = True
                else:
                    current.records.append(record)
                    budget -= 1
            record = _Record()
        elif code == "D":
            record.date_raw = value
        elif code == "T" or (code == "U" and record.amount_raw is None):
            record.amount_raw = value
        elif code == "P":
            record.payee = value
        elif code == "M":
            record.memo = value
        elif code == "N":
            record.number = value
        elif code == "C":
            record.cleared = value.strip().lower()[:1]
        elif code == "L":
            record.category = value
        elif code == "S":
            record.splits.append({"category": value})
        elif code == "E" and record.splits:
            record.splits[-1]["memo"] = value
        elif code == "$" and record.splits:
            record.splits[-1]["amount"] = value
    if current is not None and record != _Record():
        warnings.add("last_record_unterminated")
    return sections


def _splits(raw: list[dict[str, str]], warnings: set[str]) -> list[SplitLine]:
    lines: list[SplitLine] = []
    for item in raw[:1000]:
        amount = parse_amount(item.get("amount"))
        if amount is None:
            warnings.add("split_skipped_invalid_amount")
            continue
        lines.append(
            SplitLine(
                amount=amount,
                category=clean_text(item.get("category")),
                memo=clean_text(item.get("memo")),
            )
        )
    return lines


def extract_qif(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    text, _ = decode_text(data)
    warnings: set[str] = set()
    sections = _sections(text, options, warnings)
    if not sections:
        raise ExtractFailure("no_statements", "no transaction section found")
    all_parts: list[_DateParts] = []
    for section in sections:
        for record in section.records:
            parts = _date_parts(record.date_raw or "")
            if parts is not None:
                all_parts.append(parts)
    order = _date_order(all_parts)
    if order == "mdy_assumed":
        warnings.add("date_order_assumed_mdy")
    statements: list[Statement] = []
    for section in sections:
        transactions: list[ExtractedTransaction] = []
        for record in section.records:
            parts = _date_parts(record.date_raw or "")
            posted = _to_date(parts, order) if parts is not None else None
            amount = parse_amount(record.amount_raw)
            if posted is None or amount is None:
                warnings.add("transaction_skipped_invalid_date_or_amount")
                continue
            splits = _splits(record.splits, warnings)
            if splits and sum((s.amount for s in splits), Decimal(0)) != amount:
                warnings.add("split_total_mismatch")
            transactions.append(
                ExtractedTransaction(
                    posted=posted,
                    amount=amount,
                    payee=clean_text(record.payee),
                    memo=clean_text(record.memo),
                    category=clean_text(record.category),
                    check_number=clean_text(record.number, limit=64),
                    cleared=_CLEARED.get(record.cleared or ""),
                    splits=splits,
                )
            )
        if section.truncated:
            warnings.add(f"transactions_truncated:{options.max_transactions}")
        dates = [t.posted for t in transactions]
        statements.append(
            Statement(
                source_format="qif",
                account=StatementAccount(
                    account_type=section.account_type, name=clean_text(section.account_name)
                ),
                period_start=min(dates) if dates else None,
                period_end=max(dates) if dates else None,
                transactions=transactions,
                date_order=order,
                truncated=section.truncated,
            )
        )
    return ExtractResult(
        kind=kind,
        ok=True,
        extractor_version=VERSION,
        statements=statements,
        warnings=sorted(warnings),
    )


register("qif", extract_qif)
