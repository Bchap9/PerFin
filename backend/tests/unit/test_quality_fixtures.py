# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two labelled sets that measure categorisation and subscription detection.

These tests pin the shape of the yardstick (``tests/fixtures/descriptors.py``,
``synth_subscription_ledger.py``) and the arithmetic of the measuring
functions (``quality.py``). The targets themselves become tests once the
dictionary, the clean-up and the detector exist.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import timedelta
from typing import get_args

from app.domain.categorisation.cleanup import DescriptorKind
from app.domain.transactions.models import PAYEE_MAX
from tests.fixtures import descriptors, quality
from tests.fixtures import synth_subscription_ledger as synth
from tests.fixtures.descriptors import LABELLED, Labelled
from tests.fixtures.quality import FoundSeries
from tests.fixtures.synth_tax_forms import identity_tokens_in

KINDS = frozenset(get_args(DescriptorKind))
_WORD = re.compile(r"[a-z0-9]+")


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def test_the_descriptor_set_has_its_shape() -> None:
    groups = Counter(line.group for line in LABELLED)
    assert groups == {"known": 800, "invented": 250, "non_purchase": 150}
    texts = [line.text for line in LABELLED]
    assert len(set(texts)) == len(texts), "a descriptor is repeated"
    assert all(0 < len(text) <= PAYEE_MAX for text in texts)
    assert all(text == text.strip() for text in texts)
    for line in LABELLED:
        assert line.category is None or line.category in descriptors.CATEGORY_KEYS, line
        assert line.kind in KINDS, line
        assert line.sign in (-1, 1), line
        # The tax tests scan the shared database for these; no fixture may print one.
        assert identity_tokens_in(line.text) == [], line

    known = [line for line in LABELLED if line.group == "known"]
    assert all(line.merchant for line in known)
    shapes: dict[str, set[str]] = defaultdict(set)
    for line in known:
        assert line.merchant is not None
        shapes[line.merchant].add(descriptors.SHAPE_OF[line.text])
    assert len(shapes) >= 250
    assert set().union(*shapes.values()) == set(descriptors.SHAPES)
    thin = sorted(name for name, seen in shapes.items() if len(seen) < 2)
    assert thin == [], "a known merchant is printed in fewer than two shapes"
    # One merchant, one category: the label is who was paid, not what was bought.
    per_merchant: dict[str, set[str | None]] = defaultdict(set)
    for line in known:
        assert line.merchant is not None
        per_merchant[line.merchant].add(line.category)
    assert all(len(found) == 1 for found in per_merchant.values())
    # Most known lines can be judged; the rest are stores that sell everything.
    assert sum(1 for line in known if line.category is None) <= 80

    invented = [line for line in LABELLED if line.group == "invented"]
    assert all(line.merchant for line in invented)
    assert Counter(Counter(line.merchant for line in invented).values()) == {2: 125}
    telling = {line.merchant for line in invented if line.category is not None}
    assert 60 <= len(telling) <= 65
    assert not {line.merchant for line in invented} & set(shapes)

    other = [line for line in LABELLED if line.group == "non_purchase"]
    kinds = Counter(line.kind for line in other)
    assert set(kinds) == {
        "transfer",
        "card_payment",
        "loan_payment",
        "cash",
        "fee",
        "interest",
        "payroll",
        "refund",
        "check",
        "deposit",
        "p2p",
    }
    assert min(kinds.values()) >= 8
    # Never guessed: a cheque, a deposit, money between people.
    for line in other:
        if line.kind in {"check", "deposit", "p2p"}:
            assert line.category is None, line
        else:
            assert line.category is not None, line
    assert all(line.private for line in other if line.kind == "p2p")


def test_no_private_name_is_a_merchant_label() -> None:
    labels: set[str] = set()
    for line in LABELLED:
        if line.merchant is not None:
            labels |= _words(line.merchant)
    private = [line for line in LABELLED if line.private]
    assert len(private) >= 75
    for line in private:
        for name in line.private:
            words = _words(name)
            assert len(words) >= 2, name
            assert not words & labels, f"{name} shares a word with a merchant label"
            # The name is really printed in the line it is said to be in.
            assert words <= _words(line.text), line


def test_the_ledger_is_deterministic() -> None:
    assert synth.build() == synth.build()


