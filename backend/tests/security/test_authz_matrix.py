# SPDX-License-Identifier: AGPL-3.0-or-later
"""Authorisation matrix over every route of the app (SECURITY.md F3, I2, I3, A5, A6).

* :func:`test_every_route_has_a_policy` walks ``app.routes`` (so routes added
  in later phases are seen without editing this file) and fails for a route
  without an entry in ``tests/security/authz_policy.py`` (and for entries
  whose route is gone).
* :func:`test_route_matrix` sends every route a request as each principal of
  ``authz_policy.PRINCIPALS`` (anonymous, viewer, member, admin, owner,
  instance admin, owner of another household, MFA-restricted and
  MFA-pending sessions, a session whose step-up is stale, personal access
  tokens with no scope / the route's scope / expired, and a valid session
  cookie plus a junk bearer token) and compares the answer with what the
  policy table says. Path parameters are real objects built by the
  factories; objects that belong to a user are the caller's own, except for
  the owner of the other household, who is handed the victim's and must get
  ``404`` (never ``403`` or ``200``). Requests naming the matrix household
  are ``404`` for everyone who is not a member of it.
* :func:`test_foreign_objects_are_404` hands the objects of one user to
  another (same household and other household) and checks they are ``404``
  and untouched.
* The CSRF matrix: every state-changing route with a valid session cookie is
  refused (``403 csrf-failed``) without ``X-CSRF-Token``, with the token of
  another session, with the pre-auth token, and with a cross-site
  ``Origin`` or ``Sec-Fetch-Site``.

"Allowed" means the request got past authorisation: no ``401``, no
authorisation ``403``/``404`` problem (``authz_policy.DENIAL_TYPES``), no
``429`` and no ``500``. Bodies are mostly empty, so an allowed caller often ends in
``422``: the guards are dependencies and run before body validation.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from starlette.routing import BaseRoute, Mount, Route

from app.auth.models import (
    ApiToken,
    GrantLevel,
    MfaMethod,
    MfaType,
    SessionStage,
    UserSession,
)
from app.auth.scopes import GRANTABLE_SCOPES
from app.auth.sessions import cookie_name
from app.core import clock
from app.core.csrf import CSRF_HEADER, DEFAULT_EXEMPT_PATHS, SAFE_METHODS, CSRFProtect
from app.core.errors import PROBLEM_MEDIA_TYPE, PROBLEM_TYPE_PREFIX
from app.core.settings import Settings
from app.db.models import Household
from app.db.queue import enqueue
from app.db.rls import set_context
from app.db.session import make_sessionmaker
from app.domain.accounts.models import Account
from app.domain.assets.models import ManualAsset
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.goals.models import Goal
from app.domain.investments.models import Security
from app.domain.investments.service import replay_position
from app.domain.loans.models import Loan
from app.domain.rules.schemas import RUN_RULES_JOB_TYPE, RunRulesJob
from app.domain.tax.models import TaxYear
from tests.fixtures.ai import ai_client
from tests.fixtures.auth_clients import SAME_ORIGIN, random_ip
from tests.fixtures.documents import make_document, make_review_item
from tests.fixtures.factories import (
    Factories,
    LoggedIn,
    UserRecord,
    grant_account,
    make_api_token,
    make_household,
    make_user,
)
from tests.fixtures.imports import make_extracted_document, make_import_mapping
from tests.fixtures.ledger import (
    household_context,
    make_account,
    make_budget,
    make_category,
    make_fx_rate,
    make_goal,
    make_goal_contribution,
    make_payee,
    make_rule,
    make_scheduled_transaction,
    make_tag,
    make_transaction,
)
from tests.fixtures.portability import make_import_record, make_ready_export
from tests.fixtures.tax import free_tax_year, make_checklist_item, make_tax_export, make_tax_year
from tests.fixtures.wealth import (
    make_asset_valuation,
    make_brokerage,
    make_investment_txn,
    make_loan,
    make_loan_payment,
    make_loan_rate_change,
    make_manual_asset,
    make_price,
    make_security,
)
from tests.security.authz_policy import (
    DENIAL_TYPES,
    OWN_KINDS,
    PARAM_KINDS,
    POLICIES,
    PRINCIPALS,
    Outcome,
    Policy,
    expected,
    policy_problems,
)

pytestmark = [pytest.mark.integration, pytest.mark.security]

_PARAM = re.compile(r"\{([^}:]+)(?::[^}]*)?\}")
ASSET = "matrix.js"
HOW_TO = (
    "Classify it in backend/tests/security/authz_policy.py (POLICIES); "
    "see docs/DEVELOPMENT.md, 'Tests'."
)


# ---------------------------------------------------------------------------
# Route enumeration
# ---------------------------------------------------------------------------


def _flatten(routes: list[BaseRoute]) -> Iterator[tuple[str, BaseRoute]]:
    """``(full path, route)`` for every route, through included routers.

    FastAPI >= 0.16x keeps included routers as nested route objects that
    expose ``effective_route_contexts()``; older versions flatten them into
    ``app.routes``. Both are handled.
    """
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            for context in contexts():
                yield str(context.path), context.original_route
            continue
        yield str(getattr(route, "path", "")), route


def route_keys(app: FastAPI) -> set[str]:
    """``"METHOD path"`` for every route of ``app`` (``"MOUNT path"`` for mounts)."""
    keys: set[str] = set()
    for path, route in _flatten(list(app.routes)):
        if isinstance(route, Mount):
            keys.add(f"MOUNT {path}")
        elif isinstance(route, Route) or hasattr(route, "methods"):
            methods = set(getattr(route, "methods", None) or ())
            if "GET" in methods:
                methods.discard("HEAD")  # Starlette adds HEAD to every GET route
            if not methods:
                keys.add(f"UNKNOWN {path} ({type(route).__name__})")
            keys.update(f"{method} {path}" for method in methods)
        else:
            keys.add(f"UNKNOWN {path} ({type(route).__name__})")
    return keys


def _template(key: str) -> tuple[str, str]:
    method, _, path = key.partition(" ")
    return method, path


def path_params(key: str) -> set[str]:
    return set(_PARAM.findall(_template(key)[1]))


ROUTES = sorted(POLICIES)
#: The only paths allowed to skip CSRF, hard-coded here on purpose: the matrix
#: below must not shrink silently when a path is added to the production
#: ``app.core.csrf.DEFAULT_EXEMPT_PATHS`` (review P1-SR-04). A new exemption
#: needs a deliberate change to this set, and
#: ``test_csrf_exemptions_are_pinned`` fails until it is made.
EXPECTED_CSRF_EXEMPT: frozenset[str] = frozenset({"/api/v1/auth/oidc/backchannel-logout"})
UNSAFE = [
    key
    for key in ROUTES
    if _template(key)[0] not in SAFE_METHODS | {"MOUNT"}
    and _template(key)[1] not in EXPECTED_CSRF_EXEMPT
]
OWN_OBJECT_ROUTES = [
    key for key in ROUTES if any(kind in OWN_KINDS for kind in POLICIES[key].params.values())
]


# ---------------------------------------------------------------------------
# Fixtures: one app (with the SPA mounted, so its mount is enumerated too)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def matrix_settings(settings: Settings, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    static: Path = tmp_path_factory.mktemp("matrix-static")
    (static / "index.html").write_text("<!doctype html><title>PerFin</title>\n")
    (static / "static").mkdir()
    (static / "static" / ASSET).write_text("export {};\n")
    blobs: Path = tmp_path_factory.mktemp("matrix-blobs")
    return settings.model_copy(
        update={
            "static_dir": static,
            # Documents the matrix downloads live in a real (temporary) store.
            "documents_dir": blobs / "documents",
            "quarantine_dir": blobs / "quarantine",
            # GET /events streams until this lifetime ends; keep it short.
            "sse_max_stream_seconds": 0.2,
        }
    )


@pytest.fixture(scope="module")
def matrix_app(matrix_settings: Settings) -> Iterator[FastAPI]:
    from app.main import create_app

    app = create_app(matrix_settings)
    yield app
    app.state.engine.dispose()


@pytest.fixture(scope="module")
def mfactories(app_engine: Any, matrix_settings: Settings) -> Factories:
    return Factories(make_sessionmaker(app_engine), matrix_settings)


def new_client(app: FastAPI) -> TestClient:
    """An https client from its own address (per-IP limits never interfere).

    Not entered as a context manager: that would run the app's lifespan,
    which disposes the shared engine on exit.
    """
    return TestClient(app, base_url="https://testserver", client=(random_ip(), 50000))


# ---------------------------------------------------------------------------
# The world one route is tried against
# ---------------------------------------------------------------------------


@dataclass
class World:
    household: Household
    users: dict[str, UserRecord]

    @property
    def victim(self) -> UserRecord:
        """The matrix household's owner: whose objects outsiders are handed."""
        return self.users["owner"]


