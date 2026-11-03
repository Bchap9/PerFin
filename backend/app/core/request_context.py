# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-request context: request id, client address behind trusted proxies.

``RequestContextMiddleware`` runs outermost in the API stack (see
``app.main._install_middleware``). It

* resolves the real client address and scheme. ``X-Forwarded-For`` and
  ``X-Forwarded-Proto`` are honoured **only** when the TCP peer is inside one
  of ``PERFIN_TRUSTED_PROXIES``; otherwise they are ignored, so a client
  cannot spoof its address (break-glass CIDRs and rate limits rely on this).
  The forwarded chain is walked right to left, skipping trusted hops; the
  first untrusted address is the client.
* assigns a request id. A client-supplied ``X-Request-ID`` is used only when
  the peer is a trusted proxy and the value is well formed; otherwise a fresh
  UUIDv7 is generated. The id is echoed in the ``X-Request-ID`` response
  header, stored on ``request.state.request_id`` and in the
  :data:`request_id_var` context variable (the logging filter reads it).

uvicorn's own ``--proxy-headers`` handling is turned off in ``perfin api`` so
this is the only place forwarded headers are interpreted.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from contextvars import ContextVar
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address

from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import HTTPConnection
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.ids import uuid7

IPNetwork = IPv4Network | IPv6Network
IPAddress = IPv4Address | IPv6Address

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_STATE_KEY = "state"
_SCHEMES = {"http": ("http", "https"), "websocket": ("ws", "wss")}

request_id_var: ContextVar[str | None] = ContextVar("perfin_request_id", default=None)


def _parse_ip(value: str) -> IPAddress | None:
    candidate = value.strip()
    if candidate.startswith("[") and "]" in candidate:  # "[2001:db8::1]:443"
        candidate = candidate[1 : candidate.index("]")]
    try:
        return ip_address(candidate)
    except ValueError:
        return None


def is_trusted(address: str | None, trusted: Sequence[IPNetwork]) -> bool:
    """True when ``address`` is an IP inside one of ``trusted``."""
    if address is None or not trusted:
        return False
    ip = _parse_ip(address)
    return ip is not None and any(ip in net for net in trusted)


def resolve_client(
    peer: str | None, forwarded_for: str | None, trusted: Sequence[IPNetwork]
) -> str | None:
    """Return the client address given the TCP ``peer`` and ``X-Forwarded-For``.

    Forwarded headers count only when ``peer`` is trusted. Walking the chain
    from the right, trusted hops are skipped and the first other address is
    the client. A malformed entry stops the walk at the last good hop.
    """
    if not is_trusted(peer, trusted) or not forwarded_for:
        return peer
    client = peer
    for hop in reversed(forwarded_for.split(",")):
        ip = _parse_ip(hop)
        if ip is None:
            break
        client = str(ip)
        if not any(ip in net for net in trusted):
            break
    return client


def valid_request_id(value: str | None) -> bool:
    """A request id we are willing to propagate from a trusted proxy."""
    return value is not None and _REQUEST_ID_RE.fullmatch(value) is not None


def _state(scope: Scope) -> dict[str, object]:
    state = scope.get(_STATE_KEY)
    if not isinstance(state, dict):
        state = {}
        scope[_STATE_KEY] = state
    return state


def route_template(scope: Scope) -> str | None:
    """The matched route's full path template (``/api/v1/accounts/{id}``).

    FastAPI >= 0.16x keeps included routers nested, so ``scope["route"].path``
    lacks the include prefix; the full template is on the effective route
    context FastAPI records in ``scope["fastapi"]``. Falls back to the
    Starlette route's own path, then ``None`` when nothing matched.
    """
    fastapi_scope = scope.get("fastapi")
    if isinstance(fastapi_scope, dict):
        context = fastapi_scope.get("effective_route_context")
        path_format = getattr(context, "path_format", None)
        if isinstance(path_format, str) and path_format:
            return path_format
    route = scope.get("route")
    for attribute in ("path_format", "path"):
        value = getattr(route, attribute, None)
        if isinstance(value, str) and value:
            return value
    return None


def request_id_from_scope(scope: Scope) -> str | None:
    value = scope.get(_STATE_KEY, {}).get("request_id")
    return value if isinstance(value, str) else None


def request_id(conn: HTTPConnection) -> str | None:
    """The id assigned to this request by :class:`RequestContextMiddleware`."""
    return request_id_from_scope(conn.scope)


def client_ip(conn: HTTPConnection) -> str | None:
    """The client address, honouring forwarded headers only from trusted proxies.

    Falls back to the TCP peer when the middleware did not run (e.g. a bare
    test app).
    """
    value = conn.scope.get(_STATE_KEY, {}).get("client_ip")
    if isinstance(value, str):
        return value
    return conn.client.host if conn.client else None


class RequestContextMiddleware:
    """Assign the request id and resolve client address/scheme (see module doc)."""

    def __init__(self, app: ASGIApp, *, trusted_proxies: Sequence[IPNetwork] = ()) -> None:
        self.app = app
        self.trusted = tuple(trusted_proxies)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        client = scope.get("client")
        peer = client[0] if client else None
        from_proxy = is_trusted(peer, self.trusted)

        resolved = resolve_client(peer, headers.get("x-forwarded-for"), self.trusted)
        if from_proxy:
            proto = (headers.get("x-forwarded-proto") or "").strip().lower()
            if proto in _SCHEMES[scope["type"]]:
                scope["scheme"] = proto
            if resolved is not None and resolved != peer:
                port = client[1] if client else 0
                scope["client"] = (resolved, port)

        supplied = headers.get("x-request-id")
        rid = str(uuid7())
        if from_proxy and supplied is not None and valid_request_id(supplied):
            rid = supplied
        state = _state(scope)
        state["request_id"] = rid
        state["client_ip"] = resolved

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = rid
            await send(message)

        token = request_id_var.set(rid)
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)
