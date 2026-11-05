# SPDX-License-Identifier: AGPL-3.0-or-later
"""The PR template gate: scripts/check_pr_body.py and its workflow."""

from __future__ import annotations

import importlib.util
import re
import sys
from functools import cache
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
TEMPLATE = ROOT / ".github" / "pull_request_template.md"
WORKFLOW = ROOT / ".github" / "workflows" / "pr-template.yml"
SHA_PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


@cache
def checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_pr_body", ROOT / "scripts" / "check_pr_body.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_pr_body"] = module
    spec.loader.exec_module(module)
    return module


def template() -> str:
    return TEMPLATE.read_text(encoding="utf-8")


def filled() -> str:
    body = template()
    body = body.replace("- [ ] `fix`", "- [x] `fix`")
    body = body.replace("## How it was tested\n", "## How it was tested\n\nRan `make check`.\n")
    return body.replace("## Summary\n", "## Summary\n\nFix the thing.\n")


def test_filled_template_passes() -> None:
    assert checker().problems(filled(), template()) == []


def test_crlf_body_passes() -> None:
    assert checker().problems(filled().replace("\n", "\r\n"), template()) == []


def test_untouched_template_fails() -> None:
    errors = checker().problems(template(), template())
    assert "section '## Summary' is empty" in errors
    assert "section '## How it was tested' is empty" in errors
    assert any("Type of change" in error for error in errors)


@pytest.mark.parametrize("body", ["", "Quick fix, see commits."])
def test_missing_template_fails(body: str) -> None:
    errors = checker().problems(body, template())
    for heading in checker().template_headings(template()):
        assert f"missing section '## {heading}'" in errors


def test_dropped_section_is_reported() -> None:
    body = filled().replace("## Security and privacy", "## Other")
    assert checker().problems(body, template()) == ["missing section '## Security and privacy'"]


def test_template_has_required_sections() -> None:
    headings = checker().template_headings(template())
    for required in (*checker().REQUIRED_TEXT, checker().TYPE_SECTION):
        assert required in headings


@cache
def workflow() -> dict[Any, Any]:
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def test_workflow_reruns_on_edit_and_is_read_only() -> None:
    # PyYAML reads the bare key `on` as True.
    triggers = workflow()[True]
    assert set(triggers) == {"pull_request"}
    assert "edited" in triggers["pull_request"]["types"]
    assert workflow()["permissions"] == {"contents": "read"}


def test_workflow_passes_body_through_env_and_pins_actions() -> None:
    steps = workflow()["jobs"]["pr-body"]["steps"]
    for step in steps:
        if "uses" in step:
            assert SHA_PINNED.match(step["uses"]), step["uses"]
    check = next(step for step in steps if "run" in step)
    assert "${{" not in check["run"]
    assert check["env"]["PR_BODY"] == "${{ github.event.pull_request.body }}"