def build_world(factories: Factories) -> World:
    """One household with a user per role, plus the outsiders, in one transaction."""
    keyring = factories.keyring
    with factories.transaction() as db:
        home = make_household(db)

        def user(role: str | None, *, household: Household | None = None, **kw: Any) -> UserRecord:
            kw.setdefault("with_totp", True)
            return make_user(db, keyring=keyring, role=role, household=household, **kw)

        users = {
            "owner": user("owner", household=home),
            "admin": user("admin", household=home),
            "member": user("member", household=home),
            "viewer": user("viewer", household=home),
            "stale": user("owner", household=home, is_instance_admin=True),
            "restricted": user("owner", household=home, with_totp=False),
            "mfa_pending": user("member", household=home),
            "instance_admin": user(None, is_instance_admin=True),
            "other_owner": user("owner"),  # owner of a household of their own
        }
    return World(home, users)


@dataclass
class Caller:
    """A client set up as one principal, plus who owns the caller's objects."""

    who: str
    client: TestClient
    headers: dict[str, str] = field(default_factory=dict)
    unsafe_headers: dict[str, str] = field(default_factory=dict)
    login: LoggedIn | None = None
    record: UserRecord | None = None


def _bogus_bearer() -> str:
    return f"pfn_{secrets.token_hex(6)}_{secrets.token_urlsafe(32)}"


