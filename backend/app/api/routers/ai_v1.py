# SPDX-License-Identifier: AGPL-3.0-or-later
"""Plug-in of the AI views: ``/api/v1/ai/v1`` (``app.api.ai.router``, Phase 6b).

The views live in ``app/api/ai`` (DEVELOPMENT.md layout); this module only
exposes their router to ``create_app``'s router discovery.
"""

from __future__ import annotations

from app.api.ai.router import router

__all__ = ["router"]
