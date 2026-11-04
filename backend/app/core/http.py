# SPDX-License-Identifier: AGPL-3.0-or-later
"""SSRF-safe outbound HTTP for admin-configured endpoints (SECURITY.md B5).

The OIDC issuer URL is typed in by an administrator, and the discovery
document it serves names further URLs (JWKS, token endpoint). Fetching them
naively would let a hostile or compromised configuration make the API call
``http://169.254.169.254/`` (cloud metadata), ``http://127.0.0.1:5432`` or
anything on the LAN. :class:`SafeHttpClient` refuses that:

* **Host allowlist.** Only hosts in :attr:`EgressPolicy.allowed_hosts`
  (``host:port``, lower-case) are contacted. For OIDC that is the issuer's
  host alone (``app.auth.oidc.discovery`` rejects a discovery document whose
  endpoints live elsewhere).
* **Scheme.** ``https`` only; plain ``http`` only when
  :attr:`EgressPolicy.allow_http` (dev mode). No userinfo in URLs.
* **DNS resolved here, every address vetted, connection pinned.** The
  transport resolves the name itself (:data:`Resolver`, injectable for
  tests), refuses the host if *any* returned address is forbidden, and then
  connects to the vetted address: the request URL is rewritten to that IP,
  while ``Host`` and the TLS server name (SNI, certificate verification) stay
  the original hostname. There is no second lookup, so a DNS-rebinding answer
  cannot slip in between the check and the connection. Each new request is
  resolved and vetted again.
* **Address classes** (:func:`address_verdict`; IPv4-mapped, IPv4-compatible,
  NAT64 and 6to4 IPv6 forms are unwrapped first): link-local (including the
  metadata address ``169.254.169.254`` and ``fe80::/10``), multicast,
  unspecified, broadcast and reserved addresses are **always** refused;
  loopback only with :attr:`EgressPolicy.allow_loopback` (dev mode plus
  ``PERFIN_OIDC_ALLOW_PRIVATE_ISSUER``); every other non-global address
  (RFC 1918, ULA, CGNAT, ...) only for hosts in
  :attr:`EgressPolicy.private_hosts` (the issuer, with
  ``PERFIN_OIDC_ALLOW_PRIVATE_ISSUER``).
* **Redirects** are followed by hand, at most :attr:`EgressPolicy.max_redirects`
  times and only to the same scheme, host and port; anything else is refused.
* **Size and time.** Bodies are read in chunks and abandoned beyond
  :attr:`EgressPolicy.max_response_bytes`; connect/read/write/pool timeouts
  are :attr:`EgressPolicy.timeout_seconds`.

**Egress proxy.** In the Compose deployment the API has no route out; with
the ``egress`` profile it reaches the IdP through the CONNECT allowlist proxy
named by ``HTTPS_PROXY`` (``deploy/egress/egress_proxy.py``, SECURITY.md H2).
That proxy refuses IP literals and needs the hostname, resolves it itself and
applies the same address rules (no private addresses unless
``EGRESS_ALLOW_PRIVATE_CIDRS``, tunnel pinned to the checked address). When a
proxy applies to a host (``HTTPS_PROXY``/``NO_PROXY``, or the ``proxy``
argument), this client therefore sends the hostname through the proxy and the
address check is the proxy's; the host allowlist, scheme, redirect, size and
time rules still apply here.

Errors never contain response bodies or credentials.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import threading
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import httpx

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

#: ``resolve(host, port) -> addresses``. The default uses ``getaddrinfo``.
Resolver = Callable[[str, int], list[IPAddress]]

DEFAULT_MAX_RESPONSE_BYTES: Final = 1024 * 1024
DEFAULT_TIMEOUT_SECONDS: Final = 10.0
DEFAULT_MAX_REDIRECTS: Final = 3
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})

_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL: Final = ipaddress.ip_network("64:ff9b:1::/48")
_THIS_NETWORK: Final = ipaddress.ip_network("0.0.0.0/8")
_BROADCAST: Final = ipaddress.ip_address("255.255.255.255")


class EgressError(Exception):
    """An outbound request failed. The message is safe to log (no secrets)."""


class EgressBlockedError(EgressError):
    """The request was refused by policy (host, scheme, address or redirect)."""


class EgressResponseTooLargeError(EgressError):
    """The response body exceeded the size cap."""


# ---------------------------------------------------------------------------
# Address policy
# ---------------------------------------------------------------------------


def unwrap(address: IPAddress) -> IPAddress:
    """The IPv4 address an IPv6 address embeds (mapped, compatible, NAT64, 6to4)."""
    if isinstance(address, ipaddress.IPv4Address):
        return address
    if address.ipv4_mapped is not None:
        return address.ipv4_mapped
    if address.sixtofour is not None:
        return address.sixtofour
    if address in _NAT64 or address in _NAT64_LOCAL:
        return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    value = int(address)
    # Deprecated IPv4-compatible form ::a.b.c.d (not :: and not ::1).
    if value >> 32 == 0 and value > 1:
        return ipaddress.IPv4Address(value)
    return address


def address_verdict(
    address: IPAddress, *, allow_private: bool = False, allow_loopback: bool = False
) -> str | None:
    """``None`` when ``address`` may be contacted, else the refusal reason."""
    if isinstance(address, ipaddress.IPv6Address) and address.teredo is not None:
        return "tunnel"
    # A wrapped IPv4 address (mapped, compatible, NAT64, 6to4) is judged by the
    # IPv4 address the packets would really reach.
    return _verdict(unwrap(address), allow_private=allow_private, allow_loopback=allow_loopback)


def _verdict(address: IPAddress, *, allow_private: bool, allow_loopback: bool) -> str | None:
    if address.is_loopback:
        return None if allow_loopback else "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_multicast:
        return "multicast"
    if address.is_unspecified:
        return "unspecified"
    if isinstance(address, ipaddress.IPv4Address) and (
        address in _THIS_NETWORK or address == _BROADCAST
    ):
        return "reserved"
    if address.is_reserved:
        return "reserved"
    if address.is_global:
        return None
    # RFC 1918, ULA, CGNAT, documentation and benchmarking ranges.
    return None if allow_private else "private"


def system_resolver(host: str, port: int) -> list[IPAddress]:
    """Resolve ``host`` with ``getaddrinfo`` (TCP), de-duplicated, in order."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise EgressError(f"could not resolve {host}") from exc
    seen: list[IPAddress] = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        # IPv6 scope ids ("fe80::1%eth0") are dropped by ip_address on purpose:
        # link-local is refused anyway.
        text = str(sockaddr[0]).split("%", 1)[0]
        try:
            address = ipaddress.ip_address(text)
        except ValueError:
            continue
        if address not in seen:
            seen.append(address)
    return seen


