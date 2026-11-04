# SPDX-License-Identifier: AGPL-3.0-or-later
"""AI clients and the audit of their calls (DATA_MODEL.md "Identity and access", AI_ACCESS.md).

* ``ai_client``: one MCP client of a household, created by an owner through
  the AI-client flow (``app.domain.ai.service.create_client``), which is the
  only code that mints an ``ai:read`` token. The client's policy (mode,
  rounding, hide nicknames, hide merchant location, date-range cap, custom
  denylist, member aliases) is applied server-side to every ``/ai/v1``
  response. ``token_id`` is the client's ``api_token``; revoking the client
  revokes the token. ``custom_denylist`` and ``member_aliases`` are terms the
  scrubber removes: they are identity data themselves (a child's school, a
  nickname) and are classified ``denied`` for AI views.
* ``ai_call``: every ``/ai/v1`` call of a client: the tool (route), the
  validated parameters, the HTTP status, the byte count and the **full
  response body as sent** (M4, E4), so the owner can search what left.

Both tables are ``HouseholdScoped`` (forced RLS). ``ai_call`` references its
client household-consistently (ADR-0008). ``ai_client.token_id`` is a plain
``SET NULL`` key: ``api_token`` has no ``(household_id, id)`` key, and the
service creates both rows in one transaction in the same household.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

import app.auth.models  # noqa: F401 - api_token and app_user are referenced
from app.db.base import Base, HouseholdScoped, Timestamps, UUIDPk
from app.db.types import household_fk, household_key, pg_enum

NAME_MAX: Final = 100
TERM_MAX: Final = 100
#: Most custom denylist entries / member aliases per client.
TERMS_MAX: Final = 200
TOOL_MAX: Final = 64
#: Largest date-range cap in days (ten years).
DATE_RANGE_CAP_MAX: Final = 3660

IDENTITY: Final[dict[str, Any]] = {"classification": "identity"}


class AiMode(enum.StrEnum):
    DETAIL = "detail"
    AGGREGATES = "aggregates"


class AiTransport(enum.StrEnum):
    STDIO = "stdio"
    HTTP = "http"


#: Allowed values of ``ai_client.rounding`` (nearest 1, 10 or 100; NULL = exact).
ROUNDING_STEPS: Final = (1, 10, 100)


class AiClient(UUIDPk, Timestamps, HouseholdScoped, Base):
    """An MCP client of the household and its exposure policy."""

    __tablename__ = "ai_client"
    __table_args__ = (
        household_key("ai_client"),
        CheckConstraint("rounding IS NULL OR rounding IN (1, 10, 100)", name="rounding_step"),
        CheckConstraint(
            f"date_range_cap IS NULL OR (date_range_cap >= 1 AND date_range_cap <= "
            f"{DATE_RANGE_CAP_MAX})",
            name="date_range_cap_bounds",
        ),
        CheckConstraint(f"cardinality(custom_denylist) <= {TERMS_MAX}", name="denylist_bounded"),
        CheckConstraint(f"cardinality(member_aliases) <= {TERMS_MAX}", name="aliases_bounded"),
    )

    name: Mapped[str] = mapped_column(String(NAME_MAX), nullable=False)
    mode: Mapped[AiMode] = mapped_column(
        pg_enum(AiMode, "ai_mode"), nullable=False, default=AiMode.DETAIL
    )
    transport: Mapped[AiTransport] = mapped_column(
        pg_enum(AiTransport, "ai_transport"), nullable=False, default=AiTransport.STDIO
    )
    rounding: Mapped[int | None] = mapped_column(SmallInteger)
    hide_nicknames: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    hide_merchant_location: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    #: Furthest back (in days before today) the client may query; NULL = no cap.
    date_range_cap: Mapped[int | None] = mapped_column(Integer)
    custom_denylist: Mapped[list[str]] = mapped_column(
        ARRAY(String(TERM_MAX)),
        nullable=False,
        default=list,
        server_default=text("'{}'"),
        info=IDENTITY,
    )
    member_aliases: Mapped[list[str]] = mapped_column(
        ARRAY(String(TERM_MAX)),
        nullable=False,
        default=list,
        server_default=text("'{}'"),
        info=IDENTITY,
    )
    token_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("api_token.id", ondelete="SET NULL"),
        unique=True,
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("app_user.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def __repr__(self) -> str:
        return f"AiClient(id={self.id!s}, mode={self.mode!s})"


class AiCall(UUIDPk, Timestamps, HouseholdScoped, Base):
    """One ``/ai/v1`` call and the exact response that left (M4)."""

    __tablename__ = "ai_call"
    __table_args__ = (
        household_fk("ai_call", "ai_client_id", "ai_client"),
        Index("ix_ai_call_client_created", "ai_client_id", "created_at"),
        CheckConstraint("bytes >= 0", name="bytes_non_negative"),
        CheckConstraint("jsonb_typeof(params) = 'object'", name="params_object"),
    )

    ai_client_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    tool: Mapped[str] = mapped_column(String(TOOL_MAX), nullable=False)
    params: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    status: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The response body exactly as sent (already allowlisted and scrubbed).
    response: Mapped[Any] = mapped_column(JSONB, nullable=False)

    def __repr__(self) -> str:
        return f"AiCall(id={self.id!s}, tool={self.tool!r})"
