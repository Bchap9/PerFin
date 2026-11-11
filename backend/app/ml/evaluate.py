# SPDX-License-Identifier: AGPL-3.0-or-later
"""How well the configured local model types statement wording (ADR-0010).

The model is only ever asked one kind of question about a statement's
lines: *what kind of activity is this phrase, what kind of section is this
heading*, each answered from a closed list (``app.ml.client.activity_hints``).
PerFin's parsers already know the answer for a few hundred such phrases:
their own vocabulary (``ACTIVITY_TYPES``, ``ACTIVITY_SECTIONS`` of
``app.ingestion.parsers.brokerage``). :func:`evaluate` asks the configured
adapter about exactly those, without any statement text, and counts how
often it agrees. A model that cannot type ``dividend received`` will not
type a broker's unusual wording either, so the share of agreement is a fair
first measure of a model before it is trusted with suggestions.

Nothing personal is sent: the phrases are PerFin's own word lists. Nothing
is stored: the report is returned to the caller (``perfin ml eval``).
A suggestion remains a suggestion whatever the score: a row typed by the
model is never written without a person's confirmation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

import httpx

from app.core.settings import Settings
from app.ingestion.parsers.brokerage import ACTIVITY_SECTIONS, ACTIVITY_TYPES
from app.ingestion.parsers.vocabulary import (
    MAX_PHRASES,
    SUGGESTED_SECTIONS,
    SUGGESTED_TYPES,
    phrase_key,
)
from app.ml.client import activity_hints

#: Phrases put to the model in one call.
BATCH: Final = min(20, MAX_PHRASES)
#: Disagreements listed in a report at most.
LISTED_MAX: Final = 200


@dataclass
class Score:
    """One list's result: how many phrases were asked and how they came back."""

    asked: int = 0
    agreed: int = 0
    differed: int = 0
    unanswered: int = 0

    @property
    def agreement(self) -> float | None:
        """The share of phrases asked that came back as PerFin types them."""
        return self.agreed / self.asked if self.asked else None

    def as_dict(self) -> dict[str, Any]:
        share = self.agreement
        return {
            "asked": self.asked,
            "agreed": self.agreed,
            "differed": self.differed,
            "unanswered": self.unanswered,
            "agreement": round(share, 4) if share is not None else None,
        }


@dataclass
class Report:
    """What :func:`evaluate` found."""

    model: str | None = None
    actions: Score = field(default_factory=Score)
    headings: Score = field(default_factory=Score)
    #: ``{"list", "phrase", "expected", "answered"}`` per disagreement.
    differences: list[dict[str, str]] = field(default_factory=list)
    calls: int = 0
    failed_calls: int = 0

    @property
    def total(self) -> Score:
        return Score(
            asked=self.actions.asked + self.headings.asked,
            agreed=self.actions.agreed + self.headings.agreed,
            differed=self.actions.differed + self.headings.differed,
            unanswered=self.actions.unanswered + self.headings.unanswered,
        )

    @property
    def reachable(self) -> bool:
        """Some call to the adapter was answered."""
        return self.calls > self.failed_calls

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "calls": self.calls,
            "failed_calls": self.failed_calls,
            "total": self.total.as_dict(),
            "actions": self.actions.as_dict(),
            "headings": self.headings.as_dict(),
            "differences": self.differences[:LISTED_MAX],
        }


def ground_truth() -> tuple[dict[str, str], dict[str, str]]:
    """``(action phrase -> type, heading -> section kind)`` the model may be asked.

    PerFin's own vocabulary, keyed like a question to the model
    (:func:`phrase_key`), without the kinds no suggestion may name (a
    split, a section that only a layout's own heading opens).
    """
    actions: dict[str, str] = {
        phrase_key(phrase): kind
        for phrase, kind in ACTIVITY_TYPES.items()
        if kind in SUGGESTED_TYPES and phrase_key(phrase)
    }
    headings: dict[str, str] = {
        phrase_key(heading): kind
        for heading, kind in ACTIVITY_SECTIONS.items()
        if kind in SUGGESTED_SECTIONS and phrase_key(heading)
    }
    return actions, headings


def _batches(keys: Sequence[str]) -> list[list[str]]:
    return [list(keys[i : i + BATCH]) for i in range(0, len(keys), BATCH)]


def _count(
    score: Score,
    report: Report,
    name: str,
    expected: Mapping[str, str],
    asked: Sequence[str],
    answered: Mapping[str, str],
) -> None:
    for key in asked:
        score.asked += 1
        got = answered.get(key)
        if got is None:
            score.unanswered += 1
        elif got == expected[key]:
            score.agreed += 1
        else:
            score.differed += 1
            report.differences.append(
                {"list": name, "phrase": key, "expected": expected[key], "answered": got}
            )


def evaluate(settings: Settings, *, transport: httpx.BaseTransport | None = None) -> Report:
    """Ask the configured adapter about PerFin's own vocabulary (module doc)."""
    actions, headings = ground_truth()
    report = Report()
    for name, expected, score in (
        ("actions", actions, report.actions),
        ("headings", headings, report.headings),
    ):
        for batch in _batches(sorted(expected)):
            report.calls += 1
            if name == "actions":
                answer = activity_hints(settings, batch, [], transport=transport)
            else:
                answer = activity_hints(settings, [], batch, transport=transport)
            if answer is None:
                # Off, unreachable, or no phrase of the batch got a listed kind.
                report.failed_calls += 1
                _count(score, report, name, expected, batch, {})
                continue
            report.model = report.model or answer.model
            _count(
                score,
                report,
                name,
                expected,
                batch,
                answer.actions if name == "actions" else answer.sections,
            )
    return report
