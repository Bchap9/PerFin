# SPDX-License-Identifier: AGPL-3.0-or-later
"""Single source of "now" so tests can monkeypatch time."""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """Current time as an aware UTC datetime."""
    return datetime.now(UTC)
