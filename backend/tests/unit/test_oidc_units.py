# SPDX-License-Identifier: AGPL-3.0-or-later
"""OIDC building blocks without a database (``app.auth.oidc``, config checks)."""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic import SecretStr

from app.auth.models import HouseholdRole
from app.auth.oidc.client import CookieSealer, PreAuthState, pkce_challenge, safe_return_to
from app.auth.oidc.discovery import (
    DiscoveryError,
    SigningKey,
    discovery_url,
    parse_jwks,
    parse_metadata,
)
from app.auth.oidc.provisioning import map_groups
from app.auth.oidc.tokens import (
    TokenValidationError,
    at_hash,
    validate_id_token,
    validate_logout_token,
)
from app.core.config_check import check_config
from app.core.settings import Settings, get_settings
from tests.fixtures.fake_idp import CLIENT_ID, ISSUER, FakeIdP

# ---------------------------------------------------------------------------
# PKCE, at_hash (RFC 7636 / OIDC Core test vectors)
# ---------------------------------------------------------------------------


def test_pkce_s256_matches_rfc7636_appendix_b() -> None:
    verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
    assert pkce_challenge(verifier) == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_at_hash_is_left_half_of_sha256() -> None:
    # OIDC Core A.3 example access token and at_hash (RS256).
    assert at_hash("jHkWEdUXMU1BwAsC4vtUsZwnNvTIxEl0z9K3vx5KF0Y") == "77QmUPtjPfzWtF2AnpK9RQ"


# ---------------------------------------------------------------------------
# return_to (B4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["/", "/accounts", "/a/b?c=d&e=%2F", "/x#frag", "/.well-known/x", "/r..2026", "/s?q=../x"],
)
def test_same_origin_paths_are_kept(value: str) -> None:
    assert safe_return_to(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "accounts",
        "//evil.example.test",
        "/\\evil.example.test",
        "\\/evil.example.test",
        "https://evil.example.test/",
        "javascript:alert(1)",
        "/a b",
        "/a\tb",
        "/a\nb",
        "/a\x7fb",
        "/" + "x" * 600,
        # Dot segments (review P1-SR2-02): "/a/..//host" normalises to "//host".
        "/a/..//evil.example.test",
        "/.//evil.example.test",
        "/%2e%2e//evil.example.test/x?y#z",
        "/%2E%2E//evil.example.test",
        "/a/.%2e//evil.example.test",
        "/settings/..",
    ],
)
def test_other_return_to_values_are_dropped(value: str | None) -> None:
    assert safe_return_to(value) is None


# ---------------------------------------------------------------------------
# Sealed cookies
# ---------------------------------------------------------------------------


def test_sealed_cookie_round_trip_purpose_binding_and_tampering() -> None:
    sealer = CookieSealer(os.urandom(32))
    marker = "plaintext-marker-value"
    value = sealer.seal("state", {"s": marker, "n": "b"}, ttl=timedelta(minutes=5))
    # Opaque. (Asserting that no "_"-separated segment equals a one-letter
    # value was flaky: random base64url output sometimes contains "_a_".)
    assert marker not in value
    assert "plaintext" not in value
    payload = sealer.unseal("state", value)
    assert payload is not None
    assert payload["s"] == marker
    assert sealer.unseal("link", value) is None  # another purpose
    assert CookieSealer(os.urandom(32)).unseal("state", value) is None  # another key
    tampered = value[:10] + ("A" if value[10] != "A" else "B") + value[11:]
    assert sealer.unseal("state", tampered) is None
    assert sealer.unseal("state", "") is None
    assert sealer.unseal("state", "%%%") is None
    expired = sealer.seal("state", {"s": "a"}, ttl=timedelta(seconds=-1))
    assert sealer.unseal("state", expired) is None


