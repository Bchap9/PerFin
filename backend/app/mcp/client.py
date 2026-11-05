# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MCP process's only way to PerFin data: ``GET /api/v1/ai/v1/*`` with its token.

:class:`AiApi` wraps one ``httpx.Client`` that carries the AI client's
``ai:read`` bearer token. It exposes a single method, :meth:`AiApi.get`, for
paths under :data:`AI_PREFIX`; there is no way to send any other method or
reach any other route through it (E2/M3: no write exists). The API applies
the client's mode and options, scrubs free text and audits every call; this
module only transports.

Answers:

* ``200`` with a JSON object: returned as parsed JSON;
* a problem response (``application/problem+json``) or any other status:
  :class:`ApiRefusalError` with the status, the problem ``type`` slug and its
  title/detail. A ``403`` is a policy answer (AI access switched off,
  client revoked or expired, the view refused in Aggregates mode); a ``429``
  is the API's per-client rate limit.

Redirects are never followed, so the token is only ever sent to
``PERFIN_MCP_API_URL``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Final, Protocol

import httpx

AI_PREFIX: Final = "/api/v1/ai/v1"
_PATH_RE: Final = re.compile(r"^/[a-z0-9/_-]{1,200}$")
_PROBLEM_PREFIX: Final = "urn:perfin:problem:"
MAX_RESPONSE_BYTES: Final = 8 * 1024 * 1024

Params = Mapping[str, str | int | None]


class ApiRefusalError(Exception):
    """The API answered with an error (or not at all)."""

    def __init__(self, status: int, code: str, title: str, detail: str) -> None:
        super().__init__(status, code)
        self.status = status
        self.code = code
        self.title = title
        self.detail = detail

    def __str__(self) -> str:
        return f"{self.status} {self.code}: {self.title}" + (
            f" ({self.detail})" if self.detail else ""
        )


class HttpClient(Protocol):
    """What :class:`AiApi` needs of an ``httpx.Client`` (tests pass a ``TestClient``)."""

    headers: Any
    follow_redirects: bool

    def get(self, url: str, *, params: Any = ...) -> Any: ...

    def close(self) -> None: ...


def _problem(response: Any) -> ApiRefusalError:
    code, title, detail = "error", response.reason_phrase or "error", ""
    try:
        body = response.json()
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        body = None
    if isinstance(body, dict):
        raw_type = body.get("type")
        if isinstance(raw_type, str):
            code = raw_type.removeprefix(_PROBLEM_PREFIX)[:80]
        if isinstance(body.get("title"), str):
            title = str(body["title"])[:200]
        if isinstance(body.get("detail"), str):
            detail = str(body["detail"])[:500]
    return ApiRefusalError(response.status_code, code, title, detail)


class AiApi:
    """GET-only client of the AI views (module doc)."""

    def __init__(self, http: HttpClient) -> None:
        self._http = http

    @classmethod
    def connect(cls, base_url: str, token: str, *, timeout: float = 30.0) -> AiApi:
        """A client for ``base_url`` (``PERFIN_MCP_API_URL``) with the AI token."""
        return cls(
            httpx.Client(
                base_url=base_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                    "User-Agent": "perfin-mcp",
                },
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            )
        )

    @classmethod
    def over(cls, http: HttpClient, token: str) -> AiApi:
        """Use an existing ``httpx.Client`` (tests: a ``TestClient``) with the AI token."""
        http.headers["Authorization"] = f"Bearer {token}"
        http.headers["Accept"] = "application/json"
        http.follow_redirects = False
        return cls(http)

    def close(self) -> None:
        self._http.close()

    def get(self, path: str, params: Params | None = None) -> dict[str, Any]:
        """``GET {AI_PREFIX}{path}``; ``None`` parameters are left out."""
        if not _PATH_RE.match(path) or ".." in path:
            raise ValueError("invalid AI view path")
        query = {k: str(v) for k, v in (params or {}).items() if v is not None}
        try:
            response = self._http.get(AI_PREFIX + path, params=query)
        except httpx.HTTPError as exc:
            raise ApiRefusalError(
                502, "api-unreachable", "PerFin API unreachable", type(exc).__name__
            ) from exc
        if response.status_code != 200:
            raise _problem(response)
        if len(response.content) > MAX_RESPONSE_BYTES:
            raise ApiRefusalError(502, "response-too-large", "Response too large", "")
        try:
            body = response.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            raise ApiRefusalError(502, "bad-response", "The API sent no JSON", "") from exc
        if not isinstance(body, dict):
            raise ApiRefusalError(502, "bad-response", "The API sent no JSON object", "")
        return body
