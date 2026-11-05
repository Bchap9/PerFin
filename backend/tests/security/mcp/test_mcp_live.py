# SPDX-License-Identifier: AGPL-3.0-or-later
"""Live verification of the documented local setup (docs/MCP_CLIENTS.md, Option B).

``perfin mcp`` is started exactly as docs/MCP_CLIENTS.md tells an assistant
host to start it: the installed ``perfin`` console script with
``PERFIN_MCP_ENABLED``, ``PERFIN_MCP_API_URL``, ``PERFIN_MCP_TOKEN_FILE``
(a mode-0600 file) and ``PERFIN_MCP_TRANSPORT=stdio`` and nothing else
PerFin-specific. A real MCP client session (the ``mcp`` SDK) talks to it
over stdio while it reaches a live uvicorn API over HTTP.

The household has identity planted everywhere (``build_world``: member
names, usernames, e-mails, last 4, masked numbers, phone, address, SSN, a
custom denylist term) plus documents whose kept page text carries identity
(``plant_documents``, P56-SR-R1-02). Every one of the 19 tools is called;
the transcript (each tool's arguments and answer) is written to
``mcp_live_transcript.json`` in the test's temporary directory for review.
No planted value appears in any answer or in any ``ai_call`` row, and each
audit row is exactly the answer the client received.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import pytest
import uvicorn
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult

from app.core.settings import Settings
from app.main import create_app
from app.mcp.tools import TOOL_NAMES
from tests.fixtures.ai import ai_client
from tests.fixtures.factories import Factories
from tests.fixtures.ledger import LedgerFactories
from tests.security.ai.test_ai_documents import document_leaks, plant_documents
from tests.security.ai.test_ai_views import World, build_world, calls_of, leaks
from tests.security.mcp.test_mcp_server import text_of, tool_calls

pytestmark = [pytest.mark.security, pytest.mark.integration]

#: The console script ``uv run ... perfin`` resolves to (pyproject ``[project.scripts]``).
PERFIN = Path(sys.executable).with_name("perfin")


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


def _identity_in(world: World, text: str) -> list[str]:
    hits = leaks(text, world.usernames) + document_leaks(text)
    if world.household_name.casefold() in text.casefold():
        hits.append("household name")
    return hits


def test_documented_stdio_setup_serves_every_tool_without_identity(
    live_api: str,
    factories: Factories,
    ledger: LedgerFactories,
    world: World,
    tmp_path: Path,
) -> None:
    assert PERFIN.is_file(), PERFIN
    plant_documents(ledger, world)
    _, raw = ai_client(
        factories, world.owner, name="Claude Desktop, laptop", custom_denylist=["Oakwood Clinic"]
    )
    # As the doc says: umask 077, the token in a file only the user reads.
    token_file = tmp_path / "perfin-mcp-token"
    token_file.write_text(raw, encoding="utf-8")
    token_file.chmod(0o600)
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600

    params = StdioServerParameters(
        command=str(PERFIN),
        args=["mcp"],
        env={
            "PERFIN_MCP_ENABLED": "true",
            "PERFIN_MCP_API_URL": live_api,
            "PERFIN_MCP_TOKEN_FILE": str(token_file),
            "PERFIN_MCP_TRANSPORT": "stdio",
            "PERFIN_LOG_LEVEL": "WARNING",
            # Only so the console script finds this checkout when it is not
            # installed into site-packages; no PerFin secret is passed.
            "PYTHONPATH": os.environ.get("PYTHONPATH", str(PERFIN.parents[2])),
        },
    )
    calls = tool_calls(world)
    assert {name for name, _ in calls} == set(TOOL_NAMES)

    async def main() -> tuple[list[str], list[CallToolResult]]:
        async with Client(params) as mcp:
            names = sorted(t.name for t in (await mcp.list_tools()).tools)
            results = [await mcp.call_tool(name, args) for name, args in calls]
        return names, results

    names, results = anyio.run(main)
    assert names == sorted(TOOL_NAMES)

    transcript: list[dict[str, Any]] = []
    for (name, args), result in zip(calls, results, strict=True):
        text = text_of(result)
        transcript.append(
            {
                "tool": name,
                "arguments": args,
                "is_error": result.is_error,
                "answer": json.loads(text),
            }
        )
    record = tmp_path / "mcp_live_transcript.json"
    record.write_text(json.dumps(transcript, indent=2, sort_keys=True), encoding="utf-8")

    errors = {t["tool"]: t["answer"] for t in transcript if t["is_error"]}
    assert errors == {}
    found = {
        f"{i}:{t['tool']}": _identity_in(world, json.dumps(t["answer"]))
        for i, t in enumerate(transcript)
        if _identity_in(world, json.dumps(t["answer"]))
    }
    assert found == {}
    joined = record.read_text(encoding="utf-8")
    for placeholder in ("[MEMBER]", "[ACCT]", "[PHONE]", "[EMAIL]", "[ADDRESS]", "[REDACTED]"):
        assert placeholder in joined, placeholder

    # The audit: one row per call, each exactly the answer the client got
    # (explain_data_policy gains the MCP process's own section), none
    # carrying identity in its response or its parameters.
    rows = calls_of(factories, world)
    assert [row.tool for row in rows] == [name for name, _ in calls]
    assert all(row.status == 200 for row in rows)
    answers = [t["answer"] for t in transcript]
    assert answers[0].pop("mcp_server")["write_tools"] == []
    assert [row.response for row in rows] == answers
    for row, (_, args) in zip(rows, calls, strict=True):
        assert _identity_in(world, json.dumps(row.response)) == [], row.tool
        # Parameters are what the assistant sent in; they hold nothing it did
        # not send.
        assert _identity_in(world, json.dumps(row.params)) == _identity_in(
            world, json.dumps(args)
        ), row.tool
