# SPDX-License-Identifier: AGPL-3.0-or-later
"""Check a pull request description against .github/pull_request_template.md.

    PR_BODY="$(cat body.md)" python3 -I scripts/check_pr_body.py

The body is read from the ``PR_BODY`` environment variable (the workflow sets
it from the event payload, so it is never interpolated into a shell command).
A body passes when:

- every ``## `` section heading of the template is present;
- the required free-text sections (``Summary``, ``How it was tested``) hold
  text other than the template's HTML comments;
- at least one ``Type of change`` box is ticked.

Checklist items elsewhere may stay unticked: they are N/A for many changes.
Standard library only, so it runs on a bare runner.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / ".github" / "pull_request_template.md"

REQUIRED_TEXT = ("Summary", "How it was tested")
TYPE_SECTION = "Type of change"

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_TICKED_RE = re.compile(r"^\s*[-*]\s+\[[xX]\]", re.MULTILINE)


def template_headings(template: str) -> list[str]:
    return _HEADING_RE.findall(template)


def sections(body: str) -> dict[str, str]:
    """Map each ``## `` heading to its text, with HTML comments removed."""
    body = _COMMENT_RE.sub("", body.replace("\r\n", "\n"))
    matches = list(_HEADING_RE.finditer(body))
    result: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        result[match.group(1).strip().lower()] = body[match.end() : end].strip()
    return result


def problems(body: str, template: str) -> list[str]:
    found = sections(body)
    errors = [
        f"missing section '## {heading}'"
        for heading in template_headings(template)
        if heading.strip().lower() not in found
    ]
    errors += [
        f"section '## {heading}' is empty"
        for heading in REQUIRED_TEXT
        if heading.lower() in found and not found[heading.lower()]
    ]
    type_text = found.get(TYPE_SECTION.lower())
    if type_text is not None and not _TICKED_RE.search(type_text):
        errors.append(f"tick at least one box under '## {TYPE_SECTION}'")
    return errors


def main() -> int:
    body = os.environ.get("PR_BODY", "")
    errors = problems(body, TEMPLATE.read_text(encoding="utf-8"))
    if not errors:
        print("PR description follows the template.")
        return 0
    print("PR description does not follow .github/pull_request_template.md:", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    print("Edit the PR description; this check re-runs on edit.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
