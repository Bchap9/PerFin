# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measuring categorisation and subscription detection against the labelled sets.

:func:`categorisation_metrics` scores an ``identify`` and a ``decide``
function on ``tests/fixtures/descriptors.py``; :func:`subscription_metrics`
scores the series a detector found on ``synth_subscription_ledger.py``. The
fields are the rows of the design note's table (section 9). Ratios are
``None`` when there was nothing to judge (no fact stated, no series found).

``uv run python -m tests.fixtures.quality --now`` prints both for the code as
it is: the 1,200 lines go through ``engine.identify`` and ``engine.decide``
as a new household's rows would (:func:`engine_decide`), and the ledger is
imported month by month into a household of a scratch database with
detection after each month (:func:`replay`), once with a person filing what
the engine left open and once with nobody doing so. The second half needs
PostgreSQL (``PERFIN_TEST_DATABASE_URL``), as the integration tests do;
``--no-subscriptions`` leaves it out and ``--misses`` lists what was missed.
The targets are tests: ``tests/unit/test_categorisation_quality.py`` and
``tests/integration/test_subscription_quality.py``.

**How a line is judged** (the label rules are in ``descriptors.py``):

* ``known_fact_right``: of the known lines that have a category, the share
  stated as a fact with that category.
* ``fact_precision``: of every fact stated on any line, the share that is the
  label's category (a fact on a line labelled ``None`` is wrong).
* ``suggestion_precision``: of the suggestions that can be judged, the share
  that is the label's category. A suggestion for a store that sells
  everything (a ``known`` line labelled ``None``) is not judged; one for any
  other line labelled ``None`` is wrong.
* ``invented_wrong_fact``: the share of invented lines given a fact other
  than their label.
* ``fact_coverage``: the share of all lines given a fact.
* ``one_key_per_merchant``: of the lines that have a merchant label, the
  share carrying their label's most common key, where a key that is the most
  common one of two labels counts only for the label with more lines (so one
  key for everybody scores nothing).
* ``kind_right``: of the non-purchase lines, the share whose kind was read.
* ``private_leaks``: lines whose key contains a word of a person's name.

**How a series is judged.** A found series belongs to the planted series or
decoy that owns more than half of its rows; a series that merges two plans
therefore finds at most one of them. ``found`` counts planted series with a
live or ended series, ``found_active`` those shown as active (or ended, when
it really stopped). A found series that belongs to nothing (it was put
together from one-off purchases) counts with the decoys; salary is ignored.
"""

from __future__ import annotations

import argparse
import calendar
import re
import tempfile
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Final

from tests.fixtures import synth_subscription_ledger as synth
from tests.fixtures.descriptors import CATEGORY_KEYS, LABELLED, Labelled
from tests.fixtures.synth_subscription_ledger import Ledger

if TYPE_CHECKING:
    from app.domain.categorisation.memory import Learned
    from tests.fixtures.pipeline import Home

#: A failing target prints at most this many misses.
MISSES_SHOWN: Final = 40
#: A price change counts for the planted one when reported this long after it at most
#: (a detector may wait for the second charge at the new price).
PRICE_CHANGE_WINDOW: Final = timedelta(days=100)
LIVE: Final = frozenset({"active", "confirmed", "suggested"})
SHOWN_ACTIVE: Final = frozenset({"active", "confirmed"})
#: Series names in :attr:`Ledger.members` that are neither planted nor decoys.
NEUTRAL: Final = frozenset({"salary"})

_WORD: Final = re.compile(r"[a-z0-9]+")

Identify = Callable[[str], tuple[str | None, str]]
Decide = Callable[[Labelled], tuple[str | None, bool]]


@dataclass(frozen=True)
class CategorisationMetrics:
    known_fact_right: float | None
    fact_precision: float | None
    suggestion_precision: float | None
    invented_wrong_fact: float | None
    fact_coverage: float
    one_key_per_merchant: float | None
    kind_right: float | None
    private_leaks: int
    #: Field name to what went wrong ("line -> answer"), for a failing target.
    misses: Mapping[str, tuple[str, ...]] = field(default_factory=dict, compare=False)


def _ratio(part: int, whole: int) -> float | None:
    return part / whole if whole else None


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _key_owners(keys: Sequence[tuple[Labelled, str | None]]) -> dict[str, str]:
    """Each merchant label's most common key, kept only by the label that uses it most."""
    per_label: dict[str, Counter[str]] = defaultdict(Counter)
    for line, key in keys:
        if line.merchant is not None and key:
            per_label[line.merchant][key] += 1
    modal: dict[str, tuple[str, int]] = {}
    for label, counts in per_label.items():
        key, count = min(counts.items(), key=lambda item: (-item[1], item[0]))
        modal[label] = (key, count)
    holder: dict[str, tuple[int, str]] = {}
    for label, (key, count) in sorted(modal.items()):
        if key not in holder or count > holder[key][0]:
            holder[key] = (count, label)
    return {label: key for label, (key, _) in modal.items() if holder[key][1] == label}


