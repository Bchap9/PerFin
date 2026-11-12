# SPDX-License-Identifier: AGPL-3.0-or-later
"""PDF extractor: text layer with layout, tables, OCR fallback, previews (C1, C2, C8).

Runs only inside the sandbox child, on the bytes from stdin (PyMuPDF opens
a memory stream: nothing is written, nothing is fetched).

**Active content (C8).** Before any page is read, the document's active
content is inventoried for the result's :class:`DocumentMeta` (JavaScript
actions, launch actions, embedded files, link annotations, forms) and then
removed from the in-memory document: MuPDF's JavaScript engine is switched
off for the document (``pdf_disable_js``; PyMuPDF builds have it off, this
makes it explicit), ``Document.scrub`` drops JavaScript, embedded and
attached files and links, and the catalog's ``OpenAction``/``AA`` entries are
cleared. Nothing of the removed content is returned (no URIs, no scripts,
no file names). Pages are rendered with ``annots=False``. A browser engine
never sees the file on the server side.

A cross-reference entry whose object is not in the file (common in files
saved more than once) does not make the document unreadable: an object MuPDF
cannot load holds nothing MuPDF could run, so both passes step over it
(:func:`_object_errors`). ``Document.scrub`` stops at such an entry, before
the objects numbered after it; this module's own pass over every object is
therefore the one that counts. It skips only what cannot be loaded: an
action that is found and cannot be cleared refuses the document
(``malformed_pdf``).

**Caps (C2).** At most :attr:`ExtractOptions.max_pages` pages (later pages:
``truncated``), at most :attr:`ExtractOptions.max_pdf_objects` objects,
every render (OCR and previews) within :attr:`ExtractOptions.max_render_pixels`
by lowering the DPI, the OCR page and time caps of
:mod:`app.ingestion.extract.ocr`; memory and CPU are bounded by the sandbox.

**Encrypted PDFs** that need a password are not opened: the result is
``ok=false`` with code ``encrypted`` (the pipeline puts the document in
review, :data:`app.ingestion.sandbox.protocol.REVIEW_ERROR_CODES`).

**Pages.** Words come from the text layer with their boxes (points, origin
top left), grouped into lines; tables come from ruling lines
(``Page.find_tables(strategy="lines")``) and from aligned columns
(:func:`app.ingestion.extract.layout.column_tables`) for the remaining
lines. A page is OCR'd instead (:func:`needs_ocr`) when its text layer is
empty (``no_text``), mostly not text (``garbage``: replacement characters,
private-use glyphs; :func:`app.ingestion.extract.layout.text_quality`) or
very sparse on a page mostly covered by images (``low_density``, a scan
with a few stray characters). OCR words are converted back to points.
"""

from __future__ import annotations

import importlib
from typing import Any, Final

from PIL import Image

from app.core.csv_safety import neutralise_formula
from app.ingestion.extract.common import ExtractFailure
from app.ingestion.extract.layout import (
    RawLine,
    RawWord,
    clean_word,
    column_tables,
    group_lines,
    lines_text,
    text_quality,
    to_models,
)
from app.ingestion.extract.ocr import OcrEngine, choose_profile
from app.ingestion.extract.registry import register
from app.ingestion.extract.render import fit_scale, make_preview
from app.ingestion.sandbox.protocol import (
    MAX_COORD,
    MAX_WORDS_PER_PAGE,
    PAGE_TEXT_MAX,
    DocumentMeta,
    ExtractError,
    ExtractOptions,
    ExtractResult,
    LayoutTable,
    Page,
    Preview,
)

EXTRACTOR_VERSION: Final = "pdf-1"
#: Below this share of real-text characters a text layer is garbage.
MIN_TEXT_QUALITY: Final = 0.75
#: Characters per square inch below which an image-covered page is a scan.
MIN_DENSITY: Final = 2.0
#: Share of the page covered by images that makes a sparse page a scan.
IMAGE_COVERAGE: Final = 0.5
_META_MAX: Final = 500


