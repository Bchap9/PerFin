# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin api`` / ``perfin worker`` refuse a privileged database role (H3).

Found by the v1.0.0 verification run (docs/RELEASE_CHECKLIST.md): with
``PERFIN_DATABASE_URL`` pointing at the schema owner, the API started and
served normally, so row-level security was one ``ALTER TABLE`` away from
being off. These tests run the real startup helper against the per-session
test database as the app role (accepted), the owner role and the bootstrap
superuser (both refused), and an unreachable database (refused: fail closed).
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

from app import cli
from app.core.settings import Settings
from app.db.role_check import privileged_role_problems
from tests.conftest import TestDatabase


def _with_url(settings: Settings, url: str, *, dev_mode: bool = False) -> Settings:
    return settings.model_copy(update={"database_url": SecretStr(url), "dev_mode": dev_mode})


def _url(database: TestDatabase, which: str) -> str:
    url = database.owner_url if which == "owner" else database.app_url
    return url.render_as_string(hide_password=False)


def test_app_role_has_no_privilege(app_engine: Engine) -> None:
    assert privileged_role_problems(app_engine) == []


def test_owner_role_is_reported(owner_engine: Engine) -> None:
    problems = privileged_role_problems(owner_engine)
    assert any("owns" in p for p in problems)


def test_superuser_is_reported(admin_engine: Engine) -> None:
    assert "is a superuser" in privileged_role_problems(admin_engine)


def test_startup_accepts_the_app_role(settings: Settings) -> None:
    assert cli._database_role_ok(settings, "api") is True


@pytest.mark.parametrize("command", ["api", "worker"])
def test_startup_refuses_the_owner_role(
    settings: Settings,
    test_database: TestDatabase,
    command: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    owner = _with_url(settings, _url(test_database, "owner"))
    assert cli._database_role_ok(owner, command) is False
    err = capsys.readouterr().err
    assert f"perfin {command}: ERROR" in err
    assert "refusing to start" in err
    # No credential in the message.
    password = test_database.owner_url.password
    assert password
    assert str(password) not in err


def test_startup_refuses_the_superuser(settings: Settings, admin_engine: Engine) -> None:
    superuser = _with_url(settings, admin_engine.url.render_as_string(hide_password=False))
    assert cli._database_role_ok(superuser, "api") is False


def test_dev_mode_only_warns(
    settings: Settings, test_database: TestDatabase, capsys: pytest.CaptureFixture[str]
) -> None:
    owner = _with_url(settings, _url(test_database, "owner"), dev_mode=True)
    assert cli._database_role_ok(owner, "api") is True
    assert "WARNING" in capsys.readouterr().err


def test_unreachable_database_fails_closed(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    down = _with_url(settings, "postgresql+psycopg://nobody:x@127.0.0.1:1/none")
    assert cli._database_role_ok(down, "worker") is False
    assert "could not check the database role" in capsys.readouterr().err


def test_cmd_api_stops_before_serving_as_owner(
    settings: Settings, test_database: TestDatabase, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = _with_url(settings, _url(test_database, "owner"))
    monkeypatch.setattr("app.core.settings.get_settings", lambda: owner)
    served: list[bool] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: served.append(True))
    monkeypatch.setattr("app.core.metrics.start_metrics_server", lambda _s: None)
    assert cli.cmd_api(None) == cli.EXIT_FAIL  # type: ignore[arg-type]
    assert served == []
