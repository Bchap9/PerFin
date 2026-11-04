# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which extractions count as carrying a taxpayer identity (P56-SR-R1-02).

``carries_tax_identity`` decides whether a document outside the registered
tax form set is redacted like a tax form. All text is synthetic.
"""

from __future__ import annotations

import time

import pytest

from app.ingestion.parsers.tax_forms import carries_tax_identity
from app.ingestion.sandbox.protocol import ExtractResult


def _result(text: str) -> ExtractResult:
    return ExtractResult.model_validate({"kind": "pdf", "ok": True, "text": text})


@pytest.mark.parametrize(
    "text",
    [
        "Your social security number 000-00-0000",
        "Recipient TIN 000-00-0000 Amount 12.00",
        "Employer identification number: 00-0000000",
        "EIN 00-0000000",
        "Form 1040 U.S. Individual Income Tax Return 2024",
        "FORM 1040-SR",
        "Form 1099-K Payment Card and Third Party Network Transactions",
        "Your Social Security Number",
    ],
)
def test_identity_or_tax_heading_is_detected(text: str) -> None:
    assert carries_tax_identity(_result(text))


@pytest.mark.parametrize(
    "text",
    [
        "CORNER MARKET\nTOTAL 11.43\nVISA **** 1234",
        "Account 12-3456789 opening balance 100.00",  # an unlabelled EIN shape
        "Call 555-0100 or 1-800-555-0199",
        "Invoice 2024-01-0001 due 2024-02-01",
        "Order 1234-56-78901",
    ],
)
def test_ordinary_text_is_not(text: str) -> None:
    assert not carries_tax_identity(_result(text))


def test_detection_is_linear_on_long_hostile_text() -> None:
    hostile = ("1" * 40 + "-") * 25_000 + "form " * 50_000
    started = time.perf_counter()
    assert not carries_tax_identity(_result(hostile))
    assert time.perf_counter() - started < 2.0