def token_scopes(policy: Policy) -> tuple[str, ...]:
    """The scopes of the ``pat_scope`` principal's token for ``policy``."""
    return (policy.scope,) if policy.scope else tuple(sorted(GRANTABLE_SCOPES))


def make_caller(
    app: FastAPI, factories: Factories, world: World, who: str, policy: Policy
) -> Caller:
    client = new_client(app)
    owner = world.users["owner"]
    if who == "anonymous":
        response = client.get("/api/v1/auth/session")
        assert response.status_code == 401, response.text
        token = response.json()["csrf_token"]
        return Caller(who, client, unsafe_headers={**SAME_ORIGIN, CSRF_HEADER: token})
    if who == "ai_token":
        _, raw = ai_client(factories, owner)
        bearer = {"Authorization": f"Bearer {raw}"}
        return Caller(who, client, headers=bearer, unsafe_headers=bearer, record=owner)
    if who in ("pat_none", "pat_scope", "pat_expired"):
        scopes: tuple[str, ...] = ()
        expires_at = None
        if who != "pat_none":
            scopes = token_scopes(policy)
        if who == "pat_expired":
            expires_at = clock.utcnow() - timedelta(minutes=1)
        raw = factories.token(owner, scopes=scopes, expires_at=expires_at)
        bearer = {"Authorization": f"Bearer {raw}"}
        return Caller(who, client, headers=bearer, unsafe_headers=bearer, record=owner)
    if who == "cookie_bogus_bearer":
        login = factories.login(client, owner)
        bearer = {"Authorization": f"Bearer {_bogus_bearer()}"}
        return Caller(
            who,
            client,
            headers=bearer,
            unsafe_headers=login.headers(bearer),
            login=login,
            record=owner,
        )
    record = world.users[who]
    kwargs: dict[str, Any] = {}
    if who == "stale":
        kwargs = {"mfa_verified": True, "now": clock.utcnow() - timedelta(minutes=11)}
    elif who == "mfa_pending":
        kwargs = {"mfa_verified": False, "stage": SessionStage.MFA_PENDING}
    login = factories.login(client, record, **kwargs)
    return Caller(who, client, unsafe_headers=login.headers(), login=login, record=record)


# ---------------------------------------------------------------------------
# Path parameters from real objects
# ---------------------------------------------------------------------------


def object_owner(world: World, caller: Caller, kind: str) -> UserRecord:
    """Whose object an ``own_*`` parameter refers to for ``caller``.

    The caller's own when they can own one; the victim's for outsiders
    (the other household's owner), anonymous callers and tokens.
    """
    record = caller.record
    if caller.who in ("other_owner", "restricted") or record is None:
        return world.victim
    if kind == "own_token" and record.household is None:
        return world.victim  # no household, no tokens
    return record


