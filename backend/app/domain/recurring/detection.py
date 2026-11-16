# SPDX-License-Identifier: AGPL-3.0-or-later
"""Recurring series detection: pure functions over ledger rows.

A **series** is one merchant charging (or paying) one account on a rhythm:
a subscription, a bill, a salary. Nothing here reads a database or a clock;
the caller hands in the rows (:class:`Observed`) and, per account, the day of
its latest row (``as_of``). PerFin has no bank feed, so "still running" is
judged against what was uploaded, never against today.

1. **Input.** The caller passes the rows of the look-back window
   (:data:`LOOKBACK_DAYS`) that are not transfer-linked. Dropped here: a zero
   amount; a row after its account's ``as_of``; a descriptor kind in
   :data:`EXCLUDED_KINDS`; a category of kind transfer or ignore (a loan
   payment, :data:`BILL_KEYS`, is the one transfer that is a bill); a category
   in :data:`EXCLUDED_KEYS` (a fee or interest a person filed as such).
2. **Group** by account, merchant key and direction (money out, money in).
   Payee ids are not used, so history does not split when a payee is created.
   A row without a merchant key is grouped by its ``text_key``. A
   person-to-person payment is grouped by its ``text_key`` too (its merchant
   key is the service, and the text is what tells two people apart); without
   one it is ignored.
3. **One charge per cycle is one series.** When the whole group has a cadence
   (step 5) and no two charges closer than the cadence allows, it is one
   series whatever the amounts. Its amount is then read from its **price
   steps**: in date order, a charge within :data:`FIXED_TOLERANCE` (at least
   :data:`FIXED_SLACK`) of the running mean continues the step. A group whose
   steps never return to an earlier price, and are few (three charges of
   history for every change), is *stepwise*: a price that was raised.
   The series keeps all its charges and takes its amount from the last step,
   so the first charge at a new price already belongs to it. Any other group
   is judged on all its amounts (a utility that swings with the season).
4. **Otherwise, cluster amounts.** Sorted by size, a row joins the current
   cluster while it is within :data:`CLUSTER_TOLERANCE` (at least
   :data:`CLUSTER_SLACK`) of the cluster's running mean. Each cluster is
   tried by itself, so two plans at one merchant are two series. This is also
   what happens to a group refused in step 3 for its amounts. When no cluster
   makes a series, a group that is a bill or income by step 7 is tried once
   more as a whole without the "no two charges close together" condition (a
   bill paid late and then on time).

   **Plans told apart by their day of the month.** Money out only. Charges
   are sorted into days of the month (a day and the :data:`FIXED_DAY_SPREAD`
   days after it; a day that wraps around the end of a month is not
   followed). When every such day has at least :data:`FIXED_DAY_MIN_COUNT`
   charges on a monthly or longer rhythm without one irregular gap, and there
   are two days or more, each day is a series of its own: the ones that still
   run are reported. This is tried in two places. Rows that read as
   *fortnightly* (the whole group, or a cluster) are two monthly plans when
   they sit on two days of the month: a charge every fourteen days moves
   through the month instead. And a cluster *without* a cadence is tried this
   way when the dictionary names the merchant as a subscription or a bill
   (two plans at one price behind one descriptor); at any other merchant two
   charges per cycle stay what step 5 says, a habit.
5. **Cadence** by the median gap between consecutive charges
   (:data:`PATTERNS`), with at least :data:`MIN_REGULARITY` of the gaps inside
   the pattern's window. Every row counts, also two on one day: a second
   charge per cycle makes gaps that fit no window, which is how a merchant
   visited more than once per cycle is dropped. The charges needed are the
   pattern's ``min_count``, or ``min_count_known`` when the dictionary names
   the merchant as a subscription or a bill (``series_hint``). The one series
   found without a gap: a known merchant seen exactly once on the account is
   a *suggested* annual series, unless the dictionary's usual cadences
   (``cadence_hints``) leave annual out, and only while a renewal could not
   have shown yet.
6. **Still running**: the last charge is at most one and a half nominal
   cycles plus the pattern's grace before the account's ``as_of``. Older
   series stopped and are not reported. ``next_due`` is the first step after
   the last charge (calendar months, or days for weekly patterns); it is not
   moved forward.
7. **Amount and kind.** *Fixed*: spread within :data:`FIXED_TOLERANCE` of the
   mean (at least :data:`FIXED_SLACK`); *variable*: within
   :data:`CLUSTER_TOLERANCE`; else *wide*. A wide series qualifies only as a
   bill or as income. The kind, first that applies: ``income`` (money in);
   ``bill`` for a person-to-person payment; ``bill`` for a dictionary bill;
   ``subscription`` for a dictionary subscription with a fixed amount;
   ``bill`` for a category in :data:`BILL_KEYS` or a loan payment; ``bill``
   for an amount that is not fixed; ``subscription`` for a fixed amount in
   any other expense category; else ``other`` (fixed, and nothing says what
   it is).
8. **Habits.** A series whose category is in :data:`HABIT_KEYS` (groceries,
   restaurants, coffee, fuel, parking, fares, shopping) is dropped unless its
   amount is fixed, every gap is regular and it has
   :data:`HABIT_MIN_COUNT` charges; then it is only suggested. So is a series
   without a category at a store that sells everything (``habit_hint``: the
   dictionary's ``mixed`` stores, whose rows get a suggestion at most): what
   is bought there now and then is shopping whether or not anybody filed it.
   Such a store also bills plans (a reading or delivery membership), so
   there a series of one price to the cent with every gap regular is
   suggested from the pattern's ordinary count on, not only from six.
9. **Confidence** (0..1, two decimals): ``0.4 * regularity + 0.3 * stability
   + 0.2 * count + 0.1 * known``, where ``stability = 1 - spread / |median
   amount|`` (at least 0; counted as 1 for income and for a bill by its
   category, the dictionary or a loan, whose amount is expected to move),
   ``count = min(1, charges / (2 * min_count))`` and ``known`` is 1 for a
   dictionary subscription or bill.
10. **Status.** ``active`` when the confidence is at least
    :data:`ACTIVE_MIN` and the unreduced ``min_count`` is met; else
    ``suggested``. A habit, a person-to-person series and a merchant seen once
    are ``suggested`` whatever the confidence.

Amounts are exact ``Decimal``; no floats. No regular expression is applied to
a merchant key; :func:`normalize_payee` (the letters-only ``text_key``) uses
one fixed, linear pattern.
"""

