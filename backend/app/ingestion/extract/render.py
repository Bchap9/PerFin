# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bounded rendering and plain PNG previews (run only inside the sandbox child).

* :func:`fit_scale` lowers a requested scale until the rendered page stays
  within the pixel cap (SECURITY.md C2): a page that claims to be 200 inches
  wide is rendered small, never huge.
* :func:`plain_png` writes a **new** image from the pixel data alone, so no
  metadata of the input (EXIF, ICC profile, text chunks) can reach a preview;
  the parent's :class:`app.ingestion.sandbox.protocol.Preview` model accepts
  only ``IHDR``/``PLTE``/``IDAT``/``IEND`` chunks (C5: previews are
  server-rendered PNGs).
"""

from __future__ import annotations

import io
import math

from PIL import Image

from app.ingestion.sandbox.protocol import MAX_PREVIEW_PNG_BYTES, Preview


def fit_scale(width: float, height: float, scale: float, max_pixels: int) -> float:
    """``scale``, or less so that ``width * height * scale**2 <= max_pixels``."""
    area = max(width, 1e-6) * max(height, 1e-6)
    if area * scale * scale <= max_pixels:
        return scale
    return math.sqrt(max_pixels / area) * 0.999


def plain_png(image: Image.Image) -> bytes:
    """The pixels of ``image`` as a PNG without ancillary chunks."""
    mode = "L" if image.mode in ("1", "L", "LA", "I", "I;16", "F") else "RGB"
    pixels = image.convert(mode)
    clean = Image.frombytes(mode, pixels.size, pixels.tobytes())
    out = io.BytesIO()
    clean.save(out, format="PNG", compress_level=6)
    return out.getvalue()


def make_preview(
    image: Image.Image, *, page: int, dpi: int, scale: float, max_pixels: int
) -> Preview | None:
    """A :class:`Preview` of ``image`` (shrunk to the pixel cap); None if still too big."""
    shrink = fit_scale(image.width, image.height, 1.0, max_pixels)
    if shrink < 1.0:
        size = (max(1, int(image.width * shrink)), max(1, int(image.height * shrink)))
        image = image.resize(size, Image.Resampling.LANCZOS)
        scale *= shrink
    data = plain_png(image)
    if len(data) > MAX_PREVIEW_PNG_BYTES:
        return None
    return Preview(
        page=page,
        width=image.width,
        height=image.height,
        dpi=max(1, int(dpi)),
        scale=scale,
        png=data,
    )
