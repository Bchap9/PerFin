# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin breakglass status|enable|disable`` (SECURITY.md B7).

The bootstrap instance admin cannot change their own account through
``PATCH /admin/users``, so the local operator flags the break-glass account
with this command. The change is audited and ends every session of the
account; ``status`` is the database-side check that ``perfin config check``
cannot do.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import select

from app.auth.models import ApiToken, UserSession
from app.cli import main
from app.core.settings import Settings, get_settings
from app.db.rls import set_context
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, instance_audit

pytestmark = [pytest.mark.integration, pytest.mark.security]


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


@pytest.fixture
def cli_env(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """The CLI reads the same database as the test app, through the environment."""
    assert settings.master_key is not None
    assert settings.secret_key is not None
    monkeypatch.setenv("PERFIN_DEV_MODE", "true")
    monkeypatch.setenv("PERFIN_DATABASE_URL", settings.database_url.get_secret_value())
    monkeypatch.setenv("PERFIN_MASTER_KEY", settings.master_key.get_secret_value())
    monkeypatch.setenv("PERFIN_SECRET_KEY", settings.secret_key.get_secret_value())
    monkeypatch.setenv("PERFIN_PUBLIC_URL", settings.public_url)
    monkeypatch.delenv("PERFIN_BREAKGLASS_ADMIN_USERNAME", raising=False)
    return monkeypatch


def _live_sessions(factories: Factories, user_id: uuid.UUID) -> int:
    with factories.transaction() as db:
        rows = db.execute(
            select(UserSession.id).where(
                UserSession.user_id == user_id, UserSession.revoked_at.is_(None)
            )
        ).all()
        return len(rows)


def _live_tokens(factories: Factories, owner: AuthedClient) -> int:
    household_id = owner.record.household_id
    assert household_id is not None
    with factories.transaction() as db:
        set_context(db, household_id, owner.user.id)
        rows = db.execute(
            select(ApiToken.id).where(
                ApiToken.user_id == owner.user.id, ApiToken.revoked_at.is_(None)
            )
        ).all()
        return len(rows)


def _flag(factories: Factories, user_id: uuid.UUID) -> bool:
    from app.auth.models import User

    with factories.transaction() as db:
        user = db.get(User, user_id)
        assert user is not None
        return user.is_breakglass


def _status(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict[str, object]]:
    code = main(["breakglass", "status", *argv])
    report = json.loads(capsys.readouterr().out)
    assert isinstance(report, dict)
    return code, report


def test_enable_flags_audits_and_ends_every_session(
    actors: Actors,
    factories: Factories,
    cli_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    admin = actors.new(role="owner", is_instance_admin=True)
    assert admin.get(f"{API}/auth/session").status_code == 200
    assert _live_sessions(factories, admin.user.id) == 1
    raw = factories.token(admin.record, scopes=["households:read"])
    bearer = {"Authorization": f"Bearer {raw}"}
    assert actors.anonymous().get(f"{API}/households", headers=bearer).status_code == 200

    assert main(["breakglass", "enable", admin.user.username]) == 0
    out = capsys.readouterr()
    assert "1 session(s) ended" in out.out
    assert out.err == ""
    assert _flag(factories, admin.user.id)
    # The session predates the flag, so it was not a break-glass login and
    # would escape the per-request network check: it must be gone.
    assert _live_sessions(factories, admin.user.id) == 0
    assert admin.get(f"{API}/auth/session").status_code == 401
    # A token carries no network check at all (review P1-SR-01): revoked.
    assert _live_tokens(factories, admin) == 0
    assert actors.anonymous().get(f"{API}/households", headers=bearer).status_code == 401
    rows = instance_audit(factories, "user.breakglass_set", admin.user.id)
    assert len(rows) == 1
    assert rows[0].user_id is None  # the local operator, not a PerFin user
    assert rows[0].before == {"is_breakglass": False}
    assert rows[0].after == {
        "is_breakglass": True,
        "via": "cli",
        "sessions_revoked": 1,
        "pats_revoked": 1,
    }

    # Idempotent: no second audit row.
    assert main(["breakglass", "enable", admin.user.username]) == 0
    assert "already set" in capsys.readouterr().out
    assert len(instance_audit(factories, "user.breakglass_set", admin.user.id)) == 1

    assert main(["breakglass", "disable", admin.user.username]) == 0
    capsys.readouterr()
    assert not _flag(factories, admin.user.id)
    rows = instance_audit(factories, "user.breakglass_set", admin.user.id)
    assert rows[-1].after == {
        "is_breakglass": False,
        "via": "cli",
        "sessions_revoked": 0,
        "pats_revoked": 0,
    }
    # Clearing the flag does not bring the revoked token back.
    assert actors.anonymous().get(f"{API}/households", headers=bearer).status_code == 401


def test_enable_refuses_accounts_without_a_local_password(
    actors: Actors,
    factories: Factories,
    cli_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    oidc_only = actors.user(role=None, with_password=False)
    assert main(["breakglass", "enable", oidc_only.user.username]) == 1
    assert "local password" in capsys.readouterr().err
    assert not _flag(factories, oidc_only.id)
    assert instance_audit(factories, "user.breakglass_set", oidc_only.id) == []

    assert main(["breakglass", "enable", "no-such-user-" + uuid.uuid4().hex[:8]]) == 1
    assert "no account named" in capsys.readouterr().err


def test_enable_warns_about_a_weak_break_glass_account(
    actors: Actors,
    factories: Factories,
    cli_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    plain = actors.user(role=None, with_totp=False)
    assert main(["breakglass", "enable", plain.user.username]) == 0
    err = capsys.readouterr().err
    assert "no second factor" in err
    assert "not an instance admin" in err


def test_status_checks_the_configured_account(
    actors: Actors,
    factories: Factories,
    cli_env: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["breakglass", "status"]) == 1
    assert "no username" in capsys.readouterr().err

    admin = actors.user(role="owner", is_instance_admin=True)
    cli_env.setenv("PERFIN_BREAKGLASS_ADMIN_USERNAME", admin.user.username)
    get_settings.cache_clear()  # one process, one environment, in production
    code, report = _status(capsys)
    assert code == 1
    assert report["problems"] == ["not-flagged"]
    assert report["configured"] is True

    assert main(["breakglass", "enable", admin.user.username]) == 0
    capsys.readouterr()
    code, report = _status(capsys)
    assert (code, report["problems"]) == (0, [])
    assert report["mfa_methods"] == ["totp"]
    assert report["breakglass_cidrs"] == ["127.0.0.1/32"]

    other = actors.user(role=None, with_totp=False)
    code, report = _status(capsys, "--username", other.user.username)
    assert code == 1
    assert report["problems"] == [
        "not-flagged",
        "not-instance-admin",
        "no-second-factor",
        "not-the-configured-account",
    ]

    code, report = _status(capsys, "--username", "missing-" + uuid.uuid4().hex[:8])
    assert (code, report["exists"], report["problems"]) == (1, False, ["account-not-found"])
