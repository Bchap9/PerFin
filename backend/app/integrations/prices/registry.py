# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which price providers the settings enable.

:class:`~app.integrations.prices.manual.ManualPriceProvider` is always on.
At most one external provider exists, and only when ``PERFIN_PRICE_PROVIDER``
names it (default ``none``: nothing is ever fetched). A new external provider
is a module in this package plus a ``Literal`` value of
``Settings.price_provider`` and a branch here; it must use
``app.core.http.SafeHttpClient`` and send symbols only
(docs/security/integrations.md).
"""

from __future__ import annotations

from app.core.http import ProxySetting, Resolver, system_resolver
from app.core.settings import Settings
from app.integrations.prices.base import PriceProvider
from app.integrations.prices.http_json import HttpJsonConfig, HttpJsonPriceProvider
from app.integrations.prices.manual import ManualPriceProvider


def external_price_provider(
    settings: Settings,
    *,
    resolver: Resolver = system_resolver,
    proxy: ProxySetting = "env",
) -> PriceProvider | None:
    """The opt-in external provider, or ``None`` (the default).

    Raises :class:`~app.integrations.prices.base.PriceProviderConfigError`
    when it is enabled but misconfigured (``perfin config check`` reports the
    same problem).
    """
    if settings.price_provider == "http_json":
        return HttpJsonPriceProvider(
            HttpJsonConfig.from_settings(settings), resolver=resolver, proxy=proxy
        )
    return None


def price_providers(settings: Settings) -> list[PriceProvider]:
    """Every enabled provider: manual first, then the external one if any."""
    providers: list[PriceProvider] = [ManualPriceProvider()]
    external = external_price_provider(settings)
    if external is not None:
        providers.append(external)
    return providers
