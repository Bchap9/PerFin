# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FX provider contract.

A provider returns reference rates ``1 base = rate quote`` for a day. It is
asked for nothing in particular (no currency list, no amounts, no identity):
it returns what its source publishes, and ``app.integrations.fx.store``
keeps what a household needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol


class FxProviderError(Exception):
    """The provider's answer could not be fetched or understood (safe to log)."""


@dataclass(frozen=True)
class FxQuote:
    """``1 base = rate quote`` on ``rate_date``."""

    base: str
    quote: str
    rate_date: date
    rate: Decimal


class FxProvider(Protocol):
    @property
    def name(self) -> str:
        """Stored as ``fx_rate.source`` (at most 64 characters)."""
        ...

    @property
    def external(self) -> bool:
        """``True`` when :meth:`fetch` contacts another host."""
        ...

    def fetch(self) -> list[FxQuote]:
        """The provider's latest published rates."""
        ...
