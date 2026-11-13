# SPDX-License-Identifier: AGPL-3.0-or-later
"""Two small readings of untrusted extraction output.

- ``has_dated_rows``: whether a page table has a row a column mapping could
  read (else the document says ``no_rows`` and offers no mapping editor).
- ``records_skipped``: the count an extractor's warning gives.
"""

from __future__ import annotations

import pytest

from app.domain.imports.service import records_skipped
from app.domain.review.layout import has_dated_rows
from app.ingestion.sandbox.protocol import ExtractResult, Table


def _table(rows: list[list[str]]) -> Table:
    width = max(len(row) for row in rows)
    return Table(
        encoding="utf-8",
        delimiter="\t",
        has_header=True,
        rows=[row + [""] * (width - len(row)) for row in rows],
        column_count=width,
    )


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(
            [["Datum", "Text", "Betrag"], ["07.03", "Blumenladen Nord", "-12,40"]],
            id="day.month without a year",
        ),
        pytest.param(
            [["Date", "Details", "Amount"], ["260307", "Flower shop", "-12.40"]],
            id="six digits",
        ),
        pytest.param([["03/07", "Flower shop", "12.40"]], id="no header, one row"),
        pytest.param(
            [["Date", "Details", "Amount"], ["03/07/2026", "Flower shop", "12.40"]],
            id="slashes",
        ),
    ],
)
def test_rows_with_a_date_and_a_figure_are_offered(rows: list[list[str]]) -> None:
    assert has_dated_rows(_table(rows))


@pytest.mark.parametrize(
    "rows",
    [
        pytest.param(
            [["Summary", ""], ["Previous balance", "1,310.00"], ["New balance", "48.75"]],
            id="summary box",
        ),
        pytest.param(
            [["Summary", "", ""], ["Fees and interest", "48.75", "1,310.00"]],
            id="two amounts are not a date",
        ),
        pytest.param(
            [["Summary", "", ""], ["Reference", "481516", "none"]],
            id="six digits alone",
        ),
        pytest.param([["Date", "Details", "Amount"]], id="header only"),
    ],
)
def test_tables_without_a_dated_row_are_not_offered(rows: list[list[str]]) -> None:
    assert not has_dated_rows(_table(rows))


def _result(warnings: list[str]) -> ExtractResult:
    return ExtractResult.model_validate({"ok": True, "kind": "ofx", "warnings": warnings})


def test_skipped_counts_add_up() -> None:
    assert (
        records_skipped(_result(["transaction_skipped:2", "other", "transaction_skipped:3"])) == 5
    )


@pytest.mark.parametrize(
    "warning",
    [
        "transaction_skipped:" + "9" * 180,
        "transaction_skipped:٣",
        "transaction_skipped:-4",
        "transaction_skipped:",
    ],
)
def test_a_warning_that_is_not_a_small_ascii_count_is_ignored(warning: str) -> None:
    assert records_skipped(_result([warning, "transaction_skipped:1"])) == 1