def _pymupdf() -> Any:
    # Untyped native bindings: kept as Any in this module.
    pymupdf: Any = importlib.import_module("pymupdf")
    pymupdf.TOOLS.mupdf_display_errors(False)
    pymupdf.TOOLS.mupdf_display_warnings(False)
    return pymupdf


def _meta_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = clean_word(value)[:_META_MAX]
    return text or None


def _has_key(doc: Any, xref: int, key: str) -> bool:
    kind, _ = doc.xref_get_key(xref, key)
    return bool(kind and kind != "null")


def _object_errors() -> tuple[type[Exception], ...]:
    """What asking for one object raises when the object cannot be loaded.

    PyMuPDF's own checks raise ``ValueError`` or ``RuntimeError``; MuPDF's
    errors (``FzErrorBase``, "cannot find object in xref") are neither.
    """
    mupdf_error: type[Exception] = importlib.import_module("pymupdf").mupdf.FzErrorBase
    return (RuntimeError, ValueError, mupdf_error)


def _actions(doc: Any, xref: int) -> tuple[bool, bool]:
    """Whether object ``xref`` is (or holds, under ``/A``) a JavaScript or launch action."""
    javascript = launch = False
    for prefix in ("", "A/"):
        _, action = doc.xref_get_key(xref, f"{prefix}S")
        if action == "/JavaScript" or _has_key(doc, xref, f"{prefix}JS"):
            javascript = True
        elif action == "/Launch":
            launch = True
    return javascript, launch


def inventory_active_content(doc: Any) -> dict[str, Any]:
    """Count what C8 removes (module doc); reads object dictionaries only."""
    javascript = launch = False
    unreadable = _object_errors()
    for xref in range(1, doc.xref_length()):
        try:
            is_js, is_launch = _actions(doc, xref)
        except unreadable:
            continue
        javascript = javascript or is_js
        launch = launch or is_launch
    catalog = doc.pdf_catalog()
    names_js = _has_key(doc, catalog, "Names/JavaScript")
    links = 0
    for page in doc:
        try:
            links += len(page.get_links())
        except (RuntimeError, ValueError):
            continue
    return {
        "javascript": javascript or names_js,
        "launch_actions": launch,
        "embedded_files": int(doc.embfile_count()),
        "links": links,
        "forms": bool(doc.is_form_pdf),
    }


def _scrub(doc: Any, *, javascript: bool) -> None:
    doc.scrub(
        attached_files=True,
        clean_pages=False,
        embedded_files=True,
        hidden_text=False,
        javascript=javascript,
        metadata=False,
        redactions=False,
        remove_links=True,
        reset_fields=False,
        reset_responses=False,
        thumbnails=True,
        xml_metadata=False,
    )


def strip_active_content(pymupdf: Any, doc: Any) -> None:
    """Remove JavaScript, embedded files, links and automatic actions in memory (C8).

    Raises (the caller refuses the document) when a link, an embedded file, a
    catalog entry or an action that was found cannot be removed.
    """
    pdf = pymupdf._as_pdf_document(doc)
    pymupdf.mupdf.pdf_disable_js(pdf)
    unreadable = _object_errors()
    try:
        _scrub(doc, javascript=True)
    except unreadable:
        # Its JavaScript pass gives up at the first object it cannot load.
        # Links, attachments and embedded files are removed without that
        # pass (a failure there still raises); the loop below disarms the
        # scripts, those numbered after the unreadable object included.
        _scrub(doc, javascript=False)
    catalog = doc.pdf_catalog()
    for key in ("OpenAction", "AA", "Names/JavaScript", "Names/EmbeddedFiles"):
        if _has_key(doc, catalog, key):
            doc.xref_set_key(catalog, key, "null")
    # Whatever action dictionaries are left (reachable or not) are disarmed.
    for xref in range(1, doc.xref_length()):
        try:
            is_js, is_launch = _actions(doc, xref)
        except unreadable:
            continue  # cannot be loaded: no action MuPDF could reach
        if not (is_js or is_launch):
            continue
        for key in ("S", "JS", "F", "Win", "Unix", "Mac", "A", "AA"):
            if _has_key(doc, xref, key):
                doc.xref_set_key(xref, key, "null")
        if any(_actions(doc, xref)):
            raise ExtractFailure("malformed_pdf", "active content cannot be removed")


