# SPDX-License-Identifier: AGPL-3.0-or-later
""":mod:`tests.fixtures.sandbox_evil` with Landlock capped at ABI 4 (tests only).

Stands in for common host kernels (Debian 12's 6.1 is ABI 2-3, Ubuntu
24.04's 6.8 is ABI 4) whose Landlock has no signal scoping (ABI 6+): there
only the seccomp filter keeps the child from signalling other processes
(review finding P2-SR-R2-01).
"""

from __future__ import annotations

import sys

from app.ingestion.sandbox import child
from tests.fixtures.sandbox_evil import EXTRACTORS

_real_abi = child.landlock_abi


def _abi_at_most_4() -> int:
    return min(_real_abi(), 4)


if __name__ == "__main__":
    child.landlock_abi = _abi_at_most_4
    sys.exit(child.run(sys.argv[1:], EXTRACTORS))
