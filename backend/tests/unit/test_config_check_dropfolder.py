# SPDX-License-Identifier: AGPL-3.0-or-later
"""Drop-folder configuration checks and the deploy files that set them up.

``app.core.config_check._check_dropfolder`` (INGESTION.md "Drop folder
specifics", SECURITY.md C3/C11 and the deployment checklist: inbox mounted
read-only, processed files moved to a sibling) and the compose mounts and
``.env.example`` defaults that must agree with it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core.config_check import check_config
from app.core.settings import Settings
from app.ingestion.intake.dropfolder import DropFolderConfig
from tests.unit.test_compose_hardening import env_example, environment, mounts
from tests.unit.test_config_check import clean_env, secure_env  # noqa: F401


@pytest.fixture
def base(request: pytest.FixtureRequest) -> Settings:
    request.getfixturevalue("secure_env")
    return Settings()


def _codes(settings: Settings) -> dict[str, str]:
    return {f.code: f.level for f in check_config(settings) if f.code.startswith("dropfolder.")}


def _dirs(tmp_path: Path) -> tuple[Path, Path]:
    inbox, processed = tmp_path / "inbox", tmp_path / "processed"
    inbox.mkdir()
    processed.mkdir()
    return inbox, processed


def _with(base: Settings, **update: Any) -> Settings:
    return base.model_copy(update=update)


def test_disabled_drop_folder_is_not_checked(base: Settings, tmp_path: Path) -> None:
    inbox, _ = _dirs(tmp_path)
    assert _codes(_with(base, dropfolder_processed=inbox / "processed", role="worker")) == {}


def test_processed_or_failed_inside_the_inbox_is_an_error(base: Settings, tmp_path: Path) -> None:
    inbox, processed = _dirs(tmp_path)
    inside = _with(
        base, dropfolder_enabled=True, dropfolder_inbox=inbox, dropfolder_processed=inbox / "done"
    )
    assert _codes(inside)["dropfolder.output_in_inbox"] == "error"
    failed_inside = _with(
        base,
        dropfolder_enabled=True,
        dropfolder_inbox=inbox,
        dropfolder_processed=processed,
        dropfolder_failed=inbox / "failed",
    )
    assert _codes(failed_inside)["dropfolder.output_in_inbox"] == "error"
    sibling = _with(
        base, dropfolder_enabled=True, dropfolder_inbox=inbox, dropfolder_processed=processed
    )
    assert "dropfolder.output_in_inbox" not in _codes(sibling)


def test_short_settle_time_without_sidecar_warns(base: Settings, tmp_path: Path) -> None:
    inbox, processed = _dirs(tmp_path)
    common = {
        "dropfolder_enabled": True,
        "dropfolder_inbox": inbox,
        "dropfolder_processed": processed,
    }
    assert _codes(_with(base, **common, dropfolder_settle_seconds=1)) == {
        "dropfolder.settle_short": "warning"
    }
    assert (
        _codes(_with(base, **common, dropfolder_settle_seconds=1, dropfolder_ready_sidecar=True))
        == {}
    )


def test_worker_checks_the_mounts(
    base: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inbox, processed = _dirs(tmp_path)
    common = {
        "dropfolder_enabled": True,
        "dropfolder_inbox": inbox,
        "dropfolder_processed": processed,
        "role": "worker",
    }
    assert _codes(_with(base, **common, dropfolder_after="move")) == {}
    assert _codes(_with(base, **common, dropfolder_after="keep")) == {
        "dropfolder.inbox_writable": "warning"
    }
    assert _codes(_with(base, **{**common, "dropfolder_inbox": tmp_path / "nope"})) == {
        "dropfolder.inbox_missing": "warning"
    }
    # The api has no mounts: nothing about them is reported there.
    assert (
        _codes(_with(base, **{**common, "role": "api", "dropfolder_inbox": tmp_path / "nope"}))
        == {}
    )

    import app.core.config_check as config_check

    real = config_check._dir_writable
    monkeypatch.setattr(config_check, "_dir_writable", lambda path: path != inbox and real(path))
    assert _codes(_with(base, **common, dropfolder_after="move")) == {
        "dropfolder.inbox_read_only": "warning"
    }
    assert _codes(_with(base, **common, dropfolder_after="keep")) == {}
    monkeypatch.setattr(config_check, "_dir_writable", lambda path: path == inbox)
    assert _codes(_with(base, **common, dropfolder_after="move")) == {
        "dropfolder.processed_not_writable": "warning"
    }


def test_failed_defaults_to_a_subfolder_of_processed(base: Settings) -> None:
    config = DropFolderConfig.from_settings(
        _with(base, dropfolder_processed=Path("/srv/processed"))
    )
    assert config.failed == Path("/srv/processed/failed")
    assert config.max_file_bytes == base.upload_max_bytes


def test_env_example_matches_the_read_only_inbox_mount(base: Settings) -> None:
    env = env_example()
    (inbox,) = [m for m in mounts("worker") if ":/var/lib/perfin/inbox" in m]
    # move/delete cannot work on a read-only inbox: the shipped default must be keep.
    assert inbox.endswith(":ro")
    assert env["PERFIN_DROPFOLDER_AFTER"] == "keep"
    assert env["PERFIN_DROPFOLDER_ENABLED"] == "true"
    worker = environment("worker")
    # failed/ lives under the writable processed/ mount (the only rw drop-folder mount).
    settings = _with(
        base,
        dropfolder_inbox=Path(worker["PERFIN_DROPFOLDER_INBOX"]),
        dropfolder_processed=Path(worker["PERFIN_DROPFOLDER_PROCESSED"]),
    )
    config = DropFolderConfig.from_settings(settings)
    assert config.failed.parent == config.processed


def test_every_drop_folder_variable_in_env_example_is_a_setting() -> None:
    fields = set(Settings.model_fields)
    names = [k for k in env_example() if k.startswith("PERFIN_DROPFOLDER_")]
    assert len(names) >= 8
    for name in names:
        assert name.removeprefix("PERFIN_").lower() in fields, name
