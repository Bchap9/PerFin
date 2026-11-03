# SPDX-License-Identifier: AGPL-3.0-or-later
"""Households, membership, account grants, user preferences and user administration.

The tables live elsewhere (``household`` in ``app.db.models``; members,
grants and users in ``app.auth.models``; preferences in ``setting``), so this
bounded context has no ``models.py``. ``schemas`` holds the separate input
(``extra="forbid"``) and output models, ``repo`` the queries, ``service`` the
household and membership rules and ``users`` user administration and
preferences.
"""
