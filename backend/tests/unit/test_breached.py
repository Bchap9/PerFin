# SPDX-License-Identifier: AGPL-3.0-or-later
"""Breached-password list and the opt-in HIBP range check (SECURITY.md A1)."""

from __future__ import annotations

import gzip
import hashlib
from collections.abc import Callable
from importlib import resources

import httpx
import pytest
import respx

from app.auth import breached
from app.auth.local import validate_new_password
from app.auth.passwords import PasswordPolicyError
from app.core.settings import Settings

RANGE = "https://api.pwnedpasswords.com/range/"


def _sha1(password: str) -> str:
    return hashlib.sha1(password.encode(), usedforsecurity=False).hexdigest().upper()


def test_bundled_list_is_sorted_lowercase_and_policy_sized() -> None:
    ref = resources.files("app.auth").joinpath("data", "breached-passwords.txt.gz")
    with ref.open("rb") as raw, gzip.open(raw, "rt", encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    assert len(lines) > 50_000
    assert lines == sorted(set(lines))
    assert all(12 <= len(line) <= 128 for line in lines)
    assert all(line == line.lower() for line in lines)
    assert len(breached.local_list()) == len(lines)


@pytest.mark.parametrize(
    "password",
    ["password1234", "Password1234", "PASSWORD1234", "123456789012", "qwertyuiopasdfgh"],
)
def test_common_passwords_are_found_case_insensitively(password: str) -> None:
    assert breached.in_local_list(password)
    assert breached.is_breached(password)


def test_unusual_passphrase_is_not_found() -> None:
    assert not breached.in_local_list("zq8 marmalade orbit vexing tulips")


def _client_factory(router: respx.Router) -> Callable[[], httpx.Client]:
    def factory() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(router.handler), follow_redirects=False)

    return factory


def test_hibp_sends_only_the_prefix_and_finds_a_match() -> None:
    password = "a passphrase only hibp knows"
    digest = _sha1(password)
    router = respx.Router(assert_all_called=True)
    route = router.get(RANGE + digest[:5]).mock(
        return_value=httpx.Response(
            200, text=f"0000000000000000000000000000000000A:0\r\n{digest[5:]}:42\r\n"
        )
    )
    assert breached.in_hibp(password, client_factory=_client_factory(router)) is True
    request = route.calls.last.request
    assert request.url.path == f"/range/{digest[:5]}"
    assert request.headers["add-padding"] == "true"
    assert password.encode() not in request.url.raw_path
    assert digest[5:] not in str(request.url)
    assert request.content == b""


def test_hibp_padding_rows_and_misses_are_not_breaches() -> None:
    password = "another unusual phrase for hibp"
    digest = _sha1(password)
    router = respx.Router()
    router.get(RANGE + digest[:5]).mock(
        return_value=httpx.Response(200, text=f"{digest[5:]}:0\nABCDEF:3\n")
    )
    assert breached.in_hibp(password, client_factory=_client_factory(router)) is False
    other = respx.Router()
    other.get(RANGE + digest[:5]).mock(return_value=httpx.Response(200, text="ABCDEF:3\n"))
    assert breached.in_hibp(password, client_factory=_client_factory(other)) is False


def test_hibp_failures_are_unknown_not_fatal() -> None:
    password = "network trouble passphrase here"
    digest = _sha1(password)
    down = respx.Router()
    down.get(RANGE + digest[:5]).mock(side_effect=httpx.ConnectError("down"))
    assert breached.in_hibp(password, client_factory=_client_factory(down)) is None
    redirect = respx.Router()
    redirect.get(RANGE + digest[:5]).mock(
        return_value=httpx.Response(302, headers={"Location": "http://10.0.0.1/"})
    )
    assert breached.in_hibp(password, client_factory=_client_factory(redirect)) is None
    huge = respx.Router()
    huge.get(RANGE + digest[:5]).mock(
        return_value=httpx.Response(200, content=b"A" * (breached.HIBP_MAX_RESPONSE_BYTES + 10))
    )
    assert breached.in_hibp(password, client_factory=_client_factory(huge)) is None


def test_default_client_follows_no_redirects() -> None:
    with breached._default_client() as client:
        assert client.follow_redirects is False
        assert client.timeout.read == breached.HIBP_TIMEOUT_SECONDS


def test_hibp_only_consulted_when_enabled() -> None:
    calls: list[str] = []

    def factory() -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            digest = _sha1("hibp gated passphrase value")
            return httpx.Response(200, text=f"{digest[5:]}:7\n")

        return httpx.Client(transport=httpx.MockTransport(handler))

    assert breached.is_breached("hibp gated passphrase value", client_factory=factory) is False
    assert calls == []
    assert breached.is_breached("hibp gated passphrase value", hibp=True, client_factory=factory)
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("password", "code"),
    [
        ("short", "too-short"),
        ("y" * 129, "too-long"),
        ("password1234", "breached"),
        ("iloveyou1234", "breached"),
        ("Jane.Synthetic", "same-as-username"),
    ],
)
def test_validate_new_password_rejects(password: str, code: str) -> None:
    with pytest.raises(PasswordPolicyError) as info:
        validate_new_password(password, Settings(), username="jane.synthetic")
    assert info.value.code == code


def test_validate_new_password_accepts_a_long_unusual_phrase() -> None:
    validate_new_password("granite pelican waltzes softly", Settings(), username="someone")
