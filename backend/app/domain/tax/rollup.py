# SPDX-License-Identifier: AGPL-3.0-or-later
"""Yearly tax roll-up by category and schedule hint (API.md ``GET /tax-years/{year}/summary``).

What counts as tax relevant (deductible tagging, DATA_MODEL.md "Ledger"):

* every ledger line in a category with a ``tax_treatment``
  (``deductible``, ``taxable_income``, ``tax_exempt_income``,
  ``tax_payment``), and
* every split flagged ``tax_flag`` whatever its category (reported under
  the category's treatment, or ``flagged`` when the category has none).

Lines come from :func:`app.domain.reports.ledger.ledger_lines` for the
calendar year: the caller's **visible accounts only** (a member or viewer
never sees amounts of accounts not granted to them; ``partial`` says so),
pending review rows and transfers left out, splits instead of their
transaction. Sums are converted into the household's base currency with the
rate of each posting day; a sum without a rate is listed under
``unconverted``. Each category carries its schedule/line hint from
``tax_category_map`` for the year's jurisdiction (a hint for the
accountant, not tax advice); ``by_hint`` adds the categories per hint.

The parsed forms of the year (documents the caller may see) are summed per
form type and box; the checklist counts close the summary.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.core.money import Money, sum_amounts
from app.domain.fx.service import FxConverter
from app.domain.networth.service import sees_household_totals
from app.domain.reports.ledger import Accumulator, LedgerScope, aggregate, ledger_lines
from app.domain.tax.models import TaxCategoryMap, TaxYear
from app.domain.tax.schemas import (
    FormBoxOut,
    SummaryCategory,
    SummaryForm,
    SummaryHint,
    TaxSummaryOut,
)
from app.domain.tax.service import (
    box_amount,
    box_label,
    checklist_counts,
    form_boxes,
    require_year,
    visible_form_rows,
)
from app.domain.transactions.models import Category, TaxTreatment

FLAGGED: Final = "flagged"
ZERO: Final = Decimal(0)


@dataclass(frozen=True)
class CategoryTotal:
    category_id: uuid.UUID | None
    name: str | None
    treatment: TaxTreatment | None
    schedule: str | None
    line: str | None
    total: Decimal
    flagged: Decimal


@dataclass(frozen=True)
class Rollup:
    currency: str
    categories: list[CategoryTotal]
    unconverted: list[Money]


def year_rollup(db: Session, principal: Principal, tax_year: TaxYear) -> Rollup:
    """Tax-relevant totals per category of ``tax_year`` (module doc)."""
    household_id = tax_year.household_id
    converter = FxConverter(db, household_id)
    scope = LedgerScope(
        household_id=household_id,
        date_from=date(tax_year.year, 1, 1),
        date_to=date(tax_year.year, 12, 31),
        currency=converter.target,
    )
    lines = ledger_lines(principal, scope)
    relevant = (
        select(lines)
        .outerjoin(
            Category,
            and_(Category.household_id == household_id, Category.id == lines.c.category_id),
        )
        .where(or_(Category.tax_treatment.is_not(None), lines.c.tax_flag.is_(True)))
        .subquery("tax_line")
    )
    acc = Accumulator(converter)
    seen: set[uuid.UUID | None] = set()
    for row in aggregate(db, relevant, ["category_id", "tax_flag"]):
        category_id, flagged = row.key
        seen.add(category_id)
        acc.add((category_id, "total"), row.total, row.currency, row.fx_day)
        if flagged:
            acc.add((category_id, FLAGGED), row.total, row.currency, row.fx_day)
    ids = {i for i in seen if i is not None}
    categories = {
        c.id: c
        for c in db.execute(
            select(Category).where(Category.household_id == household_id, Category.id.in_(ids))
        ).scalars()
    }
    hints = {
        m.category_id: m
        for m in db.execute(
            select(TaxCategoryMap).where(
                TaxCategoryMap.household_id == household_id,
                TaxCategoryMap.jurisdiction == tax_year.jurisdiction,
            )
        ).scalars()
    }
    out: list[CategoryTotal] = []
    for category_id in sorted(seen, key=str):
        category = categories.get(category_id) if category_id is not None else None
        hint = hints.get(category_id) if category_id is not None else None
        out.append(
            CategoryTotal(
                category_id=category_id,
                name=category.name if category is not None else None,
                treatment=category.tax_treatment if category is not None else None,
                schedule=hint.schedule if hint is not None else None,
                line=hint.line if hint is not None else None,
                total=acc.get((category_id, "total")),
                flagged=acc.get((category_id, FLAGGED)),
            )
        )
    out.sort(key=lambda c: (c.schedule or "~", c.line or "~", c.name or "~"))
    return Rollup(currency=converter.target, categories=out, unconverted=acc.unconverted_out())


def by_treatment(rollup: Rollup) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for c in rollup.categories:
        key = c.treatment.value if c.treatment is not None else FLAGGED
        totals[key] = sum_amounts((totals[key], c.total))
    return dict(totals)


def by_hint(rollup: Rollup) -> list[tuple[str | None, str | None, Decimal]]:
    totals: dict[tuple[str | None, str | None], Decimal] = {}
    for c in rollup.categories:
        key = (c.schedule, c.line)
        totals[key] = sum_amounts((totals.get(key, ZERO), c.total))
    return [(s, line, t) for (s, line), t in sorted(totals.items(), key=_hint_order)]


def _hint_order(item: tuple[tuple[str | None, str | None], Decimal]) -> tuple[str, str]:
    (schedule, line), _ = item
    return (schedule or "~", line or "~")


def form_totals(
    db: Session, principal: Principal, tax_year: TaxYear
) -> list[tuple[str, int, dict[str, Decimal]]]:
    """``(form type, count, box -> sum)`` over the year's visible form data."""
    counts: dict[str, int] = defaultdict(int)
    sums: dict[str, dict[str, Decimal]] = defaultdict(dict)
    for row in visible_form_rows(db, principal, tax_year):
        counts[row.form_type] += 1
        for key, value in form_boxes(row).items():
            if key.startswith("cov_"):
                continue  # coverage marks are not amounts to add up
            sums[row.form_type][key] = sum_amounts((sums[row.form_type].get(key, ZERO), value))
    return [(t, counts[t], sums[t]) for t in sorted(counts)]


