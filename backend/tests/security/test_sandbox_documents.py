# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document extractors through the real sandbox (SECURITY.md C1, C2, C8, D3, D4; ADR-0009).

Every test runs ``run_extraction`` with the shipped child: rlimits,
``PR_SET_NO_NEW_PRIVS``, Landlock and the seccomp filter (no ``execve``, no
``fork``, no sockets, no file writes) are in force while PyMuPDF, Pillow,
pillow-heif and Tesseract (in process, ``tesserocr``) parse hostile and
ordinary synthetic files. OCR working here is the proof that it needs
neither a subprocess nor anything outside the read-only Landlock roots.
"""

from __future__ import annotations

import time
from dataclasses import replace

import pytest

from app.ingestion.sandbox import child
from app.ingestion.sandbox.protocol import ExtractOptions
from app.ingestion.sandbox.runner import SandboxConfig, run_extraction
from tests.fixtures import synth_documents as synth

pytestmark = pytest.mark.security

CONFIG = SandboxConfig(timeout_seconds=90)


def test_text_pdf_with_previews() -> None:
    result = run_extraction("pdf", synth.statement_pdf(ruled=True), config=CONFIG)
    assert result.ok
    page = result.pages[0]
    assert page.text_source == "text_layer"
    assert any(t.source == "rulings" for t in page.tables)
    assert [p.page for p in result.previews] == [1]


def test_text_pdf_tables_as_an_unprivileged_uid() -> None:
    """Table detection (PyMuPDF loads onnxruntime) in a non-root child (CI run 41915782513).

    Run as root, the child drops to 65534, so this binds on every host; the
    filter must allow what onnxruntime's import and an NSS fall-through need.
    """
    config = replace(CONFIG, uid=65534, gid=65534)
    result = run_extraction("pdf", synth.statement_pdf(ruled=True), config=config)
    assert result.ok
    assert any(t.source == "rulings" for t in result.pages[0].tables)


def test_ocr_runs_inside_the_hardened_child() -> None:
    pdf = synth.scanned_pdf([["SANDBOXED OCR CHECK", "BALANCE DUE 314.15"]])
    result = run_extraction("pdf", pdf, config=CONFIG, options=ExtractOptions(previews=False))
    assert result.ok
    page = result.pages[0]
    assert page.text_source == "ocr"
    assert "314.15" in page.text


def test_tessdata_is_a_read_only_landlock_root() -> None:
    roots = child.readable_roots()
    from app.ingestion.extract.ocr import tessdata_dir

    path = tessdata_dir()
    assert path is not None
    assert any(path.startswith(root + "/") or path == root for root in roots)


def test_ocr_time_budget_is_enforced() -> None:
    dense = [f"LINE {i:03d} SOME DENSE TEXT TO RECOGNISE 12.{i:02d}" for i in range(25)]
    pdf = synth.scanned_pdf([dense] * 6)
    options = ExtractOptions(previews=False, ocr_total_seconds=1, ocr_page_seconds=1)
    started = time.monotonic()
    result = run_extraction("pdf", pdf, config=CONFIG, options=options)
    assert result.ok
    assert result.truncated
    assert "ocr_time_budget" in result.warnings
    assert time.monotonic() - started < 30


def test_pdf_javascript_and_launch_actions_are_inert() -> None:
    result = run_extraction("pdf", synth.active_content_pdf(), config=CONFIG)
    assert result.ok
    assert result.meta is not None
    assert result.meta.javascript
    assert result.meta.launch_actions
    dumped = result.model_dump_json()
    assert "calc.exe" not in dumped
    assert "tracker.invalid" not in dumped


def test_encrypted_pdf_is_a_review_case() -> None:
    result = run_extraction("pdf", synth.encrypted_pdf(), config=CONFIG)
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "encrypted"


def test_malformed_pdf_is_a_clean_failure() -> None:
    result = run_extraction("pdf", b"%PDF-1.4\n" + b"\x00garbage" * 100, config=CONFIG)
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "malformed_pdf"


def test_pixel_bomb_png() -> None:
    result = run_extraction("png", synth.pixel_bomb_png(), config=CONFIG)
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "too_many_pixels"


def test_receipt_image_and_exif() -> None:
    result = run_extraction("jpeg", synth.jpeg_with_exif(["RECEIPT 8.25"]), config=CONFIG)
    assert result.ok
    assert result.meta is not None
    assert result.meta.exif_stripped
    assert "8.25" in result.text
    assert b"SyntheticCam" not in result.previews[0].png


def test_heic_decodes_in_the_sandbox() -> None:
    result = run_extraction("heic", synth.heic_bytes(["HEIC 4.20"]), config=CONFIG)
    assert result.ok
    assert "4.20" in result.text


def test_zip_bomb() -> None:
    result = run_extraction("zip", synth.zip_bomb(20 * 1024 * 1024), config=CONFIG)
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "zip_bomb"


def test_nested_zip_stops_at_depth_one() -> None:
    inner = synth.zip_bytes([("deep.csv", b"Date,Amount\n")])
    outer = run_extraction("zip", synth.zip_bytes([("inner.zip", inner)]), config=CONFIG)
    assert outer.ok
    (child_doc,) = outer.children
    assert child_doc.depth == 1
    nested = run_extraction(
        "zip", child_doc.data, config=CONFIG, options=ExtractOptions(depth=child_doc.depth)
    )
    assert not nested.ok
    assert nested.error is not None
    assert nested.error.code == "depth_limit"


def test_eml_with_html_cid_and_attachments() -> None:
    logo = synth.image_bytes(synth.text_image(["LOGO"], size=(200, 100), origin=(10, 10)), "PNG")
    data = synth.receipt_email(
        attachments=[("receipt.pdf", "application/pdf", synth.statement_pdf())],
        inline_image=logo,
    )
    result = run_extraction("eml", data, config=CONFIG)
    assert result.ok
    assert result.email is not None
    assert "tracker.invalid" not in result.email.body_text
    assert result.email.remote_resources_blocked >= 4
    assert sorted(c.origin for c in result.children) == ["attachment", "inline"]
    assert all(c.depth == 1 for c in result.children)
