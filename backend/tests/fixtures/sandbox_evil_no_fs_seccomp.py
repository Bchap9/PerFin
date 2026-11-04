# SPDX-License-Identifier: AGPL-3.0-or-later
""":mod:`tests.fixtures.sandbox_evil` without the seccomp filesystem rules (tests only).

Shows that Landlock alone stops the filesystem probes (see
:mod:`tests.fixtures.sandbox_evil_no_landlock` for the converse).
"""

from __future__ import annotations

import sys
from types import ModuleType

from app.ingestion.sandbox import child
from tests.fixtures.sandbox_evil import EXTRACTORS


def _allow_filesystem_changes(filt: object, pyseccomp: ModuleType) -> None:
    """Allow what :func:`child.deny_filesystem_changes` refuses (the filter is an allowlist)."""
    add_rule = filt.add_rule  # type: ignore[attr-defined]
    for name in child.FILESYSTEM_DENIED_SYSCALLS:
        if pyseccomp.resolve_syscall(pyseccomp.Arch.NATIVE, name) != -1:
            add_rule(pyseccomp.ALLOW, name)
    for syscall, flags_arg in (("open", 1), ("openat", 2)):
        for flag in child.WRITE_OPEN_FLAGS:
            add_rule(
                pyseccomp.ALLOW, syscall, pyseccomp.Arg(flags_arg, pyseccomp.MASKED_EQ, flag, flag)
            )


if __name__ == "__main__":
    child.deny_filesystem_changes = _allow_filesystem_changes
    sys.exit(child.run(sys.argv[1:], EXTRACTORS))
