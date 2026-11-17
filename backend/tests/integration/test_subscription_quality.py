# SPDX-License-Identifier: AGPL-3.0-or-later
"""The subscription targets of the design note (section 9), as gates.

The planted ledger (``tests/fixtures/synth_subscription_ledger.py``: 40
series, 25 decoys, 24 months, three accounts) goes through the real pipeline
(``quality.replay``): one CSV per account and month through the ingest job
and the daily detection job at the end of every month. What the household
then holds is scored by ``quality.subscription_metrics``.

It is replayed twice, and both runs are gates:

- **filed**: the household's person files what the engine left open the way
  the ledger says they would. The targets of the design note are measured on
  this run, one test per row of its table.
- **unfiled**: nobody files anything, which is what a household sees right
  after its first import. Every bar is the same but one: the kind of a
  series is held at the figure reached (:data:`KIND_RIGHT_UNFILED_MIN`).
  Rent, water, a sewer and tuition at businesses no dictionary knows are
  bills by the category the engine suggests for their rows (the owner's
  decision for 1.0.0); a car loan whose entry says only ``RETAIL PMT``, a
  local climbing gym and a swim school suggest nothing that says a kind and
  stay ``other`` until somebody files a row. The 90% target is met on this
  run too.

A failing target prints the series behind it.

Each replay takes about a minute, so each runs once for the module (once per
worker under ``pytest -n``).

A threshold here is never lowered to make a change pass.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from app.core.settings import Settings
from tests.fixtures import quality
from tests.fixtures import synth_subscription_ledger as synth
from tests.fixtures.factories import Factories
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.pipeline import open_home
from tests.fixtures.quality import MISSES_SHOWN, SubscriptionMetrics

pytestmark = pytest.mark.integration

FOUND_MIN = 0.90
FOUND_ACTIVE_MIN = 0.80
DECOYS_ACTIVE_MAX = 0
DECOYS_SUGGESTED_MAX = 2
KIND_RIGHT_MIN = 0.90
PRICE_CHANGES_FALSE_MAX = 0
ENDED_WRONG_MAX = 0

#: Series kind right when nobody files anything: 37 of the 40 planted series
#: (module doc). Raised when the figure rises; never lowered.
KIND_RIGHT_UNFILED_MIN = 0.925

_measured: dict[bool, SubscriptionMetrics] = {}


def _measure(
    filed: bool,
    factories: Factories,
    ledger: LedgerFactories,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SubscriptionMetrics:
    if filed not in _measured:
        planted = synth.build()
        with open_home(factories, ledger, settings, tmp_path, monkeypatch) as home:
            found = quality.replay(home, planted, filed=filed)
        _measured[filed] = quality.subscription_metrics(found, planted)
    return _measured[filed]


@pytest.fixture
def metrics(
    factories: Factories,
    ledger: LedgerFactories,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SubscriptionMetrics:
    """The run the targets are set for: a person files what the engine left open."""
    return _measure(True, factories, ledger, settings, tmp_path, monkeypatch)


@pytest.fixture
def unfiled(
    factories: Factories,
    ledger: LedgerFactories,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> SubscriptionMetrics:
    """The same ledger with nobody filing anything."""
    return _measure(False, factories, ledger, settings, tmp_path, monkeypatch)


def _report(name: str, value: object, metrics: SubscriptionMetrics, *others: str) -> str:
    misses: Sequence[str] = [
        miss for field in (name, *others) for miss in metrics.misses.get(field, ())
    ]
    shown = "\n".join(f"  {miss}" for miss in misses[:MISSES_SHOWN])
    return f"{name} is {value}; behind it:\n{shown}"


def test_the_ledger_is_the_one_the_targets_were_set_for(metrics: SubscriptionMetrics) -> None:
    assert (metrics.planted, metrics.price_changes_planted, metrics.stopped_planted) == (40, 5, 4)


def test_planted_series_are_found(metrics: SubscriptionMetrics) -> None:
    assert metrics.found >= FOUND_MIN, _report("found", metrics.found, metrics)


def test_planted_series_are_found_as_active(metrics: SubscriptionMetrics) -> None:
    assert metrics.found_active >= FOUND_ACTIVE_MIN, _report(
        "found_active", metrics.found_active, metrics
    )


def test_no_decoy_is_shown_as_active(metrics: SubscriptionMetrics) -> None:
    assert metrics.decoys_active <= DECOYS_ACTIVE_MAX, _report(
        "decoys_active", metrics.decoys_active, metrics
    )


def test_few_decoys_are_suggested(metrics: SubscriptionMetrics) -> None:
    assert metrics.decoys_suggested <= DECOYS_SUGGESTED_MAX, _report(
        "decoys_suggested", metrics.decoys_suggested, metrics
    )


def test_the_kind_of_a_series_is_right(metrics: SubscriptionMetrics) -> None:
    assert metrics.kind_right is not None
    assert metrics.kind_right >= KIND_RIGHT_MIN, _report("kind_right", metrics.kind_right, metrics)


def test_every_planted_price_change_is_flagged(metrics: SubscriptionMetrics) -> None:
    assert metrics.price_changes_flagged == metrics.price_changes_planted, _report(
        "price_changes_flagged", metrics.price_changes_flagged, metrics
    )


def test_no_price_change_is_made_up(metrics: SubscriptionMetrics) -> None:
    assert metrics.price_changes_false <= PRICE_CHANGES_FALSE_MAX, _report(
        "price_changes_false", metrics.price_changes_false, metrics
    )


def test_every_stopped_series_is_ended(metrics: SubscriptionMetrics) -> None:
    assert metrics.ended_right == metrics.stopped_planted, _report(
        "ended_right", metrics.ended_right, metrics
    )


def test_no_running_series_is_ended(metrics: SubscriptionMetrics) -> None:
    assert metrics.ended_wrong <= ENDED_WRONG_MAX, _report(
        "ended_wrong", metrics.ended_wrong, metrics
    )


# ---------------------------------------------------------------------------
# Nobody files anything
# ---------------------------------------------------------------------------


def test_unfiled_planted_series_are_found(unfiled: SubscriptionMetrics) -> None:
    assert unfiled.found >= FOUND_MIN, _report("found", unfiled.found, unfiled)
    assert unfiled.found_active >= FOUND_ACTIVE_MIN, _report(
        "found_active", unfiled.found_active, unfiled
    )


def test_unfiled_decoys_stay_out(unfiled: SubscriptionMetrics) -> None:
    assert unfiled.decoys_active <= DECOYS_ACTIVE_MAX, _report(
        "decoys_active", unfiled.decoys_active, unfiled
    )
    assert unfiled.decoys_suggested <= DECOYS_SUGGESTED_MAX, _report(
        "decoys_suggested", unfiled.decoys_suggested, unfiled
    )


def test_unfiled_the_kind_of_a_series_does_not_get_worse(unfiled: SubscriptionMetrics) -> None:
    # Under the target of KIND_RIGHT_MIN, and said so (module doc).
    assert unfiled.kind_right is not None
    assert unfiled.kind_right >= KIND_RIGHT_UNFILED_MIN, _report(
        "kind_right", unfiled.kind_right, unfiled
    )


def test_unfiled_price_changes_and_endings_are_right(unfiled: SubscriptionMetrics) -> None:
    assert unfiled.price_changes_flagged == unfiled.price_changes_planted, _report(
        "price_changes_flagged", unfiled.price_changes_flagged, unfiled
    )
    assert unfiled.price_changes_false <= PRICE_CHANGES_FALSE_MAX, _report(
        "price_changes_false", unfiled.price_changes_false, unfiled
    )
    assert unfiled.ended_right == unfiled.stopped_planted, _report(
        "ended_right", unfiled.ended_right, unfiled
    )
    assert unfiled.ended_wrong <= ENDED_WRONG_MAX, _report(
        "ended_wrong", unfiled.ended_wrong, unfiled
    )
