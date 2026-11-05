# SPDX-License-Identifier: AGPL-3.0-or-later
"""AI access settings end to end (AI_ACCESS.md "Client management"; E3, M2, M4, M5).

Over HTTP as the household owner on a stepped-up browser session: creating a
client returns the ``ai:read`` token once (expiry mandatory, hashed at rest),
revoking it ends access at once, the exposure preview renders every tool
with chosen options from the household's real data without storing or
sending anything, the call audit is searchable, the scrubber preview shows
original next to scrubbed free text, the instance kill switch and the
household flag stop every AI view, and ``GET /auth/session`` carries the
banner flag while a client is live.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.auth.models import ApiToken
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.ai.models import AiCall, AiClient
from app.main import create_app
from tests.fixtures.ai import ai_client, ai_headers
from tests.fixtures.factories import Factories, LoggedIn
from tests.fixtures.ledger import LedgerFactories
from tests.security.ai.test_ai_views import World, build_world, leaks

pytestmark = [pytest.mark.security, pytest.mark.integration]

API = "/api/v1"
AI = f"{API}/ai/v1"
SETTINGS = f"{API}/settings/ai"

#: Every tool of AI_ACCESS.md "Tools"; the preview must render each of them.
ALL_TOOLS = {
    "explain_data_policy",
    "get_overview",
    "get_spending",
    "get_income",
    "get_trends",
    "get_budget_status",
    "get_savings_goals",
    "get_recurring",
    "get_net_worth_history",
    "get_allocation",
    "list_accounts",
    "list_transactions",
    "find_anomalies",
    "list_holdings",
    "get_performance",
    "list_loans",
    "get_loan_schedule",
    "simulate_loan",
    "get_tax_year_summary",
}


@pytest.fixture
def world(factories: Factories, ledger: LedgerFactories) -> World:
    return build_world(factories, ledger)


@pytest.fixture
def app_client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        yield client


@pytest.fixture
def owner(app_client: TestClient, factories: Factories, world: World) -> LoggedIn:
    return factories.login(app_client, world.owner, mfa_verified=True)


def _type(response: Any) -> str:
    return str(response.json()["type"]).rsplit(":", 1)[-1]


def _counts(factories: Factories, world: World) -> tuple[int, int, int]:
    with factories.transaction() as db:
        set_context(db, world.household_id)
        clients = db.execute(
            select(func.count())
            .select_from(AiClient)
            .where(AiClient.household_id == world.household_id)
        ).scalar_one()
        calls = db.execute(
            select(func.count())
            .select_from(AiCall)
            .where(AiCall.household_id == world.household_id)
        ).scalar_one()
        tokens = db.execute(
            select(func.count())
            .select_from(ApiToken)
            .where(ApiToken.household_id == world.household_id)
        ).scalar_one()
    return int(clients), int(calls), int(tokens)


# ---------------------------------------------------------------------------
# Create, list, revoke
# ---------------------------------------------------------------------------


def test_create_returns_the_token_once_hashed_and_expiring(
    app_client: TestClient, factories: Factories, world: World, owner: LoggedIn
) -> None:
    body = {"name": "Desktop assistant", "mode": "detail", "custom_denylist": ["Oakwood"]}
    # Expiry is mandatory.
    missing = app_client.post(f"{SETTINGS}/clients", json=body, headers=owner.headers())
    assert missing.status_code == 422
    too_long = app_client.post(
        f"{SETTINGS}/clients", json={**body, "expires_in_days": 9999}, headers=owner.headers()
    )
    assert too_long.status_code == 422

    created = app_client.post(
        f"{SETTINGS}/clients", json={**body, "expires_in_days": 30}, headers=owner.headers()
    )
    assert created.status_code == 201, created.text
    out = created.json()
    raw = out["token"]
    assert raw.startswith("pfn_")
    assert out["status"] == "active"
    assert raw.startswith(f"pfn_{out['token_prefix']}_")

    with factories.transaction() as db:
        set_context(db, world.household_id)
        token = db.execute(
            select(ApiToken).join(AiClient, AiClient.token_id == ApiToken.id)
        ).scalar_one()
        assert token.scopes == ["ai:read"]
        assert token.token_hash != raw
        assert raw not in token.token_hash
        assert token.expires_at is not None
        days = (token.expires_at - token.created_at).days
        assert 29 <= days <= 30

    # The token is never shown again.
    listed = app_client.get(f"{SETTINGS}/clients")
    assert listed.status_code == 200
    assert raw not in listed.text
    assert "token_hash" not in listed.text
    assert [c["id"] for c in listed.json()] == [out["id"]]
    one = app_client.get(f"{SETTINGS}/clients/{out['id']}")
    assert "token" not in one.json()
    assert raw not in one.text
    assert one.json()["custom_denylist"] == ["Oakwood"]

    # It reads the AI views, and only them.
    assert app_client.get(f"{AI}/policy", headers=ai_headers(raw)).status_code == 200
    confined = app_client.get(f"{SETTINGS}/clients", headers=ai_headers(raw))
    assert _type(confined) == "ai-token-confined"


def test_revocation_is_immediate(
    app_client: TestClient, factories: Factories, world: World, owner: LoggedIn
) -> None:
    created = app_client.post(
        f"{SETTINGS}/clients",
        json={"name": "To revoke", "expires_in_days": 7},
        headers=owner.headers(),
    ).json()
    raw = created["token"]
    assert app_client.get(f"{AI}/spending", headers=ai_headers(raw)).status_code == 200
    gone = app_client.delete(f"{SETTINGS}/clients/{created['id']}", headers=owner.headers())
    assert gone.status_code == 204
    # The very next call is refused (a cached answer is not served either).
    assert app_client.get(f"{AI}/spending", headers=ai_headers(raw)).status_code == 401
    again = app_client.delete(f"{SETTINGS}/clients/{created['id']}", headers=owner.headers())
    assert again.status_code == 204
    assert app_client.get(f"{SETTINGS}/clients/{created['id']}").json()["status"] == "revoked"


def test_create_and_revoke_need_a_fresh_step_up(
    app_client: TestClient, factories: Factories, world: World
) -> None:
    client, _ = ai_client(factories, world.owner)
    login = factories.login(app_client, world.owner, mfa_verified=False)
    body = {"name": "x", "expires_in_days": 1}
    created = app_client.post(f"{SETTINGS}/clients", json=body, headers=login.headers())
    assert _type(created) == "step-up-required"
    revoked = app_client.delete(f"{SETTINGS}/clients/{client.id}", headers=login.headers())
    assert _type(revoked) == "step-up-required"
    # Reading is fine without step-up.
    assert app_client.get(f"{SETTINGS}/clients").status_code == 200


def test_another_households_client_is_not_found(
    app_client: TestClient, factories: Factories, world: World, owner: LoggedIn
) -> None:
    other = factories.user(role="owner", with_totp=True)
    foreign, _ = ai_client(factories, other)
    for method, path in (
        ("GET", f"{SETTINGS}/clients/{foreign.id}"),
        ("GET", f"{SETTINGS}/clients/{foreign.id}/preview"),
        ("DELETE", f"{SETTINGS}/clients/{foreign.id}"),
        ("GET", f"{SETTINGS}/calls?client_id={foreign.id}"),
        ("GET", f"{SETTINGS}/scrubber-preview?client_id={foreign.id}"),
    ):
        response = app_client.request(method, path, headers=owner.headers())
        assert response.status_code == 404, (method, path)
        assert _type(response) == "ai-client-not-found"


# ---------------------------------------------------------------------------
# Exposure preview
# ---------------------------------------------------------------------------


def test_preview_renders_every_tool_from_real_data_and_stores_nothing(
    app_client: TestClient, factories: Factories, world: World, owner: LoggedIn
) -> None:
    before = _counts(factories, world)
    response = app_client.post(
        f"{SETTINGS}/preview",
        json={"custom_denylist": ["Oakwood Clinic"], "hide_merchant_location": True},
        headers=owner.headers(),
    )
    assert response.status_code == 200, response.text
    out = response.json()
    tools = {t["tool"]: t for t in out["tools"]}
    assert set(tools) == ALL_TOOLS
    assert all(t["status"] == 200 for t in tools.values()), {
        k: t["body"] for k, t in tools.items() if t["status"] != 200
    }
    # Real data, through the same serialisers and scrubber, nothing planted leaks.
    assert tools["list_transactions"]["body"]["items"]
    assert leaks(response.text, world.usernames) == []
    # Placeholders are marked by path for highlighting.
    marks = [m for t in tools.values() for m in t["placeholders"]]
    assert marks
    for tool in tools.values():
        for mark in tool["placeholders"]:
            node: Any = tool["body"]
            for part in mark["path"].replace("[", ".[").split("."):
                node = node[int(part[1:-1])] if part.startswith("[") else node[part]
            assert all(p in node for p in mark["placeholders"])
    for placeholder in ("[MEMBER]", "[ACCT]", "[REDACTED]"):
        assert out["placeholder_counts"][placeholder] > 0, placeholder
    # The body is the one the AI view would send.
    assert "STARBUCKS #1234" in response.text
    assert "SEATTLE" not in response.text
    # Nothing is stored: no client, no token, no ai_call row.
    assert _counts(factories, world) == before


def test_preview_applies_the_chosen_options(
    app_client: TestClient, factories: Factories, world: World, owner: LoggedIn
) -> None:
    out = app_client.post(
        f"{SETTINGS}/preview",
        json={"mode": "aggregates", "rounding": 100},
        headers=owner.headers(),
    ).json()
    tools = {t["tool"]: t for t in out["tools"]}
    assert out["mode"] == "aggregates"
    for name in ("list_transactions", "list_loans", "list_holdings", "get_recurring"):
        assert tools[name]["status"] == 403
        assert tools[name]["body"]["type"].endswith("not-in-aggregates-mode")
    assert tools["get_spending"]["status"] == 200
    assert tools["explain_data_policy"]["body"]["rounding"] == 100

    bad = app_client.post(f"{SETTINGS}/preview", json={"rounding": 5}, headers=owner.headers())
    assert bad.status_code == 422

    # A saved client's preview uses its saved options and leaves it untouched.
    client, _ = ai_client(factories, world.owner, hide_nicknames=True)
    saved = app_client.get(f"{SETTINGS}/clients/{client.id}/preview")
    assert saved.status_code == 200
    labels = [a["label"] for a in _tool(saved.json(), "list_accounts")["items"]]
    assert labels
    assert all(" " in label and label.split()[-1].isalpha() for label in labels)
    assert "everyday" not in saved.text
    assert _counts(factories, world)[1] == 0


def _tool(out: dict[str, Any], name: str) -> Any:
    return next(t["body"] for t in out["tools"] if t["tool"] == name)


# ---------------------------------------------------------------------------
# Call audit
# ---------------------------------------------------------------------------


def test_call_audit_is_searchable(
    app_client: TestClient, factories: Factories, world: World, owner: LoggedIn
) -> None:
    client, raw = ai_client(factories, world.owner)
    _, raw2 = ai_client(factories, world.owner)
    assert app_client.get(f"{AI}/transactions", headers=ai_headers(raw)).status_code == 200
    assert app_client.get(f"{AI}/policy", headers=ai_headers(raw2)).status_code == 200

    everything = app_client.get(f"{SETTINGS}/calls").json()["items"]
    assert {c["tool"] for c in everything} == {"list_transactions", "explain_data_policy"}
    mine = app_client.get(f"{SETTINGS}/calls", params={"client_id": str(client.id)}).json()
    assert [c["tool"] for c in mine["items"]] == ["list_transactions"]
    row = mine["items"][0]
    assert row["bytes"] > 0
    assert row["status"] == 200
    assert row["response"]["items"]

    # Confirm something never left / did leave as a placeholder.
    for needle in ("Jane", "5678", "jane.doe@example.org", "%", "%Jane%"):
        found = app_client.get(f"{SETTINGS}/calls", params={"contains": needle}).json()
        assert found["items"] == [], needle
    hits = app_client.get(
        f"{SETTINGS}/calls", params={"contains": "[member]", "client_id": str(client.id)}
    ).json()
    assert [c["tool"] for c in hits["items"]] == ["list_transactions"]
    # The audit is not reachable with the AI token.
    assert _type(app_client.get(f"{SETTINGS}/calls", headers=ai_headers(raw))) == (
        "ai-token-confined"
    )


# ---------------------------------------------------------------------------
# Scrubber preview
# ---------------------------------------------------------------------------


def test_scrubber_preview_shows_original_and_scrubbed(
    app_client: TestClient, factories: Factories, world: World, owner: LoggedIn
) -> None:
    out = app_client.get(f"{SETTINGS}/scrubber-preview").json()
    by_original = {s["original"]: s for s in out["samples"]}
    memo = by_original["ZELLE TO JANE DOE ACCT ...5678"]
    assert memo["source"] == "transaction.memo"
    assert memo["changed"] is True
    assert "[MEMBER]" in memo["scrubbed"]
    assert "[ACCT]" in memo["scrubbed"]
    assert "JANE" not in memo["scrubbed"]
    assert "5678" not in memo["scrubbed"]
    assert by_original["Oakwood Clinic"]["changed"] is False  # not on any denylist yet
    assert out["changed"] == sum(1 for s in out["samples"] if s["changed"])

    # Try a denylist term before saving it, with ad-hoc text.
    tried = app_client.post(
        f"{SETTINGS}/scrubber-preview",
        json={"custom_denylist": ["Oakwood"], "text": "Visit Oakwood today, call 555-123-4567"},
        headers=owner.headers(),
    ).json()
    first = tried["samples"][0]
    assert first["source"] == "text"
    assert first["scrubbed"] == "Visit [REDACTED] today, call [PHONE]"
    assert first["placeholders"] == ["[PHONE]", "[REDACTED]"]
    clinic = next(s for s in tried["samples"] if s["original"] == "Oakwood Clinic")
    assert clinic["scrubbed"] == "[REDACTED] Clinic"

    # A saved client's terms.
    client, raw = ai_client(factories, world.owner, member_aliases=["Bobby"])
    saved = app_client.post(
        f"{SETTINGS}/scrubber-preview",
        json={"client_id": str(client.id), "text": "lunch with Bobby"},
        headers=owner.headers(),
    ).json()
    assert saved["samples"][0]["scrubbed"] == "lunch with [MEMBER]"
    # Never to an AI token.
    assert _type(app_client.get(f"{SETTINGS}/scrubber-preview", headers=ai_headers(raw))) == (
        "ai-token-confined"
    )


# ---------------------------------------------------------------------------
# Switches and the banner flag
# ---------------------------------------------------------------------------


def test_household_flag_stops_every_ai_view(
    app_client: TestClient, factories: Factories, world: World, owner: LoggedIn
) -> None:
    _, raw = ai_client(factories, world.owner)
    assert app_client.get(SETTINGS).json() == {
        "enabled": True,
        "household_enabled": True,
        "system_enabled": True,
        "active_clients": 1,
    }
    off = app_client.patch(SETTINGS, json={"enabled": False}, headers=owner.headers())
    assert off.status_code == 200
    assert off.json()["enabled"] is False
    for path in ("/policy", "/accounts", "/spending", "/transactions"):
        response = app_client.get(AI + path, headers=ai_headers(raw))
        assert response.status_code == 403, path
        assert _type(response) == "ai-access-disabled"
    refused = app_client.post(
        f"{SETTINGS}/clients", json={"name": "x", "expires_in_days": 1}, headers=owner.headers()
    )
    assert _type(refused) == "ai-access-disabled"
    # Another household is unaffected.
    other = factories.user(role="owner", with_totp=True)
    _, other_raw = ai_client(factories, other)
    assert app_client.get(f"{AI}/policy", headers=ai_headers(other_raw)).status_code == 200

    on = app_client.patch(SETTINGS, json={"enabled": True}, headers=owner.headers())
    assert on.json()["enabled"] is True
    assert app_client.get(f"{AI}/policy", headers=ai_headers(raw)).status_code == 200


def test_instance_kill_switch_stops_every_household(
    app_client: TestClient, factories: Factories, world: World
) -> None:
    _, raw = ai_client(factories, world.owner)
    other = factories.user(role="owner", with_totp=True)
    _, other_raw = ai_client(factories, other)
    admin_client = TestClient(app_client.app, base_url="https://testserver")
    admin = factories.user(role=None, is_instance_admin=True, with_totp=True)
    login = factories.login(admin_client, admin, mfa_verified=True)
    assert admin_client.get(f"{API}/settings/system/ai").json() == {"enabled": True}
    try:
        off = admin_client.patch(
            f"{API}/settings/system/ai", json={"enabled": False}, headers=login.headers()
        )
        assert off.status_code == 200
        assert off.json() == {"enabled": False}
        for token in (raw, other_raw):
            response = app_client.get(f"{AI}/policy", headers=ai_headers(token))
            assert response.status_code == 403
            assert _type(response) == "ai-access-disabled"
            assert response.json()["scope"] == "instance"
        owner = factories.login(app_client, world.owner, mfa_verified=True)
        state = app_client.get(SETTINGS).json()
        assert state["system_enabled"] is False
        assert state["enabled"] is False
        refused = app_client.post(
            f"{SETTINGS}/clients",
            json={"name": "x", "expires_in_days": 1},
            headers=owner.headers(),
        )
        assert _type(refused) == "ai-access-disabled"
    finally:
        restored = admin_client.patch(
            f"{API}/settings/system/ai", json={"enabled": True}, headers=login.headers()
        )
        assert restored.json() == {"enabled": True}
    assert app_client.get(f"{AI}/policy", headers=ai_headers(raw)).status_code == 200


def test_session_carries_the_banner_flag(
    app_client: TestClient, factories: Factories, world: World, owner: LoggedIn
) -> None:
    assert app_client.get(f"{API}/auth/session").json()["ai_clients_active"] is False
    created = app_client.post(
        f"{SETTINGS}/clients",
        json={"name": "Banner", "expires_in_days": 3},
        headers=owner.headers(),
    ).json()
    assert app_client.get(f"{API}/auth/session").json()["ai_clients_active"] is True
    # Every member of the household sees it, not only the owner.
    assert world.owner.household is not None
    member = factories.user(role="viewer", household=world.owner.household)
    viewer_client = TestClient(app_client.app, base_url="https://testserver")
    factories.login(viewer_client, member)
    assert viewer_client.get(f"{API}/auth/session").json()["ai_clients_active"] is True
    app_client.delete(f"{SETTINGS}/clients/{created['id']}", headers=owner.headers())
    assert app_client.get(f"{API}/auth/session").json()["ai_clients_active"] is False


def test_token_hash_is_sha256_of_the_raw_token(
    app_client: TestClient, factories: Factories, world: World, owner: LoggedIn
) -> None:
    raw = app_client.post(
        f"{SETTINGS}/clients", json={"name": "Hash", "expires_in_days": 1}, headers=owner.headers()
    ).json()["token"]
    with factories.transaction() as db:
        set_context(db, world.household_id)
        hashes = db.execute(select(ApiToken.token_hash)).scalars().all()
    assert raw not in hashes
    assert hashlib.sha256(raw.encode()).hexdigest() in hashes