def _ip_literal(host: str) -> IPAddress | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def host_key(host: str, port: int) -> str:
    """``host:port`` as used in :attr:`EgressPolicy.allowed_hosts`."""
    return f"{host.strip('[]').lower()}:{port}"


def url_host_key(url: str | httpx.URL) -> str:
    parsed = httpx.URL(str(url))
    port = parsed.port or _DEFAULT_PORTS.get(parsed.scheme, 0)
    return host_key(parsed.host, port)


# ---------------------------------------------------------------------------
# Policy and transport
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EgressPolicy:
    """What :class:`SafeHttpClient` may contact.

    ``allowed_hosts`` and ``private_hosts`` hold ``host:port`` keys
    (:func:`host_key`).
    """

    allowed_hosts: frozenset[str]
    private_hosts: frozenset[str] = frozenset()
    allow_http: bool = False
    allow_loopback: bool = False
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    max_redirects: int = DEFAULT_MAX_REDIRECTS

    def check_url(self, url: httpx.URL) -> str:
        """Validate scheme, userinfo and host; return the URL's host key."""
        scheme = url.scheme
        if scheme == "http":
            if not self.allow_http:
                raise EgressBlockedError("plain http is not allowed (https only)")
        elif scheme != "https":
            raise EgressBlockedError(f"scheme {scheme!r} is not allowed")
        if url.userinfo:
            raise EgressBlockedError("credentials in URLs are not allowed")
        if not url.host:
            raise EgressBlockedError("URL has no host")
        key = host_key(url.host, url.port or _DEFAULT_PORTS[scheme])
        if key not in self.allowed_hosts:
            raise EgressBlockedError(f"host {key} is not on the egress allowlist")
        return key

    def vet(self, key: str, addresses: Iterable[IPAddress]) -> IPAddress:
        """The address to connect to; every address of the answer must pass."""
        found = list(addresses)
        if not found:
            raise EgressError(f"{key} did not resolve")
        allow_private = key in self.private_hosts
        for address in found:
            reason = address_verdict(
                address, allow_private=allow_private, allow_loopback=self.allow_loopback
            )
            if reason is not None:
                raise EgressBlockedError(f"{key} resolves to a forbidden address ({reason})")
        return found[0]


