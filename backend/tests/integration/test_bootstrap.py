# SPDX-License-Identifier: AGPL-3.0-or-later
"""First-run setup: one-time token, single use, race safety (SECURITY.md A10).

Runs against its own empty database (``tests/fixtures/fresh_db.py``); the
shared test database always has users.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Engine, select, text

from app.auth import bootstrap
from app.auth.local import AuthLimits
from app.auth.models import HouseholdMember, HouseholdRole, User
from app.auth.ratelimit import Limit
from app.cli import main
from app.core import clock
from app.core.logging import scrub_text
from app.core.settings import Settings
from app.db.models import Setting
from app.db.rls import set_context
from app.db.session import make_app_engine, make_engine, make_sessionmaker
from app.domain.transactions.categories import default_category_keys
from app.domain.transactions.models import Category
from app.main import API_PREFIX
from tests.fixtures.auth_clients import browser, build_app, pre_auth_headers
from tests.fixtures.fresh_db import FreshDatabase, fresh_database, reset_identity

pytestmark = pytest.mark.integration

DEFAULT_ADMIN_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
STATUS = f"{API_PREFIX}/setup/status"
SETUP = f"{API_PREFIX}/setup"
LOGIN = f"{API_PREFIX}/auth/login"
PASSWORD = "violet harbor lighthouse keeper"

ROOMY = AuthLimits(setup_per_ip=Limit("setup:ip", 1000, timedelta(minutes=15)))


@pytest.fixture(scope="module")
def fresh() -> Iterator[FreshDatabase]:
    with fresh_database(os.environ.get("PERFIN_TEST_DATABASE_URL", DEFAULT_ADMIN_URL)) as db:
        yield db


@pytest.fixture(scope="module")
def fresh_admin(fresh: FreshDatabase) -> Iterator[Engine]:
    engine = make_engine(fresh.admin_url.render_as_string(hide_password=False))
    yield engine
    engine.dispose()


@pytest.fixture
def boot_settings(fresh: FreshDatabase, fresh_admin: Engine, tmp_path: Path) -> Settings:
    reset_identity(fresh_admin)
    return fresh.settings.model_copy(
        update={"bootstrap_url_file": tmp_path / "perfin-setup" / "setup-url"}
    )


@pytest.fixture
def app_engine_fresh(boot_settings: Settings) -> Iterator[Engine]:
    engine = make_app_engine(boot_settings)
    yield engine
    engine.dispose()


def issue(engine: Engine) -> str:
    with make_sessionmaker(engine)() as db, db.begin():
        token = bootstrap.issue_setup_token(db)
    assert token is not None
    return token


def setup_body(token: str, **overrides: Any) -> dict[str, Any]:
    body = {
        "token": token,
        "username": "Founder",
        "password": PASSWORD,
        "display_name": "Synthetic Founder",
        "household_name": "Synthetic Household",
    }
    body.update(overrides)
    return body


def post_setup(client: TestClient, body: dict[str, Any]) -> Any:
    return client.post(SETUP, json=body, headers=pre_auth_headers(client))


def count_users(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text("SELECT count(*) FROM app_user")).scalar_one())


# ---------------------------------------------------------------------------


def test_no_default_credentials(boot_settings: Settings, fresh_admin: Engine) -> None:
    app = build_app(boot_settings)
    with browser(app) as client:
        assert client.get(STATUS).json() == {"setup_required": True}
        for username, password in (("admin", "admin"), ("admin", "password"), ("root", "")):
            response = client.post(
                LOGIN,
                json={"username": username, "password": password or "x"},
                headers=pre_auth_headers(client),
            )
            assert response.status_code == 401
    assert count_users(fresh_admin) == 0


def test_announce_logs_and_writes_a_private_file(
    boot_settings: Settings, app_engine_fresh: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="perfin.bootstrap"):
        url = bootstrap.announce_with_engine(boot_settings, app_engine_fresh)
    assert url is not None
    assert url.startswith("http://testserver/setup#t=")
    token = url.split("#t=", 1)[1]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert url in warnings[0].getMessage()
    # The scrubbing log filter leaves the URL usable.
    assert scrub_text(warnings[0].getMessage()) == warnings[0].getMessage()
    path = boot_settings.bootstrap_url_file
    assert path.read_text(encoding="utf-8").strip() == url
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    # Only the hash is stored.
    with make_sessionmaker(app_engine_fresh)() as db:
        row = db.execute(select(Setting).where(Setting.key == bootstrap.TOKEN_KEY)).scalar_one()
    assert row.value["hash"] == bootstrap.hash_token(token)
    assert token not in str(row.value)


def test_setup_creates_the_admin_and_disables_itself(
    boot_settings: Settings, app_engine_fresh: Engine, fresh_admin: Engine
) -> None:
    url = bootstrap.announce_with_engine(boot_settings, app_engine_fresh)
    assert url is not None
    token = url.split("#t=", 1)[1]
    app = build_app(boot_settings, limits=ROOMY)
    with browser(app) as client:
        response = post_setup(client, setup_body(token, base_currency="eur"))
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["user"]["username"] == "founder"
        assert body["user"]["is_instance_admin"] is True
        assert body["household"] == {
            "id": body["household"]["id"],
            "name": "Synthetic Household",
            "base_currency": "EUR",
            "role": "owner",
        }
        # Logged in, restricted to MFA enrolment (PERFIN_REQUIRE_MFA).
        session = client.get(f"{API_PREFIX}/auth/session").json()
        assert session["auth_method"] == "bootstrap"
        assert session["csrf_token"] == body["csrf_token"]
        assert session["mfa"]["enrollment_required"] is True
        assert session["household"]["role"] == "owner"
        # The route is gone for good.
        assert client.get(STATUS).json() == {"setup_required": False}
        client.cookies.clear()
        again = post_setup(client, setup_body(token, username="second"))
        assert again.status_code == 404
    assert not boot_settings.bootstrap_url_file.exists()
    assert count_users(fresh_admin) == 1
    with make_sessionmaker(app_engine_fresh)() as db, db.begin():
        assert bootstrap.issue_setup_token(db) is None
        user = db.execute(select(User)).scalar_one()
        set_context(db, None, user.id)
        member = db.execute(select(HouseholdMember)).scalar_one()
        assert member.role is HouseholdRole.OWNER
        # The first household gets the default category set.
        set_context(db, member.household_id, user.id)
        keys = db.execute(select(Category.system_key)).scalars().all()
        assert sorted(k or "" for k in keys) == sorted(default_category_keys())
        set_context(db, None, user.id)
        assert (
            db.execute(
                select(Setting).where(Setting.key == bootstrap.TOKEN_KEY)
            ).scalar_one_or_none()
            is None
        )
    with fresh_admin.connect() as conn:
        actions = set(conn.execute(text("SELECT action FROM audit_log")).scalars())
    assert {"instance.bootstrap", "household.create"} <= actions


def test_setup_stays_disabled_even_if_every_user_is_deleted(
    boot_settings: Settings, app_engine_fresh: Engine, fresh_admin: Engine
) -> None:
    token = issue(app_engine_fresh)
    app = build_app(boot_settings, limits=ROOMY)
    with browser(app) as client:
        assert post_setup(client, setup_body(token)).status_code == 201
    with fresh_admin.begin() as conn:
        conn.execute(text("DELETE FROM app_user"))
    with browser(app) as client:
        assert client.get(STATUS).json() == {"setup_required": False}
        assert post_setup(client, setup_body(token)).status_code == 404
    with make_sessionmaker(app_engine_fresh)() as db, db.begin():
        assert bootstrap.issue_setup_token(db) is None


@pytest.mark.parametrize("case", ["wrong", "expired", "never-issued"])
def test_bad_tokens_are_refused(
    boot_settings: Settings, app_engine_fresh: Engine, fresh_admin: Engine, case: str
) -> None:
    token = issue(app_engine_fresh) if case != "never-issued" else bootstrap.new_setup_token()
    if case == "wrong":
        token = bootstrap.new_setup_token()
    if case == "expired":
        past = (clock.utcnow() - timedelta(seconds=1)).isoformat()
        with fresh_admin.begin() as conn:
            conn.execute(
                text(
                    "UPDATE setting SET value = "
                    "jsonb_set(value, '{expires_at}', to_jsonb(CAST(:p AS text))) WHERE key = :k"
                ),
                {"p": past, "k": bootstrap.TOKEN_KEY},
            )
    app = build_app(boot_settings, limits=ROOMY)
    with browser(app) as client:
        response = post_setup(client, setup_body(token))
    assert response.status_code == 403
    assert response.json()["type"] == "urn:perfin:problem:invalid-setup-token"
    assert count_users(fresh_admin) == 0


def test_weak_password_keeps_setup_open(
    boot_settings: Settings, app_engine_fresh: Engine, fresh_admin: Engine
) -> None:
    token = issue(app_engine_fresh)
    app = build_app(boot_settings, limits=ROOMY)
    with browser(app) as client:
        weak = post_setup(client, setup_body(token, password="administrator"))
        assert weak.status_code == 422
        assert weak.json()["reason"] == "breached"
        assert post_setup(client, setup_body(token, password="short")).status_code == 422
        bad_name = post_setup(client, setup_body(token, username="no spaces allowed"))
        assert bad_name.status_code == 422
        assert client.get(STATUS).json() == {"setup_required": True}
        assert post_setup(client, setup_body(token)).status_code == 201
    assert count_users(fresh_admin) == 1


def test_concurrent_setup_creates_exactly_one_admin(
    boot_settings: Settings, app_engine_fresh: Engine, fresh_admin: Engine
) -> None:
    token = issue(app_engine_fresh)
    app = build_app(boot_settings, limits=ROOMY)
    racers = 4
    barrier = threading.Barrier(racers)
    results: list[int] = []
    lock = threading.Lock()

    def race(index: int) -> None:
        with browser(app) as client:
            headers = pre_auth_headers(client)
            barrier.wait()
            response = client.post(
                SETUP, json=setup_body(token, username=f"racer{index}"), headers=headers
            )
            with lock:
                results.append(response.status_code)

    threads = [threading.Thread(target=race, args=(i,)) for i in range(racers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert sorted(results) == [201] + [404] * (racers - 1)
    assert count_users(fresh_admin) == 1


def test_setup_is_rate_limited(boot_settings: Settings, app_engine_fresh: Engine) -> None:
    issue(app_engine_fresh)
    limits = AuthLimits(setup_per_ip=Limit("setup:ip", 2, timedelta(minutes=15)))
    app = build_app(boot_settings, limits=limits)
    with browser(app) as client:
        codes = [
            post_setup(client, setup_body(bootstrap.new_setup_token())).status_code
            for _ in range(3)
        ]
    assert codes == [403, 403, 429]


def test_cli_bootstrap_url(
    boot_settings: Settings,
    app_engine_fresh: Engine,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("PERFIN_DATABASE_URL", boot_settings.database_url.get_secret_value())
    monkeypatch.setenv("PERFIN_PUBLIC_URL", "https://perfin.example.test")
    monkeypatch.setenv("PERFIN_BOOTSTRAP_URL_FILE", str(boot_settings.bootstrap_url_file))
    assert main(["bootstrap-url"]) == 0
    first = capsys.readouterr().out.strip()
    assert first.startswith("https://perfin.example.test/setup#t=")
    assert main(["bootstrap-url"]) == 0
    second = capsys.readouterr().out.strip()
    assert second != first
    assert boot_settings.bootstrap_url_file.read_text(encoding="utf-8").strip() == second
    app = build_app(boot_settings, limits=ROOMY)
    with browser(app) as client:
        # Regenerating replaced the first token.
        old = post_setup(client, setup_body(first.split("#t=", 1)[1]))
        assert old.status_code == 403
        assert post_setup(client, setup_body(second.split("#t=", 1)[1])).status_code == 201
    assert main(["bootstrap-url"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "already complete" in captured.err
    assert not boot_settings.bootstrap_url_file.exists()


def _secret(value: SecretStr | None) -> str:
    assert value is not None
    return value.get_secret_value()


@pytest.fixture
def restore_root_logging() -> Iterator[None]:
    """``perfin api`` installs the JSON root handler; put the old ones back."""
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    yield
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)


@pytest.mark.usefixtures("restore_root_logging")
def test_perfin_api_prints_the_setup_url_on_first_boot(
    boot_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``perfin api`` with no user: the one-time URL is in the process output
    (the JSON log on stderr) and in the URL file, and it works; once setup is
    done the next start prints nothing and removes the file."""
    import re

    import uvicorn

    from app.core import metrics
    from app.core.settings import get_settings

    for name in list(os.environ):
        if name.startswith("PERFIN_"):
            monkeypatch.delenv(name)
    env = {
        "PERFIN_DEV_MODE": "true",
        "PERFIN_DATABASE_URL": boot_settings.database_url.get_secret_value(),
        "PERFIN_MASTER_KEY": _secret(boot_settings.master_key),
        "PERFIN_SECRET_KEY": _secret(boot_settings.secret_key),
        "PERFIN_PUBLIC_URL": "https://perfin.example.test",
        "PERFIN_BOOTSTRAP_URL_FILE": str(boot_settings.bootstrap_url_file),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    served: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda *_a, **kwargs: served.append(kwargs))
    monkeypatch.setattr(metrics, "start_metrics_server", lambda *_a, **_k: None)

    get_settings.cache_clear()
    assert main(["api"]) == 0
    assert len(served) == 1  # announced, then served
    output = capsys.readouterr()
    urls = re.findall(r"https://perfin\.example\.test/setup#t=[A-Za-z]+", output.out + output.err)
    assert len(urls) == 1, output
    (url,) = urls
    assert boot_settings.bootstrap_url_file.read_text(encoding="utf-8").strip() == url
    app = build_app(boot_settings, limits=ROOMY)
    with browser(app) as client:
        assert post_setup(client, setup_body(url.split("#t=", 1)[1])).status_code == 201

    # Second start: setup is complete, nothing is announced, the file is gone.
    get_settings.cache_clear()
    assert main(["api"]) == 0
    assert len(served) == 2
    output = capsys.readouterr()
    assert "/setup#t=" not in output.out + output.err
    assert not boot_settings.bootstrap_url_file.exists()