from __future__ import annotations

import enum
import itertools
import re
import uuid
from collections import Counter, defaultdict
from collections.abc import Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final, Literal

from app.core.money import calc_context
from app.domain.reports.dates import add_months
from app.domain.transactions.models import Cadence

#: How far back detection looks (three years: an annual pattern shows twice).
LOOKBACK_DAYS: Final = 1100
MIN_REGULARITY: Final = Decimal("0.8")
#: A row joins a cluster within this share of its running mean...
CLUSTER_TOLERANCE: Final = Decimal("0.075")
#: ...or within this much, whichever is more.
CLUSTER_SLACK: Final = Decimal("0.50")
#: A fixed amount: spread within this share of the mean...
FIXED_TOLERANCE: Final = Decimal("0.01")
#: ...or within this much, whichever is more.
FIXED_SLACK: Final = Decimal("0.50")
#: The confidence a series needs to be shown as active.
ACTIVE_MIN: Final = Decimal("0.80")
#: Charges a fixed, perfectly regular series in a habit category needs to be suggested.
HABIT_MIN_COUNT: Final = 6
#: Charges on "one day of the month" land at most this many days after its earliest.
FIXED_DAY_SPREAD: Final = 2
#: Charges each day of the month needs before days tell plans apart (step 4).
FIXED_DAY_MIN_COUNT: Final = 3
CONFIDENCE_QUANTUM: Final = Decimal("0.01")
PAYEE_KEY_MAX: Final = 64
_NOT_WORD: Final = re.compile(r"[^a-z]+")

