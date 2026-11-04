# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live updates: ``GET /events`` (Server-Sent Events; ``app.domain.events``).

A browser session (any household role) gets ``text/event-stream`` messages
``id: <event id>`` / ``data: {"id", "type"}`` for changes in its active
household, filtered to what it may see (account grants, document
visibility); payloads never leave the server, the client refetches. Personal
access tokens are refused (``403 session-required``): scripts poll the API.

``Last-Event-ID`` (or ``?last_event_id=``) resumes after an event within the
retention window. Comment lines keep the connection alive every
``PERFIN_SSE_HEARTBEAT_SECONDS``; a stream ends after
``PERFIN_SSE_MAX_STREAM_SECONDS`` and re-checks its session every
``PERFIN_SSE_REVALIDATE_SECONDS``. More than
``PERFIN_SSE_MAX_STREAMS_PER_USER`` concurrent streams is ``429
too-many-streams``. The request's own database transaction ends before
streaming starts; each read uses a short transaction of its own.
"""

from __future__ import annotations

import threading
from typing import Annotated, Any

import anyio.to_thread
import psycopg
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session, sessionmaker

from app.api.routers.accounts import PROBLEMS
from app.auth.authz import require_role
from app.auth.deps import DbSession, get_app_settings, session_acceptor
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.auth.sessions import SessionPolicy, raw_session_id
from app.core.clock import utcnow
from app.core.errors import ProblemError
from app.domain.events import service
from app.domain.events.stream import (
    StreamConfig,
    StreamDeps,
    StreamSlots,
    event_stream,
    open_listener,
    start_point,
)

router = APIRouter(tags=["events"])

_SLOTS_LOCK = threading.Lock()


def require_browser_viewer(
    principal: Annotated[Principal, Depends(require_role(HouseholdRole.VIEWER))],
) -> Principal:
    """Any household role, on a browser session (tokens poll the API instead)."""
    if principal.auth_method != "session":
        raise ProblemError(
            403, "session-required", "Browser session required", "Use the web app for this."
        )
    return principal


Viewer = Annotated[Principal, Depends(require_browser_viewer)]


def stream_slots(request: Request) -> StreamSlots:
    with _SLOTS_LOCK:
        slots = getattr(request.app.state, "sse_slots", None)
        if not isinstance(slots, StreamSlots):
            slots = StreamSlots()
            request.app.state.sse_slots = slots
        return slots


def too_many_streams() -> ProblemError:
    return ProblemError(
        429,
        "too-many-streams",
        "Too many live streams",
        "Close another tab of PerFin, or wait a moment.",
        headers={"Retry-After": "10"},
    )


def events_unavailable() -> ProblemError:
    return ProblemError(
        503,
        "events-unavailable",
        "Live updates unavailable",
        "Live updates are not available right now.",
        headers={"Retry-After": "10"},
    )


_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"description": "An event stream", "content": {"text/event-stream": {}}},
    **{k: v for k, v in PROBLEMS.items() if k in (403,)},
    **{
        code: {"description": "Problem details", "content": {"application/problem+json": {}}}
        for code in (429, 503)
    },
}


@router.get(
    "/events",
    response_class=StreamingResponse,
    responses=_RESPONSES,
    summary="Live updates (Server-Sent Events; browser sessions)",
)
async def get_events(
    request: Request,
    db: DbSession,
    principal: Viewer,
    last_event_id: Annotated[str | None, Query(max_length=64)] = None,
) -> StreamingResponse:
    settings = get_app_settings(request)
    household_id = principal.household_id
    if household_id is None:  # pragma: no cover - require_role guarantees it
        raise events_unavailable()
    # The guard's transaction (session lookup, RLS context) ends here; the
    # stream must not hold a pooled connection or row lock while it runs.
    await anyio.to_thread.run_sync(db.commit)
    config = StreamConfig.from_settings(settings)
    slots = stream_slots(request)
    token = slots.acquire(
        principal.user_id,
        limit=settings.sse_max_streams_per_user,
        lease_seconds=config.max_seconds + 60,
    )
    if token is None:
        raise too_many_streams()
    user_id = principal.user_id

    def release() -> None:
        slots.release(user_id, token)

    try:
        conn = await open_listener(settings)
    except psycopg.Error:
        release()
        raise events_unavailable() from None
    raw_id = raw_session_id(request, settings)
    policy = SessionPolicy.from_settings(settings)
    accept = session_acceptor(request)

    def refresh(session: Session) -> service.StreamView | None:
        return service.refresh_view(
            session,
            raw_id,
            household_id=household_id,
            policy=policy,
            require_mfa=settings.require_mfa,
            accept=accept,
        )

    factory: sessionmaker[Session] = request.app.state.sessionmaker
    start = start_point(
        request.headers.get("last-event-id") or last_event_id,
        retention=config.retention,
        now=utcnow(),
    )
    deps = StreamDeps(factory=factory, refresh=refresh, config=config, release=release)
    return StreamingResponse(
        event_stream(conn, deps, start),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
