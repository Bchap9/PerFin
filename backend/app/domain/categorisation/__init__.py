# SPDX-License-Identifier: AGPL-3.0-or-later
"""How a transaction gets its category when no person, file or rule decided.

Everything here runs on the host from data that ships with PerFin: no model,
no lookup service, nothing leaves the instance.

* :mod:`app.domain.categorisation.models`: ``merchant_memory``, the
  household's counted corrections.
* :mod:`app.domain.categorisation.memory`: reading and writing that table.
* :mod:`app.domain.categorisation.pinned`: the only way the system writes
  provenance to a transaction, without moving ``updated_at``.
"""