def categorisation_metrics(
    identify: Identify, decide: Decide, lines: Sequence[Labelled] = LABELLED
) -> CategorisationMetrics:
    """Score ``identify`` (text to merchant key and kind) and ``decide``.

    ``decide`` returns the category it would give the line and whether that is
    a fact (``True``) or only a suggestion; ``(None, False)`` for nothing.
    """
    misses: dict[str, list[str]] = defaultdict(list)
    identified = [(line, *identify(line.text)) for line in lines]
    decided = [(line, *decide(line)) for line in lines]

    known = [(line, cat, fact) for line, cat, fact in decided if line.group == "known"]
    judged_known = [item for item in known if item[0].category is not None]
    known_right = 0
    for line, category, fact in judged_known:
        if fact and category == line.category:
            known_right += 1
        else:
            said = f"{category} ({'fact' if fact else 'suggestion'})" if category else "nothing"
            misses["known_fact_right"].append(f"{line.text} [{line.category}] -> {said}")

    facts = [(line, cat) for line, cat, fact in decided if fact and cat is not None]
    facts_right = 0
    for line, category in facts:
        if category == line.category:
            facts_right += 1
        else:
            misses["fact_precision"].append(f"{line.text} [{line.category}] -> {category}")

    suggestions = [
        (line, cat)
        for line, cat, fact in decided
        if not fact and cat is not None and not (line.group == "known" and line.category is None)
    ]
    suggestions_right = 0
    for line, category in suggestions:
        if category == line.category:
            suggestions_right += 1
        else:
            misses["suggestion_precision"].append(f"{line.text} [{line.category}] -> {category}")

    invented = [line for line in lines if line.group == "invented"]
    invented_wrong = [
        f"{line.text} [{line.category}] -> {category}"
        for line, category in facts
        if line.group == "invented" and category != line.category
    ]
    misses["invented_wrong_fact"] = invented_wrong

    keys = [(line, key) for line, key, _ in identified]
    owners = _key_owners(keys)
    with_label = [(line, key) for line, key in keys if line.merchant is not None]
    keyed_right = 0
    for line, key in with_label:
        assert line.merchant is not None
        if key and owners.get(line.merchant) == key:
            keyed_right += 1
        else:
            misses["one_key_per_merchant"].append(f"{line.text} [{line.merchant}] -> {key}")

    other = [(line, kind) for line, _, kind in identified if line.group == "non_purchase"]
    kinds_right = 0
    for line, kind in other:
        if kind == line.kind:
            kinds_right += 1
        else:
            misses["kind_right"].append(f"{line.text} [{line.kind}] -> {kind}")

    leaks = 0
    for line, key in keys:
        named = {w for name in line.private for w in _words(name) if len(w) >= 3}
        if key and named & _words(key):
            leaks += 1
            misses["private_leaks"].append(f"{line.text} -> {key}")

    return CategorisationMetrics(
        known_fact_right=_ratio(known_right, len(judged_known)),
        fact_precision=_ratio(facts_right, len(facts)),
        suggestion_precision=_ratio(suggestions_right, len(suggestions)),
        invented_wrong_fact=_ratio(len(invented_wrong), len(invented)),
        fact_coverage=len(facts) / len(lines) if lines else 0.0,
        one_key_per_merchant=_ratio(keyed_right, len(with_label)),
        kind_right=_ratio(kinds_right, len(other)),
        private_leaks=leaks,
        misses=MappingProxyType({name: tuple(found) for name, found in misses.items()}),
    )


@dataclass(frozen=True)
class FoundSeries:
    """One series a detector reported, in the ledger's terms."""

    account: str
    #: Indexes into :attr:`Ledger.rows` of the charges it is made of.
    rows: frozenset[int]
    #: ``suggested``, ``active``, ``confirmed``, ``paused``, ``ended`` or ``dismissed``.
    status: str
    #: ``subscription``, ``bill``, ``income`` or ``other``; ``None`` if the detector has no kinds.
    kind: str | None = None
    #: The days it says the price changed.
    price_changes: tuple[date, ...] = ()


