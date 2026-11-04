# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recurring transaction detection: pure functions over ledger rows.

Input: the transactions of the look-back window (:data:`LOOKBACK_DAYS`),
transfers excluded. Steps:

1. **Group** by account, payee (the ``payee_id``, else the normalised raw
   payee text: lower case, digits and punctuation removed, so ``NETFLIX
   #1234`` and ``Netflix 5678`` meet) and sign (money in vs out). Rows with
   neither payee nor text are ignored.
2. **Cluster amounts** inside a group: sorted by size, a row joins the
   current cluster while it is within :data:`AMOUNT_TOLERANCE` (10 %) plus
   :data:`AMOUNT_SLACK` (1.00) of the cluster's smallest amount. Two plans
   with the same payee become two clusters. When no cluster forms a
   pattern, the whole group is tried as one (a variable bill such as a
   utility: same payee, regular dates, changing amounts).
3. **Cadence**: one row per day; the median gap between consecutive dates
   picks the pattern (:data:`PATTERNS`): weekly 6-8 days, biweekly 12-16,
   monthly 26-35, quarterly 83-99, annual 350-380. ``regularity`` is the
   share of gaps inside that pattern's window. Minimum occurrences: weekly
   4, biweekly/monthly/quarterly 3, annual 2.
4. **Still running**: the last occurrence is at most twice the pattern's
   longest gap before ``today``; older patterns ended and are dropped.
5. **Confidence** (0..1, two decimals): ``0.5 * regularity + 0.3 *
   stability + 0.2 * count``, where ``stability = 1 - (max - min) /
   |median amount|`` (at least 0) and ``count = min(1, occurrences / (2 *
   minimum))``. Candidates need ``regularity >= 0.6`` and ``confidence >=``
   :data:`MIN_CONFIDENCE`.
6. **Next due**: the last occurrence plus one step (days for weekly
   patterns, calendar months for the others, anchored on the last
   occurrence), moved forward by whole steps while it is more than the
   pattern's grace (longest gap minus the step) before ``today``.

Amounts are exact ``Decimal``; no floats.
"""

from __future__ import annotations

import enum
import itertools
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final

from app.core.money import calc_context
from app.domain.reports.dates import add_months
from app.domain.transactions.models import Cadence

#: How far back detection looks (three years: annual patterns need two).
LOOKBACK_DAYS: Final = 1100
AMOUNT_TOLERANCE: Final = Decimal("0.10")
AMOUNT_SLACK: Final = Decimal("1.00")
MIN_REGULARITY: Final = Decimal("0.6")
MIN_CONFIDENCE: Final = Decimal("0.60")
CONFIDENCE_QUANTUM: Final = Decimal("0.01")
PAYEE_KEY_MAX: Final = 64
_NOT_WORD: Final = re.compile(r"[^a-z]+")


class Pattern(enum.StrEnum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ANNUAL = "annual"


@dataclass(frozen=True)
class PatternSpec:
    pattern: Pattern
    low: int  # shortest gap in days
    high: int  # longest gap in days
    min_count: int
    cadence: Cadence
    interval: int
    step_days: int | None  # day steps (weekly patterns), else month steps
    step_months: int | None

    @property
    def nominal_days(self) -> int:
        return self.step_days if self.step_days is not None else 30 * (self.step_months or 1)

    def step(self, start: date, k: int) -> date:
        if self.step_days is not None:
            return start + timedelta(days=self.step_days * k)
        return add_months(start, (self.step_months or 1) * k)


PATTERNS: Final[tuple[PatternSpec, ...]] = (
    PatternSpec(Pattern.WEEKLY, 6, 8, 4, Cadence.WEEKLY, 1, 7, None),
    PatternSpec(Pattern.BIWEEKLY, 12, 16, 3, Cadence.WEEKLY, 2, 14, None),
    PatternSpec(Pattern.MONTHLY, 26, 35, 3, Cadence.MONTHLY, 1, None, 1),
    PatternSpec(Pattern.QUARTERLY, 83, 99, 3, Cadence.QUARTERLY, 1, None, 3),
    PatternSpec(Pattern.ANNUAL, 350, 380, 2, Cadence.YEARLY, 1, None, 12),
)


@dataclass(frozen=True)
class Observed:
    """One ledger row as detection needs it."""

    id: uuid.UUID
    account_id: uuid.UUID
    posted_date: date
    amount: Decimal
    payee_id: uuid.UUID | None = None
    raw_payee: str | None = None
    category_id: uuid.UUID | None = None


@dataclass(frozen=True)
class Candidate:
    account_id: uuid.UUID
    payee_id: uuid.UUID | None
    payee_key: str
    label: str | None  # most recent raw payee text (for a name without a payee)
    pattern: Pattern
    cadence: Cadence
    interval_count: int
    amount_min: Decimal
    amount_max: Decimal
    category_id: uuid.UUID | None
    occurrences: int
    first_seen: date
    last_seen: date
    next_due: date
    regularity: Decimal
    confidence: Decimal
    transaction_ids: tuple[uuid.UUID, ...]

    @property
    def inflow(self) -> bool:
        return self.amount_min >= 0


def normalize_payee(text: str | None) -> str:
    """Lower case letters only, single spaces (digits and punctuation removed)."""
    if not text:
        return ""
    return " ".join(_NOT_WORD.sub(" ", text.lower()).split())[:PAYEE_KEY_MAX]


def payee_key(row: Observed) -> str | None:
    if row.payee_id is not None:
        return f"payee:{row.payee_id}"
    text = normalize_payee(row.raw_payee)
    return f"text:{text}" if text else None


def cluster_amounts(rows: Sequence[Observed]) -> list[list[Observed]]:
    """Greedy clusters by amount size (module doc, step 2)."""
    ordered = sorted(rows, key=lambda r: (abs(r.amount), r.posted_date, r.id.bytes))
    clusters: list[list[Observed]] = []
    current: list[Observed] = []
    floor = Decimal(0)
    with localcontext(calc_context()):
        for row in ordered:
            size = abs(row.amount)
            if current and size <= floor * (1 + AMOUNT_TOLERANCE) + AMOUNT_SLACK:
                current.append(row)
                continue
            if current:
                clusters.append(current)
            current = [row]
            floor = size
    if current:
        clusters.append(current)
    return clusters


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    with localcontext(calc_context()):
        return (ordered[mid - 1] + ordered[mid]) / 2


def _spec_for(gap: Decimal) -> PatternSpec | None:
    for spec in PATTERNS:
        if spec.low <= gap <= spec.high:
            return spec
    return None


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(CONFIDENCE_QUANTUM, rounding=ROUND_HALF_EVEN)


def evaluate(rows: Sequence[Observed], today: date) -> Candidate | None:
    """A candidate from one cluster (module doc, steps 3-6), or ``None``."""
    by_day: dict[date, Observed] = {}
    for row in sorted(rows, key=lambda r: (r.posted_date, r.id.bytes)):
        by_day.setdefault(row.posted_date, row)
    days = sorted(by_day)
    if len(days) < 2:
        return None
    gaps = [(b - a).days for a, b in itertools.pairwise(days)]
    spec = _spec_for(_median([Decimal(g) for g in gaps]))
    if spec is None or len(days) < spec.min_count:
        return None
    last = days[-1]
    if (today - last).days > 2 * spec.high:
        return None
    kept = [by_day[d] for d in days]
    amounts = [r.amount for r in kept]
    with localcontext(calc_context()):
        regularity = Decimal(sum(1 for g in gaps if spec.low <= g <= spec.high)) / Decimal(
            len(gaps)
        )
        median = abs(_median(amounts))
        spread = max(amounts) - min(amounts)
        stability = max(Decimal(0), 1 - spread / max(median, Decimal("0.01")))
        count = min(Decimal(1), Decimal(len(days)) / Decimal(2 * spec.min_count))
        confidence = _quantize(
            Decimal("0.5") * regularity + Decimal("0.3") * stability + Decimal("0.2") * count
        )
    if regularity < MIN_REGULARITY or confidence < MIN_CONFIDENCE:
        return None
    grace = spec.high - spec.nominal_days
    k = 1
    next_due = spec.step(last, k)
    while next_due + timedelta(days=max(grace, 0)) < today:
        k += 1
        next_due = spec.step(last, k)
    categories = Counter(r.category_id for r in kept if r.category_id is not None)
    first = kept[0]
    return Candidate(
        account_id=first.account_id,
        payee_id=first.payee_id,
        payee_key=payee_key(first) or "",
        label=next((r.raw_payee for r in reversed(kept) if r.raw_payee), None),
        pattern=spec.pattern,
        cadence=spec.cadence,
        interval_count=spec.interval,
        amount_min=min(amounts),
        amount_max=max(amounts),
        category_id=categories.most_common(1)[0][0] if categories else None,
        occurrences=len(days),
        first_seen=days[0],
        last_seen=last,
        next_due=next_due,
        regularity=_quantize(regularity),
        confidence=confidence,
        transaction_ids=tuple(r.id for r in kept),
    )


def detect(rows: Iterable[Observed], today: date) -> list[Candidate]:
    """Every recurring pattern in ``rows`` (module doc), most confident first."""
    groups: dict[tuple[uuid.UUID, str, bool], list[Observed]] = defaultdict(list)
    for row in rows:
        if row.amount == 0 or row.posted_date > today:
            continue
        key = payee_key(row)
        if key is None:
            continue
        groups[(row.account_id, key, row.amount > 0)].append(row)
    found: list[Candidate] = []
    for _, members in sorted(groups.items(), key=lambda kv: (kv[0][0].bytes, kv[0][1], kv[0][2])):
        clustered = [c for c in (evaluate(cl, today) for cl in cluster_amounts(members)) if c]
        if not clustered:
            whole = evaluate(members, today)
            if whole is not None:
                clustered = [whole]
        found.extend(clustered)
    found.sort(key=lambda c: (-c.confidence, c.next_due, c.account_id.bytes, c.payee_key))
    return found
