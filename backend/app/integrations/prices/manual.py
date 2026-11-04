# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manual prices: always on, never fetches.

Manual closes are entered with ``POST /securities/{id}/prices``
(``app.domain.investments.securities.set_price``, ``source`` ``manual``). They
always win over fetched prices: ``app.integrations.prices.store`` never
replaces a manual row.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from app.domain.investments.models import MANUAL_SOURCE
from app.integrations.prices.base import PriceQuote


class ManualPriceProvider:
    name: str = MANUAL_SOURCE
    external: bool = False

    def fetch(self, symbols: Sequence[str], *, today: date) -> list[PriceQuote]:
        """Nothing to fetch: manual prices are typed in through the API."""
        return []