@dataclass(frozen=True)
class SubscriptionMetrics:
    planted: int
    found: float
    found_active: float
    decoys_active: int
    decoys_suggested: int
    kind_right: float | None
    price_changes_planted: int
    price_changes_flagged: int
    price_changes_false: int
    stopped_planted: int
    ended_right: int
    ended_wrong: int
    #: Field name to the series names behind the figure, for a failing target.
    misses: Mapping[str, tuple[str, ...]] = field(default_factory=dict, compare=False)


def _owner(series: FoundSeries, owner_of: Mapping[int, str]) -> str | None:
    """The planted series, decoy or neutral name that owns more than half of the rows."""
    counts = Counter(owner_of[i] for i in series.rows if i in owner_of)
    if not counts:
        return None
    name, count = min(counts.items(), key=lambda item: (-item[1], item[0]))
    return name if 2 * count > len(series.rows) else None


def subscription_metrics(found: Sequence[FoundSeries], ledger: Ledger) -> SubscriptionMetrics:
    """Score the series ``found`` against the ledger's planted series and decoys."""
    owner_of = {i: name for name, indexes in ledger.members.items() for i in indexes}
    by_owner: dict[str | None, list[FoundSeries]] = defaultdict(list)
    for series in found:
        owner = _owner(series, owner_of)
        # A series on the wrong account is not the planted one.
        accounts = {ledger.rows[i].account for i in series.rows}
        by_owner[owner if accounts <= {series.account} else None].append(series)

    misses: dict[str, list[str]] = defaultdict(list)
    found_count = active_count = kind_right = kinds_said = flagged = false_changes = 0
    ended_right = ended_wrong = 0
    for planted in ledger.planted:
        matches = by_owner.get(planted.name, [])
        stopped = planted.stopped_after is not None
        statuses = {m.status for m in matches}
        if statuses & (LIVE | {"ended"}):
            found_count += 1
            kinds_said += any(m.kind is not None for m in matches)
            if any(m.kind == planted.kind for m in matches):
                kind_right += 1
            else:
                misses["kind_right"].append(
                    f"{planted.name} -> {sorted({str(m.kind) for m in matches})}"
                )
        else:
            misses["found"].append(planted.name)
        if statuses & SHOWN_ACTIVE or (stopped and "ended" in statuses):
            active_count += 1
        else:
            misses["found_active"].append(f"{planted.name} -> {sorted(statuses)}")
        if "ended" in statuses:
            if stopped:
                ended_right += 1
            else:
                ended_wrong += 1
                misses["ended_wrong"].append(planted.name)
        elif stopped:
            misses["ended_right"].append(f"{planted.name} -> {sorted(statuses)}")
        reported = [day for m in matches for day in m.price_changes]
        on = planted.price_change_on
        good = [day for day in reported if on is not None and on <= day <= on + PRICE_CHANGE_WINDOW]
        if good:
            flagged += 1
        elif on is not None:
            misses["price_changes_flagged"].append(planted.name)
        if len(reported) > len(good):
            false_changes += len(reported) - len(good)
            misses["price_changes_false"].append(planted.name)

    planted_names = {p.name for p in ledger.planted}
    decoys_active = decoys_suggested = 0
    for owner, matches in sorted(by_owner.items(), key=lambda item: str(item[0])):
        if owner in planted_names or owner in NEUTRAL:
            continue
        # A decoy, or (owner None) series put together from rows of no series at all.
        groups = [matches] if owner is not None else [[m] for m in matches]
        for group in groups:
            statuses = {m.status for m in group}
            label = owner or f"stray on {group[0].account} ({len(group[0].rows)} rows)"
            if statuses & SHOWN_ACTIVE:
                decoys_active += 1
                misses["decoys_active"].append(label)
            elif "suggested" in statuses:
                decoys_suggested += 1
                misses["decoys_suggested"].append(label)
            changes = sum(len(m.price_changes) for m in group)
            if changes:
                false_changes += changes
                misses["price_changes_false"].append(label)

    total = len(ledger.planted)
    return SubscriptionMetrics(
        planted=total,
        found=found_count / total,
        found_active=active_count / total,
        decoys_active=decoys_active,
        decoys_suggested=decoys_suggested,
        # Not judged when the detector says no kind for anything it found.
        kind_right=_ratio(kind_right, found_count) if kinds_said else None,
        price_changes_planted=sum(1 for p in ledger.planted if p.price_change_on is not None),
        price_changes_flagged=flagged,
        price_changes_false=false_changes,
        stopped_planted=sum(1 for p in ledger.planted if p.stopped_after is not None),
        ended_right=ended_right,
        ended_wrong=ended_wrong,
        misses=MappingProxyType({name: tuple(names) for name, names in misses.items()}),
    )