#: Descriptor kinds that are never a series.
EXCLUDED_KINDS: Final = frozenset(
    {"transfer", "card_payment", "cash", "check", "fee", "interest", "deposit"}
)
#: Category kinds that are never a series (but see :data:`BILL_KEYS`).
EXCLUDED_CATEGORY_KINDS: Final = frozenset({"transfer", "ignore"})
#: Categories that are never a series: a fee or interest, when only the category says so.
EXCLUDED_KEYS: Final = ("financial.bank_fees", "financial.interest_paid", "income.interest")
#: ``system_key`` prefixes of habits: bought often, never a subscription by rhythm alone.
HABIT_KEYS: Final = (
    "food.",
    "transport.fuel",
    "transport.parking",
    "transport.taxi",
    "transport.public_transit",
    "shopping.",
    "transfers.cash",
)
#: ``system_key`` prefixes of bills.
BILL_KEYS: Final = (
    "housing.",
    "utilities.",
    "transfers.loan_payment",
    "transport.car_insurance",
    "health.insurance",
    "financial.insurance",
    "financial.taxes",
    "family.childcare",
    "family.education",
)
#: Names the dictionary may use for a yearly cadence (``Observed.cadence_hints``).
ANNUAL_HINTS: Final = frozenset({"annual", "yearly"})

SeriesKind = Literal["subscription", "bill", "income", "other"]
FoundStatus = Literal["active", "suggested"]


class Pattern(enum.StrEnum):
    WEEKLY = "weekly"
    BIWEEKLY = "biweekly"  # fortnightly; the value is the API's and stays
    MONTHLY = "monthly"
    TWO_MONTHLY = "two_monthly"
    QUARTERLY = "quarterly"
    HALF_YEARLY = "half_yearly"
    ANNUAL = "annual"


@dataclass(frozen=True)
class PatternSpec:
    pattern: Pattern
    low: int  # shortest gap in days
    high: int  # longest gap in days
    min_count: int
    min_count_known: int  # for a merchant the dictionary names as a series
    cadence: Cadence
    interval: int
    step_days: int | None  # day steps (weekly patterns), else month steps
    step_months: int | None
    grace_days: int  # how late a charge may be before it is missed

    @property
    def nominal_days(self) -> int:
        return self.step_days if self.step_days is not None else 30 * (self.step_months or 1)

    def step(self, start: date, k: int) -> date:
        if self.step_days is not None:
            return start + timedelta(days=self.step_days * k)
        return add_months(start, (self.step_months or 1) * k)

    def running(self, last: date, as_of: date) -> bool:
        """Whether a series last charged on ``last`` still runs on ``as_of`` (step 6)."""
        return 2 * ((as_of - last).days - self.grace_days) <= 3 * self.nominal_days


PATTERNS: Final[tuple[PatternSpec, ...]] = (
    PatternSpec(Pattern.WEEKLY, 6, 8, 4, 4, Cadence.WEEKLY, 1, 7, None, 3),
    PatternSpec(Pattern.BIWEEKLY, 12, 16, 3, 3, Cadence.WEEKLY, 2, 14, None, 3),
    PatternSpec(Pattern.MONTHLY, 26, 35, 3, 2, Cadence.MONTHLY, 1, None, 1, 5),
    PatternSpec(Pattern.TWO_MONTHLY, 55, 66, 3, 2, Cadence.MONTHLY, 2, None, 2, 10),
    PatternSpec(Pattern.QUARTERLY, 83, 99, 3, 2, Cadence.QUARTERLY, 1, None, 3, 10),
    PatternSpec(Pattern.HALF_YEARLY, 170, 195, 2, 2, Cadence.MONTHLY, 6, None, 6, 21),
    PatternSpec(Pattern.ANNUAL, 350, 380, 2, 1, Cadence.YEARLY, 1, None, 12, 21),
)
_ANNUAL: Final = PATTERNS[-1]


@dataclass(frozen=True)
class Observed:
    """One ledger row as detection needs it.

    ``kind`` is the descriptor kind clean-up read (``purchase``, ``p2p``,
    ``transfer``...). ``text_key`` is the letters-only key of the descriptor
    (:func:`normalize_payee`), used only when there is no merchant key and for
    ``p2p``. ``category_key`` is the category's ``system_key`` and
    ``category_kind`` its kind. ``series_hint``, ``cadence_hints`` and
    ``habit_hint`` (a store that sells everything and bills no series) are
    what the dictionary says about the merchant. ``category_id`` and ``label``
    (the text to name a series by) are carried through to the candidate.
    """

    id: uuid.UUID
    account_id: uuid.UUID
    posted_date: date
    amount: Decimal
    merchant_key: str | None
    text_key: str | None
    kind: str
    category_key: str | None
    category_kind: str | None
    series_hint: Literal["subscription", "bill"] | None = None
    payee_id: uuid.UUID | None = None
    category_id: uuid.UUID | None = None
    label: str | None = None
    cadence_hints: frozenset[str] = frozenset()
    habit_hint: bool = False


