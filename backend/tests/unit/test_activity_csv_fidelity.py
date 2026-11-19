# SPDX-License-Identifier: AGPL-3.0-or-later
"""Fidelity-style activity exports: the rows and layouts rc.2 did not read.

Masked account numbers, account nicknames (retirement accounts), cash
management and retirement wording, ``, "quoted"`` fields, short exports,
the activity-mode column mapping and its suggestion. Every value is
invented.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.imports.schemas import MappingSpec
from app.domain.imports.service import spec_digest
from app.ingestion.extract.delimited import extract_delimited, spaced_quotes
from app.ingestion.intake.sniff import FileType, sniff
from app.ingestion.parsers.brokerage import activity_type
from app.ingestion.parsers.brokerage_table import (
    account_type_for,
    activity_spec,
    read_activity_table,
    suggest_activity_mapping,
)
from app.ingestion.sandbox.protocol import ExtractOptions, Table
from tests.fixtures.synth_activity_csv import DISCLAIMERS, HEADER


def _table(rows: list[list[str]]) -> Table:
    return Table(
        encoding="utf-8",
        delimiter=",",
        has_header=True,
        rows=rows,
        column_count=max(len(r) for r in rows),
    )


def _row(
    action: str,
    *,
    name: str = "Individual",
    account: str = "Z00001111",
    symbol: str = "",
    quantity: str = "0",
    amount: str = "",
    price: str = "",
    run: str = "10/14/2026",
) -> list[str]:
    return [
        run,
        name,
        account,
        action,
        symbol,
        "SYNTHETIC GROWTH INDEX ETF" if symbol else "No Description",
        "Cash",
        "0",
        "",
        "USD",
        price,
        quantity,
        "0",
        "",
        "",
        "",
        amount,
        "",
    ]


def _with(*rows: list[str]) -> Table:
    return _table([list(HEADER), *rows, [DISCLAIMERS[0]]])


# ---------------------------------------------------------------------------
# Action words
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("DEBIT CARD PURCHASE SYNTHETIC SHOP", "withdrawal"),
        ("CHECK PAID # 1001", "withdrawal"),
        ("BILL PAYMENT SYNTHETIC UTILITY", "withdrawal"),
        ("DIRECT DEBIT SYNTHETIC INSURANCE", "withdrawal"),
        ("MARGIN INTEREST PAID", "fee"),
        ("FED TAX WITHHELD", "fee"),
        ("NON-RESIDENT TAX", "fee"),
        ("PARTIC CONTR CURRENT YEAR", "deposit"),
        ("CASH CONTRIBUTION CURRENT YEAR (Cash)", "deposit"),
        ("CASH CONTRIBUTION PRIOR YEAR (Cash)", "deposit"),
        ("ROTH CONTRIBUTION CURRENT YEAR", "deposit"),
        ("IRA CONTRIBUTION PRIOR YEAR", "deposit"),
        ("EMPLOYEE CONTRIBUTION SYNTHETIC PLAN", "deposit"),
        ("RETURN OF CAPITAL SYNTHETIC FUND", "deposit"),
        ("REDEMPTION FROM CORE ACCOUNT SYNTHETIC MONEY MARKET (SMMXX)", "sell"),
        ("PURCHASE INTO CORE ACCOUNT SYNTHETIC MONEY MARKET (SMMXX)", "buy"),
    ],
)
def test_cash_management_and_retirement_words(text: str, kind: str) -> None:
    assert activity_type(text) == kind


def test_a_cash_contribution_row_imports_as_a_deposit() -> None:
    found = read_activity_table(
        _with(
            _row(
                "CASH CONTRIBUTION CURRENT YEAR (Cash)",
                name="ROTH IRA",
                account="Z3xxxxxxxx",
                quantity="0",
                price="0",
                amount="275",
            )
        )
    )
    assert found is not None
    assert found.unknown_activity == 0
    (statement,) = found.statements
    assert [(a.type, a.amount) for a in statement.activity] == [("deposit", Decimal("275"))]


def test_atm_cash_and_fee_rebates_take_their_direction_from_the_amount() -> None:
    assert activity_type("ADJUST FEE CHARGED ATM FEE REBATE (Cash)", cash_sign=1) == "deposit"
    assert activity_type("ADJUST FEE CHARGED SYNTHETIC FEE (Cash)", cash_sign=-1) == "withdrawal"
    advance = "CASH ADVANCE SYNTHETIC BANK SOMEWHERE ZZ 010126 AUTHID:000000 (Cash)"
    assert activity_type(advance, cash_sign=-1) == "withdrawal"
    assert activity_type("CASH ADVANCE SYNTHETIC BANK (Cash)") is None


def test_atm_cash_and_fee_rebate_rows_import() -> None:
    found = read_activity_table(
        _with(
            _row(
                "ADJUST FEE CHARGED ATM FEE REBATE (Cash)",
                name="Cash Management (Individual)",
                price="0",
                amount="3",
            ),
            _row(
                "CASH ADVANCE SYNTHETIC BANK SOMEWHERE ZZ 010126 AUTHID:000000 (Cash)",
                name="Cash Management (Individual)",
                price="0",
                amount="-123",
            ),
        )
    )
    assert found is not None
    assert found.unknown_activity == 0
    (statement,) = found.statements
    assert sorted((a.type, a.amount) for a in statement.activity) == [
        ("deposit", Decimal("3")),
        ("withdrawal", Decimal("-123")),
    ]


def test_coins_moved_in_or_out_by_quantity_and_symbol() -> None:
    assert activity_type("0.01234500 SYN deposited", moves_security=True) == "transfer_in"
    assert activity_type("1 SYN received", moves_security=True) == "transfer_in"
    assert activity_type("0.50000000 SYN withdrawn", moves_security=True) == "transfer_out"
    assert activity_type("0.50000000 SYN sent", moves_security=True) == "transfer_out"
    # Not a security on the row: cash in or out.
    assert activity_type("12.00 USD deposited") == "deposit"
    # A leading number before any other word is not this phrase.
    assert activity_type("0.5 SYN staked") is None


def test_a_crypto_deposit_row_imports_as_units_in() -> None:
    row = _row(
        "0.01234500 SYN deposited",
        name="Synthetic Crypto",
        symbol="SYN",
        quantity="0.012345",
        amount="321.50",
    )
    found = read_activity_table(_with(row))
    assert found is not None
    assert found.unknown_activity == 0
    (statement,) = found.statements
    (activity,) = statement.activity
    assert activity.security is not None
    assert (activity.type, activity.security.symbol, activity.quantity) == (
        "transfer_in",
        "SYN",
        Decimal("0.012345"),
    )


def test_an_unreadable_row_is_recorded_by_its_position() -> None:
    found = read_activity_table(
        _with(
            _row("DIVIDEND RECEIVED SGRX", symbol="SGRX", amount="4"),
            _row("DIVIDEND RECEIVED SGRX", symbol="SGRX", amount="not a number"),
        )
    )
    assert found is not None
    assert (found.unknown_rows, found.unreadable_rows) == ([], [2])


def test_signed_words_take_their_direction_from_the_row() -> None:
    assert activity_type("JOURNALED CASH", cash_sign=-1) == "withdrawal"
    assert activity_type("JOURNALED CASH", cash_sign=1) == "deposit"
    assert activity_type("JOURNALED SGRX", moves_security=True, units_sign=1) == "transfer_in"
    assert activity_type("ROLLOVER SHARES", moves_security=True, units_sign=-1) == "transfer_out"
    assert activity_type("EXCHANGED TO SGRX", moves_security=True, units_sign=1) == "buy"
    assert activity_type("EXPIRED CALL", moves_security=True, units_sign=-1) == "sell"
    # Without a sign the direction is never guessed.
    assert activity_type("JOURNALED CASH") is None


def test_journal_rows_read_by_their_signs() -> None:
    found = read_activity_table(
        _with(
            _row(
                "YOU BOUGHT SYNTHETIC GROWTH INDEX ETF (SGRX) (Cash)",
                symbol="SGRX",
                quantity="2",
                price="10",
                amount="-20",
            ),
            _row("JOURNALED CASH (Cash)", amount="-30.00"),
            _row("JOURNALED CASH (Cash)", amount="45.00"),
            _row("DEBIT CARD PURCHASE SYNTHETIC SHOP", amount="-12.50"),
        )
    )
    assert found is not None
    assert found.unknown_activity == 0
    (statement,) = found.statements
    kinds = sorted((a.type, a.amount) for a in statement.activity)
    assert kinds == [
        ("buy", Decimal("-20")),
        ("deposit", Decimal("45.00")),
        ("withdrawal", Decimal("-30.00")),
        ("withdrawal", Decimal("-12.50")),
    ]


def test_unknown_actions_are_listed_by_their_first_words_without_digits() -> None:
    found = read_activity_table(
        _with(
            _row(
                "YOU BOUGHT SYNTHETIC GROWTH INDEX ETF (SGRX) (Cash)",
                symbol="SGRX",
                quantity="2",
                amount="-20",
            ),
            _row("MYSTERY 12345 THING HAPPENED", amount="-3"),
        )
    )
    assert found is not None
    assert found.unknown_actions == {"mystery # thing": 1}
    # Positions in the table (row 0 is the header): the queue shows the line.
    assert (found.header.row, found.unknown_rows, found.unreadable_rows) == (0, [2], [])
    assert found.summary()["unknown_actions"] == [{"action": "mystery # thing", "rows": 1}]


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


def test_masked_account_numbers_keep_accounts_apart() -> None:
    found = read_activity_table(
        _with(
            _row("DIVIDEND RECEIVED SGRX", symbol="SGRX", amount="4", account="Z2xxxxxxxx"),
            _row(
                "DIVIDEND RECEIVED SGRX",
                symbol="SGRX",
                amount="5",
                name="ROTH IRA",
                account="Z3xxxxxxxx",
            ),
        )
    )
    assert found is not None
    first, second = found.statements
    assert (first.account.last4, second.account.last4) == (None, None)
    assert (first.account.name, second.account.name) == ("Individual", "ROTH IRA")
    assert (first.account.account_type, second.account.account_type) == (
        "brokerage",
        "retirement",
    )


@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("ROTH IRA", "retirement"),
        ("Traditional IRA", "retirement"),
        ("401(k) PLAN", "retirement"),
        ("Health Savings Account HSA", "retirement"),
        ("Rollover IRA", "retirement"),
        ("Individual", "brokerage"),
        ("Joint WROS", "brokerage"),
        (None, "brokerage"),
    ],
)
def test_account_type_from_the_nickname(name: str | None, kind: str) -> None:
    assert account_type_for(name) == kind


def test_the_fidelity_layout_names_its_institution() -> None:
    found = read_activity_table(_with(_row("DIVIDEND RECEIVED SGRX", symbol="SGRX", amount="4")))
    assert found is not None
    assert found.statements[0].account.institution == "Fidelity Investments"


# ---------------------------------------------------------------------------
# Reading the file
# ---------------------------------------------------------------------------


def _spaced(rows: list[list[str]]) -> bytes:
    def cell(value: str) -> str:
        return f'"{value}"' if "," in value or not value else value

    return "\r\n".join(", ".join(cell(v) for v in row) for row in rows).encode()


def test_quoted_fields_after_a_space_do_not_shift_columns() -> None:
    rows = [
        list(HEADER),
        _row(
            "YOU BOUGHT SYNTHETIC CO, INC (SGRX) (Cash)",
            symbol="SGRX",
            quantity="1",
            price="10",
            amount="-10",
        ),
    ]
    data = _spaced(rows)
    assert spaced_quotes(data.decode(), ",")
    table = extract_delimited(data, ExtractOptions(), "csv").tables[0]
    assert table.rows[1][3] == "YOU BOUGHT SYNTHETIC CO, INC (SGRX) (Cash)"
    found = read_activity_table(table)
    assert found is not None
    (activity,) = found.statements[0].activity
    assert (activity.type, activity.quantity, activity.amount) == ("buy", Decimal(1), Decimal(-10))


def test_plain_csv_keeps_leading_spaces_as_before() -> None:
    assert not spaced_quotes('a,b\r\n"x, y",z\r\n', ",")


def test_a_short_export_with_long_disclaimers_is_still_csv() -> None:
    lines = [
        ",".join(HEADER),
        ",".join(_row("DIVIDEND RECEIVED SGRX", symbol="SGRX", amount="4")),
        *(f'"{DISCLAIMERS[i % 2]}"' for i in range(6)),
        '"Date downloaded 11/05/2026 10:00 am"',
    ]
    assert sniff(("\r\n".join(lines) + "\r\n").encode()) == FileType.CSV


# ---------------------------------------------------------------------------
# The activity-mode mapping
# ---------------------------------------------------------------------------


def test_the_suggestion_maps_every_fidelity_column_it_reads() -> None:
    table = _with(_row("DIVIDEND RECEIVED SGRX", symbol="SGRX", amount="4"))
    spec = activity_spec(table)
    assert spec is not None
    assert spec.mode == "activity"
    mapped = {role: column for role, column in spec.columns().items() if column is not None}
    assert mapped == {
        "date": 0,
        "account_name": 1,
        "account": 2,
        "action": 3,
        "symbol": 4,
        "security": 5,
        "currency": 9,
        "price": 10,
        "quantity": 11,
        "commission": 13,
        "fees": 14,
        "amount": 16,
        "settle": 17,
    }
    suggestion = suggest_activity_mapping(table)
    assert suggestion is not None
    assert suggestion.confident
    assert suggestion.parsed_rows == 1


def test_a_saved_activity_mapping_reads_a_table_of_mostly_unknown_actions() -> None:
    table = _with(
        _row(
            "YOU BOUGHT SYNTHETIC GROWTH INDEX ETF (SGRX) (Cash)",
            symbol="SGRX",
            quantity="1",
            amount="-10",
        ),
        _row("MYSTERY ONE", amount="-1"),
        _row("MYSTERY TWO", amount="-2"),
    )
    assert read_activity_table(table) is None  # not recognised on its own
    spec = activity_spec(table)
    assert spec is not None
    found = read_activity_table(table, spec)
    assert found is not None
    assert (found.rows, found.unknown_activity) == (1, 2)
    suggestion = suggest_activity_mapping(table)
    assert suggestion is not None
    assert "activity_type_unknown" in suggestion.doubts


def test_activity_specs_validate() -> None:
    base = {"date_column": 0, "date_format": "MM/DD/YYYY", "amount_column": 16}
    MappingSpec.model_validate({**base, "mode": "activity", "action_column": 3, "symbol_column": 4})
    with pytest.raises(ValidationError):
        MappingSpec.model_validate({**base, "mode": "activity", "symbol_column": 4})
    with pytest.raises(ValidationError):
        MappingSpec.model_validate({**base, "mode": "activity", "action_column": 3})
    with pytest.raises(ValidationError):
        MappingSpec.model_validate(
            {**base, "mode": "activity", "action_column": 3, "symbol_column": 4, "payee_column": 5}
        )
    with pytest.raises(ValidationError):  # activity columns need activity mode
        MappingSpec.model_validate({**base, "symbol_column": 4})


def test_cash_spec_digests_do_not_change() -> None:
    spec = MappingSpec.model_validate(
        {"date_column": 0, "date_format": "MM/DD/YYYY", "amount_column": 2, "account_column": 1}
    )
    data = spec.model_dump(mode="json")
    assert "mode" in data
    old = {
        k: v
        for k, v in data.items()
        if k not in MappingSpec.model_fields
        or k
        in {
            "version",
            "skip_rows",
            "has_header",
            "date_column",
            "date_format",
            "amount_column",
            "sign",
            "indicator_column",
            "outflow_values",
            "debit_column",
            "credit_column",
            "decimal_separator",
            "payee_column",
            "memo_column",
            "balance_column",
            "currency_column",
            "currency",
            "account_column",
        }
    }
    expected = hashlib.sha256(
        json.dumps(old, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    assert spec_digest(spec) == expected
