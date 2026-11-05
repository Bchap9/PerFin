# SPDX-License-Identifier: AGPL-3.0-or-later
"""The MCP second-stage scrub keeps numeric fields intact and nothing else (M2).

Found by the live run of every tool (``tests/security/mcp/test_mcp_live.py``):
``get_allocation`` sent ``"weight": "1.0000000000"`` and the second scrub
replaced it with ``[ACCT]`` (a run of 10 digits looks like an account number),
so the assistant lost every weight, return and rate with many decimals.
"""

from __future__ import annotations

import pytest

from app.mcp.scrub import NUMERIC_FIELDS, ResponseScrubber

SCRUB = ResponseScrubber()


@pytest.mark.parametrize("field", sorted(NUMERIC_FIELDS))
@pytest.mark.parametrize("value", ["1.0000000000", "-0.1234567890", "12345.12345678", "0", "7"])
def test_numeric_fields_survive(field: str, value: str) -> None:
    assert SCRUB.json({field: value}) == {field: value}


@pytest.mark.parametrize("field", sorted(NUMERIC_FIELDS))
@pytest.mark.parametrize(
    "value",
    [
        "4111 1111 1111 1111",
        "acct 123456789012",
        "1234567890123456",  # 16 integer digits: not a number the API sends
        "123-45-6789",
        "jane.doe@example.org",
        "1.0000000000 call 555-123-4567",
    ],
)
def test_numeric_fields_with_anything_else_are_scrubbed(field: str, value: str) -> None:
    out = SCRUB.json({field: value})[field]
    assert out != value
    assert "[" in out


def test_other_fields_with_long_digit_runs_are_still_scrubbed() -> None:
    body = {"memo": "1.0000000000", "items": [{"note": "0.1234567890"}]}
    out = SCRUB.json(body)
    assert out["memo"] != "1.0000000000"
    assert out["items"][0]["note"] != "0.1234567890"


def test_allocation_shape_keeps_its_weights() -> None:
    body = {
        "by": {
            "account": [
                {
                    "key": "Brokerage 4111111111111111",
                    "value": {"amount": "1100.00", "currency": "USD"},
                    "weight": "1.0000000000",
                }
            ]
        }
    }
    row = SCRUB.json(body)["by"]["account"][0]
    assert row["weight"] == "1.0000000000"
    assert row["value"] == {"amount": "1100.00", "currency": "USD"}
    assert "4111" not in row["key"]