def build_object(
    factories: Factories,
    world: World,
    kind: str,
    owner: UserRecord,
    built: Mapping[str, str] | None = None,
) -> str:
    """Create a fresh object of ``kind``; return its id for the path.

    ``built`` holds the objects already built for earlier parameters of the
    same path (kind -> id), so a nested object (a goal's contribution) can
    belong to its parent.
    """
    if kind == "household":
        return str(world.household.id)
    if kind == "member":
        return str(factories.user(role="member", household=world.household).id)
    if kind == "user":
        return str(factories.user(role=None).id)
    if kind == "own_session":
        return str(factories.session(owner).row.id)
    if kind == "own_token":
        household_id = owner.household_id
        assert household_id is not None
        with factories.transaction() as db:
            row, _raw = make_api_token(db, owner.id, household_id)
            return str(row.id)
    if kind == "own_webauthn":
        with factories.transaction() as db:
            method = MfaMethod(
                user_id=owner.id,
                type=MfaType.WEBAUTHN,
                name="Matrix key",
                credential_id=secrets.token_bytes(32),
                public_key=secrets.token_bytes(77),
            )
            db.add(method)
            db.flush()
            return str(method.id)
    if kind in ("transaction", "account"):
        return str(make_ledger_object(factories, world, kind).id)
    if kind in ("category", "payee", "tag"):
        return str(make_taxonomy_object(factories, world, kind).id)
    if kind == "rule":
        with factories.transaction() as db:
            return str(make_rule(db, world.household.id).id)
    if kind in ("budget", "goal", "goal_contribution"):
        return str(make_budget_object(factories, world, kind, built or {}).id)
    if kind in ("document", "import_mapping"):
        return str(make_import_object(factories, world, kind).id)
    if kind in ("stored_document", "review_item"):
        return str(make_stored_document_object(factories, world, kind).id)
    if kind in ("security", "investment_txn", "price"):
        return str(make_investment_object(factories, world, kind, built or {}).id)
    if kind in ("loan", "loan_payment", "loan_rate_change", "manual_asset", "asset_valuation"):
        return str(make_loan_or_asset_object(factories, world, kind, built or {}).id)
    if kind == "fx_rate":
        # A fresh day each time, so every caller deletes its own rate.
        day = date(2000, 1, 1) + timedelta(days=secrets.randbelow(9000))
        with factories.transaction() as db:
            return str(make_fx_rate(db, world.household.id, rate_date=day).id)
    if kind == "scheduled_transaction":
        household_id = world.household.id
        with factories.transaction() as db:
            account = make_account(db, household_id)
            for role, level in (("member", GrantLevel.EDIT), ("viewer", GrantLevel.VIEW)):
                grant_account(db, household_id, world.users[role].id, account.id, level)
            return str(make_scheduled_transaction(db, account).id)
    if kind == "ai_client":
        # Always the matrix household's owner: the owner of another household
        # is handed this one and must get 404.
        client, _raw = ai_client(factories, world.users["owner"])
        return str(client.id)
    if kind in ("export_archive", "import_archive"):
        return str(make_portability_object(factories, world, kind, owner).job_id)
    if kind in ("tax_year", "tax_checklist_item", "tax_export"):
        return make_tax_object(factories, world, kind, owner, built or {})
    if kind == "job":
        household_id = world.household.id
        with factories.transaction() as db, household_context(db, household_id):
            return str(enqueue(db, RUN_RULES_JOB_TYPE, RunRulesJob(), household_id=household_id))
    raise AssertionError(f"no generator for parameter kind {kind!r}")


def make_investment_object(
    factories: Factories, world: World, kind: str, built: Mapping[str, str]
) -> Any:
    """A security without activity, a buy of a new one on a fresh brokerage
    account (member ``edit``, viewer ``view``; lots replayed), or a price of
    the security built for the same path."""
    household_id = world.household.id
    with factories.transaction() as db:
        if kind == "security":
            return make_security(db, household_id)
        if kind == "price":
            security_id = uuid.UUID(built["security"]) if "security" in built else None
            if security_id is None:
                security = make_security(db, household_id)
            else:
                with household_context(db, household_id):
                    found = db.get(Security, security_id)
                assert found is not None
                security = found
            return make_price(db, security)
        if kind == "investment_txn":
            account = make_brokerage(db, household_id)
            grant_account(db, household_id, world.users["member"].id, account.id, GrantLevel.EDIT)
            grant_account(db, household_id, world.users["viewer"].id, account.id, GrantLevel.VIEW)
            security = make_security(db, household_id)
            row = make_investment_txn(db, account, security=security, amount="-1000")
            with household_context(db, household_id):
                replay_position(db, account, security.id)
            return row
    raise AssertionError(f"no investment generator for {kind!r}")