@dataclass(frozen=True)
class Candidate:
    account_id: uuid.UUID
    payee_id: uuid.UUID | None  # the most common payee of its rows
    payee_key: str  # the group: ``merchant:<key>``, ``text:<key>`` or ``p2p:<key>``
    merchant_key: str | None  # ``None`` for a text group and for person-to-person
    text_key: str | None  # the most recent row's
    label: str | None  # the most recent row's
    pattern: Pattern
    cadence: Cadence
    interval_count: int
    amount_min: Decimal  # of the current price step, or of every charge
    amount_max: Decimal
    last_amount: Decimal  # the latest charge
    category_id: uuid.UUID | None
    kind: SeriesKind
    status: FoundStatus
    fixed: bool
    known: bool
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


def _matches(key: str | None, prefixes: tuple[str, ...]) -> bool:
    """Whether ``key`` is under one of ``prefixes``; ``food`` itself is under ``food.``."""
    return key is not None and (key.startswith(prefixes) or f"{key}." in prefixes)


def _most_common[T: Hashable](values: Iterable[T | None]) -> T | None:
    counted = Counter(v for v in values if v is not None)
    return counted.most_common(1)[0][0] if counted else None


def _by_date(rows: Iterable[Observed]) -> list[Observed]:
    return sorted(rows, key=lambda r: (r.posted_date, r.id.bytes))


def _median(values: Sequence[Decimal]) -> Decimal:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    with localcontext(calc_context()):
        return (ordered[mid - 1] + ordered[mid]) / 2


def _mean(values: Sequence[Decimal]) -> Decimal:
    with localcontext(calc_context()):
        return sum(values, Decimal(0)) / len(values)


def _near(size: Decimal, mean: Decimal, share: Decimal, slack: Decimal) -> bool:
    with localcontext(calc_context()):
        return abs(size - mean) <= max(mean * share, slack)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(CONFIDENCE_QUANTUM, rounding=ROUND_HALF_EVEN)


def _spec_for(gap: Decimal) -> PatternSpec | None:
    for spec in PATTERNS:
        if spec.low <= gap <= spec.high:
            return spec
    return None


def cluster_amounts(rows: Sequence[Observed]) -> list[list[Observed]]:
    """Greedy clusters by amount size around a running mean (module doc, step 4)."""
    ordered = sorted(rows, key=lambda r: (abs(r.amount), r.posted_date, r.id.bytes))
    clusters: list[list[Observed]] = []
    sizes: list[Decimal] = []
    for row in ordered:
        size = abs(row.amount)
        if sizes and _near(size, _mean(sizes), CLUSTER_TOLERANCE, CLUSTER_SLACK):
            clusters[-1].append(row)
            sizes.append(size)
        else:
            clusters.append([row])
            sizes = [size]
    return clusters


def _price_steps(rows: Sequence[Observed]) -> list[list[Observed]]:
    """``rows`` (in date order) cut where the amount leaves the running mean (step 3)."""
    steps: list[list[Observed]] = []
    sizes: list[Decimal] = []
    for row in rows:
        size = abs(row.amount)
        if sizes and _near(size, _mean(sizes), FIXED_TOLERANCE, FIXED_SLACK):
            steps[-1].append(row)
            sizes.append(size)
        else:
            steps.append([row])
            sizes = [size]
    return steps


def _current_price(rows: Sequence[Observed]) -> Sequence[Observed]:
    """The rows whose amounts describe the series: the last price step, or all of them."""
    steps = _price_steps(rows)
    if len(steps) == 1 or 3 * (len(steps) - 1) > len(rows):
        return rows
    means = [_mean([abs(r.amount) for r in step]) for step in steps]
    for a, b in itertools.combinations(means, 2):
        if _near(a, b, FIXED_TOLERANCE, FIXED_SLACK):
            return rows  # a price that comes back is a swing, not a rise
    return steps[-1]


