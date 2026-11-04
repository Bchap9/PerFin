# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MCP process boundary (AI_ACCESS.md M7, M3; SECURITY.md E2, E4).

* No module of ``app/mcp`` imports a database, crypto, settings, auth, domain,
  API or ingestion module (AST walk, imports inside functions included), and
  a fresh interpreter that builds the server and the HTTP app has none of
  them (nor SQLAlchemy or psycopg) in ``sys.modules``.
* The only way to PerFin data is ``AiApi.get`` on ``/api/v1/ai/v1``: no other
  HTTP method is called anywhere in ``app/mcp``.
"""

from __future__ import annotations

import ast
import json
import subprocess  # nosec B404 - runs this interpreter on a fixed script
import sys
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.mcp.client import AI_PREFIX, AiApi, ApiRefusalError
from tests.unit.test_import_boundaries import imported_modules

pytestmark = pytest.mark.security

BACKEND = Path(__file__).resolve().parents[3]
MCP_DIR = BACKEND / "app" / "mcp"
#: Prefixes the MCP process must never import (M7: no DB, keys or documents).
FORBIDDEN = (
    "app.db",
    "app.core.crypto",
    "app.core.archive_crypto",
    "app.core.settings",
    "app.core.config_check",
    "app.core.audit",
    "app.auth",
    "app.domain",
    "app.api",
    "app.ingestion",
    "app.workers",
    "app.integrations",
    "app.main",
    "sqlalchemy",
    "psycopg",
    "alembic",
)
#: The app modules the MCP package may use.
ALLOWED_APP = ("app", "app.core.scrubber", "app.core.logging", "app.mcp")


def _forbidden(name: str) -> bool:
    return any(name == p or name.startswith(p + ".") for p in FORBIDDEN)


def _sources() -> list[Path]:
    files = sorted(MCP_DIR.rglob("*.py"))
    assert MCP_DIR / "server.py" in files
    return files


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_mcp_modules_import_nothing_forbidden(path: Path) -> None:
    module = "app.mcp." + path.stem if path.stem != "__init__" else "app.mcp"
    names = imported_modules(path.read_text(), module, is_package=path.stem == "__init__")
    assert not {n for n in names if _forbidden(n)}
    app_names = {n for n in names if n == "app" or n.startswith("app.")}
    stray = {
        n
        for n in app_names
        if not any(n == a or n.startswith(a + ".") for a in ALLOWED_APP) and n != "app.__version__"
    }
    assert stray == set(), stray


def test_boundary_check_catches_a_forbidden_import() -> None:
    names = imported_modules(
        "def f():\n    from app.db.session import make_app_engine\n", "app.mcp.x"
    )
    assert any(_forbidden(n) for n in names)


_PROBE = """
import json, os, sys
os.environ.update({
    "PERFIN_MCP_ENABLED": "true",
    "PERFIN_MCP_API_URL": "http://api.invalid:8000",
    "PERFIN_MCP_TOKEN": "pfa_probe_token_value_0123456789",
})
from app.cli import build_parser
from app.mcp.client import AiApi
from app.mcp.config import load
from app.mcp.server import build_server, http_app
config = load()
api = AiApi.connect(config.api_url, config.token)
server = build_server(api, rate_per_minute=config.rate_per_minute)
http_app(server, token="t" * 40, allowed_origins=["https://ai.example.test"])
build_parser()
print(json.dumps(sorted(sys.modules)))
"""


def test_a_running_mcp_process_loads_no_database_or_key_module() -> None:
    result = subprocess.run(  # nosec B603 - fixed argv, this interpreter
        [sys.executable, "-I", "-c", _PROBE],
        cwd=BACKEND,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(BACKEND)},
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout.strip().splitlines()[-1])
    assert "app.mcp.server" in loaded
    assert "app.core.scrubber" in loaded
    assert sorted(n for n in loaded if _forbidden(n)) == []


def _http_method_calls(tree: ast.AST) -> set[str]:
    methods = {"post", "put", "patch", "delete", "request", "stream", "send", "head", "options"}
    found: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in methods
            and isinstance(node.func.value, ast.Attribute | ast.Name)
            and "http" in ast.unparse(node.func.value).lower()
        ):
            found.add(ast.unparse(node.func))
    return found


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_http_method_but_get_reaches_the_api(path: Path) -> None:
    assert _http_method_calls(ast.parse(path.read_text())) == set()


def test_api_client_reaches_only_the_ai_views() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    api = AiApi.over(
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api"), "tok"
    )
    assert api.get("/policy") == {"ok": True}
    assert seen[0].method == "GET"
    assert seen[0].url.path == f"{AI_PREFIX}/policy"
    assert seen[0].headers["authorization"] == "Bearer tok"
    for bad in ("/../auth/session", "policy", "/policy?x=1", "/a b", "//evil.example/x"):
        with pytest.raises(ValueError, match="invalid AI view path"):
            api.get(bad)
    public = {name for name in vars(AiApi) if not name.startswith("_")}
    assert public == {"connect", "over", "close", "get"}


def test_redirects_are_not_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://elsewhere.example/steal"})

    api = AiApi.connect("http://api", "tok")
    http = cast(httpx.Client, api._http)
    assert http.follow_redirects is False
    http._transport = httpx.MockTransport(handler)

    with pytest.raises(ApiRefusalError) as caught:
        api.get("/policy")
    assert caught.value.status == 302
