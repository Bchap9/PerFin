# SPDX-License-Identifier: AGPL-3.0-or-later
"""Security price providers (docs/security/integrations.md, ARCHITECTURE.md).

* ``base``: the :class:`PriceProvider` protocol and :class:`PriceQuote`.
* ``manual``: :class:`ManualPriceProvider`, always on: prices typed in through
  ``POST /securities/{id}/prices`` (``app.domain.investments.securities``).
  It never fetches anything.
* ``http_json``: the one external provider, opt-in with
  ``PERFIN_PRICE_PROVIDER=http_json``: a generic JSON-over-HTTPS adapter driven
  by a URL template (``PERFIN_PRICE_PROVIDER_URL``), through the SSRF-safe
  client (``app.core.http``) with an allowlist of the template's host only.
  A request carries one ticker symbol and nothing else.
* ``registry``: which providers the settings enable.
* ``store``: writing fetched quotes into ``price`` rows (manual rows win).

The ``refresh_prices`` worker job (``app.workers.handlers.market_data``) is
the only caller of the external provider.
"""
