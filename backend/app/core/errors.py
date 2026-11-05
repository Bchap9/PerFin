# SPDX-License-Identifier: AGPL-3.0-or-later
"""RFC 9457 problem details (SECURITY.md F5).

Every error leaves the API as ``application/problem+json`` with a stable
``type`` of ``urn:perfin:problem:<slug>``. Application code raises
:class:`ProblemError`; framework errors (404/405, request validation) and
unexpected exceptions are converted by the handlers installed with
:func:`install_error_handlers` and the :class:`UnhandledErrorMiddleware`.

What never reaches a client: stack traces, exception messages from
unexpected errors, SQL, internal identifiers, and the *input values* of a
request that failed validation (only field paths and generic messages are
returned; the values may be secrets or account numbers).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.request_context import request_id_from_scope, route_template

PROBLEM_MEDIA_TYPE = "application/problem+json"
PROBLEM_TYPE_PREFIX = "urn:perfin:problem:"

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# Members a caller may not overwrite through ``extra``.
_RESERVED_MEMBERS = frozenset({"type", "title", "status", "detail", "instance"})

logger = logging.getLogger("perfin.errors")


def problem_type(slug: str) -> str:
    """The ``type`` URI for ``slug``."""
    return f"{PROBLEM_TYPE_PREFIX}{slug}"


def _slug_for_status(status: int) -> str:
    try:
        phrase = HTTPStatus(status).phrase
    except ValueError:
        return "error"
    return re.sub(r"[^a-z0-9]+", "-", phrase.lower()).strip("-") or "error"


def _title_for_status(status: int) -> str:
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


class ProblemError(StarletteHTTPException):
    """An error with an RFC 9457 body.

    ``ProblemError(404, "account-not-found", "Account not found")``. ``detail``
    is a human-readable explanation specific to this occurrence; ``extra``
    adds extension members (never ``type``/``title``/``status``/``detail``).
    It subclasses Starlette's ``HTTPException`` so FastAPI re-raises it from
    dependencies and body parsing instead of wrapping it.
    """

    def __init__(
        self,
        status: int,
        slug: str,
        title: str,
        detail: str | None = None,
        extra: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        if not 400 <= status <= 599:
            raise ValueError("problem status must be 4xx or 5xx")
        if not _SLUG_RE.fullmatch(slug):
            raise ValueError("problem slug must be lowercase kebab-case")
        if extra is not None and _RESERVED_MEMBERS.intersection(extra):
            raise ValueError("extra may not override standard problem members")
        super().__init__(status_code=status, detail=detail, headers=dict(headers or {}))
        self.status = status
        self.slug = slug
        self.title = title
        self.problem_detail = detail
        self.extra: dict[str, Any] = dict(extra or {})


def problem_body(
    status: int,
    slug: str,
    title: str,
    detail: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The JSON object of a problem response."""
    body: dict[str, Any] = {"type": problem_type(slug), "title": title, "status": status}
    if detail:
        body["detail"] = detail
    if extra:
        body.update({k: v for k, v in extra.items() if k not in _RESERVED_MEMBERS})
    return body


def problem_response(
    status: int,
    slug: str,
    title: str,
    detail: str | None = None,
    extra: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    """A ready ``application/problem+json`` response (always ``no-store``)."""
    merged = dict(headers or {})
    merged["Cache-Control"] = "no-store"
    return JSONResponse(
        problem_body(status, slug, title, detail, extra),
        status_code=status,
        media_type=PROBLEM_MEDIA_TYPE,
        headers=merged,
    )


def _error_location(loc: Sequence[Any]) -> list[str | int]:
    return [part if isinstance(part, int) else str(part) for part in loc]


def validation_errors(errors: Sequence[Any]) -> list[dict[str, Any]]:
    """Reduce Pydantic/FastAPI errors to ``{loc, msg, type}``.

    Drops ``input``, ``ctx`` and ``url``: the input is whatever the client
    sent (possibly a password or an account number) and ``ctx`` can embed it.
    """
    cleaned: list[dict[str, Any]] = []
    for error in errors:
        if not isinstance(error, Mapping):
            continue
        cleaned.append(
            {
                "loc": _error_location(error.get("loc", ())),
                "msg": str(error.get("msg", "Invalid value")),
                "type": str(error.get("type", "value_error")),
            }
        )
    return cleaned


async def _handle_problem(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ProblemError):  # registered for ProblemError only
        raise exc
    return problem_response(
        exc.status, exc.slug, exc.title, exc.problem_detail, exc.extra, exc.headers
    )


async def _handle_validation(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    return problem_response(
        422,
        "validation-failed",
        "Request validation failed",
        "One or more fields are invalid.",
        {"errors": validation_errors(exc.errors())},
    )


async def _handle_http_exception(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    status = exc.status_code
    title = _title_for_status(status)
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail != title else None
    if status >= 500:
        detail = None  # framework 5xx text is not for clients
    return problem_response(status, _slug_for_status(status), title, detail, None, exc.headers)


def internal_error_response() -> JSONResponse:
    """The generic 500 body: no message, no trace, no identifiers."""
    return problem_response(500, "internal-error", "Internal Server Error")


def _log_unhandled(scope: Scope, exc: BaseException) -> None:
    logger.error(
        "unhandled exception",
        exc_info=(type(exc), exc, exc.__traceback__),
        extra={
            "request_id": request_id_from_scope(scope),
            "method": scope.get("method"),
            "route": route_template(scope),
        },
    )


async def _handle_unexpected(request: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler (Starlette's ServerErrorMiddleware) for errors raised
    outside :class:`UnhandledErrorMiddleware`, e.g. in an outer middleware."""
    _log_unhandled(request.scope, exc)
    return internal_error_response()


class UnhandledErrorMiddleware:
    """Turn any exception that escapes the routes into a generic 500 problem.

    Installed inside the security-header and request-id middleware so the 500
    still carries security headers and ``X-Request-ID``. The stack trace is
    logged with the request id; the client sees none of it. If the response
    had already started the exception is re-raised (the server aborts the
    connection).
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def send_wrapper(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        except Exception as exc:
            if started:
                raise
            _log_unhandled(scope, exc)
            await internal_error_response()(scope, receive, send)


def install_error_handlers(app: FastAPI) -> None:
    """Register the problem-details exception handlers on ``app``."""
    app.add_exception_handler(ProblemError, _handle_problem)
    app.add_exception_handler(RequestValidationError, _handle_validation)
    app.add_exception_handler(StarletteHTTPException, _handle_http_exception)
    app.add_exception_handler(Exception, _handle_unexpected)
