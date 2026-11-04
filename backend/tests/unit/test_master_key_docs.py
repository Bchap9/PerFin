# SPDX-License-Identifier: AGPL-3.0-or-later
"""The operator procedures for the master key keep one key (SECURITY.md G2, G4).

docs/DEPLOYMENT.md section 3 once told operators to back up the key in
``.env`` and then to create the key file with ``openssl rand``: a new, different
key, so the key they had backed up no longer opened the instance, and applied
after first boot every encrypted value became unreadable (P7-SR-R1-02). The
key-file recipe is run here against a synthetic ``.env`` and must produce a
file holding exactly the configured key. The Compose key-rotation procedure in
docs/BACKUP_RESTORE.md must cover a key held in PERFIN_MASTER_KEY_FILE and keep
the old file until the rotation is verified (P7-SR-R1-07).
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import subprocess  # nosec B404 - runs a documented shell recipe in a temp dir
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
DEPLOYMENT = ROOT / "docs" / "DEPLOYMENT.md"
BACKUP_RESTORE = ROOT / "docs" / "BACKUP_RESTORE.md"

_FENCE = re.compile(r"```sh\n(.*?)```", re.DOTALL)


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    following = re.search(r"^## ", text[start + len(heading) :], re.MULTILINE)
    end = start + len(heading) + following.start() if following else len(text)
    return text[start:end]


def _master_key_section() -> str:
    return _section(DEPLOYMENT.read_text(encoding="utf-8"), "## 3. The master key")


def _key_file_recipe() -> str:
    blocks = [b for b in _FENCE.findall(_master_key_section()) if "secrets/master.key" in b]
    assert len(blocks) == 1, "DEPLOYMENT.md section 3 must hold one key-file recipe"
    recipe: str = blocks[0]
    return recipe


def test_key_file_recipe_never_generates_a_key() -> None:
    recipe = _key_file_recipe()
    assert "openssl rand" not in recipe
    assert not re.search(r"rand\b.*>\s*secrets/master\.key", recipe)


def test_key_file_recipe_writes_the_configured_key(tmp_path: Path) -> None:
    bash = shutil.which("bash")
    if bash is None:
        pytest.fail("bash is required to run the documented recipe")
    key = base64.b64encode(os.urandom(32)).decode("ascii")  # synthetic key
    env = tmp_path / ".env"
    env.write_text(
        "PERFIN_SECRET_KEY=synthetic\n"
        f"PERFIN_MASTER_KEY={key}\n"
        "PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED=false\n",
        encoding="utf-8",
    )
    # sudo chown/chmod need root; the file's ownership is not under test here.
    script = "\n".join(
        line for line in _key_file_recipe().splitlines() if not line.lstrip().startswith("sudo ")
    )
    result = subprocess.run(  # fixed interpreter, recipe from the repo's own docs
        [bash, "-euo", "pipefail", "-c", script],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "stop" not in result.stdout
    assert (tmp_path / "secrets" / "master.key").read_text(encoding="ascii").strip() == key
    remaining = env.read_text(encoding="utf-8")
    assert not re.search(r"^PERFIN_MASTER_KEY=", remaining, re.MULTILINE)
    assert "PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED=false" in remaining


def test_key_file_comes_before_the_backup_acknowledgement() -> None:
    section = _master_key_section()
    assert section.index("secrets/master.key") < section.index(
        "PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED=true"
    )
    assert "perfin rotate-key" in section
    assert "never changes after\nfirst boot" in section


def test_rotation_procedure_covers_the_key_file() -> None:
    text = BACKUP_RESTORE.read_text(encoding="utf-8")
    procedure = _section(text, "### Procedure (Docker Compose)")
    assert "PERFIN_MASTER_KEY_FILE" in procedure
    assert "secrets/master.key.old" in procedure
    (block,) = _FENCE.findall(procedure)[:1]
    keep = block.index("cp -p secrets/master.key secrets/master.key.old")
    replace = block.index("new-master.key secrets/master.key")
    status = block.index("rotate-key --status")
    backup = block.index("scripts/backup.sh --compose-dir deploy")
    remove_old = block.index("shred -u deploy/secrets/master.key.old")
    assert keep < replace < status < backup < remove_old
    assert "PERFIN_MASTER_KEY_VERSION" in block
