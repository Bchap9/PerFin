# SPDX-License-Identifier: AGPL-3.0-or-later
"""Accounts: institutions, accounts, balance snapshots (DATA_MODEL.md "Ledger").

``models`` holds the tables, ``visibility`` who may see or edit which account
(role plus ``account_grant``), ``balances`` balance computation from
snapshots and transactions, ``repo`` the shared queries.
"""
