# SPDX-License-Identifier: AGPL-3.0-or-later
""":mod:`tests.fixtures.sandbox_evil` with the Landlock layer removed (tests only).

Shows that the remaining layers (the seccomp filesystem rules, the parent's
non-dumpable flag) stop a probe on their own: each sandbox layer is tested
alone, so removing any one of them makes a test fail.
"""

from __future__ import annotations

import sys

from app.ingestion.sandbox import child
from tests.fixtures.sandbox_evil import EXTRACTORS


def _no_landlock(*, required: bool = True) -> bool:
    return False


if __name__ == "__main__":
    child.install_landlock = _no_landlock
    sys.exit(child.run(sys.argv[1:], EXTRACTORS))
