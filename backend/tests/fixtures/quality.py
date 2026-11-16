# SPDX-License-Identifier: AGPL-3.0-or-later
"""Measuring categorisation and subscription detection against the labelled sets.

:func:`categorisation_metrics` scores an ``identify`` and a ``decide``
function on ``tests/fixtures/descriptors.py``; :func:`subscription_metrics`
scores the series a detector found on ``synth_subscription_ledger.py``. The
fields are the rows of the design note's table (section 9). Ratios are
``None`` when there was nothing to judge (no fact stated, no series found).

``uv run python -m tests.fixtures.quality --today`` prints both for the code
as it is before the dictionary, the engine and the new detector exist: the
1,200 lines are imported as a CSV into an empty household of a scratch
database and their categories read back, and the ledger goes through the
current ``recurring.detection.detect``. It needs PostgreSQL
(``PERFIN_TEST_DATABASE_URL``), as the integration tests do.

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
import re
import tempfile
import uuid
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Final

from tests.fixtures import synth_subscription_ledger as synth
from tests.fixtures.descriptors import LABELLED, Labelled
from tests.fixtures.synth_subscription_ledger import Ledger

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
# "Today": the code before the dictionary, the engine and the new detector
# ---------------------------------------------------------------------------


def _squash(text: str) -> str:
    return " ".join(text.split())


def today_categories() -> dict[str, tuple[str | None, bool]]:
    """Import the labelled lines as one CSV into a new household; what each line got.

    Squashed descriptor text to (category ``system_key``, is it a fact). A
    line the importer did not write is absent.
    """
    from sqlalchemy import select

    from app.db.rls import set_context
    from app.db.session import make_sessionmaker
    from app.domain.imports import service as imports_service
    from app.domain.transactions.categories import seed_default_categories
    from app.domain.transactions.models import Category, Transaction
    from tests.fixtures.factories import Factories
    from tests.fixtures.imports import make_extracted_document, make_import_mapping, table_result
    from tests.fixtures.ledger import LedgerFactories
    from tests.fixtures.scratch_db import scratch_instance

    start = date(2026, 1, 1)
    cells = [["Date", "Description", "Amount"]]
    for index, line in enumerate(LABELLED):
        day = start + timedelta(days=index % 270)
        amount = line.sign * (1000 + index) / 100
        cells.append([f"{day.month:02d}/{day.day:02d}/{day.year}", line.text, f"{amount:.2f}"])
    spec = {"date_column": 0, "date_format": "MM/DD/YYYY", "payee_column": 1, "amount_column": 2}

    with tempfile.TemporaryDirectory() as tmp, scratch_instance() as instance:
        settings = instance.settings(instance.source, Path(tmp))
        engine = instance.source.engine()
        try:
            factories = Factories(make_sessionmaker(engine), settings)
            owner = factories.user(role="owner")
            household_id = owner.household_id
            assert household_id is not None
            account = LedgerFactories(factories.session_factory).account(household_id)
            result = table_result(cells)
            with factories.transaction() as db:
                set_context(db, household_id)
                seed_default_categories(db, household_id)
                mapping = make_import_mapping(db, household_id, spec=spec, rows=cells)
                document = make_extracted_document(
                    db, household_id, account=account, result=result, uploaded_by=owner.user.id
                )
                imports_service.import_extraction(
                    db,
                    document,
                    result,
                    mapping_id=mapping.id,
                    account_id=account.id,
                    user_id=owner.user.id,
                )
            with factories.transaction() as db:
                set_context(db, household_id)
                keys: dict[uuid.UUID, str | None] = {}
                for found_id, key in db.execute(select(Category.id, Category.system_key)):
                    keys[found_id] = key
                out: dict[str, tuple[str | None, bool]] = {}
                for raw, category_id, suggested_id in db.execute(
                    select(
                        Transaction.raw_payee,
                        Transaction.category_id,
                        Transaction.suggested_category_id,
                    ).where(Transaction.account_id == account.id)
                ):
                    if category_id is not None:
                        answer: tuple[str | None, bool] = (keys.get(category_id), True)
                    elif suggested_id is not None:
                        answer = (keys.get(suggested_id), False)
                    else:
                        answer = (None, False)
                    out[_squash(raw or "")] = answer
                return out
        finally:
            engine.dispose()


#: The kind of a default category, by the group its key starts with.
_GROUP_KINDS: Final = MappingProxyType(
    {"income": "income", "transfers": "transfer", "excluded": "ignore"}
)


def today_series(ledger: Ledger) -> list[FoundSeries]:
    """The ledger through the detector as it is, with the categories its rows are labelled with.

    Each row is described the way ``recurring.service`` describes a ledger
    row: who it names (``engine.identify``), what the dictionary says about
    that merchant, and its category.
    """
    from app.domain.categorisation import engine
    from app.domain.recurring.detection import Observed, detect, normalize_payee

    accounts = {name: uuid.uuid5(uuid.NAMESPACE_OID, name) for name in ledger.coverage}
    names = {account_id: name for name, account_id in accounts.items()}
    categories: dict[str, uuid.UUID] = {}
    index_of: dict[uuid.UUID, int] = {}
    observed = []
    for index, row in enumerate(ledger.rows):
        row_id = uuid.uuid5(uuid.NAMESPACE_OID, f"row:{index}")
        index_of[row_id] = index
        category_id = None
        category_kind = None
        if row.category is not None:
            category_id = categories.setdefault(
                row.category, uuid.uuid5(uuid.NAMESPACE_OID, row.category)
            )
            category_kind = _GROUP_KINDS.get(row.category.partition(".")[0], "expense")
        identity = engine.identify(row.text)
        merchant = identity.hit.merchant if identity.hit is not None else None
        observed.append(
            Observed(
                id=row_id,
                account_id=accounts[row.account],
                posted_date=row.posted,
                amount=row.amount,
                merchant_key=identity.key,
                text_key=normalize_payee(row.text) or None,
                kind=identity.kind,
                category_key=row.category,
                category_kind=category_kind,
                series_hint=merchant.series if merchant is not None else None,
                category_id=category_id,
                label=row.text,
                cadence_hints=frozenset(merchant.cadences) if merchant is not None else frozenset(),
            )
        )
    clock = {accounts[name]: day for name, day in ledger.coverage.items()}
    return [
        FoundSeries(
            account=names[candidate.account_id],
            rows=frozenset(index_of[i] for i in candidate.transaction_ids),
            status=candidate.status,
            kind=candidate.kind,
        )
        for candidate in detect(observed, clock)
    ]


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


def today() -> list[str]:
    """Both sets of figures for the code as it is now, as lines to print."""
    from app.domain.categorisation import engine

    got = today_categories()

    def identify(text: str) -> tuple[str | None, str]:
        identity = engine.identify(text)
        return identity.key, identity.kind

    def decide(line: Labelled) -> tuple[str | None, bool]:
        return got.get(_squash(line.text), (None, False))

    ledger = synth.build()
    lines = [
        f"Categorisation today ({len(LABELLED)} labelled descriptors, {len(got)} imported):",
        *(f"  {text}" for text in categorisation_report(categorisation_metrics(identify, decide))),
        f"Subscriptions today ({len(ledger.rows)} rows, {len(ledger.planted)} planted series, "
        f"{len(ledger.decoys)} decoys):",
        *(
            f"  {text}"
            for text in subscription_report(subscription_metrics(today_series(ledger), ledger))
        ),
    ]
    return lines


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--today", action="store_true", required=True)
    parser.parse_args(argv)
    print("\n".join(today()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
