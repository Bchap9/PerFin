# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reference deployment of the ``mcp`` role (AI_ACCESS.md M7, SECURITY.md H1/H2).

The ``mcp`` service in ``deploy/docker-compose.yml`` shares a network with
the API and (for the optional HTTP transport) the proxy only, mounts nothing,
runs read-only with every capability dropped, reads no ``env_file`` and is
handed none of the variables ``perfin mcp`` refuses; its environment passes
the role's own configuration check once the token is filled in.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.mcp.config import FORBIDDEN_ENV, FORBIDDEN_ENV_RE, check

pytestmark = pytest.mark.security

COMPOSE = Path(__file__).resolve().parents[4] / "deploy" / "docker-compose.yml"


@cache
def compose() -> dict[str, Any]:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def service(name: str) -> dict[str, Any]:
    svc = compose()["services"][name]
    assert isinstance(svc, dict)
    return svc


def networks_of(name: str) -> set[str]:
    nets = service(name).get("networks", [])
    return set(nets)


def test_mcp_reaches_only_the_api_and_the_proxy() -> None:
    mcp_nets = networks_of("mcp")
    assert mcp_nets == {"ai", "mcp_edge"}
    for net in mcp_nets:
        assert compose()["networks"][net]["internal"] is True
    neighbours = {
        name for name in compose()["services"] if name != "mcp" and networks_of(name) & mcp_nets
    }
    assert neighbours == {"api", "proxy"}
    assert {n for n in compose()["services"] if "ai" in networks_of(n)} == {"api", "mcp"}
    assert {n for n in compose()["services"] if "mcp_edge" in networks_of(n)} == {"proxy", "mcp"}


def test_mcp_container_is_locked_down() -> None:
    mcp = service("mcp")
    assert not mcp.get("volumes")
    assert "env_file" not in mcp
    assert "ports" not in mcp
    assert mcp["read_only"] is True
    assert mcp["cap_drop"] == ["ALL"]
    assert "cap_add" not in mcp
    assert "no-new-privileges:true" in mcp["security_opt"]
    assert mcp["user"] == "10001:10001"
    assert mcp["command"] == ["mcp"]
    assert mcp["profiles"] == ["mcp"]


def test_mcp_environment_is_only_its_own() -> None:
    env = {str(k): str(v) for k, v in service("mcp")["environment"].items()}
    for name in env:
        assert name not in FORBIDDEN_ENV, name
        assert not FORBIDDEN_ENV_RE.match(name), name
        assert name.startswith(("PERFIN_MCP_", "PERFIN_ROLE", "PERFIN_LOG_LEVEL")), name
    assert env["PERFIN_ROLE"] == "mcp"
    assert env["PERFIN_MCP_API_URL"] == "http://api:8000"
    # With the token and secrets filled in, the role's own gate accepts it.
    filled = {
        k: v
        for k, v in env.items()
        if not v.startswith("${") and k not in ("PERFIN_MCP_RATE_PER_MINUTE",)
    } | {
        "PERFIN_MCP_ENABLED": "true",
        "PERFIN_MCP_TOKEN": "pfa_" + "a" * 40,
        "PERFIN_MCP_HTTP_TOKEN": "b" * 40,
        "PERFIN_MCP_HTTP_ALLOWED_ORIGINS": "https://ai.example.test",
    }
    config, findings = check(filled)
    assert config is not None, findings
    assert config.transport == "http"
    assert config.http_host == "0.0.0.0"  # noqa: S104 - inside its container, on internal networks


def test_stdio_override_of_the_compose_service_needs_nothing_else() -> None:
    """docs/MCP_CLIENTS.md: ``docker exec -i -e PERFIN_MCP_TRANSPORT=stdio perfin-mcp-1 perfin mcp``
    (and ``docker compose run --rm -T -e PERFIN_MCP_TRANSPORT=stdio mcp``) start with the
    service's own environment plus that one override; the HTTP settings are then ignored, so
    the stdio form works with or without the HTTP secret and origins filled in.
    """
    assert compose()["name"] == "perfin"  # container perfin-mcp-1 (project perfin, service mcp)
    env = {str(k): str(v) for k, v in service("mcp")["environment"].items()}
    base = {k: v for k, v in env.items() if not v.startswith("${")} | {
        "PERFIN_MCP_ENABLED": "true",
        "PERFIN_MCP_TOKEN": "pfa_" + "a" * 40,
        "PERFIN_MCP_TRANSPORT": "stdio",
    }
    for extra in ({}, {"PERFIN_MCP_HTTP_TOKEN": "b" * 40, "PERFIN_MCP_HTTP_ALLOWED_ORIGINS": ""}):
        config, findings = check(base | extra)
        assert config is not None, findings
        assert config.transport == "stdio"
        assert config.api_url == "http://api:8000"
