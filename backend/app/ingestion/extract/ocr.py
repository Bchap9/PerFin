# SPDX-License-Identifier: AGPL-3.0-or-later
"""OCR inside the sandbox child: preprocessing and Tesseract (ADR-0009, SECURITY.md C1/C2).

Tesseract runs **in process** through ``tesserocr`` (the C++ API, linked
into the child), not as a ``tesseract`` subprocess: the child's seccomp
filter denies ``execve`` and ``fork``, and keeping it that way is the point
(ADR-0009 "OCR"). The language data is read from the system's tessdata
directory, which Landlock grants read-only access to
(:data:`TESSDATA_DIRS`).

Preprocessing (:func:`preprocess`), all with Pillow and NumPy on an image
whose pixel count the caller has already bounded:

1. grayscale, EXIF orientation already applied by the caller;
2. upscaling of small text: the median height of the text lines (runs of
   inked rows in a binarised copy) is brought up to :data:`MIN_LINE_HEIGHT_PX`
   (at most 3x, never beyond the render pixel cap); a narrow receipt whose
   lines cannot be measured is widened to :data:`MIN_TEXT_HEIGHT_PX`. The
   target was tuned on synthetic thermal receipts
   (``tests/fixtures/synth_receipts.py``): upscaling text that is already
   large enough costs more accuracy than it gains;
3. denoise: a 3x3 median filter;
4. deskew: the angle in +-:data:`MAX_SKEW` degrees that maximises the
   variance of the row sums of a binarised, downscaled copy (projection
   profile); the image is turned by it when it is at least 0.3 degrees;
5. binarise: Otsu's threshold.

Profiles: both use page segmentation mode 3 (automatic; on the synthetic
receipt set it read more totals and dates than mode 4, "a single column",
and mode 6, "a single block"); ``receipt`` (narrow thermal prints: tall,
narrow images) also keeps inter-word spaces, so price columns stay apart.
Lines are grouped from the word boxes afterwards
(:func:`app.ingestion.extract.layout.group_lines`), not from Tesseract's
blocks. Every page gets :attr:`ExtractOptions.ocr_page_seconds` of
recognition (Tesseract's own deadline) and the document
:attr:`ExtractOptions.ocr_total_seconds` in all; the sandbox's CPU limit
and the runner's wall clock stay the outer bounds.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from typing import Any, Final, Literal

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from app.ingestion.extract.common import ExtractFailure
from app.ingestion.extract.layout import RawWord, clean_word

#: Where the language data may live (Debian/Ubuntu tesseract-ocr-eng, others).
TESSDATA_DIRS: Final = (
    "/usr/share/tesseract-ocr/5/tessdata",
    "/usr/share/tesseract-ocr/4.00/tessdata",
    "/usr/share/tessdata",
    "/usr/local/share/tessdata",
    "/usr/share/tesseract/tessdata",
)
LANGUAGE: Final = "eng"
MAX_SKEW: Final = 5.0
SKEW_STEP: Final = 0.5
MIN_TEXT_HEIGHT_PX: Final = 1000
#: Text lines lower than this many pixels are upscaled (tuned, module doc).
MIN_LINE_HEIGHT_PX: Final = 24
MAX_UPSCALE: Final = 3.0
#: Images narrower than this and at least RECEIPT_ASPECT times taller than
#: wide are receipts in the ``auto`` profile.
RECEIPT_ASPECT: Final = 1.8

Profile = Literal["document", "receipt"]


def tessdata_dir() -> str | None:
    for path in TESSDATA_DIRS:
        if os.path.isfile(os.path.join(path, f"{LANGUAGE}.traineddata")):
            return path
    return None


def choose_profile(requested: str, width: int, height: int) -> Profile:
    if requested == "receipt":
        return "receipt"
    if requested == "document":
        return "document"
    return "receipt" if height >= width * RECEIPT_ASPECT else "document"


def _otsu(gray: np.ndarray[Any, np.dtype[np.uint8]]) -> int:
    hist = np.bincount(gray.ravel(), minlength=256).astype(np.float64)
    total = hist.sum()
    if total == 0:
        return 128
    levels = np.arange(256, dtype=np.float64)
    weight_bg = np.cumsum(hist)
    weight_fg = total - weight_bg
    sum_bg = np.cumsum(hist * levels)
    mean_bg = np.divide(sum_bg, weight_bg, out=np.zeros(256), where=weight_bg > 0)
    mean_fg = np.divide(sum_bg[-1] - sum_bg, weight_fg, out=np.zeros(256), where=weight_fg > 0)
    between = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
    return int(np.argmax(between))


def binarise(image: Image.Image) -> Image.Image:
    gray = np.asarray(image.convert("L"), dtype=np.uint8)
    threshold = _otsu(gray)
    return Image.fromarray(np.where(gray > threshold, 255, 0).astype(np.uint8), mode="L")


def estimate_skew(image: Image.Image) -> float:
    """The deskew angle in degrees (module doc, step 4); 0.0 when nothing is clear."""
    small = image.convert("L")
    scale = min(1.0, 800 / max(small.size))
    if scale < 1.0:
        small = small.resize(
            (max(1, int(small.width * scale)), max(1, int(small.height * scale))),
            Image.Resampling.BILINEAR,
        )
    ink = binarise(small).point(lambda v: 255 - v)  # text white on black
    best_angle, best_score = 0.0, -1.0
    steps = int(MAX_SKEW / SKEW_STEP)
    for step in range(-steps, steps + 1):
        angle = step * SKEW_STEP
        rotated = ink.rotate(angle, resample=Image.Resampling.NEAREST, expand=False, fillcolor=0)
        rows = np.asarray(rotated, dtype=np.uint8).sum(axis=1, dtype=np.float64)
        score = float(np.var(rows))
        if score > best_score:
            best_angle, best_score = angle, score
    return best_angle


@dataclass(frozen=True)
class Prepared:
    image: Image.Image
    #: Factor from the prepared image's pixels back to the input's pixels.
    scale_back: float
    skew: float


def line_height(image: Image.Image) -> float | None:
    """Median height in pixels of the text lines (runs of inked rows), if measurable."""
    ink = np.asarray(binarise(image), dtype=np.uint8) < 128
    rows = ink.sum(axis=1) > max(2, ink.shape[1] * 0.01)
    runs: list[int] = []
    count = 0
    for inked in rows.tolist():
        if inked:
            count += 1
        elif count:
            runs.append(count)
            count = 0
    if count:
        runs.append(count)
    runs = [r for r in runs if r >= 3]
    return float(np.median(runs)) if len(runs) >= 3 else None


def preprocess(image: Image.Image, *, profile: Profile, max_pixels: int) -> Prepared:
    """Grayscale, upscale, denoise, deskew and binarise (module doc)."""
    gray = ImageOps.grayscale(image)
    factor = 1.0
    height = line_height(gray)
    if height is not None and height < MIN_LINE_HEIGHT_PX:
        factor = min(MIN_LINE_HEIGHT_PX / height, MAX_UPSCALE)
    elif height is None and profile == "receipt" and gray.width < MIN_TEXT_HEIGHT_PX:
        factor = min(MIN_TEXT_HEIGHT_PX / gray.width, MAX_UPSCALE)
    if gray.width * gray.height * factor * factor > max_pixels:
        factor = max(1.0, (max_pixels / (gray.width * gray.height)) ** 0.5)
    if factor > 1.0:
        gray = gray.resize(
            (int(gray.width * factor), int(gray.height * factor)), Image.Resampling.LANCZOS
        )
    gray = gray.filter(ImageFilter.MedianFilter(3))
    skew = estimate_skew(gray)
    if abs(skew) >= 0.3:
        gray = gray.rotate(skew, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=255)
    else:
        skew = 0.0
    return Prepared(image=binarise(gray), scale_back=1.0 / factor, skew=skew)


@dataclass
class OcrPage:
    words: list[RawWord]
    confidence: float | None
    profile: Profile
    skew: float
    timed_out: bool


class OcrEngine:
    """One Tesseract instance for a document, with the document's time budget."""

    def __init__(self, *, page_seconds: int, total_seconds: int) -> None:
        path = tessdata_dir()
        if path is None:
            raise ExtractFailure("ocr_unavailable", "no Tesseract language data")
        try:
            import tesserocr
        except ImportError as exc:  # pragma: no cover - a dependency of the image
            raise ExtractFailure("ocr_unavailable", "tesserocr is not installed") from exc
        self._tess = tesserocr
        self._api = tesserocr.PyTessBaseAPI(path=path, lang=LANGUAGE, oem=tesserocr.OEM.LSTM_ONLY)
        self._page_ms = page_seconds * 1000
        self._deadline = time.monotonic() + total_seconds

    def close(self) -> None:
        self._api.End()

    def __enter__(self) -> OcrEngine:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def remaining_ms(self) -> int:
        return int((self._deadline - time.monotonic()) * 1000)

    def recognise(
        self, image: Image.Image, *, profile: Profile, dpi: int, max_pixels: int
    ) -> OcrPage:
        """OCR one page image; word boxes are in the input image's pixels."""
        budget = min(self._page_ms, self.remaining_ms)
        if budget <= 0:
            return OcrPage([], None, profile, 0.0, timed_out=True)
        prepared = preprocess(image, profile=profile, max_pixels=max_pixels)
        tess = self._tess
        api = self._api
        api.SetPageSegMode(tess.PSM.AUTO)
        api.SetVariable("preserve_interword_spaces", "1" if profile == "receipt" else "0")
        api.SetImage(prepared.image)
        api.SetSourceResolution(int(dpi / prepared.scale_back))
        finished = api.Recognize(timeout=budget)
        words: list[RawWord] = []
        confidences: list[float] = []
        if finished:
            iterator = api.GetIterator()
            level = tess.RIL.WORD
            back = prepared.scale_back
            for item in tess.iterate_level(iterator, level):
                try:
                    text = clean_word(item.GetUTF8Text(level) or "")
                except RuntimeError:  # "No text returned" (blank page; fuzz corpus)
                    continue
                box = item.BoundingBox(level)
                if not text or box is None:
                    continue
                conf = max(0.0, min(100.0, float(item.Confidence(level))))
                x0, y0, x1, y1 = box
                words.append(RawWord(text, x0 * back, y0 * back, x1 * back, y1 * back, conf))
                confidences.append(conf)
        api.Clear()
        mean = sum(confidences) / len(confidences) if confidences else None
        return OcrPage(words, mean, profile, prepared.skew, timed_out=not finished)
