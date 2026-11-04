# SPDX-License-Identifier: AGPL-3.0-or-later
"""Rules engine (VISION.md "Rules engine", DATA_MODEL.md ``rule``).

* :mod:`app.domain.rules.schemas`: the rule document (conditions and actions
  as Pydantic discriminated unions) and the API models.
* :mod:`app.domain.rules.engine`: pure evaluation over transaction DTOs.
* :mod:`app.domain.rules.service`: CRUD, dry-run, apply (job) and
  :func:`app.domain.rules.service.apply_rules_to`, the hook the transaction
  and import code calls for new transactions.
"""