ProxySetting = str | None | Literal["env"]


def env_proxy_for(url: httpx.URL) -> str | None:
    """The proxy ``HTTPS_PROXY``/``HTTP_PROXY`` names for ``url``, honouring ``NO_PROXY``."""
    proxies = urllib.request.getproxies_environment()
    proxy = proxies.get(url.scheme)
    if not proxy:
        return None
    host = url.host
    if url.port is not None:
        host = f"{host}:{url.port}"
    if urllib.request.proxy_bypass_environment(host, proxies):  # type: ignore[attr-defined]
        return None
    return proxy


class PinnedTransport(httpx.BaseTransport):
    """Resolve, vet and pin each request to a checked address (see module doc)."""

    def __init__(
        self,
        policy: EgressPolicy,
        *,
        resolver: Resolver = system_resolver,
        proxy: ProxySetting = "env",
        ssl_context: ssl.SSLContext | None = None,
    ) -> None:
        self.policy = policy
        self._resolver = resolver
        self._proxy = proxy
        self._ssl = ssl_context or ssl.create_default_context()
        self._lock = threading.Lock()
        self._direct: dict[str, httpx.HTTPTransport] = {}
        self._proxied: dict[str, httpx.HTTPTransport] = {}

    def _proxy_for(self, url: httpx.URL) -> str | None:
        if self._proxy == "env":
            return env_proxy_for(url)
        return self._proxy

    def _inner(self, key: str, proxy: str | None) -> httpx.HTTPTransport:
        # One pool per host (and proxy), so a connection opened with one
        # host's SNI is never reused for another host on the same address.
        with self._lock:
            if proxy is not None:
                cache_key = f"{key}|{proxy}"
                transport = self._proxied.get(cache_key)
                if transport is None:
                    transport = httpx.HTTPTransport(
                        verify=self._ssl, proxy=proxy, trust_env=False, retries=0
                    )
                    self._proxied[cache_key] = transport
                return transport
            transport = self._direct.get(key)
            if transport is None:
                transport = httpx.HTTPTransport(verify=self._ssl, trust_env=False, retries=0)
                self._direct[key] = transport
            return transport

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        key = self.policy.check_url(request.url)
        proxy = self._proxy_for(request.url)
        if proxy is not None:
            # The egress proxy resolves and vets the name (module doc).
            return self._inner(key, proxy).handle_request(request)
        host = request.url.host
        literal = _ip_literal(host)
        port = request.url.port or _DEFAULT_PORTS[request.url.scheme]
        addresses = [literal] if literal is not None else self._resolver(host, port)
        address = self.policy.vet(key, addresses)
        if literal is None:
            if "host" not in request.headers:
                request.headers["Host"] = request.url.netloc.decode("ascii")
            if request.url.scheme == "https":
                request.extensions = {**request.extensions, "sni_hostname": host}
            request.url = request.url.copy_with(host=str(address))
        return self._inner(key, None).handle_request(request)

    def close(self) -> None:
        with self._lock:
            transports = [*self._direct.values(), *self._proxied.values()]
            self._direct.clear()
            self._proxied.clear()
        for transport in transports:
            transport.close()


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HttpResult:
    """A fully read response (body capped)."""

    status_code: int
    headers: httpx.Headers
    content: bytes = field(repr=False)
    url: str

    def json(self) -> Any:
        import json

        try:
            return json.loads(self.content)
        except ValueError as exc:
            raise EgressError("response is not valid JSON") from exc