def test_the_ledger_has_its_plants_and_decoys() -> None:
    ledger = synth.build()
    assert len(ledger.planted) == 40
    assert len(ledger.decoys) == 25
    names = [p.name for p in ledger.planted] + [d.name for d in ledger.decoys]
    assert len(set(names)) == len(names)
    assert all(ledger.members[name] for name in names), "a series has no row"
    accounts = set(ledger.coverage)
    assert {row.account for row in ledger.rows} == accounts
    for row in ledger.rows:
        assert row.amount != 0
        assert row.posted <= ledger.coverage[row.account] <= synth.TODAY
        assert row.posted >= synth.TODAY - timedelta(days=731)
        assert row.category is None or row.category in descriptors.CATEGORY_KEYS
        assert 0 < len(row.text) <= PAYEE_MAX
        assert identity_tokens_in(row.text) == [], row
    for planted in ledger.planted:
        rows = [ledger.rows[i] for i in ledger.members[planted.name]]
        assert {row.account for row in rows} == {planted.account}
        if planted.stopped_after is not None:
            assert max(row.posted for row in rows) == planted.stopped_after
            # Stopped for good: the account has later rows, so it is not a late statement.
            assert ledger.coverage[planted.account] - planted.stopped_after > timedelta(days=100)
        if planted.price_change_on is not None:
            before = {r.amount for r in rows if r.posted < planted.price_change_on}
            after = {r.amount for r in rows if r.posted >= planted.price_change_on}
            assert len(before) == 1
            assert len(after) == 1
            assert before != after

    # One account stopped being uploaded three months ago; its series ran on.
    late = [a for a, last in ledger.coverage.items() if last < synth.TODAY - timedelta(days=90)]
    assert len(late) == 1
    assert max(r.posted for r in ledger.rows if r.account == late[0]) == ledger.coverage[late[0]]
    on_late = [p for p in ledger.planted if p.account == late[0]]
    assert len(on_late) >= 5
    assert all(p.stopped_after is None for p in on_late)

    # The mix the design note asks for.
    cadences = Counter(p.cadence for p in ledger.planted)
    assert cadences["annual"] == 4
    assert cadences["weekly"] == 3
    assert cadences["quarterly"] == 2
    assert cadences["two_monthly"] == 1
    assert sum(1 for p in ledger.planted if p.price_change_on is not None) == 5
    assert sum(1 for p in ledger.planted if p.stopped_after is not None) == 4
    assert sum(1 for p in ledger.planted if p.expect == "suggested") <= 4
    once = [p for p in ledger.planted if len(ledger.members[p.name]) == 1]
    assert [p.cadence for p in once] == ["annual"]
    assert {p.kind for p in ledger.planted} == {"subscription", "bill"}
    # Two plans at one merchant: the same text on the same account, two amounts.
    texts = Counter(
        (p.account, frozenset(ledger.rows[i].text for i in ledger.members[p.name]))
        for p in ledger.planted
    )
    assert max(texts.values()) == 2
    # No row belongs to two series.
    owned = [i for name in names for i in ledger.members[name]]
    assert len(set(owned)) == len(owned)


def _perfect_identify(text: str) -> tuple[str | None, str]:
    line = _BY_TEXT[text]
    key = " ".join(sorted(_words(line.merchant))) if line.merchant else None
    return key, line.kind


_BY_TEXT = {line.text: line for line in LABELLED}


def test_metrics_on_a_perfect_and_an_empty_answer() -> None:
    def perfect(line: Labelled) -> tuple[str | None, bool]:
        return line.category, line.category is not None

    best = quality.categorisation_metrics(_perfect_identify, perfect)
    assert best.known_fact_right == 1.0
    assert best.fact_precision == 1.0
    assert best.invented_wrong_fact == 0.0
    assert best.one_key_per_merchant == 1.0
    assert best.kind_right == 1.0
    assert best.private_leaks == 0
    assert best.suggestion_precision is None  # nothing was only suggested
    labelled = sum(1 for line in LABELLED if line.category is not None)
    assert best.fact_coverage == labelled / len(LABELLED)
    assert best.fact_coverage >= 0.60  # the target can be reached at all

    def nothing(line: Labelled) -> tuple[str | None, bool]:
        return None, False

    worst = quality.categorisation_metrics(lambda text: (None, "unknown"), nothing)
    assert worst.fact_coverage == 0.0
    assert worst.known_fact_right == 0.0
    assert worst.fact_precision is None
    assert worst.suggestion_precision is None
    assert worst.invented_wrong_fact == 0.0
    assert worst.one_key_per_merchant == 0.0
    assert worst.kind_right == 0.0
    assert worst.private_leaks == 0


