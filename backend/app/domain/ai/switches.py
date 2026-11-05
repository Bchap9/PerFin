# SPDX-License-Identifier: AGPL-3.0-or-later
"""The AI access switches: the instance kill switch and the household flag (E3, M5).

Two ``setting`` rows under the key :data:`AI_ENABLED_KEY`, value
``{"enabled": bool}``:

* **system** scope: the instance kill switch, changed only by an instance
  admin (``PATCH /settings/system/ai``, step-up). Off disables the AI role
  of the whole instance: every ``/ai/v1`` view answers ``403
  ai-access-disabled`` and no client can be created. The MCP process only
  reaches data through those views, so it is disabled with them.
* **household** scope: the household owner's flag (``PATCH /settings/ai``,
  step-up), with the same effect for one household.

A missing row means **enabled**: with no AI client nothing is exposed, and
creating one already needs the owner on a stepped-up session. Turning a
switch off does not revoke clients; turning it back on resumes them (revoke
a client to end it for good).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.db.models import Setting, SettingScope
from app.domain.ai.models import AiClient

AI_ENABLED_KEY: Final = "ai.enabled"


def ai_access_disabled(where: str) -> ProblemError:
    return ProblemError(
        403,
        "ai-access-disabled",
        "AI access is disabled",
        f"AI access is switched off for this {where}.",
        {"scope": where},
    )


def _row(db: Session, scope: SettingScope, scope_id: uuid.UUID | None) -> Setting | None:
    stmt = select(Setting).where(
        Setting.scope == scope,
        Setting.scope_id.is_(None) if scope_id is None else Setting.scope_id == scope_id,
        Setting.key == AI_ENABLED_KEY,
    )
    return db.execute(stmt).scalar_one_or_none()


def _enabled(row: Setting | None) -> bool:
    if row is None:
        return True
    value = row.value
    return not (isinstance(value, dict) and value.get("enabled") is False)


def system_enabled(db: Session) -> bool:
    """The instance kill switch (True = AI access allowed)."""
    return _enabled(_row(db, SettingScope.SYSTEM, None))


def household_enabled(db: Session, household_id: uuid.UUID) -> bool:
    return _enabled(_row(db, SettingScope.HOUSEHOLD, household_id))


def require_enabled(db: Session, household_id: uuid.UUID) -> None:
    """``403 ai-access-disabled`` when either switch is off."""
    if not system_enabled(db):
        raise ai_access_disabled("instance")
    if not household_enabled(db, household_id):
        raise ai_access_disabled("household")


def _put(
    db: Session, scope: SettingScope, scope_id: uuid.UUID | None, enabled: bool
) -> tuple[bool, bool]:
    """Store the switch; returns ``(before, after)``. RLS decides who may write."""
    row = _row(db, scope, scope_id)
    before = _enabled(row)
    now = clock.utcnow()
    if row is None:
        db.add(
            Setting(
                scope=scope,
                scope_id=scope_id,
                key=AI_ENABLED_KEY,
                value={"enabled": enabled},
                created_at=now,
                updated_at=now,
            )
        )
    else:
        row.value = {"enabled": enabled}
        row.updated_at = now
    db.flush()
    return before, enabled


def set_system_enabled(
    db: Session, principal: Principal, enabled: bool, *, request: Request | None = None
) -> bool:
    """Flip the kill switch (the router requires an instance admin, step-up)."""
    before, after = _put(db, SettingScope.SYSTEM, None, enabled)
    if before != after:
        audit(
            db,
            action="ai.system_switch",
            target_type="setting",
            target_id=None,
            before={"enabled": before},
            after={"enabled": after},
            household_id=None,
            user_id=principal.user_id,
            request=request,
        )
    return after


def set_household_enabled(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    enabled: bool,
    *,
    request: Request | None = None,
) -> bool:
    """Flip the household flag (the router requires the owner, step-up)."""
    before, after = _put(db, SettingScope.HOUSEHOLD, household_id, enabled)
    if before != after:
        audit(
            db,
            action="ai.household_switch",
            target_type="household",
            target_id=household_id,
            before={"enabled": before},
            after={"enabled": after},
            household_id=household_id,
            user_id=principal.user_id,
            request=request,
        )
    return after


@dataclass(frozen=True)
class AccessState:
    system_enabled: bool
    household_enabled: bool
    active_clients: int

    @property
    def enabled(self) -> bool:
        return self.system_enabled and self.household_enabled


def active_client_count(
    db: Session, household_id: uuid.UUID, *, now: datetime | None = None
) -> int:
    """Clients that are neither revoked nor expired (the banner of M5)."""
    now = now or clock.utcnow()
    count = db.execute(
        select(func.count())
        .select_from(AiClient)
        .where(
            AiClient.household_id == household_id,
            AiClient.revoked_at.is_(None),
            AiClient.expires_at > now,
        )
    ).scalar_one()
    return int(count)


def access_state(db: Session, household_id: uuid.UUID) -> AccessState:
    return AccessState(
        system_enabled=system_enabled(db),
        household_enabled=household_enabled(db, household_id),
        active_clients=active_client_count(db, household_id),
    )
