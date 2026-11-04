# SPDX-License-Identifier: AGPL-3.0-or-later
"""What an event stream may show, read from the database (SECURITY.md F3).

A stream belongs to one browser session in one household. Every
``PERFIN_SSE_REVALIDATE_SECONDS`` (and at the start) :func:`refresh_view`
re-reads the session (live, ``authenticated``, same household, without
extending its idle expiry), the user (active), the membership (current
role) and, for members and viewers, the accounts they may view. A stream
whose session ended, was revoked or switched household, or whose user lost
the membership, stops.

:func:`deliverable` decides per event row:

* owners and admins receive every event of the household;
* an event addressed to the user (payload ``user_id``, e.g. a grant change)
  is delivered;
* an event whose payload names accounts (``account_id``, ``account_ids``) is
  delivered only if at least one of them is visible;
* an event whose payload has ``all_account_ids`` (goals: readable only when
  every account is visible) is delivered only if all of them are visible;
* fail closed (review finding P2-SR-03): a ``transaction.*``, ``balance.*``
  or ``goal.*`` event that names no account is dropped;
* a ``document.*`` event (payload ``id``) or ``review.*`` event (payload
  ``document_id``) about a document that exists and is hidden is dropped;
  ``document.deleted`` (the row is gone) names the document's
  ``account_id``, and without one is delivered only to its uploader
  (payload ``uploaded_by``).

What goes on the wire is :func:`frame`: the event id and type, nothing from
the payload. The client refetches through the API, which applies the same
rules again.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import SessionStage, User, UserSession
from app.auth.principal import Principal, apply_rls_context, has_primary_mfa, membership_in
from app.auth.sessions import SessionPolicy, load_session
from app.db.models import Event
from app.domain.accounts.visibility import sees_all_accounts, visible_account_ids
from app.domain.documents.models import Document
from app.domain.documents.service import visible_document_filter

#: Payload keys naming accounts, as written by the services' ``emit`` calls.
ACCOUNT_KEYS: Final = ("account_id", "account_ids")
#: Payload key naming accounts that must *all* be visible.
ALL_ACCOUNTS_KEY: Final = "all_account_ids"
#: Event types about account data: without a named account they are dropped
#: for members and viewers.
ACCOUNT_SCOPED_PREFIXES: Final = (
    "transaction.",
    "balance.",
    "goal.",
    "investment.",
    "security.",
    "loan.",
    "asset.",
    "schedule.",
    "networth.",
)


@dataclass(frozen=True)
class StreamView:
    """The principal a stream acts as, and the accounts it may see (``None``: all)."""

    principal: Principal
    accounts: frozenset[uuid.UUID] | None

    @property
    def household_id(self) -> uuid.UUID:
        household_id = self.principal.household_id
        if household_id is None:  # pragma: no cover - refresh_view always sets it
            raise RuntimeError("a stream view needs a household")
        return household_id


def refresh_view(
    db: Session,
    raw_session_id: str | None,
    *,
    household_id: uuid.UUID,
    policy: SessionPolicy,
    require_mfa: bool,
    accept: Callable[[UserSession], bool] | None = None,
) -> StreamView | None:
    """The stream's current view, or ``None`` when it must stop (module doc)."""
    row = load_session(db, raw_session_id, policy=policy, touch=False, accept=accept)
    if row is None or row.stage is not SessionStage.AUTHENTICATED:
        return None
    if row.household_id != household_id:
        return None
    user = db.get(User, row.user_id)
    if user is None or not user.is_active:
        return None
    member = membership_in(db, user.id, household_id)
    if member is None:
        return None
    principal = Principal(
        user=user,
        household_id=household_id,
        role=member.role,
        auth_method="session",
        scopes=None,
        session_id=row.id,
        mfa_verified_at=row.mfa_verified_at,
        login_method=row.login_method,
        mfa_enrolled=has_primary_mfa(db, user.id),
        mfa_required=require_mfa,
    )
    if principal.restricted:
        return None
    apply_rls_context(db, principal)
    if sees_all_accounts(principal):
        return StreamView(principal, None)
    return StreamView(principal, frozenset(visible_account_ids(db, principal)))