@dataclass(frozen=True)
class _Group:
    """What every row of a group shares (module doc, steps 2 and 7)."""

    key: str
    merchant_key: str | None
    inflow: bool
    p2p: bool
    hint: Literal["subscription", "bill"] | None
    category_key: str | None
    category_kind: str | None
    loan: bool
    annual_ok: bool
    #: A store that sells everything, by the dictionary (step 8).
    general_store: bool = False

    @property
    def known(self) -> bool:
        return self.hint is not None

    @property
    def habit(self) -> bool:
        if self.category_key is None:
            return self.general_store
        return _matches(self.category_key, HABIT_KEYS)

    @property
    def general_plan(self) -> bool:
        """A habit by the dictionary's word alone (step 8): nobody filed its rows."""
        return self.category_key is None and self.general_store

    @property
    def bill_like(self) -> bool:
        """A bill whatever its amounts do: the dictionary, the category or a loan says so."""
        return not self.inflow and (
            self.hint == "bill" or self.loan or _matches(self.category_key, BILL_KEYS)
        )


@dataclass(frozen=True)
class _Shape:
    """The rhythm of some rows (module doc, steps 5 and 6)."""

    spec: PatternSpec
    regularity: Decimal
    #: Gaps shorter than the pattern allows: a second charge in a cycle.
    short: int


def _shape(
    rows: Sequence[Observed], as_of: date, known: bool, *, stopped: bool = False
) -> _Shape | None:
    """The cadence of ``rows`` (date order), if they have one and still run.

    ``stopped``: a series that no longer runs has a shape too.
    """
    if len(rows) < 2:
        return None
    gaps = [(b.posted_date - a.posted_date).days for a, b in itertools.pairwise(rows)]
    spec = _spec_for(_median([Decimal(g) for g in gaps]))
    if spec is None:
        return None
    if len(rows) < (spec.min_count_known if known else spec.min_count):
        return None
    if not stopped and not spec.running(rows[-1].posted_date, as_of):
        return None
    with localcontext(calc_context()):
        regularity = Decimal(sum(1 for g in gaps if spec.low <= g <= spec.high)) / len(gaps)
    if regularity < MIN_REGULARITY:
        return None
    return _Shape(spec, regularity, sum(1 for g in gaps if g < spec.low))


def _kind(group: _Group, fixed: bool) -> SeriesKind:
    """Module doc, step 7."""
    if group.inflow:
        return "income"
    if group.p2p or group.hint == "bill":
        return "bill"
    if group.hint == "subscription" and fixed:
        return "subscription"
    if group.bill_like or not fixed:
        return "bill"
    if group.category_key is not None and group.category_kind == "expense":
        return "subscription"
    return "other"


def _candidate(
    group: _Group,
    rows: Sequence[Observed],
    spec: PatternSpec,
    regularity: Decimal,
    *,
    once: bool = False,
) -> Candidate | None:
    """A candidate from ``rows`` (date order) with a cadence, or ``None`` (steps 7 to 10)."""
    priced = _current_price(rows)
    amounts = [r.amount for r in priced]
    sizes = [abs(a) for a in amounts]
    with localcontext(calc_context()):
        mean = _mean(sizes)
        spread = max(sizes) - min(sizes)
        fixed = spread <= max(mean * FIXED_TOLERANCE, FIXED_SLACK)
        wide = spread > max(mean * CLUSTER_TOLERANCE, CLUSTER_SLACK)
        stability = max(Decimal(0), 1 - spread / max(_median(sizes), CONFIDENCE_QUANTUM))
        if group.bill_like or group.inflow:
            stability = Decimal(1)  # its amount is expected to move: the rhythm is the evidence
        count = min(Decimal(1), Decimal(len(rows)) / Decimal(2 * spec.min_count))
        confidence = _quantize(
            Decimal("0.4") * regularity
            + Decimal("0.3") * stability
            + Decimal("0.2") * count
            + (Decimal("0.1") if group.known else Decimal(0))
        )
    if wide and not (group.bill_like or group.inflow):
        return None
    if group.habit and regularity < 1:
        return None
    # A habit is shown only as a plan: fixed and with six charges, or, at a
    # general store whose rows nobody filed, one price to the cent on every
    # charge (a plan billed through the store) as early as any series.
    steady = fixed and len(rows) >= HABIT_MIN_COUNT
    if group.habit and not (steady or (group.general_plan and spread == 0)):
        return None
    sure = confidence >= ACTIVE_MIN and len(rows) >= spec.min_count
    status: FoundStatus = "active"
    if once or group.habit or group.p2p or not sure:
        status = "suggested"
    last = rows[-1]
    return Candidate(
        account_id=last.account_id,
        payee_id=_most_common(r.payee_id for r in rows),
        payee_key=group.key,
        merchant_key=group.merchant_key,
        text_key=next((r.text_key for r in reversed(rows) if r.text_key), None),
        label=next((r.label for r in reversed(rows) if r.label), None),
        pattern=spec.pattern,
        cadence=spec.cadence,
        interval_count=spec.interval,
        amount_min=min(amounts),
        amount_max=max(amounts),
        last_amount=last.amount,
        category_id=_most_common(r.category_id for r in rows),
        kind=_kind(group, fixed),
        status=status,
        fixed=fixed,
        known=group.known,
        occurrences=len(rows),
        first_seen=rows[0].posted_date,
        last_seen=last.posted_date,
        next_due=spec.step(last.posted_date, 1),
        regularity=_quantize(regularity),
        confidence=confidence,
        transaction_ids=tuple(r.id for r in rows),
    )


