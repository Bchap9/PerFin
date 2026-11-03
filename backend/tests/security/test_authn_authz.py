# SPDX-License-Identifier: AGPL-3.0-or-later
"""Principal resolution and authorisation guards over HTTP.

Covers review finding SR-8 (an ``Authorization`` header never falls back to
the session cookie), token validation, the RLS context a request runs in,
role ordering, instance admin, scopes, step-up, the MFA-enrolment
restriction and object-level account grants.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import Annotated, Any

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import delete, select, update

from app.auth.authz import (
    Authenticated,
    account_access,
    require_instance_admin,
    require_role,
    require_scope,
    require_step_up,
)
from app.auth.deps import DbSession, require_principal
from app.auth.models import (
    ApiToken,
    GrantLevel,
    HouseholdMember,
    HouseholdRole,
    SessionStage,
    User,
    UserSession,
)
from app.auth.principal import Principal
from app.core import clock
from app.core.csrf import CSRF_HEADER
from app.core.settings import Settings
from app.db.rls import get_context, set_context
from app.main import API_PREFIX, create_app
from tests.fixtures.factories import Factories, LoggedIn, UserRecord
from tests.fixtures.ledger import LedgerFactories

pytestmark = [pytest.mark.integration, pytest.mark.security]

T = f"{API_PREFIX}/t"


def _describe(principal: Principal, db: Any) -> dict[str, Any]:
    ctx = get_context(db)
    return {
        "user_id": str(principal.user_id),
        "household_id": None if principal.household_id is None else str(principal.household_id),
        "role": None if principal.role is None else principal.role.value,
        "auth_method": principal.auth_method,
        "ctx_household": None if ctx.household_id is None else str(ctx.household_id),
        "ctx_user": None if ctx.user_id is None else str(ctx.user_id),
        "ctx_admin": ctx.instance_admin,
    }


def _role_route(role: HouseholdRole) -> Any:
    guard = require_role(role)

    def guarded(principal: Principal = Depends(guard)) -> dict[str, str]:  # noqa: B008
        return {"role": str(principal.role)}

    return guarded


def build_app(settings: Settings) -> FastAPI:
    app = create_app(settings)
    router = APIRouter()

    @router.get("/t/whoami")
    def whoami(principal: Authenticated, db: DbSession) -> dict[str, Any]:
        return _describe(principal, db)

    @router.post("/t/write")
    def write(principal: Authenticated) -> dict[str, str]:
        return {"ok": str(principal.user_id)}

    @router.get("/t/enroll")
    def enroll(principal: Annotated[Principal, Depends(require_principal)]) -> dict[str, bool]:
        return {"restricted": principal.restricted}

    for role in HouseholdRole:
        router.add_api_route(f"/t/role/{role.value}", _role_route(role), methods=["GET"])

    @router.get("/t/admin")
    def admin(principal: Annotated[Principal, Depends(require_instance_admin)]) -> dict[str, bool]:
        return {"ok": True}

    @router.get("/t/scope")
    def scoped(
        principal: Annotated[Principal, Depends(require_scope("transactions:read"))],
    ) -> dict[str, bool]:
        return {"ok": True}

    @router.get("/t/step-up")
    def step_up(principal: Annotated[Principal, Depends(require_step_up)]) -> dict[str, bool]:
        return {"ok": True}

    @router.get("/t/members")
    def members(principal: Authenticated, db: DbSession) -> list[str]:
        return [str(h) for h in db.execute(select(HouseholdMember.household_id)).scalars()]

    @router.get("/t/account/{account_id}")
    def account(
        account_id: uuid.UUID, need: str, principal: Authenticated, db: DbSession
    ) -> dict[str, bool]:
        return {
            "allowed": account_access(
                db, principal, account_id, "edit" if need == "edit" else "view"
            )
        }

    app.include_router(router, prefix=API_PREFIX)
    return app


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return build_app(settings)


@pytest.fixture
def api(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as client:
        yield client


def _user(factories: Factories, **kwargs: Any) -> UserRecord:
    kwargs.setdefault("with_totp", True)
    return factories.user(**kwargs)


def _login(factories: Factories, api: TestClient, record: UserRecord, **kwargs: Any) -> LoggedIn:
    return factories.login(api, record, **kwargs)


def _problem_type(response: Any) -> str:
    assert response.headers["content-type"] == "application/problem+json"
    return str(response.json()["type"]).removeprefix("urn:perfin:problem:")


# ---- authentication ----------------------------------------------------------------


def test_anonymous_is_401(api: TestClient) -> None:
    response = api.get(f"{T}/whoami")
    assert response.status_code == 401
    assert _problem_type(response) == "unauthenticated"


def test_session_sets_rls_context(factories: Factories, api: TestClient) -> None:
    record = _user(factories, role="admin")
    _login(factories, api, record)
    body = api.get(f"{T}/whoami").json()
    assert body == {
        "user_id": str(record.id),
        "household_id": str(record.household_id),
        "role": "admin",
        "auth_method": "session",
        "ctx_household": str(record.household_id),
        "ctx_user": str(record.id),
        "ctx_admin": False,
    }


def test_instance_admin_flag_reaches_rls_context(factories: Factories, api: TestClient) -> None:
    record = _user(factories, role="owner", is_instance_admin=True)
    _login(factories, api, record)
    assert api.get(f"{T}/whoami").json()["ctx_admin"] is True


def test_requests_only_see_their_household(factories: Factories, api: TestClient) -> None:
    mine = _user(factories, role="member")
    other = _user(factories, role="owner")
    _login(factories, api, mine)
    seen = api.get(f"{T}/members").json()
    assert str(mine.household_id) in seen
    assert str(other.household_id) not in seen


@pytest.mark.parametrize(
    "authorization",
    ["Bearer pfn_000000000000_" + "A" * 43, "Bearer bogus", "Bearer ", "Basic dXNlcjpwYXNz", ""],
)
def test_valid_cookie_plus_bad_authorization_is_401(
    factories: Factories, api: TestClient, authorization: str
) -> None:
    """SR-8: an Authorization header never falls back to the session cookie."""
    record = _user(factories)
    _login(factories, api, record)
    assert api.get(f"{T}/whoami").status_code == 200
    response = api.get(f"{T}/whoami", headers={"Authorization": authorization})
    assert response.status_code == 401
    assert _problem_type(response) == "invalid-token"
    assert response.headers["www-authenticate"].startswith("Bearer")


def test_bogus_bearer_cannot_ride_session_on_unsafe_request(
    factories: Factories, api: TestClient
) -> None:
    """The CSRF layer exempts Bearer requests; auth must then refuse the cookie."""
    record = _user(factories)
    _login(factories, api, record)
    response = api.post(
        f"{T}/write",
        headers={"Authorization": "Bearer bogus", "Origin": "http://testserver"},
    )
    assert response.status_code == 401


def test_bearer_token_authenticates(factories: Factories, api: TestClient) -> None:
    record = _user(factories, role="viewer")
    raw = factories.token(record, scopes=["transactions:read"])
    response = api.get(f"{T}/whoami", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 200
    body = response.json()
    assert body["auth_method"] == "token"
    assert body["ctx_household"] == str(record.household_id)
    assert body["role"] == "viewer"
    with factories.transaction() as db:
        set_context(db, record.household_id)
        used = db.execute(select(ApiToken.last_used_at)).scalars().all()
    assert any(value is not None for value in used)


def test_bearer_post_needs_no_csrf_token(factories: Factories, api: TestClient) -> None:
    record = _user(factories)
    raw = factories.token(record)
    response = api.post(f"{T}/write", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 200


@pytest.mark.parametrize("variant", ["expired", "revoked", "wrong-secret", "unknown"])
def test_bad_tokens_are_rejected(factories: Factories, api: TestClient, variant: str) -> None:
    record = _user(factories)
    if variant == "expired":
        raw = factories.token(record, expires_at=clock.utcnow() - timedelta(seconds=1))
    elif variant == "revoked":
        raw = factories.token(record, revoked=True)
    elif variant == "wrong-secret":
        good = factories.token(record)
        raw = good[:-4] + ("AAAA" if not good.endswith("AAAA") else "BBBB")
    else:
        raw = "pfn_0123456789ab_" + "x" * 43
    response = api.get(f"{T}/whoami", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 401


def test_token_dies_with_membership(factories: Factories, api: TestClient) -> None:
    record = _user(factories)
    raw = factories.token(record)
    with factories.transaction() as db:
        set_context(db, record.household_id)
        db.execute(delete(HouseholdMember).where(HouseholdMember.user_id == record.id))
    assert api.get(f"{T}/whoami", headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_inactive_user_cannot_authenticate(factories: Factories, api: TestClient) -> None:
    record = _user(factories)
    raw = factories.token(record)
    _login(factories, api, record)
    with factories.transaction() as db:
        db.execute(update(User).where(User.id == record.id).values(is_active=False))
    assert api.get(f"{T}/whoami").status_code == 401
    assert api.get(f"{T}/whoami", headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_revoked_session_is_401(factories: Factories, api: TestClient) -> None:
    record = _user(factories)
    login = _login(factories, api, record)
    with factories.transaction() as db:
        db.execute(
            update(UserSession)
            .where(UserSession.id == login.session_id)
            .values(revoked_at=clock.utcnow())
        )
    assert api.get(f"{T}/whoami").status_code == 401


def test_mfa_pending_session_is_not_a_principal(factories: Factories, api: TestClient) -> None:
    record = _user(factories)
    _login(factories, api, record, stage=SessionStage.MFA_PENDING)
    assert api.get(f"{T}/whoami").status_code == 401
    assert api.get(f"{T}/enroll").status_code == 401


def test_household_falls_back_to_a_current_membership(
    factories: Factories, api: TestClient
) -> None:
    record = _user(factories, role="member")
    second = factories.household()
    factories.member(second.id, record.id, HouseholdRole.VIEWER)
    _login(factories, api, record, household_id=second.id)
    assert api.get(f"{T}/whoami").json()["household_id"] == str(second.id)
    # Removed from the session's household: the next request uses the other one.
    with factories.transaction() as db:
        set_context(db, second.id)
        db.execute(delete(HouseholdMember).where(HouseholdMember.user_id == record.id))
    body = api.get(f"{T}/whoami").json()
    assert body["household_id"] == str(record.household_id)
    assert body["role"] == "member"


def test_no_membership_means_no_household(factories: Factories, api: TestClient) -> None:
    record = _user(factories, role=None)
    _login(factories, api, record)
    body = api.get(f"{T}/whoami").json()
    assert body["household_id"] is None
    assert body["ctx_household"] is None
    response = api.get(f"{T}/role/viewer")
    assert response.status_code == 403


def test_demotion_applies_on_next_request(factories: Factories, api: TestClient) -> None:
    record = _user(factories, role="owner")
    _login(factories, api, record)
    assert api.get(f"{T}/role/owner").status_code == 200
    with factories.transaction() as db:
        set_context(db, record.household_id)
        db.execute(
            update(HouseholdMember)
            .where(HouseholdMember.user_id == record.id)
            .values(role=HouseholdRole.VIEWER)
        )
    assert api.get(f"{T}/role/owner").status_code == 403
    assert api.get(f"{T}/role/viewer").status_code == 200


# ---- guards ------------------------------------------------------------------------

ROLES = ["owner", "admin", "member", "viewer"]


@pytest.mark.parametrize("actual", ROLES)
@pytest.mark.parametrize("minimum", ROLES)
def test_role_matrix(factories: Factories, api: TestClient, actual: str, minimum: str) -> None:
    record = _user(factories, role=actual)
    _login(factories, api, record)
    expected = 200 if ROLES.index(actual) <= ROLES.index(minimum) else 403
    response = api.get(f"{T}/role/{minimum}")
    assert response.status_code == expected
    if expected == 403:
        assert _problem_type(response) == "forbidden"


def test_instance_admin_guard(factories: Factories, api: TestClient) -> None:
    plain = _user(factories, role="owner")
    _login(factories, api, plain)
    assert api.get(f"{T}/admin").status_code == 403
    admin = _user(factories, role="owner", is_instance_admin=True)
    _login(factories, api, admin)
    assert api.get(f"{T}/admin").status_code == 200
    api.cookies.clear()
    raw = factories.token(admin, scopes=["admin"])
    response = api.get(f"{T}/admin", headers={"Authorization": f"Bearer {raw}"})
    assert response.status_code == 403  # instance administration needs a browser session


def test_scope_guard(factories: Factories, api: TestClient) -> None:
    record = _user(factories)
    _login(factories, api, record)
    assert api.get(f"{T}/scope").status_code == 200  # sessions hold every scope
    api.cookies.clear()
    good = factories.token(record, scopes=["transactions:read"])
    bad = factories.token(record, scopes=["documents:write"])
    assert api.get(f"{T}/scope", headers={"Authorization": f"Bearer {good}"}).status_code == 200
    response = api.get(f"{T}/scope", headers={"Authorization": f"Bearer {bad}"})
    assert response.status_code == 403
    assert _problem_type(response) == "insufficient-scope"


def test_step_up_guard(factories: Factories, api: TestClient) -> None:
    record = _user(factories)
    login = _login(factories, api, record, mfa_verified=True)
    assert api.get(f"{T}/step-up").status_code == 200
    with factories.transaction() as db:
        db.execute(
            update(UserSession)
            .where(UserSession.id == login.session_id)
            .values(mfa_verified_at=clock.utcnow() - timedelta(minutes=11))
        )
    response = api.get(f"{T}/step-up")
    assert response.status_code == 403
    assert _problem_type(response) == "step-up-required"
    api.cookies.clear()
    raw = factories.token(record)
    assert api.get(f"{T}/step-up", headers={"Authorization": f"Bearer {raw}"}).status_code == 403


def test_mfa_required_restricts_users_without_mfa(factories: Factories, api: TestClient) -> None:
    record = factories.user(role="member", with_totp=False)
    _login(factories, api, record)
    response = api.get(f"{T}/whoami")
    assert response.status_code == 403
    assert _problem_type(response) == "mfa-enrollment-required"
    assert api.get(f"{T}/enroll").json() == {"restricted": True}
    api.cookies.clear()
    raw = factories.token(record)
    assert api.get(f"{T}/whoami", headers={"Authorization": f"Bearer {raw}"}).status_code == 403


def test_idp_verified_mfa_satisfies_requirement(factories: Factories, api: TestClient) -> None:
    record = factories.user(role="member", with_totp=False)
    _login(factories, api, record, mfa_verified=True)  # e.g. OIDC amr contained mfa
    assert api.get(f"{T}/whoami").status_code == 200


def test_mfa_not_required_when_disabled(factories: Factories, settings: Settings) -> None:
    relaxed = settings.model_copy(update={"require_mfa": False})
    with TestClient(build_app(relaxed)) as client:
        record = factories.user(role="member", with_totp=False)
        factories.login(client, record)
        assert client.get(f"{T}/whoami").status_code == 200


def test_csrf_token_is_bound_to_the_session(factories: Factories, api: TestClient) -> None:
    record = _user(factories)
    login = _login(factories, api, record)
    base = {"Origin": "http://testserver", "Sec-Fetch-Site": "same-origin"}
    assert api.post(f"{T}/write", headers=base).status_code == 403
    assert api.post(f"{T}/write", headers=login.headers()).status_code == 200
    other = _user(factories)
    other_login = factories.login(TestClient(api.app), other)
    wrong = {**base, CSRF_HEADER: other_login.csrf_token}
    assert api.post(f"{T}/write", headers=wrong).status_code == 403


# ---- account grants ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "grant", "view", "edit"),
    [
        ("owner", None, True, True),
        ("admin", None, True, True),
        ("member", None, False, False),
        ("member", GrantLevel.NONE, False, False),
        ("member", GrantLevel.VIEW, True, False),
        ("member", GrantLevel.EDIT, True, True),
        ("viewer", None, False, False),
        ("viewer", GrantLevel.VIEW, True, False),
        ("viewer", GrantLevel.EDIT, True, False),
    ],
)
def test_account_access_matrix(
    factories: Factories,
    api: TestClient,
    role: str,
    grant: GrantLevel | None,
    view: bool,
    edit: bool,
) -> None:
    record = _user(factories, role=role)
    assert record.household_id is not None
    account_id = LedgerFactories(factories.session_factory).account(record.household_id).id
    if grant is not None:
        factories.grant(record.household_id, record.id, account_id, grant)
    _login(factories, api, record)
    url = f"{T}/account/{account_id}"
    assert api.get(url, params={"need": "view"}).json() == {"allowed": view}
    assert api.get(url, params={"need": "edit"}).json() == {"allowed": edit}


def test_grant_in_another_household_does_not_count(factories: Factories, api: TestClient) -> None:
    record = _user(factories, role="member")
    elsewhere = factories.household()
    factories.member(elsewhere.id, record.id, HouseholdRole.MEMBER)
    account_id = LedgerFactories(factories.session_factory).account(elsewhere.id).id
    factories.grant(elsewhere.id, record.id, account_id, GrantLevel.EDIT)
    _login(factories, api, record)  # acting in the first household
    assert api.get(f"{T}/account/{account_id}", params={"need": "view"}).json() == {
        "allowed": False
    }
