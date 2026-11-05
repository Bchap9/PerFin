# SPDX-License-Identifier: AGPL-3.0-or-later
"""Categories, payees and tags: the household's classification of transactions.

The tables live in ``app.domain.transactions.models`` (they are referenced by
transactions, splits, schedules, budgets and rules); this package holds
their API: ``schemas`` (inputs forbid unknown fields), ``repo`` (queries,
always filtered by ``household_id``) and ``service`` (rules, audit, events).
The default category set is ``app.domain.transactions.categories``.
"""
