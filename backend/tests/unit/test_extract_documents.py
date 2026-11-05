# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF, image/OCR, EML and ZIP extractors called in-process (SECURITY.md C2, C5, C8, D3, D4).

The same extractors run through the real sandbox in
``tests/security/test_sandbox_documents.py``.
"""

from __future__ import annotations

import importlib
import io
import zipfile
from typing import Any

import pytest
from PIL import Image
from pydantic import ValidationError

from app.ingestion.extract.archive import extract_zip
from app.ingestion.extract.common import ExtractFailure, metadata_name
from app.ingestion.extract.eml import extract_eml
from app.ingestion.extract.htmltext import html_to_text
from app.ingestion.extract.image import extract_image
from app.ingestion.extract.layout import RawWord, column_tables, group_lines, text_quality
from app.ingestion.extract.ocr import binarise, estimate_skew, preprocess
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.extract.registry import builtin_extractors
from app.ingestion.extract.render import fit_scale, plain_png
from app.ingestion.sandbox.protocol import (
    ChildDocument,
    ExtractOptions,
    ExtractResult,
    Preview,
)
from tests.fixtures import synth_documents as synth

OPTIONS = ExtractOptions()
FAST = ExtractOptions(previews=False)


def _png_chunks(data: bytes) -> list[bytes]:
    chunks, offset = [], 8
    while offset < len(data):
        length = int.from_bytes(data[offset : offset + 4], "big")
        chunks.append(data[offset + 4 : offset + 8])
        offset += 12 + length
    return chunks


def _roundtrip(result: ExtractResult) -> ExtractResult:
    """What the parent sees: the result through JSON and strict validation."""
    return ExtractResult.model_validate_json(result.model_dump_json())


# -- registry ----------------------------------------------------------------------


def test_document_kinds_are_registered() -> None:
    registry = builtin_extractors()
    for kind in ("pdf", "png", "jpeg", "tiff", "heic", "eml", "zip"):
        assert kind in registry


# -- PDF: text layer, layout, tables --------------------------------------------------


def test_text_layer_pdf_words_lines_and_column_table() -> None:
    result = _roundtrip(extract_pdf(synth.statement_pdf(), OPTIONS, "pdf"))
    assert result.ok
    assert result.meta is not None
    assert result.meta.page_count == 1
    assert result.meta.title == "Example Savings Bank"
    (page,) = result.pages
    assert page.text_source == "text_layer"
    assert page.unit == "pt"
    assert (page.width, page.height) == (612, 792)
    assert "Statement period 2026-01-01 to 2026-01-31" in page.text
    payroll = next(w for w in page.words if w.text == "PAYROLL")
    x0, y0, x1, y1 = payroll.bbox
    assert 170 < x0 < x1 < 380
    assert 0 < y0 < y1 < 792
    line = page.lines[payroll.line]
    assert line.text == "2026-01-05 EXAMPLE PAYROLL 2500.00"
    (table,) = [t for t in page.tables if t.source == "columns"]
    assert table.rows[0] == ["Date", "Description", "Amount"]
    assert ["2026-01-12", "PARKSIDE RENT", "-1200.00"] in table.rows
    assert table.column_count == 3
    assert result.text == page.text


def test_ruled_table_is_found_from_rulings() -> None:
    result = extract_pdf(synth.statement_pdf(ruled=True), FAST, "pdf")
    rulings = [t for t in result.pages[0].tables if t.source == "rulings"]
    assert rulings
    rows = rulings[0].rows
    assert rows[0] == ["Date", "Description", "Amount"]
    assert ["2026-01-03", "COFFEE CORNER", "-4.50"] in rows
    # The ruled rows are not reported a second time as a column table.
    assert all(
        ["2026-01-03", "COFFEE CORNER", "-4.50"] not in t.rows
        for t in result.pages[0].tables
        if t.source == "columns"
    )


def test_table_cells_with_formulas_are_neutralised() -> None:
    rows = (("2026-01-01", "=HYPERLINK(1)", "-1.00"), *synth.STATEMENT_ROWS)
    result = extract_pdf(synth.statement_pdf(rows), FAST, "pdf")
    cells = [cell for t in result.pages[0].tables for row in t.rows for cell in row]
    assert "'=HYPERLINK(1)" in cells
    assert "-1.00" in cells


def test_page_cap_truncates_and_says_so() -> None:
    result = extract_pdf(synth.statement_pdf(pages=4), ExtractOptions(max_pages=2), "pdf")
    assert [p.number for p in result.pages] == [1, 2]
    assert result.meta is not None
    assert result.meta.page_count == 4
    assert result.truncated
    assert "page_cap" in result.warnings


def test_previews_are_plain_pngs_within_the_pixel_cap() -> None:
    options = ExtractOptions(preview_dpi=100, max_render_pixels=200_000)
    result = _roundtrip(extract_pdf(synth.statement_pdf(pages=2), options, "pdf"))
    assert [p.page for p in result.previews] == [1, 2]
    for preview in result.previews:
        assert preview.width * preview.height <= 200_000
        assert set(_png_chunks(preview.png)) <= {b"IHDR", b"PLTE", b"IDAT", b"IEND"}
        # scale maps points to preview pixels
        assert abs(preview.width - 612 * preview.scale) < 2
        image = Image.open(io.BytesIO(preview.png))
        assert image.size == (preview.width, preview.height)


def test_previews_can_be_turned_off_or_capped() -> None:
    pdf = synth.statement_pdf(pages=3)
    assert extract_pdf(pdf, ExtractOptions(previews=False), "pdf").previews == []
    capped = extract_pdf(pdf, ExtractOptions(max_preview_pages=1), "pdf")
    assert [p.page for p in capped.previews] == [1]


# -- PDF: OCR fallback ------------------------------------------------------------------


def test_scanned_pdf_is_ocrd_with_boxes_in_points() -> None:
    pdf = synth.scanned_pdf([["SCANNED INVOICE 2026-02-01", "TOTAL DUE 123.45"]])
    result = extract_pdf(pdf, FAST, "pdf")
    (page,) = result.pages
    assert page.text_source == "ocr"
    assert page.ocr_reason == "no_text"
    assert "INVOICE" in page.text
    assert "123.45" in page.text
    assert page.ocr_confidence is not None
    assert page.ocr_confidence > 60
    total = next(w for w in page.words if w.text == "TOTAL")
    assert total.conf is not None
    x0, _, x1, y1 = total.bbox
    # 150 px of a 1700 px wide scan of a 612 pt page = 54 pt from the left.
    assert 40 < x0 < 70
    assert x1 <= 612
    assert y1 <= 792


def test_garbage_text_layer_falls_back_to_ocr() -> None:
    pdf = synth.garbage_text_layer_pdf(["REAL TEXT UNDER JUNK", "AMOUNT 42.00"])
    page = extract_pdf(pdf, FAST, "pdf").pages[0]
    assert page.ocr_reason == "garbage"
    assert page.text_source == "ocr"
    assert "42.00" in page.text


def test_skewed_scan_is_deskewed() -> None:
    pdf = synth.scanned_pdf([["DESKEW CHECK LINE ONE", "SECOND LINE 77.10"]], angle=3.0)
    page = extract_pdf(pdf, FAST, "pdf").pages[0]
    assert page.deskew_degrees is not None
    assert 2.0 <= abs(page.deskew_degrees) <= 4.0
    assert "77.10" in page.text


def test_ocr_can_be_disabled_and_is_capped() -> None:
    pdf = synth.scanned_pdf([["PAGE ONE 1.00"], ["PAGE TWO 2.00"]])
    off = extract_pdf(pdf, ExtractOptions(ocr=False, previews=False), "pdf")
    assert [p.text_source for p in off.pages] == ["none", "none"]
    assert off.truncated
    capped = extract_pdf(pdf, ExtractOptions(max_ocr_pages=1, previews=False), "pdf")
    assert [p.text_source for p in capped.pages] == ["ocr", "none"]
    assert capped.truncated
    assert "ocr_page_cap" in capped.warnings


# -- PDF: hostile and broken files -------------------------------------------------------


def test_active_content_is_reported_and_removed() -> None:
    result = _roundtrip(extract_pdf(synth.active_content_pdf(), OPTIONS, "pdf"))
    assert result.ok
    meta = result.meta
    assert meta is not None
    assert meta.javascript
    assert meta.launch_actions
    assert meta.embedded_files == 1
    assert meta.links == 2
    for warning in (
        "javascript_removed",
        "launch_actions_removed",
        "embedded_files_removed",
        "links_removed",
    ):
        assert warning in result.warnings
    dumped = result.model_dump_json()
    for leaked in ("calc.exe", "tracker.invalid", "payload.exe", "app.alert"):
        assert leaked not in dumped
    assert "PAYROLL" in result.pages[0].text


def test_scrub_really_strips_the_active_content() -> None:
    pymupdf: Any = importlib.import_module("pymupdf")

    from app.ingestion.extract.pdf import inventory_active_content, strip_active_content

    doc = pymupdf.open(stream=synth.active_content_pdf(), filetype="pdf")
    before = inventory_active_content(doc)
    assert before["javascript"]
    strip_active_content(pymupdf, doc)
    after = inventory_active_content(doc)
    assert after == {
        **before,
        "javascript": False,
        "launch_actions": False,
        "embedded_files": 0,
        "links": 0,
    }
    assert not pymupdf.mupdf.pdf_js_supported(pymupdf._as_pdf_document(doc))


def test_encrypted_pdf_needs_review() -> None:
    result = _roundtrip(extract_pdf(synth.encrypted_pdf(), OPTIONS, "pdf"))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "encrypted"
    assert result.meta is not None
    assert result.meta.encrypted
    assert result.pages == []


@pytest.mark.parametrize(
    "data",
    [
        b"%PDF-1.7\n",
        b"%PDF-1.4\n1 0 obj << /Type /Catalog >> endobj\n%%EOF",
        b"%PDF-1.4\n" + b"\x00\xff" * 500,
        synth.statement_pdf()[:300],
    ],
    ids=["header-only", "no-pages", "binary-junk", "truncated"],
)
def test_malformed_pdfs_fail_cleanly(data: bytes) -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_pdf(data, FAST, "pdf")
    assert caught.value.code == "malformed_pdf"


def test_object_cap() -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_pdf(synth.statement_pdf(pages=80), ExtractOptions(max_pdf_objects=100), "pdf")
    assert caught.value.code == "too_complex"


def test_giant_page_is_rendered_within_the_pixel_cap() -> None:
    pymupdf: Any = importlib.import_module("pymupdf")

    doc = pymupdf.open()
    doc.new_page(width=14_400, height=14_400)  # 200 x 200 inches
    doc[0].insert_text((72, 72), "HUGE PAGE 1.00", fontsize=12)
    result = extract_pdf(doc.tobytes(), ExtractOptions(max_render_pixels=1_000_000), "pdf")
    (preview,) = result.previews
    assert preview.width * preview.height <= 1_000_000


# -- images and OCR -----------------------------------------------------------------------


def test_png_scan_is_ocrd_with_pixel_boxes() -> None:
    data = synth.image_bytes(synth.text_image(["CORNER MARKET 2026-03-14", "TOTAL 11.43"]), "PNG")
    result = _roundtrip(extract_image(data, OPTIONS, "png"))
    (page,) = result.pages
    assert page.unit == "px"
    assert (page.width, page.height) == (1700, 2200)
    assert page.text_source == "ocr"
    assert page.ocr_profile == "document"
    assert "11.43" in page.text
    word = next(w for w in page.words if w.text == "TOTAL")
    assert 140 <= word.bbox[0] <= 165
    (preview,) = result.previews
    assert set(_png_chunks(preview.png)) <= {b"IHDR", b"PLTE", b"IDAT", b"IEND"}


def test_receipt_profile_for_narrow_images() -> None:
    data = synth.image_bytes(synth.receipt_image(), "PNG")
    page = extract_image(data, FAST, "png").pages[0]
    assert page.ocr_profile == "receipt"
    assert "TOTAL" in page.text
    assert "11.43" in page.text
    total_line = next(line for line in page.lines if line.text.startswith("TOTAL"))
    assert "11.43" in total_line.text


def test_jpeg_exif_is_stripped() -> None:
    data = synth.jpeg_with_exif(["EXIF SCAN 5.55"])
    result = _roundtrip(extract_image(data, OPTIONS, "jpeg"))
    assert result.meta is not None
    assert result.meta.exif_stripped
    assert "5.55" in result.text
    dumped = result.model_dump_json()
    assert "SyntheticCam" not in dumped
    preview = Image.open(io.BytesIO(result.previews[0].png))
    assert not preview.getexif()
    assert "exif" not in preview.info
    assert b"SyntheticCam" not in result.previews[0].png


def test_multipage_tiff_and_frame_cap() -> None:
    data = synth.multipage_tiff([["FRAME ONE 1.11"], ["FRAME TWO 2.22"], ["FRAME THREE"]])
    result = extract_image(data, ExtractOptions(max_pages=2, previews=False), "tiff")
    assert [p.number for p in result.pages] == [1, 2]
    assert "2.22" in result.pages[1].text
    assert result.truncated
    assert "page_cap" in result.warnings


def test_heic_is_decoded() -> None:
    data = synth.heic_bytes(["HEIC PHOTO 9.99"])
    result = extract_image(data, FAST, "heic")
    assert "9.99" in result.text


def test_pixel_bomb_is_refused_before_decoding() -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_image(synth.pixel_bomb_png(), OPTIONS, "png")
    assert caught.value.code == "too_many_pixels"


def test_explicit_pixel_cap_below_pillows_bomb_limit() -> None:
    data = synth.image_bytes(Image.new("L", (400, 400), 255), "PNG")
    with pytest.raises(ExtractFailure) as caught:
        extract_image(data, ExtractOptions(max_image_pixels=100_000), "png")
    assert caught.value.code == "too_many_pixels"


def test_image_decoder_is_restricted_to_the_sniffed_format() -> None:
    gif = synth.image_bytes(Image.new("L", (20, 20), 255), "GIF")
    with pytest.raises(ExtractFailure) as caught:
        extract_image(gif, OPTIONS, "png")
    assert caught.value.code == "malformed_image"


def test_truncated_image_fails_cleanly() -> None:
    data = synth.image_bytes(synth.text_image(["X"]), "PNG")[:200]
    with pytest.raises(ExtractFailure) as caught:
        extract_image(data, OPTIONS, "png")
    assert caught.value.code == "malformed_image"


def test_preprocessing_steps() -> None:
    image = synth.text_image(["SKEWED LINE OF TEXT FOR THE PROFILE"] * 6, angle=-2.5)
    angle = estimate_skew(image)
    assert 1.5 <= angle <= 3.5  # the turn that straightens it
    prepared = preprocess(image, profile="document", max_pixels=12_000_000)
    assert prepared.image.mode == "L"
    assert set(prepared.image.tobytes()) <= {0, 255}
    small = Image.new("L", (300, 900), 255)
    upscaled = preprocess(small, profile="receipt", max_pixels=12_000_000)
    assert upscaled.image.width > 300
    assert upscaled.scale_back < 1
    assert set(binarise(Image.new("L", (10, 10), 200)).tobytes()) <= {0, 255}


def test_render_helpers() -> None:
    assert fit_scale(100, 100, 2.0, 1_000_000) == 2.0
    scale = fit_scale(14_400, 14_400, 300 / 72, 12_000_000)
    assert (14_400 * scale) ** 2 <= 12_000_000
    rgba = Image.new("RGBA", (10, 10))
    rgba.info["icc_profile"] = b"x" * 100
    png = plain_png(rgba)
    assert _png_chunks(png) == [b"IHDR", b"IDAT", b"IEND"]


# -- layout ------------------------------------------------------------------------------


def _w(text: str, x0: float, y0: float, width: float = 40, height: float = 10) -> RawWord:
    return RawWord(text, x0, y0, x0 + width, y0 + height)


def test_lines_and_aligned_columns() -> None:
    words = [
        _w("Intro", 10, 0),
        _w("text", 55, 0),
        *(
            word
            for row, y in enumerate((20, 34, 48, 62))
            for word in (
                _w(f"D{row}", 10, y),
                _w(f"Payee{row}", 120, y, 60),
                _w(f"{row}.00", 300 - 10 * (row % 2), y, 30 + 10 * (row % 2)),
            )
        ),
        _w("Footer", 10, 200),
    ]
    lines = group_lines(words)
    assert lines[0].text == "Intro text"
    assert len(lines) == 6
    (table,) = column_tables(lines)
    assert table.rows == [[f"D{r}", f"Payee{r}", f"{r}.00"] for r in range(4)]
    assert len(table.column_bounds) == 2


def test_no_table_for_prose() -> None:
    words = [_w("word", 10 + 45 * i, 20 * row) for row in range(5) for i in range(8)]
    assert column_tables(group_lines(words)) == []


def test_text_quality() -> None:
    assert text_quality("Statement period 2026-01-01, total $12.00") == 1.0
    assert text_quality("\ufffd\ufffd\ue000\ue001ab") < 0.5
    assert text_quality("") == 1.0


# -- EML (D3, D4) --------------------------------------------------------------------------


def test_html_mail_is_text_and_nothing_remote_survives() -> None:
    pdf = synth.statement_pdf()
    logo = synth.image_bytes(Image.new("RGB", (8, 8), "red"), "PNG")
    data = synth.receipt_email(
        attachments=[("receipt.pdf", "application/pdf", pdf)], inline_image=logo
    )
    result = _roundtrip(extract_eml(data, OPTIONS, "eml"))
    assert result.ok
    email = result.email
    assert email is not None
    assert email.headers.from_addr == "Corner Market <receipts@shop.invalid>"
    assert email.headers.to == ["household+alias-token@perfin.invalid"]
    assert email.headers.delivered_to == ["household+alias-token@perfin.invalid"]
    assert email.headers.subject == "Your receipt from Corner Market"
    assert email.headers.message_id == "<receipt-0001@shop.invalid>"
    assert email.headers.authentication_results == ["mx.perfin.invalid; spf=pass; dkim=pass"]
    assert email.body_source == "html"
    assert "Thank you for shopping at Corner Market!" in email.body_text
    assert "Total 11.43" in email.body_text
    assert "View online" in email.body_text
    for gone in ("tracker.invalid", "fetch(", "background", "a comment", "Your receipt\n"):
        assert gone not in email.body_text
    # pixel, iframe, stylesheet, CSS background, script src-less is not counted
    assert email.remote_resources_blocked >= 4
    assert email.attachments == 1
    assert email.inline_images == 1
    by_origin = {child.origin: child for child in result.children}
    assert by_origin["attachment"].name == "receipt.pdf"
    assert by_origin["attachment"].data == pdf
    assert by_origin["attachment"].depth == 1
    assert by_origin["inline"].content_id == "logo@example.invalid"
    assert by_origin["inline"].data == logo
    assert result.text == email.body_text


def test_plain_body_is_preferred() -> None:
    data = synth.receipt_email(plain="Plain receipt: total 11.43\n")
    email = extract_eml(data, FAST, "eml").email
    assert email is not None
    assert email.body_source == "plain"
    assert email.body_text.strip() == "Plain receipt: total 11.43"
    assert email.remote_resources_blocked >= 4  # still counted from the HTML part


def test_attached_message_is_a_child_but_not_descended_into() -> None:
    inner = synth.receipt_email(
        attachments=[("inner.csv", "text/csv", b"Date,Amount\n2026-01-01,1.00\n")]
    )
    data = synth.receipt_email(attached_message=inner)
    result = extract_eml(data, FAST, "eml")
    assert [c.declared_type for c in result.children] == ["message/rfc822"]
    nested = result.children[0]
    # Extracting the attached message (depth 1): text yes, attachments no.
    again = extract_eml(nested.data, ExtractOptions(depth=nested.depth), "eml")
    assert again.ok
    assert again.children == []
    assert "depth_limit" in again.warnings
    assert again.email is not None
    assert again.email.attachments == 1


def test_attachment_caps() -> None:
    attachments = [(f"a{i}.csv", "text/csv", b"x" * 1000) for i in range(5)]
    data = synth.receipt_email(attachments=attachments)
    capped = extract_eml(data, ExtractOptions(max_children=2), "eml")
    assert len(capped.children) == 2
    assert capped.truncated
    too_big = extract_eml(data, ExtractOptions(max_child_bytes=500), "eml")
    assert too_big.children == []
    assert "attachment_too_large" in too_big.warnings
    total = extract_eml(data, ExtractOptions(max_children_total_bytes=2500), "eml")
    assert len(total.children) == 2
    assert "children_total_cap" in total.warnings


def test_attachment_names_are_metadata_only() -> None:
    data = synth.receipt_email(attachments=[("../../etc/cron.d/x.csv", "text/csv", b"a,b\n")])
    (child,) = extract_eml(data, FAST, "eml").children
    assert child.name == "x.csv"


def test_garbage_is_not_a_mail() -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_eml(b"\x00\x01\x02 not a message", FAST, "eml")
    assert caught.value.code == "malformed_email"


def test_html_converter() -> None:
    text, remote, truncated = html_to_text(synth.TRACKING_HTML)
    assert "Corner Market" in text
    assert "fetch" not in text
    assert "Your receipt" not in text  # <title> is in <head>
    assert remote >= 4
    assert not truncated
    text, _, truncated = html_to_text("<p>" + "a" * 100 + "</p>", limit=10)
    assert text in ("a" * 9, "a" * 10)
    assert truncated
    text, _, _ = html_to_text("<p>a<script>b</p>c</script>d<svg><text>e</text></svg>f</p>")
    assert text == "adf"
    text, _, _ = html_to_text("x&lt;y&amp;z&#65;\x00\x07<br>next")
    assert text == "x<y&zA\nnext"


# -- ZIP (C2, C3, D4) -----------------------------------------------------------------------


def test_zip_members_become_children() -> None:
    pdf = synth.statement_pdf()
    data = synth.zip_bytes(
        [
            ("statements/jan.pdf", pdf),
            ("../../escape.csv", b"Date,Amount\n"),
            ("__MACOSX/._jan.pdf", b"fork"),
        ]
    )
    result = _roundtrip(extract_zip(data, OPTIONS, "zip"))
    assert result.ok
    names = [(c.name, c.origin, c.depth) for c in result.children]
    assert names == [("jan.pdf", "zip_member", 1), ("escape.csv", "zip_member", 1)]
    assert result.children[0].data == pdf
    assert "resource_fork_skipped" in result.warnings


def test_zip_bomb_by_ratio() -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_zip(synth.zip_bomb(20 * 1024 * 1024), OPTIONS, "zip")
    assert caught.value.code == "zip_bomb"


def test_zip_bomb_by_total_size() -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_zip(synth.zip_bomb(64 * 1024 * 1024), OPTIONS, "zip")
    assert caught.value.code == "zip_too_large"


def test_zip_member_count_cap() -> None:
    data = synth.zip_bytes([(f"f{i}.csv", b"a,b\n") for i in range(20)])
    with pytest.raises(ExtractFailure) as caught:
        extract_zip(data, ExtractOptions(max_children=10), "zip")
    assert caught.value.code == "too_many_members"


def test_zip_member_lying_about_its_size() -> None:
    data = bytearray(synth.zip_bytes([("a.csv", b"x" * 5000)], compression=zipfile.ZIP_STORED))
    # Shrink the declared size in the central directory: reading must stop.
    cd = data.rfind(b"PK\x01\x02")
    data[cd + 24 : cd + 28] = (100).to_bytes(4, "little")
    with pytest.raises(ExtractFailure) as caught:
        extract_zip(bytes(data), OPTIONS, "zip")
    assert caught.value.code in ("zip_bomb", "malformed_zip")


def test_nested_zip_is_returned_but_not_expanded() -> None:
    inner = synth.zip_bytes([("deep.csv", b"Date,Amount\n")])
    outer = synth.zip_bytes([("inner.zip", inner)])
    result = extract_zip(outer, OPTIONS, "zip")
    (child,) = result.children
    assert child.data == inner
    assert child.depth == 1
    nested = extract_zip(child.data, ExtractOptions(depth=child.depth), "zip")
    assert not nested.ok
    assert nested.error is not None
    assert nested.error.code == "depth_limit"
    assert nested.children == []


def test_zip_links_and_encryption_are_skipped() -> None:
    result = extract_zip(synth.zip_with_symlink(), OPTIONS, "zip")
    assert [c.name for c in result.children] == ["real.csv"]
    assert "link_skipped" in result.warnings


def test_not_a_zip() -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_zip(b"PK\x03\x04 broken", OPTIONS, "zip")
    assert caught.value.code == "malformed_zip"


def test_metadata_name() -> None:
    assert metadata_name("../../a/b.pdf") == "b.pdf"
    assert metadata_name("C:\\Users\\x\\s.csv") == "s.csv"
    assert metadata_name("..") is None
    assert metadata_name("bad\x00name\x07.pdf") == "badname.pdf"
    assert metadata_name("x" * 400 + ".pdf") == "x" * 255


# -- contract (what the parent accepts) ------------------------------------------------------


def test_preview_must_be_a_plain_png() -> None:
    png = plain_png(Image.new("L", (4, 4), 255))
    Preview(page=1, width=4, height=4, dpi=72, scale=1.0, png=png)
    out = io.BytesIO()
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "<script>alert(1)</script>")
    Image.new("L", (4, 4), 255).save(out, format="PNG", pnginfo=info)
    with pytest.raises(ValidationError):
        Preview(page=1, width=4, height=4, dpi=72, scale=1.0, png=out.getvalue())
    with pytest.raises(ValidationError):
        Preview(page=1, width=4, height=4, dpi=72, scale=1.0, png=b"<svg onload=x>")
    with pytest.raises(ValidationError):
        Preview(page=1, width=4, height=4, dpi=72, scale=1.0, png=png + b"trailing")


def test_child_size_must_match() -> None:
    with pytest.raises(ValidationError):
        ChildDocument(origin="zip_member", size=10, depth=1, data=b"abc")
    with pytest.raises(ValidationError):
        ChildDocument(origin="zip_member", size=3, depth=0, data=b"abc")
