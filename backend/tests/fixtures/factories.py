# SPDX-License-Identifier: AGPL-3.0-or-later
"""Synthetic identity data for tests (docs/DEVELOPMENT.md, "Tests").

Plain functions that take a SQLAlchemy ``Session`` and only ``flush`` (so they
work inside the rolled-back ``db`` fixture), plus :class:`Factories`, which
commits each object in its own transaction so a ``TestClient`` (another
connection) sees it. ``tests/conftest.py`` exposes the committed variants as
the ``make_household``, ``make_user``, ``login_as``, ``bearer_token`` and
``authed_client`` fixtures.

Everything here is synthetic: generated names, random passwords, random TOTP
secrets. Passwords are hashed with deliberately cheap Argon2id parameters
(:data:`TEST_PASSWORD_PARAMS`) to keep the suite fast; production code
hashes with ``app.auth.passwords.DEFAULT_PARAMS``.
"""

from __future__ import annotations

import re
import secrets
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import pyotp
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from app.auth.models import (
    AccountGrant,
    ApiToken,
    Credential,
    GrantLevel,
    HouseholdMember,
    HouseholdRole,
    Identity,
    IdentityProvider,
    LoginMethod,
    MfaMethod,
    MfaType,
    SessionStage,
    User,
)
from app.auth.passwords import PasswordParams, hash_password
from app.auth.principal import generate_api_token
from app.auth.sessions import IssuedSession, SessionPolicy, cookie_name, create_session
from app.core import clock
from app.core.crypto import Keyring, column_ad
from app.core.csrf import CSRF_HEADER, CSRFProtect
from app.core.ids import uuid7
from app.core.settings import Settings
from app.db.models import Household
from app.db.rls import get_context, restore_context, set_context

#: Cheap Argon2id parameters for test users only.
TEST_PASSWORD_PARAMS = PasswordParams(memory_kib=1024, time_cost=1, parallelism=1)
TEST_ORIGIN = "http://testserver"

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


def without_ids(value: object) -> str:
    """``str(value)`` with every UUID masked, for "these digits never appear" checks.

    Audit and review payloads list row ids; random UUID hex holds any four
    digits now and then, so a check for card or account digits must not
    look inside them.
    """
    return _UUID.sub("<id>", str(value))


def unique_name(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(5)}"


def random_password() -> str:
    return secrets.token_urlsafe(18)


@contextmanager
def _context(
    db: Session, household_id: uuid.UUID | None, user_id: uuid.UUID | None = None
) -> Iterator[None]:
    """Run in a household context, then put the caller's context back."""
    saved = get_context(db)
    set_context(db, household_id, user_id)
    yield
    # Not in ``finally``: after a failed statement the transaction is aborted
    # and the context ends with it anyway.
    restore_context(db, saved)


# ---------------------------------------------------------------------------
# Flush-only builders (any Session)
# ---------------------------------------------------------------------------


def make_household(db: Session, *, name: str | None = None, currency: str = "USD") -> Household:
    household_id = uuid7()
    with _context(db, household_id):
        household = Household(id=household_id, name=name or unique_name("Household"))
        household.base_currency = currency
        db.add(household)
        db.flush()
    return household


def add_member(
    db: Session, household_id: uuid.UUID, user_id: uuid.UUID, role: HouseholdRole
) -> HouseholdMember:
    with _context(db, household_id):
        member = HouseholdMember(household_id=household_id, user_id=user_id, role=role)
        db.add(member)
        db.flush()
    return member


def grant_account(
    db: Session,
    household_id: uuid.UUID,
    user_id: uuid.UUID,
    account_id: uuid.UUID,
    level: GrantLevel,
) -> AccountGrant:
    with _context(db, household_id):
        grant = AccountGrant(
            household_id=household_id, user_id=user_id, account_id=account_id, level=level
        )
        db.add(grant)
        db.flush()
    return grant


