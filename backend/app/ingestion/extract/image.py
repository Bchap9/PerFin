# SPDX-License-Identifier: AGPL-3.0-or-later
"""Image extractor: PNG, JPEG, TIFF and HEIC through OCR (SECURITY.md C1/C2).

Runs only inside the sandbox child. Decoding is bounded before any pixel is
decoded:

* the decoder is restricted to the sniffed format (``Image.open(...,
  formats=[...])``), so a file cannot pick another, riskier Pillow plugin;
* ``Image.MAX_IMAGE_PIXELS`` is set to :attr:`ExtractOptions.max_image_pixels`
  with decompression-bomb warnings turned into errors, and the size in the
  header is checked against the same cap explicitly (``too_many_pixels``);
  PNG text chunks are capped too (``PngImagePlugin.MAX_TEXT_*``);
* multi-frame TIFFs are read up to :attr:`ExtractOptions.max_pages` frames;
* HEIC is decoded by ``pillow-heif`` (its bundled libheif), registered here
  only, inside the child.

The EXIF orientation is applied, then **all metadata is dropped**: OCR and
the preview work on a fresh image built from the pixels
(:func:`app.ingestion.extract.render.plain_png`), and no EXIF field (GPS
position, camera serial numbers) is ever returned (``meta.exif_stripped``).
Words are in image pixels (``unit: px``); see :mod:`app.ingestion.extract.ocr`.
"""

from __future__ import annotations

import io
import warnings
from typing import Final

from PIL import Image, ImageOps, PngImagePlugin, UnidentifiedImageError

from app.ingestion.extract.common import ExtractFailure
from app.ingestion.extract.layout import column_tables, group_lines, lines_text, to_models
from app.ingestion.extract.ocr import OcrEngine, choose_profile
from app.ingestion.extract.registry import register
from app.ingestion.extract.render import make_preview
from app.ingestion.sandbox.protocol import (
    MAX_WORDS_PER_PAGE,
    PAGE_TEXT_MAX,
    DocumentMeta,
    ExtractOptions,
    ExtractResult,
    Page,
    Preview,
)

EXTRACTOR_VERSION: Final = "image-1"
FORMATS: Final[dict[str, str]] = {"png": "PNG", "jpeg": "JPEG", "tiff": "TIFF", "heic": "HEIF"}
_PNG_TEXT_LIMIT: Final = 256 * 1024
#: Nominal resolution for images without a usable DPI.
DEFAULT_DPI: Final = 300

_HEIF_REGISTERED: list[bool] = []


def configure_pillow(max_pixels: int) -> None:
    """Pixel and text-chunk caps for every decode in this process (module doc)."""
    Image.MAX_IMAGE_PIXELS = max_pixels
    PngImagePlugin.MAX_TEXT_CHUNK = _PNG_TEXT_LIMIT
    PngImagePlugin.MAX_TEXT_MEMORY = _PNG_TEXT_LIMIT
    warnings.simplefilter("error", Image.DecompressionBombWarning)


def _register_heif() -> None:
    if not _HEIF_REGISTERED:
        import pillow_heif

        pillow_heif.register_heif_opener()
        _HEIF_REGISTERED.append(True)


def open_bounded(data: bytes, kind: str, options: ExtractOptions) -> Image.Image:
    """Open ``data`` as the ``kind`` format only, refusing too many pixels before decoding."""
    configure_pillow(options.max_image_pixels)
    if kind == "heic":
        _register_heif()
    try:
        image = Image.open(io.BytesIO(data), formats=[FORMATS[kind]])
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ExtractFailure("too_many_pixels", "image exceeds the pixel cap") from exc
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
        raise ExtractFailure("malformed_image", "the image cannot be decoded") from exc
    check_pixels(image, options)
    return image


def check_pixels(image: Image.Image, options: ExtractOptions) -> None:
    width, height = image.size
    if width < 1 or height < 1 or width * height > options.max_image_pixels:
        raise ExtractFailure("too_many_pixels", "image exceeds the pixel cap")


def _dpi(image: Image.Image) -> int:
    raw = image.info.get("dpi")
    try:
        value = round(float(raw[0])) if raw else 0
    except (TypeError, ValueError, IndexError):
        value = 0
    return value if 72 <= value <= 1200 else DEFAULT_DPI