# ---------------------------------------------------------------------------
# "Now": the clean-up, the dictionary, the engine and the detector as they are
# ---------------------------------------------------------------------------

#: The moment the figures are taken at: the last day of the planted ledger.
NOW: Final = datetime(synth.TODAY.year, synth.TODAY.month, synth.TODAY.day, tzinfo=UTC)
#: What a person files an invented shop under when its label names no category.
TAUGHT_WHEN_UNLABELLED: Final = "shopping.general"

#: A new household's categories: every default key, with an id of its own.
DEFAULT_CATEGORIES: Final[Mapping[str, uuid.UUID]] = MappingProxyType(
    {key: uuid.uuid5(uuid.NAMESPACE_OID, f"category:{key}") for key in sorted(CATEGORY_KEYS)}
)
_KEY_OF: Final[Mapping[uuid.UUID, str]] = MappingProxyType(
    {category_id: key for key, category_id in DEFAULT_CATEGORIES.items()}
)


def engine_identify(text: str) -> tuple[str | None, str]:
    """The merchant key and the kind the engine reads from ``text``."""
    from app.domain.categorisation import engine

    identity = engine.identify(text)
    return identity.key, identity.kind


def engine_decide(line: Labelled, learned: Learned | None = None) -> tuple[str | None, bool]:
    """What a new household's row with this descriptor is given, and whether as a fact.

    No rule, no payee and (unless ``learned`` is passed) nothing learned: the
    descriptor's kind, the dictionary and the keywords alone.
    """
    from app.domain.categorisation import engine

    decision = engine.decide(
        engine.identify(line.text),
        "in" if line.sign > 0 else "out",
        learned=learned,
        payee_default=None,
        categories=DEFAULT_CATEGORIES,
        now=NOW,
    )
    if decision.category_id is not None:
        return _KEY_OF[decision.category_id], True
    if decision.suggested_category_id is not None:
        return _KEY_OF[decision.suggested_category_id], False
    return None, False


@dataclass(frozen=True)
class TeachingMetrics:
    #: Invented merchants printed twice.
    merchants: int
    #: Those whose second line got the taught category as a fact.
    right: int
    misses: tuple[str, ...] = field(default=(), compare=False)

    @property
    def taught_right(self) -> float | None:
        return _ratio(self.right, self.merchants)


def teaching_metrics(lines: Sequence[Labelled] = LABELLED) -> TeachingMetrics:
    """One correction, then the same merchant again.

    For each invented merchant printed twice, a person files the first line
    (under the label's category, or :data:`TAUGHT_WHEN_UNLABELLED`). The
    memory then holds what ``engine.person_decided`` records: one decision
    for that line's merchant key and direction, if its descriptor is of a
    teachable kind. The second line is right when the engine states the
    taught category as a fact.
    """
    from app.domain.categorisation import cleanup, engine
    from app.domain.categorisation.memory import Learned

    by_merchant: dict[str, list[Labelled]] = defaultdict(list)
    for line in lines:
        if line.group == "invented" and line.merchant is not None:
            by_merchant[line.merchant].append(line)
    pairs = {name: found for name, found in by_merchant.items() if len(found) >= 2}
    right = 0
    misses: list[str] = []
    for name, (first, second, *_) in sorted(pairs.items()):
        taught = first.category or TAUGHT_WHEN_UNLABELLED
        corrected = engine.identify(first.text)
        again = engine.identify(second.text)
        learned = None
        if (
            corrected.key
            and corrected.kind in cleanup.TEACHABLE
            and corrected.key == again.key
            and first.sign == second.sign
        ):
            learned = Learned(
                memory_id=uuid.uuid5(uuid.NAMESPACE_OID, f"memory:{name}"),
                category_id=DEFAULT_CATEGORIES[taught],
                decisions=1,
                total=1,
                share=Decimal(1),
                last_decided_at=NOW,
                merchant_key=corrected.key,
                direction="in" if first.sign > 0 else "out",
            )
        answer = engine_decide(second, learned)
        if answer == (taught, True):
            right += 1
        else:
            said = f"{answer[0]} ({'fact' if answer[1] else 'suggestion'})" if answer[0] else None
            misses.append(
                f"{first.text} [{corrected.key}] then {second.text} [{again.key}] "
                f"[{taught}] -> {said}"
            )
    return TeachingMetrics(merchants=len(pairs), right=right, misses=tuple(misses))