@dataclass
class UserRecord:
    """A synthetic user plus the secrets a test needs to act as them."""

    user: User
    household: Household | None
    role: HouseholdRole | None
    password: str = field(repr=False)
    totp_secret: str | None = field(default=None, repr=False)

    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def household_id(self) -> uuid.UUID | None:
        return self.household.id if self.household else None

    def totp_now(self) -> str:
        if self.totp_secret is None:
            raise ValueError("user has no TOTP secret")
        return pyotp.TOTP(self.totp_secret).now()


def add_totp(db: Session, keyring: Keyring, user_id: uuid.UUID, *, name: str = "Phone") -> str:
    """Enrol a TOTP factor with a random secret (encrypted at rest); return the secret."""
    secret = pyotp.random_base32()
    method_id = uuid7()
    method = MfaMethod(
        id=method_id,
        user_id=user_id,
        type=MfaType.TOTP,
        name=name,
        secret_encrypted=keyring.encrypt_str(
            secret, ad=column_ad("mfa_method", "secret_encrypted", method_id)
        ),
    )
    db.add(method)
    db.flush()
    return secret


def make_user(
    db: Session,
    *,
    keyring: Keyring | None = None,
    role: HouseholdRole | str | None = HouseholdRole.MEMBER,
    household: Household | None = None,
    with_totp: bool = False,
    with_password: bool = True,
    password: str | None = None,
    username: str | None = None,
    display_name: str | None = None,
    email: str | None = None,
    is_instance_admin: bool = False,
    is_active: bool = True,
    is_breakglass: bool = False,
) -> UserRecord:
    """A user with a local password and, unless ``role=None``, a household role.

    A new household is created when ``role`` is set and ``household`` is not.
    ``with_totp`` needs ``keyring``.
    """
    role_value = HouseholdRole(role) if role is not None else None
    name = (username or unique_name("user")).lower()
    user = User(
        username=name,
        display_name=display_name or f"Test {name}",
        email=email,
        email_verified=email is not None,
        is_instance_admin=is_instance_admin,
        is_active=is_active,
        is_breakglass=is_breakglass,
    )
    db.add(user)
    db.flush()
    secret = password or random_password()
    if with_password:
        hashed = hash_password(secret, TEST_PASSWORD_PARAMS)
        db.add(
            Credential(
                user_id=user.id, password_hash=hashed.encoded, hash_params=hashed.params_dict()
            )
        )
        db.add(
            Identity(
                user_id=user.id,
                provider=IdentityProvider.LOCAL,
                issuer="local",
                subject=str(user.id),
            )
        )
        db.flush()
    totp_secret: str | None = None
    if with_totp:
        if keyring is None:
            raise ValueError("with_totp needs a keyring")
        totp_secret = add_totp(db, keyring, user.id)
    if role_value is not None:
        household = household or make_household(db)
        add_member(db, household.id, user.id, role_value)
    return UserRecord(
        user=user,
        household=household if role_value is not None else None,
        role=role_value,
        password=secret,
        totp_secret=totp_secret,
    )


def make_api_token(
    db: Session,
    user_id: uuid.UUID,
    household_id: uuid.UUID,
    *,
    scopes: Iterable[str] = (),
    expires_at: datetime | None = None,
    revoked: bool = False,
    name: str = "test token",
) -> tuple[ApiToken, str]:
    """A personal access token row; returns ``(row, raw token)``."""
    material = generate_api_token()
    now = clock.utcnow()
    with _context(db, household_id):
        token = ApiToken(
            household_id=household_id,
            user_id=user_id,
            prefix=material.prefix,
            token_hash=material.token_hash,
            name=name,
            scopes=sorted(set(scopes)),
            expires_at=expires_at or now + timedelta(days=30),
            revoked_at=now if revoked else None,
        )
        db.add(token)
        db.flush()
    return token, material.raw


def make_session(
    db: Session,
    user_id: uuid.UUID,
    *,
    settings: Settings,
    household_id: uuid.UUID | None = None,
    mfa_verified: bool = False,
    stage: SessionStage = SessionStage.AUTHENTICATED,
    login_method: LoginMethod = LoginMethod.LOCAL,
    remember_device: bool = False,
    now: datetime | None = None,
) -> IssuedSession:
    """A real session through ``app.auth.sessions.create_session``."""
    return create_session(
        db,
        user_id=user_id,
        policy=SessionPolicy.from_settings(settings),
        login_method=login_method,
        stage=stage,
        household_id=household_id,
        ip="192.0.2.10",
        user_agent="pytest",
        mfa_verified=mfa_verified,
        remember_device=remember_device,
        now=now,
    )


