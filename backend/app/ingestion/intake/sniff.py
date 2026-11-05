# SPDX-License-Identifier: AGPL-3.0-or-later
"""Magic-byte sniffing of received files (INGESTION.md "Receive", SECURITY.md C4/C5).

:func:`sniff` looks only at the first :data:`SNIFF_BYTES` of a file; the
declared content type and the file name extension are never consulted. It
does not parse anything (ADR-0006): fixed signatures for binary formats and
byte-level heuristics for text formats. The result is one of
:class:`FileType` (stored as ``document.mime_detected``) or a
:class:`Rejected` reason.

Accepted: PDF, PNG, JPEG, HEIC/HEIF, TIFF, ZIP (XLSX when the first entries
look like a workbook), OFX (SGML v1 and XML v2), QFX, QIF, EML, CSV, TSV and
plain text. Rejected, whatever they are called:

* **SVG and HTML** (C5): anything whose text starts like markup the browser
  would render (the WHATWG "scriptable" sniffing patterns: ``<!DOCTYPE
  html``, ``<html``, ``<script``, ``<svg``, ``<iframe`` ...) and XML that is
  not OFX;
* **polyglots** (C4): an accepted type whose sniffed window also carries
  active markup (``<script``, ``<html``, ``<svg``, ``<iframe``, ``<object``,
  ``<embed``, ``javascript:``). For the **text** types (:data:`TEXT_TYPES`:
  CSV, TSV, OFX, QFX, QIF, plain text) the whole file is scanned, not just
  the window (:class:`MarkupScanner`, run by the upload intake while it
  hashes and encrypts; review finding P2-SR-04). Binary types (PDF, images,
  ZIP) are checked in the window only: their compressed bytes would match
  these short patterns by chance, and a browser looks for HTML only at the
  start of a file. What makes a stored file harmless in the browser is the
  download response (``Content-Disposition: attachment``, ``nosniff``, a
  sandboxing CSP; C4), not this check. EML is exempt: HTML bodies are
  normal mail and are converted to text by the extractor;
* binary content that matches no signature, text with NUL or other control
  bytes, UTF-16/32 text, and empty files.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass
from typing import Final

#: Bytes examined by :func:`sniff`.
SNIFF_BYTES: Final = 8192


class FileType(enum.StrEnum):
    """Sniffed type; the value is the MIME type stored in ``document.mime_detected``."""

    PDF = "application/pdf"
    PNG = "image/png"
    JPEG = "image/jpeg"
    HEIC = "image/heic"
    TIFF = "image/tiff"
    ZIP = "application/zip"
    XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    OFX = "application/x-ofx"
    QFX = "application/vnd.intu.qfx"
    QIF = "application/qif"
    EML = "message/rfc822"
    CSV = "text/csv"
    TSV = "text/tab-separated-values"
    TEXT = "text/plain"

    @property
    def extract_kind(self) -> str | None:
        """The sandbox extractor kind for this type, else None (no extractor yet)."""
        return _EXTRACT_KINDS.get(self)


_STRUCTURED_KINDS: Final[dict[FileType, str]] = {
    FileType.CSV: "csv",
    FileType.TSV: "tsv",
    FileType.OFX: "ofx",
    FileType.QFX: "qfx",
    FileType.QIF: "qif",
    # XLSX: worksheets as tables (values only, formulas never evaluated).
    FileType.XLSX: "xlsx",
}
_EXTRACT_KINDS: Final[dict[FileType, str]] = {
    **_STRUCTURED_KINDS,
    # Phases 3-4: documents (PDF text layer and OCR, images), mail, archives.
    FileType.PDF: "pdf",
    FileType.PNG: "png",
    FileType.JPEG: "jpeg",
    FileType.TIFF: "tiff",
    FileType.HEIC: "heic",
    FileType.EML: "eml",
    FileType.ZIP: "zip",
}

#: Structured formats (records the structured importer reads directly).
STRUCTURED_TYPES: Final = frozenset(_STRUCTURED_KINDS)
#: Text types scanned for active markup over the whole file (module doc).
TEXT_TYPES: Final = frozenset(
    {FileType.CSV, FileType.TSV, FileType.OFX, FileType.QFX, FileType.QIF, FileType.TEXT}
)


class RejectReason(enum.StrEnum):
    EMPTY = "empty"
    MARKUP = "markup"
    POLYGLOT = "polyglot"
    BINARY_UNKNOWN = "binary_unknown"
    TEXT_ENCODING = "text_encoding"
    XML_NOT_OFX = "xml_not_ofx"


@dataclass(frozen=True)
class Rejected:
    reason: RejectReason


_HEIF_BRANDS: Final = frozenset(
    {b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1", b"avif"}
)

# WHATWG MIME sniffing "scriptable" patterns, plus SVG, matched at the start of
# the text after whitespace (case-insensitive), followed by a tag terminator.
_MARKUP_START: Final = re.compile(
    rb"^<(?:!doctype\s+html|html|head|script|iframe|h1|div|font|table|a|style|title|b|body"
    rb"|br|p|svg|math|object|embed|meta|link|base|form|img|frameset|xsl:stylesheet)"
    rb"(?=[\s>/])|^<!--",
    re.IGNORECASE,
)
# Active content anywhere in the window turns an accepted type into a polyglot.
_ACTIVE_MARKUP: Final = re.compile(
    rb"<(?:script|html|svg|iframe|object|embed)(?=[\s>/])|javascript:", re.IGNORECASE
)
#: Bytes kept between chunks so a pattern split across two chunks is still found
#: (longer than the longest pattern, ``javascript:``, and ``<object`` plus terminator).
_MARKUP_OVERLAP: Final = 16


class MarkupScanner:
    """Finds :data:`_ACTIVE_MARKUP` anywhere in a stream fed chunk by chunk."""

    def __init__(self) -> None:
        self._tail = b""
        self.found = False

    def feed(self, chunk: bytes) -> bool:
        """Scan ``chunk`` (with the end of the previous one); return whether markup was seen."""
        if not self.found:
            window = self._tail + chunk
            self.found = _ACTIVE_MARKUP.search(window) is not None
            self._tail = window[-_MARKUP_OVERLAP:]
        return self.found


_XML_DECL: Final = re.compile(rb"^<\?xml[\s?]", re.IGNORECASE)
_OFX_PI: Final = re.compile(rb"<\?OFX[\s?]", re.IGNORECASE)
_OFX_SGML_HEADER: Final = re.compile(rb"^OFXHEADER\s*:\s*\d+", re.IGNORECASE)
_OFX_TAG: Final = re.compile(rb"<OFX>", re.IGNORECASE)
_QFX_MARKER: Final = re.compile(rb"<INTU\.BID>", re.IGNORECASE)
_QIF_HEADER: Final = re.compile(rb"^!(?:Type|Account|Option|Clear)\s*:?", re.IGNORECASE)
_EML_HEADER: Final = re.compile(
    rb"^(?:Received|Return-Path|From|To|Subject|Date|Message-ID|MIME-Version|Delivered-To"
    rb"|DKIM-Signature|Authentication-Results|X-[A-Za-z0-9-]+|Reply-To|Content-Type|Cc"
    rb"|ARC-[A-Za-z-]+|Sender|In-Reply-To|References)\s*:",
    re.IGNORECASE,
)
_EML_REQUIRED: Final = (b"from:", b"date:", b"subject:", b"message-id:", b"received:")
# Control bytes refused in text (TAB, LF, CR, FF and SUB, an old DOS EOF, are allowed).
_TEXT_CONTROL: Final = re.compile(rb"[\x00-\x08\x0b\x0e-\x19\x1b-\x1f\x7f]")


def _strip_bom_ws(head: bytes) -> bytes:
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    return head.lstrip(b" \t\r\n\x0c")


def _text_rejection(head: bytes) -> RejectReason | None:
    """Why ``head`` is not acceptable text (None when it is).

    UTF-8 and the single-byte Windows-1252/Latin-1 family are accepted (the
    extractors decode them); UTF-16/32 and anything with NUL or C0 control
    bytes other than TAB, LF, CR, FF and SUB is not.
    """
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        return RejectReason.TEXT_ENCODING
    if b"\x00" in head or _TEXT_CONTROL.search(head):
        return RejectReason.BINARY_UNKNOWN
    return None


def _zip_type(head: bytes) -> FileType:
    if b"[Content_Types].xml" in head and (b"xl/" in head or b"xl\\" in head):
        return FileType.XLSX
    return FileType.ZIP


def _binary_type(head: bytes) -> FileType | None:
    if head.startswith(b"%PDF-"):
        return FileType.PDF
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return FileType.PNG
    if head.startswith(b"\xff\xd8\xff"):
        return FileType.JPEG
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return FileType.TIFF
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return FileType.HEIC
    if head.startswith(b"PK\x03\x04"):
        return _zip_type(head)
    return None


def _rows_then_notes(counts: list[int]) -> bool:
    """Rows of three columns or more followed only by notes (``counts``: delimiters per line).

    A brokerage export ends a few rows with disclaimer paragraphs and a
    "Date downloaded" line whose delimiters (none, or the commas of a quoted
    sentence) are not the table's. The first line sets the row shape; the
    lines of that shape must be a leading block of most lines, two at least,
    and every other line must come after it.
    """
    first = counts[0]
    if first < 2:
        return False
    tolerance = max(2, first // 2)
    rows = [abs(c - first) <= tolerance for c in counts]
    block = rows.index(False) if False in rows else len(rows)
    return block >= max(2, (len(counts) + 1) // 2) and not any(rows[block:])


def _delimited_type(text: bytes, *, window_full: bool) -> FileType:
    """CSV/TSV when most of the first non-empty lines carry the same delimiter count."""
    lines = text.splitlines()
    if window_full and len(lines) > 1:
        lines = lines[:-1]  # the last line may be cut by the window
    lines = [line for line in lines[:20] if line.strip()]
    if not lines:
        return FileType.TEXT
    for delimiter, file_type in ((b"\t", FileType.TSV), (b",", FileType.CSV), (b";", FileType.CSV)):
        counts = [line.count(delimiter) for line in lines]
        # Quoted fields may hold the delimiter, so allow a little variation.
        if min(counts) >= 1 and max(counts) - min(counts) <= max(2, min(counts) // 2):
            return file_type
        if _rows_then_notes(counts):
            return file_type
    return FileType.TEXT


def _text_type(text: bytes, *, window_full: bool) -> FileType | Rejected:
    if _MARKUP_START.match(text):
        return Rejected(RejectReason.MARKUP)
    if _XML_DECL.match(text):
        if _OFX_PI.search(text):
            return FileType.QFX if _QFX_MARKER.search(text) else FileType.OFX
        return Rejected(RejectReason.XML_NOT_OFX)
    if _OFX_SGML_HEADER.match(text) or (text.startswith(b"<") and _OFX_TAG.search(text)):
        return FileType.QFX if _QFX_MARKER.search(text) else FileType.OFX
    if text.startswith(b"<"):
        return Rejected(RejectReason.MARKUP)
    if _QIF_HEADER.match(text):
        return FileType.QIF
    if _EML_HEADER.match(text):
        lowered = text.lower()
        if sum(marker in lowered for marker in _EML_REQUIRED) >= 2:
            return FileType.EML
    return _delimited_type(text, window_full=window_full)


def sniff(head: bytes) -> FileType | Rejected:
    """Classify a file from its first bytes (at most :data:`SNIFF_BYTES` are used)."""
    window_full = len(head) >= SNIFF_BYTES
    head = bytes(head[:SNIFF_BYTES])
    if not head.strip():
        return Rejected(RejectReason.EMPTY)
    detected: FileType | Rejected | None = _binary_type(head)
    if detected is None:
        reason = _text_rejection(head)
        if reason is not None:
            return Rejected(reason)
        detected = _text_type(_strip_bom_ws(head), window_full=window_full)
    if isinstance(detected, Rejected):
        return detected
    if detected is not FileType.EML and _ACTIVE_MARKUP.search(head):
        return Rejected(RejectReason.POLYGLOT)
    return detected