def _months(first: date, last: date) -> list[tuple[int, int]]:
    out = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        out.append((year, month))
        year, month = (year, month + 1) if month < 12 else (year + 1, 1)
    return out


def replay(home: Home, ledger: Ledger, *, filed: bool) -> list[FoundSeries]:
    """The ledger through the real pipeline, as a household would live it.

    Each account's rows are uploaded as one CSV per month (the ingest job
    imports and categorises them), and the daily detection job runs on the
    last day of every month. Nobody decides anything about a series.

    ``filed``: after each upload the household's person files, in the grid,
    every new row the engine gave no category whose :attr:`Row.category` names
    one (the ledger's label is "what a person chose for a local business").
    What the engine stated is left as it is, right or wrong, and a row the
    ledger labels with nothing stays open. The corrections teach the memory,
    so most merchants are filed once. Without it nobody files anything.

    Returns the series the household holds at the end, each with the ledger
    rows linked to it and the days of its ``price_changed`` events.
    """
    from sqlalchemy import select

    from app.domain.transactions.models import Transaction
    from tests.fixtures.pipeline import Line, squash

    household_id = home.household_id
    accounts = {
        name: home.ledger.account(household_id, name=name.title(), currency="USD")
        for name in ledger.coverage
    }
    name_of = {account.id: name for name, account in accounts.items()}
    by_month: dict[tuple[int, int, str], list[int]] = defaultdict(list)
    for index, planted in enumerate(ledger.rows):
        by_month[(planted.posted.year, planted.posted.month, planted.account)].append(index)
    # Ledger rows that wait to be matched to an imported row: by account and line.
    waiting: dict[tuple[str, date, Decimal, str], list[int]] = defaultdict(list)
    index_of: dict[uuid.UUID, int] = {}
    category_ids: dict[str, uuid.UUID] = {}
    for year, month in _months(synth.START, synth.TODAY):
        for name, account in accounts.items():
            indexes = by_month.get((year, month, name), [])
            if not indexes:
                continue
            lines = [
                Line(ledger.rows[index].posted, ledger.rows[index].amount, ledger.rows[index].text)
                for index in indexes
            ]
            for index, line in zip(indexes, lines, strict=True):
                waiting[(name, line.posted, Decimal(line.amount), squash(line.text))].append(index)
            document_id, summary = home.upload(account, lines, f"{name}-{year}-{month:02d}.csv")
            assert summary["imported"] == len(lines), (name, year, month, summary)
            imported: list[Transaction] = home.query(
                select(Transaction).where(Transaction.source_document_id == document_id)
            )
            for stored in imported:
                index_of[stored.id] = waiting[
                    (name, stored.posted_date, stored.amount, squash(stored.raw_payee))
                ].pop()
            for stored in imported if filed else ():
                label = ledger.rows[index_of[stored.id]].category
                if stored.category_id is None and label is not None:
                    if label not in category_ids:
                        category_ids[label] = home.category(label).id
                    home.choose(stored.id, category_ids[label])
        home.detect(date(year, month, calendar.monthrange(year, month)[1]))

    linked: dict[uuid.UUID, set[int]] = defaultdict(set)
    charges: list[Transaction] = home.query(
        select(Transaction).where(
            Transaction.household_id == household_id,
            Transaction.scheduled_transaction_id.is_not(None),
        )
    )
    for charge in charges:
        assert charge.scheduled_transaction_id is not None
        linked[charge.scheduled_transaction_id].add(index_of[charge.id])
    prices: dict[uuid.UUID, list[date]] = defaultdict(list)
    for event in home.changes():
        if event.kind == "price_changed":
            prices[event.schedule_id].append(event.occurred_on)
    return [
        FoundSeries(
            account=name_of[row.account_id],
            rows=frozenset(linked[row.id]),
            status=row.status,
            kind=row.kind,
            price_changes=tuple(prices[row.id]),
        )
        for row in home.series()
    ]