def _seen_once(group: _Group, row: Observed, as_of: date) -> Candidate | None:
    """A known merchant charged exactly once: a suggested annual series (step 5)."""
    if not group.known or not group.annual_ok or group.inflow or group.p2p or group.habit:
        return None
    if (as_of - row.posted_date).days > _ANNUAL.high + _ANNUAL.grace_days:
        return None
    return _candidate(group, [row], _ANNUAL, Decimal(0), once=True)


def _by_day_of_month(rows: Iterable[Observed]) -> list[list[Observed]]:
    """``rows`` sorted into days of the month (a day and :data:`FIXED_DAY_SPREAD` after it)."""
    parts: list[list[Observed]] = []
    for row in sorted(rows, key=lambda r: (r.posted_date.day, r.posted_date, r.id.bytes)):
        if parts and row.posted_date.day - parts[-1][0].posted_date.day <= FIXED_DAY_SPREAD:
            parts[-1].append(row)
        else:
            parts.append([row])
    return parts


def _plans_by_day(group: _Group, rows: Sequence[Observed], as_of: date) -> list[Candidate]:
    """The running plans of ``rows`` when their days of the month tell plans apart, else none.

    Module doc, step 4: every day of the month must be a series in its own
    right, or the rows are not read this way at all.
    """
    if group.inflow:
        return []
    parts = _by_day_of_month(rows)
    if len(parts) < 2:
        return []
    out: list[Candidate] = []
    for part in parts:
        ordered = _by_date(part)
        if len(ordered) < FIXED_DAY_MIN_COUNT:
            return []
        shape = _shape(ordered, as_of, group.known, stopped=True)
        if shape is None or shape.spec.step_months is None or shape.regularity < 1:
            return []
        if not shape.spec.running(ordered[-1].posted_date, as_of):
            continue  # a plan that was cancelled: it told the days apart, and is not reported
        found = _candidate(group, ordered, shape.spec, shape.regularity)
        if found is None:
            return []
        out.append(found)
    return out


def _series(group: _Group, members: Sequence[Observed], as_of: date) -> list[Candidate]:
    """The series of one group (module doc, steps 3 to 5)."""
    rows = _by_date(members)
    if len(rows) == 1:
        once = _seen_once(group, rows[0], as_of)
        return [once] if once is not None else []
    whole = _shape(rows, as_of, group.known)
    if whole is not None and whole.short == 0:
        if whole.spec.pattern is Pattern.BIWEEKLY and (plans := _plans_by_day(group, rows, as_of)):
            return plans
        found = _candidate(group, rows, whole.spec, whole.regularity)
        if found is not None:
            return [found]
    out: list[Candidate] = []
    for cluster in cluster_amounts(rows):
        ordered = _by_date(cluster)
        shape = _shape(ordered, as_of, group.known)
        by_day = group.known if shape is None else shape.spec.pattern is Pattern.BIWEEKLY
        plans = _plans_by_day(group, ordered, as_of) if by_day else []
        if plans:
            out.extend(plans)
            continue
        if shape is None:
            continue
        found = _candidate(group, ordered, shape.spec, shape.regularity)
        if found is not None:
            out.append(found)
    if not out and whole is not None and (group.bill_like or group.inflow):
        found = _candidate(group, rows, whole.spec, whole.regularity)
        if found is not None:
            out.append(found)
    return out


