# SPDX-License-Identifier: AGPL-3.0-or-later
"""Answers about one held statement (``app.domain.imports.statement_answers``).

A statement's key names it exactly as read, never by account or place; an
answer applies to its statement on its account only.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.domain.imports.statement_answers import (
    IMPORT_ANYWAY,
    IMPORT_AS_READ,
    StatementAnswers,
    payload_key,
    statement_key,
)
from app.ingestion.sandbox.protocol import (
    Balance,
    InvestmentPosition,
    InvestmentStatement,
    SecurityRef,
    StatementAccount,
)

ON = date(2026, 1, 31)


def _statement(**overrides: Any) -> InvestmentStatement:
    values: dict[str, Any] = {
        "source_format": "pdf",
        "account": StatementAccount(last4="4821"),
        "period_start": date(2026, 1, 1),
        "period_end": ON,
        "as_of": ON,
        "positions": [
            InvestmentPosition(
                security=SecurityRef(symbol="ZZAA"),
                quantity=Decimal(10),
                unit_price=Decimal("12.50"),
                market_value=Decimal("125.00"),
            )
        ],
        "total_value": Balance(amount=Decimal("125.00"), as_of=ON),
    }
    values.update(overrides)
    return InvestmentStatement(**values)


def test_a_statement_key_names_the_statement_as_read() -> None:
    key = statement_key(_statement())
    # The same statement read again: the same key.
    assert statement_key(_statement()) == key
    # Another account, another figure: another statement.
    assert statement_key(_statement(account=StatementAccount(last4="4822"))) != key
    assert statement_key(_statement(total_value=Balance(amount=Decimal(126), as_of=ON))) != key
    # Letters only: nothing the audit log's scrubber would mask as an account number.
    assert len(key) == 64
    assert key.isalpha()
    assert payload_key({"statement_key": key}) == key


@pytest.mark.parametrize(
    "value",
    [None, 7, "", "a" * 63, "a" * 65, "q" * 64, "0" * 64, "A" * 64, ["a" * 64]],
)
def test_a_payload_key_must_be_one(value: object) -> None:
    assert payload_key({"statement_key": value}) is None


def test_an_answer_is_for_its_statement_on_its_account() -> None:
    one, other = statement_key(_statement()), statement_key(_statement(as_of=date(2026, 1, 30)))
    first, second = uuid.uuid4(), uuid.uuid4()
    answers = StatementAnswers()
    answers.add("investment", one, first, IMPORT_AS_READ)
    assert answers.account("investment", one) == first
    assert answers.as_read("investment", one, first)
    # Not another statement, not another account, not another kind of statement.
    assert answers.account("investment", other) is None
    assert not answers.as_read("investment", other, first)
    assert not answers.as_read("investment", one, second)
    assert not answers.as_read("statement", one, first)
    # Not the other answer either.
    assert not answers.overlap("investment", one, first)
    # A later answer about the statement on another account decides where it
    # goes; each answer stays bound to its own account.
    answers.add("investment", one, second, IMPORT_ANYWAY)
    assert answers.account("investment", one) == second
    assert answers.overlap("investment", one, second)
    assert not answers.overlap("investment", one, first)
    assert answers.as_read("investment", one, first)
