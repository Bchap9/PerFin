# SPDX-License-Identifier: AGPL-3.0-or-later
"""Exchange-rate providers (docs/security/integrations.md).

* ``base``: the :class:`FxProvider` protocol and :class:`FxQuote`.
* ``manual``: :class:`ManualFxProvider`, always on: rates typed in through
  ``POST /fx-rates`` (``app.domain.fx.manage``). It never fetches.
* ``ecb``: the European Central Bank's daily euro reference rates, opt-in
  with ``PERFIN_FX_PROVIDER=ecb``: one ``GET`` of a fixed public URL, nothing
  household-specific sent, the XML parsed with ``defusedxml``.
* ``registry``: which providers the settings enable.
* ``store``: cross rates against each household's base currency into
  ``fx_rate`` rows (manual rows win).
"""
