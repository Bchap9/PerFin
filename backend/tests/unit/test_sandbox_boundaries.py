# SPDX-License-Identifier: AGPL-3.0-or-later
"""Parsing stays in the sandbox child (ADR-0006, SECURITY.md C1).

``tests/unit/test_import_boundaries.py`` keeps ``app/api`` and ``app/domain``
away from ``app.ingestion.extract``. This extends the rule to the code around
the sandbox: the API-side intake, the sandbox runner and protocol, the
pipeline and the worker handlers must not import the extractors either (only
``app/ingestion/sandbox/child.py`` may). A runtime check confirms that
building the API app and loading the whole worker path leaves
``app.ingestion.extract`` out of ``sys.modules``.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.unit.test_import_boundaries import APP_DIR, _module_name, imported_modules, violates

CHILD = APP_DIR / "ingestion" / "sandbox" / "child.py"


def _guarded() -> list[Path]:
    files: list[Path] = []
    for relative in ("ingestion/intake", "ingestion/sandbox", "workers"):
        files.extend(sorted((APP_DIR / relative).rglob("*.py")))
    files.append(APP_DIR / "ingestion" / "pipeline.py")
    files.append(APP_DIR / "main.py")
    return [path for path in files if path != CHILD]


@pytest.mark.parametrize("path", _guarded(), ids=lambda p: str(p.relative_to(APP_DIR)))
def test_no_extractor_imports_outside_the_child(path: Path) -> None:
    name = _module_name(path, APP_DIR)
    names = imported_modules(
        path.read_text(encoding="utf-8"), name, is_package=path.name == "__init__.py"
    )
    assert violates(names) == set(), f"{name} imports the extraction stage"


def test_child_is_the_one_that_does() -> None:
    names = imported_modules(CHILD.read_text(encoding="utf-8"), "app.ingestion.sandbox.child")
    assert violates(names)


def test_api_and_worker_processes_never_load_extractors() -> None:
    probe = (
        "import sys\n"
        "from app.core.settings import Settings\n"
        "from app.main import create_app\n"
        "create_app(Settings(dev_mode=True))\n"
        "import app.ingestion.intake.upload, app.ingestion.pipeline\n"
        "from app.workers.registry import discover_handlers\n"
        "discover_handlers()\n"
        "loaded = sorted(m for m in sys.modules if m.startswith('app.ingestion.extract'))\n"
        "print(','.join(loaded))\n"
    )
    proc = subprocess.run(
        [sys.executable, "-I", "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert proc.stdout.strip() == ""
