# SPDX-License-Identifier: AGPL-3.0-or-later
"""Several logged-in users at once, for the household, member and admin tests.

:class:`Actors` owns one app and opens a separate ``TestClient`` (own cookie
jar, https so ``Secure`` cookies round-trip) per user, so a test can act as
an owner and check what happened to the member it demoted. Users get a TOTP
factor and a session with a fresh MFA verification by default, which
satisfies ``require_step_up``; pass ``mfa_verified=False`` for a session
whose step-up window has not been opened.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.settings import Settings
from app.db.models import AuditLog, Event
from app.db.rls import set_context
from app.main import API_PREFIX, create_app
from tests.fixtures.auth_clients import browser
from tests.fixtures.factories import AuthedClient, Factories, UserRecord

API = API_PREFIX


class Actors:
    """Builds logged-in :class:`AuthedClient` objects sharing one app."""

    def __init__(self, app: FastAPI, factories: Factories, stack: ExitStack) -> None:
        self.app = app
        self.factories = factories
        self._stack = stack

    @classmethod
    @contextmanager
    def open(cls, settings: Settings, factories: Factories) -> Iterator[Actors]:
        with ExitStack() as stack:
            yield cls(create_app(settings), factories, stack)

    def client(self) -> TestClient:
        return self._stack.enter_context(browser(self.app))

    def user(self, **kwargs: Any) -> UserRecord:
        kwargs.setdefault("with_totp", True)
        return self.factories.user(**kwargs)

    def login(
        self, record: UserRecord, *, mfa_verified: bool | None = None, **kwargs: Any
    ) -> AuthedClient:
        client = self.client()
        login = self.factories.login(client, record, mfa_verified=mfa_verified, **kwargs)
        return AuthedClient(client, record, login)

    def new(self, *, mfa_verified: bool | None = None, **kwargs: Any) -> AuthedClient:
        """A new user (``factories.user(**kwargs)``, TOTP by default), logged in."""
        return self.login(self.user(**kwargs), mfa_verified=mfa_verified)

    def anonymous(self) -> TestClient:
        return self.client()


def household_audit(factories: Factories, household_id: uuid.UUID, action: str) -> list[AuditLog]:
    with factories.transaction() as db:
        set_context(db, household_id)
        rows = db.execute(
            select(AuditLog)
            .where(AuditLog.household_id == household_id, AuditLog.action == action)
            .order_by(AuditLog.seq)
        ).scalars()
        out = list(rows)
        for row in out:
            db.expunge(row)
        return out


def instance_audit(factories: Factories, action: str, target_id: uuid.UUID) -> list[AuditLog]:
    with factories.transaction() as db:
        set_context(db, None, instance_admin=True)
        rows = db.execute(
            select(AuditLog)
            .where(
                AuditLog.household_id.is_(None),
                AuditLog.action == action,
                AuditLog.target_id == str(target_id),
            )
            .order_by(AuditLog.seq)
        ).scalars()
        out = list(rows)
        for row in out:
            db.expunge(row)
        return out


def household_events(factories: Factories, household_id: uuid.UUID, type_: str) -> list[Event]:
    with factories.transaction() as db:
        set_context(db, household_id)
        rows = db.execute(
            select(Event)
            .where(Event.household_id == household_id, Event.type == type_)
            .order_by(Event.created_at, Event.id)
        ).scalars()
        out = list(rows)
        for row in out:
            db.expunge(row)
        return out


def problem_type(response: Any) -> str:
    assert response.headers["content-type"] == "application/problem+json", response.text
    return str(response.json()["type"]).removeprefix("urn:perfin:problem:")
