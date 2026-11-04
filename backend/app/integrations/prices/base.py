# SPDX-License-Identifier: AGPL-3.0-or-later
"""The price provider contract.

A provider answers "what was the latest close of these ticker symbols?". It
receives **symbols only** (never quantities, holdings, accounts, household or
user identity) and returns one :class:`PriceQuote` per symbol it could price.
A symbol it cannot price is left out; a provider never raises for one symbol.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Final, Protocol

#: The symbols a provider may be asked for: the ``security.symbol`` check, plus
#: at least one letter or digit (a symbol of dots alone could act as a path
#: segment such as ``..`` once put into a URL).
SYMBOL_RE: Final = re.compile(r"^(?=[^A-Za-z0-9]*[A-Za-z0-9])[A-Za-z0-9.^/:_-]{1,32}$")


class PriceProviderConfigError(ValueError):
    """The provider settings are unusable (message names settings, never secrets)."""


@dataclass(frozen=True)
class PriceQuote:
    """The close of ``symbol`` on ``price_date``, in ``currency``."""

    symbol: str
    price_date: date
    close: Decimal
    currency: str


class PriceProvider(Protocol):
    @property
    def name(self) -> str:
        """Stored as ``price.source`` (at most 64 characters)."""
        ...

    @property
    def external(self) -> bool:
        """``True`` when :meth:`fetch` contacts another host."""
        ...

    def fetch(self, symbols: Sequence[str], *, today: date) -> list[PriceQuote]:
        """Quotes for ``symbols`` (dated on or before ``today``)."""
        ...