@contextmanager
def scratch_home() -> Iterator[Home]:
    """A new household in a scratch database, for a run outside pytest."""
    import pytest

    from app.db.session import make_sessionmaker
    from tests.fixtures.factories import Factories
    from tests.fixtures.ledger import LedgerFactories
    from tests.fixtures.pipeline import open_home
    from tests.fixtures.scratch_db import scratch_instance

    with tempfile.TemporaryDirectory() as tmp, scratch_instance() as instance:
        settings = instance.settings(instance.source, Path(tmp))
        engine = instance.source.engine()
        try:
            factories = Factories(make_sessionmaker(engine), settings)
            ledger = LedgerFactories(factories.session_factory)
            with (
                pytest.MonkeyPatch.context() as patch,
                open_home(factories, ledger, settings, Path(tmp), patch) as home,
            ):
                yield home
        finally:
            engine.dispose()


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def categorisation_report(metrics: CategorisationMetrics) -> list[str]:
    return [
        f"Known merchants given the right category as a fact: {_percent(metrics.known_fact_right)}",
        f"Facts that are right (precision), whole set: {_percent(metrics.fact_precision)}",
        f"Suggestions that are right: {_percent(metrics.suggestion_precision)}",
        f"Invented merchants given a wrong fact: {_percent(metrics.invented_wrong_fact)}",
        f"All lines with a fact: {_percent(metrics.fact_coverage)}",
        f"Lines of one merchant sharing one key: {_percent(metrics.one_key_per_merchant)}",
        f"Non-purchase kind recognised: {_percent(metrics.kind_right)}",
        f"A person's name in a merchant key: {metrics.private_leaks} lines",
    ]


def teaching_report(metrics: TeachingMetrics) -> list[str]:
    return [
        f"Merchant repeated after one correction: right: {_percent(metrics.taught_right)} "
        f"({metrics.right} of {metrics.merchants})"
    ]


def subscription_report(metrics: SubscriptionMetrics) -> list[str]:
    return [
        f"Planted series found (active or suggested): {_percent(metrics.found)}",
        f"Planted series found as active: {_percent(metrics.found_active)}",
        f"Decoys shown as active / as suggested: "
        f"{metrics.decoys_active} / {metrics.decoys_suggested}",
        f"Series kind right: {_percent(metrics.kind_right)}",
        f"Planted price changes flagged / false ones: "
        f"{metrics.price_changes_flagged} of {metrics.price_changes_planted} / "
        f"{metrics.price_changes_false}",
        f"Stopped series ended / ended wrongly: "
        f"{metrics.ended_right} of {metrics.stopped_planted} / {metrics.ended_wrong}",
    ]


def _misses(misses: Mapping[str, Sequence[str]]) -> list[str]:
    return [
        f"    {name}: {miss}"
        for name, found in sorted(misses.items())
        for miss in found[:MISSES_SHOWN]
    ]


def now(*, subscriptions: bool = True, misses: bool = False) -> list[str]:
    """The figures for the code as it is now, as lines to print."""
    categorised = categorisation_metrics(engine_identify, engine_decide)
    taught = teaching_metrics()
    lines = [
        f"Categorisation now ({len(LABELLED)} labelled descriptors):",
        *(f"  {text}" for text in categorisation_report(categorised)),
        *(f"  {text}" for text in teaching_report(taught)),
    ]
    if misses:
        lines += _misses({**categorised.misses, "taught_right": taught.misses})
    if subscriptions:
        ledger = synth.build()
        lines.append(
            f"Subscriptions now ({len(ledger.rows)} rows, {len(ledger.planted)} planted series, "
            f"{len(ledger.decoys)} decoys, imported month by month):"
        )
        for filed, who in (
            (True, "a person files the rows the engine left open (the targets)"),
            (False, "nobody files anything"),
        ):
            with scratch_home() as home:
                found = subscription_metrics(replay(home, ledger, filed=filed), ledger)
            lines.append(f"  {who}:")
            lines += [f"    {text}" for text in subscription_report(found)]
            if misses:
                lines += [f"  {miss}" for miss in _misses(found.misses)]
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--now", action="store_true", required=True)
    parser.add_argument(
        "--no-subscriptions",
        action="store_true",
        help="the descriptor figures only (these need no database)",
    )
    parser.add_argument("--misses", action="store_true", help="also print what was missed")
    args = parser.parse_args(argv)
    print("\n".join(now(subscriptions=not args.no_subscriptions, misses=args.misses)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
