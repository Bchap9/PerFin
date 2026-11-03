#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fail if a source file lacks the SPDX licence line in its first three lines.

Files considered: tracked plus untracked-but-not-ignored files reported by
git, filtered by extension (.py .ts .tsx .js .mjs .cjs .css .sh .yml .yaml)
or name (Dockerfile*, Makefile). Generated files are excluded below.
Standard library only.
"""

from __future__ import annotations

import argparse
import fnmatch
import shutil
import subprocess  # nosec B404
import sys
from pathlib import Path

IDENTIFIER = "SPDX-License-Identifier: AGPL-3.0-or-later"
HEADER_LINES = 3
EXTENSIONS = frozenset(
    {".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".css", ".sh", ".yml", ".yaml"}
)
NAMES = ("Dockerfile", "Dockerfile.*", "*.Dockerfile", "Makefile")

# Generated files (lockfiles are not matched by extension in the first place).
EXCLUDED = (
    "frontend/src/lib/api/schema.d.ts",
    "backend/migrations/versions/*.py",
)


def candidate_files(root: Path) -> list[str]:
    git = shutil.which("git")
    if git is None:
        raise SystemExit("check_spdx: git not found on PATH")
    # Fixed argv, no shell: the only variable is the resolved git binary.
    result = subprocess.run(  # noqa: S603  # nosec B603
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    paths = {p for p in result.stdout.decode("utf-8").split("\0") if p}
    return sorted(paths)


def is_source(path: str) -> bool:
    name = Path(path).name
    if any(fnmatch.fnmatch(name, pattern) for pattern in NAMES):
        return True
    return Path(path).suffix in EXTENSIONS


def is_excluded(path: str) -> bool:
    return any(fnmatch.fnmatch(path, pattern) for pattern in EXCLUDED)


def has_header(file: Path) -> bool:
    try:
        with file.open("r", encoding="utf-8", errors="replace") as handle:
            head = [handle.readline() for _ in range(HEADER_LINES)]
    except OSError:
        return False
    return any(IDENTIFIER in line for line in head)


def find_missing(root: Path) -> list[str]:
    missing = []
    for rel in candidate_files(root):
        if not is_source(rel) or is_excluded(rel):
            continue
        file = root / rel
        if not file.is_file():  # deleted in the working tree but still in the index
            continue
        if not has_header(file):
            missing.append(rel)
    return missing


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: this script's parent directory)",
    )
    args = parser.parse_args(argv)
    missing = find_missing(args.root)
    if missing:
        print(f"Missing '{IDENTIFIER}' in the first {HEADER_LINES} lines:", file=sys.stderr)
        for path in missing:
            print(f"  {path}", file=sys.stderr)
        return 1
    print("SPDX headers: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
