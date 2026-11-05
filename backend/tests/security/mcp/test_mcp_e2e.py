# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 6b end to end: owner flow -> ``perfin mcp`` over stdio -> audit -> off switches.

One story, through the real interfaces (AI_ACCESS.md "Client management",
"The MCP server"; SECURITY.md E1-E4; M1-M7):

1. The household owner (identity planted everywhere, ``build_world``) holds a
   browser session whose step-up is stale: creating an AI client is refused
   with ``step-up-required``; ``POST /auth/step-up`` with a TOTP code rotates
   the session and the create succeeds, returning the ``ai:read`` token once.
2. The owner reads the saved client's exposure preview: every tool renders,
   nothing planted appears, nothing is stored.
3. ``perfin mcp`` runs as a real subprocess over stdio with only its token
   and the URL of a live (uvicorn) API in its environment. An MCP client
   session (the ``mcp`` SDK) lists the tools and calls several of them.
   No planted identity appears in any answer, and each answer equals the
   ``ai_call`` audit row the API wrote for it, which carries none either.
4. The household flag and the instance kill switch (both changed over HTTP,
   step-up) make the running MCP server's tools answer "refused by policy"
   and turning them back on resumes the client.
5. Revoking the client over HTTP stops the token at once: the next tool call
   reports the token as not accepted, and the API answers ``401``.

The owner and admin requests go through an in-process ``TestClient`` of the
same application and database (session cookies are ``Secure``); the MCP
process reaches the API over real HTTP.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import pytest
import uvicorn
from fastapi.testclient import TestClient
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult, TextContent
from sqlalchemy import Engine, func, select, update

from app.auth.models import ApiToken, UserSession
from app.core import clock
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.ai.models import AiCall, AiClient
from app.main import create_app
from app.mcp.tools import TOOL_NAMES
from tests.fixtures.ai import ai_headers
from tests.fixtures.factories import Factories
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.mfa import STEP_UP, adopt_session_cookie, unsafe_headers
from tests.security.ai.test_ai_views import World, build_world, leaks

pytestmark = [pytest.mark.security, pytest.mark.integration]

BACKEND = Path(__file__).resolve().parents[3]
API = "/api/v1"
SETTINGS = f"{API}/settings/ai"

#: Tools called over stdio and the arguments used.
CALLS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("explain_data_policy", {}),
    ("list_accounts", {}),
    ("get_spending", {"period": "last_12_months", "group_by": "payee"}),
    ("list_transactions", {"period": "last_90_days"}),
    ("get_net_worth_history", {"range": "1y"}),
    ("list_loans", {}),
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture
def live_api(settings: Settings) -> Iterator[str]:
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def world(factories: Factories, ledger: LedgerFactories) -> World:
    return build_world(factories, ledger)