def test_pre_auth_state_requires_every_field() -> None:
    good = {"s": "x" * 43, "n": "y" * 43, "v": "z" * 86, "r": "/ok"}
    assert PreAuthState.from_payload(good) is not None
    for missing in good:
        assert PreAuthState.from_payload({k: v for k, v in good.items() if k != missing}) is None
    assert PreAuthState.from_payload({**good, "v": "short"}) is None
    state = PreAuthState.from_payload({**good, "r": "//evil.example.test"})
    assert state is not None
    assert state.return_to == "/"
    assert not state.reauth


def test_pre_auth_state_reauth_needs_a_start_time() -> None:
    """Only ``"a": true`` with a valid start time reads as a reauth login
    (the one OIDC login that can earn a step-up, P1-SR3-01)."""
    good = {"s": "x" * 43, "n": "y" * 43, "v": "z" * 86, "r": "/ok"}
    started_ms = 1_800_000_000_000
    state = PreAuthState.from_payload({**good, "a": True, "t": started_ms})
    assert state is not None
    assert state.reauth
    assert state.started_at == datetime.fromtimestamp(started_ms / 1000, tz=UTC)
    for bad in (
        {"a": True},
        {"a": True, "t": None},
        {"a": True, "t": "1800000000000"},
        {"a": True, "t": True},
        {"a": True, "t": 1.8e12},
        {"a": True, "t": 0},
        {"a": True, "t": 10**20},
        {"a": "true", "t": started_ms},
        {"a": 1, "t": started_ms},
        {"a": False, "t": started_ms},
    ):
        parsed = PreAuthState.from_payload({**good, **bad})
        assert parsed is not None, bad
        assert not parsed.reauth, bad


# ---------------------------------------------------------------------------
# Group mapping (B6)
# ---------------------------------------------------------------------------


def _settings(**update: Any) -> Settings:
    return Settings(**update)


def test_group_mapping_strongest_wins_and_never_owner() -> None:
    s = _settings()
    assert map_groups(["perfin-viewers", "perfin-admins"], s) is HouseholdRole.ADMIN
    assert map_groups(["perfin-users"], s) is HouseholdRole.MEMBER
    assert map_groups(["perfin-viewers"], s) is HouseholdRole.VIEWER
    assert map_groups(["owner", "admins", "instance-admins"], s) is None
    assert map_groups([], s) is None
    disabled = _settings(oidc_group_admin="")
    assert map_groups(["", "perfin-admins"], disabled) is None


# ---------------------------------------------------------------------------
# Discovery and JWKS parsing
# ---------------------------------------------------------------------------


def test_discovery_url_and_exact_issuer(idp: FakeIdP) -> None:
    assert discovery_url(ISSUER) == ISSUER + ".well-known/openid-configuration"
    doc = idp
    metadata = parse_metadata(doc.discovery(), issuer=ISSUER, auth_method="client_secret_basic")
    assert metadata.end_session_endpoint is not None
    with pytest.raises(DiscoveryError, match="exactly"):
        parse_metadata(
            doc.discovery(), issuer=ISSUER.rstrip("/"), auth_method="client_secret_basic"
        )
    with pytest.raises(DiscoveryError, match="private_key_jwt"):
        parse_metadata(
            {**doc.discovery(), "token_endpoint_auth_methods_supported": ["client_secret_post"]},
            issuer=ISSUER,
            auth_method="private_key_jwt",
        )
    for broken in ({"jwks_uri": None}, {"id_token_signing_alg_values_supported": ["HS256"]}):
        with pytest.raises(DiscoveryError):
            parse_metadata(
                {**doc.discovery(), **broken}, issuer=ISSUER, auth_method="client_secret_basic"
            )


@pytest.fixture(scope="module")
def idp() -> FakeIdP:
    return FakeIdP()


