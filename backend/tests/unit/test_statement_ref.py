# SPDX-License-Identifier: AGPL-3.0-or-later
"""The stable reference of a statement (``statement_answers.statement_ref``).

A placement ("this statement belongs on that account") must survive a
corrected balance and a parser bump, so it is also kept under a reference
that leaves out everything such a change moves. Every statement is invented.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.domain.imports.statement_answers import (
    REF_FIELD,
    StatementAnswers,
    payload_ref,
    placement,
    statement_key,
    statement_ref,
    statement_refs,
)
from app.ingestion.sandbox.protocol import InvestmentStatement, Statement

ACCOUNT = uuid.UUID("01900000-0000-7000-8000-0000000000e7")
OTHER = uuid.UUID("01900000-0000-7000-8000-0000000000e8")


def _statement(**changes: Any) -> Statement:
    base: dict[str, Any] = {
        "source_format": "pdf",
        "account": {"last4": "7306", "name": "Everyday Spending", "currency": "USD"},
        "period_start": "2026-04-01",
        "period_end": "2026-04-30",
        "opening_balance": {"amount": "100.00", "as_of": "2026-04-01"},
        "ledger_balance": {"amount": "80.50", "as_of": "2026-04-30"},
        "transactions": [{"posted": "2026-04-03", "amount": "-19.50", "payee": "Lamplight Books"}],
        "parser": {"name": "statement_generic", "version": "7", "kind": "statement"},
    }
    return Statement.model_validate({**base, **changes})


def test_ref_ignores_balances_rows_period_and_parser() -> None:
    first = _statement()
    second = _statement(
        period_start="2026-04-02",
        period_end="2026-05-01",
        opening_balance={"amount": "101.00", "as_of": "2026-04-02"},
        ledger_balance={"amount": "75.00", "as_of": "2026-05-01"},
        transactions=[{"posted": "2026-04-09", "amount": "-26.00", "payee": "Corner Bakery"}],
        parser={"name": "statement_generic", "version": "8", "kind": "statement"},
    )
    assert statement_key(first) != statement_key(second)
    assert statement_ref("statement", first, 0) == statement_ref("statement", second, 0)


def test_ref_differs_by_account_digits_name_currency_and_target() -> None:
    base = _statement()
    account = base.account.model_dump(mode="json")
    refs = {
        statement_ref("statement", base, 0),
        statement_ref("statement", _statement(account={**account, "last4": "7307"}), 0),
        statement_ref("statement", _statement(account={**account, "name": "Rainy Day"}), 0),
        statement_ref("statement", _statement(account={**account, "currency": "EUR"}), 0),
        statement_ref("investment", base, 0),
        statement_ref("statement", base, 1),
    }
    assert len(refs) == 6
    # The name is compared without case: the same account printed another way.
    shouted = _statement(account={**account, "name": "EVERYDAY SPENDING"})
    assert statement_ref("statement", shouted, 0) == statement_ref("statement", base, 0)
    brokerage = InvestmentStatement.model_validate(
        {"source_format": "pdf", "account": account, "as_of": "2026-04-30"}
    )
    assert statement_ref("investment", brokerage, 0) == statement_ref("investment", base, 0)


def test_two_statements_of_one_account_get_ordinals() -> None:
    april = _statement()
    may = _statement(period_start="2026-05-01", period_end="2026-05-31")
    other = _statement(account={"last4": "9914", "currency": "USD"})
    refs = statement_refs("statement", [april, other, may])
    assert len(set(refs)) == 3
    assert refs == [
        statement_ref("statement", april, 0),
        statement_ref("statement", other, 0),
        statement_ref("statement", may, 1),
    ]


def test_ref_has_no_digits() -> None:
    ref = statement_ref("statement", _statement(), 3)
    assert len(ref) == 64
    assert not any(c.isdigit() for c in ref)
    assert payload_ref({REF_FIELD: ref}) == ref
    assert payload_ref({REF_FIELD: ref[:-1] + "9"}) is None
    assert payload_ref({}) is None


def test_a_placement_is_found_by_its_key_first_then_by_its_ref() -> None:
    statement = _statement()
    key, ref = statement_key(statement), statement_ref("statement", statement, 0)
    recorded = placement("statement", key, ACCOUNT, ref)
    assert recorded[REF_FIELD] == ref
    assert REF_FIELD not in placement("statement", key, ACCOUNT)

    answers = StatementAnswers()
    answers.place("statement", key, ACCOUNT, ref)
    corrected = statement_key(_statement(ledger_balance={"amount": "80.00", "as_of": "2026-04-30"}))
    assert answers.account("statement", corrected) is None
    assert answers.account("statement", corrected, ref) == ACCOUNT
    # The exact key wins over the reference.
    answers.place("statement", corrected, OTHER)
    assert answers.account("statement", corrected, ref) == OTHER
    # "Import anyway" and "import as read" stay with the statement as read.
    assert not answers.overlap("statement", corrected, ACCOUNT)
    assert not answers.as_read("statement", corrected, ACCOUNT)
    assert answers.account("investment", corrected, ref) is None
