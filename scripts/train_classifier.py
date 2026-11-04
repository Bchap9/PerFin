#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Train the heuristic document-kind classifier and write its artefact.

Usage (from ``backend/``): ``uv run python ../scripts/train_classifier.py [--check]``

Trains the naive Bayes model of ``app/ingestion/classify/model.py`` on the
synthetic corpus of ``backend/tests/fixtures/synth_classify.py`` (fixed seed,
no network, no real documents) and writes
``backend/app/ingestion/classify/artefacts/kind_nb_v1.json``. Training is
deterministic: ``--check`` exits 1 if the shipped artefact differs from a
fresh training run (the test suite checks the same).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
ARTEFACT = BACKEND / "app" / "ingestion" / "classify" / "artefacts" / "kind_nb_v1.json"


def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="compare, do not write")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(BACKEND))
    from tests.fixtures.synth_classify import build_artefact

    fresh = build_artefact()
    if args.check:
        current = ARTEFACT.read_text(encoding="utf-8") if ARTEFACT.exists() else ""
        if current != fresh:
            print(f"{ARTEFACT} is out of date; run scripts/train_classifier.py", file=sys.stderr)
            return 1
        print("classifier artefact is up to date")
        return 0
    ARTEFACT.parent.mkdir(parents=True, exist_ok=True)
    ARTEFACT.write_text(fresh, encoding="utf-8")
    print(f"wrote {ARTEFACT} ({len(fresh.encode())} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(run(sys.argv[1:]))