def _excluded(row: Observed) -> bool:
    """Module doc, step 1."""
    if row.amount == 0 or row.kind in EXCLUDED_KINDS:
        return True
    if _matches(row.category_key, EXCLUDED_KEYS):
        return True
    return row.category_kind in EXCLUDED_CATEGORY_KINDS and not _matches(
        row.category_key, BILL_KEYS
    )


def group_key(row: Observed) -> str | None:
    """The group ``row`` belongs to (module doc, step 2), ``None`` for a row without one."""
    if row.kind == "p2p":
        return f"p2p:{row.text_key}" if row.text_key else None
    if row.merchant_key:
        return f"merchant:{row.merchant_key}"
    return f"text:{row.text_key}" if row.text_key else None


def _describe(key: str, inflow: bool, members: Sequence[Observed]) -> _Group:
    p2p = key.startswith("p2p:")
    category_key = _most_common(r.category_key for r in members)
    hints = frozenset(hint for r in members for hint in r.cadence_hints)
    return _Group(
        key=key,
        merchant_key=members[0].merchant_key if key.startswith("merchant:") else None,
        inflow=inflow,
        p2p=p2p,
        hint=_most_common(r.series_hint for r in members),
        category_key=category_key,
        category_kind=next(
            (r.category_kind for r in members if r.category_key == category_key), None
        ),
        loan=_most_common(r.kind for r in members) == "loan_payment",
        annual_ok=not hints or bool(hints & ANNUAL_HINTS),
        general_store=any(r.habit_hint for r in members),
    )


def detect(rows: Iterable[Observed], as_of: Mapping[uuid.UUID, date]) -> list[Candidate]:
    """Every running series in ``rows`` (module doc), most confident first.

    ``as_of`` is each account's data clock, the day of its latest row. An
    account it does not name is read up to the latest row passed for it.
    """
    kept = [row for row in rows if not _excluded(row)]
    clock = dict(as_of)
    for row in kept:
        if row.account_id not in as_of:
            clock[row.account_id] = max(clock.get(row.account_id, row.posted_date), row.posted_date)
    groups: dict[tuple[uuid.UUID, str, bool], list[Observed]] = defaultdict(list)
    for row in kept:
        key = group_key(row)
        if key is None or row.posted_date > clock[row.account_id]:
            continue
        groups[(row.account_id, key, row.amount > 0)].append(row)
    found: list[Candidate] = []
    for (account_id, key, inflow), members in sorted(
        groups.items(), key=lambda kv: (kv[0][0].bytes, kv[0][1], kv[0][2])
    ):
        found.extend(_series(_describe(key, inflow, members), members, clock[account_id]))
    found.sort(
        key=lambda c: (-c.confidence, c.next_due, c.account_id.bytes, c.payee_key, c.amount_min)
    )
    return found


# ---------------------------------------------------------------------------
# Cost
# ---------------------------------------------------------------------------

#: Charges per year of one interval of each cadence.
_PER_YEAR: Final[Mapping[Cadence, int]] = {
    Cadence.DAILY: 365,
    Cadence.WEEKLY: 52,
    Cadence.MONTHLY: 12,
    Cadence.QUARTERLY: 4,
    Cadence.YEARLY: 1,
}


def yearly_cost(amount: Decimal, cadence: Cadence, interval: int) -> Decimal:
    """``amount`` every ``interval`` ``cadence`` spread over a year (not rounded)."""
    with localcontext(calc_context()):
        return amount * _PER_YEAR[cadence] / max(interval, 1)


def monthly_cost(amount: Decimal, cadence: Cadence, interval: int) -> Decimal:
    """``amount`` every ``interval`` ``cadence`` spread over a month (not rounded)."""
    with localcontext(calc_context()):
        return amount * _PER_YEAR[cadence] / (12 * max(interval, 1))
