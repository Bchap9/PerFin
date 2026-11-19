# SPDX-License-Identifier: AGPL-3.0-or-later
"""A parsed delimited table records which row holds its header."""

from __future__ import annotations

from app.domain.imports.schemas import MappingSpec
from app.ingestion.parsers.structured import from_table
from app.ingestion.sandbox.protocol import Table


def test_a_parsed_table_knows_its_header_row() -> None:
    rows = [
        ["Synthetic export", "", ""],
        ["Date", "Description", "Amount"],
        ["03/02/2026", "SYNTHETIC SHOP", "-4.00"],
        ["not a date", "SYNTHETIC SHOP", "-5.00"],
    ]
    table = Table(encoding="utf-8", delimiter=",", has_header=True, rows=rows, column_count=3)
    spec = MappingSpec.model_validate(
        {
            "skip_rows": 1,
            "date_column": 0,
            "date_format": "MM/DD/YYYY",
            "amount_column": 2,
            "payee_column": 1,
        }
    )
    parsed = from_table(table, spec)
    assert (parsed.header_row, parsed.skipped_rows) == (1, [3])
