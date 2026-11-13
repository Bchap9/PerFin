# SPDX-License-Identifier: AGPL-3.0-or-later
"""The OCR page-segmentation mode follows ``ExtractOptions.ocr_psm``.

Tesseract's automatic page layout analysis (mode 3) can crash natively on
some scans; the pipeline then reads the document again as one block of text
(mode 6), which skips that analysis. These tests pin which mode the engine
is given, with the Tesseract API replaced by a recorder.
"""

from __future__ import annotations

from typing import Any

import pytest
from PIL import Image

from app.ingestion.extract import image as image_extract
from app.ingestion.extract import ocr
from app.ingestion.extract import pdf as pdf_extract
from app.ingestion.sandbox.protocol import ExtractOptions

tesserocr = pytest.importorskip("tesserocr")


class _FakeApi:
    """Stands in for ``tesserocr.PyTessBaseAPI``: records the mode, reads nothing."""

    modes: list[int]

    def __init__(self, **_: Any) -> None:
        type(self).modes = []

    def SetPageSegMode(self, mode: Any) -> None:  # noqa: N802 - the tesserocr name
        type(self).modes.append(int(mode))

    def SetVariable(self, name: str, value: str) -> None:  # noqa: N802
        pass

    def SetImage(self, image: Image.Image) -> None:  # noqa: N802
        pass

    def SetSourceResolution(self, dpi: int) -> None:  # noqa: N802
        pass

    def Recognize(self, timeout: int = 0) -> bool:  # noqa: N802
        return False  # "timed out": no iterator is asked for

    def Clear(self) -> None:  # noqa: N802
        pass

    def End(self) -> None:  # noqa: N802
        pass


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> type[_FakeApi]:
    monkeypatch.setattr(tesserocr, "PyTessBaseAPI", _FakeApi)
    monkeypatch.setattr(ocr, "tessdata_dir", lambda: "/nonexistent/tessdata")
    _FakeApi.modes = []
    return _FakeApi


def _page() -> Image.Image:
    return Image.new("RGB", (400, 300), "white")


def test_default_mode_is_auto() -> None:
    assert ExtractOptions().ocr_psm == "auto"


def test_single_block_mode_selects_psm_6(fake_api: type[_FakeApi]) -> None:
    assert (int(tesserocr.PSM.AUTO), int(tesserocr.PSM.SINGLE_BLOCK)) == (3, 6)
    with ocr.OcrEngine(page_seconds=5, total_seconds=5, psm="single_block") as engine:
        engine.recognise(_page(), profile="document", dpi=300, max_pixels=1_000_000)
    assert fake_api.modes == [6]
    with ocr.OcrEngine(page_seconds=5, total_seconds=5, psm="auto") as engine:
        engine.recognise(_page(), profile="document", dpi=300, max_pixels=1_000_000)
    assert fake_api.modes == [3]
    with ocr.OcrEngine(page_seconds=5, total_seconds=5) as engine:
        engine.recognise(_page(), profile="document", dpi=300, max_pixels=1_000_000)
    assert fake_api.modes == [3]


@pytest.mark.parametrize(("psm", "mode"), [("auto", 3), ("single_block", 6)])
def test_image_extractor_passes_the_mode_on(fake_api: type[_FakeApi], psm: str, mode: int) -> None:
    import io

    buffer = io.BytesIO()
    _page().save(buffer, "PNG")
    options = ExtractOptions(ocr_psm=psm, previews=False)  # type: ignore[arg-type]
    image_extract.extract_image(buffer.getvalue(), options, "png")
    assert fake_api.modes == [mode]


@pytest.mark.parametrize(("psm", "mode"), [("auto", 3), ("single_block", 6)])
def test_pdf_ocr_budget_passes_the_mode_on(fake_api: type[_FakeApi], psm: str, mode: int) -> None:
    budget = pdf_extract._OcrBudget(ExtractOptions(ocr_psm=psm))  # type: ignore[arg-type]
    engine = budget.take()
    assert engine is not None
    engine.recognise(_page(), profile="document", dpi=300, max_pixels=1_000_000)
    budget.close()
    assert fake_api.modes == [mode]


def test_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="ocr_psm"):
        ExtractOptions(ocr_psm="sparse")  # type: ignore[arg-type]
