# SPDX-License-Identifier: AGPL-3.0-or-later
"""Receipt field accuracy on a held-out synthetic set (INGESTION.md "Receipts").

The OCR preprocessing (``app.ingestion.extract.ocr``) and the receipt
parser's rules were tuned on ``synth_receipts.TUNING_SEEDS``; this measures
them on receipts generated from seeds never looked at while tuning
(``synth_receipts.held_out_specs``): thermal-print images through the image
extractor and Tesseract, e-receipt PDFs through the PDF extractor, and HTML
receipt mails through the EML extractor, all in process (the sandbox adds
isolation, not different results).

Two numbers per field: **accuracy** (the value read equals the truth) and,
for the figures the matcher relies on, the **precision of confident
values** (of the values scored at or above the threshold, the share that is
right): a receipt is only linked automatically on a confident total, so a
wrong total must not be confident. The floors below sit under the measured
values (see INGESTION.md) so that a regression fails while OCR's small
platform differences do not.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache

import pytest

from app.ingestion.extract.eml import extract_eml
from app.ingestion.extract.image import extract_image
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.receipt import parse_email_receipt
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.sandbox.protocol import ExtractOptions, Receipt
from tests.fixtures import synth_receipts as sr

THRESHOLD = 0.85
COUNT = 40
FIELDS = ("merchant", "date", "total", "subtotal", "tax", "last4", "currency")
OPTIONS = ExtractOptions(previews=False)


@dataclass
class Report:
    count: int = 0
    correct: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FIELDS, 0))
    confident: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FIELDS, 0))
    confident_correct: dict[str, int] = field(default_factory=lambda: dict.fromkeys(FIELDS, 0))

    def accuracy(self, name: str) -> float:
        return self.correct[name] / self.count

    def precision(self, name: str) -> float:
        return self.confident_correct[name] / self.confident[name] if self.confident[name] else 1.0

    def table(self, title: str) -> str:
        rows = [
            f"{name:9} accuracy {self.accuracy(name):5.1%}  confident {self.confident[name]:2}"
            f"  precision {self.precision(name):5.1%}"
            for name in FIELDS
        ]
        return "\n".join([f"{title} ({self.count} receipts)", *rows])


def _merchant_ok(receipt: Receipt, spec: sr.ReceiptSpec) -> bool:
    found = (receipt.merchant or "").lower()
    return any(
        difflib.SequenceMatcher(None, found, name.lower()).ratio() >= 0.8
        for name in (spec.merchant, spec.payee)
    )


def _score(report: Report, receipt: Receipt | None, spec: sr.ReceiptSpec) -> None:
    report.count += 1
    if receipt is None:
        return
    checks = {
        "merchant": _merchant_ok(receipt, spec),
        "date": receipt.purchase_date == spec.when,
        "total": receipt.total == spec.total,
        "subtotal": receipt.subtotal == spec.subtotal,
        "tax": receipt.tax == spec.tax,
        "last4": (receipt.payment.last4 if receipt.payment else None) == spec.last4,
        "currency": receipt.currency in (spec.currency, None)
        if spec.currency == "USD"
        else receipt.currency == spec.currency,
    }
    scores = {"last4": receipt.confidence.get("payment_last4"), **receipt.confidence}
    for name, ok in checks.items():
        report.correct[name] += ok
        value = scores.get(name)
        if value is not None and value >= THRESHOLD:
            report.confident[name] += 1
            report.confident_correct[name] += ok


def _image(spec: sr.ReceiptSpec) -> Receipt | None:
    result = parse_extraction(extract_image(sr.thermal_png(spec), OPTIONS, "png"))
    return result.parse.receipt if result.parse else None


def _pdf(spec: sr.ReceiptSpec) -> Receipt | None:
    result = parse_extraction(extract_pdf(sr.receipt_pdf(spec), OPTIONS, "pdf"))
    return result.parse.receipt if result.parse else None


def _mail(spec: sr.ReceiptSpec) -> Receipt | None:
    result = extract_eml(sr.receipt_html_email(spec), OPTIONS, "eml")
    return parse_email_receipt(result, threshold=THRESHOLD)


READERS: dict[str, Callable[[sr.ReceiptSpec], Receipt | None]] = {
    "image": _image,
    "pdf": _pdf,
    "mail": _mail,
}


@cache
def report(source: str) -> Report:
    out = Report()
    for spec in sr.held_out_specs(COUNT):
        _score(out, READERS[source](spec), spec)
    return out


def test_held_out_and_tuning_seeds_are_disjoint() -> None:
    held_out = {s.seed for s in sr.held_out_specs(COUNT)}
    assert held_out.isdisjoint(sr.TUNING_SEEDS)


#: (field, accuracy floor, precision floor of confident values)
IMAGE_FLOORS = (
    ("total", 0.80, 0.97),
    ("date", 0.75, 0.95),
    ("merchant", 0.80, 0.95),
    ("subtotal", 0.75, 0.95),
    ("tax", 0.75, 0.95),
    ("last4", 0.75, 0.95),
    ("currency", 0.80, 0.95),
)


def test_thermal_receipt_images(capsys: pytest.CaptureFixture[str]) -> None:
    result = report("image")
    with capsys.disabled():
        print("\n" + result.table("thermal receipt images (OCR)"))
    for name, accuracy, precision in IMAGE_FLOORS:
        assert result.accuracy(name) >= accuracy, result.table("image")
        assert result.precision(name) >= precision, result.table("image")


@pytest.mark.parametrize("source", ["pdf", "mail"])
def test_e_receipts_are_read_exactly(source: str, capsys: pytest.CaptureFixture[str]) -> None:
    result = report(source)
    with capsys.disabled():
        print("\n" + result.table(f"{source} e-receipts"))
    for name in FIELDS:
        assert result.accuracy(name) == 1.0, result.table(source)
        assert result.precision(name) == 1.0, result.table(source)
