# SPDX-License-Identifier: AGPL-3.0-or-later
"""Server-side sessions (ADR-0005; SECURITY.md A3, A4, A6): hashed ids,
idle/absolute expiry, rotation, revocation and the cookie attributes."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import LoginMethod, SessionStage, UserSession
from app.auth.sessions import (
    PENDING_TTL,
    TOUCH_INTERVAL,
    IssuedSession,
    SessionPolicy,
    clear_session_cookie,
    complete_mfa,
    create_session,
    hash_session_id,
    list_sessions,
    load_session,
    prune_sessions,
    record_step_up,
    revoke_all_sessions,
    revoke_other_sessions,
    revoke_session,
    rotate_session,
    set_session_cookie,
    switch_household,
)
from app.core.settings import Settings
from tests.fixtures.factories import make_household, make_user

pytestmark = pytest.mark.integration

POLICY = SessionPolicy(
    idle=timedelta(minutes=30), absolute=timedelta(hours=12), remember=timedelta(days=30)
)
T0 = datetime(2026, 3, 1, 9, 0, tzinfo=UTC)


def _new(db: Session, **kwargs: object) -> IssuedSession:
    record = make_user(db, role=None)
    options: dict[str, object] = {"login_method": LoginMethod.LOCAL, "now": T0}
    options.update(kwargs)
    return create_session(db, user_id=record.id, policy=POLICY, **options)  # type: ignore[arg-type]


def test_only_the_hash_is_stored(db: Session) -> None:
    issued = _new(db)
    assert len(issued.raw_id) == 43
    stored = db.execute(select(UserSession.token_hash)).scalars().all()
    assert hash_session_id(issued.raw_id) in stored
    assert issued.raw_id not in stored
    assert issued.raw_id not in repr(issued)


def test_load_by_cookie_value(db: Session) -> None:
    issued = _new(db)
    row = load_session(db, issued.raw_id, policy=POLICY, now=T0 + timedelta(minutes=1))
    assert row is not None
    assert row.id == issued.row.id
    assert load_session(db, "A" * 43, policy=POLICY, now=T0) is None
    assert load_session(db, None, policy=POLICY, now=T0) is None
    assert load_session(db, issued.raw_id + "x", policy=POLICY, now=T0) is None


def test_idle_timeout_and_sliding(db: Session) -> None:
    issued = _new(db)
    assert issued.row.idle_expires_at == T0 + timedelta(minutes=30)
    # Activity at +20 min slides the idle expiry to +50 min.
    assert load_session(db, issued.raw_id, policy=POLICY, now=T0 + timedelta(minutes=20))
    assert issued.row.idle_expires_at == T0 + timedelta(minutes=50)
    assert load_session(db, issued.raw_id, policy=POLICY, now=T0 + timedelta(minutes=49))
    assert load_session(db, issued.raw_id, policy=POLICY, now=T0 + timedelta(minutes=80)) is None


def test_touch_is_throttled(db: Session) -> None:
    issued = _new(db)
    load_session(db, issued.raw_id, policy=POLICY, now=T0 + TOUCH_INTERVAL / 2)
    assert issued.row.last_seen_at == T0
    load_session(db, issued.raw_id, policy=POLICY, now=T0 + TOUCH_INTERVAL)
    assert issued.row.last_seen_at == T0 + TOUCH_INTERVAL


def test_absolute_lifetime_is_not_extended_by_activity(db: Session) -> None:
    issued = _new(db)
    now = T0
    while now + timedelta(minutes=20) < T0 + timedelta(hours=12):
        now += timedelta(minutes=20)
        assert load_session(db, issued.raw_id, policy=POLICY, now=now) is not None
    assert issued.row.idle_expires_at <= issued.row.absolute_expires_at
    assert load_session(db, issued.raw_id, policy=POLICY, now=T0 + timedelta(hours=12)) is None


def test_remember_device_lives_thirty_days_without_idle_logout(db: Session) -> None:
    issued = _new(db, remember_device=True)
    assert issued.row.absolute_expires_at == T0 + timedelta(days=30)
    assert load_session(db, issued.raw_id, policy=POLICY, now=T0 + timedelta(days=3))
    assert load_session(db, issued.raw_id, policy=POLICY, now=T0 + timedelta(days=30)) is None


def test_pending_mfa_session_is_short_lived(db: Session) -> None:
    issued = _new(db, stage=SessionStage.MFA_PENDING, remember_device=True)
    assert issued.row.absolute_expires_at == T0 + PENDING_TTL
    assert load_session(db, issued.raw_id, policy=POLICY, now=T0 + PENDING_TTL) is None


def test_rotation_revokes_the_old_id(db: Session) -> None:
    """A3: after rotation the previous cookie value is dead."""
    issued = _new(db)
    later = T0 + timedelta(minutes=5)
    rotated = rotate_session(db, issued.row, policy=POLICY, reason="privilege_change", now=later)
    assert rotated.raw_id != issued.raw_id
    assert load_session(db, issued.raw_id, policy=POLICY, now=later) is None
    assert load_session(db, rotated.raw_id, policy=POLICY, now=later) is not None
    assert issued.row.revoked_reason == "privilege_change"
    assert rotated.row.rotated_from_id == issued.row.id
    # Rotation does not extend the absolute lifetime.
    assert rotated.row.absolute_expires_at == issued.row.absolute_expires_at


def test_complete_mfa_rotates_and_starts_full_lifetime(db: Session) -> None:
    pending = _new(db, stage=SessionStage.MFA_PENDING)
    done_at = T0 + timedelta(minutes=2)
    full = complete_mfa(db, pending.row, policy=POLICY, now=done_at)
    assert full.row.stage is SessionStage.AUTHENTICATED
    assert full.row.mfa_verified_at == done_at
    assert full.row.absolute_expires_at == done_at + POLICY.absolute
    assert load_session(db, pending.raw_id, policy=POLICY, now=done_at) is None
    assert pending.row.revoked_reason == "mfa_completed"


def test_step_up_rotates_and_stamps(db: Session) -> None:
    issued = _new(db)
    at = T0 + timedelta(hours=1)
    stepped = record_step_up(db, issued.row, policy=POLICY, now=at)
    assert stepped.row.mfa_verified_at == at
    assert issued.row.revoked_at == at


def test_switch_household_rotates(db: Session) -> None:
    issued = _new(db)
    household = make_household(db)
    switched = switch_household(db, issued.row, household.id, policy=POLICY, now=T0)
    assert switched.row.household_id == household.id
    assert issued.row.revoked_reason == "privilege_change"


def test_list_and_revoke(db: Session) -> None:
    record = make_user(db, role=None)
    now = T0 + timedelta(minutes=1)
    sessions = [
        create_session(db, user_id=record.id, policy=POLICY, login_method=LoginMethod.LOCAL, now=T0)
        for _ in range(3)
    ]
    other_user = _new(db)
    listed = list_sessions(db, record.id, now=now)
    assert {s.id for s in listed} == {s.row.id for s in sessions}
    assert revoke_session(db, sessions[0].row.id, user_id=record.id, now=now)
    assert not revoke_session(db, sessions[0].row.id, user_id=record.id, now=now)
    # Cannot revoke another user's session.
    assert not revoke_session(db, other_user.row.id, user_id=record.id, now=now)
    assert load_session(db, other_user.raw_id, policy=POLICY, now=now) is not None
    assert len(list_sessions(db, record.id, now=now)) == 2


def test_revoke_others_keeps_current(db: Session) -> None:
    record = make_user(db, role=None)
    a, b, c = (
        create_session(db, user_id=record.id, policy=POLICY, login_method=LoginMethod.LOCAL, now=T0)
        for _ in range(3)
    )
    assert revoke_other_sessions(db, record.id, keep_session_id=b.row.id, now=T0) == 2
    db.expire_all()
    assert load_session(db, a.raw_id, policy=POLICY, now=T0) is None
    assert load_session(db, b.raw_id, policy=POLICY, now=T0) is not None
    assert load_session(db, c.raw_id, policy=POLICY, now=T0) is None
    assert revoke_all_sessions(db, record.id, now=T0) == 1
    db.expire_all()
    assert load_session(db, b.raw_id, policy=POLICY, now=T0) is None


def test_prune_old_sessions(db: Session) -> None:
    issued = _new(db)
    assert prune_sessions(db, older_than=timedelta(days=7), now=T0 + timedelta(days=1)) == 0
    assert prune_sessions(db, older_than=timedelta(days=7), now=T0 + timedelta(days=9)) >= 1
    db.expire_all()
    assert db.get(UserSession, issued.row.id) is None


# ---- cookies ---------------------------------------------------------------------


def _cookie_app(settings: Settings, issued: IssuedSession) -> TestClient:
    app = FastAPI()

    @app.get("/set")
    def set_cookie() -> Response:
        response = Response()
        set_session_cookie(response, settings, issued)
        return response

    @app.get("/clear")
    def clear() -> Response:
        response = Response()
        clear_session_cookie(response, settings)
        return response

    return TestClient(app)


def test_session_cookie_attributes(db: Session, settings: Settings) -> None:
    """A4: HttpOnly, Secure, __Host- prefix (Path=/, no Domain), SameSite=Lax."""
    issued = _new(db, now=None)
    header = _cookie_app(settings, issued).get("/set").headers["set-cookie"]
    assert header.startswith(f"__Host-perfin-session={issued.raw_id};")
    lowered = header.lower()
    for attribute in ("httponly", "secure", "path=/", "samesite=lax"):
        assert attribute in lowered
    assert "domain=" not in lowered
    assert "max-age" not in lowered  # a browser-session cookie


def test_remember_device_cookie_has_max_age(db: Session, settings: Settings) -> None:
    issued = _new(db, remember_device=True, now=None)
    header = _cookie_app(settings, issued).get("/set").headers["set-cookie"].lower()
    max_age = int(header.split("max-age=")[1].split(";")[0])
    assert 29 * 86400 < max_age <= 30 * 86400


def test_dev_cookie_without_secure(db: Session) -> None:
    dev = Settings(dev_mode=True, secure_cookies=False)
    issued = _new(db, now=None)
    header = _cookie_app(dev, issued).get("/set").headers["set-cookie"]
    assert header.startswith("perfin-session-dev=")
    assert "secure" not in header.lower()
    assert "httponly" in header.lower()


def test_clear_cookie(db: Session, settings: Settings) -> None:
    issued = _new(db, now=None)
    header = _cookie_app(settings, issued).get("/clear").headers["set-cookie"]
    assert header.startswith('__Host-perfin-session="";') or header.startswith(
        "__Host-perfin-session=;"
    )
    assert "max-age=0" in header.lower()


def test_auth_prune_job(db: Session) -> None:
    """The hourly ``auth_prune`` job removes long-ended sessions and old events."""
    import threading

    from app.auth.models import RateLimitEvent
    from app.core import clock
    from app.workers.handlers.auth_maintenance import AuthPrunePayload, auth_prune
    from app.workers.registry import JobContext
    from app.workers.scheduler import SCHEDULE

    assert any(job.job_type == "auth_prune" for job in SCHEDULE.jobs())
    now = clock.utcnow()
    old = _new(db, now=now - timedelta(days=30))
    live = _new(db, now=now)
    db.add(
        RateLimitEvent(scope="test:prune", key_hash="0" * 64, created_at=now - timedelta(days=2))
    )
    db.flush()
    ctx = JobContext(
        job_id=old.row.id,
        job_type="auth_prune",
        attempt=1,
        max_attempts=3,
        household_id=None,
        session=db,
        deadline=now + timedelta(minutes=5),
        cancelled=threading.Event(),
    )
    auth_prune(ctx, AuthPrunePayload())
    db.expire_all()
    assert db.get(UserSession, old.row.id) is None
    assert db.get(UserSession, live.row.id) is not None
    remaining = (
        db.execute(select(RateLimitEvent).where(RateLimitEvent.scope == "test:prune"))
        .scalars()
        .all()
    )
    assert remaining == []
