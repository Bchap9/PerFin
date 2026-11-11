# SPDX-License-Identifier: AGPL-3.0-or-later
"""Words a statement uses that the parsers' own vocabulary does not know (ADR-0010).

A brokerage parser types an activity row by its action words
(``app.ingestion.parsers.brokerage.ACTIVITY_TYPES``) and by the heading of
its section (``ACTIVITY_SECTIONS``). A row neither names is not read: it is
counted, reported and left for a person. Such words are what a language
model is good at and a rule table is not, so they are the one thing about a
statement's lines the optional local model may be asked
(``app.ml.client.activity_hints``): *what kind of activity is this phrase,
what kind of section is this heading*, each answered from a closed list.

A :class:`Vocabulary` is the checked answer, and nothing more than a
suggestion:

* its keys are PerFin's own (:func:`phrase_key` of the words the parser
  could not type, never text a model sent back) and its values come from
  :data:`SUGGESTED_TYPES` / :data:`SUGGESTED_SECTIONS` only. A model cannot
  say "this is not activity": a heading it would skip is simply not typed,
  so its rows stay reported as unread, as without a model;
* a row typed through it carries the ``suggested_type`` doubt
  (:data:`SUGGESTED_DOUBT`, scored :data:`~app.ingestion.parsers.confidence.SUGGESTED`,
  below any threshold), so the importer never writes it on its own: it
  waits in the review queue, marked as the model's suggestion, until a
  person confirms it (``app.domain.investments.statement_import``);
* the amounts, dates, quantities and securities of such a row are read
  from the page by the parser exactly as for any other row. The model
  names a kind, it supplies no figure.

Keys hold no digits (:func:`phrase_key` masks them), so a phrase such as
``Transferred from account 123-456789`` is neither stored nor sent with its
number, and one answer covers every row worded the same way.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final

from app.ingestion.parsers.text import normalise

#: The doubt of a row whose kind is the local model's suggestion.
SUGGESTED_DOUBT: Final = "suggested_type"
#: Activity types a suggestion may name (a split needs a ratio no phrase gives).
SUGGESTED_TYPES: Final = frozenset(
    {
        "buy",
        "sell",
        "dividend",
        "interest",
        "fee",
        "reinvest",
        "transfer_in",
        "transfer_out",
        "deposit",
        "withdrawal",
        "corporate_action",
    }
)
#: Section kinds a suggestion may name: those that type rows. Never ``skip``
#: (a model may not hide a table) nor the kinds only a layout's own heading opens.
SUGGESTED_SECTIONS: Final = frozenset(
    {
        "income",
        "deposit",
        "withdrawal",
        "card",
        "cash",
        "fee",
        "transfer_in",
        "transfer_out",
    }
)
#: Phrases and headings asked about, and kept, at most; and their length.
MAX_PHRASES: Final = 40
PHRASE_MAX: Final = 80

_DIGITS: Final = re.compile(r"\d+")


def phrase_key(text: str) -> str:
    """``text`` as a vocabulary key: normalised, every run of digits masked, bounded.

    ``Transferred from account 123-456789`` -> ``transferred from account 0 0``
    (the mask is a digit so that a key is its own key).
    """
    return _DIGITS.sub("0", normalise(text))[:PHRASE_MAX].strip()


@dataclass(frozen=True)
class Vocabulary:
    """Suggested kinds by :func:`phrase_key` (module doc)."""

    #: Action phrase -> activity type (:data:`SUGGESTED_TYPES`).
    actions: Mapping[str, str] = field(default_factory=dict)
    #: Section heading -> section kind (:data:`SUGGESTED_SECTIONS`).
    sections: Mapping[str, str] = field(default_factory=dict)
    model: str | None = None

    @property
    def empty(self) -> bool:
        return not self.actions and not self.sections

    def action(self, text: str) -> str | None:
        """The type suggested for an action text, if any."""
        return self.actions.get(phrase_key(text)) if text.strip() else None

    def section(self, heading: str) -> str | None:
        """The kind suggested for a section heading, if any."""
        return self.sections.get(phrase_key(heading)) if heading.strip() else None

    def merged(self, other: Vocabulary) -> Vocabulary:
        """Both vocabularies; ``other`` only adds (an answer already kept stays)."""
        return Vocabulary(
            actions={**other.actions, **self.actions},
            sections={**other.sections, **self.sections},
            model=self.model or other.model,
        )

    def stored(self) -> dict[str, Any]:
        """The JSON kept on the document (``document.confidence["ml_vocabulary"]``)."""
        return {
            "actions": dict(self.actions),
            "sections": dict(self.sections),
            "model": self.model,
        }

    @classmethod
    def from_stored(cls, data: Any) -> Vocabulary:
        """The vocabulary stored on a document, checked again (anything else dropped)."""
        if not isinstance(data, dict):
            return cls()
        model = data.get("model")
        return cls(
            actions=_checked(data.get("actions"), SUGGESTED_TYPES),
            sections=_checked(data.get("sections"), SUGGESTED_SECTIONS),
            model=model[:PHRASE_MAX] if isinstance(model, str) and model.isprintable() else None,
        )


def _checked(data: Any, allowed: frozenset[str]) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in list(data.items())[:MAX_PHRASES]:
        if isinstance(key, str) and key and key == phrase_key(key) and value in allowed:
            out[key] = value
    return out