class SafeHttpClient:
    """GET/POST through :class:`PinnedTransport` with capped bodies (module doc)."""

    def __init__(
        self,
        policy: EgressPolicy,
        *,
        resolver: Resolver = system_resolver,
        proxy: ProxySetting = "env",
        transport: httpx.BaseTransport | None = None,
        user_agent: str = "PerFin",
    ) -> None:
        self.policy = policy
        self._transport = transport or PinnedTransport(policy, resolver=resolver, proxy=proxy)
        self._client = httpx.Client(
            transport=self._transport,
            follow_redirects=False,
            timeout=httpx.Timeout(policy.timeout_seconds),
            trust_env=False,
            headers={"User-Agent": user_agent, "Accept-Encoding": "identity"},
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> SafeHttpClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _read(self, response: httpx.Response) -> bytes:
        cap = self.policy.max_response_bytes
        encoding = response.headers.get("content-encoding", "identity").strip().lower()
        if encoding not in ("", "identity"):
            # Only identity was accepted; a compressed body could expand past the cap.
            raise EgressError("compressed responses are not accepted")
        declared = response.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > cap:
            raise EgressResponseTooLargeError("response exceeds the size limit")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_raw():
            total += len(chunk)
            if total > cap:
                raise EgressResponseTooLargeError("response exceeds the size limit")
            chunks.append(chunk)
        return b"".join(chunks)

    def _send(
        self,
        method: str,
        url: str,
        *,
        data: Mapping[str, str] | None = None,
        content: bytes | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResult:
        current = httpx.URL(url)
        origin = (current.scheme, url_host_key(current))
        for _hop in range(self.policy.max_redirects + 1):
            try:
                request = self._client.build_request(
                    method,
                    current,
                    data=dict(data) if data is not None else None,
                    content=content,
                    headers=dict(headers or {}),
                )
                response = self._client.send(request, stream=True)
            except EgressError:
                raise
            except httpx.HTTPError as exc:
                raise EgressError(
                    f"request to {url_host_key(current)} failed ({type(exc).__name__})"
                ) from exc
            try:
                if response.status_code in _REDIRECT_STATUSES and method == "GET":
                    location = response.headers.get("location")
                    if not location:
                        raise EgressError("redirect without a location")
                    target = current.join(location)
                    if (target.scheme, url_host_key(target)) != origin:
                        raise EgressBlockedError("redirect to another host or scheme refused")
                    current = target
                    continue
                if response.status_code in _REDIRECT_STATUSES:
                    raise EgressBlockedError("redirect on a non-GET request refused")
                body = self._read(response)
                return HttpResult(response.status_code, response.headers, body, str(current))
            except httpx.HTTPError as exc:
                raise EgressError(
                    f"reading the response from {url_host_key(current)} failed "
                    f"({type(exc).__name__})"
                ) from exc
            finally:
                response.close()
        raise EgressBlockedError("too many redirects")

    def get(self, url: str, *, headers: Mapping[str, str] | None = None) -> HttpResult:
        return self._send("GET", url, headers=headers)

    def post_form(
        self, url: str, data: Mapping[str, str], *, headers: Mapping[str, str] | None = None
    ) -> HttpResult:
        return self._send("POST", url, data=data, headers=headers)

    def send(
        self,
        method: str,
        url: str,
        *,
        content: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> HttpResult:
        """``POST``/``PUT`` a raw body (notification channels); never redirected."""
        if method not in ("POST", "PUT"):
            raise EgressBlockedError(f"method {method!r} is not allowed")
        return self._send(method, url, content=content, headers=headers)
