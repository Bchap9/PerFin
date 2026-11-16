# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Inbox: one count of what waits on a person (``GET /inbox/summary``).

Nothing is stored here. :mod:`app.domain.inbox.service` counts what the
review queue, the subscription list and the categorisation summary already
know, over what the caller may see, so the rail badge, Home and the Inbox
read one cheap request instead of four.
"""
