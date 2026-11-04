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
    return None


if __name__ == "__main__":
    child.deny_filesystem_changes = _allow_filesystem_changes
    sys.exit(child.run(sys.argv[1:], EXTRACTORS))
