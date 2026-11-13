# SPDX-License-Identifier: AGPL-3.0-or-later
"""A PDF with one cross-reference entry that cannot be loaded (SECURITY.md C8).

Such a file is an ordinary statement to every viewer: it is read. Reading it
must not cost anything of the active-content guarantee: a script numbered
after the unreadable entry is still reported and still disarmed, a script
that cannot be disarmed still refuses the document, and bytes that are no
PDF at all are still ``malformed_pdf``.
"""

from __future__ import annotations

import importlib
import random
from typing import Any

import pytest

from app.ingestion.extract.common import ExtractFailure
from app.ingestion.extract.pdf import (
    extract_pdf,
    inventory_active_content,
    strip_active_content,
)
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from tests.fixtures.synth_pdf_damaged import (
    ROWS,
    SCRIPT_MARKER,
    TITLE,
    damaged_xref_statement_pdf,
)

FAST = ExtractOptions(previews=False)


def _roundtrip(result: ExtractResult) -> ExtractResult:
    """What the parent sees: the result through JSON and strict validation."""
    return ExtractResult.model_validate_json(result.model_dump_json())


def _pymupdf() -> Any:
    pymupdf: Any = importlib.import_module("pymupdf")  # untyped bindings
    return pymupdf


def _script_objects(doc: Any) -> list[int]:
    """Every object that can be loaded and is a script or carries one."""
    pymupdf = _pymupdf()
    found: list[int] = []
    for xref in range(1, doc.xref_length()):
        try:
            source = doc.xref_object(xref, compressed=True)
            action = doc.xref_get_key(xref, "S")[1]
            script = doc.xref_get_key(xref, "JS")[0]
        except (RuntimeError, pymupdf.mupdf.FzErrorBase):
            continue
        if action == "/JavaScript" or script != "null" or SCRIPT_MARKER in source:
            found.append(xref)
    return found


def test_pdf_with_broken_xref_entry_is_extracted() -> None:
    result = _roundtrip(extract_pdf(damaged_xref_statement_pdf(False), FAST, "pdf"))
    assert result.ok
    assert result.error is None
    assert len(result.pages) == 1
    text = result.pages[0].text
    assert f"{TITLE} - Statement period 2026-01-01 to 2026-01-31" in text
    for _, payee, amount in ROWS:
        assert payee in text
        assert amount in text
    assert result.pages[0].text_source == "text_layer"
    assert result.meta is not None
    assert result.meta.page_count == 1
    assert not result.meta.javascript
    assert "javascript_removed" not in result.warnings


def test_javascript_is_still_removed_from_damaged_pdf() -> None:
    data = damaged_xref_statement_pdf(True)

    result = _roundtrip(extract_pdf(data, FAST, "pdf"))
    assert result.ok
    assert result.meta is not None
    assert result.meta.javascript
    assert "javascript_removed" in result.warnings
    assert f"{TITLE} - Statement period" in result.pages[0].text
    dumped = result.model_dump_json()
    for leaked in (SCRIPT_MARKER, "app.alert"):
        assert leaked not in dumped

    # The same inventory the active-content tests use, on the document in memory.
    pymupdf = _pymupdf()
    doc = pymupdf.open(stream=data, filetype="pdf")
    before = inventory_active_content(doc)
    assert before["javascript"]
    assert len(_script_objects(doc)) == 1
    strip_active_content(pymupdf, doc)
    after = inventory_active_content(doc)
    assert after == {**before, "javascript": False}
    assert _script_objects(doc) == []
    assert doc.xref_get_key(doc.pdf_catalog(), "OpenAction") == ("null", "null")
    assert not pymupdf.mupdf.pdf_js_supported(pymupdf._as_pdf_document(doc))


def test_script_that_cannot_be_disarmed_refuses_the_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping what cannot be loaded is not skipping what cannot be cleared."""
    pymupdf = _pymupdf()
    data = damaged_xref_statement_pdf(True)
    probe = pymupdf.open(stream=data, filetype="pdf")
    (script,) = _script_objects(probe)
    probe.close()
    real = pymupdf.Document.xref_set_key

    def refuse_the_script(self: Any, xref: int, key: str, value: str) -> Any:
        if xref == script:
            raise RuntimeError("synthetic: the object cannot be written")
        return real(self, xref, key, value)

    monkeypatch.setattr(pymupdf.Document, "xref_set_key", refuse_the_script)
    with pytest.raises(ExtractFailure) as caught:
        extract_pdf(data, FAST, "pdf")
    assert caught.value.code == "malformed_pdf"


def test_truly_unreadable_pdf_is_still_malformed() -> None:
    noise = random.Random(20261112).randbytes(4096)
    with pytest.raises(ExtractFailure) as caught:
        extract_pdf(b"%PDF-1.6\n" + noise, FAST, "pdf")
    assert caught.value.code == "malformed_pdf"
