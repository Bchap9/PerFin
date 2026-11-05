# SPDX-License-Identifier: AGPL-3.0-or-later
"""Telling whether two institution names mean the same institution (deterministic).

Statements, exports and people write the same institution differently:
``Chase``, ``JPMorgan Chase Bank, N.A.``, ``CHASE BANK``; ``Fidelity`` and
``Fidelity Investments``. :func:`institution_key` reduces a name to its
distinctive words (``normalize_payee``, then corporate and generic words
such as *bank*, *N.A.*, *investments*, *financial*, *inc* dropped), and
:func:`same_institution` says two names match when the keys are equal, or the
shorter key's words are all in the longer one's and are not only common words
(:data:`AMBIGUOUS_WORDS`: ``First``, ``American``, ``Capital``...).
:func:`pick` chooses among names: an exact normalised match first, else the
single fuzzy match (several fuzzy matches are no match).
No model, no network.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Final

from app.domain.transactions.import_hash import normalize_payee

#: Words that say what kind of company it is, not which one.
GENERIC_WORDS: Final = frozenset(
    {
        "the",
        "bank",
        "banking",
        "bancorp",
        "na",
        "n",
        "a",
        "fsb",
        "ssb",
        "of",
        "and",
        "co",
        "company",
        "corp",
        "corporation",
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "plc",
        "group",
        "financial",
        "finance",
        "services",
        "service",
        "investments",
        "investment",
        "investor",
        "investors",
        "brokerage",
        "securities",
        "trust",
        "credit",
        "union",
        "fcu",
        "cu",
        "federal",
        "national",
        "association",
        "card",
        "cards",
        "usa",
        "us",
        "online",
    }
)


#: Words too common to identify an institution alone (``First National
#: Bank`` is not ``First Republic``): a shorter name made only of these
#: matches only an identical key.
AMBIGUOUS_WORDS: Final = frozenset(
    {
        "first",
        "american",
        "united",
        "citizens",
        "peoples",
        "people",
        "community",
        "state",
        "city",
        "county",
        "central",
        "southern",
        "northern",
        "western",
        "eastern",
        "pacific",
        "atlantic",
        "capital",
        "home",
        "savings",
        "farmers",
        "merchants",
        "mutual",
        "security",
        "general",
        "standard",
        "one",
        "new",
        "york",
        "texas",
        "california",
    }
)


def institution_key(name: str | None) -> frozenset[str]:
    """The distinctive words of an institution name (empty: nothing distinctive)."""
    words = normalize_payee(name).split()
    return frozenset(w for w in words if w not in GENERIC_WORDS and len(w) > 1)


def same_institution(a: str | None, b: str | None) -> bool:
    """Whether two names mean the same institution (module doc)."""
    if not a or not b:
        return False
    if normalize_payee(a) == normalize_payee(b):
        return True
    left, right = institution_key(a), institution_key(b)
    if not left or not right:
        return False
    if left == right:
        return True
    smaller, larger = (left, right) if len(left) <= len(right) else (right, left)
    return smaller <= larger and not smaller <= AMBIGUOUS_WORDS


def pick[T](wanted: str | None, named: Sequence[tuple[T, str | None]]) -> T | None:
    """The one item whose name means ``wanted``: exact first, else the single fuzzy match."""
    target = normalize_payee(wanted)
    if not target:
        return None
    exact = [item for item, name in named if normalize_payee(name) == target]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None
    fuzzy = _distinct(item for item, name in named if same_institution(wanted, name))
    return fuzzy[0] if len(fuzzy) == 1 else None


def _distinct[T](items: Iterable[T]) -> list[T]:
    return list(dict.fromkeys(items))