def make_loan_or_asset_object(
    factories: Factories, world: World, kind: str, built: Mapping[str, str]
) -> Any:
    """A loan on a fresh mortgage account or a manual asset on a fresh property
    account (member ``edit``, viewer ``view``), or a payment / rate change of
    the path's loan, a valuation of the path's asset."""
    household_id = world.household.id
    with factories.transaction() as db:
        if kind in ("loan", "loan_payment", "loan_rate_change"):
            if "loan" in built:
                with household_context(db, household_id):
                    found = db.get(Loan, uuid.UUID(built["loan"]))
                assert found is not None
                loan = found
            else:
                loan = make_loan(db, household_id)
                for role, level in (("member", GrantLevel.EDIT), ("viewer", GrantLevel.VIEW)):
                    grant_account(db, household_id, world.users[role].id, loan.account_id, level)
            if kind == "loan":
                return loan
            if kind == "loan_payment":
                return make_loan_payment(db, loan)
            day = date(2025, 1, 1) + timedelta(days=secrets.randbelow(9000))
            return make_loan_rate_change(db, loan, effective_date=day, new_rate="0.05")
        if "manual_asset" in built:
            with household_context(db, household_id):
                found_asset = db.get(ManualAsset, uuid.UUID(built["manual_asset"]))
            assert found_asset is not None
            asset = found_asset
        else:
            asset = make_manual_asset(db, household_id)
            for role, level in (("member", GrantLevel.EDIT), ("viewer", GrantLevel.VIEW)):
                grant_account(db, household_id, world.users[role].id, asset.account_id, level)
        if kind == "manual_asset":
            return asset
        with household_context(db, household_id):
            account = db.get(Account, asset.account_id)
        assert account is not None
        day = date(2000, 1, 1) + timedelta(days=secrets.randbelow(9000))
        return make_asset_valuation(db, account, valued_on=day)


def make_ledger_object(factories: Factories, world: World, kind: str) -> Any:
    """A ledger object of the matrix household on a fresh account.

    The matrix member gets ``edit`` and the viewer ``view`` on the account,
    so what they may do is decided by the route's role, not by a missing
    grant (grant-specific outcomes are tested per domain).
    """
    household_id = world.household.id
    with factories.transaction() as db:
        account = make_account(db, household_id)
        grant_account(db, household_id, world.users["member"].id, account.id, GrantLevel.EDIT)
        grant_account(db, household_id, world.users["viewer"].id, account.id, GrantLevel.VIEW)
        if kind == "transaction":
            return make_transaction(db, account, amount="-25.00")
        if kind == "account":
            return account
    raise AssertionError(f"no ledger generator for {kind!r}")


def make_budget_object(
    factories: Factories, world: World, kind: str, built: Mapping[str, str]
) -> Any:
    """A budget (one line), a goal on a fresh account (member ``edit``, viewer
    ``view``) or a contribution to the goal built for the same path."""
    household_id = world.household.id
    with factories.transaction() as db:
        if kind == "budget":
            category = make_category(db, household_id)
            return make_budget(db, household_id, lines={category.id: "100.00"})
        if kind == "goal":
            account = make_account(db, household_id)
            grant_account(db, household_id, world.users["member"].id, account.id, GrantLevel.EDIT)
            grant_account(db, household_id, world.users["viewer"].id, account.id, GrantLevel.VIEW)
            return make_goal(db, household_id, accounts=[account])
        if kind == "goal_contribution":
            goal_id = uuid.UUID(built["goal"]) if "goal" in built else None
            if goal_id is None:
                goal = make_goal(db, household_id)
            else:
                with household_context(db, household_id):
                    found = db.get(Goal, goal_id)
                assert found is not None
                goal = found
            return make_goal_contribution(db, goal)
    raise AssertionError(f"no budget generator for {kind!r}")


def make_import_object(factories: Factories, world: World, kind: str) -> Any:
    """A document with an extracted table, or a column mapping, on a fresh
    account (member ``edit``, viewer ``view``)."""
    household_id = world.household.id
    with factories.transaction() as db:
        account = make_account(db, household_id)
        grant_account(db, household_id, world.users["member"].id, account.id, GrantLevel.EDIT)
        grant_account(db, household_id, world.users["viewer"].id, account.id, GrantLevel.VIEW)
        if kind == "document":
            return make_extracted_document(db, household_id, account=account)
        if kind == "import_mapping":
            return make_import_mapping(db, household_id, account=account)
    raise AssertionError(f"no import generator for {kind!r}")


