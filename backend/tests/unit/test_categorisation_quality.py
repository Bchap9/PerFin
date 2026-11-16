# SPDX-License-Identifier: AGPL-3.0-or-later
"""The categorisation targets of the design note (section 9), as gates.

The 1,200 labelled descriptors (``tests/fixtures/descriptors.py``) go through
the clean-up, the dictionary and the engine as they ship, the way a new
household's first import does: no rule, no payee, nothing learned
(``quality.engine_decide``). One test per row of the table. A failing target
prints what was missed (the line, its label and what was answered).

A threshold here is never lowered to make a change pass. A miss is fixed in
the dictionary, the clean-up or the engine; a label believed wrong is changed
only with its reason in the commit message.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from tests.fixtures import quality
from tests.fixtures.quality import MISSES_SHOWN, CategorisationMetrics, TeachingMetrics

KNOWN_FACT_RIGHT_MIN = 0.85
FACT_PRECISION_MIN = 0.97
SUGGESTION_PRECISION_MIN = 0.80
INVENTED_WRONG_FACT_MAX = 0.01
FACT_COVERAGE_MIN = 0.60
ONE_KEY_PER_MERCHANT_MIN = 0.95
KIND_RIGHT_MIN = 0.97
PRIVATE_LEAKS_MAX = 0
TAUGHT_RIGHT_MIN = 0.95


@pytest.fixture(scope="module")
def metrics() -> CategorisationMetrics:
    return quality.categorisation_metrics(quality.engine_identify, quality.engine_decide)


@pytest.fixture(scope="module")
def taught() -> TeachingMetrics:
    return quality.teaching_metrics()


def _report(name: str, value: object, misses: Sequence[str]) -> str:
    shown = "\n".join(f"  {miss}" for miss in misses[:MISSES_SHOWN])
    more = len(misses) - MISSES_SHOWN
    tail = f"\n  ... and {more} more" if more > 0 else ""
    return f"{name} is {value}; {len(misses)} missed:\n{shown}{tail}"


def _misses(metrics: CategorisationMetrics, name: str) -> Sequence[str]:
    return metrics.misses.get(name, ())


def test_known_merchants_get_the_right_category_as_a_fact(metrics: CategorisationMetrics) -> None:
    value = metrics.known_fact_right
    assert value is not None
    assert value >= KNOWN_FACT_RIGHT_MIN, _report(
        "known_fact_right", value, _misses(metrics, "known_fact_right")
    )


def test_facts_are_right(metrics: CategorisationMetrics) -> None:
    value = metrics.fact_precision
    assert value is not None
    assert value >= FACT_PRECISION_MIN, _report(
        "fact_precision", value, _misses(metrics, "fact_precision")
    )


def test_suggestions_are_right(metrics: CategorisationMetrics) -> None:
    value = metrics.suggestion_precision
    assert value is not None
    assert value >= SUGGESTION_PRECISION_MIN, _report(
        "suggestion_precision", value, _misses(metrics, "suggestion_precision")
    )


def test_invented_merchants_get_no_wrong_fact(metrics: CategorisationMetrics) -> None:
    value = metrics.invented_wrong_fact
    assert value is not None
    assert value <= INVENTED_WRONG_FACT_MAX, _report(
        "invented_wrong_fact", value, _misses(metrics, "invented_wrong_fact")
    )


def test_most_lines_get_a_fact(metrics: CategorisationMetrics) -> None:
    # Nothing to list: every line without a fact would be a miss.
    assert metrics.fact_coverage >= FACT_COVERAGE_MIN, metrics.fact_coverage


def test_lines_of_one_merchant_share_one_key(metrics: CategorisationMetrics) -> None:
    value = metrics.one_key_per_merchant
    assert value is not None
    assert value >= ONE_KEY_PER_MERCHANT_MIN, _report(
        "one_key_per_merchant", value, _misses(metrics, "one_key_per_merchant")
    )


def test_the_kind_of_a_non_purchase_is_recognised(metrics: CategorisationMetrics) -> None:
    value = metrics.kind_right
    assert value is not None
    assert value >= KIND_RIGHT_MIN, _report("kind_right", value, _misses(metrics, "kind_right"))


def test_no_person_s_name_is_in_a_merchant_key(metrics: CategorisationMetrics) -> None:
    assert metrics.private_leaks <= PRIVATE_LEAKS_MAX, _report(
        "private_leaks", metrics.private_leaks, _misses(metrics, "private_leaks")
    )


def test_one_correction_teaches(taught: TeachingMetrics) -> None:
    # Every invented merchant is printed twice: a person files the first
    # line, and the second must then carry that category as a fact.
    assert taught.merchants >= 100
    value = taught.taught_right
    assert value is not None
    assert value >= TAUGHT_RIGHT_MIN, _report("taught_right", value, taught.misses)
