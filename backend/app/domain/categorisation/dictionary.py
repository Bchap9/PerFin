# SPDX-License-Identifier: AGPL-3.0-or-later
"""The merchant dictionary that ships with PerFin.

Static data inside the package, read on this host: looking a merchant up
makes no request to anything. :func:`lookup` finds the merchant a descriptor's
core names, :func:`keyword` a word in it that says what was bought.

This is the declaration the other modules build against: the dictionary is
empty for now, so nothing is found.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Final, Literal

_EMPTY: Final[Mapping[str, Merchant]] = MappingProxyType({})


@dataclass(frozen=True)
class Merchant:
    """One entry: who it is and the default category its charges get."""

    key: str
    name: str
    #: A default category's ``system_key``.
    category: str
    #: Its charges are usually a recurring series of this kind.
    series: Literal["subscription", "bill"] | None = None
    cadences: tuple[str, ...] = ()
    #: A store that sells everything: its category is only ever a suggestion.
    mixed: bool = False
    #: Pays money in (a payroll processor).
    inflow: bool = False


@dataclass(frozen=True)
class Hit:
    """A merchant found in a core, and how: the whole core, its start, or inside it."""

    merchant: Merchant
    how: Literal["exact", "prefix", "anywhere"]


@dataclass(frozen=True)
class KeywordHit:
    """A word that says what was bought; ``strong`` ones are sure enough to state."""

    category: str
    word: str
    strong: bool


def version() -> int:
    """The dictionary's version; a household evaluated with an older one is refreshed."""
    return 1


def merchants() -> Mapping[str, Merchant]:
    """Every merchant by key."""
    return _EMPTY


def lookup(core: str) -> Hit | None:
    """The merchant ``core`` names, or ``None``."""
    del core
    return None


def keyword(core: str, via: str | None) -> KeywordHit | None:
    """A telling word in ``core`` (or behind the processor ``via``), or ``None``."""
    del core, via
    return None


def name_for(key: str) -> str | None:
    """The display name of the merchant with this key, or ``None`` when it has no entry."""
    del key
    return None
