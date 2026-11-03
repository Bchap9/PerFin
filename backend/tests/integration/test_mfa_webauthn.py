# SPDX-License-Identifier: AGPL-3.0-or-later
"""WebAuthn registration, login and step-up with a software authenticator
(``tests/fixtures/webauthn_authenticator.py``): challenge single use, sign
counter, origin/RP binding, credential ownership."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.auth.mfa.models import MfaChallenge
from app.auth.models import MfaMethod, MfaType, SessionStage
from app.core.settings import Settings
from app.main import API_PREFIX
from tests.fixtures.factories import Factories, LoggedIn, UserRecord
from tests.fixtures.mfa import (
    MFA,
    SESSIONS,
    STEP_UP,
    VERIFY,
    WEBAUTHN,
    WEBAUTHN_AUTH_OPTIONS,
    WEBAUTHN_REG_OPTIONS,
    FrozenClock,
    adopt_session_cookie,
    freezer,
    unsafe_headers,
)
from tests.fixtures.webauthn_authenticator import SoftAuthenticator, b64url

pytestmark = pytest.mark.integration

RP_ID = "testserver"
ORIGIN = "http://testserver"


@pytest.fixture
def freeze(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FrozenClock]:
    return freezer(monkeypatch)


def _register(
    factories: Factories, client: TestClient, settings: Settings, record: UserRecord
) -> tuple[SoftAuthenticator, dict[str, Any]]:
    """Enrol a software authenticator as the user's first factor."""
    login = factories.login(client, record, mfa_verified=False)
    options = client.post(WEBAUTHN_REG_OPTIONS, headers=login.headers())
    assert options.status_code == 200, options.text
    authenticator = SoftAuthenticator(RP_ID, ORIGIN)
    response = client.post(
        WEBAUTHN,
        json={"credential": authenticator.create(options.json()), "name": "Desk key"},
        headers=login.headers(),
    )
    assert response.status_code == 201, response.text
    adopt_session_cookie(client, settings, response)
    client.cookies.clear()
    return authenticator, response.json()


def _pending(factories: Factories, client: TestClient, record: UserRecord) -> LoggedIn:
    return factories.login(client, record, stage=SessionStage.MFA_PENDING, mfa_verified=False)


def _options(client: TestClient, login: LoggedIn) -> dict[str, Any]:
    response = client.post(WEBAUTHN_AUTH_OPTIONS, headers=login.headers())
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _verify(client: TestClient, login: LoggedIn, credential: dict[str, Any]) -> Any:
    return client.post(
        VERIFY, json={"method": "webauthn", "credential": credential}, headers=login.headers()
    )


def test_registration_options_shape(
    factories: Factories, client: TestClient, admin_engine: Engine
) -> None:
    record = factories.user()
    login = factories.login(client, record, mfa_verified=False)
    options = client.post(WEBAUTHN_REG_OPTIONS, headers=login.headers()).json()
    assert options["rp"] == {"id": RP_ID, "name": "PerFin"}
    assert options["attestation"] == "none"
    assert options["authenticatorSelection"]["residentKey"] == "preferred"
    assert options["authenticatorSelection"]["userVerification"] == "preferred"
    assert options["user"]["name"] == record.user.username
    with Session(admin_engine) as db:
        stored = db.execute(
            select(MfaChallenge).where(MfaChallenge.session_id == login.session_id)
        ).scalar_one()
    assert stored.purpose == "webauthn_register"
    assert stored.challenge is not None
    assert len(stored.challenge) == 32


def test_register_then_login_with_security_key(
    factories: Factories, client: TestClient, settings: Settings, admin_engine: Engine
) -> None:
    record = factories.user()
    authenticator, added = _register(factories, client, settings, record)
    assert added["method"]["type"] == "webauthn"
    assert added["method"]["name"] == "Desk key"
    assert len(added["recovery_codes"]) == 10
    with Session(admin_engine) as db:
        method = db.execute(
            select(MfaMethod).where(
                MfaMethod.user_id == record.id, MfaMethod.type == MfaType.WEBAUTHN
            )
        ).scalar_one()
    assert method.credential_id == authenticator.credential_id
    assert method.public_key == authenticator.cose_public_key()
    assert method.transports == ["nfc", "usb"]  # unknown transports dropped

    login = _pending(factories, client, record)
    options = _options(client, login)
    assert options["rpId"] == RP_ID
    assert options["userVerification"] == "preferred"
    assert [c["id"] for c in options["allowCredentials"]] == [b64url(authenticator.credential_id)]
    response = _verify(client, login, authenticator.get(options))
    assert response.status_code == 200, response.text
    assert response.json()["method"] == "webauthn"
    adopt_session_cookie(client, settings, response)
    assert client.get(SESSIONS).status_code == 200
    with Session(admin_engine) as db:
        count = db.execute(
            select(MfaMethod.sign_count).where(MfaMethod.id == method.id)
        ).scalar_one()
    assert count == authenticator.sign_count == 1


def test_assertion_cannot_be_replayed(
    factories: Factories, client: TestClient, settings: Settings
) -> None:
    record = factories.user()
    authenticator, _ = _register(factories, client, settings, record)
    login = _pending(factories, client, record)
    assertion = authenticator.get(_options(client, login))
    assert _verify(client, login, assertion).status_code == 200
    client.cookies.clear()
    again = _pending(factories, client, record)
    _options(client, again)  # a new challenge exists, but the old assertion signed another one
    assert _verify(client, again, assertion).status_code == 400


