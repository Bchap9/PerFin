# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live updates over Server-Sent Events (``GET /events``; ARCHITECTURE.md "perfin-api").

``service``: the database side (re-checking the session, reading and
filtering event rows); ``stream``: the async SSE generator and the
per-user stream cap. Event rows are written by :func:`app.core.events.emit`.
"""
