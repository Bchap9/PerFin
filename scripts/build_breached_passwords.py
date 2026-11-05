#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Build ``backend/app/auth/data/breached-passwords.txt.gz`` from public lists.

Usage (inputs are downloaded separately, see
``backend/app/auth/data/README.md`` for the exact sources and checksums)::

    python3 -I scripts/build_breached_passwords.py OUT.txt.gz LIST [LIST ...]

Each input is a newline-separated password list. Only entries a PerFin user
could actually choose are kept (12 to 128 printable characters, valid UTF-8;
shorter ones are refused by the length rule anyway). Entries are lower-cased
(the check is case-insensitive), de-duplicated and sorted, one per line, and
written with gzip (no file name, fixed mtime) so the output is reproducible.
Standard library only.
"""

from __future__ import annotations

import argparse
import gzip
from pathlib import Path

MIN_LENGTH = 12
MAX_LENGTH = 128


def entries(path: Path) -> set[str]:
    out: set[str] = set()
    with path.open("rb") as handle:
        for raw in handle:
            try:
                line = raw.rstrip(b"\r\n").decode("utf-8")
            except UnicodeDecodeError:
                continue
            if MIN_LENGTH <= len(line) <= MAX_LENGTH and line.isprintable():
                out.add(line.lower())
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("output", type=Path)
    parser.add_argument("inputs", type=Path, nargs="+")
    args = parser.parse_args()
    merged: set[str] = set()
    for path in args.inputs:
        merged |= entries(path)
    data = ("\n".join(sorted(merged)) + "\n").encode("utf-8")
    # No file name and a fixed mtime in the gzip header: reproducible bytes.
    with (
        args.output.open("wb") as raw,
        gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=9, mtime=0) as handle,
    ):
        handle.write(data)
    print(f"{len(merged)} entries, {len(data)} bytes uncompressed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
