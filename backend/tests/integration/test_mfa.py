# SPDX-License-Identifier: AGPL-3.0-or-later
"""TOTP, recovery codes, the login's second step and step-up over HTTP
(SECURITY.md A3, A6, A7, A8; AUTH.md "Multi-factor")."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, select, text, update
from sqlalchemy.orm import Session

from app.auth.authz import STEP_UP_WINDOW, step_up_is_fresh
from app.auth.mfa import totp
from app.auth.mfa.models import MfaChallenge
from app.auth.mfa.policy import login_stage, mark_mfa_from_idp
from app.auth.mfa.stepup import RECOVERY_USED_EVENT
from app.auth.models import MfaMethod, MfaType, SessionStage, User, UserSession
from app.auth.principal import authenticate_session
from app.auth.sessions import SessionPolicy, cookie_name, hash_session_id
from app.core import clock
from app.core.crypto import DecryptionError, Keyring
from app.core.settings import Settings
from app.main import create_app
from tests.fixtures import factories as f
from tests.fixtures.factories import Factories, LoggedIn, UserRecord
from tests.fixtures.mfa import (
    MFA,
    RECOVERY,
    SESSION,
    SESSIONS,
    STEP_UP,
    TOTP,
    TOTP_CONFIRM,
    VERIFY,
    WEBAUTHN_REG_OPTIONS,
    FrozenClock,
    adopt_session_cookie,
    freezer,
    totp_code,
    unsafe_headers,
    use_cookie,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def freeze(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FrozenClock]:
    return freezer(monkeypatch)


def _pending(factories: Factories, client: TestClient, record: UserRecord) -> LoggedIn:
    """A password-accepted login waiting for its second factor."""
    return factories.login(client, record, stage=SessionStage.MFA_PENDING, mfa_verified=False)


def _verify(client: TestClient, login: LoggedIn, body: dict[str, Any]) -> Any:
    return client.post(VERIFY, json=body, headers=login.headers())


def _session_row(admin_engine: Engine, session_id: uuid.UUID) -> UserSession:
    with Session(admin_engine) as db:
        row = db.get(UserSession, session_id)
        assert row is not None
        db.expunge(row)
        return row


def _row_by_cookie(admin_engine: Engine, raw: str) -> UserSession:
    with Session(admin_engine) as db:
        row = db.execute(
            select(UserSession).where(UserSession.token_hash == hash_session_id(raw))
        ).scalar_one()
        db.expunge(row)
        return row


def _user(admin_engine: Engine, user_id: uuid.UUID) -> User:
    with Session(admin_engine) as db:
        user = db.get(User, user_id)
        assert user is not None
        db.expunge(user)
        return user


def _set_verified_at(admin_engine: Engine, session_id: uuid.UUID, value: Any) -> None:
    with admin_engine.begin() as conn:
        conn.execute(
            update(UserSession).where(UserSession.id == session_id).values(mfa_verified_at=value)
        )


def _audit_actions(admin_engine: Engine, user_id: uuid.UUID) -> list[str]:
    with admin_engine.connect() as conn:
        rows = conn.execute(
            text("SELECT action FROM audit_log WHERE user_id = :u ORDER BY seq"), {"u": user_id}
        )
        return [r.action for r in rows]


def _fresh_codes(
    factories: Factories, client: TestClient, settings: Settings, record: UserRecord
) -> list[str]:
    """Log in with fresh MFA and regenerate recovery codes (step-up endpoint)."""
    login = factories.login(client, record, mfa_verified=True)
    response = client.post(RECOVERY, headers=login.headers())
    assert response.status_code == 200, response.text
    codes: list[str] = response.json()["codes"]
    client.cookies.clear()
    return codes


# ---------------------------------------------------------------------------
# Login second step (A3: rotation on MFA completion)
# ---------------------------------------------------------------------------


def test_verify_with_totp_rotates_into_full_session(
    factories: Factories, client: TestClient, settings: Settings, admin_engine: Engine
) -> None:
    record = factories.user(with_totp=True)
    assert record.totp_secret is not None
    login = _pending(factories, client, record)
    pending = client.get(SESSION)
    assert pending.status_code == 401
    assert pending.json()["type"] == "urn:perfin:problem:mfa-required"
    # The pending session authorises nothing else.
    assert client.get(SESSIONS).status_code == 401

    response = _verify(client, login, {"method": "totp", "code": record.totp_now()})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["method"] == "totp"
    new_raw = adopt_session_cookie(client, settings, response)
    assert new_raw != login.raw_id
    assert body["csrf_token"] == client.app.state.csrf.token_for_session(new_raw)  # type: ignore[attr-defined]

    old = _session_row(admin_engine, login.session_id)
    assert old.revoked_at is not None
    assert old.revoked_reason == "mfa_completed"
    new = _row_by_cookie(admin_engine, new_raw)
    assert new.stage is SessionStage.AUTHENTICATED
    assert new.rotated_from_id == login.session_id
    assert new.mfa_verified_at is not None

    me = client.get(SESSION)
    assert me.status_code == 200
    assert me.json()["mfa"]["verified"] is True
    assert client.get(SESSIONS).status_code == 200
    # The pre-rotation cookie is dead.
    use_cookie(client, settings, login.raw_id)
    assert client.get(SESSION).json()["type"] == "urn:perfin:problem:unauthenticated"
    assert "auth.mfa.verified" in _audit_actions(admin_engine, record.id)


def test_verify_refuses_bearer_tokens_and_full_sessions(
    factories: Factories, client: TestClient, bearer_token: Callable[..., str]
) -> None:
    record = factories.user(with_totp=True)
    token = bearer_token(record)
    response = client.post(
        VERIFY,
        json={"method": "totp", "code": record.totp_now()},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
    login = factories.login(client, record)  # already a full session
    assert _verify(client, login, {"method": "totp", "code": record.totp_now()}).status_code == 401


def test_totp_replay_is_rejected(
    factories: Factories, client: TestClient, freeze: Callable[..., FrozenClock]
) -> None:
    freeze()
    record = factories.user(with_totp=True)
    code = record.totp_now()
    first = _pending(factories, client, record)
    assert _verify(client, first, {"method": "totp", "code": code}).status_code == 200
    second = _pending(factories, client, record)
    replay = _verify(client, second, {"method": "totp", "code": code})
    assert replay.status_code == 400
    assert replay.json()["type"] == "urn:perfin:problem:mfa-invalid"


def test_totp_window_and_counter_over_http(
    factories: Factories,
    client: TestClient,
    freeze: Callable[..., FrozenClock],
    admin_engine: Engine,
) -> None:
    clk = freeze()
    record = factories.user(with_totp=True)
    secret = record.totp_secret
    assert secret is not None
    step = totp.counter_at(clk.now)
    # One step behind: accepted (clock drift).
    login = _pending(factories, client, record)
    assert (
        _verify(
            client, login, {"method": "totp", "code": totp_code(secret, clk.now, -1)}
        ).status_code
        == 200
    )
    # One step ahead: accepted, and newer than the last.
    login = _pending(factories, client, record)
    assert (
        _verify(
            client, login, {"method": "totp", "code": totp_code(secret, clk.now, 1)}
        ).status_code
        == 200
    )
    with Session(admin_engine) as db:
        stored = db.execute(
            select(MfaMethod.totp_last_counter).where(
                MfaMethod.user_id == record.id, MfaMethod.type == MfaType.TOTP
            )
        ).scalar_one()
    assert stored == step + 1
    # The current step is now older than the last accepted one: refused.
    login = _pending(factories, client, record)
    assert (
        _verify(client, login, {"method": "totp", "code": totp_code(secret, clk.now)}).status_code
        == 400
    )
    # Two steps ahead (outside the window): refused.
    login = _pending(factories, client, record)
    assert (
        _verify(
            client, login, {"method": "totp", "code": totp_code(secret, clk.now, 2)}
        ).status_code
        == 400
    )


def test_wrong_code_is_counted_audited_and_keeps_session_pending(
    factories: Factories, client: TestClient, admin_engine: Engine
) -> None:
    record = factories.user(with_totp=True)
    good = record.totp_now()
    wrong = f"{(int(good) + 500_000) % 1_000_000:06d}"
    login = _pending(factories, client, record)
    response = _verify(client, login, {"method": "totp", "code": wrong})
    assert response.status_code == 400
    assert _user(admin_engine, record.id).failed_login_count == 1
    assert "auth.mfa.failed" in _audit_actions(admin_engine, record.id)
    row = _session_row(admin_engine, login.session_id)
    assert row.revoked_at is None
    assert row.stage is SessionStage.MFA_PENDING
    # Malformed proofs are validation errors, not verification attempts.
    assert _verify(client, login, {"method": "totp", "code": "12"}).status_code == 422
    assert _verify(client, login, {"method": "sms", "code": "123456"}).status_code == 422
    # The right code still works and clears the failure counter.
    assert _verify(client, login, {"method": "totp", "code": good}).status_code == 200
    assert _user(admin_engine, record.id).failed_login_count == 0


def test_repeated_failures_lock_the_account(
    factories: Factories,
    client: TestClient,
    freeze: Callable[..., FrozenClock],
    admin_engine: Engine,
) -> None:
    clk = freeze()
    record = factories.user(with_totp=True)
    good = record.totp_now()
    wrong = f"{(int(good) + 1) % 1_000_000:06d}"
    login = _pending(factories, client, record)
    for _ in range(3):
        assert _verify(client, login, {"method": "totp", "code": wrong}).status_code == 400
    locked = _user(admin_engine, record.id).locked_until
    assert locked is not None
    assert locked > clk.now
    # Even the right code is refused while locked.
    refused = _verify(client, login, {"method": "totp", "code": good})
    assert refused.status_code == 429
    assert int(refused.headers["retry-after"]) >= 1
    clk.advance(seconds=5)
    assert _verify(client, login, {"method": "totp", "code": good}).status_code == 200


def test_mfa_attempts_are_rate_limited_per_session(
    factories: Factories, client: TestClient, freeze: Callable[..., FrozenClock]
) -> None:
    clk = freeze()
    record = factories.user(with_totp=True)
    good = record.totp_now()
    wrong = f"{(int(good) + 7) % 1_000_000:06d}"
    login = _pending(factories, client, record)
    for _ in range(5):
        assert _verify(client, login, {"method": "totp", "code": wrong}).status_code == 400
        clk.advance(seconds=20)  # outlive the progressive delays, stay in the window
    assert (
        _verify(
            client, login, {"method": "totp", "code": totp_code(record.totp_secret or "")}
        ).status_code
        == 429
    )


# ---------------------------------------------------------------------------
# TOTP enrolment, PERFIN_REQUIRE_MFA
# ---------------------------------------------------------------------------


def test_restricted_user_enrols_totp_then_gets_full_access(
    factories: Factories,
    client: TestClient,
    settings: Settings,
    admin_engine: Engine,
    freeze: Callable[..., FrozenClock],
) -> None:
    clk = freeze()
    assert settings.require_mfa
    record = factories.user()  # no factor
    login = factories.login(client, record, mfa_verified=False)
    blocked = client.get(SESSIONS)
    assert blocked.status_code == 403
    assert blocked.json()["type"] == "urn:perfin:problem:mfa-enrollment-required"
    status = client.get(MFA)
    assert status.status_code == 200
    assert status.json() == {"methods": [], "recovery_codes_remaining": 0, "required": True}

    started = client.post(TOTP, headers=login.headers())
    assert started.status_code == 201, started.text
    secret = started.json()["secret"]
    assert started.json()["otpauth_uri"].startswith("otpauth://totp/PerFin:")
    with Session(admin_engine) as db:
        parked = db.execute(
            select(MfaChallenge).where(MfaChallenge.session_id == login.session_id)
        ).scalar_one()
        assert parked.secret_encrypted is not None
        assert secret.encode() not in parked.secret_encrypted
        assert db.execute(select(MfaMethod).where(MfaMethod.user_id == record.id)).first() is None

    wrong = f"{(int(totp_code(secret)) + 1) % 1_000_000:06d}"
    assert (
        client.post(TOTP_CONFIRM, json={"code": wrong}, headers=login.headers()).status_code == 400
    )
    code = totp_code(secret)
    done = client.post(
        TOTP_CONFIRM, json={"code": code, "name": " Phone\x00 "}, headers=login.headers()
    )
    assert done.status_code == 201, done.text
    body = done.json()
    assert body["method"]["type"] == "totp"
    assert body["method"]["name"] == "Phone"
    assert len(body["recovery_codes"]) == 10
    adopt_session_cookie(client, settings, done)
    assert client.get(SESSIONS).status_code == 200  # no longer restricted
    assert client.get(MFA).json()["recovery_codes_remaining"] == 10

    keyring = Keyring.from_settings(settings)
    with Session(admin_engine) as db:
        method = db.execute(
            select(MfaMethod).where(MfaMethod.user_id == record.id, MfaMethod.type == MfaType.TOTP)
        ).scalar_one()
        assert method.secret_encrypted is not None
        assert keyring.decrypt_str(method.secret_encrypted, ad=totp.method_ad(method.id)) == secret
        # Bound to the row: under another id it does not decrypt.
        with pytest.raises(DecryptionError):
            keyring.decrypt_str(method.secret_encrypted, ad=totp.method_ad(uuid.uuid4()))
        assert method.totp_last_counter == totp.counter_at(clk.now)
        assert (
            db.execute(select(MfaChallenge).where(MfaChallenge.user_id == record.id)).first()
            is None
        )
    # The confirming code cannot be replayed for a login.
    client.cookies.clear()
    record.totp_secret = secret
    pending = _pending(factories, client, record)
    assert _verify(client, pending, {"method": "totp", "code": code}).status_code == 400


def test_totp_enrolment_expires(
    factories: Factories, client: TestClient, freeze: Callable[..., FrozenClock]
) -> None:
    clk = freeze()
    record = factories.user()
    login = factories.login(client, record, mfa_verified=False)
    secret = client.post(TOTP, headers=login.headers()).json()["secret"]
    clk.advance(minutes=16)
    # The session itself is idle-expired by now too; use a fresh one.
    login = factories.login(client, record, mfa_verified=False, now=clk.now)
    response = client.post(TOTP_CONFIRM, json={"code": totp_code(secret)}, headers=login.headers())
    assert response.status_code == 409
    assert response.json()["type"] == "urn:perfin:problem:no-totp-enrollment"


def test_adding_a_factor_needs_step_up_once_enrolled(
    factories: Factories, client: TestClient, settings: Settings, admin_engine: Engine
) -> None:
    record = factories.user(with_totp=True)
    login = factories.login(client, record)
    _set_verified_at(admin_engine, login.session_id, clock.utcnow() - timedelta(minutes=11))
    for path in (TOTP, WEBAUTHN_REG_OPTIONS):
        stale = client.post(path, headers=login.headers())
        assert stale.status_code == 403
        assert stale.json()["type"] == "urn:perfin:problem:step-up-required"
    up = client.post(
        STEP_UP, json={"method": "totp", "code": record.totp_now()}, headers=login.headers()
    )
    assert up.status_code == 200, up.text
    adopt_session_cookie(client, settings, up)
    headers = unsafe_headers(up.json()["csrf_token"])
    assert client.post(WEBAUTHN_REG_OPTIONS, headers=headers).status_code == 200
    # One authenticator app per user.
    duplicate = client.post(TOTP, headers=headers)
    assert duplicate.status_code == 409


# ---------------------------------------------------------------------------
# Step-up (A6)
# ---------------------------------------------------------------------------


def test_step_up_freshness_boundary(
    factories: Factories,
    client: TestClient,
    admin_engine: Engine,
    freeze: Callable[..., FrozenClock],
) -> None:
    clk = freeze()
    record = factories.user(with_totp=True)
    login = factories.login(client, record)
    _set_verified_at(admin_engine, login.session_id, clk.now - timedelta(minutes=10))
    assert client.post(RECOVERY, headers=login.headers()).status_code == 200
    _set_verified_at(
        admin_engine, login.session_id, clk.now - timedelta(minutes=10, microseconds=1)
    )
    late = client.post(RECOVERY, headers=login.headers())
    assert late.status_code == 403
    assert late.json()["type"] == "urn:perfin:problem:step-up-required"


def test_remember_device_session_still_needs_fresh_mfa(
    factories: Factories, client: TestClient, settings: Settings, admin_engine: Engine
) -> None:
    record = factories.user(with_totp=True)
    login = factories.login(client, record, remember_device=True)
    _set_verified_at(admin_engine, login.session_id, clock.utcnow() - timedelta(hours=3))
    assert client.get(SESSIONS).status_code == 200  # a long-lived, valid session
    refused = client.post(RECOVERY, headers=login.headers())
    assert refused.status_code == 403
    assert refused.json()["type"] == "urn:perfin:problem:step-up-required"

    up = client.post(
        STEP_UP, json={"method": "totp", "code": record.totp_now()}, headers=login.headers()
    )
    assert up.status_code == 200, up.text
    raw = adopt_session_cookie(client, settings, up)
    old = _session_row(admin_engine, login.session_id)
    new = _row_by_cookie(admin_engine, raw)
    assert old.revoked_reason == "step_up"
    assert new.remember_device is True
    assert new.absolute_expires_at == old.absolute_expires_at  # rotation never extends
    assert new.mfa_verified_at is not None
    assert new.mfa_verified_at > old.created_at
    assert client.post(RECOVERY, headers=unsafe_headers(up.json()["csrf_token"])).status_code == 200
    assert "auth.step_up" in _audit_actions(admin_engine, record.id)


def test_step_up_rejects_wrong_codes_and_tokens(
    factories: Factories, client: TestClient, bearer_token: Callable[..., str]
) -> None:
    record = factories.user(with_totp=True)
    login = factories.login(client, record)
    wrong = f"{(int(record.totp_now()) + 3) % 1_000_000:06d}"
    bad = client.post(STEP_UP, json={"method": "totp", "code": wrong}, headers=login.headers())
    assert bad.status_code == 400
    client.cookies.clear()
    token = bearer_token(record)
    response = client.post(
        STEP_UP,
        json={"method": "totp", "code": record.totp_now()},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert response.json()["type"] == "urn:perfin:problem:session-required"


# ---------------------------------------------------------------------------
# Recovery codes (A8)
# ---------------------------------------------------------------------------


def test_recovery_code_works_once_audits_and_notifies(
    factories: Factories, client: TestClient, settings: Settings, admin_engine: Engine
) -> None:
    record = factories.user(with_totp=True)
    codes = _fresh_codes(factories, client, settings, record)
    assert len(codes) == len(set(codes)) == 10
    with Session(admin_engine) as db:
        hashes = db.execute(
            select(MfaMethod.code_hash).where(
                MfaMethod.user_id == record.id, MfaMethod.type == MfaType.RECOVERY
            )
        ).scalars()
        stored = list(hashes)
    assert len(stored) == 10
    assert all(h and h.startswith("$argon2id$") for h in stored)
    assert not any(c.replace("-", "") in (h or "") for c in codes for h in stored)

    login = _pending(factories, client, record)
    used = _verify(client, login, {"method": "recovery", "code": codes[0].lower()})
    assert used.status_code == 200, used.text
    assert used.json()["method"] == "recovery"
    assert used.json()["recovery_codes_remaining"] == 9
    assert "auth.mfa.recovery_used" in _audit_actions(admin_engine, record.id)
    with admin_engine.connect() as conn:
        events = conn.execute(
            text("SELECT payload FROM event WHERE household_id = :h AND type = :t"),
            {"h": record.household_id, "t": RECOVERY_USED_EVENT},
        ).all()
    assert [e.payload for e in events] == [{"user_id": str(record.id), "remaining": 9}]

    client.cookies.clear()
    again = _pending(factories, client, record)
    assert _verify(client, again, {"method": "recovery", "code": codes[0]}).status_code == 400


def test_regenerating_invalidates_old_codes(
    factories: Factories, client: TestClient, settings: Settings
) -> None:
    record = factories.user(with_totp=True)
    old = _fresh_codes(factories, client, settings, record)
    new = _fresh_codes(factories, client, settings, record)
    assert not set(old) & set(new)
    login = _pending(factories, client, record)
    assert _verify(client, login, {"method": "recovery", "code": old[1]}).status_code == 400
    assert _verify(client, login, {"method": "recovery", "code": new[1]}).status_code == 200


def test_recovery_codes_are_rate_limited(
    factories: Factories,
    client: TestClient,
    settings: Settings,
    freeze: Callable[..., FrozenClock],
) -> None:
    record = factories.user(with_totp=True)
    codes = _fresh_codes(factories, client, settings, record)
    clk = freeze()
    for i in range(5):
        login = _pending(factories, client, record)
        guess = f"0000-0000-0000-000{i}"
        assert _verify(client, login, {"method": "recovery", "code": guess}).status_code == 400
        clk.advance(seconds=30)  # past each progressive delay
    login = _pending(factories, client, record)
    limited = _verify(client, login, {"method": "recovery", "code": codes[2]})
    assert limited.status_code == 429
    assert "retry-after" in limited.headers
    # A refused attempt never spends the code: an hour later it still works.
    clk.advance(hours=1, seconds=1)
    login = factories.login(
        client, record, stage=SessionStage.MFA_PENDING, mfa_verified=False, now=clk.now
    )
    assert _verify(client, login, {"method": "recovery", "code": codes[2]}).status_code == 200


def test_recovery_regeneration_needs_a_factor_and_step_up(
    factories: Factories, client: TestClient, admin_engine: Engine
) -> None:
    record = factories.user(with_totp=True)
    login = factories.login(client, record)
    _set_verified_at(admin_engine, login.session_id, clock.utcnow() - timedelta(minutes=30))
    assert client.post(RECOVERY, headers=login.headers()).status_code == 403
    client.cookies.clear()
    nofactor = factories.user()
    other_client = TestClient(create_app(_settings_without_required_mfa(client)))
    with other_client:
        login = factories.login(other_client, nofactor, mfa_verified=True)
        response = other_client.post(RECOVERY, headers=login.headers())
    assert response.status_code == 409
    assert response.json()["type"] == "urn:perfin:problem:no-mfa-factor"


# ---------------------------------------------------------------------------
# Removing factors
# ---------------------------------------------------------------------------


def _settings_without_required_mfa(client: TestClient) -> Settings:
    current = client.app.state.settings  # type: ignore[attr-defined]
    assert isinstance(current, Settings)
    return current.model_copy(update={"require_mfa": False})


@pytest.fixture
def lenient_client(client: TestClient) -> Iterator[TestClient]:
    with TestClient(create_app(_settings_without_required_mfa(client))) as other:
        yield other


def test_last_factor_cannot_be_removed_while_required(
    factories: Factories, client: TestClient
) -> None:
    record = factories.user(with_totp=True)
    login = factories.login(client, record)
    response = client.delete(TOTP, headers=login.headers())
    assert response.status_code == 409
    assert response.json()["type"] == "urn:perfin:problem:last-mfa-factor"


def test_removing_totp_needs_step_up_and_drops_recovery_codes(
    factories: Factories, lenient_client: TestClient, admin_engine: Engine
) -> None:
    record = factories.user(with_totp=True)
    login = factories.login(lenient_client, record)
    assert lenient_client.post(RECOVERY, headers=login.headers()).status_code == 200
    _set_verified_at(admin_engine, login.session_id, clock.utcnow() - timedelta(minutes=11))
    assert lenient_client.delete(TOTP, headers=login.headers()).status_code == 403
    _set_verified_at(admin_engine, login.session_id, clock.utcnow())
    assert lenient_client.delete(TOTP, headers=login.headers()).status_code == 204
    with Session(admin_engine) as db:
        left = db.execute(select(MfaMethod).where(MfaMethod.user_id == record.id)).all()
    assert left == []
    assert "auth.mfa.removed" in _audit_actions(admin_engine, record.id)
    assert lenient_client.delete(TOTP, headers=login.headers()).status_code == 404


# ---------------------------------------------------------------------------
# Login policy helpers (for the local and OIDC login flows)
# ---------------------------------------------------------------------------


def _verified_at(row: UserSession) -> Any:
    return row.mfa_verified_at


def test_login_stage_and_idp_mfa(db: Session, settings: Settings) -> None:
    keyring = Keyring.from_settings(settings)
    with_factor = f.make_user(db, keyring=keyring, with_totp=True)
    without = f.make_user(db, keyring=keyring)
    assert login_stage(db, with_factor.id) is SessionStage.MFA_PENDING
    assert login_stage(db, without.id) is SessionStage.AUTHENTICATED
    assert login_stage(db, with_factor.id, idp_mfa=True) is SessionStage.AUTHENTICATED

    policy = SessionPolicy.from_settings(settings)
    trusting = settings.model_copy(update={"oidc_trust_idp_mfa": True})
    issued = f.make_session(db, without.id, settings=settings, household_id=without.household_id)
    now = clock.utcnow()
    assert not mark_mfa_from_idp(
        db, issued.row, ["pwd", "mfa"], auth_time=now, settings=settings, now=now
    )
    assert issued.row.mfa_verified_at is None
    restricted = authenticate_session(db, issued.raw_id, policy=policy, require_mfa=True)
    assert restricted is not None
    assert restricted.restricted
    assert not mark_mfa_from_idp(db, issued.row, ["pwd"], auth_time=now, settings=trusting)
    # The stamp is the IdP's authentication time, never "now" (P1-SR2-01).
    long_ago = now - timedelta(days=30)
    assert mark_mfa_from_idp(
        db, issued.row, ["pwd", "mfa"], auth_time=long_ago, settings=trusting, now=now
    )
    assert _verified_at(issued.row) == long_ago
    satisfied = authenticate_session(db, issued.raw_id, policy=policy, require_mfa=True)
    assert satisfied is not None
    assert not satisfied.restricted
    assert not step_up_is_fresh(satisfied.mfa_verified_at, now)
    # Unknown auth_time: MFA for the login, just outside the step-up window.
    assert mark_mfa_from_idp(db, issued.row, ["mfa"], auth_time=None, settings=trusting, now=now)
    assert _verified_at(issued.row) == now - STEP_UP_WINDOW - timedelta(microseconds=1)
    assert not step_up_is_fresh(issued.row.mfa_verified_at, now)
    # Not a reauth login (no step_up_from): a current auth_time is still no
    # step-up, it is dated just outside the window (P1-SR3-01).
    assert mark_mfa_from_idp(db, issued.row, ["mfa"], auth_time=now, settings=trusting, now=now)
    assert _verified_at(issued.row) == now - STEP_UP_WINDOW - timedelta(microseconds=1)
    assert not step_up_is_fresh(issued.row.mfa_verified_at, now)
    # A reauth login whose auth_time is at or after its start: a step-up
    # dated by auth_time.
    started = now - timedelta(minutes=2)
    assert mark_mfa_from_idp(
        db,
        issued.row,
        ["mfa"],
        auth_time=started,
        step_up_from=started,
        settings=trusting,
        now=now,
    )
    assert _verified_at(issued.row) == started
    assert step_up_is_fresh(issued.row.mfa_verified_at, now)
    # A reauth the IdP ignored (auth_time before the start): no step-up.
    assert mark_mfa_from_idp(
        db,
        issued.row,
        ["mfa"],
        auth_time=started - timedelta(microseconds=1),
        step_up_from=started,
        settings=trusting,
        now=now,
    )
    assert not step_up_is_fresh(issued.row.mfa_verified_at, now)
    # An auth_time in the future is never fresher than now.
    assert mark_mfa_from_idp(
        db,
        issued.row,
        ["mfa"],
        auth_time=now + timedelta(hours=1),
        step_up_from=started,
        settings=trusting,
        now=now,
    )
    assert _verified_at(issued.row) == now
    assert step_up_is_fresh(issued.row.mfa_verified_at, now)

    pending = f.make_session(db, with_factor.id, settings=settings, stage=SessionStage.MFA_PENDING)
    with pytest.raises(ValueError, match="authenticated"):
        mark_mfa_from_idp(db, pending.row, ["mfa"], auth_time=None, settings=trusting)


def test_cookie_name_used_by_helpers(settings: Settings) -> None:
    # The helpers above parse Set-Cookie for this name.
    assert cookie_name(settings) == "__Host-perfin-session"
