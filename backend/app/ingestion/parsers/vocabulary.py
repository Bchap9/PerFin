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

A third kind of word is asked the same way: the **column headings** of an
activity table whose header the parser cannot use (``Posted | Details |
Money In/Out``: no column it knows as the date or the amount). The model
says what each heading names from :data:`SUGGESTED_COLUMNS`; the table is
then read by the parser, every row of it marked as a suggestion.

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
``Transferred from account 123-456789`` is never stored with its number,
and one answer covers every row worded the same way.

**What is remembered.** Once a person has confirmed a suggestion, the
household keeps it as *learned wording* (``app.domain.imports.wording``):
``learned_actions`` / ``learned_sections`` here. Learned wording types a row
like the parser's own lists do, with no doubt and without asking the model
or the person again. Only wording is ever learned, never a detail of the
statement it was learned from: a section's heading, or the action words of a
table that names its securities in a column of their own (a broker's words:
``Periodic Investment Plan``). The free text of a cash table (a payee, a
merchant, a memo) is typed for its own document only and is not
:func:`rememberable`; nor is any phrase holding a household member's name,
an address or an account number (the household's
:class:`~app.core.scrubber.Scrubber` decides).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
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
#: What a column heading of an activity table may be said to name
#: (``app.ingestion.parsers.brokerage.ActivityRole`` without ``other`` and
#: ``name``: a model may not hide a column, it can only leave it unnamed).
SUGGESTED_COLUMNS: Final = frozenset(
    {"date", "settle", "type", "description", "symbol", "quantity", "price", "amount", "fees"}
)
#: Phrases and headings asked about, and kept, at most; and their length.
MAX_PHRASES: Final = 40
PHRASE_MAX: Final = 80

_DIGITS: Final = re.compile(r"\d+")


def masked(text: str) -> str:
    """``text`` normalised with every run of digits masked (a :func:`phrase_key`, unbounded)."""
    return _DIGITS.sub("0", normalise(text))


def phrase_key(text: str) -> str:
    """``text`` as a vocabulary key: normalised, every run of digits masked, bounded.

    ``Transferred from account 123-456789`` -> ``transferred from account 0 0``
    (the mask is a digit so that a key is its own key).
    """
    return masked(text)[:PHRASE_MAX].strip()


#: How a row typed through a vocabulary names the entry that typed it
#: (``InvestmentActivity.suggestion``): ``action:<key>`` / ``section:<key>``.
ACTION_PREFIX: Final = "action:"
SECTION_PREFIX: Final = "section:"
#: A row of a table read through a suggested column heading: ``column:<key>``.
COLUMN_PREFIX: Final = "column:"


def column_key(text: str) -> str | None:
    """``text`` as the key of a column heading, or None when it is not words.

    A column heading is a few words without digits (``money in out``): its
    key is looked up as a table header is, by its normalised text.
    """
    key = normalise(text)[:PHRASE_MAX].strip()
    words = key.split()
    if not 1 <= len(words) <= 5 or any(ch.isdigit() for ch in key):
        return None
    return key if any(ch.isalpha() for ch in key) else None


@dataclass(frozen=True)
class Vocabulary:
    """Kinds by :func:`phrase_key`: the model's suggestions and learned wording (module doc)."""

    #: Action phrase -> activity type (:data:`SUGGESTED_TYPES`): suggestions.
    actions: Mapping[str, str] = field(default_factory=dict)
    #: Section heading -> section kind (:data:`SUGGESTED_SECTIONS`): suggestions.
    sections: Mapping[str, str] = field(default_factory=dict)
    model: str | None = None
    #: The same, confirmed by a person on an earlier statement of this layout.
    learned_actions: Mapping[str, str] = field(default_factory=dict)
    learned_sections: Mapping[str, str] = field(default_factory=dict)
    #: Column heading (:func:`column_key`) -> role (:data:`SUGGESTED_COLUMNS`):
    #: suggestions, and the same confirmed by a person.
    columns: Mapping[str, str] = field(default_factory=dict)
    learned_columns: Mapping[str, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (
            self.actions
            or self.sections
            or self.learned_actions
            or self.learned_sections
            or self.columns
            or self.learned_columns
        )

    def action(self, text: str) -> tuple[str, bool] | None:
        """``(type, it is only a suggestion)`` for an action text, if any (learned first)."""
        key = phrase_key(text)
        if not key:
            return None
        if key in self.learned_actions:
            return self.learned_actions[key], False
        return (self.actions[key], True) if key in self.actions else None

    def section(self, heading: str) -> tuple[str, bool] | None:
        """``(kind, it is only a suggestion)`` for a section heading, if any (learned first)."""
        key = phrase_key(heading)
        if not key:
            return None
        if key in self.learned_sections:
            return self.learned_sections[key], False
        return (self.sections[key], True) if key in self.sections else None

    def merged(self, other: Vocabulary) -> Vocabulary:
        """Both vocabularies; ``other`` only adds (an answer already kept stays)."""
        return Vocabulary(
            actions={**other.actions, **self.actions},
            sections={**other.sections, **self.sections},
            model=self.model or other.model,
            learned_actions={**other.learned_actions, **self.learned_actions},
            learned_sections={**other.learned_sections, **self.learned_sections},
            columns={**other.columns, **self.columns},
            learned_columns={**other.learned_columns, **self.learned_columns},
        )

    def untyped_columns(self, columns: Sequence[str]) -> list[str]:
        """The keys of ``columns`` nothing here names yet."""
        known = {*self.columns, *self.learned_columns}
        return [c for c in columns if c not in known]

    def untyped(
        self, actions: Sequence[str], headings: Sequence[str]
    ) -> tuple[list[str], list[str]]:
        """The keys of ``actions`` and ``headings`` nothing here names yet."""
        known_actions = {*self.actions, *self.learned_actions}
        known_sections = {*self.sections, *self.learned_sections}
        return (
            [a for a in actions if a not in known_actions],
            [h for h in headings if h not in known_sections],
        )

    def stored(self) -> dict[str, Any]:
        """The suggestions as kept on the document (``confidence["ml_vocabulary"]``).

        Learned wording is the household's, read from its own store each run.
        """
        return {
            "actions": dict(self.actions),
            "sections": dict(self.sections),
            "columns": dict(self.columns),
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
            columns={
                key: role
                for key, role in _checked(data.get("columns"), SUGGESTED_COLUMNS).items()
                if column_key(key) == key
            },
            model=model[:PHRASE_MAX] if isinstance(model, str) and model.isprintable() else None,
        )


def rememberable(key: str, clean: Callable[[str], str] | None = None) -> bool:
    """``key`` may be kept as learned wording: words only, nothing personal.

    A :func:`phrase_key` of two characters or more with a letter; ``clean``
    (the household scrubber's ``scrub_text``) must leave it as it is: a
    phrase it would mask holds a member's name, an address, an e-mail
    address or an account number.
    """
    if key != phrase_key(key) or len(key) < 2 or not any(ch.isalpha() for ch in key):
        return False
    return clean is None or normalise(clean(key)) == key


def _checked(data: Any, allowed: frozenset[str]) -> dict[str, str]:
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in list(data.items())[:MAX_PHRASES]:
        if isinstance(key, str) and key and key == phrase_key(key) and value in allowed:
            out[key] = value
    return out
