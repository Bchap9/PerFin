# SPDX-License-Identifier: AGPL-3.0-or-later
"""QIF records that are not read are counted (synthetic files).

A record with no amount line, an amount that is not a number, a date that is
no date, a last record with no ``^``: none becomes a transaction, and each is
counted in a ``transaction_skipped:<n>`` warning, one per section, so the
importer can say how many records the file lost.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.ingestion.extract.qif import extract_qif
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult

OPTIONS = ExtractOptions()

#: Three invented records; the second has a date and a payee and no ``T`` line.
WITHOUT_AMOUNT = (
    b"!Type:Bank\n"
    b"D03/14/2026\nT-23.80\nPOrchard Row Market\n^\n"
    b"D03/19/2026\nPBrightwater Utilities\nMno amount printed\n^\n"
    b"D03/27/2026\nT410.00\nPStipend\n^\n"
)


def _skips(result: ExtractResult) -> list[str]:
    return [warning for warning in result.warnings if "skipped" in warning]


def _rows(result: ExtractResult, index: int = 0) -> list[tuple[date, Decimal, str | None]]:
    return [(t.posted, t.amount, t.payee) for t in result.statements[index].transactions]


def test_qif_record_without_amount_is_counted() -> None:
    result = extract_qif(WITHOUT_AMOUNT, OPTIONS, "qif")
    assert _rows(result) == [
        (date(2026, 3, 14), Decimal("-23.80"), "Orchard Row Market"),
        (date(2026, 3, 27), Decimal("410.00"), "Stipend"),
    ]
    assert _skips(result) == ["transaction_skipped:1"]


def test_qif_unreadable_amount_and_date_are_counted() -> None:
    data = (
        b"!Type:Bank\n"
        b"D03/14/2026\nTtwelve\nPOrchard Row Market\n^\n"
        b"Dsoon\nT-5.00\nPBrightwater Utilities\n^\n"
        b"D02/30/2026\nT-6.00\nPNo such day\n^\n"
        b"D03/27/2026\nT410.00\nPStipend\n^\n"
    )
    result = extract_qif(data, OPTIONS, "qif")
    assert _rows(result) == [(date(2026, 3, 27), Decimal("410.00"), "Stipend")]
    assert _skips(result) == ["transaction_skipped:3"]


def test_qif_each_section_counts_its_own_skipped_records() -> None:
    """Two sections that lose one record each: two warnings, not one of a set."""
    card = WITHOUT_AMOUNT.replace(b"!Type:Bank", b"!Type:CCard")
    result = extract_qif(WITHOUT_AMOUNT + card, OPTIONS, "qif")
    assert [len(statement.transactions) for statement in result.statements] == [2, 2]
    assert _skips(result) == ["transaction_skipped:1", "transaction_skipped:1"]


def test_qif_last_record_without_terminator_is_counted() -> None:
    data = b"!Type:Bank\nD03/14/2026\nT-23.80\nPOrchard Row Market\n^\nD03/19/2026\nT-7.10\n"
    result = extract_qif(data, OPTIONS, "qif")
    assert _rows(result) == [(date(2026, 3, 14), Decimal("-23.80"), "Orchard Row Market")]
    assert "last_record_unterminated" in result.warnings
    assert _skips(result) == ["transaction_skipped:1"]


def test_qif_that_loses_no_record_has_no_skip_warning() -> None:
    data = WITHOUT_AMOUNT.replace(b"PBrightwater Utilities", b"T-58.12\nPBrightwater Utilities")
    result = extract_qif(data, OPTIONS, "qif")
    assert len(_rows(result)) == 3
    assert _skips(result) == []
