# SPDX-License-Identifier: AGPL-3.0-or-later
"""A delimited file's category column, found by its header when the file is read."""

from __future__ import annotations

from app.domain.imports.schemas import MappingSpec
from app.ingestion.parsers.structured import category_column

HEADER = ["date", "description", "amount", "extended details", "category"]


def _spec(**fields: object) -> MappingSpec:
    return MappingSpec.model_validate(
        {"date_column": 0, "date_format": "MM/DD/YYYY", "amount_column": 2, **fields}
    )


def test_a_mapping_that_took_the_column_as_the_memo_still_reads_categories() -> None:
    assert category_column(HEADER, _spec(payee_column=1, memo_column=4)) == 4


def test_the_column_is_found_when_nothing_maps_it() -> None:
    assert category_column(HEADER, _spec(payee_column=1, memo_column=3)) == 4


def test_a_column_mapped_to_another_role_is_not_a_category() -> None:
    assert category_column(HEADER, _spec(payee_column=4)) is None


def test_no_header_or_no_category_header() -> None:
    assert category_column(None, _spec(payee_column=1)) is None
    assert category_column(["date", "description", "amount"], _spec(payee_column=1)) is None


def test_other_languages() -> None:
    header = ["datum", "empfänger", "betrag", "kategorie"]
    assert category_column(header, _spec(payee_column=1)) == 3