def _text_words(page: Any) -> list[RawWord]:
    words: list[RawWord] = []
    for item in page.get_text("words", sort=False):
        x0, y0, x1, y1, text = item[:5]
        cleaned = clean_word(str(text))
        if cleaned:
            words.append(RawWord(cleaned, float(x0), float(y0), float(x1), float(y1)))
        if len(words) > MAX_WORDS_PER_PAGE:
            break
    return words


def _image_coverage(page: Any) -> float:
    area = max(page.rect.width * page.rect.height, 1e-6)
    covered = 0.0
    try:
        infos = page.get_image_info()
    except (RuntimeError, ValueError):
        return 0.0
    for info in infos[:1000]:
        x0, y0, x1, y1 = info.get("bbox", (0, 0, 0, 0))
        covered += max(0.0, x1 - x0) * max(0.0, y1 - y0)
    return float(min(covered / area, 1.0))


def needs_ocr(page: Any, words: list[RawWord]) -> str | None:
    """Why the page's text layer cannot be used (module doc), or None."""
    text = "".join(w.text for w in words)
    if not text.strip():
        return "no_text"
    if text_quality(text) < MIN_TEXT_QUALITY:
        return "garbage"
    square_inches = max(page.rect.width * page.rect.height / (72.0 * 72.0), 1e-6)
    if len(text) / square_inches < MIN_DENSITY and _image_coverage(page) >= IMAGE_COVERAGE:
        return "low_density"
    return None


def _render(pymupdf: Any, page: Any, dpi: int, max_pixels: int) -> tuple[Image.Image, float]:
    """The page as an RGB image within the pixel cap, and the scale (pixels per point)."""
    scale = fit_scale(page.rect.width, page.rect.height, dpi / 72.0, max_pixels)
    pix = page.get_pixmap(
        matrix=pymupdf.Matrix(scale, scale), colorspace=pymupdf.csRGB, alpha=False, annots=False
    )
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return image, scale


def _has_rulings(page: Any) -> bool:
    """Whether the page draws any lines or rectangles (cheap; capped)."""
    try:
        drawings = page.get_cdrawings()
    except (RuntimeError, ValueError):
        return False
    for drawing in drawings[:5000]:
        for item in drawing.get("items", ())[:100]:
            if item and item[0] in ("l", "re"):
                return True
    return False


def _cell_text(words: list[RawWord], bbox: Any) -> str:
    if bbox is None:
        return ""
    x0, y0, x1, y1 = (float(v) for v in bbox)
    inside = [w for w in words if x0 <= (w.x0 + w.x1) / 2 <= x1 and y0 <= (w.y0 + w.y1) / 2 <= y1]
    inside.sort(key=lambda w: (round(w.yc), w.x0))
    return neutralise_formula(" ".join(w.text for w in inside))


def _ruling_tables(page: Any, words: list[RawWord]) -> list[LayoutTable]:
    """Tables bounded by drawn lines; cells filled with the page's words by position."""
    tables: list[LayoutTable] = []
    if not _has_rulings(page):
        return tables
    try:
        # use_layout=False: PyMuPDF 2.x otherwise gates the ruled tables with
        # its neural layout model, loading onnxruntime into the sandbox (a
        # large native runtime parsing nothing but untrusted pages, whose
        # thread pool makes calls the seccomp allowlist refuses at random).
        found = page.find_tables(strategy="lines_strict", use_layout=False)
        candidates = list(found.tables)[:50]
    except (RuntimeError, ValueError, ZeroDivisionError, IndexError):
        return tables
    for table in candidates:
        try:
            rows = [[_cell_text(words, cell) for cell in row.cells] for row in table.rows]
        except (RuntimeError, ValueError, IndexError, TypeError):
            continue
        rows = rows[:10_000]
        width = max((len(r) for r in rows), default=0)
        if len(rows) < 2 or width < 2:
            continue
        x0, y0, x1, y1 = table.bbox
        tables.append(
            LayoutTable(
                source="rulings",
                bbox=(float(x0), float(y0), float(x1), float(y1)),
                rows=rows,
                column_count=width,
            )
        )
    return tables


