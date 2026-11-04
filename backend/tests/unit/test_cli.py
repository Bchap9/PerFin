# SPDX-License-Identifier: AGPL-3.0-or-later
"""The perfin CLI surface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import main


def test_rotate_key_without_a_master_key_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("PERFIN_MASTER_KEY", raising=False)
    monkeypatch.delenv("PERFIN_MASTER_KEY_FILE", raising=False)
    assert main(["rotate-key", "--new-key-file", "/nonexistent"]) == 1
    assert "no master key configured" in capsys.readouterr().err


def test_openapi_prints_json(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["openapi"]) == 0
    schema = json.loads(capsys.readouterr().out)
    assert schema["info"]["title"] == "PerFin"
    assert schema["openapi"].startswith("3.")


def test_openapi_writes_file(tmp_path: Path) -> None:
    target = tmp_path / "openapi.json"
    assert main(["openapi", "--output", str(target)]) == 0
    assert json.loads(target.read_text())["info"]["title"] == "PerFin"


def test_config_check_reports_invalid_settings_without_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("PERFIN_BIND_PORT", "99999999")
    assert main(["config", "check"]) == 1
    err = capsys.readouterr().err
    assert "bind_port" in err
    assert "99999999" not in err


def test_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        main([])
    assert info.value.code == 2
