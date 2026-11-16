# SPDX-License-Identifier: AGPL-3.0-or-later
"""Count what waits on a person (design note, stage 3 UI redesign, section 11).

Each count reuses the rule of the page that shows it, so the badge and the
Inbox cards cannot disagree:

* ``documents`` and ``loose``: open review items under
  :func:`app.domain.review.service.visible_filter`, by document or with none.
* ``subscriptions``: the series ``GET /subscriptions?status=suggested`` lists,
  together with the series of every open change ``GET /subscriptions/events``
  lists, each counted once.
* ``suggestions`` and ``uncategorised``:
  :func:`app.domain.categorisation.service.summary`.

What was learned (an offer to make a rule) is not a question and is not
computed here, so a live update stays cheap.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.domain.categorisation import service as categorisation
from app.domain.documents.models import ReviewItem, ReviewItemState
from app.domain.inbox.schemas import InboxSummary
from app.domain.recurring import subscriptions
from app.domain.recurring.schemas import MAX_EVENTS
from app.domain.review import service as review


def _review_counts(db: Session, principal: Principal) -> tuple[int, int]:
    """Documents with an open item, and open items of no document, the caller may see."""
    visible = review.visible_filter(principal)
    household_id = principal.household_id
    has_document = ReviewItem.document_id.is_not(None)
    documents, loose = db.execute(
        select(
            func.count(func.distinct(ReviewItem.document_id)).filter(has_document),
            func.count().filter(~has_document),
        ).where(
            ReviewItem.household_id == household_id,
            visible,
            ReviewItem.state == ReviewItemState.OPEN,
        )
    ).one()
    return int(documents), int(loose)


def _series(db: Session, principal: Principal) -> int:
    """Series that are suggested or have an open change, each once."""
    listed = subscriptions.list_subscriptions(
        db, principal, kind=None, status=("suggested",), account_ids=None
    )
    ids: set[uuid.UUID] = {row.id for row in listed.items}
    cursor: uuid.UUID | None = None
    while True:
        page = subscriptions.list_events(
            db, principal, state="open", cursor=cursor, limit=MAX_EVENTS
        )
        ids.update(event.schedule_id for event in page.items)
        if page.next_cursor is None:
            return len(ids)
        cursor = page.next_cursor


def summary(db: Session, principal: Principal) -> InboxSummary:
    """What waits on the caller (module doc)."""
    documents, loose = _review_counts(db, principal)
    series = _series(db, principal)
    categories = categorisation.summary(db, principal)
    return InboxSummary(
        documents=documents,
        loose=loose,
        subscriptions=series,
        suggestions=categories.suggestions,
        uncategorised=categories.uncategorised,
        cards=documents + loose + series + (1 if categories.suggestions else 0),
    )