def _inside(line: RawLine, tables: list[LayoutTable]) -> bool:
    for table in tables:
        x0, y0, x1, y1 = table.bbox
        if line.x0 >= x0 - 1 and line.x1 <= x1 + 1 and line.y0 >= y0 - 1 and line.y1 <= y1 + 1:
            return True
    return False


class _OcrBudget:
    def __init__(self, options: ExtractOptions) -> None:
        self.options = options
        self.engine: OcrEngine | None = None
        self.pages = 0
        self.warnings: list[str] = []

    def take(self) -> OcrEngine | None:
        if not self.options.ocr:
            return None
        if self.pages >= self.options.max_ocr_pages:
            self._warn("ocr_page_cap")
            return None
        if self.engine is None:
            self.engine = OcrEngine(
                page_seconds=self.options.ocr_page_seconds,
                total_seconds=self.options.ocr_total_seconds,
            )
        self.pages += 1
        return self.engine

    def _warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def close(self) -> None:
        if self.engine is not None:
            self.engine.close()


def _page(
    pymupdf: Any, page: Any, number: int, options: ExtractOptions, budget: _OcrBudget
) -> tuple[Page, bool]:
    """One page (module doc); the flag says it is incomplete (caps)."""
    width, height = float(page.rect.width), float(page.rect.height)
    if not (0 < width <= MAX_COORD and 0 < height <= MAX_COORD):
        raise ExtractFailure("malformed_pdf", "a page has an impossible size")
    raw = _text_words(page)
    incomplete = len(raw) > MAX_WORDS_PER_PAGE
    raw = raw[:MAX_WORDS_PER_PAGE]
    reason = needs_ocr(page, raw)
    source = "text_layer"
    confidence: float | None = None
    profile = None
    skew: float | None = None
    if reason is not None:
        engine = budget.take()
        ocr_words: list[RawWord] = []
        if engine is not None:
            image, scale = _render(pymupdf, page, options.ocr_dpi, options.max_render_pixels)
            profile = choose_profile(options.ocr_profile, image.width, image.height)
            result = engine.recognise(
                image,
                profile=profile,
                dpi=int(scale * 72),
                max_pixels=options.max_render_pixels,
            )
            if result.timed_out:
                budget._warn("ocr_time_budget")
                incomplete = True
            ocr_words = [
                RawWord(w.text, w.x0 / scale, w.y0 / scale, w.x1 / scale, w.y1 / scale, w.conf)
                for w in result.words[:MAX_WORDS_PER_PAGE]
            ]
            confidence, skew = result.confidence, result.skew
        else:
            incomplete = True
        if ocr_words:
            raw, source = ocr_words, "ocr"
        elif reason == "no_text" or not raw:
            raw, source = [], "none"
    lines = group_lines(raw)
    rulings = _ruling_tables(page, raw) if source == "text_layer" else []
    remaining = [line for line in lines if not _inside(line, rulings)]
    words, line_models = to_models(lines)
    return (
        Page(
            number=number,
            width=width,
            height=height,
            unit="pt",
            text_source=source,  # type: ignore[arg-type]
            text=lines_text(lines)[:PAGE_TEXT_MAX],
            words=words,
            lines=line_models,
            tables=(rulings + column_tables(remaining))[:100],
            ocr_confidence=confidence if source == "ocr" else None,
            ocr_profile=profile if source == "ocr" else None,
            ocr_reason=reason,
            deskew_degrees=skew if source == "ocr" else None,
            words_truncated=incomplete and len(raw) >= MAX_WORDS_PER_PAGE,
        ),
        incomplete,
    )


def _preview(pymupdf: Any, page: Any, number: int, options: ExtractOptions) -> Preview | None:
    image, scale = _render(pymupdf, page, options.preview_dpi, options.max_render_pixels)
    return make_preview(
        image,
        page=number,
        dpi=round(scale * 72),
        scale=scale,
        max_pixels=options.max_render_pixels,
    )


