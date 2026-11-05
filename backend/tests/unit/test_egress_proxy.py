# SPDX-License-Identifier: AGPL-3.0-or-later
"""deploy/egress/egress_proxy.py: the worker's allowlisting CONNECT proxy (SECURITY.md H2).

The proxy is a standalone standard-library script shipped in its own image;
it is loaded here by path. The end-to-end tests run it on loopback with an
injected resolver, so no real DNS or internet access is involved.
"""

from __future__ import annotations

import asyncio
import importlib.util
import ipaddress
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

PROXY_PATH = Path(__file__).resolve().parents[3] / "deploy" / "egress" / "egress_proxy.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("perfin_egress_proxy", PROXY_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)
    return module


ep: Any = _load()

IPAddr = ipaddress.IPv4Address | ipaddress.IPv6Address


# ---- allowlist parsing ------------------------------------------------------


def test_allowlist_entries_and_default_ports() -> None:
    rules = ep.parse_allowlist(
        """
        # comment line
        imap.example.com            # IMAPS and HTTPS by default
        prices.example.org:8443
        *.feeds.example.net
        UPPER.Example.COM.
        """
    )
    assert [(r.host, r.wildcard, sorted(r.ports)) for r in rules] == [
        ("imap.example.com", False, [443, 993]),
        ("prices.example.org", False, [8443]),
        ("feeds.example.net", True, [443, 993]),
        ("upper.example.com", False, [443, 993]),
    ]


def test_empty_allowlist_denies_everything() -> None:
    policy = ep.Policy(rules=ep.parse_allowlist("# nothing\n\n"))
    assert not policy.permits_name("imap.example.com", 993)


@pytest.mark.parametrize(
    "line",
    [
        "10.0.0.5",
        "127.0.0.1:993",
        "[::1]:443",
        "localhost",
        "db",
        "*.com",
        "example.com:0",
        "example.com:70000",
        "example.com:https",
        "two words.example.com",
        "bad_label.example.com",
        "-lead.example.com",
        "127.1",
    ],
)
def test_allowlist_rejects_ip_literals_and_bad_names(line: str) -> None:
    with pytest.raises(ep.AllowlistError, match="line 1"):
        ep.parse_allowlist(line)


def test_shipped_allowlist_parses() -> None:
    shipped = PROXY_PATH.parents[1] / "egress-allowlist.txt"
    ep.parse_allowlist(shipped.read_text(encoding="utf-8"))


def test_name_policy_exact_wildcard_and_port() -> None:
    policy = ep.Policy(
        rules=ep.parse_allowlist("imap.example.com\n*.feeds.example.net:443\n"),
    )
    assert policy.permits_name("imap.example.com", 993)
    assert policy.permits_name("imap.example.com", 443)
    assert not policy.permits_name("imap.example.com", 25)
    assert not policy.permits_name("evil-imap.example.com", 993)
    assert not policy.permits_name("imap.example.com.evil.test", 993)
    assert policy.permits_name("a.feeds.example.net", 443)
    assert policy.permits_name("a.b.feeds.example.net", 443)
    assert not policy.permits_name("feeds.example.net", 443)  # wildcard excludes the apex
    assert not policy.permits_name("xfeeds.example.net", 443)
    assert not policy.permits_name("a.feeds.example.net", 993)


@pytest.mark.parametrize(
    ("address", "allowed"),
    [
        ("93.184.215.14", True),
        ("2606:2800:21f:cb07:6820:80da:af6b:8b2c", True),
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.18.0.5", False),
        ("192.168.1.10", False),
        ("100.64.0.1", False),
        ("169.254.169.254", False),
        ("0.0.0.0", False),  # noqa: S104 - an address under test, not a bind
        ("224.0.0.1", False),
        ("::1", False),
        ("fd00::1", False),
        ("fe80::1", False),
        ("::ffff:127.0.0.1", False),
        ("::ffff:10.0.0.1", False),
    ],
)
def test_address_policy_refuses_non_public(address: str, allowed: bool) -> None:
    policy = ep.Policy(rules=())
    assert policy.permits_address(ipaddress.ip_address(address)) is allowed


