# SPDX-License-Identifier: AGPL-3.0-or-later
"""What ``GET /inbox/summary`` answers."""

from __future__ import annotations

from pydantic import BaseModel, Field


class InboxSummary(BaseModel):
    """What waits on a person, counted over what the caller may see.

    ``documents``: documents with an open review item. ``loose``: open review
    items of no document. ``subscriptions``: series that are suggested or
    have an open change, each counted once. ``suggestions`` and
    ``uncategorised``: the categorisation summary's counts. ``cards``: what
    the Inbox shows as cards, ``documents + loose + subscriptions``, plus one
    when there are suggestions; an offer to make a rule is not counted.
    """

    documents: int = Field(ge=0)
    loose: int = Field(ge=0)
    subscriptions: int = Field(ge=0)
    suggestions: int = Field(ge=0)
    uncategorised: int = Field(ge=0)
    cards: int = Field(ge=0)