def make_stored_document_object(factories: Factories, world: World, kind: str) -> Any:
    """A processed document whose ciphertext is in the matrix blob store, on a
    fresh account (member ``edit``, viewer ``view``), or an open review item
    of such a document."""
    household_id = world.household.id
    store = LocalFSBlobStore.from_settings(factories.settings)
    with factories.transaction() as db:
        account = make_account(db, household_id)
        grant_account(db, household_id, world.users["member"].id, account.id, GrantLevel.EDIT)
        grant_account(db, household_id, world.users["viewer"].id, account.id, GrantLevel.VIEW)
        document = make_document(
            db,
            store,
            factories.keyring,
            household_id,
            account_id=account.id,
            uploaded_by=world.users["owner"].id,
        )
        if kind == "stored_document":
            return document
        if kind == "review_item":
            return make_review_item(db, document)
    raise AssertionError(f"no document generator for {kind!r}")


def make_portability_object(
    factories: Factories, world: World, kind: str, owner: UserRecord
) -> Any:
    """A ready export (bytes in the matrix blob store) or a finished import of
    ``owner`` in the matrix household (the requester is the only one who may
    see it, so the generator makes the caller the requester)."""
    household_id = world.household.id
    store = LocalFSBlobStore.from_settings(factories.settings)
    with factories.transaction() as db:
        if kind == "export_archive":
            return make_ready_export(db, store, factories.keyring, household_id, owner.id)
        if kind == "import_archive":
            return make_import_record(db, household_id, owner.id)
    raise AssertionError(f"no portability generator for {kind!r}")


def make_tax_object(
    factories: Factories, world: World, kind: str, owner: UserRecord, built: Mapping[str, str]
) -> str:
    """A tax year of the matrix household (a random year no other path uses
    yet), a checklist item of the path's year, or a ready accountant export of
    the path's year requested by ``owner``. Returns the path value."""
    household_id = world.household.id
    with factories.transaction() as db:
        if kind == "tax_year":
            return str(make_tax_year(db, household_id, year=free_tax_year(db, household_id)).year)
        year = int(built["tax_year"]) if "tax_year" in built else None
        if year is None:
            year = make_tax_year(db, household_id, year=free_tax_year(db, household_id)).year
        with household_context(db, household_id):
            tax_year = db.execute(
                select(TaxYear).where(TaxYear.household_id == household_id, TaxYear.year == year)
            ).scalar_one()
        if kind == "tax_checklist_item":
            return str(make_checklist_item(db, tax_year).id)
        if kind == "tax_export":
            store = LocalFSBlobStore.from_settings(factories.settings)
            return str(make_tax_export(db, store, factories.keyring, tax_year, owner.id).job_id)
    raise AssertionError(f"no tax generator for {kind!r}")


def make_taxonomy_object(factories: Factories, world: World, kind: str) -> Any:
    """A household-wide category, payee or tag of the matrix household."""
    household_id = world.household.id
    with factories.transaction() as db:
        if kind == "category":
            return make_category(db, household_id)
        if kind == "payee":
            return make_payee(db, household_id)
        if kind == "tag":
            return make_tag(db, household_id)
    raise AssertionError(f"no taxonomy generator for {kind!r}")


def _owner_for(world: World, caller: Caller) -> Callable[[str], UserRecord]:
    def owner_of(kind: str) -> UserRecord:
        return object_owner(world, caller, kind)

    return owner_of


def resolve_url(
    key: str,
    policy: Policy,
    factories: Factories,
    world: World,
    owner_of: Callable[[str], UserRecord],
) -> str:
    method, path = _template(key)
    if method == "MOUNT":
        return f"{path}/{ASSET}"

    built: dict[str, str] = {}

    def fill(match: re.Match[str]) -> str:
        kind = policy.params[match.group(1)]
        built[kind] = build_object(factories, world, kind, owner_of(kind), built)
        return built[kind]

    return _PARAM.sub(fill, path)


def resolve_body(policy: Policy, world: World) -> Any:
    if policy.body is None:
        return {}
    filled: dict[str, Any] = {}
    for name, value in policy.body.items():
        filled[name] = (
            value.replace("{household_id}", str(world.household.id))
            if isinstance(value, str)
            else value
        )
    return filled


def send(caller: Caller, key: str, url: str, body: Any) -> Any:
    method, _ = _template(key)
    if method == "MOUNT":
        method = "GET"
    kwargs: dict[str, Any] = {"follow_redirects": False}
    if method in SAFE_METHODS:
        kwargs["headers"] = caller.headers
    else:
        kwargs["headers"] = caller.unsafe_headers
        kwargs["json"] = body
    return caller.client.request(method, url, **kwargs)


