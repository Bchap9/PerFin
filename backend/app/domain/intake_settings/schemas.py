# SPDX-License-Identifier: AGPL-3.0-or-later
"""Input and output schemas of ``/settings/intake`` (API.md, SECURITY.md F4, D2).

The mailbox password is write-only: it is accepted by ``PATCH`` and never
returned (``password_set`` says whether one is stored). The username is
returned to owners and admins (it names the mailbox, it is not a secret on
its own). The alias token is returned so the owner can set up forwarding.
"""

from __future__ import annotations

import ipaddress
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.intake_settings.models import FOLDER_MAX, HOST_MAX

USERNAME_MAX: Final = 320
PASSWORD_MAX: Final = 1024
MAX_ALLOWED_SENDERS: Final = 100
ADDRESS_MAX: Final = 254

# A DNS name (letters, digits, hyphens; at least one dot) or an IP literal.
_LABEL: Final = r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)"
HOSTNAME_PATTERN: Final = re.compile(rf"^(?=.{{1,{HOST_MAX}}}$){_LABEL}(?:\.{_LABEL})+\.?$")
# A plain mailbox address: one '@', no spaces or control characters.
ADDRESS_PATTERN: Final = re.compile(
    r"^[^@\s\x00-\x1f\x7f<>(),;:\"\[\]\\]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z0-9-]+$"
)
# Folder names are printable ASCII (IMAP modified UTF-7 is not supported).
FOLDER_PATTERN: Final = re.compile(rf"^[\x20-\x7e]{{1,{FOLDER_MAX}}}$")

PollOutcome = Literal[
    "ok",
    "not_configured",
    "blocked",
    "connect_failed",
    "tls_failed",
    "auth_failed",
    "folder_missing",
    "protocol_error",
    "timeout",
]


def clean_host(value: str) -> str:
    host = value.strip().rstrip(".").lower()
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not HOSTNAME_PATTERN.fullmatch(host):
            raise ValueError("must be a host name or an IP address") from None
    return host


def clean_address(value: str) -> str:
    address = value.strip().lower()
    if len(address) > ADDRESS_MAX or not ADDRESS_PATTERN.fullmatch(address):
        raise ValueError("must be a plain email address")
    return address


def clean_folder(value: str) -> str:
    if not FOLDER_PATTERN.fullmatch(value) or value != value.strip():
        raise ValueError(f"must be 1 to {FOLDER_MAX} printable ASCII characters")
    return value


class AllowedSender(BaseModel):
    """An extra sender address and the member its mail belongs to."""

    model_config = ConfigDict(extra="forbid")

    address: Annotated[str, Field(min_length=3, max_length=ADDRESS_MAX)]
    user_id: uuid.UUID

    @field_validator("address")
    @classmethod
    def _address(cls, value: str) -> str:
        return clean_address(value)


class MailIntakeUpdate(BaseModel):
    """``PATCH /settings/intake``: only the fields given change."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    host: Annotated[str, Field(min_length=1, max_length=HOST_MAX)] | None = None
    port: Annotated[int, Field(ge=1, le=65535)] | None = None
    username: Annotated[str, Field(min_length=1, max_length=USERNAME_MAX)] | None = None
    password: Annotated[str, Field(min_length=1, max_length=PASSWORD_MAX)] | None = None
    inbox_folder: str | None = None
    processed_folder: str | None = None
    rejected_folder: str | None = None
    allowed_senders: (
        Annotated[list[AllowedSender], Field(max_length=MAX_ALLOWED_SENDERS)] | None
    ) = None
    member_addresses: bool | None = None
    require_auth_results: bool | None = None
    authserv_id: Annotated[str, Field(max_length=HOST_MAX)] | None = None

    @field_validator("host")
    @classmethod
    def _host(cls, value: str | None) -> str | None:
        return None if value is None else clean_host(value)

    @field_validator("username", "password")
    @classmethod
    def _printable(cls, value: str | None) -> str | None:
        if value is not None and not value.isprintable():
            raise ValueError("must be printable text")
        return value

    @field_validator("inbox_folder", "processed_folder", "rejected_folder")
    @classmethod
    def _folder(cls, value: str | None) -> str | None:
        return None if value is None else clean_folder(value)

    @field_validator("authserv_id")
    @classmethod
    def _authserv(cls, value: str | None) -> str | None:
        if value is None:
            return None
        cleaned = value.strip().lower()
        if cleaned and not HOSTNAME_PATTERN.fullmatch(cleaned):
            raise ValueError("must be the host name of the receiving mail server")
        return cleaned


class MailIntakeOut(BaseModel):
    configured: bool
    instance_enabled: bool
    enabled: bool
    host: str | None
    port: int
    username: str | None
    password_set: bool
    inbox_folder: str
    processed_folder: str
    rejected_folder: str
    alias_token: str | None
    alias_rotated_at: datetime | None
    allowed_senders: list[AllowedSender]
    member_addresses: bool
    require_auth_results: bool
    authserv_id: str | None
    last_poll_at: datetime | None
    last_poll_outcome: str | None
    last_poll_counts: dict[str, Any]


class AliasOut(BaseModel):
    alias_token: str
    alias_rotated_at: datetime


class MailTestOut(BaseModel):
    ok: bool
    outcome: PollOutcome
    messages: int | None = None
    move_supported: bool | None = None
