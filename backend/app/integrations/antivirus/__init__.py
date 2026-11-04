# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional antivirus scanning of quarantined documents (SECURITY.md C9).

:class:`Scanner` is what the ingestion pipeline's ``scan`` stage calls; the
shipped implementation is the ClamAV sidecar client
(:mod:`app.integrations.antivirus.clamav`). :func:`scanner_from_settings`
returns ``None`` when ``PERFIN_ANTIVIRUS=none``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, Protocol

if TYPE_CHECKING:
    from app.core.settings import Settings

ScanStatus = Literal["clean", "infected", "error"]

#: Characters kept from a signature name (it is scanner output, shown to people).
_SIGNATURE_CHARS: Final = re.compile(r"[^A-Za-z0-9_.:/+()-]")
SIGNATURE_MAX: Final = 200


def clean_signature(raw: str) -> str:
    """A signature name safe to store and display: allowlisted characters, bounded."""
    text = _SIGNATURE_CHARS.sub("_", raw.strip())[:SIGNATURE_MAX]
    return text or "unknown"


@dataclass(frozen=True)
class ScanResult:
    """What a scan found. ``error`` carries a stable code, never file content."""

    status: ScanStatus
    signature: str | None = None
    error: str | None = None
    #: Whether trying again later may succeed (scanner down) or not (file too large).
    retryable: bool = False

    @property
    def infected(self) -> bool:
        return self.status == "infected"


class Scanner(Protocol):
    @property
    def name(self) -> str: ...

    def scan(self, chunks: Iterable[bytes], *, size: int) -> ScanResult: ...


def scanner_from_settings(settings: Settings) -> Scanner | None:
    """The configured scanner, or ``None`` when antivirus is off."""
    if settings.antivirus != "clamav":
        return None
    from app.integrations.antivirus.clamav import ClamdScanner

    return ClamdScanner.from_settings(settings)