def problem(response: Any) -> str | None:
    if response.headers.get("content-type") != PROBLEM_MEDIA_TYPE:
        return None
    return str(response.json().get("type", "")).removeprefix(PROBLEM_TYPE_PREFIX)


def is_denial(status: int, ptype: str | None) -> bool:
    if status == 401:
        return True
    return ptype is not None and ptype in DENIAL_TYPES.get(status, frozenset())


def mismatch(outcome: Outcome, response: Any) -> str | None:
    status, ptype = response.status_code, problem(response)
    if outcome.allowed:
        # 500 is an unhandled error; 503 is a legitimate answer (/readyz on
        # an unmigrated test database).
        if status == 500 or status == 429 or is_denial(status, ptype):
            return f"expected allowed, got {status} {ptype}"
        return None
    if status != outcome.status or ptype not in outcome.types:
        return f"expected {outcome}, got {status} {ptype}"
    return None


def session_alive(app: FastAPI, settings: Settings, login: LoggedIn) -> bool:
    client = new_client(app)
    client.cookies.set(cookie_name(settings), login.raw_id)
    response = client.get("/api/v1/auth/session")
    client.close()
    # Same CSRF token = same session id: not revoked, not rotated.
    return response.status_code == 200 and response.json()["csrf_token"] == login.csrf_token


# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------


def test_policy_table_is_consistent() -> None:
    assert policy_problems() == []
    assert set(PRINCIPALS) >= {
        "anonymous",
        "viewer",
        "member",
        "admin",
        "owner",
        "instance_admin",
        "other_owner",
        "pat_none",
        "pat_scope",
        "pat_expired",
        "ai_token",
    }


def test_every_route_has_a_policy(matrix_app: FastAPI) -> None:
    live = route_keys(matrix_app)
    missing = sorted(live - set(POLICIES))
    stale = sorted(set(POLICIES) - live)
    assert not missing, f"routes without an authorisation policy: {missing}. {HOW_TO}"
    assert not stale, f"policy entries for routes that no longer exist: {stale}"


def test_enumeration_sees_every_documented_operation(matrix_app: FastAPI) -> None:
    """Guards against a vacuous walk: every OpenAPI operation is enumerated."""
    documented = {
        f"{method.upper()} {path}"
        for path, operations in matrix_app.openapi()["paths"].items()
        for method in operations
    }
    live = route_keys(matrix_app)
    assert documented, "the OpenAPI document lists no operations"
    assert documented <= live, sorted(documented - live)
    assert "MOUNT /static" in live


@pytest.mark.parametrize("key", ROUTES)
def test_every_path_parameter_has_a_generator(key: str) -> None:
    declared = set(POLICIES[key].params)
    assert path_params(key) == declared, (
        f"{key}: path parameters {sorted(path_params(key))} vs policy params "
        f"{sorted(declared)}; name a kind from authz_policy.PARAM_KINDS for each"
    )


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", ROUTES)
def test_route_matrix(
    key: str, matrix_app: FastAPI, matrix_settings: Settings, mfactories: Factories
) -> None:
    policy = POLICIES[key]
    world = build_world(mfactories)
    failures: list[str] = []
    for who in PRINCIPALS:
        caller = make_caller(matrix_app, mfactories, world, who, policy)
        try:
            url = resolve_url(key, policy, mfactories, world, _owner_for(world, caller))
            response = send(caller, key, url, resolve_body(policy, world))
            outcome = expected(policy, who)
            wrong = mismatch(outcome, response)
            if wrong is not None:
                failures.append(f"{who}: {wrong} ({response.text[:200]})")
            if who == "cookie_bogus_bearer":
                # The junk Authorization header must never fall back to the
                # cookie: the session is neither used, revoked nor rotated.
                assert caller.login is not None
                if not session_alive(matrix_app, matrix_settings, caller.login):
                    failures.append(f"{who}: the cookie's session was used or ended")
        finally:
            caller.client.close()
    assert not failures, f"{key}:\n  " + "\n  ".join(failures)


def _object_exists(
    factories: Factories, kind: str, object_id: uuid.UUID, household_id: uuid.UUID
) -> bool:
    """The victim's object is still there and not revoked."""
    with factories.transaction() as db:
        if kind == "own_token":
            set_context(db, household_id)  # household-scoped (RLS)
            token = db.get(ApiToken, object_id)
            return token is not None and token.revoked_at is None
        if kind == "own_session":
            row = db.get(UserSession, object_id)
            return row is not None and row.revoked_at is None
        return db.get(MfaMethod, object_id) is not None


