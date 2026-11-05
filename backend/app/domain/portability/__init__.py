# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full export and import of a household (docs/PORTABILITY.md).

``entities`` lists what is exported and how references are re-mapped,
``codec`` converts column values to and from JSON, ``zipsafe`` reads an
uploaded archive defensively (SECURITY.md C2/C3), ``export`` writes the
archive, ``import_`` validates and applies one, ``service`` holds the API and
job rules on top (``app.api.routers.portability``,
``app.workers.handlers.portability``).
"""