def test_jwks_keeps_only_pinned_signing_keys(idp: FakeIdP) -> None:
    weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)  # noqa: S505 - must be refused
    weak_jwk: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(weak.public_key(), as_dict=True)
    weak_jwk["kid"] = "weak"
    private_jwk: dict[str, Any] = jwt.algorithms.RSAAlgorithm.to_jwk(idp.keys["rsa"], as_dict=True)
    private_jwk["kid"] = "private"
    doc = {
        "keys": [
            idp.jwk("rsa"),
            idp.jwk("ec"),
            {**idp.jwk("rsa-other"), "use": "enc"},
            {**idp.jwk("rsa-other"), "kid": "hs", "alg": "HS256"},
            {"kty": "oct", "k": base64.urlsafe_b64encode(b"k" * 32).decode(), "kid": "oct"},
            weak_jwk,
            private_jwk,
            "not-a-key",
        ]
    }
    keys = parse_jwks(doc)
    assert sorted((k.kid, k.algorithm) for k in keys) == [
        ("ec-key", "ES256"),
        ("rsa-key", "RS256"),
    ]
    with pytest.raises(DiscoveryError):
        parse_jwks({"keys": [weak_jwk]})
    with pytest.raises(DiscoveryError):
        parse_jwks({"nokeys": []})


# ---------------------------------------------------------------------------
# Token validation without the HTTP layer
# ---------------------------------------------------------------------------


def _lookup(idp: FakeIdP, *names: str) -> Any:
    keys = parse_jwks({"keys": [idp.jwk(n) for n in names or ("rsa", "ec")]})

    def lookup(kid: str | None, alg: str) -> list[SigningKey]:
        return [k for k in keys if k.algorithm == alg and (kid is None or k.kid == kid)]

    return lookup


def test_id_token_happy_path_and_kidless_single_key(idp: FakeIdP) -> None:
    claims = idp.base_claims("u1", nonce="n1", at_hash=at_hash("access"))
    token = idp.sign(claims)
    validated = validate_id_token(
        token,
        issuer=ISSUER,
        client_id=CLIENT_ID,
        keys=_lookup(idp),
        nonce="n1",
        access_token="access",
    )
    assert validated.subject == "u1"
    assert validated.algorithm == "RS256"
    with pytest.raises(TokenValidationError, match="bad-at-hash"):
        validate_id_token(
            token,
            issuer=ISSUER,
            client_id=CLIENT_ID,
            keys=_lookup(idp),
            nonce="n1",
            access_token=None,
        )
    no_kid = jwt.encode(claims, idp.keys["rsa"], algorithm="RS256")
    assert validate_id_token(
        no_kid,
        issuer=ISSUER,
        client_id=CLIENT_ID,
        keys=_lookup(idp, "rsa"),
        nonce="n1",
        access_token="access",
    )


def test_logout_token_rules(idp: FakeIdP) -> None:
    keys = _lookup(idp)
    good = idp.logout_token(sub="u1", sid="s1")
    validated = validate_logout_token(good, issuer=ISSUER, client_id=CLIENT_ID, keys=keys)
    assert (validated.subject, validated.sid) == ("u1", "s1")
    assert validated.jti is not None
    assert validated.issued_at is not None
    sid_only = idp.logout_token(sid="s1")
    assert (
        validate_logout_token(sid_only, issuer=ISSUER, client_id=CLIENT_ID, keys=keys).subject
        is None
    )
    cases = {
        "nonce-in-logout-token": idp.logout_token(sub="u1", nonce="x"),
        "missing-logout-event": idp.logout_token(sub="u1", events={"other": {}}),
        "missing-sub-and-sid": idp.logout_token(),
        "invalid-aud": idp.logout_token(sub="u1", aud="nope"),
        "iat-in-future": idp.logout_token(sub="u1", iat=int(time.time()) + 3600),
        "missing-jti": idp.logout_token(sub="u1", jti=None),
        "invalid-jti": idp.logout_token(sub="u1", jti=""),
    }
    for reason, token in cases.items():
        with pytest.raises(TokenValidationError) as caught:
            validate_logout_token(token, issuer=ISSUER, client_id=CLIENT_ID, keys=keys)
        assert caught.value.reason == reason


# ---------------------------------------------------------------------------
# Config check (B4, B5, B7)
# ---------------------------------------------------------------------------