def test_challenge_is_single_use_even_after_a_failure(
    factories: Factories, client: TestClient, settings: Settings
) -> None:
    record = factories.user()
    authenticator, _ = _register(factories, client, settings, record)
    login = _pending(factories, client, record)
    options = _options(client, login)
    wrong_origin = authenticator.get(options, origin="https://evil.example")
    assert _verify(client, login, wrong_origin).status_code == 400
    # A correct assertion over the spent challenge is refused too.
    assert _verify(client, login, authenticator.get(options)).status_code == 400
    # A fresh challenge works.
    assert _verify(client, login, authenticator.get(_options(client, login))).status_code == 200


def test_challenge_without_options_or_expired_is_refused(
    factories: Factories,
    client: TestClient,
    settings: Settings,
    freeze: Callable[..., FrozenClock],
) -> None:
    record = factories.user()
    authenticator, _ = _register(factories, client, settings, record)
    clk = freeze()
    login = _pending(factories, client, record)
    forged = authenticator.get({"challenge": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"})
    assert _verify(client, login, forged).status_code == 400
    options = _options(client, login)
    clk.advance(minutes=6)
    login = factories.login(
        client, record, stage=SessionStage.MFA_PENDING, mfa_verified=False, now=clk.now
    )
    # The old challenge belonged to the old session and has expired anyway.
    assert _verify(client, login, authenticator.get(options)).status_code == 400


def test_sign_counter_must_increase(
    factories: Factories, client: TestClient, settings: Settings
) -> None:
    record = factories.user()
    authenticator, _ = _register(factories, client, settings, record)
    login = _pending(factories, client, record)
    assert (
        _verify(client, login, authenticator.get(_options(client, login), increment=5)).status_code
        == 200
    )
    client.cookies.clear()
    login = _pending(factories, client, record)
    # A cloned authenticator would report a counter not above the stored one.
    authenticator.sign_count = 2
    cloned = authenticator.get(_options(client, login), increment=1)  # counter 3 <= 5
    assert _verify(client, login, cloned).status_code == 400


def test_assertion_bound_to_rp_and_user(
    factories: Factories, client: TestClient, settings: Settings
) -> None:
    alice = factories.user()
    bob = factories.user()
    alice_key, _ = _register(factories, client, settings, alice)
    _register(factories, client, settings, bob)
    login = _pending(factories, client, alice)
    wrong_rp = alice_key.get(_options(client, login), rp_id="evil.example")
    assert _verify(client, login, wrong_rp).status_code == 400
    wrong_handle = alice_key.get(_options(client, login), user_handle=bob.id.bytes)
    assert _verify(client, login, wrong_handle).status_code == 400
    client.cookies.clear()
    # Alice's key cannot complete Bob's login.
    bob_login = _pending(factories, client, bob)
    assert _verify(client, bob_login, alice_key.get(_options(client, bob_login))).status_code == 400
    # Garbage is a clean 400, not a server error.
    garbage = {"id": "x", "rawId": "x", "type": "public-key", "response": {}}
    _options(client, bob_login)
    assert _verify(client, bob_login, garbage).status_code == 400


def test_webauthn_step_up_and_management(
    factories: Factories, client: TestClient, settings: Settings, admin_engine: Engine
) -> None:
    record = factories.user()
    authenticator, added = _register(factories, client, settings, record)
    method_id = added["method"]["id"]
    login = factories.login(client, record, mfa_verified=False)
    listed = client.get(WEBAUTHN).json()
    assert [m["id"] for m in listed] == [method_id]
    status = client.get(MFA).json()
    assert status["recovery_codes_remaining"] == 10
    # Removing needs step-up.
    stale = client.delete(f"{WEBAUTHN}/{method_id}", headers=login.headers())
    assert stale.status_code == 403
    options = _options(client, login)
    up = client.post(
        STEP_UP,
        json={"method": "webauthn", "credential": authenticator.get(options)},
        headers=login.headers(),
    )
    assert up.status_code == 200, up.text
    adopt_session_cookie(client, settings, up)
    headers = unsafe_headers(up.json()["csrf_token"])
    # The only factor while MFA is required: refused.
    last = client.delete(f"{WEBAUTHN}/{method_id}", headers=headers)
    assert last.status_code == 409
    # Someone else's id looks like a missing one.
    other = factories.user()
    _, other_added = _register(factories, client, settings, other)
    adopt_session_cookie(client, settings, up)
    missing = client.delete(f"{WEBAUTHN}/{other_added['method']['id']}", headers=headers)
    assert missing.status_code == 404


def test_registration_rejects_wrong_origin_and_reused_challenge(
    factories: Factories, client: TestClient
) -> None:
    record = factories.user()
    login = factories.login(client, record, mfa_verified=False)
    options = client.post(WEBAUTHN_REG_OPTIONS, headers=login.headers()).json()
    authenticator = SoftAuthenticator(RP_ID, ORIGIN)
    bad = client.post(
        WEBAUTHN,
        json={"credential": authenticator.create(options, origin="https://evil.example")},
        headers=login.headers(),
    )
    assert bad.status_code == 400
    assert bad.json()["type"] == "urn:perfin:problem:webauthn-registration-failed"
    reused = client.post(
        WEBAUTHN, json={"credential": authenticator.create(options)}, headers=login.headers()
    )
    assert reused.status_code == 400


def test_authentication_options_need_a_credential(factories: Factories, client: TestClient) -> None:
    record = factories.user(with_totp=True)
    login = _pending(factories, client, record)
    response = client.post(WEBAUTHN_AUTH_OPTIONS, headers=login.headers())
    assert response.status_code == 409
    client.cookies.clear()
    anonymous = client.post(
        f"{API_PREFIX}/auth/mfa/webauthn/authentication-options", headers=login.headers()
    )
    assert anonymous.status_code in (401, 403)
