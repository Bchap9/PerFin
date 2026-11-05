# SPDX-License-Identifier: AGPL-3.0-or-later
"""The dev seed (``scripts/seed_dev.py``, docs/DEVELOPMENT.md "Dev stack").

Runs against its own empty database (the seed completes first-run setup,
which needs "no user exists"): the household it builds is read back through
the API as the dev admin, the second run changes nothing, and the seed
refuses outside dev mode, next to other users and after an unfinished run.
"""

from __future__ import annotations

import importlib.util
import os
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

import app.workers.handlers.ingest as ingest
import app.workers.handlers.tax as tax_handlers
from app.core.settings import Settings
from app.db.session import make_app_engine, make_engine, make_sessionmaker
from app.main import API_PREFIX, create_app
from tests.fixtures.auth_clients import browser, pre_auth_headers
from tests.fixtures.factories import make_user
from tests.fixtures.fresh_db import FreshDatabase, fresh_database, reset_identity

pytestmark = pytest.mark.integration

DEFAULT_ADMIN_URL = "postgresql+psycopg://postgres:postgres@localhost:5432/postgres"
SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "seed_dev.py"
API = API_PREFIX


def load_seed() -> ModuleType:
    spec = importlib.util.spec_from_file_location("seed_dev_script", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["seed_dev_script"] = module
    spec.loader.exec_module(module)
    return module


@dataclass
class Seeded:
    module: Any
    settings: Settings
    admin: Engine
    result: Any


@pytest.fixture(scope="module")
def fresh() -> Iterator[FreshDatabase]:
    with fresh_database(os.environ.get("PERFIN_TEST_DATABASE_URL", DEFAULT_ADMIN_URL)) as db:
        yield db


@pytest.fixture(scope="module")
def seeded(fresh: FreshDatabase, tmp_path_factory: pytest.TempPathFactory) -> Iterator[Seeded]:
    root = tmp_path_factory.mktemp("seed")
    settings = fresh.settings.model_copy(
        update={
            "documents_dir": root / "documents",
            "quarantine_dir": root / "quarantine",
            "bootstrap_url_file": root / "setup-url",
        }
    )
    admin = make_engine(fresh.admin_url.render_as_string(hide_password=False))
    module = load_seed()
    patch = pytest.MonkeyPatch()
    # The ingestion handlers read process settings, as the worker does.
    patch.setattr(ingest, "get_settings", lambda: settings)
    patch.setattr(tax_handlers, "get_settings", lambda: settings)
    ingest.default_deps.cache_clear()
    try:
        result = module.seed_dev(settings, log=lambda _line: None)
        yield Seeded(module=module, settings=settings, admin=admin, result=result)
    finally:
        ingest.default_deps.cache_clear()
        patch.undo()
        admin.dispose()


@pytest.fixture
def admin_client(seeded: Seeded) -> Iterator[TestClient]:
    """The dev admin, logged in with the printed credentials (the real login)."""
    result = seeded.result
    settings = seeded.settings.model_copy(update={"require_mfa": False})
    with browser(create_app(settings)) as client:
        response = client.post(
            f"{API}/auth/login",
            json={"username": result.username, "password": result.password},
            headers=pre_auth_headers(client),
        )
        assert response.status_code == 200, response.text
        yield client


def items(client: TestClient, path: str) -> list[dict[str, object]]:
    response = client.get(f"{API}{path}", params={"limit": 200})
    assert response.status_code == 200, response.text
    body = response.json()
    rows: list[dict[str, object]] = body["items"] if isinstance(body, dict) else body
    return rows


def test_seed_creates_credentials_printed_once(seeded: Seeded) -> None:
    result = seeded.result
    assert result.created is True
    password = result.password
    assert result.username == "dev-admin"
    assert isinstance(password, str)
    assert len(password) >= 20
    # Only a hash is stored.
    with seeded.admin.connect() as conn:
        stored = conn.execute(text("SELECT password_hash FROM credential")).scalars().all()
        sessions = conn.execute(text("SELECT count(*) FROM user_session")).scalar_one()
    assert len(stored) == 1
    assert password not in stored[0]
    assert stored[0].startswith("$argon2id$")
    # The seeding session was ended (a login after the seed makes the only live one).
    assert sessions == 1


def test_household_has_every_account_type(admin_client: TestClient) -> None:
    accounts = items(admin_client, "/accounts")
    assert {a["type"] for a in accounts} == {
        "checking",
        "savings",
        "credit_card",
        "cash",
        "brokerage",
        "retirement",
        "loan",
        "mortgage",
        "line_of_credit",
        "manual_asset",
        "manual_liability",
    }


def test_ledger_budgets_goals_rules(admin_client: TestClient) -> None:
    transactions = admin_client.get(f"{API}/transactions", params={"limit": 200}).json()
    assert len(transactions["items"]) == 200
    assert transactions["next_cursor"] is not None
    assert len(items(admin_client, "/budgets")) == 1
    assert len(items(admin_client, "/goals")) == 2
    assert len(items(admin_client, "/rules")) >= 3
    spending = admin_client.get(f"{API}/reports/spending")
    assert spending.status_code == 200, spending.text


def test_investments_loans_assets_and_net_worth(admin_client: TestClient) -> None:
    holdings = admin_client.get(f"{API}/investments/holdings")
    assert holdings.status_code == 200, holdings.text
    assert len(holdings.json()["holdings"]) >= 3
    loans = items(admin_client, "/loans")
    assert len(loans) == 2
    schedule = admin_client.get(f"{API}/loans/{loans[0]['id']}/schedule")
    assert schedule.status_code == 200, schedule.text
    assert len(items(admin_client, "/assets")) == 2
    history = admin_client.get(f"{API}/networth", params={"range": "1y"})
    assert history.status_code == 200, history.text
    assert len(history.json()["points"]) >= 6
    latest = admin_client.get(f"{API}/networth/latest").json()
    assert Decimal(latest["assets"]["amount"]) > 0
    assert Decimal(latest["liabilities"]["amount"]) != 0


def test_tax_year_documents_and_review_queue(admin_client: TestClient) -> None:
    years = items(admin_client, "/tax-years")
    assert len(years) == 1
    year = years[0]["year"]
    forms = admin_client.get(f"{API}/tax-years/{year}/forms")
    assert forms.status_code == 200, forms.text
    form_rows = forms.json()["items"] if isinstance(forms.json(), dict) else forms.json()
    assert len(form_rows) >= 3
    documents = items(admin_client, "/documents")
    assert len(documents) >= 7
    assert {d["status"] for d in documents} <= {"processed", "needs_review", "failed"}
    review = items(admin_client, "/review")
    assert review, "the review queue has items"


def test_ai_client_example_is_disabled(admin_client: TestClient) -> None:
    clients = items(admin_client, "/settings/ai/clients")
    assert len(clients) == 1
    assert clients[0]["revoked_at"] is not None
    assert "token" not in clients[0]
    settings = admin_client.get(f"{API}/settings/ai").json()
    assert settings["active_clients"] == 0


def test_second_run_changes_nothing(seeded: Seeded) -> None:
    def snapshot() -> list[int]:
        with seeded.admin.connect() as conn:
            return [
                int(conn.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())  # noqa: S608
                for table in ("app_user", "account", "transaction", "document", "app_user")
            ]

    before = snapshot()
    again = seeded.module.seed_dev(seeded.settings, log=lambda _line: None)
    assert again.created is False
    assert again.password is None
    assert again.notes
    assert "already seeded" in again.notes[0]
    assert snapshot() == before


def test_refuses_outside_dev_mode(seeded: Seeded) -> None:
    off = seeded.settings.model_copy(update={"dev_mode": False})
    with pytest.raises(seeded.module.SeedRefusedError, match="PERFIN_DEV_MODE"):
        seeded.module.seed_dev(off, log=lambda _line: None)


def test_main_exits_2_without_dev_mode(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    off = seeded.settings.model_copy(update={"dev_mode": False})
    monkeypatch.setattr(seeded.module, "get_settings", lambda: off)
    assert seeded.module.main([]) == 2
    err = capsys.readouterr().err
    assert "refused" in err
    assert "PERFIN_DEV_MODE" in err


@pytest.fixture(scope="module")
def other() -> Iterator[tuple[FreshDatabase, Engine]]:
    """A second empty database, for the refusals (the seeded one stays untouched)."""
    with fresh_database(os.environ.get("PERFIN_TEST_DATABASE_URL", DEFAULT_ADMIN_URL)) as db:
        admin = make_engine(db.admin_url.render_as_string(hide_password=False))
        try:
            yield db, admin
        finally:
            admin.dispose()


def test_unfinished_seed_is_detected(
    seeded: Seeded, other: tuple[FreshDatabase, Engine], tmp_path: Path
) -> None:
    db, admin = other
    reset_identity(admin)
    settings = db.settings.model_copy(update={"bootstrap_url_file": tmp_path / "setup-url"})
    engine = make_app_engine(settings)
    try:
        # The admin exists but the last step (the dev-seed tag) never ran.
        with make_sessionmaker(engine)() as session, session.begin():
            seeded.module._create_admin(session, settings)
    finally:
        engine.dispose()
    with pytest.raises(seeded.module.SeedRefusedError, match="did not finish"):
        seeded.module.seed_dev(settings, log=lambda _line: None)


def test_refuses_next_to_other_users(seeded: Seeded, other: tuple[FreshDatabase, Engine]) -> None:
    # Any user besides the dev admin makes the database "real": the seed leaves it alone.
    db, admin = other
    reset_identity(admin)
    engine = make_app_engine(db.settings)
    try:
        with make_sessionmaker(engine)() as session, session.begin():
            make_user(session, username="someone-real")
    finally:
        engine.dispose()
    with pytest.raises(seeded.module.SeedRefusedError, match="users other than the dev admin"):
        seeded.module.seed_dev(db.settings, log=lambda _line: None)
    with admin.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM app_user")).scalar_one() == 1
        assert conn.execute(text("SELECT count(*) FROM account")).scalar_one() == 0
