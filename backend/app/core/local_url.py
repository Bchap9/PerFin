# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whether a URL names a service on this host or its private network (ADR-0010).

Used for the optional local-model adapter (``PERFIN_ML_URL``): documents must
never be sent off the premises, so the address is checked when settings load
(:func:`is_local_url`) and the name's addresses before every call
(:func:`resolves_locally`).
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Final
from urllib.parse import urlsplit

#: Host-name suffixes that only resolve on a private network.
LOCAL_SUFFIXES: Final = (".local", ".lan", ".internal", ".home.arpa", ".localhost")


def _local_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return address.is_loopback or address.is_private or address.is_link_local


def _host(url: str) -> str | None:
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    if parts.username or parts.password:
        return None
    return parts.hostname


def is_local_url(url: str) -> bool:
    """An http(s) URL whose host is a local address or an internal-only name."""
    host = _host(url)
    if host is None:
        return False
    try:
        return _local_address(ipaddress.ip_address(host))
    except ValueError:
        pass
    name = host.lower().rstrip(".")
    return name == "localhost" or "." not in name or name.endswith(LOCAL_SUFFIXES)


def resolves_locally(url: str) -> bool:
    """Every address the URL's host resolves to is local (False when it does not resolve)."""
    host = _host(url)
    if host is None:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    addresses = {str(info[4][0]) for info in infos}
    if not addresses:
        return False
    try:
        return all(_local_address(ipaddress.ip_address(a.split("%", 1)[0])) for a in addresses)
    except ValueError:
        return False
