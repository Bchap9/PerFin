# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MCP server end to end (AI_ACCESS.md M2, M3, M4, M6, M7; SECURITY.md E1, E2, E4).

The server runs in-process (the SDK's in-memory transport) against the real
API behind a ``TestClient``, reading the household of
``tests/security/ai/test_ai_views.py``: identity planted everywhere (a
member's name as a payee and in memos, an account's last four digits,
masked account and card numbers, an address in a manual asset and a memo,
an SSN and a phone number in notes, e-mail addresses, a denylisted clinic).

* No planted identity string appears in any tool output (answers and errors)
  for any mode/options combination: a hypothesis property over every tool x
  mode x rounding x hidden labels/location x date cap x denylist.
* The second-stage scrub works on its own: a fake API that leaks raw
  identifiers is scrubbed by the MCP process.
* The tool list is exactly the read-only set; unknown arguments, bad values
  and over-limit calls are refused; a ``403`` (AI access off, revoked client,
  Aggregates mode) is reported as policy.
* The HTTP transport needs the MCP-client bearer and an allowed origin.
* ``perfin mcp`` as a real subprocess over stdio, with nothing but its token
  and the API URL in its environment, answers through a live API.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import anyio
import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, example, given
from hypothesis import settings as hsettings
from hypothesis import strategies as st
from mcp import Client, StdioServerParameters
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent
from starlette.testclient import TestClient as StarletteClient

from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.ai import service as ai_service
from app.domain.ai.models import AiMode
from app.main import create_app
from app.mcp.client import AiApi
from app.mcp.server import MCP_PATH, build_server, http_app
from app.mcp.tools import TOOL_NAMES
from tests.fixtures.ai import ai_client, owner_principal
from tests.fixtures.factories import Factories
from tests.fixtures.ledger import LedgerFactories
from tests.security.ai.test_ai_views import TODAY, World, build_world, leaks

pytestmark = [pytest.mark.security, pytest.mark.integration]

BACKEND = Path(__file__).resolve().parents[3]


def tool_calls(world: World) -> list[tuple[str, dict[str, Any]]]:
    """Every tool, several with filters and groupings."""
    year = str(TODAY.year)
    return [
        ("explain_data_policy", {}),
        ("get_overview", {"period": "last_12_months"}),
        ("get_spending", {"period": year}),
        ("get_spending", {"period": year, "group_by": "payee"}),
        ("get_spending", {"period": year, "group_by": "merchant_category", "depth": 2}),
        ("get_spending", {"period": year, "group_by": "account"}),
        ("get_income", {"period": year, "group_by": "payee"}),
        ("get_budget_status", {}),
        ("get_trends", {"metric": "spending", "periods": 12}),
        ("get_recurring", {}),
        ("get_savings_goals", {}),
        ("get_net_worth_history", {"range": "1y"}),
        ("get_allocation", {}),
        ("list_accounts", {}),
        ("list_transactions", {"date_from": (TODAY - timedelta(days=400)).isoformat()}),
        # The payee as the assistant sees it (a guess of the hidden name
        # answers like a miss: P6B-SR-R1-03, test_ai_views).
        ("list_transactions", {"period": "last_90_days", "payee": "[MEMBER]"}),
        ("list_transactions", {"period": "last_90_days", "account": str(world.checking_id)}),
        ("find_anomalies", {"period": "last_90_days"}),
        ("list_holdings", {}),
        ("get_performance", {"range": "1y"}),
        ("list_loans", {}),
        ("get_loan_schedule", {"loan": str(world.loan_id), "limit": 24}),
        ("simulate_loan", {"loan": str(world.loan_id), "extra_payment": "200"}),
        ("get_tax_year_summary", {"year": TODAY.year - 1}),
    ]


def text_of(result: CallToolResult) -> str:
    parts = [c.text for c in result.content if isinstance(c, TextContent)]
    assert len(parts) == len(result.content)
    return "\n".join(parts)


@pytest.fixture
def world(factories: Factories, ledger: LedgerFactories) -> World:
    return build_world(factories, ledger)


@pytest.fixture
def api_app(settings: Settings) -> FastAPI:
    return create_app(settings)


class Harness:
    """Builds an MCP server for a token, talking to the API through a TestClient."""

    def __init__(self, app: FastAPI) -> None:
        self._app = app
        self._clients: list[TestClient] = []

    def server(self, raw_token: str, *, rate_per_minute: int = 600) -> MCPServer:
        http = TestClient(self._app, base_url="https://testserver")
        http.__enter__()
        self._clients.append(http)
        return build_server(AiApi.over(http, raw_token), rate_per_minute=rate_per_minute)

    def close(self) -> None:
        for http in self._clients:
            http.__exit__(None, None, None)


@pytest.fixture
def harness(api_app: FastAPI) -> Iterator[Harness]:
    h = Harness(api_app)
    yield h
    h.close()


def run_with(server: MCPServer, body: Callable[[Client], Awaitable[Any]]) -> Any:
    async def main() -> Any:
        async with Client(server) as client:
            return await body(client)

    return anyio.run(main)


def call_all(
    server: MCPServer, calls: list[tuple[str, dict[str, Any]]]
) -> list[tuple[str, CallToolResult]]:
    async def body(client: Client) -> list[tuple[str, CallToolResult]]:
        return [(name, await client.call_tool(name, args)) for name, args in calls]

    result: list[tuple[str, CallToolResult]] = run_with(server, body)
    return result


def assert_no_leak(
    world: World, outputs: list[tuple[str, CallToolResult]], *, denylist: bool
) -> None:
    extra = world.usernames
    found: dict[str, list[str]] = {}
    for name, result in outputs:
        text = text_of(result)
        hits = [v for v in leaks(text, extra) if denylist or v != "Oakwood"]
        if world.household_name.casefold() in text.casefold():
            hits.append("household name")
        if hits:
            found[name] = hits
    assert found == {}


# ---------------------------------------------------------------------------
# No identity leaves, for any tool x mode x options
# ---------------------------------------------------------------------------


def test_every_tool_answers_in_detail_mode_without_identity(
    harness: Harness, factories: Factories, world: World
) -> None:
    _, raw = ai_client(factories, world.owner, custom_denylist=["Oakwood Clinic"])
    calls = tool_calls(world)
    assert {name for name, _ in calls} == set(TOOL_NAMES)
    outputs = call_all(harness.server(raw), calls)
    errors = {name: text_of(r) for name, r in outputs if r.is_error}
    assert errors == {}
    assert_no_leak(world, outputs, denylist=True)
    joined = "\n".join(text_of(r) for _, r in outputs)
    for placeholder in ("[MEMBER]", "[ACCT]", "[PHONE]", "[EMAIL]", "[ADDRESS]", "[REDACTED]"):
        assert placeholder in joined, placeholder
    # Real data came through: the scrubbed-name filter matched exactly.
    by_payee = json.loads(text_of(outputs[15][1]))
    assert [t["amount"]["amount"] for t in by_payee["items"]] == ["-250.00"]
    policy = json.loads(text_of(outputs[0][1]))
    assert policy["mode"] == "detail"
    assert policy["never_exposed"]
    assert policy["mcp_server"]["write_tools"] == []
    assert sorted(policy["mcp_server"]["tools"]) == sorted(TOOL_NAMES)


OPTIONS = st.fixed_dictionaries(
    {
        "mode": st.sampled_from([AiMode.DETAIL, AiMode.AGGREGATES]),
        "rounding": st.sampled_from([None, 1, 10, 100]),
        "hide_nicknames": st.booleans(),
        "hide_merchant_location": st.booleans(),
        "date_range_cap": st.sampled_from([None, 30, 365]),
        "custom_denylist": st.sampled_from([[], ["Oakwood Clinic"]]),
    }
)


@hsettings(
    max_examples=8,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@example(
    options={
        "mode": AiMode.AGGREGATES,
        "rounding": 100,
        "hide_nicknames": True,
        "hide_merchant_location": True,
        "date_range_cap": 30,
        "custom_denylist": [],
    }
)
@example(
    options={
        "mode": AiMode.DETAIL,
        "rounding": None,
        "hide_nicknames": False,
        "hide_merchant_location": False,
        "date_range_cap": None,
        "custom_denylist": [],
    }
)
@given(options=OPTIONS)
def test_no_planted_identity_in_any_tool_output_for_any_policy(
    harness: Harness, factories: Factories, world: World, options: dict[str, Any]
) -> None:
    _, raw = ai_client(factories, world.owner, **options)
    outputs = call_all(harness.server(raw), tool_calls(world))
    assert_no_leak(world, outputs, denylist=bool(options["custom_denylist"]))
    for name, result in outputs:
        if result.is_error:
            # Only policy refusals, never a crash or a validation problem.
            text = text_of(result)
            assert "by policy" in text, (name, text)
            if "not-in-aggregates-mode" in text:
                assert options["mode"] == AiMode.AGGREGATES, (name, text)
            else:
                assert "outside-date-range" in text, (name, text)
                assert options["date_range_cap"] is not None, (name, text)


# ---------------------------------------------------------------------------
# The second scrub, on its own
# ---------------------------------------------------------------------------

RAW_LEAKS = {
    "acct": "ACCT 123456789012",
    "card": "4111 1111 1111 1111",
    "masked": "XXXX5678",
    "ssn": "123-45-6789",
    "email": "jane.doe@example.org",
    "phone": "(555) 123-4567",
    "address": "742 Evergreen Terrace, Springfield, IL 62704",
}


def leaky_api(status: int = 200) -> AiApi:
    item_id = str(uuid.uuid4())

    def handler(request: httpx.Request) -> httpx.Response:
        if status != 200:
            return httpx.Response(
                status,
                json={
                    "type": "urn:perfin:problem:ai-access-disabled",
                    "title": "AI access is switched off",
                    "detail": f"ask {RAW_LEAKS['email']} or call {RAW_LEAKS['phone']}",
                    "status": status,
                },
                headers={"content-type": "application/problem+json"},
            )
        return httpx.Response(
            200,
            json={
                "mode": "detail",
                "next_cursor": "MjAyNi0wMS0wMXwwMTkz",
                "items": [
                    {
                        "id": item_id,
                        "label": f"Checking {RAW_LEAKS['masked']}",
                        "memo": " / ".join(RAW_LEAKS.values()),
                        "amount": {"amount": "-123456789.00", "currency": "USD"},
                        RAW_LEAKS["email"]: [RAW_LEAKS["ssn"], {"deep": RAW_LEAKS["acct"]}],
                    }
                ],
            },
        )

    return AiApi.over(
        httpx.Client(transport=httpx.MockTransport(handler), base_url="http://api"), "tok"
    )


def test_second_stage_scrubs_what_the_api_let_through() -> None:
    server = build_server(leaky_api(), rate_per_minute=60)
    outputs = call_all(server, [("list_accounts", {}), ("list_transactions", {})])
    for _, result in outputs:
        assert not result.is_error
        text = text_of(result)
        for value in RAW_LEAKS.values():
            assert value not in text
        for digits in ("123456789012", "5678", "6789", "5551234567", "4111", "742"):
            assert digits not in text.replace("-123456789.00", "")
        body = json.loads(text)
        item = body["items"][0]
        assert uuid.UUID(item["id"])  # structural values survive
        assert item["amount"] == {"amount": "-123456789.00", "currency": "USD"}
        assert body["next_cursor"] == "MjAyNi0wMS0wMXwwMTkz"
        for placeholder in ("[ACCT]", "[GOVID]", "[EMAIL]", "[PHONE]", "[ADDRESS]"):
            assert placeholder in text


def test_kill_switch_and_policy_refusals_are_reported() -> None:
    server = build_server(leaky_api(status=403), rate_per_minute=60)
    outputs = call_all(server, [("explain_data_policy", {}), ("get_overview", {})])
    for _, result in outputs:
        assert result.is_error
        text = text_of(result)
        assert "by policy" in text
        assert "ai-access-disabled" in text
        assert "switched off" in text
        assert RAW_LEAKS["email"] not in text
        assert "123-4567" not in text


def test_revoked_client_and_aggregates_mode_are_reported(
    harness: Harness, factories: Factories, world: World
) -> None:
    client, raw = ai_client(factories, world.owner, mode=AiMode.AGGREGATES)
    server = harness.server(raw)
    outputs = call_all(server, [("list_transactions", {}), ("get_overview", {})])
    assert outputs[0][1].is_error
    assert "not-in-aggregates-mode" in text_of(outputs[0][1])
    assert not outputs[1][1].is_error
    with factories.transaction() as db:
        set_context(db, world.household_id, world.owner.id)
        ai_service.revoke_client(db, owner_principal(world.owner), client.id)
    ((_, after),) = call_all(server, [("get_overview", {})])
    assert after.is_error
    assert "did not accept" in text_of(after)


def test_household_kill_switch_reaches_the_mcp_tools(
    harness: Harness, factories: Factories, world: World
) -> None:
    from app.domain.ai import switches

    _, raw = ai_client(factories, world.owner)
    server = harness.server(raw)
    calls: list[tuple[str, dict[str, Any]]] = [
        ("explain_data_policy", {}),
        ("get_overview", {"period": "last_12_months"}),
    ]
    assert [r.is_error for _, r in call_all(server, calls)] == [False, False]

    def flip(enabled: bool) -> None:
        with factories.transaction() as db:
            set_context(db, world.household_id, world.owner.id)
            switches.set_household_enabled(
                db, owner_principal(world.owner), world.household_id, enabled
            )

    flip(False)
    for name, result in call_all(server, calls):
        assert result.is_error, name
        text = text_of(result)
        assert "by policy" in text, text
        assert "ai-access-disabled" in text, text
    flip(True)
    assert [r.is_error for _, r in call_all(server, calls)] == [False, False]


# ---------------------------------------------------------------------------
# Tools, arguments and limits
# ---------------------------------------------------------------------------


def test_the_tool_list_is_exactly_the_read_only_set() -> None:
    server = build_server(leaky_api(), rate_per_minute=60)

    async def body(client: Client) -> Any:
        return await client.list_tools()

    tools = run_with(server, body).tools
    assert sorted(t.name for t in tools) == sorted(TOOL_NAMES)
    assert len(TOOL_NAMES) == 19
    for tool in tools:
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.name.split("_", 1)[0] in {"get", "list", "find", "simulate", "explain"}
    assert run_with(server, lambda c: c.list_resources()).resources == []
    assert run_with(server, lambda c: c.list_prompts()).prompts == []


def test_unknown_and_invalid_arguments_are_refused() -> None:
    server = build_server(leaky_api(), rate_per_minute=60)
    bad: list[tuple[str, dict[str, Any]]] = [
        ("get_overview", {"bogus": 1}),
        ("list_transactions", {"payee_like": "%"}),
        ("get_overview", {"period": "1=1; drop table"}),
        ("get_spending", {"group_by": "member"}),
        ("list_transactions", {"limit": 10_000}),
        ("list_transactions", {"payee": "a\nb"}),
        ("list_transactions", {"account": "not-a-uuid"}),
        ("simulate_loan", {"loan": str(uuid.uuid4()), "extra_payment": "-5"}),
        ("get_tax_year_summary", {"year": 99999}),
        ("delete_transaction", {}),
    ]
    for (name, args), (_, result) in zip(bad, call_all(server, bad), strict=True):
        assert result.is_error, (name, args)


def test_the_mcp_process_rate_limits_tool_calls() -> None:
    server = build_server(leaky_api(), rate_per_minute=2)
    outputs = call_all(server, [("list_accounts", {})] * 3)
    assert [r.is_error for _, r in outputs] == [False, False, True]
    assert "Rate limit" in text_of(outputs[2][1])


def test_free_text_filters_are_exact_names_not_patterns(
    harness: Harness, factories: Factories, world: World
) -> None:
    _, raw = ai_client(factories, world.owner)
    outputs = call_all(
        harness.server(raw),
        [
            ("list_transactions", {"period": "last_90_days", "payee": "%"}),
            ("list_transactions", {"period": "last_90_days", "payee": "Jane%"}),
            ("list_transactions", {"period": "last_90_days", "category": "groceries"}),
            # Guessing the hidden name confirms nothing (P6B-SR-R1-03).
            ("list_transactions", {"period": "last_90_days", "payee": "Jane Doe"}),
            ("list_transactions", {"period": "last_90_days", "category": "Gifts for Jane"}),
        ],
    )
    assert json.loads(text_of(outputs[0][1]))["items"] == []
    assert json.loads(text_of(outputs[1][1]))["items"] == []
    assert [t["amount"]["amount"] for t in json.loads(text_of(outputs[2][1]))["items"]] == ["-4.75"]
    assert json.loads(text_of(outputs[3][1]))["items"] == []
    assert json.loads(text_of(outputs[4][1]))["items"] == []


# ---------------------------------------------------------------------------
# HTTP transport: bearer and origin
# ---------------------------------------------------------------------------

HTTP_TOKEN = "h" * 48
INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "1"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def test_http_transport_requires_bearer_and_allowed_origin() -> None:
    server = build_server(leaky_api(), rate_per_minute=60, transport="http")
    app = http_app(server, token=HTTP_TOKEN, allowed_origins=["https://ai.example.test"])
    with StarletteClient(app, base_url="http://mcp:8001") as client:
        good = MCP_HEADERS | {"Authorization": f"Bearer {HTTP_TOKEN}"}
        assert client.post(MCP_PATH, json=INIT, headers=MCP_HEADERS).status_code == 401
        wrong = MCP_HEADERS | {"Authorization": "Bearer " + "x" * 48}
        assert client.post(MCP_PATH, json=INIT, headers=wrong).status_code == 401
        basic = MCP_HEADERS | {"Authorization": f"Basic {HTTP_TOKEN}"}
        assert client.post(MCP_PATH, json=INIT, headers=basic).status_code == 401
        evil = good | {"Origin": "https://evil.example"}
        assert client.post(MCP_PATH, json=INIT, headers=evil).status_code == 403
        null = good | {"Origin": "null"}
        assert client.post(MCP_PATH, json=INIT, headers=null).status_code == 403
        ok = client.post(MCP_PATH, json=INIT, headers=good | {"Origin": "https://ai.example.test"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["result"]["serverInfo"]["name"] == "perfin"
        # A non-browser host sends no Origin; the bearer is enough.
        listed = client.post(
            MCP_PATH,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=good | {"MCP-Protocol-Version": "2025-06-18"},
        )
        assert listed.status_code == 200, listed.text
        assert sorted(t["name"] for t in listed.json()["result"]["tools"]) == sorted(TOOL_NAMES)


# ---------------------------------------------------------------------------
# perfin mcp over stdio, as a real process with only its token
# ---------------------------------------------------------------------------


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


def test_perfin_mcp_over_stdio_against_a_live_api(
    live_api: str, factories: Factories, world: World
) -> None:
    _, raw = ai_client(factories, world.owner, custom_denylist=["Oakwood Clinic"])
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

    async def main() -> list[tuple[str, CallToolResult]]:
        async with Client(params) as client:
            names = sorted(t.name for t in (await client.list_tools()).tools)
            assert names == sorted(TOOL_NAMES)
            return [
                (name, await client.call_tool(name, args))
                for name, args in (
                    ("explain_data_policy", {}),
                    ("list_accounts", {}),
                    ("list_transactions", {"period": "last_90_days"}),
                )
            ]

    outputs = anyio.run(main)
    assert [r.is_error for _, r in outputs] == [False, False, False]
    assert_no_leak(world, outputs, denylist=True)
    txns = json.loads(text_of(outputs[2][1]))["items"]
    assert any(t["memo"] == "ZELLE TO [MEMBER] [ACCT]" for t in txns)
