# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fingerprint evaluation has a total work budget (security review P34-SR-R2-01).

Every accepted pattern is linear per match attempt, but a search still costs
up to (pattern x line^2) per line: 32 ``any`` rules of ``[a ]*`` x 12 then
``!`` over 4000 lines of 300 characters took over a minute for one
``match_fingerprint`` call. Household fingerprints therefore run under a
shared wall-clock :class:`MatchBudget`; running out means "no match".
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from app.ingestion.classify.fingerprints import (
    HOUSEHOLD_MATCH_SECONDS,
    MAX_RULES,
    FingerprintSpec,
    MatchBudget,
    best_match,
    builtin_fingerprints,
    compile_fingerprint,
    match_fingerprint,
)
from app.ingestion.classify.safe_regex import MAX_QUANTIFIERS, MAX_SUBJECT_CHARS
from app.ingestion.classify.text import HEADER_LINES, MAX_LINES, DocumentView

pytestmark = pytest.mark.security

#: Each search of this pattern on a line of "a a a ..." costs ~line^2 steps.
HOSTILE = "[a ]*" * MAX_QUANTIFIERS + "!"
#: Generous bound for a whole evaluation (budget plus one line's search plus
#: scheduling noise on a loaded CI machine); unbounded it is about a minute.
BOUND_SECONDS = 2.0


def _spec(match: dict[str, Any], key: str = "household-hostile") -> FingerprintSpec:
    return FingerprintSpec.model_validate(
        {
            "schema": 1,
            "key": key,
            "institution": "Hostile Invented Bank",
            "kind": "statement",
            "parser": "statement_generic",
            "version": 1,
            "match": match,
        }
    )


def _hostile_match() -> dict[str, Any]:
    return {
        "all": [{"regex": HOSTILE + "x" * i} for i in range(MAX_RULES - 1)] + [{"regex": "a"}],
        "any": [{"regex": HOSTILE + "y" * i} for i in range(MAX_RULES)],
        "min_any": 0,
    }


def _hostile_view() -> DocumentView:
    line = ("a " * MAX_SUBJECT_CHARS)[:MAX_SUBJECT_CHARS]
    lines = (line,) * MAX_LINES
    return DocumentView(lines=lines, header=lines[:HEADER_LINES])


def test_hostile_household_fingerprint_is_cut_off_by_the_budget() -> None:
    # The "all" rules never hold, so every one of them scans all 4000 lines.
    match = _hostile_match()
    match["all"] = [{"regex": "a"}, *match["all"][:-1]]
    fp = compile_fingerprint(_spec(match), origin="household")
    view = _hostile_view()
    started = time.perf_counter()
    assert match_fingerprint(fp, view) is None
    assert time.perf_counter() - started < BOUND_SECONDS


def test_hostile_any_rules_are_cut_off_and_do_not_match() -> None:
    # "all" holds at once; the 32 "any" rules are always all evaluated.
    match = {"all": [{"regex": "a"}], "any": _hostile_match()["any"], "min_any": 0}
    fp = compile_fingerprint(_spec(match), origin="household")
    budget = MatchBudget(HOUSEHOLD_MATCH_SECONDS)
    started = time.perf_counter()
    assert match_fingerprint(fp, _hostile_view(), budget) is None
    assert time.perf_counter() - started < BOUND_SECONDS
    assert budget.exhausted


def test_many_hostile_household_fingerprints_share_one_budget() -> None:
    match = {"all": [{"regex": "a"}], "any": _hostile_match()["any"], "min_any": 0}
    hostile = [
        compile_fingerprint(_spec(match, key=f"household-hostile-{i}"), origin="household")
        for i in range(100)
    ]
    started = time.perf_counter()
    top, matches = best_match([*hostile, *builtin_fingerprints()], _hostile_view())
    assert time.perf_counter() - started < BOUND_SECONDS * 2
    assert top is None
    assert all(m.fingerprint.origin == "builtin" for m in matches)


def test_spent_budget_fails_closed_and_shipped_fingerprints_are_not_budgeted() -> None:
    now = [0.0]
    budget = MatchBudget(1.0, clock=lambda: now[0])
    child = budget.child(10.0)
    view = DocumentView.from_text("Plain Invented Bank\nStatement period 01\n")
    fp = compile_fingerprint(
        _spec({"all": [{"regex": "plain invented"}]}, key="household-plain"),
        origin="household",
    )
    assert match_fingerprint(fp, view, budget) is not None
    now[0] = 1.0
    assert budget.spent()
    assert child.spent()
    assert match_fingerprint(fp, view, budget) is None
    assert match_fingerprint(fp, view, child) is None
    # A shipped fingerprint is reviewed code: a spent budget does not apply.
    shipped = compile_fingerprint(
        _spec({"all": [{"regex": "plain invented"}]}, key="shipped-plain")
    )
    assert match_fingerprint(shipped, view, budget) is not None


def test_budget_stops_between_lines() -> None:
    calls = [0]

    def clock() -> float:
        calls[0] += 1
        return 0.0 if calls[0] <= 3 else 5.0

    budget = MatchBudget(1.0, clock=clock)
    fp = compile_fingerprint(
        _spec({"all": [{"regex": "needle"}]}, key="household-needle"), origin="household"
    )
    lines = ("hay",) * 10 + ("needle",)
    view = DocumentView(lines=lines, header=lines[:HEADER_LINES])
    # The deadline passes after two lines, before the needle is reached.
    assert match_fingerprint(fp, view, budget) is None
    assert budget.exhausted