def test_private_exemption_is_explicit() -> None:
    policy = ep.Policy(rules=(), allow_private=ep.parse_cidrs("192.168.1.0/24, fd00::/8"))
    assert policy.permits_address(ipaddress.ip_address("192.168.1.10"))
    assert policy.permits_address(ipaddress.ip_address("fd00::1"))
    assert not policy.permits_address(ipaddress.ip_address("192.168.2.10"))
    assert not policy.permits_address(ipaddress.ip_address("127.0.0.1"))


# ---- request parsing --------------------------------------------------------


def test_connect_head_is_parsed() -> None:
    head = b"CONNECT IMAP.Example.com:993 HTTP/1.1\r\nHost: imap.example.com:993\r\n\r\n"
    assert ep.parse_connect_head(head) == ("imap.example.com", 993)


@pytest.mark.parametrize(
    ("head", "status"),
    [
        (b"GET http://example.com/ HTTP/1.1\r\n\r\n", 405),
        (b"POST http://example.com/ HTTP/1.1\r\n\r\n", 405),
        (b"CONNECT 93.184.215.14:443 HTTP/1.1\r\n\r\n", 403),
        (b"CONNECT [2606:2800::1]:443 HTTP/1.1\r\n\r\n", 403),
        (b"CONNECT db:5432 HTTP/1.1\r\n\r\n", 403),
        (b"CONNECT example.com HTTP/1.1\r\n\r\n", 400),
        (b"CONNECT example.com:99999 HTTP/1.1\r\n\r\n", 400),
        (b"CONNECT example.com:443 HTTP/2.0\r\n\r\n", 400),
        (b"connect example.com:443 HTTP/1.1\r\n\r\n", 400),
        ("CONNECT exämple.com:443 HTTP/1.1\r\n\r\n".encode(), 400),
    ],
)
def test_bad_heads_are_refused(head: bytes, status: int) -> None:
    with pytest.raises(ep.ProxyRefusal) as info:
        ep.parse_connect_head(head)
    assert info.value.status == status


def test_config_from_env() -> None:
    config = ep.Config.from_env(
        {
            "EGRESS_LISTEN": "127.0.0.1:4000",
            "EGRESS_ALLOWLIST": "/srv/egress/list.txt",
            "EGRESS_ALLOW_PRIVATE_CIDRS": "192.168.0.0/16",
            "EGRESS_MAX_CLIENTS": "8",
            "EGRESS_IDLE_TIMEOUT": "30",
        }
    )
    assert (config.listen_host, config.listen_port) == ("127.0.0.1", 4000)
    assert config.allowlist_path == Path("/srv/egress/list.txt")
    assert config.allow_private == (ipaddress.ip_network("192.168.0.0/16"),)
    assert (config.max_clients, config.idle_timeout) == (8, 30.0)


def test_main_check_validates_allowlist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    good = tmp_path / "good.txt"
    good.write_text("imap.example.com\n", encoding="utf-8")
    bad = tmp_path / "bad.txt"
    bad.write_text("10.0.0.1\n", encoding="utf-8")
    monkeypatch.setenv("EGRESS_ALLOWLIST", str(good))
    assert ep.main(["check"]) == 0
    monkeypatch.setenv("EGRESS_ALLOWLIST", str(bad))
    assert ep.main(["check"]) == 1
    monkeypatch.setenv("EGRESS_ALLOWLIST", str(tmp_path / "missing.txt"))
    assert ep.main(["check"]) == 1
    assert "config_error" in capsys.readouterr().out


# ---- end to end on loopback -------------------------------------------------


def _fake_resolver(table: dict[str, list[str]]) -> Callable[[str, int], Awaitable[list[IPAddr]]]:
    async def resolver(host: str, port: int) -> list[IPAddr]:
        if host not in table:
            raise ep.ProxyRefusal(502, "name does not resolve")
        return [ipaddress.ip_address(a) for a in table[host]]

    return resolver