def test_metrics_count_what_is_wrong() -> None:
    def everything_is_groceries(line: Labelled) -> tuple[str | None, bool]:
        return "food.groceries", line.group != "invented"

    def one_key(text: str) -> tuple[str | None, str]:
        # Every line under one key, and the whole text in it: both must cost.
        low = " ".join(_WORD.findall(text.lower()))
        return (low[:64] if "zelle" in low else "shop"), "purchase"

    got = quality.categorisation_metrics(one_key, everything_is_groceries)
    assert got.fact_coverage == 950 / 1200
    assert got.fact_precision is not None
    assert got.fact_precision < 0.10
    assert got.suggestion_precision is not None
    assert got.suggestion_precision < 0.10
    assert got.invented_wrong_fact == 0.0
    # One key for every merchant is no key at all: only the largest label keeps it.
    assert got.one_key_per_merchant is not None
    assert got.one_key_per_merchant < 0.05
    assert got.kind_right == 0.0
    assert got.private_leaks > 0
    assert got.misses["fact_precision"]


def test_subscription_metrics_on_a_perfect_and_an_empty_answer() -> None:
    ledger = synth.build()
    nothing = quality.subscription_metrics([], ledger)
    assert (nothing.found, nothing.found_active) == (0.0, 0.0)
    assert (nothing.decoys_active, nothing.decoys_suggested) == (0, 0)
    assert nothing.kind_right is None
    assert (nothing.price_changes_flagged, nothing.price_changes_planted) == (0, 5)
    assert (nothing.ended_right, nothing.stopped_planted) == (0, 4)

    perfect = [
        FoundSeries(
            account=p.account,
            rows=frozenset(ledger.members[p.name]),
            status="ended" if p.stopped_after else p.expect,
            kind=p.kind,
            price_changes=(p.price_change_on,) if p.price_change_on else (),
        )
        for p in ledger.planted
    ]
    best = quality.subscription_metrics(perfect, ledger)
    assert best.found == 1.0
    assert best.found_active >= 0.80
    assert (best.decoys_active, best.decoys_suggested) == (0, 0)
    assert best.kind_right == 1.0
    assert (best.price_changes_flagged, best.price_changes_false) == (5, 0)
    assert (best.ended_right, best.ended_wrong) == (4, 0)

    # Every decoy shown, every series ended, a price change everywhere.
    decoy = ledger.decoys[0]
    wrong = [
        FoundSeries(decoy.account, frozenset(ledger.members[decoy.name]), "active"),
        FoundSeries(
            ledger.decoys[1].account, frozenset(ledger.members[ledger.decoys[1].name]), "suggested"
        ),
        *(
            FoundSeries(
                account=p.account,
                rows=frozenset(ledger.members[p.name]),
                status="ended",
                kind="other",
                price_changes=(synth.TODAY,),
            )
            for p in ledger.planted
        ),
    ]
    worst = quality.subscription_metrics(wrong, ledger)
    assert (worst.decoys_active, worst.decoys_suggested) == (1, 1)
    assert worst.kind_right == 0.0
    assert worst.price_changes_flagged == 0
    assert worst.price_changes_false == 40
    assert (worst.ended_right, worst.ended_wrong) == (4, 36)
    assert worst.found_active == 4 / 40


def test_a_series_that_merges_two_plans_finds_one_of_them() -> None:
    ledger = synth.build()
    plans = [p for p in ledger.planted if p.name.startswith("apple-")]
    assert len(plans) == 2
    merged = FoundSeries(
        plans[0].account,
        frozenset(ledger.members[plans[0].name]) | frozenset(ledger.members[plans[1].name]),
        "active",
        "subscription",
    )
    got = quality.subscription_metrics([merged], ledger)
    assert got.found <= 1 / 40