@pytest.mark.usefixtures("restore_root_logging")
def test_perfin_api_prints_the_setup_url_even_when_warnings_are_off(
    boot_settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``PERFIN_LOG_LEVEL=ERROR`` hides the WARNING line; the URL is printed once anyway."""
    import uvicorn

    from app.core import metrics
    from app.core.settings import get_settings

    for name in list(os.environ):
        if name.startswith("PERFIN_"):
            monkeypatch.delenv(name)
    env = {
        "PERFIN_DEV_MODE": "true",
        "PERFIN_LOG_LEVEL": "ERROR",
        "PERFIN_DATABASE_URL": boot_settings.database_url.get_secret_value(),
        "PERFIN_MASTER_KEY": _secret(boot_settings.master_key),
        "PERFIN_SECRET_KEY": _secret(boot_settings.secret_key),
        "PERFIN_PUBLIC_URL": "https://perfin.example.test",
        "PERFIN_BOOTSTRAP_URL_FILE": str(boot_settings.bootstrap_url_file),
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(uvicorn, "run", lambda *_a, **_k: None)
    monkeypatch.setattr(metrics, "start_metrics_server", lambda *_a, **_k: None)
    get_settings.cache_clear()
    assert main(["api"]) == 0
    output = capsys.readouterr()
    url = boot_settings.bootstrap_url_file.read_text(encoding="utf-8").strip()
    assert url.startswith("https://perfin.example.test/setup#t=")
    assert (output.out + output.err).count(url) == 1
