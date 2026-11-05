# SPDX-License-Identifier: AGPL-3.0-or-later
"""ZIP extractor: members as child documents, depth 1 (SECURITY.md C2, C3, D4).

Runs only inside the sandbox child, on the archive's bytes in memory.
Everything is checked on the central directory **before** a member is
decompressed, and again while reading:

* an archive being extracted at depth ``>= options.max_depth`` (a ZIP that
  came out of a ZIP or a mail) is not expanded: ``ok=false`` with code
  ``depth_limit`` (the pipeline sends it to review);
* at most :attr:`ExtractOptions.max_children` entries (``too_many_members``);
  the declared sizes together at most ``max_children_total_bytes``
  (``zip_too_large``); a member above 1 MiB may not expand more than
  ``max_zip_ratio`` times (``zip_bomb``); members may not overlap each other
  (a known bomb construction, ``zip_bomb``); the archive is refused as a
  whole in each case;
* members above ``max_child_bytes``, encrypted members, links and special
  files, compression methods other than stored and deflate, directories and
  macOS resource forks (``__MACOSX/``) are skipped and reported;
* reading a member stops one byte past its declared size and must end
  exactly there (``zipfile`` checks the CRC).

Member names are **metadata only** (C3): the last path component, printable,
at most 255 characters. Nothing is written anywhere; the bytes go back to
the worker as :class:`ChildDocument` entries at depth ``options.depth + 1``.
"""

from __future__ import annotations

import io
import itertools
import stat
import zipfile
from typing import Final

from app.ingestion.extract.common import ExtractFailure, metadata_name
from app.ingestion.extract.registry import register
from app.ingestion.sandbox.protocol import (
    ChildDocument,
    ExtractError,
    ExtractOptions,
    ExtractResult,
)

EXTRACTOR_VERSION: Final = "zip-1"
RATIO_FLOOR: Final = 1024 * 1024
_ALLOWED: Final = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
_CHUNK: Final = 64 * 1024


def _name_bytes(info: zipfile.ZipInfo) -> int:
    encoding = "utf-8" if info.flag_bits & 0x800 else "cp437"
    return len(info.orig_filename.encode(encoding, "replace"))


def _read(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    out = bytearray()
    with archive.open(info) as handle:
        while True:
            chunk = handle.read(min(_CHUNK, info.file_size + 1 - len(out)) or 1)
            if not chunk:
                break
            out += chunk
            if len(out) > info.file_size:
                raise ExtractFailure("zip_bomb", "a member is larger than declared")
    if len(out) != info.file_size:
        raise ExtractFailure("malformed_zip", "a member is shorter than declared")
    return bytes(out)


def extract_zip(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    if options.depth >= options.max_depth:
        return ExtractResult(
            kind=kind,
            ok=False,
            extractor_version=EXTRACTOR_VERSION,
            error=ExtractError(code="depth_limit", message="nested archives are not expanded"),
        )
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        infos = archive.infolist()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError, EOFError) as exc:
        raise ExtractFailure("malformed_zip", "the archive cannot be read") from exc
    if len(infos) > options.max_children:
        raise ExtractFailure("too_many_members", "the archive has too many entries")
    declared = sum(info.file_size for info in infos)
    if declared > options.max_children_total_bytes:
        raise ExtractFailure("zip_too_large", "the archive expands beyond the size cap")
    if sum(info.compress_size for info in infos) > len(data):
        raise ExtractFailure("zip_bomb", "the archive's members overlap")
    for info in infos:
        if info.file_size > RATIO_FLOOR and info.file_size > options.max_zip_ratio * max(
            info.compress_size, 1
        ):
            raise ExtractFailure("zip_bomb", "a member expands too much")
    spans = sorted(
        (info.header_offset, info.header_offset + 30 + _name_bytes(info) + info.compress_size)
        for info in infos
    )
    for (_, end), (start, _) in itertools.pairwise(spans):
        if start < end:
            raise ExtractFailure("zip_bomb", "the archive's members overlap")

    warnings_out: list[str] = []
    children: list[ChildDocument] = []

    def skip(code: str) -> None:
        if code not in warnings_out:
            warnings_out.append(code)

    for info in infos:
        name = info.filename
        if info.is_dir():
            continue
        parts = [p for p in name.replace("\\", "/").split("/") if p]
        if not parts or parts[0] == "__MACOSX" or parts[-1].startswith("._"):
            skip("resource_fork_skipped")
            continue
        mode = info.external_attr >> 16
        if mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG):
            skip("link_skipped")
            continue
        if info.flag_bits & 0x1:
            skip("encrypted_member_skipped")
            continue
        if info.compress_type not in _ALLOWED:
            skip("compression_skipped")
            continue
        if info.file_size > options.max_child_bytes:
            skip("member_too_large")
            continue
        if info.file_size == 0:
            skip("empty_member_skipped")
            continue
        try:
            content = _read(archive, info)
        except (zipfile.BadZipFile, OSError, EOFError, ValueError) as exc:
            raise ExtractFailure("malformed_zip", "a member is damaged") from exc
        children.append(
            ChildDocument(
                origin="zip_member",
                name=metadata_name(name),
                size=len(content),
                depth=options.depth + 1,
                data=content,
            )
        )
    return ExtractResult(
        kind=kind,
        ok=True,
        extractor_version=EXTRACTOR_VERSION,
        children=children,
        warnings=warnings_out,
    )


register("zip", extract_zip)
