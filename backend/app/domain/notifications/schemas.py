# SPDX-License-Identifier: AGPL-3.0-or-later
"""Schemas of ``/settings/notifications/channels`` (API.md, docs/security/notifications.md).

A channel's configuration is a tagged object (``type``). Credentials are
**write-only**: they are accepted on create and change and never returned
(``secrets_set`` names the ones stored). The webhook signing secret is made
by the server and shown once, in the create answer and after
``rotate_secret``.

Destinations: ``https://`` URLs only, without credentials or a fragment;
SMTP is a host name or IP address with ``starttls`` or ``implicit`` TLS.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Annotated, Final, Literal, get_args

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.intake_settings.schemas import clean_address, clean_host
from app.domain.notifications.models import NAME_MAX
from app.domain.notifications.routing import KINDS, Kind
from app.integrations.notifications.base import ChannelType

URL_MAX: Final = 2048
SECRET_MAX: Final = 1024
RECIPIENTS_MAX: Final = 10
TOPIC_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
#: ``!opaque:server.name`` (room ids only; aliases are resolved by the client).
ROOM_PATTERN: Final = re.compile(r"^![A-Za-z0-9._=/+-]{1,128}:[A-Za-z0-9.:\[\]-]{1,255}$")

#: Write-only fields per channel type.
SECRET_FIELDS: Final[dict[ChannelType, tuple[str, ...]]] = {
    ChannelType.SMTP: ("password",),
    ChannelType.WEBHOOK: ("secret",),
    ChannelType.NTFY: ("topic", "token"),
    ChannelType.GOTIFY: ("token",),
    ChannelType.MATRIX: ("access_token",),
}
#: Fields that say where messages go (changing one keeps no stored secret by accident).
DESTINATION_FIELDS: Final[dict[ChannelType, tuple[str, ...]]] = {
    ChannelType.SMTP: ("host", "port"),
    ChannelType.WEBHOOK: ("url",),
    ChannelType.NTFY: ("server_url",),
    ChannelType.GOTIFY: ("server_url",),
    ChannelType.MATRIX: ("homeserver_url",),
}

Secret = Annotated[str, Field(min_length=1, max_length=SECRET_MAX)]


def clean_https_url(value: str, *, base: bool) -> str:
    """An ``https`` URL without credentials or fragment (a base URL: no query either)."""
    text = value.strip()
    if len(text) > URL_MAX or any(c in text for c in "\r\n\t "):
        raise ValueError("must be an https URL")
    try:
        url = httpx.URL(text)
    except (httpx.InvalidURL, ValueError):
        raise ValueError("must be an https URL") from None
    if url.scheme != "https" or not url.host:
        raise ValueError("must be an https URL")
    if url.userinfo:
        raise ValueError("must not contain credentials")
    if url.fragment:
        raise ValueError("must not contain a fragment")
    if base and url.query:
        raise ValueError("must not contain a query")
    clean_host(url.host)
    return text.rstrip("/") if base else text


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SmtpConfigIn(_Strict):
    type: Literal["smtp"]
    host: Annotated[str, Field(max_length=253)]
    port: Annotated[int, Field(ge=1, le=65535)] = 587
    tls: Literal["starttls", "implicit"] = "starttls"
    sender: Annotated[str, Field(max_length=254)]
    recipients: Annotated[list[str], Field(min_length=1, max_length=RECIPIENTS_MAX)]
    username: Annotated[str, Field(max_length=320)] | None = None
    password: Secret | None = None

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        return clean_host(value)

    @field_validator("sender")
    @classmethod
    def _sender(cls, value: str) -> str:
        return clean_address(value)

    @field_validator("recipients")
    @classmethod
    def _recipients(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(clean_address(v) for v in value))


class WebhookConfigIn(_Strict):
    type: Literal["webhook"]
    url: Annotated[str, Field(max_length=URL_MAX)]

    @field_validator("url")
    @classmethod
    def _url(cls, value: str) -> str:
        return clean_https_url(value, base=False)


class NtfyConfigIn(_Strict):
    type: Literal["ntfy"]
    server_url: Annotated[str, Field(max_length=URL_MAX)] = "https://ntfy.sh"
    topic: Annotated[str, Field(pattern=TOPIC_PATTERN.pattern)]
    token: Secret | None = None
    priority: Annotated[int, Field(ge=1, le=5)] = 3

    @field_validator("server_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return clean_https_url(value, base=True)


class GotifyConfigIn(_Strict):
    type: Literal["gotify"]
    server_url: Annotated[str, Field(max_length=URL_MAX)]
    token: Secret
    priority: Annotated[int, Field(ge=0, le=10)] = 5

    @field_validator("server_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return clean_https_url(value, base=True)


class MatrixConfigIn(_Strict):
    type: Literal["matrix"]
    homeserver_url: Annotated[str, Field(max_length=URL_MAX)]
    room_id: Annotated[str, Field(pattern=ROOM_PATTERN.pattern)]
    access_token: Secret

    @field_validator("homeserver_url")
    @classmethod
    def _url(cls, value: str) -> str:
        return clean_https_url(value, base=True)


ChannelConfigIn = Annotated[
    SmtpConfigIn | WebhookConfigIn | NtfyConfigIn | GotifyConfigIn | MatrixConfigIn,
    Field(discriminator="type"),
]
CONFIG_MODELS: Final[dict[ChannelType, type[_Strict]]] = {
    ChannelType.SMTP: SmtpConfigIn,
    ChannelType.WEBHOOK: WebhookConfigIn,
    ChannelType.NTFY: NtfyConfigIn,
    ChannelType.GOTIFY: GotifyConfigIn,
    ChannelType.MATRIX: MatrixConfigIn,
}

Name = Annotated[str, Field(min_length=1, max_length=NAME_MAX)]


def _clean_name(value: str) -> str:
    name = value.strip()
    if not name or any(ord(c) < 32 for c in name):
        raise ValueError("must be a non-empty single line")
    return name


def _all_kinds() -> list[Kind]:
    return list(get_args(Kind))


class ChannelCreate(_Strict):
    name: Name
    enabled: bool = True
    events: Annotated[list[Kind], Field(max_length=len(KINDS))] = Field(default_factory=_all_kinds)
    config: ChannelConfigIn

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return _clean_name(value)


class ChannelConfigPatch(_Strict):
    """Fields to change in a stored configuration (checked again after merging)."""

    type: ChannelType | None = None
    host: Annotated[str, Field(max_length=253)] | None = None
    port: Annotated[int, Field(ge=1, le=65535)] | None = None
    tls: Literal["starttls", "implicit"] | None = None
    sender: Annotated[str, Field(max_length=254)] | None = None
    recipients: Annotated[list[str], Field(max_length=RECIPIENTS_MAX)] | None = None
    username: Annotated[str, Field(max_length=320)] | None = None
    password: Secret | None = None
    url: Annotated[str, Field(max_length=URL_MAX)] | None = None
    server_url: Annotated[str, Field(max_length=URL_MAX)] | None = None
    topic: Annotated[str, Field(max_length=64)] | None = None
    token: Secret | None = None
    priority: Annotated[int, Field(ge=0, le=10)] | None = None
    homeserver_url: Annotated[str, Field(max_length=URL_MAX)] | None = None
    room_id: Annotated[str, Field(max_length=400)] | None = None
    access_token: Secret | None = None


class ChannelUpdate(_Strict):
    """Change some fields; ``config`` is merged into the stored one (same type)."""

    name: Name | None = None
    enabled: bool | None = None
    events: Annotated[list[Kind], Field(max_length=len(KINDS))] | None = None
    config: ChannelConfigPatch | None = None
    rotate_secret: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        return None if value is None else _clean_name(value)


class ChannelConfigOut(BaseModel):
    """The non-secret part of a channel's configuration."""

    host: str | None = None
    port: int | None = None
    tls: Literal["starttls", "implicit"] | None = None
    sender: str | None = None
    recipients: list[str] | None = None
    username: str | None = None
    url: str | None = None
    server_url: str | None = None
    homeserver_url: str | None = None
    room_id: str | None = None
    priority: int | None = None


Outcome = Literal[
    "ok",
    "blocked",
    "tls_failed",
    "auth_failed",
    "rejected",
    "connect_failed",
    "timeout",
    "server_error",
    "protocol_error",
]


class ChannelOut(BaseModel):
    id: uuid.UUID
    name: str
    type: ChannelType
    enabled: bool
    events: list[Kind]
    config: ChannelConfigOut
    secrets_set: list[str]
    last_sent_at: datetime | None
    last_outcome: Outcome | None
    created_at: datetime
    updated_at: datetime


class ChannelSavedOut(ChannelOut):
    """``ChannelOut`` plus the webhook signing secret, only when just made."""

    signing_secret: str | None = None


class ChannelTestOut(BaseModel):
    ok: bool
    outcome: Outcome
