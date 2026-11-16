# SPDX-License-Identifier: AGPL-3.0-or-later
"""QIF records made only of column names are in doubt (synthetic files).

A record whose text fields are all column names (``Description`` /
``Reference`` / ``Debit``) keeps its date and amount, which may be money
that moved, but its payee is low confidence, so it is imported pending.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.ingestion.extract.qif import extract_qif
from app.ingestion.parsers.structured import from_statement
from app.ingestion.sandbox.protocol import ExtractOptions

OPTIONS = ExtractOptions()

BODY = (
    b"D03/14/2026\nT-23.80\nPOrchard Row Market\nLGroceries\n^\n"
    b"D03/27/2026\nT410.00\nPStipend\nLIncome\n^\n"
)


def _qif(*records: bytes) -> bytes:
    return b"!Account\nNEveryday\nTBank\n^\n!Type:Bank\n" + b"".join(records)


def _payees(data: bytes) -> list[str | None]:
    return [t.payee for t in extract_qif(data, OPTIONS, "qif").statements[0].transactions]


# ---------------------------------------------------------------------------
# QIF records made of column names
# ---------------------------------------------------------------------------


def test_qif_record_whose_text_is_all_column_names_is_in_doubt() -> None:
    placeholder = b"D03/05/2026\nT-40.00\nPDescription\nMReference\nLDebit\n^\n"
    result = extract_qif(_qif(BODY, placeholder), OPTIONS, "qif")
    rows = result.statements[0].transactions
    assert [(t.payee, t.amount) for t in rows] == [
        ("Orchard Row Market", Decimal("-23.80")),
        ("Stipend", Decimal("410.00")),
        ("Description", Decimal("-40.00")),
    ]
    assert [list(t.low_confidence) for t in rows] == [[], [], ["payee"]]
    parsed = from_statement(result.statements[0])
    assert parsed.transactions[2].low_confidence == ("payee",)


@pytest.mark.parametrize(
    "record",
    [
        b"D03/05/2026\nT-40.00\nPDescription\nMRent for March\n^\n",
        b"D03/05/2026\nT-40.00\nPCheck\nN1042\nLDebit\n^\n",
        b"D03/05/2026\nT-40.00\nPTransfer\nMReference\n^\n",
    ],
)
def test_qif_record_with_real_text_is_not_in_doubt(record: bytes) -> None:
    rows = extract_qif(_qif(BODY, record), OPTIONS, "qif").statements[0].transactions
    assert [list(t.low_confidence) for t in rows] == [[], [], []]
