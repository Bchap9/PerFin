# SPDX-License-Identifier: AGPL-3.0-or-later
"""The sandbox child cannot claim the document parsers' output (SECURITY.md C1, C12).

Parsed PDF/image statements, their parser, their reconciliation and the
parse record are written by the worker from the stored pages. A child
(compromised by a hostile file) that answers with any of them is refused as
``bad_output`` by the runner, so it can never hand the importer a statement
marked "reconciled" or skip the worker's checks.
"""

from __future__ import annotations

import pytest

from app.ingestion.sandbox.runner import SandboxConfig, SandboxError, run_extraction

pytestmark = pytest.mark.security

EVIL = SandboxConfig(
    module="tests.fixtures.sandbox_evil",
    memory_bytes=384 * 1024 * 1024,
    cpu_seconds=5,
    timeout_seconds=30,
    max_output_bytes=1024 * 1024,
)


@pytest.mark.parametrize("extractor", ["forged_statement", "forged_parse_info"])
def test_child_cannot_answer_with_parser_output(extractor: str) -> None:
    with pytest.raises(SandboxError) as caught:
        run_extraction(extractor, b"%PDF-1.7", config=EVIL)
    assert caught.value.code == "bad_output"