async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    data = await reader.read(1024)
    writer.write(b"echo:" + data)
    await writer.drain()
    writer.close()


async def _exchange(proxy_port: int, request: bytes, payload: bytes = b"") -> tuple[bytes, bytes]:
    reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
    writer.write(request)
    await writer.drain()
    status_head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5)
    tunnelled = b""
    if status_head.startswith(b"HTTP/1.1 200") and payload:
        writer.write(payload)
        await writer.drain()
        tunnelled = await asyncio.wait_for(reader.read(1024), timeout=5)
    writer.close()
    return status_head, tunnelled


def _run_scenario(
    allowlist: str,
    table: dict[str, list[str]],
    request_for: Callable[[int], bytes],
    allow_private: str = "127.0.0.0/8",
    payload: bytes = b"",
) -> tuple[bytes, bytes]:
    async def scenario() -> tuple[bytes, bytes]:
        upstream = await asyncio.start_server(_echo, "127.0.0.1", 0)
        upstream_port = upstream.sockets[0].getsockname()[1]
        proxy = ep.EgressProxy(
            policy=ep.Policy(
                rules=ep.parse_allowlist(allowlist.format(port=upstream_port)),
                allow_private=ep.parse_cidrs(allow_private),
            ),
            max_clients=4,
            idle_timeout=5.0,
            resolver=_fake_resolver(table),
        )
        server = await proxy.start("127.0.0.1", 0)
        proxy_port = server.sockets[0].getsockname()[1]
        async with upstream, server:
            return await _exchange(proxy_port, request_for(upstream_port), payload)

    return asyncio.run(scenario())


def test_allowed_destination_is_tunnelled(capsys: pytest.CaptureFixture[str]) -> None:
    head, body = _run_scenario(
        "upstream.example.test:{port}\n",
        {"upstream.example.test": ["127.0.0.1"]},
        lambda port: f"CONNECT upstream.example.test:{port} HTTP/1.1\r\n\r\n".encode(),
        payload=b"hello",
    )
    assert head.startswith(b"HTTP/1.1 200")
    assert body == b"echo:hello"
    log = capsys.readouterr().out
    assert '"decision": "allow"' in log
    assert "hello" not in log  # tunnel content is never logged


def test_unlisted_destination_is_refused() -> None:
    head, _ = _run_scenario(
        "other.example.test:{port}\n",
        {"upstream.example.test": ["127.0.0.1"]},
        lambda port: f"CONNECT upstream.example.test:{port} HTTP/1.1\r\n\r\n".encode(),
    )
    assert head.startswith(b"HTTP/1.1 403")


def test_listed_name_resolving_to_private_address_is_refused() -> None:
    # DNS for an allowed name points at an internal address: refused unless
    # the operator exempted that range.
    head, _ = _run_scenario(
        "upstream.example.test:{port}\n",
        {"upstream.example.test": ["127.0.0.1"]},
        lambda port: f"CONNECT upstream.example.test:{port} HTTP/1.1\r\n\r\n".encode(),
        allow_private="",
    )
    assert head.startswith(b"HTTP/1.1 403")


def test_plain_http_and_ip_literals_are_refused() -> None:
    head, _ = _run_scenario(
        "upstream.example.test:{port}\n",
        {},
        lambda port: f"GET http://upstream.example.test:{port}/ HTTP/1.1\r\n\r\n".encode(),
    )
    assert head.startswith(b"HTTP/1.1 405")
    head, _ = _run_scenario(
        "upstream.example.test:{port}\n",
        {},
        lambda port: f"CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n".encode(),
    )
    assert head.startswith(b"HTTP/1.1 403")


def test_oversized_head_is_refused() -> None:
    head, _ = _run_scenario(
        "upstream.example.test:{port}\n",
        {},
        lambda port: (
            b"CONNECT upstream.example.test:443 HTTP/1.1\r\nX-Pad: " + b"a" * 20000 + b"\r\n\r\n"
        ),
    )
    assert head.startswith(b"HTTP/1.1 431")
