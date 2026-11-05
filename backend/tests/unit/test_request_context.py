# SPDX-License-Identifier: AGPL-3.0-or-later
"""Trusted-proxy client resolution and request ids (no database)."""

from __future__ import annotations

from ipaddress import ip_network

import pytest

from app.core.request_context import (
    is_trusted,
    request_id_var,
    resolve_client,
    route_template,
    valid_request_id,
)

TRUSTED = [ip_network("10.0.0.0/8"), ip_network("fd00::/8")]


@pytest.mark.parametrize(
    ("peer", "xff", "expected"),
    [
        # No trusted proxies configured / untrusted peer: header ignored.
        ("198.51.100.1", "203.0.113.5", "198.51.100.1"),
        ("testclient", "203.0.113.5", "testclient"),
        (None, "203.0.113.5", None),
        # Trusted peer, single hop.
        ("10.0.0.2", "203.0.113.5", "203.0.113.5"),
        # Spoofed leftmost entries are skipped by walking from the right.
        ("10.0.0.2", "1.2.3.4, 203.0.113.5", "203.0.113.5"),
        # Chained trusted proxies are skipped.
        ("10.0.0.2", "203.0.113.5, 10.9.9.9, 10.1.1.1", "203.0.113.5"),
        # All hops trusted: the leftmost trusted hop is the best we know.
        ("10.0.0.2", "10.3.3.3, 10.1.1.1", "10.3.3.3"),
        # Malformed entry stops the walk at the last good hop.
        ("10.0.0.2", "203.0.113.5, garbage", "10.0.0.2"),
        ("10.0.0.2", "203.0.113.5, garbage, 10.1.1.1", "10.1.1.1"),
        # IPv6, with and without brackets/port.
        ("fd00::1", "2001:db8::7", "2001:db8::7"),
        ("fd00::1", "[2001:db8::7]:4433", "2001:db8::7"),
        ("10.0.0.2", "", "10.0.0.2"),
        ("10.0.0.2", None, "10.0.0.2"),
    ],
)
def test_resolve_client(peer: str | None, xff: str | None, expected: str | None) -> None:
    assert resolve_client(peer, xff, TRUSTED) == expected


def test_nothing_is_trusted_by_default() -> None:
    assert not is_trusted("10.0.0.2", [])
    assert resolve_client("10.0.0.2", "203.0.113.5", []) == "10.0.0.2"


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("0192f1d2-1c4b-7c1e-8a00-123456789abc", True),
        ("proxy.req:0001", True),
        ("short", False),
        ("x" * 129, False),
        ("has space-1234", False),
        ("new\nline-12345", False),
        ("-leading-dash", False),
        (None, False),
    ],
)
def test_valid_request_id(value: str | None, ok: bool) -> None:
    assert valid_request_id(value) is ok


def test_request_id_var_defaults_to_none() -> None:
    assert request_id_var.get() is None


def test_route_template_fallbacks() -> None:
    class FakeRoute:
        path = "/plain/{id}"

    assert route_template({"type": "http"}) is None
    assert route_template({"type": "http", "route": FakeRoute()}) == "/plain/{id}"