@pytest.fixture
def web(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


def _text(result: CallToolResult) -> str:
    parts = [c.text for c in result.content if isinstance(c, TextContent)]
    assert len(parts) == len(result.content)
    return "\n".join(parts)


def _type(response: Any) -> str:
    return str(response.json()["type"]).rsplit(":", 1)[-1]


def _calls(factories: Factories, world: World) -> list[AiCall]:
    with factories.transaction() as db:
        set_context(db, world.household_id)
        rows = list(
            db.execute(
                select(AiCall)
                .where(AiCall.household_id == world.household_id)
                .order_by(AiCall.created_at, AiCall.id)
            ).scalars()
        )
        for row in rows:
            db.expunge(row)
    return rows


def _row_counts(factories: Factories, world: World) -> tuple[int, int, int]:
    with factories.transaction() as db:
        set_context(db, world.household_id)
        counts = tuple(
            int(
                db.execute(
                    select(func.count())
                    .select_from(model)
                    .where(model.household_id == world.household_id)
                ).scalar_one()
            )
            for model in (AiClient, AiCall, ApiToken)
        )
    return counts[0], counts[1], counts[2]


def _assert_clean(world: World, text: str, where: str) -> None:
    hits = leaks(text, world.usernames)
    if world.household_name.casefold() in text.casefold():
        hits.append("household name")
    assert hits == [], where


def test_phase6b_end_to_end(
    web: TestClient,
    live_api: str,
    factories: Factories,
    world: World,
    settings: Settings,
    admin_engine: Engine,
) -> None:
    owner = world.owner

    # 1. Create the client: a stale step-up is refused, a TOTP step-up passes.
    login = factories.login(web, owner, mfa_verified=True)
    with admin_engine.begin() as conn:
        conn.execute(
            update(UserSession)
            .where(UserSession.id == login.session_id)
            .values(mfa_verified_at=clock.utcnow() - timedelta(minutes=11))
        )
    body = {
        "name": "Desktop assistant",
        "mode": "detail",
        "expires_in_days": 30,
        "custom_denylist": ["Oakwood Clinic"],
    }
    stale = web.post(f"{SETTINGS}/clients", json=body, headers=login.headers())
    assert stale.status_code == 403
    assert _type(stale) == "step-up-required"
    stepped = web.post(
        STEP_UP, json={"method": "totp", "code": owner.totp_now()}, headers=login.headers()
    )
    assert stepped.status_code == 200, stepped.text
    adopt_session_cookie(web, settings, stepped)
    headers = unsafe_headers(stepped.json()["csrf_token"])
    created = web.post(f"{SETTINGS}/clients", json=body, headers=headers)
    assert created.status_code == 201, created.text
    client_id = created.json()["id"]
    raw = created.json()["token"]
    assert raw.startswith("pfn_")
    # The token is shown once: reading the client back never returns it.
    assert raw not in web.get(f"{SETTINGS}/clients/{client_id}").text
    assert web.get(f"{API}/auth/session").json()["ai_clients_active"] is True

    # 2. The exposure preview of the saved client: real data, nothing planted, nothing stored.
    before = _row_counts(factories, world)
    preview = web.get(f"{SETTINGS}/clients/{client_id}/preview")
    assert preview.status_code == 200, preview.text
    tools = {t["tool"]: t for t in preview.json()["tools"]}
    assert set(tools) == set(TOOL_NAMES)
    assert all(t["status"] == 200 for t in tools.values())
    assert tools["list_transactions"]["body"]["items"]
    _assert_clean(world, preview.text, "preview")
    assert _row_counts(factories, world) == before

    # 3-5. perfin mcp over stdio against the live API, with only its token.
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.cli", "mcp"],
        cwd=str(BACKEND),
        env={
            "PERFIN_MCP_ENABLED": "true",
            "PERFIN_MCP_API_URL": live_api,
            "PERFIN_MCP_TOKEN": raw,
            "PERFIN_LOG_LEVEL": "WARNING",
            "PYTHONPATH": os.environ.get("PYTHONPATH", str(BACKEND)),
        },
    )
    admin = factories.user(role=None, is_instance_admin=True, with_totp=True)
    admin_web = TestClient(web.app, base_url="https://testserver")
    admin_login = factories.login(admin_web, admin, mfa_verified=True)

    def owner_patch(enabled: bool) -> None:
        response = web.patch(SETTINGS, json={"enabled": enabled}, headers=headers)
        assert response.status_code == 200, response.text
        assert response.json()["household_enabled"] is enabled

    def admin_patch(enabled: bool) -> None:
        response = admin_web.patch(
            f"{API}/settings/system/ai", json={"enabled": enabled}, headers=admin_login.headers()
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"enabled": enabled}

    def revoke() -> None:
        response = web.delete(f"{SETTINGS}/clients/{client_id}", headers=headers)
        assert response.status_code == 204

    async def blocking(fn: Callable[[], None]) -> None:
        await anyio.to_thread.run_sync(fn)

    async def main() -> dict[str, Any]:
        out: dict[str, Any] = {}
        async with Client(params) as mcp:
            out["names"] = sorted(t.name for t in (await mcp.list_tools()).tools)
            out["answers"] = [(name, await mcp.call_tool(name, args)) for name, args in CALLS]

            await blocking(lambda: owner_patch(False))
            out["household_off"] = await mcp.call_tool("get_spending", {"period": "2026"})
            await blocking(lambda: owner_patch(True))
            out["household_on"] = await mcp.call_tool("explain_data_policy", {})

            await blocking(lambda: admin_patch(False))
            try:
                out["instance_off"] = await mcp.call_tool("list_accounts", {})
            finally:
                await blocking(lambda: admin_patch(True))
            out["instance_on"] = await mcp.call_tool("list_accounts", {})

            await blocking(revoke)
            out["revoked"] = await mcp.call_tool("list_accounts", {})
        return out

    out = anyio.run(main)

    # The tool list is exactly the read-only set.
    assert out["names"] == sorted(TOOL_NAMES)
    answers: list[tuple[str, CallToolResult]] = out["answers"]
    assert [(name, r.is_error) for name, r in answers] == [(name, False) for name, _ in CALLS]
    for name, result in answers:
        _assert_clean(world, _text(result), name)
    txns = json.loads(_text(answers[3][1]))["items"]
    assert txns
    assert any(t["memo"] == "ZELLE TO [MEMBER] [ACCT]" for t in txns)

    # The switches stop the running MCP server and resume it.
    for key in ("household_off", "instance_off"):
        refused: CallToolResult = out[key]
        assert refused.is_error, key
        assert "refused" in _text(refused), key
        assert "ai-access-disabled" in _text(refused), key
    assert not out["household_on"].is_error
    assert not out["instance_on"].is_error

    # Revocation is immediate.
    revoked: CallToolResult = out["revoked"]
    assert revoked.is_error
    assert "did not accept this MCP server's token" in _text(revoked)
    assert web.get(f"{API}/ai/v1/policy", headers=ai_headers(raw)).status_code == 401
    assert web.get(f"{SETTINGS}/clients/{client_id}").json()["status"] == "revoked"
    assert web.get(f"{API}/auth/session").json()["ai_clients_active"] is False

    # The audit: one row per answered call (switch refusals happen before the
    # view and are not audited), each the exact answer the MCP client got,
    # none carrying planted identity.
    rows = _calls(factories, world)
    answered = [*[name for name, _ in CALLS], "explain_data_policy", "list_accounts"]
    assert [row.tool for row in rows] == answered
    assert all(row.status == 200 for row in rows)
    assert all(str(row.ai_client_id) == client_id for row in rows)
    mcp_bodies = [json.loads(_text(r)) for _, r in answers]
    # explain_data_policy gains the MCP server's own section (its rate limit,
    # the second-stage scrub, the transport); everything else is unchanged.
    policy = mcp_bodies[0]
    assert policy.pop("mcp_server")["write_tools"] == []
    assert [row.response for row in rows[: len(CALLS)]] == mcp_bodies
    for row in rows:
        _assert_clean(world, json.dumps(row.response) + json.dumps(row.params), row.tool)
    # The owner can search the audit for something that must never have left.
    found = web.get(f"{SETTINGS}/calls", params={"contains": "jane.doe@example.org"}).json()
    assert found["items"] == []