def _load_frame(image: Image.Image) -> Image.Image:
    """The current frame, oriented, decoded and detached from the input's metadata."""
    try:
        oriented = ImageOps.exif_transpose(image)
        frame = (oriented or image).convert("RGB")
        frame.load()
    except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ExtractFailure("too_many_pixels", "image exceeds the pixel cap") from exc
    except (OSError, ValueError, SyntaxError) as exc:
        raise ExtractFailure("malformed_image", "the image cannot be decoded") from exc
    return Image.frombytes("RGB", frame.size, frame.tobytes())


def ocr_page(
    engine: OcrEngine | None, frame: Image.Image, number: int, options: ExtractOptions, dpi: int
) -> tuple[Page, bool]:
    """A page of OCR words for ``frame`` (pixels); the flag says the time budget ran out."""
    if engine is None:
        return (
            Page(
                number=number, width=frame.width, height=frame.height, unit="px", text_source="none"
            ),
            False,
        )
    profile = choose_profile(options.ocr_profile, frame.width, frame.height)
    result = engine.recognise(frame, profile=profile, dpi=dpi, max_pixels=options.max_render_pixels)
    raw = result.words[:MAX_WORDS_PER_PAGE]
    lines = group_lines(raw)
    words, line_models = to_models(lines)
    return (
        Page(
            number=number,
            width=frame.width,
            height=frame.height,
            unit="px",
            text_source="ocr" if words else "none",
            text=lines_text(lines)[:PAGE_TEXT_MAX],
            words=words,
            lines=line_models,
            tables=column_tables(lines),
            ocr_confidence=result.confidence,
            ocr_profile=result.profile,
            ocr_reason="image",
            deskew_degrees=result.skew,
            words_truncated=len(result.words) > MAX_WORDS_PER_PAGE,
        ),
        result.timed_out,
    )


def extract_image(data: bytes, options: ExtractOptions, kind: str) -> ExtractResult:
    if kind not in FORMATS:
        raise ExtractFailure("unknown_kind", "not an image kind")
    image = open_bounded(data, kind, options)
    exif = False
    try:
        exif = bool(image.getexif())
    except (OSError, ValueError, SyntaxError):
        exif = True  # unreadable EXIF is dropped all the same
    frames = int(getattr(image, "n_frames", 1) or 1)
    count = min(frames, options.max_pages)
    warnings_out: list[str] = []
    pages: list[Page] = []
    previews: list[Preview] = []
    truncated = frames > count
    engine = (
        OcrEngine(page_seconds=options.ocr_page_seconds, total_seconds=options.ocr_total_seconds)
        if options.ocr and options.max_ocr_pages > 0
        else None
    )
    try:
        for index in range(count):
            if index:
                try:
                    image.seek(index)
                except (EOFError, OSError, ValueError) as exc:
                    raise ExtractFailure("malformed_image", "a frame cannot be read") from exc
                check_pixels(image, options)
            dpi = _dpi(image)
            frame = _load_frame(image)
            use_engine = engine if index < options.max_ocr_pages else None
            if engine is not None and use_engine is None:
                truncated = True
                if "ocr_page_cap" not in warnings_out:
                    warnings_out.append("ocr_page_cap")
            page, timed_out = ocr_page(use_engine, frame, index + 1, options, dpi)
            if timed_out:
                truncated = True
                if "ocr_time_budget" not in warnings_out:
                    warnings_out.append("ocr_time_budget")
            pages.append(page)
            if options.previews and index < options.max_preview_pages:
                scale = min(1.0, options.preview_dpi / dpi)
                preview = make_preview(
                    frame.resize(
                        (max(1, int(frame.width * scale)), max(1, int(frame.height * scale))),
                        Image.Resampling.LANCZOS,
                    )
                    if scale < 1.0
                    else frame,
                    page=index + 1,
                    dpi=int(dpi * scale),
                    scale=scale,
                    max_pixels=options.max_render_pixels,
                )
                if preview is None:
                    warnings_out.append(f"preview_too_large:{index + 1}")
                else:
                    previews.append(preview)
    finally:
        if engine is not None:
            engine.close()
    if truncated and frames > count:
        warnings_out.append("page_cap")
    return ExtractResult(
        kind=kind,
        ok=True,
        extractor_version=EXTRACTOR_VERSION,
        pages=pages,
        previews=previews,
        meta=DocumentMeta(page_count=frames, image_format=kind, exif_stripped=exif),
        text="\f".join(page.text for page in pages),
        truncated=truncated,
        warnings=warnings_out[:100],
    )


for _kind in FORMATS:
    register(_kind, extract_image)
