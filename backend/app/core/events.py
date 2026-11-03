# SPDX-License-Identifier: AGPL-3.0-or-later
"""Household events: a row in ``event`` plus a ``NOTIFY`` for live clients.

``emit`` inserts the event and calls ``pg_notify('perfin_events', ...)`` in the
caller's transaction, so listeners hear about it only if (and when) the
change commits. The notification carries only ``{"id", "household_id",
"type"}``; the SSE layer checks the household against the subscriber and
reads the row (under RLS) if it needs the payload. Payloads hold ids and
small facts, never document text or secrets.

``event`` is ``HouseholdScoped``: ``emit`` must run in the RLS context of the
household it writes for (``WITH CHECK`` rejects anything else).
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final

from sqlalchemy import Connection, delete, insert, text
from sqlalchemy.orm import Session

from app.core.clock import utcnow
from app.core.ids import uuid7
from app.db.models import Event
from app.db.rls import get_context, restore_context, set_context

EVENTS_CHANNEL: Final = "perfin_events"
MAX_PAYLOAD_BYTES: Final = 16 * 1024
DEFAULT_RETENTION: Final = timedelta(days=7)

_TYPE_RE: Final = re.compile(r"^[a-z][a-z0-9_.:-]{0,99}$")


@dataclass(frozen=True)
class EventNotice:
    """What a ``LISTEN perfin_events`` connection receives."""

    id: uuid.UUID
    household_id: uuid.UUID
    type: str


def _encode_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if not isinstance(payload, Mapping):
        raise TypeError("event payload must be a mapping")
    data = dict(payload)
    try:
        encoded = json.dumps(data, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("event payload must be JSON-serialisable") from exc
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise ValueError(f"event payload exceeds {MAX_PAYLOAD_BYTES} bytes")
    loaded: dict[str, Any] = json.loads(encoded)
    return loaded


def emit(
    session: Session | Connection,
    household_id: uuid.UUID,
    type: str,
    payload: Mapping[str, Any] | None = None,
) -> uuid.UUID:
    """Insert an event and ``NOTIFY`` it on commit; return the event id."""
    if not isinstance(household_id, uuid.UUID):
        raise TypeError("household_id must be a UUID")
    if not _TYPE_RE.fullmatch(type):
        raise ValueError(f"invalid event type: {type!r}")
    data = _encode_payload(payload)
    event_id = uuid7()
    session.execute(
        insert(Event).values(id=event_id, household_id=household_id, type=type, payload=data)
    )
    notice = json.dumps(
        {"id": str(event_id), "household_id": str(household_id), "type": type},
        separators=(",", ":"),
    )
    session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": EVENTS_CHANNEL, "payload": notice},
    )
    return event_id


def parse_notice(payload: str) -> EventNotice | None:
    """Parse a ``perfin_events`` notification payload; ``None`` if malformed."""
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            return None
        kind = data["type"]
        if not isinstance(kind, str) or not _TYPE_RE.fullmatch(kind):
            return None
        return EventNotice(
            id=uuid.UUID(str(data["id"])),
            household_id=uuid.UUID(str(data["household_id"])),
            type=kind,
        )
    except (KeyError, TypeError, ValueError):
        return None


def prune_events(
    session: Session | Connection,
    *,
    older_than: timedelta = DEFAULT_RETENTION,
    household_ids: Iterable[uuid.UUID] | None = None,
    now: datetime | None = None,
) -> int:
    """Delete events created before ``now - older_than``; return the count.

    RLS stays in force: each household is pruned inside its own context
    (``household_ids`` defaults to every household, listed with
    ``perfin_household_ids()`` because ``household`` itself is under RLS). The
    caller's context is restored afterwards. Runs in the caller's transaction.
    """
    if older_than < timedelta(0):
        raise ValueError("older_than must not be negative")
    cutoff = (now or utcnow()) - older_than
    previous = get_context(session)
    ids = (
        list(household_ids)
        if household_ids is not None
        else [
            uuid.UUID(str(value))
            for value in session.execute(text("SELECT * FROM perfin_household_ids()")).scalars()
        ]
    )
    removed = 0
    try:
        for household_id in ids:
            set_context(session, household_id)
            result = session.execute(
                delete(Event)
                .where(
                    Event.household_id == household_id,
                    Event.created_at < cutoff,
                )
                .execution_options(synchronize_session=False)
            )
            removed += int(getattr(result, "rowcount", 0) or 0)
    finally:
        restore_context(session, previous)
    return removed
