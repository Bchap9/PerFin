# SPDX-License-Identifier: AGPL-3.0-or-later
"""Manual FX rates: always on, never fetches.

Rates are typed in with ``POST /fx-rates`` (``app.domain.fx.manage``,
``source`` ``manual``) and always win over fetched ones:
``app.integrations.fx.store`` never writes a pair and day that has a manual
rate in either direction.
"""

from __future__ import annotations

from app.domain.fx.models import MANUAL_SOURCE
from app.integrations.fx.base import FxQuote


class ManualFxProvider:
    name: str = MANUAL_SOURCE
    external: bool = False

    def fetch(self) -> list[FxQuote]:
        """Nothing to fetch: manual rates are typed in through the API."""
        return []
