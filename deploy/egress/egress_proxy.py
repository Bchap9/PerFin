#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PerFin egress allowlist proxy (SECURITY.md H2, ARCHITECTURE.md "Deployment topology").

The worker sits on an internal-only Docker network with no route out. When an
integration needs the internet (IMAPS for email intake, a price feed), the
optional ``egress`` Compose profile starts this proxy, which is attached to
both the internal network and an outbound network, and the worker is pointed
at it with ``HTTPS_PROXY=http://egress:3128``.

Policy, deliberately narrow:

* Only ``CONNECT host:port`` is served. Every other method is refused with
  405, so the proxy never forwards plaintext HTTP and never sees content: the
  client negotiates TLS end to end through the tunnel.
* ``host`` must be a DNS name listed in the allowlist file (one entry per
  line, ``host``, ``host:port`` or ``*.domain[:port]``; ``#`` comments). IP
  literals are always refused, so the allowlist cannot be bypassed by
  addressing a server directly. Without a port an entry allows 443 and 993.
* The name is resolved here and the tunnel is opened to the checked address
  (no second lookup, so no DNS-rebinding window). Addresses that are not
  globally routable (loopback, RFC 1918, link-local, ULA, CGNAT, multicast,
  reserved) are refused unless they fall inside ``EGRESS_ALLOW_PRIVATE_CIDRS``
  (for an IMAP server on the home LAN). This keeps the proxy from being used
  to reach the database or other containers by name.
* Request heads are capped at 8 KiB and must arrive within 10 s; concurrent
  tunnels are capped; idle tunnels are closed.

Every decision is logged as one JSON line (host, port, decision, reason);
nothing from inside the tunnels is logged. Standard library only.

Environment:
    EGRESS_LISTEN               address:port to listen on (default 0.0.0.0:3128)
    EGRESS_ALLOWLIST            allowlist file (default /etc/egress/allowlist.txt)
    EGRESS_ALLOW_PRIVATE_CIDRS  comma-separated CIDRs exempt from the
                                public-address rule (default: none)
    EGRESS_MAX_CLIENTS          concurrent tunnels (default 64)
    EGRESS_IDLE_TIMEOUT         seconds without traffic before a tunnel closes
                                (default 300)
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import os
import re
import signal
import socket
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network
Resolver = Callable[[str, int], Awaitable[list[IPAddress]]]

DEFAULT_PORTS: frozenset[int] = frozenset({443, 993})
MAX_HEAD_BYTES = 8192
HEAD_TIMEOUT_SECONDS = 10.0
CONNECT_TIMEOUT_SECONDS = 10.0
CHUNK_BYTES = 65536

_LABEL = r"(?!-)[a-z0-9-]{1,63}(?<!-)"
_HOSTNAME = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})+$")
_REQUEST_LINE = re.compile(r"^([A-Z]{1,16}) (\S{1,300}) HTTP/1\.[01]$")


class AllowlistError(ValueError):
    """The allowlist file is malformed."""


