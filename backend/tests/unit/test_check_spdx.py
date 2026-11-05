# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/check_spdx.py: the licence-header gate itself."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "check_spdx.py"
HEADER = "# SPDX-License-Identifier: AGPL-3.0-or-later\n"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_spdx", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text("ignored/\n")
    return tmp_path


def _write(root: Path, rel: str, content: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def test_reports_files_without_header(repo: Path) -> None:
    check = _load()
    _write(repo, "ok.py", HEADER + "x = 1\n")
    _write(repo, "script.sh", "#!/usr/bin/env bash\n" + HEADER + "true\n")
    _write(repo, "web/app.tsx", "// SPDX-License-Identifier: AGPL-3.0-or-later\n")
    _write(repo, "web/style.css", "/* SPDX-License-Identifier: AGPL-3.0-or-later */\n")
    _write(repo, "bad.py", "x = 1\n")
    _write(repo, "late.yml", "a: 1\nb: 2\nc: 3\n" + HEADER)
    _write(repo, "Dockerfile", "FROM scratch\n")
    _write(repo, "Makefile", "all:\n")
    _write(repo, "wrong.ts", "// SPDX-License-Identifier: MIT\n")
    _write(repo, "notes.txt", "no header needed\n")
    _write(repo, "ignored/skip.py", "x = 1\n")
    _write(repo, "frontend/src/lib/api/schema.d.ts", "export {};\n")
    assert check.find_missing(repo) == ["Dockerfile", "Makefile", "bad.py", "late.yml", "wrong.ts"]
    assert check.main(["--root", str(repo)]) == 1


def test_clean_repo_passes(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    check = _load()
    _write(repo, "ok.py", HEADER)
    assert check.main(["--root", str(repo)]) == 0
    assert "OK" in capsys.readouterr().out
