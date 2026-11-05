# SPDX-License-Identifier: AGPL-3.0-or-later
"""Request body size limits at the application layer (SECURITY.md F8, C2).

The reverse proxy enforces the same limits (``deploy/Caddyfile.example``);
this is the second layer for deployments that skip or misconfigure it.
Every request body is capped at :data:`DEFAULT_MAX_BODY_BYTES` (1 MiB)
unless its exact path has an override (the document upload route gets
``PERFIN_UPLOAD_MAX_MB``). Both a declared ``Content-Length`` and the bytes
actually streamed (chunked bodies) are checked, so the cap holds without
trusting the header. Oversized requests get a ``413`` problem response.

Request timeouts (F8)
---------------------
Timeouts are layered rather than enforced by cancelling handlers (a
cancelled sync handler keeps running in its thread and could commit after
the client was told it failed):

* Postgres ``statement_timeout`` on every app connection
  (``PERFIN_DB_STATEMENT_TIMEOUT_MS``, default 30 s) bounds the work a
  request can make the database do.
* The reverse proxy bounds slow clients: Caddy's ``servers { timeouts {
  read_header 10s; read_body 60s; idle 2m } }`` (nginx:
  ``client_header_timeout``/``client_body_timeout``/``proxy_read_timeout``).
* uvicorn closes idle keep-alive connections after 5 s
  (``timeout_keep_alive``) and drains in-flight requests for at most 30 s
  on shutdown.
"""

from __future__ import annotations

from collections.abc import Mapping

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import ProblemError, problem_response

DEFAULT_MAX_BODY_BYTES = 1024 * 1024

_TITLE = "Request body too large"


def _too_large(limit: int) -> ProblemError:
    return ProblemError(
        413,
        "body-too-large",
        _TITLE,
        f"The request body exceeds the limit of {limit} bytes for this endpoint.",
    )


class BodySizeLimitMiddleware:
    """Reject request bodies larger than the limit for their path."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        default_limit: int = DEFAULT_MAX_BODY_BYTES,
        overrides: Mapping[str, int] | None = None,
    ) -> None:
        if default_limit < 0:
            raise ValueError("default_limit must be >= 0")
        self.app = app
        self.default_limit = default_limit
        # Held by reference: the app factory may register more paths later.
        self.overrides: Mapping[str, int] = overrides if overrides is not None else {}

    def limit_for(self, path: str) -> int:
        return self.overrides.get(path.rstrip("/") or "/", self.default_limit)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope.get("path", ""))
        declared = Headers(scope=scope).get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                declared_size = -1
            if declared_size < 0:
                response = problem_response(
                    400, "bad-request", "Bad Request", "Invalid Content-Length header."
                )
            elif declared_size > limit:
                exc = _too_large(limit)
                response = problem_response(exc.status, exc.slug, exc.title, exc.problem_detail)
            else:
                response = None
            if response is not None:
                response.headers["Connection"] = "close"
                await response(scope, receive, send)
                return

        received = 0
        started = False

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # An HTTPException subclass: FastAPI re-raises it from body
                    # parsing and the problem handler renders it.
                    raise _too_large(limit)
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except ProblemError as exc:
            if started or exc.status != 413:
                raise
            response = problem_response(exc.status, exc.slug, exc.title, exc.problem_detail)
            response.headers["Connection"] = "close"
            await response(scope, receive, send)
