# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading an uploaded zip archive defensively (SECURITY.md C2, C3).

Every check runs on the central directory before a single member is
decompressed, and again while reading:

* the archive must parse as a zip; the members' compressed sizes cannot add
  up to more than the archive itself (overlapping members);
* at most :attr:`ZipLimits.max_members` members, none above
  :attr:`ZipLimits.max_member_bytes` uncompressed, all together at most
  :attr:`ZipLimits.max_total_bytes`, and no member above
  :attr:`ZipLimits.ratio_floor` bytes that expands more than
  :attr:`ZipLimits.max_ratio` times (zip bombs);
* names are relative POSIX paths without ``..``, ``.``, empty parts,
  backslashes, drive letters, NUL or control characters, at most 512
  characters; no directories, no duplicates, no symbolic links, no
  encrypted members, only stored or deflated members;
* reading a member stops at its declared size and checks it was reached
  exactly (``zipfile`` checks the CRC at the end).

Nothing is ever written to disk under a member's name: the importer maps
known names to entities and stores document bytes under new random keys.
"""

from __future__ import annotations

import io
import itertools
import stat
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Final

from app.domain.portability.codec import ArchiveInvalidError

NAME_MAX: Final = 512
_READ_CHUNK: Final = 64 * 1024
_ALLOWED_COMPRESSION: Final = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})


@dataclass(frozen=True)
class ZipLimits:
    max_members: int = 100_000
    max_member_bytes: int = 1024 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024 * 1024
    max_ratio: int = 200
    ratio_floor: int = 1024 * 1024


def unsafe(detail: str) -> ArchiveInvalidError:
    return ArchiveInvalidError("archive-unsafe", detail)


def check_member_name(name: str) -> str:
    """``name`` if it is a safe relative member path (module doc), else refuse."""
    if not name or len(name) > NAME_MAX:
        raise unsafe("a member name is empty or too long")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in name):
        raise unsafe("a member name contains control characters")
    if "\\" in name:
        raise unsafe("a member name contains a backslash")
    if name.startswith("/"):
        raise unsafe("a member name is an absolute path")
    parts = name.split("/")
    if ":" in parts[0]:
        raise unsafe("a member name starts with a drive or scheme")
    if any(part in ("", ".", "..") for part in parts):
        raise unsafe("a member name contains an empty, '.' or '..' component")
    return name


def _name_bytes(info: zipfile.ZipInfo) -> int:
    """Length of the member name as stored (UTF-8 when flagged, else CP437)."""
    encoding = "utf-8" if info.flag_bits & 0x800 else "cp437"
    return len(info.orig_filename.encode(encoding, "replace"))


class SafeZip:
    """A zip archive held in memory that passed the module's checks."""

    def __init__(self, data: bytes, limits: ZipLimits | None = None) -> None:
        self.limits = limits or ZipLimits()
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
            infos = self._zip.infolist()
        except (zipfile.BadZipFile, zipfile.LargeZipFile, ValueError, OSError):
            raise ArchiveInvalidError(
                "archive-not-zip", "the file is not a valid zip archive"
            ) from None
        if len(infos) > self.limits.max_members:
            raise unsafe("the archive has too many members")
        self._infos: dict[str, zipfile.ZipInfo] = {}
        total = 0
        compressed = 0
        for info in infos:
            name = check_member_name(info.filename)
            if name in self._infos:
                raise unsafe("the archive has duplicate member names")
            if info.is_dir():
                raise unsafe("the archive contains a directory entry")
            mode = info.external_attr >> 16
            if mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                raise unsafe("the archive contains a link or special file")
            if info.flag_bits & 0x1:
                raise unsafe("the archive contains an encrypted member")
            if info.compress_type not in _ALLOWED_COMPRESSION:
                raise unsafe("the archive uses an unsupported compression method")
            if info.file_size > self.limits.max_member_bytes:
                raise unsafe("a member is too large")
            if (
                info.file_size > self.limits.ratio_floor
                and info.file_size > self.limits.max_ratio * max(info.compress_size, 1)
            ):
                raise unsafe("a member expands too much (compression ratio)")
            total += info.file_size
            compressed += info.compress_size
            if total > self.limits.max_total_bytes:
                raise unsafe("the archive expands beyond the total size limit")
            self._infos[name] = info
        if compressed > len(data):
            raise unsafe("the archive's members overlap")
        # Each member's local header and data must end before the next member
        # starts (overlapping members are a known zip-bomb construction).
        spans = sorted(
            (
                info.header_offset,
                info.header_offset + 30 + _name_bytes(info) + info.compress_size,
            )
            for info in self._infos.values()
        )
        for (_, end), (start, _) in itertools.pairwise(spans):
            if start < end:
                raise unsafe("the archive's members overlap")

    @property
    def names(self) -> list[str]:
        return list(self._infos)

    def size(self, name: str) -> int:
        return self._infos[name].file_size

    def chunks(self, name: str) -> Iterator[bytes]:
        """The member's bytes, never more than its declared size."""
        info = self._infos[name]
        remaining = info.file_size
        try:
            with self._zip.open(info) as handle:
                while True:
                    chunk = handle.read(min(_READ_CHUNK, remaining + 1))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    if remaining < 0:
                        raise unsafe("a member is larger than declared")
                    yield chunk
        except (zipfile.BadZipFile, OSError, EOFError, ValueError):
            raise ArchiveInvalidError("archive-not-zip", "a member is damaged") from None
        if remaining != 0:
            raise ArchiveInvalidError("archive-not-zip", "a member is shorter than declared")

    def read(self, name: str, *, limit: int) -> bytes:
        """The whole member, refusing one above ``limit`` bytes."""
        if self._infos[name].file_size > limit:
            raise unsafe("a member is too large")
        return b"".join(self.chunks(name))