def fetch_events(
    db: Session,
    view: StreamView,
    *,
    after: uuid.UUID,
    not_before: datetime,
    limit: int,
) -> list[Event]:
    """Events of the household with ``id > after`` (and not older than retention)."""
    apply_rls_context(db, view.principal)
    return list(
        db.execute(
            select(Event)
            .where(
                Event.household_id == view.household_id,
                Event.id > after,
                Event.created_at >= not_before,
            )
            .order_by(Event.id)
            .limit(limit)
        ).scalars()
    )


def _uuids(values: Iterable[Any]) -> set[uuid.UUID]:
    found: set[uuid.UUID] = set()
    for value in values:
        try:
            found.add(uuid.UUID(str(value)))
        except (TypeError, ValueError):
            continue
    return found


def named_accounts(payload: dict[str, Any]) -> set[uuid.UUID]:
    values: list[Any] = []
    single = payload.get("account_id")
    if single is not None:
        values.append(single)
    many = payload.get("account_ids")
    if isinstance(many, list):
        values.extend(many)
    return _uuids(values)


def named_document(event: Event) -> uuid.UUID | None:
    payload = event.payload or {}
    if event.type.startswith("document."):
        found = _uuids([payload.get("id")])
    elif event.type.startswith("review."):
        found = _uuids([payload.get("document_id")])
    else:
        return None
    return next(iter(found), None)


def hidden_documents(db: Session, view: StreamView, ids: set[uuid.UUID]) -> set[uuid.UUID]:
    """Of ``ids``, the documents that exist in the household but are hidden from the view."""
    if not ids:
        return set()
    existing = set(
        db.execute(
            select(Document.id).where(
                Document.household_id == view.household_id, Document.id.in_(ids)
            )
        ).scalars()
    )
    if not existing:
        return set()
    visible = set(
        db.execute(
            select(Document.id).where(
                Document.id.in_(existing), visible_document_filter(view.principal)
            )
        ).scalars()
    )
    return existing - visible


def deliverable(db: Session, view: StreamView, events: Sequence[Event]) -> list[Event]:
    """The events the view may be told about (module doc)."""
    if view.accounts is None:
        return list(events)
    user = str(view.principal.user_id)
    candidates: list[Event] = []
    for event in events:
        payload = event.payload or {}
        if payload.get("user_id") == user:
            candidates.append(event)
            continue
        accounts = named_accounts(payload)
        if accounts and not accounts & view.accounts:
            continue
        required = payload.get(ALL_ACCOUNTS_KEY)
        if isinstance(required, list) and not _uuids(required) <= view.accounts:
            continue
        if (
            not accounts
            and not isinstance(required, list)
            and event.type.startswith(ACCOUNT_SCOPED_PREFIXES)
        ):
            continue
        if event.type == "document.deleted" and not accounts and payload.get("uploaded_by") != user:
            continue
        candidates.append(event)
    documents = {d for d in (named_document(e) for e in candidates) if d is not None}
    hidden = hidden_documents(db, view, documents)
    if not hidden:
        return candidates
    return [e for e in candidates if named_document(e) not in hidden]


def frame(event_id: uuid.UUID | None, data: dict[str, str]) -> bytes:
    """One SSE message: ``id`` (when given) and a JSON ``data`` line."""
    body = json.dumps(data, separators=(",", ":"), ensure_ascii=True)
    head = f"id: {event_id}\n" if event_id is not None else ""
    return f"{head}data: {body}\n\n".encode("ascii")


def event_frame(event: Event) -> bytes:
    """The wire form of an event: its id and type only."""
    return frame(event.id, {"id": str(event.id), "type": event.type})
