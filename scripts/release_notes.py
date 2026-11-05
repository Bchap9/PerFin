# SPDX-License-Identifier: AGPL-3.0-or-later
"""Release tag checks and release notes for .github/workflows/release.yml.

    python3 -I scripts/release_notes.py check v1.0.0   # prints version=..., prerelease=...
    python3 -I scripts/release_notes.py notes v1.0.0   # prints the CHANGELOG section

``check`` refuses a tag that is not ``v<MAJOR>.<MINOR>.<PATCH>[-<pre>]`` or
that disagrees with any version string in the repository (backend package,
``app.__version__``, frontend package, Helm chart version and appVersion),
and a CHANGELOG without a ``## [<version>]`` section. ``notes`` prints that
section followed by how to verify the release (docs/UPGRADING.md). Standard
library only, so it runs on a bare runner.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

TAG_RE = re.compile(
    r"^v(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?P<pre>-[0-9A-Za-z]+(?:\.[0-9A-Za-z]+)*)?)$"
)
_INIT_VERSION_RE = re.compile(r'^__version__\s*=\s*"([^"]+)"\s*$', re.MULTILINE)
_CHART_FIELD_RE = r'^{field}:\s*"?([^"\s#]+)"?\s*(?:#.*)?$'

VERIFY_FOOTER = """
---

### Verify this release

Images are signed with cosign (keyless, GitHub Actions OIDC) and carry SBOM
and SLSA provenance attestations. Before deploying, verify the digest you will
run (docs/UPGRADING.md, "Verifying a release"):

```sh
cosign verify ghcr.io/bchap9/perfin:{version} \\
  --certificate-identity "{identity}@refs/tags/v{version}" \\
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
gh attestation verify oci://ghcr.io/bchap9/perfin:{version} --repo Bchap9/PerFin
```

SBOMs (CycloneDX and SPDX, per platform), the Helm chart package, their
Sigstore bundles and `SHA256SUMS` are attached below.
"""


#: Subject of the Sigstore certificate of every release signature.
SIGNER_IDENTITY = "https://github.com/Bchap9/PerFin/.github/workflows/release.yml"


class ReleaseError(Exception):
    """A tag or repository state that must not be released."""


def parse_tag(tag: str) -> tuple[str, bool]:
    """Return (version, is_prerelease) for a release tag, or raise."""
    match = TAG_RE.match(tag)
    if match is None:
        raise ReleaseError(f"tag {tag!r} is not v<MAJOR>.<MINOR>.<PATCH>[-<prerelease>]")
    return match.group("version"), match.group("pre") is not None


def repository_versions(root: Path) -> dict[str, str]:
    """Every version string the release must agree with, by source."""
    found: dict[str, str] = {}
    pyproject = tomllib.loads((root / "backend/pyproject.toml").read_text(encoding="utf-8"))
    found["backend/pyproject.toml"] = str(pyproject["project"]["version"])
    init = _INIT_VERSION_RE.search((root / "backend/app/__init__.py").read_text(encoding="utf-8"))
    found["backend/app/__init__.py"] = init.group(1) if init else "<missing>"
    package = json.loads((root / "frontend/package.json").read_text(encoding="utf-8"))
    found["frontend/package.json"] = str(package.get("version", "<missing>"))
    chart = (root / "deploy/helm/perfin/Chart.yaml").read_text(encoding="utf-8")
    for field in ("version", "appVersion"):
        match = re.search(_CHART_FIELD_RE.format(field=field), chart, re.MULTILINE)
        found[f"deploy/helm/perfin/Chart.yaml {field}"] = match.group(1) if match else "<missing>"
    return found


def changelog_section(changelog: str, version: str) -> str:
    """The body of ``## [<version>]`` up to the next level-2 heading."""
    heading = re.compile(rf"^## \[{re.escape(version)}\][^\n]*$", re.MULTILINE)
    match = heading.search(changelog)
    if match is None:
        raise ReleaseError(f"CHANGELOG.md has no '## [{version}]' section")
    following = re.compile(r"^## ", re.MULTILINE).search(changelog, match.end())
    body = changelog[match.end() : following.start() if following else len(changelog)].strip()
    if not body:
        raise ReleaseError(f"CHANGELOG.md section [{version}] is empty")
    return body


def check(tag: str, root: Path = ROOT) -> tuple[str, bool]:
    """Validate ``tag`` against the repository; return (version, prerelease)."""
    version, prerelease = parse_tag(tag)
    mismatched = {
        source: found for source, found in repository_versions(root).items() if found != version
    }
    if mismatched:
        detail = ", ".join(f"{source} is {found}" for source, found in sorted(mismatched.items()))
        raise ReleaseError(f"tag {tag} does not match the repository: {detail}")
    changelog_section((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    return version, prerelease


def notes(tag: str, root: Path = ROOT) -> str:
    """Release notes: the CHANGELOG section plus verification instructions."""
    version, _ = parse_tag(tag)
    section = changelog_section((root / "CHANGELOG.md").read_text(encoding="utf-8"), version)
    return section + "\n" + VERIFY_FOOTER.format(version=version, identity=SIGNER_IDENTITY)


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] not in ("check", "notes"):
        print("usage: release_notes.py check|notes <tag>", file=sys.stderr)
        return 2
    try:
        if argv[1] == "check":
            version, prerelease = check(argv[2])
            print(f"version={version}")
            print(f"prerelease={'true' if prerelease else 'false'}")
        else:
            print(notes(argv[2]))
    except ReleaseError as exc:
        print(f"release: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
