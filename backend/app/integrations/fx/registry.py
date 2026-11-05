# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which FX providers the settings enable.

:class:`~app.integrations.fx.manual.ManualFxProvider` is always on; the ECB
feed only with ``PERFIN_FX_PROVIDER=ecb`` (default ``none``).
"""

from __future__ import annotations

from app.core.http import ProxySetting, Resolver, system_resolver
from app.core.settings import Settings
from app.integrations.fx.base import FxProvider
from app.integrations.fx.ecb import EcbFxProvider
from app.integrations.fx.manual import ManualFxProvider


def external_fx_provider(
    settings: Settings, *, resolver: Resolver = system_resolver, proxy: ProxySetting = "env"
) -> FxProvider | None:
    """The opt-in external provider, or ``None`` (the default)."""
    if settings.fx_provider == "ecb":
        return EcbFxProvider(resolver=resolver, proxy=proxy)
    return None


def fx_providers(settings: Settings) -> list[FxProvider]:
    """Every enabled provider: manual first, then the external one if any."""
    providers: list[FxProvider] = [ManualFxProvider()]
    external = external_fx_provider(settings)
    if external is not None:
        providers.append(external)
    return providers
