# SPDX-License-Identifier: AGPL-3.0-or-later
"""Underscore modules are never imported by discovery."""

raise RuntimeError("discovery must not import private modules")