@pytest.mark.parametrize("key", OWN_OBJECT_ROUTES)
@pytest.mark.parametrize("actor", ["admin", "other_owner"])
def test_foreign_objects_are_404(
    key: str, actor: str, matrix_app: FastAPI, mfactories: Factories
) -> None:
    """Another user's session, token or key is 404 and stays (same or other household)."""
    policy = POLICIES[key]
    world = build_world(mfactories)
    caller = make_caller(matrix_app, mfactories, world, actor, policy)
    created: list[tuple[str, uuid.UUID]] = []

    def victims(kind: str) -> UserRecord:
        return world.victim

    def fill(match: re.Match[str]) -> str:
        kind = policy.params[match.group(1)]
        object_id = build_object(mfactories, world, kind, victims(kind))
        if kind in OWN_KINDS:
            created.append((kind, uuid.UUID(object_id)))
        return object_id

    url = _PARAM.sub(fill, _template(key)[1])
    try:
        response = send(caller, key, url, resolve_body(policy, world))
    finally:
        caller.client.close()
    kind, object_id = created[0]
    assert (response.status_code, problem(response)) == (404, PARAM_KINDS[kind].not_found), (
        response.text
    )
    assert _object_exists(mfactories, kind, object_id, world.household.id), (
        f"{key}: the victim's {kind} changed"
    )


# ---------------------------------------------------------------------------
# CSRF matrix
# ---------------------------------------------------------------------------


def _csrf_variant(
    variant: str, login: LoggedIn, other: LoggedIn, pre_auth_token: str
) -> dict[str, str]:
    base = login.headers()
    if variant == "no-token":
        base.pop(CSRF_HEADER)
    elif variant == "other-session-token":
        base[CSRF_HEADER] = other.csrf_token
    elif variant == "pre-auth-token":
        base[CSRF_HEADER] = pre_auth_token
    elif variant == "cross-site-origin":
        base["Origin"] = "https://evil.example"
    elif variant == "cross-site-fetch":
        base["Sec-Fetch-Site"] = "cross-site"
    else:  # pragma: no cover - parametrize typo
        raise AssertionError(variant)
    return base


CSRF_VARIANTS = [
    "no-token",
    "other-session-token",
    "pre-auth-token",
    "cross-site-origin",
    "cross-site-fetch",
]


@pytest.fixture(scope="module")
def csrf_world(mfactories: Factories) -> World:
    return build_world(mfactories)


def test_csrf_matrix_is_not_vacuous() -> None:
    assert len(UNSAFE) >= 30
    assert "DELETE /api/v1/households/{household_id}" in UNSAFE
    assert "POST /api/v1/auth/logout" in UNSAFE
    assert "POST /api/v1/auth/oidc/backchannel-logout" not in UNSAFE


def test_csrf_exemptions_are_pinned(matrix_app: FastAPI) -> None:
    """Exempting a path from CSRF is a reviewed change, never a side effect."""
    assert DEFAULT_EXEMPT_PATHS == EXPECTED_CSRF_EXEMPT
    protect = matrix_app.state.csrf
    assert isinstance(protect, CSRFProtect)
    assert protect.exempt_paths == EXPECTED_CSRF_EXEMPT


@pytest.mark.parametrize("variant", CSRF_VARIANTS)
@pytest.mark.parametrize("key", UNSAFE)
def test_csrf_matrix(
    key: str,
    variant: str,
    matrix_app: FastAPI,
    matrix_settings: Settings,
    mfactories: Factories,
    csrf_world: World,
) -> None:
    """A state-changing request riding a valid session cookie is refused."""
    policy = POLICIES[key]
    owner = csrf_world.users["owner"]
    other_client = new_client(matrix_app)
    client = new_client(matrix_app)
    try:
        other = mfactories.login(other_client, csrf_world.users["admin"])
        pre_auth = client.get("/api/v1/auth/session").json()["csrf_token"]
        login = mfactories.login(client, owner)
        protect = matrix_app.state.csrf
        assert isinstance(protect, CSRFProtect)
        assert protect.token_for_session(login.raw_id) == login.csrf_token
        url = resolve_url(key, policy, mfactories, csrf_world, lambda _kind: owner)
        response = client.request(
            _template(key)[0],
            url,
            headers=_csrf_variant(variant, login, other, pre_auth),
            json=resolve_body(policy, csrf_world),
            follow_redirects=False,
        )
    finally:
        client.close()
        other_client.close()
    assert (response.status_code, problem(response)) == (403, "csrf-failed"), response.text
    assert session_alive(matrix_app, matrix_settings, login)
