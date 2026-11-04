#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Write synthetic (invented) bank statements for the dev seed and manual testing.

Usage: ``python3 scripts/synth/statements.py --out /tmp/statements [--seed N] [--days N]``

Writes one file per CSV/TSV layout and one each of OFX (SGML and XML), QFX
and QIF, plus ``manifest.json`` (account numbers, layouts and the column
mapping that reads each CSV). Every institution, merchant, account number and
amount is generated; nothing real is involved. The generator is
``backend/tests/fixtures/synth_statements.py`` (standard library only), the
same code the import tests use.
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "backend"


def run(argv: list[str]) -> int:
    sys.path.insert(0, str(BACKEND))
    from tests.fixtures.synth_statements import main

    return main(argv)


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
