# SPDX-License-Identifier: AGPL-3.0-or-later
"""Email intake acceptance policy and connection rules, without a mailbox (D1, D5).

* the alias token must be a recipient's local part or ``+`` tag;
* exactly one allowed ``From`` address; spoofed and unknown senders refused;
* ``Authentication-Results``: only the topmost (or the configured server's)
  header counts, comments are ignored, ``spf=pass`` and an aligned
  ``dkim=pass`` are required when the header is present;
* bounded header parsing; the dedupe key;
* the TLS context verifies certificates; direct connections refuse
  link-local, metadata and (unless allowed) private and loopback addresses.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
import uuid

import pytest

from app.core.http import EgressBlockedError, IPAddress, Resolver
from app.core.settings import Settings
from app.ingestion.intake import email
from app.ingestion.intake.email import (
    AcceptancePolicy,
    ConnectOptions,
    RunLimits,
    evaluate,
    has_alias,
    message_key,
    parse_headers,
    read_auth_results,
)
from tests.fixtures.fake_imap import make_mail

TOKEN = "k7q2m4x9t3b5n6pa"
ALEX = uuid.UUID("0190a000-0000-7000-8000-000000000001")
SAM = uuid.UUID("0190a000-0000-7000-8000-000000000002")
LIMITS = RunLimits(max_messages=10, max_message_bytes=1024 * 1024, max_run_bytes=10 * 1024 * 1024)


def _policy(**kwargs: object) -> AcceptancePolicy:
    base: dict[str, object] = {
        "alias_token": TOKEN,
        "senders": {"alex@home.invalid": ALEX, "sam@home.invalid": SAM},
        "require_auth_results": True,
        "authserv_id": None,
    }
    base.update(kwargs)
    return AcceptancePolicy(**base)  # type: ignore[arg-type]


def _decide(mail: bytes, **policy: object) -> email.Decision:
    return evaluate(parse_headers(mail), len(mail), _policy(**policy), LIMITS)


# ---------------------------------------------------------------------------
# Alias token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recipients", "expected"),
    [
        (["receipts+k7q2m4x9t3b5n6pa@perfin.test"], True),
        (["RECEIPTS+K7Q2M4X9T3B5N6PA@perfin.test".lower()], True),
        (["k7q2m4x9t3b5n6pa@perfin.test"], True),
        (["someone@else.invalid", "receipts+k7q2m4x9t3b5n6pa@perfin.test"], True),
        (["receipts@perfin.test"], False),
        (["receipts+k7q2m4x9t3b5n6pb@perfin.test"], False),
        (["receipts+k7q2m4x9t3b5n6pa-x@perfin.test"], False),
        (["receipts+@perfin.test"], False),
        ([], False),
    ],
)
def test_alias_token_match(recipients: list[str], expected: bool) -> None:
    assert has_alias(recipients, TOKEN) is expected


def test_alias_in_delivered_to_counts_and_header_case_folds() -> None:
    mail = make_mail(
        to="receipts@perfin.test", delivered_to=f"Receipts+{TOKEN.upper()}@perfin.test"
    )
    assert _decide(mail).accepted


def test_missing_alias_is_rejected() -> None:
    decision = _decide(make_mail(to="receipts@perfin.test"))
    assert (decision.accepted, decision.reason) == (False, "no_alias")


def test_wrong_alias_is_rejected() -> None:
    decision = _decide(make_mail(to="receipts+aaaaaaaaaaaaaaaa@perfin.test"))
    assert decision.reason == "no_alias"


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------


def _to() -> str:
    return f"receipts+{TOKEN}@perfin.test"


def test_allowed_sender_is_accepted_and_mapped_to_its_member() -> None:
    decision = _decide(make_mail(sender="Sam <SAM@home.invalid>", to=_to()))
    assert decision.accepted
    assert decision.user_id == SAM
    assert decision.sender == "sam@home.invalid"


def test_spoofed_sender_is_rejected() -> None:
    decision = _decide(make_mail(sender="Alex <alex@home.invalid.attacker.invalid>", to=_to()))
    assert (decision.accepted, decision.reason) == (False, "sender_not_allowed")


def test_several_from_addresses_are_rejected() -> None:
    mail = make_mail(sender="mallory@attacker.invalid", extra_from="alex@home.invalid", to=_to())
    assert _decide(mail).reason == "sender_invalid"


def test_display_name_cannot_smuggle_an_allowed_address() -> None:
    mail = make_mail(sender='"alex@home.invalid" <mallory@attacker.invalid>', to=_to())
    assert _decide(mail).reason == "sender_not_allowed"


# ---------------------------------------------------------------------------
# Authentication-Results
# ---------------------------------------------------------------------------

PASS = "mx.perfin.test; spf=pass smtp.mailfrom=home.invalid; dkim=pass header.d=home.invalid"


def test_passing_auth_results_are_accepted() -> None:
    decision = _decide(make_mail(to=_to(), auth_results=[PASS]))
    assert decision.accepted
    assert decision.auth.summary() == {
        "present": True,
        "spf": "pass",
        "dkim": "pass",
    }


@pytest.mark.parametrize(
    "header",
    [
        "mx.perfin.test; spf=pass smtp.mailfrom=home.invalid; dkim=fail header.d=home.invalid",
        "mx.perfin.test; spf=fail smtp.mailfrom=home.invalid; dkim=pass header.d=home.invalid",
        "mx.perfin.test; spf=pass smtp.mailfrom=home.invalid",
        "mx.perfin.test; dkim=pass header.d=home.invalid",
        "mx.perfin.test; spf=softfail; dkim=none",
        # Signed, but by someone else's domain: not aligned with From.
        "mx.perfin.test; spf=pass; dkim=pass header.d=attacker.invalid",
        # A comment cannot carry a result.
        "mx.perfin.test; spf=fail (spf=pass dkim=pass); dkim=fail (dkim=pass)",
        "mx.perfin.test; none",
    ],
)
def test_failing_auth_results_are_rejected(header: str) -> None:
    decision = _decide(make_mail(to=_to(), auth_results=[header]))
    assert (decision.accepted, decision.reason) == (False, "auth_failed")


def test_dkim_from_a_parent_domain_is_aligned() -> None:
    header = "mx.perfin.test; spf=pass; dkim=pass header.i=@home.invalid"
    mail = make_mail(sender="alex@mail.home.invalid", to=_to(), auth_results=[header])
    decision = evaluate(
        parse_headers(mail),
        len(mail),
        _policy(senders={"alex@mail.home.invalid": ALEX}),
        LIMITS,
    )
    assert decision.accepted


def test_without_auth_results_the_alias_is_the_gate() -> None:
    assert _decide(make_mail(to=_to())).accepted


def test_auth_results_check_can_be_turned_off() -> None:
    header = "mx.perfin.test; spf=fail; dkim=fail"
    assert _decide(make_mail(to=_to(), auth_results=[header]), require_auth_results=False).accepted


def test_only_the_topmost_header_counts() -> None:
    # make_mail writes headers top-down in this order: the receiving server
    # prepends its own header above anything the sender added.
    forged = "mx.perfin.test; spf=pass; dkim=pass header.d=home.invalid"
    real_fail = "mx.perfin.test; spf=fail; dkim=fail"
    assert not _decide(make_mail(to=_to(), auth_results=[real_fail, forged])).accepted
    assert _decide(make_mail(to=_to(), auth_results=[PASS, real_fail])).accepted


def test_authserv_id_selects_the_receiving_servers_header() -> None:
    forged = "evil.invalid; spf=pass; dkim=pass header.d=home.invalid"
    real = "mx.perfin.test; spf=fail; dkim=fail"
    mail = make_mail(to=_to(), auth_results=[forged, real])
    assert not _decide(mail, authserv_id="mx.perfin.test").accepted
    # Only foreign headers: present but not from our server -> rejected.
    only_forged = make_mail(to=_to(), auth_results=[forged])
    assert _decide(only_forged, authserv_id="mx.perfin.test").reason == "auth_failed"
    good = make_mail(to=_to(), auth_results=[forged, PASS])
    assert _decide(good, authserv_id="mx.perfin.test").accepted


def test_read_auth_results_without_headers() -> None:
    verdict = read_auth_results([], authserv_id=None, sender_domain="home.invalid")
    assert not verdict.present
    assert not verdict.passed


# ---------------------------------------------------------------------------
# Size, parsing, key
# ---------------------------------------------------------------------------


def test_too_large_is_rejected_before_anything_else() -> None:
    mail = make_mail(to="nobody@perfin.test")
    decision = evaluate(parse_headers(mail), LIMITS.max_message_bytes + 1, _policy(), LIMITS)
    assert decision.reason == "too_large"


def test_header_parsing_is_bounded() -> None:
    many = ", ".join(f"r{i}@x.invalid" for i in range(5000))
    raw = (
        b"Received: from x\r\nFrom: alex@home.invalid\r\n"
        + b"Subject: =?utf-8?q?Caf=C3=A9_receipt?=\x07\r\nTo: "
        + many[:50_000].encode()
        + b"\r\n"
        + b"".join(b"Authentication-Results: a; spf=pass\r\n" for _ in range(500))
        + b"\r\nbody"
    )
    headers = parse_headers(raw)
    assert len(headers.recipients) <= email.ADDRESSES_MAX
    assert len(headers.auth_results) <= email.AUTH_RESULTS_MAX
    assert headers.subject == "Café receipt"
    assert headers.raw_size <= email.HEADER_MAX
    huge = b"From: alex@home.invalid\r\nX-Pad: " + b"a" * (2 * email.HEADER_MAX) + b"\r\n\r\n"
    assert parse_headers(huge).raw_size == email.HEADER_MAX


def test_message_key_uses_message_id_and_sender() -> None:
    a = make_mail(message_id="<same@shop.invalid>", subject="one")
    b = make_mail(message_id="<SAME@shop.invalid>", subject="two")
    c = make_mail(message_id="<same@shop.invalid>", sender="mallory@attacker.invalid")
    d = make_mail(message_id=None, subject="three")
    e = make_mail(message_id=None, subject="four")
    keys = [message_key(parse_headers(m), m) for m in (a, b, c, d, e)]
    assert keys[0] == keys[1]
    assert len({keys[0], keys[2], keys[3], keys[4]}) == 4
    assert all(len(k) == 64 for k in keys)


# ---------------------------------------------------------------------------
# Connection rules (D5)
# ---------------------------------------------------------------------------


def test_client_context_verifies_certificates_and_names() -> None:
    context = email.client_ssl_context()
    assert context.verify_mode is ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2


def test_connect_options_from_settings() -> None:
    options = ConnectOptions.from_settings(Settings(email_intake_timeout_seconds=7))
    assert options.timeout_seconds == 7
    assert not options.allow_private
    assert not options.allow_loopback
    dev = ConnectOptions.from_settings(
        Settings(dev_mode=True, email_intake_allow_private_hosts=True)
    )
    assert dev.allow_private
    assert dev.allow_loopback


def _resolver(*addresses: str) -> Resolver:
    def resolve(host: str, port: int) -> list[IPAddress]:
        return [ipaddress.ip_address(a) for a in addresses]

    return resolve


@pytest.mark.parametrize(
    ("addresses", "allow_private", "allow_loopback"),
    [
        (("169.254.169.254",), True, True),  # metadata: always refused
        (("fe80::1",), True, True),
        (("10.0.0.5",), False, False),
        (("93.184.216.34", "192.168.1.2"), False, False),  # one bad answer is enough
        (("127.0.0.1",), True, False),
        (("::ffff:127.0.0.1",), True, False),
        (("0.0.0.0",), True, True),  # noqa: S104 - an address to refuse
    ],
)
def test_direct_connection_refuses_forbidden_addresses(
    addresses: tuple[str, ...],
    allow_private: bool,
    allow_loopback: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_connect(*args: object, **kwargs: object) -> socket.socket:
        raise AssertionError("must not connect")

    monkeypatch.setattr(socket, "create_connection", no_connect)
    options = ConnectOptions(
        proxy=None,
        allow_private=allow_private,
        allow_loopback=allow_loopback,
        resolver=_resolver(*addresses),
    )
    with pytest.raises(EgressBlockedError):
        email.open_socket("imap.perfin.test", 993, options)


def test_ip_literal_hosts_are_vetted_too(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "create_connection", lambda *a, **k: None)
    with pytest.raises(EgressBlockedError):
        email.open_socket("169.254.169.254", 993, ConnectOptions(proxy=None, allow_private=True))


def test_proxy_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("HTTPS_PROXY", "https_proxy"):
        monkeypatch.setenv(name, "http://egress:3128")
    for name in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)
    assert ConnectOptions().proxy_for("imap.perfin.test", 993) == "http://egress:3128"
    monkeypatch.setenv("no_proxy", "imap.perfin.test")
    assert ConnectOptions().proxy_for("imap.perfin.test", 993) is None
    assert ConnectOptions(proxy=None).proxy_for("imap.perfin.test", 993) is None
