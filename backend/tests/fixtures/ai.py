# SPDX-License-Identifier: AGPL-3.0-or-later
"""AI clients for tests (Phase 6b): created through the real AI-client flow.

``make_ai_client(db, owner, settings, **options)`` (flush-only) calls
``app.domain.ai.service.create_client`` as the owner on a freshly stepped-up
browser session and returns ``(client, raw token)``; ``Factories`` users from
``tests/fixtures/factories.py`` work as ``owner``. ``ai_headers(raw)`` is the
bearer header an MCP process would send.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.ai.models import AiClient
from app.domain.ai.service import ClientOptions, create_client
from tests.fixtures.factories import Factories, UserRecord


def owner_principal(owner: UserRecord) -> Principal:
    """The owner on a browser session that has just passed step-up."""
    assert owner.household_id is not None
    return Principal(
        user=owner.user,
        household_id=owner.household_id,
        role=HouseholdRole.OWNER,
        auth_method="session",
        mfa_verified_at=clock.utcnow(),
        mfa_enrolled=True,
    )


def make_ai_client(
    db: Session, owner: UserRecord, settings: Settings, **options: Any
) -> tuple[AiClient, str]:
    options.setdefault("name", "Test assistant")
    assert owner.household_id is not None
    set_context(db, owner.household_id, owner.id)
    created = create_client(db, owner_principal(owner), ClientOptions(**options), settings=settings)
    return created.client, created.raw_token


def ai_client(factories: Factories, owner: UserRecord, **options: Any) -> tuple[AiClient, str]:
    """Committed :func:`make_ai_client`."""
    with factories.transaction() as db:
        client, raw = make_ai_client(db, owner, factories.settings, **options)
        db.expunge(client)
    return client, raw


def ai_headers(raw: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw}"}