KEY = base64.b64encode(bytes(range(32))).decode("ascii")
SECRET = base64.b64encode(bytes((i * 37 + 11) % 256 for i in range(48))).decode("ascii")
SECURE_ENV = {
    "PERFIN_PUBLIC_URL": "https://perfin.example.test",
    "PERFIN_MASTER_KEY": KEY,
    "PERFIN_SECRET_KEY": SECRET,
    "PERFIN_DATABASE_URL": "postgresql+psycopg://perfin:synthetic-Pw-7Hq2@db:5432/perfin",
    "PERFIN_BREAKGLASS_CIDRS": "127.0.0.1/32",
    "PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED": "true",
}
OIDC = {
    "oidc_issuer": "https://auth.example.test/application/o/perfin/",
    "oidc_client_id": "perfin",
    "oidc_client_secret": SecretStr("synthetic-oidc-client-secret-Kd83"),
}


@pytest.fixture
def secure_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in list(os.environ):
        if name.startswith("PERFIN_"):
            monkeypatch.delenv(name)
    for name, value in SECURE_ENV.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def codes(**update: Any) -> dict[str, str]:
    return {f.code: f.level for f in check_config(Settings().model_copy(update=update))}


@pytest.mark.usefixtures("secure_env")
def test_oidc_only_mode_needs_client_and_breakglass_admin() -> None:
    assert codes(**OIDC) == {}
    found = codes(**OIDC, local_login_enabled=False)
    assert found == {"breakglass.admin_missing": "error"}
    assert codes(**OIDC, local_login_enabled=False, breakglass_admin_username="rescue") == {}
    assert codes(
        **{**OIDC, "oidc_client_id": ""}, local_login_enabled=False, breakglass_admin_username="r"
    ) == {"oidc.client_id_missing": "error", "auth.oidc_only_incomplete": "error"}
    assert codes(
        **OIDC, local_login_enabled=False, breakglass_admin_username="r", breakglass_cidrs=[]
    ) == {"breakglass.no_cidrs": "error"}
    assert codes(local_login_enabled=False, breakglass_admin_username="r") == {
        "auth.lockout": "error"
    }


@pytest.mark.usefixtures("secure_env")
def test_oidc_client_auth_and_logout_allowlist_rules(tmp_path: Path) -> None:
    assert codes(**OIDC, oidc_post_logout_redirect_uris=["http://perfin.example.test/"]) == {
        "oidc.post_logout_uri_invalid": "error"
    }
    assert codes(**OIDC, oidc_post_logout_redirect_uris=["https://perfin.example.test/?x=1"]) == {
        "oidc.post_logout_uri_invalid": "error"
    }
    assert codes(**OIDC, oidc_post_logout_redirect_uris=["https://perfin.example.test/"]) == {}
    pkj = {**OIDC, "oidc_client_secret": None, "oidc_token_auth_method": "private_key_jwt"}
    assert codes(**pkj) == {"oidc.private_key_missing": "error"}
    key = tmp_path / "key.pem"
    assert codes(**pkj, oidc_private_key_file=key) == {"oidc.private_key_unreadable": "error"}
    key.write_text("synthetic")
    key.chmod(0o644)
    assert codes(**pkj, oidc_private_key_file=key) == {"oidc.private_key_world_readable": "error"}
    key.chmod(0o400)
    assert codes(**pkj, oidc_private_key_file=key) == {}
    assert codes(**{**OIDC, "oidc_issuer": "https://u:p@auth.example.test/"}) == {
        "oidc.issuer_invalid": "error"
    }
    assert codes(**OIDC, oidc_jit_provisioning=True, oidc_require_group=False) == {
        "oidc.jit_without_group": "warning"
    }
    assert codes(oidc_claim_refresh_minutes=5) == {"oidc.claim_refresh_without_oidc": "warning"}


def test_redirect_uri_is_derived_from_public_url() -> None:
    s = Settings(public_url="https://perfin.example.test/")
    assert s.oidc_redirect_uri == "https://perfin.example.test/api/v1/auth/oidc/callback"
    assert s.effective_post_logout_redirect_uris == ("https://perfin.example.test/",)
