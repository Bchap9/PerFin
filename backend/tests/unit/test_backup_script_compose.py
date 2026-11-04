# SPDX-License-Identifier: AGPL-3.0-or-later
"""scripts/backup.sh in Compose mode needs only `db` running (G3).

The script tells operators to `docker compose stop api worker` for a
strictly consistent snapshot, but it used `docker compose exec` into `api`
for `backup-info`, the document tars and the encryption, so a backup of a
stopped stack failed with `service "api" is not running` (v1.0.0
verification run, docs/RELEASE_CHECKLIST.md). Everything except the
database commands must run in one-off containers (`compose run --rm`).
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts" / "backup.sh"


def _code_lines() -> list[str]:
    return [
        line
        for line in SCRIPT.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_backup_execs_only_into_db() -> None:
    execs = [line for line in _code_lines() if re.search(r"\bcompose\s+exec\b", line)]
    assert execs, "the database commands still use compose exec into db"
    offenders = [line for line in execs if not re.search(r"\bexec\s+-T\s+db\b", line)]
    assert offenders == [], offenders


def test_app_side_steps_use_one_off_containers() -> None:
    code = "\n".join(_code_lines())
    assert "compose run --rm --no-deps -T api" in code  # backup-info
    assert "compose run --rm --no-deps -T --entrypoint tar api" in code  # document tars
    assert "compose run --rm --no-deps -T -e PERFIN_BACKUP_PASSPHRASE api backup-encrypt" in code
