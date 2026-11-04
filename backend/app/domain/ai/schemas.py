# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schemas of the AI settings routes (``/settings/ai``, ``/settings/system/ai``; API.md).

These are owner-facing (browser session) models, never returned to an AI
client: an ``ai:read`` token is confined to ``/ai/v1`` (``app.auth.deps``).
Input models forbid unknown fields (F4). The raw token appears only in
:class:`AiClientCreatedOut`, once.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.auth.tokens import TOKEN_MAX_DAYS_CAP
from app.domain.ai.models import (
    DATE_RANGE_CAP_MAX,
    NAME_MAX,
    TERM_MAX,
    TERMS_MAX,
    AiMode,
    AiTransport,
)

Term = Annotated[str, Field(min_length=1, max_length=TERM_MAX)]
#: Most characters of ad-hoc text in the scrubber preview.
SAMPLE_TEXT_MAX = 4096


class AiOptionsIn(BaseModel):
    """The exposure options of a client (also the body of ``POST /settings/ai/preview``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(min_length=1, max_length=NAME_MAX)] = "Preview"
    mode: AiMode = AiMode.DETAIL
    transport: AiTransport = AiTransport.STDIO
    rounding: Literal[1, 10, 100] | None = None
    hide_nicknames: bool = False
    hide_merchant_location: bool = False
    date_range_cap: Annotated[int | None, Field(ge=1, le=DATE_RANGE_CAP_MAX)] = None
    custom_denylist: Annotated[list[Term], Field(max_length=TERMS_MAX)] = Field(
        default_factory=list
    )
    member_aliases: Annotated[list[Term], Field(max_length=TERMS_MAX)] = Field(default_factory=list)


class AiClientCreate(AiOptionsIn):
    """``POST /settings/ai/clients``: a name and an expiry are mandatory."""

    name: Annotated[str, Field(min_length=1, max_length=NAME_MAX)]
    expires_in_days: Annotated[int, Field(ge=1, le=TOKEN_MAX_DAYS_CAP)]


class AiClientOut(BaseModel):
    """A client as the owner sees it (never the token or its hash)."""

    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    mode: AiMode
    transport: AiTransport
    rounding: int | None
    hide_nicknames: bool
    hide_merchant_location: bool
    date_range_cap: int | None
    custom_denylist: list[str]
    member_aliases: list[str]
    token_prefix: str | None
    status: Literal["active", "expired", "revoked"]
    created_at: datetime
    expires_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


class AiClientCreatedOut(AiClientOut):
    token: str = Field(
        description="The raw ai:read token. Shown once; it cannot be retrieved again."
    )


class AiAccessOut(BaseModel):
    """``GET /settings/ai``: the switches and the number of live clients."""

    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(description="Both switches are on: active clients can read.")
    household_enabled: bool
    system_enabled: bool
    active_clients: int


class AiAccessUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool | None = None


class SystemAiOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    enabled: bool = Field(description="False: the kill switch is engaged for the instance.")


class SystemAiUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool | None = None


class PlaceholderMark(BaseModel):
    """A string in a preview body that carries scrubber placeholders."""

    model_config = ConfigDict(frozen=True)

    path: str = Field(description="JSON path of the string, e.g. items[2].payee")
    placeholders: list[str]


class PreviewToolOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    tool: str
    params: dict[str, Any]
    status: int
    bytes: int
    body: Any = Field(description="The body exactly as the AI view would send it.")
    placeholders: list[PlaceholderMark]


class ExposurePreviewOut(BaseModel):
    """What every tool would return with these options; nothing is stored or sent."""

    model_config = ConfigDict(frozen=True)

    mode: AiMode
    tools: list[PreviewToolOut]
    placeholder_counts: dict[str, int]


class AiCallOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    client_id: uuid.UUID
    tool: str
    params: dict[str, Any]
    status: int
    bytes: int
    created_at: datetime
    response: Any


class AiCallsOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[AiCallOut]


class ScrubberPreviewIn(BaseModel):
    """Terms to try before saving them on a client, and optional ad-hoc text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    client_id: uuid.UUID | None = None
    custom_denylist: Annotated[list[Term], Field(max_length=TERMS_MAX)] = Field(
        default_factory=list
    )
    member_aliases: Annotated[list[Term], Field(max_length=TERMS_MAX)] = Field(default_factory=list)
    text: Annotated[str | None, Field(max_length=SAMPLE_TEXT_MAX)] = None
    limit: Annotated[int, Field(ge=1, le=500)] = 100


class ScrubbedSample(BaseModel):
    model_config = ConfigDict(frozen=True)

    source: str = Field(description="Where the text lives, e.g. transaction.memo")
    original: str
    scrubbed: str
    changed: bool
    placeholders: list[str]


class ScrubberPreviewOut(BaseModel):
    model_config = ConfigDict(frozen=True)

    samples: list[ScrubbedSample]
    changed: int
