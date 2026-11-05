# SPDX-License-Identifier: AGPL-3.0-or-later
"""SSRF-safe outbound client (``app.core.http``; SECURITY.md B5).

No network: ``respx`` answers the vetted address, and the one real-socket
test talks to a server on 127.0.0.1 that only a test resolver can name.
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import httpx
import pytest
import respx

from app.core.http import (
    EgressBlockedError,
    EgressError,
    EgressPolicy,
    EgressResponseTooLargeError,
    IPAddress,
    SafeHttpClient,
    address_verdict,
)

pytestmark = pytest.mark.security

HOST = "idp.example.test"
KEY = f"{HOST}:443"
PUBLIC = "151.101.10.10"
POLICY = EgressPolicy(allowed_hosts=frozenset({KEY}))


def resolver_for(*answers: list[str]) -> tuple[Callable[[str, int], list[IPAddress]], list[str]]:
    """A resolver answering successive lookups with ``answers`` (last one repeats)."""
    seen: list[str] = []

    def resolve(host: str, _port: int) -> list[IPAddress]:
        seen.append(host)
        index = min(len(seen) - 1, len(answers) - 1)
        return [ipaddress.ip_address(a) for a in answers[index]]

    return resolve, seen


def client(
    *answers: list[str], policy: EgressPolicy = POLICY, proxy: str | None = None
) -> tuple[SafeHttpClient, list[str]]:
    resolve, seen = resolver_for(*answers) if answers else resolver_for([PUBLIC])
    return SafeHttpClient(policy, resolver=resolve, proxy=proxy), seen


@pytest.fixture
def mock() -> Iterator[respx.MockRouter]:
    # Any request respx has no route for raises: nothing can leave the host.
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        yield router


# ---------------------------------------------------------------------------
# Address classes
# ---------------------------------------------------------------------------

ALWAYS_REFUSED = [
    "169.254.169.254",  # cloud metadata
    "169.254.0.1",
    "fe80::1",
    "::ffff:169.254.169.254",  # IPv4-mapped metadata
    "64:ff9b::a9fe:a9fe",  # NAT64 of 169.254.169.254
    "2002:a9fe:a9fe::1",  # 6to4 of 169.254.169.254
    "0.0.0.0",  # noqa: S104 - an address under test, not a bind
    "::",
    "224.0.0.1",
    "ff02::1",
    "255.255.255.255",
    "240.0.0.1",
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo
]
LOOPBACK = ["127.0.0.1", "127.8.9.10", "::1", "::ffff:127.0.0.1", "::127.0.0.1"]
PRIVATE = [
    "10.0.0.5",
    "172.16.3.4",
    "192.168.1.10",
    "100.64.0.1",
    "fc00::1",
    "fd12:3456::1",
    "::ffff:10.0.0.1",
    "64:ff9b::a00:1",
]
GLOBAL = ["8.8.8.8", PUBLIC, "2606:4700:4700::1111", "::ffff:8.8.8.8", "64:ff9b::808:808"]


@pytest.mark.parametrize("address", ALWAYS_REFUSED)
def test_link_local_metadata_multicast_reserved_are_always_refused(address: str) -> None:
    ip = ipaddress.ip_address(address)
    assert address_verdict(ip) is not None
    assert address_verdict(ip, allow_private=True, allow_loopback=True) is not None


@pytest.mark.parametrize("address", LOOPBACK)
def test_loopback_needs_the_dev_flag(address: str) -> None:
    ip = ipaddress.ip_address(address)
    assert address_verdict(ip) == "loopback"
    assert address_verdict(ip, allow_private=True) == "loopback"
    assert address_verdict(ip, allow_loopback=True) is None


@pytest.mark.parametrize("address", PRIVATE)
def test_private_ranges_need_allow_private(address: str) -> None:
    ip = ipaddress.ip_address(address)
    assert address_verdict(ip) == "private"
    assert address_verdict(ip, allow_private=True) is None


@pytest.mark.parametrize("address", GLOBAL)
def test_global_addresses_pass(address: str) -> None:
    assert address_verdict(ipaddress.ip_address(address)) is None


# ---------------------------------------------------------------------------
# Transport: resolve, vet, pin
# ---------------------------------------------------------------------------


def test_connects_to_the_vetted_address_with_original_host_and_sni(
    mock: respx.MockRouter,
) -> None:
    route = mock.route(host=PUBLIC).mock(return_value=httpx.Response(200, json={"ok": True}))
    http, seen = client([PUBLIC])
    result = http.get(f"https://{HOST}/.well-known/openid-configuration")
    assert result.json() == {"ok": True}
    assert seen == [HOST]
    sent = route.calls.last.request
    assert sent.url.host == PUBLIC
    assert sent.headers["host"] == HOST
    assert sent.extensions["sni_hostname"] == HOST


@pytest.mark.parametrize(
    "answer",
    [["127.0.0.1"], ["169.254.169.254"], ["10.0.0.5"], ["::1"], ["::ffff:127.0.0.1"]],
)
def test_forbidden_resolution_is_refused_before_connecting(
    mock: respx.MockRouter, answer: list[str]
) -> None:
    http, _ = client(answer)
    with pytest.raises(EgressBlockedError, match="forbidden address"):
        http.get(f"https://{HOST}/jwks")
    assert not mock.calls


def test_allow_private_permits_lan_but_never_metadata_or_loopback(
    mock: respx.MockRouter,
) -> None:
    lan = EgressPolicy(allowed_hosts=frozenset({KEY}), private_hosts=frozenset({KEY}))
    route = mock.route(host="10.0.0.5").mock(return_value=httpx.Response(200, json={}))
    http, _ = client(["10.0.0.5"], policy=lan)
    assert http.get(f"https://{HOST}/x").status_code == 200
    assert route.called
    for answer in (["169.254.169.254"], ["127.0.0.1"], ["fe80::1"]):
        http, _ = client(answer, policy=lan)
        with pytest.raises(EgressBlockedError):
            http.get(f"https://{HOST}/x")


def test_dns_rebinding_is_caught_on_every_connection(mock: respx.MockRouter) -> None:
    """A resolver that answers public first and private next never gets a private connect."""
    route = mock.route(host=PUBLIC).mock(return_value=httpx.Response(200, json={}))
    http, seen = client([PUBLIC], ["10.0.0.5"], ["169.254.169.254"])
    assert http.get(f"https://{HOST}/discovery").status_code == 200
    with pytest.raises(EgressBlockedError):
        http.get(f"https://{HOST}/jwks")
    with pytest.raises(EgressBlockedError):
        http.get(f"https://{HOST}/token")
    assert route.call_count == 1
    assert len(seen) == 3  # resolved once per request, never twice per connection


def test_mixed_answer_with_one_private_address_is_refused(mock: respx.MockRouter) -> None:
    http, _ = client([PUBLIC, "10.0.0.5"])
    with pytest.raises(EgressBlockedError):
        http.get(f"https://{HOST}/x")
    assert not mock.calls


def test_ip_literal_urls_are_vetted_too(mock: respx.MockRouter) -> None:
    policy = EgressPolicy(allowed_hosts=frozenset({"169.254.169.254:443", "127.0.0.1:443"}))
    http, seen = client(policy=policy)
    for url in ("https://169.254.169.254/latest/meta-data/", "https://127.0.0.1/"):
        with pytest.raises(EgressBlockedError):
            http.get(url)
    assert seen == []
    assert not mock.calls


@pytest.mark.parametrize(
    "url",
    [
        "https://other.example.test/x",  # not the issuer host
        f"https://{HOST}:8443/x",  # other port
        f"http://{HOST}/x",  # plain http outside dev mode
        f"ftp://{HOST}/x",
        f"https://user:pw@{HOST}/x",  # credentials in the URL
    ],
)
def test_only_https_to_allowlisted_hosts(mock: respx.MockRouter, url: str) -> None:
    http, seen = client([PUBLIC])
    with pytest.raises(EgressBlockedError):
        http.get(url)
    assert seen == []
    assert not mock.calls


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example.test/steal",
        "http://169.254.169.254/latest/meta-data/",
        "https://127.0.0.1/",
        f"http://{HOST}/downgrade",
        f"https://{HOST}:8443/other-port",
    ],
)
def test_redirects_to_another_origin_are_refused(mock: respx.MockRouter, location: str) -> None:
    mock.route(host=PUBLIC).mock(return_value=httpx.Response(302, headers={"Location": location}))
    http, _ = client([PUBLIC])
    with pytest.raises(EgressBlockedError, match="redirect"):
        http.get(f"https://{HOST}/.well-known/openid-configuration")
    assert mock.calls.call_count == 1


def test_same_origin_redirects_are_followed_a_few_times(mock: respx.MockRouter) -> None:
    mock.route(host=PUBLIC, path="/a").mock(
        return_value=httpx.Response(302, headers={"Location": "/b"})
    )
    mock.route(host=PUBLIC, path="/b").mock(return_value=httpx.Response(200, json={"b": 1}))
    mock.route(host=PUBLIC, path="/loop").mock(
        return_value=httpx.Response(302, headers={"Location": "/loop"})
    )
    http, _ = client([PUBLIC])
    assert http.get(f"https://{HOST}/a").json() == {"b": 1}
    with pytest.raises(EgressBlockedError, match="too many redirects"):
        http.get(f"https://{HOST}/loop")


def test_post_redirects_are_refused(mock: respx.MockRouter) -> None:
    mock.route(host=PUBLIC).mock(
        return_value=httpx.Response(307, headers={"Location": f"https://{HOST}/elsewhere"})
    )
    http, _ = client([PUBLIC])
    with pytest.raises(EgressBlockedError):
        http.post_form(f"https://{HOST}/token", {"a": "b"})


def test_response_size_is_capped(mock: respx.MockRouter) -> None:
    small = EgressPolicy(allowed_hosts=frozenset({KEY}), max_response_bytes=1024)
    mock.route(host=PUBLIC, path="/declared").mock(
        return_value=httpx.Response(200, content=b"x" * 4096)
    )

    def chunks() -> Iterator[bytes]:
        for _ in range(64):
            yield b"y" * 512

    mock.route(host=PUBLIC, path="/streamed").mock(
        return_value=httpx.Response(200, content=chunks())
    )
    http, _ = client([PUBLIC], policy=small)
    with pytest.raises(EgressResponseTooLargeError):
        http.get(f"https://{HOST}/declared")
    with pytest.raises(EgressResponseTooLargeError):
        http.get(f"https://{HOST}/streamed")


def test_compressed_bodies_are_refused(mock: respx.MockRouter) -> None:
    mock.route(host=PUBLIC).mock(
        return_value=httpx.Response(200, content=b"\x1f\x8b", headers={"Content-Encoding": "gzip"})
    )
    http, _ = client([PUBLIC])
    with pytest.raises(EgressError, match="compressed"):
        http.get(f"https://{HOST}/x")


def test_proxy_mode_sends_the_hostname_to_the_egress_proxy(mock: respx.MockRouter) -> None:
    """With HTTPS_PROXY the CONNECT proxy resolves and vets (deploy/egress, H2)."""
    route = mock.route(host=HOST).mock(return_value=httpx.Response(200, json={}))

    def no_dns(_host: str, _port: int) -> list[IPAddress]:
        raise AssertionError("no local DNS lookup in proxy mode")

    http = SafeHttpClient(POLICY, resolver=no_dns, proxy="http://egress.internal:3128")
    assert http.get(f"https://{HOST}/x").status_code == 200
    assert route.called
    # The allowlist still applies in proxy mode.
    with pytest.raises(EgressBlockedError):
        http.get("https://169.254.169.254/latest/meta-data/")


def test_env_proxy_and_no_proxy(monkeypatch: pytest.MonkeyPatch, mock: respx.MockRouter) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://egress.internal:3128")
    monkeypatch.setenv("NO_PROXY", HOST)
    for name in ("https_proxy", "no_proxy", "ALL_PROXY", "all_proxy"):
        monkeypatch.delenv(name, raising=False)
    route = mock.route(host=PUBLIC).mock(return_value=httpx.Response(200, json={}))
    resolve, seen = resolver_for([PUBLIC])
    http = SafeHttpClient(POLICY, resolver=resolve)  # proxy="env"
    assert http.get(f"https://{HOST}/x").status_code == 200
    assert route.called
    assert seen == [HOST]


def test_timeouts_come_from_the_policy() -> None:
    policy = EgressPolicy(allowed_hosts=frozenset({KEY}), timeout_seconds=3.5)
    http = SafeHttpClient(policy)
    timeout = http._client.timeout
    assert (timeout.connect, timeout.read, timeout.write, timeout.pool) == (3.5, 3.5, 3.5, 3.5)


# ---------------------------------------------------------------------------
# Real sockets: pinning without system DNS, and the read timeout
# ---------------------------------------------------------------------------


class _Handler(BaseHTTPRequestHandler):
    seen: list[str] = []  # noqa: RUF012 - shared by the test server only

    def do_GET(self) -> None:
        self.seen.append(self.headers.get("Host", ""))
        body = b'{"pinned": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: Any) -> None:
        return


@pytest.fixture
def local_server() -> Iterator[tuple[int, list[str]]]:
    _Handler.seen = []
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], _Handler.seen
    finally:
        server.shutdown()
        server.server_close()


def test_real_connection_goes_to_the_resolved_address(
    local_server: tuple[int, list[str]],
) -> None:
    port, seen = local_server
    key = f"idp.invalid:{port}"  # .invalid never resolves through system DNS
    dev = EgressPolicy(allowed_hosts=frozenset({key}), allow_http=True, allow_loopback=True)
    resolve, _ = resolver_for(["127.0.0.1"])
    http = SafeHttpClient(dev, resolver=resolve, proxy=None)
    assert http.get(f"http://idp.invalid:{port}/x").json() == {"pinned": True}
    assert seen == [f"idp.invalid:{port}"]
    # Without the dev loopback allowance the same request never connects.
    strict = EgressPolicy(allowed_hosts=frozenset({key}), allow_http=True)
    with pytest.raises(EgressBlockedError):
        SafeHttpClient(strict, resolver=resolve, proxy=None).get(f"http://idp.invalid:{port}/x")
    assert seen == [f"idp.invalid:{port}"]


def test_read_timeout_is_enforced() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]
    try:
        key = f"slow.invalid:{port}"
        policy = EgressPolicy(
            allowed_hosts=frozenset({key}),
            allow_http=True,
            allow_loopback=True,
            timeout_seconds=0.3,
        )
        resolve, _ = resolver_for(["127.0.0.1"])
        http = SafeHttpClient(policy, resolver=resolve, proxy=None)
        with pytest.raises(EgressError, match="ReadTimeout"):
            http.get(f"http://slow.invalid:{port}/never-answers")
    finally:
        listener.close()
