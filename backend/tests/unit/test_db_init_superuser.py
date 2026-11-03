# SPDX-License-Identifier: AGPL-3.0-or-later
"""deploy/db-init/20-perfin-superuser-local.sh (SECURITY.md H3).

The bootstrap superuser must be refused on every TCP connection. The script
prepends ``reject`` lines to pg_hba.conf (first match wins). The live check
against a real postgres container is part of the deploy verification; here
the file transformation is tested directly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "db-init" / "20-perfin-superuser-local.sh"

IMAGE_HBA = """\
# TYPE  DATABASE        USER            ADDRESS                 METHOD
local   all             all                                     scram-sha-256
host    all             all             127.0.0.1/32            scram-sha-256
host    replication     all             127.0.0.1/32            scram-sha-256

host all all all scram-sha-256
"""

BASH = shutil.which("bash")


def run(pgdata: Path, **env: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "PGDATA": str(pgdata), **env}
    return subprocess.run(
        [BASH, str(SCRIPT)], env=environment, capture_output=True, text=True, check=False
    )


def rules(text: str) -> list[list[str]]:
    return [line.split() for line in text.splitlines() if line and not line.startswith("#")]


@pytest.fixture
def pgdata(tmp_path: Path) -> Path:
    (tmp_path / "pg_hba.conf").write_text(IMAGE_HBA, encoding="utf-8")
    (tmp_path / "pg_hba.conf").chmod(0o600)
    return tmp_path


def test_script_is_executable() -> None:
    assert os.access(SCRIPT, os.X_OK)


def test_superuser_rejected_before_any_host_rule(pgdata: Path) -> None:
    result = run(pgdata, POSTGRES_USER="postgres")
    assert result.returncode == 0, result.stderr
    hba = (pgdata / "pg_hba.conf").read_text(encoding="utf-8")
    parsed = rules(hba)
    assert parsed[0] == ["host", "all", "postgres", "all", "reject"]
    assert parsed[1] == ["host", "replication", "postgres", "all", "reject"]
    # Everything the image had is kept, after the reject lines.
    assert hba.endswith(IMAGE_HBA)
    # The local socket rule is untouched, so administration still works.
    assert ["local", "all", "all", "scram-sha-256"] in parsed
    assert (pgdata / "pg_hba.conf").stat().st_mode & 0o777 == 0o600


def test_rerun_is_idempotent(pgdata: Path) -> None:
    assert run(pgdata, POSTGRES_USER="postgres").returncode == 0
    first = (pgdata / "pg_hba.conf").read_text(encoding="utf-8")
    assert run(pgdata, POSTGRES_USER="postgres").returncode == 0
    assert (pgdata / "pg_hba.conf").read_text(encoding="utf-8") == first


def test_custom_superuser_name(pgdata: Path) -> None:
    assert run(pgdata, POSTGRES_USER="pgadmin").returncode == 0
    parsed = rules((pgdata / "pg_hba.conf").read_text(encoding="utf-8"))
    assert parsed[0] == ["host", "all", "pgadmin", "all", "reject"]


@pytest.mark.parametrize("name", ["bad name", "x;y", "Postgres", ""])
def test_unsafe_superuser_name_refused(pgdata: Path, name: str) -> None:
    result = run(pgdata, POSTGRES_USER=name)
    if name == "":
        # Empty falls back to the image default.
        assert result.returncode == 0
        return
    assert result.returncode != 0
    assert (pgdata / "pg_hba.conf").read_text(encoding="utf-8") == IMAGE_HBA


def test_symlinked_hba_refused(tmp_path: Path) -> None:
    target = tmp_path / "elsewhere"
    target.write_text(IMAGE_HBA, encoding="utf-8")
    pgdata = tmp_path / "data"
    pgdata.mkdir()
    (pgdata / "pg_hba.conf").symlink_to(target)
    assert run(pgdata, POSTGRES_USER="postgres").returncode != 0
    assert target.read_text(encoding="utf-8") == IMAGE_HBA
