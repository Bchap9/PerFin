# SPDX-License-Identifier: AGPL-3.0-or-later
"""Helpers shared by the extractors (run only inside the sandbox child)."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Final

from app.core.csv_safety import neutralise_formula
from app.ingestion.sandbox.protocol import MAX_ABS_AMOUNT, TEXT_MAX


class ExtractFailure(Exception):  # noqa: N818 - a result, not a programming error
    """The input cannot be extracted; ``code`` is machine-readable, ``message`` short.

    Messages never quote file content.
    """

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.message = message[:500]


#: Encodings tried in order. UTF-8 (BOM stripped), then Windows-1252 (a strict
#: superset of the printable Latin-1 range used by many bank exports), then
#: Latin-1, which decodes any byte string.
ENCODINGS: Final = ("utf-8-sig", "cp1252", "latin-1")

_BAD_CONTROL: Final = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")
_WS: Final = re.compile(r"\s+")


def decode_text(data: bytes, *, encodings: tuple[str, ...] = ENCODINGS) -> tuple[str, str]:
    """``(text, encoding)``; UTF-16/32 and text with NUL/control bytes are refused."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ExtractFailure("unsupported_encoding", "UTF-16/32 text is not supported")
    for encoding in encodings:
        try:
            text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        if _BAD_CONTROL.search(text):
            raise ExtractFailure("binary_content", "control characters in text")
        return text, "utf-8" if encoding == "utf-8-sig" else encoding
    raise ExtractFailure("unsupported_encoding", "text encoding not recognised")


def clean_text(value: str | None, *, limit: int = TEXT_MAX) -> str | None:
    """Collapse whitespace, cut to ``limit``, neutralise formulas (C7); empty -> None."""
    if value is None:
        return None
    text = _WS.sub(" ", value).strip()
    if not text:
        return None
    return neutralise_formula(text[:limit])


_US_THOUSANDS: Final = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d+)?")
_DECIMAL_COMMA: Final = re.compile(r"[+-]?\d+,\d{1,4}")
_PLAIN: Final = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")


def parse_amount(raw: str | None) -> Decimal | None:
    """A decimal amount from statement text (``1,234.56``, ``-12,34``, ``+5``) or None."""
    if raw is None:
        return None
    text = raw.strip().replace(" ", "")
    if _US_THOUSANDS.fullmatch(text):
        text = text.replace(",", "")
    elif _DECIMAL_COMMA.fullmatch(text):
        text = text.replace(",", ".")
    elif not _PLAIN.fullmatch(text):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:  # pragma: no cover - the patterns above only admit numbers
        return None
    if not value.is_finite() or abs(value) >= MAX_ABS_AMOUNT:
        return None
    return value


def last4(account_number: str | None) -> str | None:
    """The last four digits of an account number (the full number never leaves the child)."""
    if not account_number:
        return None
    digits = re.sub(r"\D", "", account_number)
    return digits[-4:] if len(digits) >= 4 else None
