# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static checks of the release pipeline (SECURITY.md H4).

``.github/workflows/release.yml`` only runs when a tag is pushed, so these
tests keep its promises honest between releases: it never runs on branches,
every action is pinned to a commit SHA, permissions are least-privilege with
``id-token: write`` only where something is signed, nothing is tagged before
the Trivy scan passed, and the image digest and SBOMs are signed with cosign
and attested (SBOM and SLSA provenance). ``scripts/release_notes.py`` (tag and
version checks, release notes from CHANGELOG.md) is exercised directly.
"""

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
WORKFLOWS = ROOT / ".github" / "workflows"
RELEASE = WORKFLOWS / "release.yml"
SHA_PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


@cache
def release() -> dict[str, Any]:
    data = yaml.safe_load(RELEASE.read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return data


def jobs() -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = release()["jobs"]
    return result


def steps(job: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = jobs()[job]["steps"]
    return result


def step_index(job: str, predicate: str) -> int:
    for index, step in enumerate(steps(job)):
        if predicate in str(step.get("uses", "")) or predicate in str(step.get("run", "")):
            return index
    raise AssertionError(f"no step in {job} matching {predicate!r}")


@cache
def release_notes_module() -> ModuleType:
    path = ROOT / "scripts" / "release_notes.py"
    spec = importlib.util.spec_from_file_location("release_notes", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["release_notes"] = module
    spec.loader.exec_module(module)
    return module


# ---- triggers and permissions ----------------------------------------------------


def test_release_runs_only_on_version_tags() -> None:
    # PyYAML reads the bare key `on` as True.
    triggers = {str(k): v for k, v in release().items()}["True"]
    assert triggers == {"push": {"tags": ["v*"]}}
    gate = jobs()["check"]["if"]
    assert "github.ref_type == 'tag'" in gate
    assert "startsWith(github.ref, 'refs/tags/v')" in gate
    for name, job in jobs().items():
        if name != "check":
            needs = [job["needs"]] if isinstance(job["needs"], str) else job["needs"]
            assert "check" in needs, name


def test_permissions_are_least_privilege() -> None:
    assert release()["permissions"] == {}
    expected = {
        "check": {"contents": "read"},
        "image": {
            "contents": "read",
            "packages": "write",
            "id-token": "write",
            "attestations": "write",
        },
        "chart": {"contents": "read", "packages": "write", "id-token": "write"},
        "release": {"contents": "write"},
    }
    assert {name: job["permissions"] for name, job in jobs().items()} == expected


@pytest.mark.parametrize("workflow", sorted(WORKFLOWS.glob("*.yml")), ids=lambda p: p.name)
def test_every_action_is_pinned_to_a_commit_sha(workflow: Path) -> None:
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    for name, job in data["jobs"].items():
        for step in job.get("steps", []):
            uses = step.get("uses")
            if uses is not None:
                assert SHA_PINNED.match(uses), f"{workflow.name}:{name}: {uses} is not SHA-pinned"


def test_checkout_never_persists_credentials() -> None:
    for name, job in jobs().items():
        for step in job["steps"]:
            if str(step.get("uses", "")).startswith("actions/checkout@"):
                assert step["with"]["persist-credentials"] is False, name


# ---- build, scan, sign, attest -------------------------------------------------------


def test_image_is_multi_arch_and_pushed_by_digest_only() -> None:
    build = steps("image")[step_index("image", "docker/build-push-action@")]
    assert build["with"]["platforms"] == "linux/amd64,linux/arm64"
    assert "push-by-digest=true" in build["with"]["outputs"]
    assert "tags" not in build["with"]
    qemu = step_index("image", "docker/setup-qemu-action@")
    assert qemu < step_index("image", "docker/build-push-action@")
    images = {entry["name"] for entry in jobs()["image"]["strategy"]["matrix"]["image"]}
    assert images == {"perfin", "perfin-egress"}
    assert release()["env"]["IMAGE_NAMESPACE"] == "ghcr.io/bchap9"
    assert jobs()["image"]["env"]["IMAGE"] == "ghcr.io/bchap9/${{ matrix.image.name }}"


def test_trivy_gates_tagging() -> None:
    scan = steps("image")[step_index("image", 'trivy" image --platform')]
    command = scan["run"]
    for flag in ("--severity HIGH,CRITICAL", "--ignore-unfixed", "--exit-code 1"):
        assert flag in command
    assert "linux/amd64 linux/arm64" in command
    install = steps("image")[step_index("image", "trivy.tgz")]
    assert "sha256sum --check --strict" in install["run"]
    tag = step_index("image", "imagetools create")
    assert step_index("image", "--exit-code 1") < tag
    assert tag < step_index("image", "cosign sign --yes")


def test_digest_and_sboms_are_signed_and_attested() -> None:
    sign = steps("image")[step_index("image", "cosign sign --yes")]["run"]
    assert 'cosign sign --yes "${IMAGE}@${DIGEST}"' in sign
    assert "cosign attest --yes --type cyclonedx" in sign
    assert "cosign sign-blob --yes --bundle" in sign
    sbom = steps("image")[step_index("image", '--format "${format%%:*}"')]["run"]
    assert "cyclonedx:cdx" in sbom
    assert "spdx-json:spdx" in sbom
    provenance = steps("image")[step_index("image", "actions/attest-build-provenance@")]
    assert provenance["with"]["subject-digest"] == "${{ steps.build.outputs.digest }}"
    assert provenance["with"]["push-to-registry"] is True
    sbom_attestations = [
        s for s in steps("image") if str(s.get("uses", "")).startswith("actions/attest@")
    ]
    assert {s["with"]["sbom-path"].rsplit("-", 1)[-1] for s in sbom_attestations} == {
        "amd64.spdx.json",
        "arm64.spdx.json",
    }
    installer = steps("image")[step_index("image", "sigstore/cosign-installer@")]
    assert installer["with"]["cosign-release"] == "${{ env.COSIGN_VERSION }}"


def _go_setups(workflow: Path) -> list[tuple[str, dict[str, Any]]]:
    """(job, setup-go step) for every job that builds the chart tools."""
    data = yaml.safe_load(workflow.read_text(encoding="utf-8"))
    found: list[tuple[str, dict[str, Any]]] = []
    for name, job in data["jobs"].items():
        job_steps: list[dict[str, Any]] = job.get("steps", [])
        tools = [
            i
            for i, step in enumerate(job_steps)
            if "scripts/install-chart-tools.sh" in str(step.get("run", ""))
        ]
        if not tools:
            continue
        setups = [
            (i, step)
            for i, step in enumerate(job_steps)
            if str(step.get("uses", "")).startswith("actions/setup-go@")
        ]
        assert setups, f"{workflow.name} job {name} builds the chart tools with an unpinned Go"
        index, setup = setups[0]
        assert index < tools[0], f"{workflow.name} job {name}: setup-go must come first"
        found.append((name, setup))
    return found


def test_chart_tools_are_built_with_the_go_ci_pins() -> None:
    """P7-SR-R1-06: the release builds helm/kubeconform with the Go CI tested."""
    ci = _go_setups(WORKFLOWS / "ci.yml")
    released = _go_setups(RELEASE)
    assert ci
    assert [name for name, _ in released] == ["chart"]
    pins = {(setup["uses"], str(setup["with"]["go-version"])) for _, setup in ci + released}
    assert len(pins) == 1, pins
    for _, setup in ci + released:
        assert setup["with"]["cache"] is False


def test_chart_is_validated_signed_and_released() -> None:
    chart = "\n".join(str(s.get("run", "")) for s in steps("chart"))
    assert "scripts/install-chart-tools.sh" in chart
    assert "helm lint --strict" in chart
    assert "kubeconform -strict" in chart
    assert 'cosign sign --yes "ghcr.io/bchap9/charts/perfin@${DIGEST}"' in chart
    publish = "\n".join(str(s.get("run", "")) for s in steps("release"))
    assert "gh release create" in publish
    assert "--notes-file dist/release-notes.md" in publish
    assert "--verify-tag" in publish
    assert "SHA256SUMS" in publish


# ---- scripts/release_notes.py ------------------------------------------------------


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("v1.0.0", ("1.0.0", False)),
        ("v1.2.3-rc.1", ("1.2.3-rc.1", True)),
        ("v0.9.10", ("0.9.10", False)),
    ],
)
def test_parse_tag_accepts_semver(tag: str, expected: tuple[str, bool]) -> None:
    version, prerelease = release_notes_module().parse_tag(tag)
    assert (version, prerelease) == expected


@pytest.mark.parametrize("tag", ["1.0.0", "v1.0", "v01.0.0", "v1.0.0+build", "vX", "v1.0.0-"])
def test_parse_tag_refuses_anything_else(tag: str) -> None:
    module = release_notes_module()
    with pytest.raises(module.ReleaseError):
        module.parse_tag(tag)


def _fake_repo(
    tmp_path: Path, version: str, *, chart_app: str | None = None, section: bool = True
) -> Path:
    (tmp_path / "backend/app").mkdir(parents=True)
    (tmp_path / "frontend").mkdir()
    (tmp_path / "deploy/helm/perfin").mkdir(parents=True)
    (tmp_path / "backend/pyproject.toml").write_text(f'[project]\nversion = "{version}"\n')
    (tmp_path / "backend/app/__init__.py").write_text(f'__version__ = "{version}"\n')
    (tmp_path / "frontend/package.json").write_text(f'{{"version": "{version}"}}')
    (tmp_path / "deploy/helm/perfin/Chart.yaml").write_text(
        f'version: {version}\nappVersion: "{chart_app or version}"\n'
    )
    body = f"## [{version}] - 2026-11-04\n\n### Added\n\n- Synthetic entry.\n\n" if section else ""
    (tmp_path / "CHANGELOG.md").write_text(
        f"# Changelog\n\n## [Unreleased]\n\n{body}## [0.1.0]\n\n- old\n"
    )
    return tmp_path


def test_check_passes_when_everything_agrees(tmp_path: Path) -> None:
    root = _fake_repo(tmp_path, "1.0.0")
    assert release_notes_module().check("v1.0.0", root) == ("1.0.0", False)
    notes = release_notes_module().notes("v1.0.0", root)
    assert notes.startswith("### Added\n\n- Synthetic entry.")
    assert "old" not in notes
    assert "cosign verify ghcr.io/bchap9/perfin:1.0.0" in notes
    assert "refs/tags/v1.0.0" in notes


def test_check_refuses_version_mismatch(tmp_path: Path) -> None:
    module = release_notes_module()
    root = _fake_repo(tmp_path, "1.0.0", chart_app="0.9.0")
    with pytest.raises(module.ReleaseError, match=r"Chart\.yaml appVersion is 0\.9\.0"):
        module.check("v1.0.0", root)


def test_check_refuses_missing_changelog_section(tmp_path: Path) -> None:
    module = release_notes_module()
    root = _fake_repo(tmp_path, "1.0.0", section=False)
    with pytest.raises(module.ReleaseError, match=r"no '## \[1\.0\.0\]' section"):
        module.check("v1.0.0", root)


def test_chart_and_app_versions_agree() -> None:
    """The chart's version and appVersion move together (release check)."""
    found = release_notes_module().repository_versions(ROOT)
    chart = "deploy/helm/perfin/Chart.yaml"
    assert found[f"{chart} version"] == found[f"{chart} appVersion"]
