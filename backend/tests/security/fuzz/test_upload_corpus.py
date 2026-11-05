# SPDX-License-Identifier: AGPL-3.0-or-later
"""The hostile upload corpus through the real intake and the real sandbox (C1-C6, C8).

For every sample in :mod:`tests.security.fuzz.corpus`:

1. the upload intake (``store_stream``: sniff, whole-file markup scan for
   text types, hash, encrypt) refuses it with the expected reason and stores
   nothing, or accepts it into quarantine;
2. an accepted sample is extracted by the sandboxed child with the worker's
   default limits: the answer is a validated result (``ok`` or the expected
   error codes) or a sandbox resource-limit error, always within the
   wall-clock budget, and no forbidden text (file contents of the host,
   scripts, launch targets, path components) reaches the validated output.

The integration test in ``test_worker_survives.py`` runs the same corpus
through the worker's job handler.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

import pytest

from app.core.crypto import Keyring
from app.domain.documents.blobstore import LocalFSBlobStore
from app.ingestion.intake.upload import UploadRejectedError, store_stream
from app.ingestion.sandbox.protocol import ExtractOptions
from app.ingestion.sandbox.runner import SandboxConfig, SandboxError, run_extraction
from tests.security.fuzz.corpus import LIMIT_CODES, SAMPLES, Sample

pytestmark = pytest.mark.security

#: The worker's defaults (PERFIN_EXTRACT_*): 1 GiB, 60 s CPU, 120 s wall clock.
SANDBOX = SandboxConfig()
#: Slack over the sandbox's own wall-clock limit for spawning and validation.
BUDGET_SECONDS = SANDBOX.timeout_seconds + 30
MAX_UPLOAD = 200 * 1024 * 1024


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def check_sample(sample: Sample, tmp_path: Path) -> str:
    """Run one sample through intake and sandbox; return a short outcome label."""
    data = sample.build()
    store = LocalFSBlobStore(tmp_path / "documents", tmp_path / "quarantine")
    keyring = Keyring({1: secrets.token_bytes(32)}, 1)
    try:
        stored = store_stream(store=store, keyring=keyring, chunks=(data,), max_bytes=MAX_UPLOAD)
    except UploadRejectedError as exc:
        reason: str | None = str(exc.reason)
    else:
        reason = None
    if reason is not None:
        assert sample.expect == "rejected", f"{sample.name} refused at intake: {reason}"
        assert reason in sample.codes, f"{sample.name}: {reason}"
        assert _files(tmp_path) == [], "a refused upload leaves nothing behind"
        return f"rejected:{reason}"
    assert sample.expect != "rejected", f"{sample.name} was accepted as {stored.file_type}"
    assert len(_files(tmp_path / "quarantine")) == 1
    kind = stored.file_type.extract_kind
    assert kind is not None, f"{sample.name}: no extractor for {stored.file_type}"
    started = time.monotonic()
    try:
        result = run_extraction(kind, data, config=SANDBOX, options=ExtractOptions())
    except SandboxError as exc:
        sandbox_code: str | None = exc.code
    else:
        sandbox_code = None
    if sandbox_code is not None:
        elapsed = time.monotonic() - started
        assert elapsed < BUDGET_SECONDS
        assert sample.expect in ("contained", "extract_error"), f"{sample.name}: {sandbox_code}"
        assert sandbox_code in LIMIT_CODES | {"crashed"}, f"{sample.name}: sandbox {sandbox_code}"
        return f"sandbox:{sandbox_code}"
    assert time.monotonic() - started < BUDGET_SECONDS
    assert result.kind == kind
    dumped = result.model_dump_json()
    for text in sample.forbidden:
        assert text not in dumped, f"{sample.name}: {text!r} reached the output"
    if result.ok:
        assert sample.expect in ("ok", "contained"), f"{sample.name} extracted"
        return "ok"
    assert result.error is not None
    # An exception the extractor did not anticipate is contained by the
    # child, but it is a bug: every refusal has a deliberate code.
    assert result.error.code != "extractor_crashed", f"{sample.name}: {result.error.message}"
    if sample.expect == "extract_error":
        assert result.error.code in sample.codes, f"{sample.name}: {result.error.code}"
    else:
        assert sample.expect == "contained", f"{sample.name}: {result.error.code}"
    return f"error:{result.error.code}"


@pytest.mark.parametrize("sample", SAMPLES, ids=[s.name for s in SAMPLES])
def test_upload_corpus(sample: Sample, tmp_path: Path) -> None:
    check_sample(sample, tmp_path)


def test_corpus_covers_every_family() -> None:
    names = {s.name.split("-")[0] for s in SAMPLES}
    assert {"pdf", "zip", "png", "jpeg", "tiff", "ofx", "xlsx", "xml", "eml", "tar", "7z"} <= names
    assert len({s.name for s in SAMPLES}) == len(SAMPLES)
