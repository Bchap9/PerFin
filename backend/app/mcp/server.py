# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin mcp``: the read-only MCP protocol server (AI_ACCESS.md, ADR-0007).

PerFin contains no model client. This module serves the Model Context
Protocol (the ``mcp`` SDK) to whatever assistant host the household chose;
the host and its model are outside PerFin. The process:

* holds one ``ai:read`` API token (:mod:`app.mcp.config`) and reads data only
  through ``GET /api/v1/ai/v1/*`` (:mod:`app.mcp.client`); it imports no
  database, crypto, settings or domain module, which
  ``tests/security/mcp/test_mcp_boundaries.py`` enforces (M7);
* offers only the read-only tools of :mod:`app.mcp.tools`, refuses unknown
  arguments (:class:`UnknownArguments`) and rate limits tool calls;
* scrubs every answer a second time (:mod:`app.mcp.scrub`, M2);
* speaks **stdio** by default. The **streamable HTTP** transport
  (:func:`http_app`) is meant to sit behind the TLS reverse proxy; it requires
  the MCP-client bearer secret ``PERFIN_MCP_HTTP_TOKEN`` on every request
  (constant-time comparison) and refuses a request whose ``Origin`` header is
  not in ``PERFIN_MCP_HTTP_ALLOWED_ORIGINS`` (DNS rebinding, M4). Requests
  without ``Origin`` (non-browser MCP hosts) are allowed with the bearer.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterable
from typing import Any, Final

import anyio
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.logging import get_logger
from app.core.scrubber import ACCT, ADDRESS, EMAIL, GOVID, PHONE
from app.mcp.client import AiApi
from app.mcp.config import McpConfig
from app.mcp.tools import TOOL_NAMES, RateLimiter, ToolRuntime, register

log = get_logger("perfin.mcp")

SERVER_NAME: Final = "perfin"
INSTRUCTIONS: Final = (
    "Read-only access to one PerFin household's financial facts (balances, spending, "
    "budgets, goals, investments, loans, tax totals). Identity is never exposed: names, "
    "addresses, account and card numbers, government ids, e-mail addresses, phone numbers "
    "and documents are absent or replaced by placeholders such as [ACCT] or [MEMBER]. "
    "Nothing can be changed through this server; suggest changes to the user instead. "
    "Call explain_data_policy to see what this client may read."
)
MCP_PATH: Final = "/mcp"


class UnknownArguments:
    """Refuse a ``tools/call`` whose arguments name a parameter the tool lacks.

    The SDK's argument model ignores unknown keys; a mistyped filter would then
    silently widen the query, so it is an error instead.
    """

    def __init__(self, server: MCPServer) -> None:
        self._server = server
        self._known: dict[str, frozenset[str]] | None = None

    async def _params(self) -> dict[str, frozenset[str]]:
        if self._known is None:
            tools = await self._server.list_tools()
            self._known = {
                tool.name: frozenset((tool.input_schema or {}).get("properties", {}))
                for tool in tools
            }
        return self._known

    async def __call__(
        self, ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        if ctx.method == "tools/call" and ctx.params is not None:
            name = ctx.params.get("name")
            arguments = ctx.params.get("arguments") or {}
            known = (await self._params()).get(str(name))
            if known is not None and isinstance(arguments, dict):
                extra = sorted(str(k) for k in arguments if k not in known)
                if extra:
                    return CallToolResult(
                        content=[
                            TextContent(
                                type="text",
                                text=f"Unknown argument(s) for {name}: {', '.join(extra)[:200]}",
                            )
                        ],
                        is_error=True,
                    )
        return await call_next(ctx)


def build_server(api: AiApi, *, rate_per_minute: int, transport: str = "stdio") -> MCPServer:
    """The MCP server with the read-only tool set over ``api``."""
    from app import __version__

    server: MCPServer = MCPServer(
        SERVER_NAME,
        title="PerFin (read-only)",
        instructions=INSTRUCTIONS,
        version=__version__,
        log_level="WARNING",
    )
    runtime = ToolRuntime(api, limiter=RateLimiter(rate_per_minute))
    register(
        server,
        runtime,
        mcp_info={
            "transport": transport,
            "tools": list(TOOL_NAMES),
            "write_tools": [],
            "rate_limit": f"{rate_per_minute} calls per 60 s",
            "second_stage_scrub": [ACCT, GOVID, EMAIL, PHONE, ADDRESS],
        },
    )
    server.middleware.append(UnknownArguments(server))
    return server


# ---- HTTP transport ----------------------------------------------------------


class HttpGuard:
    """ASGI guard in front of the streamable-HTTP app (module doc)."""

    def __init__(self, app: ASGIApp, *, token: str, allowed_origins: Iterable[str]) -> None:
        self._app = app
        self._token = token.encode()
        self._origins = frozenset(o.rstrip("/") for o in allowed_origins)

    async def _deny(self, send: Send, status: int, reason: bytes) -> None:
        headers = [
            (b"content-type", b"text/plain; charset=utf-8"),
            (b"cache-control", b"no-store"),
        ]
        if status == 401:
            headers.append((b"www-authenticate", b'Bearer realm="perfin-mcp"'))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body", "body": reason})

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._app(scope, receive, send)
            return
        if scope["type"] != "http":
            await self._deny(send, 404, b"not found")
            return
        headers = {k.decode("latin-1").lower(): v for k, v in scope.get("headers", [])}
        origin = headers.get("origin")
        if origin is not None and origin.decode("latin-1").rstrip("/") not in self._origins:
            log.warning("mcp http request from a disallowed origin", extra={"status": 403})
            await self._deny(send, 403, b"origin not allowed")
            return
        auth = headers.get("authorization", b"")
        scheme, _, presented = auth.partition(b" ")
        if scheme.lower() != b"bearer" or not hmac.compare_digest(presented.strip(), self._token):
            log.warning("mcp http request without a valid bearer", extra={"status": 401})
            await self._deny(send, 401, b"unauthorized")
            return
        await self._app(scope, receive, send)


def http_app(server: MCPServer, *, token: str, allowed_origins: Iterable[str]) -> ASGIApp:
    """The streamable-HTTP app at :data:`MCP_PATH`, wrapped by :class:`HttpGuard`."""
    origins = tuple(allowed_origins)
    inner = server.streamable_http_app(
        streamable_http_path=MCP_PATH,
        stateless_http=True,
        json_response=True,
        # Origin and bearer are checked by HttpGuard; the Host header is the
        # proxy's public name, which the guard does not need to know.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return HttpGuard(inner, token=token, allowed_origins=origins)


# ---- Entry point -------------------------------------------------------------


def run(config: McpConfig) -> None:
    """Serve until the transport closes (stdio) or the process is stopped (HTTP)."""
    api = AiApi.connect(config.api_url, config.token, timeout=config.api_timeout_seconds)
    server = build_server(api, rate_per_minute=config.rate_per_minute, transport=config.transport)
    log.info(
        "perfin mcp starting",
        extra={"transport": config.transport, "tools": len(TOOL_NAMES)},
    )
    try:
        if config.transport == "stdio":
            anyio.run(server.run_stdio_async)
        else:
            import uvicorn

            if config.http_token is None:  # checked by app.mcp.config
                raise RuntimeError("PERFIN_MCP_HTTP_TOKEN is required for the HTTP transport")
            app = http_app(
                server, token=config.http_token, allowed_origins=config.http_allowed_origins
            )
            uvicorn.run(
                app,
                host=config.http_host,
                port=config.http_port,
                proxy_headers=False,
                server_header=False,
                date_header=False,
                log_config=None,
                access_log=False,
                timeout_keep_alive=5,
            )
    finally:
        api.close()