def summary(db: Session, principal: Principal, year: int) -> TaxSummaryOut:
    tax_year = require_year(db, principal, year)
    rollup = year_rollup(db, principal, tax_year)
    currency = rollup.currency
    return TaxSummaryOut(
        year=tax_year.year,
        jurisdiction=tax_year.jurisdiction,
        currency=currency,
        partial=not sees_household_totals(db, principal),
        by_treatment={k: Money.of(v, currency) for k, v in sorted(by_treatment(rollup).items())},
        categories=[
            SummaryCategory(
                category_id=c.category_id,
                category_name=c.name,
                tax_treatment=c.treatment,
                schedule=c.schedule,
                line=c.line,
                total=Money.of(c.total, currency),
                flagged=Money.of(c.flagged, currency),
            )
            for c in rollup.categories
        ],
        by_hint=[
            SummaryHint(schedule=s, line=line, total=Money.of(t, currency))
            for s, line, t in by_hint(rollup)
        ],
        forms=[
            SummaryForm(
                form_type=form_type,
                count=count,
                boxes=[
                    FormBoxOut(
                        box=key,
                        label=box_label(tax_year.jurisdiction, form_type, key),
                        amount=box_amount(value),
                    )
                    for key, value in sorted(sums.items())
                ],
            )
            for form_type, count, sums in form_totals(db, principal, tax_year)
        ],
        checklist=checklist_counts(db, tax_year),
        unconverted=rollup.unconverted,
    )