class ProxyRefusal(Exception):  # noqa: N818 - a refusal is an outcome, not an error
    """A request the proxy answers with an HTTP error status."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason


@dataclass(frozen=True)
class Rule:
    """One allowlist entry."""

    host: str
    wildcard: bool
    ports: frozenset[int]

    def matches(self, host: str, port: int) -> bool:
        if port not in self.ports:
            return False
        if self.wildcard:
            return host.endswith("." + self.host)
        return host == self.host


def _normalise_host(raw: str) -> str:
    return raw.strip().lower().rstrip(".")


def is_hostname(value: str) -> bool:
    """True for a multi-label DNS name; IP literals and bare words are not."""
    if not _HOSTNAME.match(value):
        return False
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        return False
    # A purely numeric final label is never a real TLD; refuse dotted-decimal
    # variants such as 127.1 or 0x7f.1 that resolvers may accept as addresses.
    return not value.rsplit(".", 1)[-1].isdigit()


def _parse_port(raw: str, where: str) -> int:
    if not raw.isdigit():
        raise AllowlistError(f"{where}: port must be a number")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise AllowlistError(f"{where}: port out of range")
    return port


def parse_allowlist(text: str) -> tuple[Rule, ...]:
    """Parse the allowlist file. Raises ``AllowlistError`` naming the bad line."""
    rules: list[Rule] = []
    for number, line in enumerate(text.splitlines(), start=1):
        entry = line.split("#", 1)[0].strip()
        if not entry:
            continue
        where = f"line {number}"
        if any(ch.isspace() for ch in entry):
            raise AllowlistError(f"{where}: one entry per line")
        host_part, sep, port_part = entry.rpartition(":")
        if not sep:
            host_part, port_part = entry, ""
        ports = frozenset({_parse_port(port_part, where)}) if port_part else DEFAULT_PORTS
        host = _normalise_host(host_part)
        wildcard = host.startswith("*.")
        if wildcard:
            host = host[2:]
        if not is_hostname(host):
            raise AllowlistError(f"{where}: not a DNS host name (IP addresses are not allowed)")
        rules.append(Rule(host=host, wildcard=wildcard, ports=ports))
    return tuple(rules)


@dataclass(frozen=True)
class Policy:
    """What the proxy may connect to."""

    rules: tuple[Rule, ...]
    allow_private: tuple[IPNetwork, ...] = ()

    def permits_name(self, host: str, port: int) -> bool:
        return any(rule.matches(host, port) for rule in self.rules)

    def permits_address(self, address: IPAddress) -> bool:
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
            address = address.ipv4_mapped
        if any(address in network for network in self.allow_private):
            return True
        return address.is_global and not address.is_multicast


def parse_cidrs(raw: str) -> tuple[IPNetwork, ...]:
    return tuple(ipaddress.ip_network(part.strip()) for part in raw.split(",") if part.strip())


def parse_connect_head(head: bytes) -> tuple[str, int]:
    """Validate a request head and return ``(host, port)`` of a CONNECT target."""
    try:
        text = head.decode("ascii")
    except UnicodeDecodeError as exc:
        raise ProxyRefusal(400, "non-ascii request") from exc
    request_line = text.split("\r\n", 1)[0]
    match = _REQUEST_LINE.match(request_line)
    if match is None:
        raise ProxyRefusal(400, "malformed request line")
    method, target = match.groups()
    if method != "CONNECT":
        raise ProxyRefusal(405, "only CONNECT is supported")
    host_part, sep, port_part = target.rpartition(":")
    if not sep or not port_part.isdigit():
        raise ProxyRefusal(400, "CONNECT target must be host:port")
    port = int(port_part)
    if not 1 <= port <= 65535:
        raise ProxyRefusal(400, "port out of range")
    host = _normalise_host(host_part)
    if host.startswith("[") or not is_hostname(host):
        raise ProxyRefusal(403, "target is not a DNS host name")
    return host, port


@dataclass
class Config:
    listen_host: str = "0.0.0.0"  # noqa: S104 - container-internal listener  # nosec B104
    listen_port: int = 3128
    allowlist_path: Path = Path("/etc/egress/allowlist.txt")
    allow_private: tuple[IPNetwork, ...] = ()
    max_clients: int = 64
    idle_timeout: float = 300.0

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Config:
        listen = env.get("EGRESS_LISTEN", "0.0.0.0:3128")
        host, _, port = listen.rpartition(":")
        return cls(
            listen_host=host or "0.0.0.0",  # noqa: S104  # nosec B104
            listen_port=int(port),
            allowlist_path=Path(env.get("EGRESS_ALLOWLIST", "/etc/egress/allowlist.txt")),
            allow_private=parse_cidrs(env.get("EGRESS_ALLOW_PRIVATE_CIDRS", "")),
            max_clients=int(env.get("EGRESS_MAX_CLIENTS", "64")),
            idle_timeout=float(env.get("EGRESS_IDLE_TIMEOUT", "300")),
        )


def log_event(**fields: object) -> None:
    record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **fields}
    sys.stdout.write(json.dumps(record, sort_keys=True) + "\n")
    sys.stdout.flush()


async def _respond(writer: asyncio.StreamWriter, status: int, reason: str) -> None:
    phrases = {
        200: "Connection established",
        400: "Bad Request",
        403: "Forbidden",
        405: "Method Not Allowed",
        431: "Request Header Fields Too Large",
        502: "Bad Gateway",
        503: "Service Unavailable",
    }
    head = f"HTTP/1.1 {status} {phrases.get(status, 'Error')}\r\n"
    if status != 200:
        body = reason.encode("ascii", "replace") + b"\n"
        head += (
            f"Content-Type: text/plain\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode("ascii") + body)
    else:
        writer.write((head + "\r\n").encode("ascii"))
    with contextlib.suppress(ConnectionError):
        await writer.drain()


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    try:
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=HEAD_TIMEOUT_SECONDS)
    except asyncio.LimitOverrunError as exc:
        raise ProxyRefusal(431, "request head too large") from exc
    except asyncio.IncompleteReadError as exc:
        raise ProxyRefusal(400, "incomplete request") from exc
    except TimeoutError as exc:
        raise ProxyRefusal(400, "request head timeout") from exc


async def resolve(host: str, port: int) -> list[IPAddress]:
    """Resolve ``host`` to its distinct addresses (A and AAAA)."""
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ProxyRefusal(502, "name does not resolve") from exc
    seen: list[IPAddress] = []
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        if address not in seen:
            seen.append(address)
    return seen


async def _open_upstream(
    addresses: Sequence[IPAddress], port: int
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    for address in addresses:
        try:
            return await asyncio.wait_for(
                asyncio.open_connection(str(address), port), timeout=CONNECT_TIMEOUT_SECONDS
            )
        except (OSError, TimeoutError):
            continue
    raise ProxyRefusal(502, "upstream unreachable")


async def _pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter, idle_timeout: float) -> None:
    try:
        while True:
            chunk = await asyncio.wait_for(src.read(CHUNK_BYTES), timeout=idle_timeout)
            if not chunk:
                break
            dst.write(chunk)
            await dst.drain()
    except (ConnectionError, TimeoutError):
        pass
    finally:
        with contextlib.suppress(OSError, RuntimeError):
            if dst.can_write_eof():
                dst.write_eof()


@dataclass
class EgressProxy:
    """The CONNECT proxy. ``serve()`` runs until cancelled."""

    policy: Policy
    max_clients: int = 64
    idle_timeout: float = 300.0
    resolver: Resolver = resolve
    _active: int = field(default=0, init=False)

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._active >= self.max_clients:
            await _respond(writer, 503, "too many tunnels")
            await _close(writer)
            return
        self._active += 1
        try:
            await self._handle(reader, writer)
        finally:
            self._active -= 1
            await _close(writer)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        host: str | None = None
        port: int | None = None
        try:
            head = await _read_head(reader)
            host, port = parse_connect_head(head)
            if not self.policy.permits_name(host, port):
                raise ProxyRefusal(403, "destination not in allowlist")
            addresses = [
                a for a in await self.resolver(host, port) if self.policy.permits_address(a)
            ]
            if not addresses:
                raise ProxyRefusal(403, "destination resolves to a non-public address")
            up_reader, up_writer = await _open_upstream(addresses, port)
        except ProxyRefusal as refusal:
            log_event(
                event="connect",
                host=host,
                port=port,
                decision="deny",
                status=refusal.status,
                reason=refusal.reason,
            )
            await _respond(writer, refusal.status, refusal.reason)
            return
        log_event(event="connect", host=host, port=port, decision="allow", status=200)
        await _respond(writer, 200, "")
        try:
            await asyncio.gather(
                _pipe(reader, up_writer, self.idle_timeout),
                _pipe(up_reader, writer, self.idle_timeout),
            )
        finally:
            await _close(up_writer)

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self.handle, host, port, limit=MAX_HEAD_BYTES)


async def _close(writer: asyncio.StreamWriter) -> None:
    with contextlib.suppress(OSError, RuntimeError):
        writer.close()
        await writer.wait_closed()


def load_policy(config: Config) -> Policy:
    rules = parse_allowlist(config.allowlist_path.read_text(encoding="utf-8"))
    return Policy(rules=rules, allow_private=config.allow_private)


async def _serve(config: Config) -> None:
    proxy = EgressProxy(
        policy=load_policy(config),
        max_clients=config.max_clients,
        idle_timeout=config.idle_timeout,
    )
    server = await proxy.start(config.listen_host, config.listen_port)
    log_event(
        event="start",
        listen=f"{config.listen_host}:{config.listen_port}",
        rules=len(proxy.policy.rules),
        allow_private=[str(n) for n in config.allow_private],
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    async with server:
        await stop.wait()
    log_event(event="stop")


def main(argv: Iterable[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    config = Config.from_env(dict(os.environ))
    try:
        policy = load_policy(config)
    except (OSError, AllowlistError) as exc:
        log_event(event="config_error", error=str(exc))
        return 1
    if args == ["check"]:
        log_event(event="config_ok", rules=len(policy.rules))
        return 0
    if args == ["healthcheck"]:
        try:
            with socket.create_connection(("127.0.0.1", config.listen_port), timeout=3):
                return 0
        except OSError:
            return 1
    if args:
        sys.stderr.write("usage: egress_proxy.py [check|healthcheck]\n")
        return 2
    asyncio.run(_serve(config))
    return 0


if __name__ == "__main__":
    sys.exit(main())