def _open(pymupdf: Any, data: bytes) -> Any:
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except (RuntimeError, ValueError) as exc:
        raise ExtractFailure("malformed_pdf", "the PDF cannot be opened") from exc
    if not doc.is_pdf:
        raise ExtractFailure("malformed_pdf", "not a PDF")
    return doc


def _meta(doc: Any, active: dict[str, Any], *, encrypted: bool) -> DocumentMeta:
    raw = doc.metadata or {}
    return DocumentMeta(
        page_count=int(doc.page_count) if not encrypted else 0,
        title=_meta_text(raw.get("title")),
        author=_meta_text(raw.get("author")),
        producer=_meta_text(raw.get("producer")),
        creator=_meta_text(raw.get("creator")),
        created=(_meta_text(raw.get("creationDate")) or "")[:64] or None,
        encrypted=encrypted,
        **active,
    )


def extract_pdf(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    """Extract a PDF (module doc); any MuPDF error is ``malformed_pdf``.

    MuPDF's own exceptions (``FzErrorBase``: page-tree cycles, its nesting
    limit, broken objects) are not ``RuntimeError``s; found by the upload fuzz
    corpus (``tests/security/fuzz``). The one exception is an object that
    cannot be loaded while active content is looked for and removed (module
    doc): it is stepped over.
    """
    pymupdf = _pymupdf()
    try:
        return _extract_pdf(pymupdf, data, options, kind)
    except pymupdf.mupdf.FzErrorBase:
        raise ExtractFailure("malformed_pdf", "the PDF structure is damaged") from None


def _extract_pdf(pymupdf: Any, data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    doc = _open(pymupdf, data)
    try:
        if doc.needs_pass:
            return ExtractResult(
                kind=kind,
                ok=False,
                extractor_version=EXTRACTOR_VERSION,
                meta=DocumentMeta(encrypted=True),
                error=ExtractError(code="encrypted", message="the PDF needs a password"),
            )
        if doc.xref_length() > options.max_pdf_objects:
            raise ExtractFailure("too_complex", "the PDF has too many objects")
        try:
            active = inventory_active_content(doc)
            strip_active_content(pymupdf, doc)
        except (RuntimeError, ValueError) as exc:
            raise ExtractFailure("malformed_pdf", "the PDF structure is damaged") from exc
        meta = _meta(doc, active, encrypted=bool(doc.is_encrypted))
        total = int(doc.page_count)
        if total < 1:
            raise ExtractFailure("malformed_pdf", "the PDF has no pages")
        count = min(total, options.max_pages)
        budget = _OcrBudget(options)
        pages: list[Page] = []
        previews: list[Preview] = []
        warnings_out: list[str] = []
        truncated = total > count
        try:
            for index in range(count):
                try:
                    page = doc.load_page(index)
                    model, incomplete = _page(pymupdf, page, index + 1, options, budget)
                    pages.append(model)
                    truncated = truncated or incomplete
                    if options.previews and index < options.max_preview_pages:
                        preview = _preview(pymupdf, page, index + 1, options)
                        if preview is None:
                            warnings_out.append(f"preview_too_large:{index + 1}")
                        else:
                            previews.append(preview)
                except (RuntimeError, ValueError) as exc:
                    raise ExtractFailure("malformed_pdf", "a page cannot be read") from exc
        finally:
            budget.close()
    finally:
        doc.close()
    if total > count:
        warnings_out.append("page_cap")
    warnings_out.extend(budget.warnings)
    for name, flag in (
        ("javascript_removed", meta.javascript),
        ("launch_actions_removed", meta.launch_actions),
        ("embedded_files_removed", meta.embedded_files > 0),
        ("links_removed", meta.links > 0),
    ):
        if flag:
            warnings_out.append(name)
    return ExtractResult(
        kind=kind,
        ok=True,
        extractor_version=EXTRACTOR_VERSION,
        pages=pages,
        previews=previews,
        meta=meta,
        text="\f".join(page.text for page in pages),
        truncated=truncated,
        warnings=warnings_out[:100],
    )


register("pdf", extract_pdf)
