# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure MFA logic: TOTP windows and replay rule, recovery code format, OIDC
``amr`` interpretation, the WebAuthn relying party (no database)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pyotp
import pytest

from app.auth.mfa import recovery, totp
from app.auth.mfa.policy import amr_indicates_mfa, idp_asserted_mfa
from app.auth.mfa.webauthn import RelyingParty

NOW = datetime(2026, 3, 14, 15, 9, 26, tzinfo=UTC)


def _code(secret: str, step: int) -> str:
    return pyotp.TOTP(secret).generate_otp(step)


def test_totp_accepts_one_step_either_side_only() -> None:
    secret = totp.generate_secret()
    current = totp.counter_at(NOW)
    for offset in (-1, 0, 1):
        assert totp.match_counter(
            secret, _code(secret, current + offset), last_counter=None, now=NOW
        ) == (current + offset)
    for offset in (-3, -2, 2, 3):
        assert (
            totp.match_counter(secret, _code(secret, current + offset), last_counter=None, now=NOW)
            is None
        )


def test_totp_code_is_accepted_once_and_older_codes_never() -> None:
    secret = totp.generate_secret()
    current = totp.counter_at(NOW)
    code = _code(secret, current)
    step = totp.match_counter(secret, code, last_counter=None, now=NOW)
    assert step == current
    # Replay of the same code: refused.
    assert totp.match_counter(secret, code, last_counter=step, now=NOW) is None
    # The previous step's code, still inside the window, is older: refused.
    assert (
        totp.match_counter(secret, _code(secret, current - 1), last_counter=step, now=NOW) is None
    )
    # The next step's code is newer: accepted.
    assert totp.match_counter(secret, _code(secret, current + 1), last_counter=step, now=NOW) == (
        current + 1
    )


def test_totp_rejects_malformed_and_wrong_codes() -> None:
    secret = totp.generate_secret()
    current = totp.counter_at(NOW)
    good = _code(secret, current)
    wrong = f"{(int(good) + 1) % 1_000_000:06d}"
    for candidate in (
        wrong,
        "",
        "12345",
        "1234567",
        "abcdef",
        good + "0",
        f" {good[:3]}x{good[4:]}",
    ):
        assert totp.match_counter(secret, candidate, last_counter=None, now=NOW) is None
    # Spaces between digits are tolerated.
    spaced = f"{good[:3]} {good[3:]}"
    assert totp.match_counter(secret, spaced, last_counter=None, now=NOW) == current


def test_totp_counter_follows_rfc6238_steps() -> None:
    assert totp.counter_at(datetime(1970, 1, 1, 0, 0, 59, tzinfo=UTC)) == 1
    assert totp.counter_at(NOW + timedelta(seconds=30)) == totp.counter_at(NOW) + 1
    # RFC 6238 Appendix B test vector (SHA-1, T = 59 s): 94287082 -> 6 digits 287082.
    secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"  # base32("12345678901234567890")
    assert _code(secret, 1) == "287082"


def test_totp_secret_and_uri() -> None:
    secret = totp.generate_secret()
    assert len(secret) == totp.SECRET_LENGTH
    uri = totp.provisioning_uri(secret, "synthetic-user")
    assert uri.startswith("otpauth://totp/PerFin:synthetic-user?")
    assert f"secret={secret}" in uri
    assert "issuer=PerFin" in uri


def test_recovery_codes_format_and_normalisation() -> None:
    codes = {recovery.generate_code() for _ in range(50)}
    assert len(codes) == 50
    for code in codes:
        assert len(code) == 19
        assert code.count("-") == 3
        normalized = recovery.normalize(code)
        assert normalized is not None
        assert len(normalized) == recovery.CODE_LENGTH
        assert recovery.normalize(code.lower().replace("-", " ")) == normalized
    assert recovery.normalize("O1IL-0000-0000-0000") == "0111000000000000"
    for bad in ("", "short", "UUUU-UUUU-UUUU-UUUU", "X" * 65, "ABCD-EFGH-JKMN-PQR"):
        assert recovery.normalize(bad) is None


def test_recovery_hash_is_argon2id_and_not_the_code() -> None:
    normalized = recovery.normalize(recovery.generate_code())
    assert normalized is not None
    hashed = recovery.hash_code(normalized)
    assert hashed.startswith("$argon2id$")
    assert normalized not in hashed


@pytest.mark.parametrize(
    ("amr", "expected"),
    [
        (["pwd", "mfa"], True),
        (["MFA"], True),
        (["pwd", "otp"], True),
        (["hwk", "fpt"], True),
        (["pwd"], False),
        (["otp"], False),
        (["pwd", "pin"], False),
        ([], False),
        (None, False),
        ("mfa", False),  # a string is not a list of methods
    ],
)
def test_amr_interpretation(amr: list[str] | str | None, expected: bool) -> None:
    assert amr_indicates_mfa(amr) is expected


def test_idp_mfa_needs_admin_trust() -> None:
    assert idp_asserted_mfa(["mfa"], trust_idp_mfa=True)
    assert not idp_asserted_mfa(["mfa"], trust_idp_mfa=False)


@pytest.mark.parametrize(
    ("url", "rp_id", "origin"),
    [
        ("https://Finance.Example.org", "finance.example.org", "https://finance.example.org"),
        (
            "https://finance.example.org:443/app/",
            "finance.example.org",
            "https://finance.example.org",
        ),
        (
            "https://finance.example.org:8443",
            "finance.example.org",
            "https://finance.example.org:8443",
        ),
        ("http://localhost:8000", "localhost", "http://localhost:8000"),
    ],
)
def test_relying_party_from_public_url(url: str, rp_id: str, origin: str) -> None:
    rp = RelyingParty.from_public_url(url)
    assert (rp.id, rp.origin, rp.name) == (rp_id, origin, "PerFin")


def test_relying_party_rejects_non_http_urls() -> None:
    for url in ("ftp://example.org", "example.org", ""):
        with pytest.raises(ValueError, match="PERFIN_PUBLIC_URL"):
            RelyingParty.from_public_url(url)