# ---------------------------------------------------------------------------
# Committed variants and client helpers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoggedIn:
    """A session set on a ``TestClient``: id, cookie value and CSRF headers."""

    session_id: uuid.UUID
    raw_id: str = field(repr=False)
    csrf_token: str = field(repr=False)

    def headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """Headers for a state-changing request from the SPA."""
        out = {CSRF_HEADER: self.csrf_token, "Origin": TEST_ORIGIN, "Sec-Fetch-Site": "same-origin"}
        out.update(extra or {})
        return out


class AuthedClient:
    """A ``TestClient`` logged in as ``record``; unsafe methods add CSRF headers."""

    def __init__(self, client: TestClient, record: UserRecord, login: LoggedIn) -> None:
        self.client = client
        self.record = record
        self.login = login

    @property
    def user(self) -> User:
        return self.record.user

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.client.get(url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = self.login.headers(kwargs.pop("headers", None))
        return self.client.request(method, url, headers=headers, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> Any:
        return self.request("PATCH", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        return self.request("PUT", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self.request("DELETE", url, **kwargs)


class Factories:
    """Committing builders on the application role's connection pool."""

    def __init__(self, session_factory: sessionmaker[Session], settings: Settings) -> None:
        self.session_factory = session_factory
        self.settings = settings
        self.keyring = Keyring.from_settings(settings)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with self.session_factory() as db, db.begin():
            yield db

    def household(self, **kwargs: Any) -> Household:
        with self.transaction() as db:
            return make_household(db, **kwargs)

    def user(self, **kwargs: Any) -> UserRecord:
        kwargs.setdefault("keyring", self.keyring)
        with self.transaction() as db:
            return make_user(db, **kwargs)

    def member(self, household_id: uuid.UUID, user_id: uuid.UUID, role: HouseholdRole) -> None:
        with self.transaction() as db:
            add_member(db, household_id, user_id, role)

    def grant(
        self,
        household_id: uuid.UUID,
        user_id: uuid.UUID,
        account_id: uuid.UUID,
        level: GrantLevel,
    ) -> None:
        with self.transaction() as db:
            grant_account(db, household_id, user_id, account_id, level)

    def token(self, record: UserRecord, **kwargs: Any) -> str:
        household_id = kwargs.pop("household_id", None) or record.household_id
        if household_id is None:
            raise ValueError("a token needs a household")
        with self.transaction() as db:
            _, raw = make_api_token(db, record.id, household_id, **kwargs)
        return raw

    def session(self, record: UserRecord, **kwargs: Any) -> IssuedSession:
        kwargs.setdefault("household_id", record.household_id)
        with self.transaction() as db:
            return make_session(db, record.id, settings=self.settings, **kwargs)

    def login(
        self,
        client: TestClient,
        record: UserRecord,
        *,
        mfa_verified: bool | None = None,
        **kwargs: Any,
    ) -> LoggedIn:
        """Create a real session for ``record`` and put its cookie on ``client``.

        ``mfa_verified`` defaults to whether the user has a TOTP factor (as if
        they had just completed the second step of the login).
        """
        verified = record.totp_secret is not None if mfa_verified is None else mfa_verified
        issued = self.session(record, mfa_verified=verified, **kwargs)
        # No domain: the jar then sends it to the TestClient host (testserver).
        client.cookies.set(cookie_name(self.settings), issued.raw_id)
        app = client.app
        protect = app.state.csrf if isinstance(app, FastAPI) else None
        if not isinstance(protect, CSRFProtect):
            raise RuntimeError("client app has no CSRF protection installed")
        return LoggedIn(
            session_id=issued.row.id,
            raw_id=issued.raw_id,
            csrf_token=protect.token_for_session(issued.raw_id),
        )
