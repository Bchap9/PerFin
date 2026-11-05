# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export/import API models, job payloads and the archive manifest (docs/PORTABILITY.md)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, StringConstraints

from app.domain.portability.models import ArchiveState

EXPORT_JOB_TYPE: Final = "portability.export"
IMPORT_JOB_TYPE: Final = "portability.import"
EXPIRE_JOB_TYPE: Final = "portability.expire"

#: The archive format this code writes; :data:`SUPPORTED_FORMAT_VERSIONS` it reads.
ARCHIVE_FORMAT: Final = "perfin-export"
#: Version 2 added the Phase 3-6 tables (``EntitySpec.since``); version 1
#: archives are still imported (those tables are simply absent).
FORMAT_VERSION: Final = 2
SUPPORTED_FORMAT_VERSIONS: Final = frozenset({1, 2})

PASSPHRASE_MIN: Final = 12
PASSPHRASE_MAX: Final = 1024
#: Request header carrying the passphrase of an encrypted archive on ``POST /import``.
PASSPHRASE_HEADER: Final = "X-Archive-Passphrase"  # noqa: S105 - a header name


class ExportRequest(BaseModel):
    """``POST /export``: an optional passphrase to encrypt the archive with (G3)."""

    model_config = ConfigDict(extra="forbid")

    passphrase: (
        Annotated[SecretStr, Field(min_length=PASSPHRASE_MIN, max_length=PASSPHRASE_MAX)] | None
    ) = None


class ExportQueuedOut(BaseModel):
    job_id: uuid.UUID
    state: ArchiveState
    passphrase_protected: bool


class ExportOut(BaseModel):
    """An export job of the caller; ``counts`` per entity once ``ready``."""

    job_id: uuid.UUID
    state: ArchiveState
    passphrase_protected: bool
    size: int | None
    sha256: str | None
    created_at: datetime
    finished_at: datetime | None
    expires_at: datetime | None
    downloaded_at: datetime | None
    error: str | None
    counts: dict[str, int]


class ImportQueuedOut(BaseModel):
    job_id: uuid.UUID
    state: ArchiveState
    passphrase_protected: bool


class ImportOut(BaseModel):
    """An import job of the caller: created/merged/skipped counts, or why it failed."""

    job_id: uuid.UUID
    state: ArchiveState
    passphrase_protected: bool
    created_at: datetime
    finished_at: datetime | None
    error: str | None
    error_detail: str | None
    summary: dict[str, Any]


class ArchiveJob(BaseModel):
    """Payload of the portability jobs: the archive row (ids only, never secrets)."""

    model_config = ConfigDict(extra="forbid")

    archive_id: uuid.UUID


Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class ManifestMember(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: Sha256Hex
    size: Annotated[int, Field(ge=0)]


class ManifestHousehold(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, StringConstraints(max_length=200)]
    base_currency: Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class Manifest(BaseModel):
    """``manifest.json``: what the archive holds and the hash of every other member."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["perfin-export"]
    format_version: int
    app_version: Annotated[str, StringConstraints(max_length=64)]
    created_at: datetime
    household: ManifestHousehold
    counts: dict[Annotated[str, StringConstraints(max_length=64)], Annotated[int, Field(ge=0)]]
    members: dict[Annotated[str, StringConstraints(max_length=512)], ManifestMember]
