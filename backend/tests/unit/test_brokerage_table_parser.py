# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brokerage activity tables -> investment statements (``app.ingestion.parsers.brokerage_table``).

Tables are synthetic: the Fidelity-style "Activity" export of
``tests/fixtures/synth_activity_csv.py`` (the real export's header, invented
accounts, funds and amounts) and small hand-written tables, run through the
real delimited and XLSX extractors in-process.
"""

from __future__ import annotations

import csv
import io
from collections import Counter
from datetime import date
from decimal import Decimal

import pytest

from app.domain.imports.service import activity_table
from app.ingestion.extract.registry import builtin_extractors
from app.ingestion.parsers import confidence as conf
from app.ingestion.parsers.brokerage_table import (
    PARSER_NAME,
    PARSER_VERSION,
    find_header,
    read_activity_table,
)
from app.ingestion.sandbox.protocol import (
    ExtractOptions,
    ExtractResult,
    InvestmentStatement,
    Table,
)
from tests.fixtures.synth_activity_csv import (
    DISCLAIMERS,
    HEADER,
    LEDGER_HEADER,
    ActivityRow,
    activity_accounts,
    activity_csv_bytes,
    activity_rows,
    investment_rows,
)
from tests.fixtures.synth_statements import LAYOUTS, csv_bytes, layout_statement, sample_statement
from tests.fixtures.synth_xlsx import workbook

EXTRACTORS = builtin_extractors()
ACCOUNTS = activity_accounts(7)


def _extract(data: bytes, kind: str = "csv") -> Table:
    return EXTRACTORS[kind](data, ExtractOptions(), kind).tables[0]


def _table(rows: list[list[str]]) -> Table:
    return Table(
        encoding="utf-8",
        delimiter=",",
        has_header=True,
        rows=rows,
        column_count=max(len(r) for r in rows),
    )


def _read(table: Table) -> list[InvestmentStatement]:
    found = read_activity_table(table)
    assert found is not None
    return found.statements


def _row(
    action: str,
    *,
    symbol: str = "SGRX",
    quantity: str = "0",
    amount: str = "",
    price: str = "",
    run: str = "10/14/2026",
    account: str = "Z00001111",
    currency: str = "USD",
    settle: str = "",
) -> list[str]:
    """One row of :data:`HEADER` (the cells this parser reads; the rest as exported)."""
    return [
        run,
        "Individual",
        account,
        action,
        symbol,
        "SYNTHETIC GROWTH INDEX ETF" if symbol else "No Description",
        "Cash",
        "0",
        "",
        currency,
        price,
        quantity,
        "0",
        "",
        "",
        "",
        amount,
        settle,
    ]


def _with(*rows: list[str]) -> Table:
    return _table([["", ""], list(HEADER), *rows, [DISCLAIMERS[0]]])


# ---------------------------------------------------------------------------
# The synthetic Fidelity-style export
# ---------------------------------------------------------------------------


def test_activity_export_becomes_one_statement_per_account() -> None:
    rows = investment_rows(ACCOUNTS)
    found = read_activity_table(_extract(activity_csv_bytes(rows)))
    assert found is not None
    assert found.header.row == 0  # the extractor drops the blank lines above it
    assert found.summary() == {
        "parser": f"{PARSER_NAME}/{PARSER_VERSION}",
        "statements": 2,
        "rows": len(rows),
        "notes": 3,  # two disclaimer paragraphs and the "Date downloaded" line
        "unknown_activity": 0,
        "skipped": 0,
        "warnings": [],
    }
    first, second = found.statements
    assert [first.account.last4, second.account.last4] == [a.last4 for a in ACCOUNTS]
    for statement, account in zip(found.statements, ACCOUNTS, strict=True):
        assert statement.source_format == "csv"
        assert statement.account.currency == "USD"
        assert statement.parser is not None
        assert (statement.parser.name, statement.parser.version) == (PARSER_NAME, PARSER_VERSION)
        assert statement.reconciliation is not None
        assert statement.reconciliation.status == "unavailable"
        assert statement.positions == []
        assert statement.cash_opening is None
        assert statement.cash_closing is None
        mine = [r for r in rows if r.account == account]
        assert statement.period_start == min(r.run_date for r in mine)
        assert statement.period_end == max(r.run_date for r in mine)
        # Oldest first, whatever the export's order.
        dates = [a.trade_date for a in statement.activity]
        assert dates == sorted(dates)
        # Nothing to score: a structured source like OFX.
        assert all(a.confidence is None and not a.low_confidence for a in statement.activity)
    assert Counter(a.type for a in first.activity) == {
        "buy": 2,
        "sell": 1,
        "dividend": 1,
        "reinvest": 1,
        "interest": 1,
        "fee": 1,
        "deposit": 1,
        "withdrawal": 1,
    }
    assert Counter(a.type for a in second.activity) == {
        "deposit": 1,
        "buy": 1,
        "dividend": 1,
        "reinvest": 1,
        "sell": 1,
    }


def test_rows_keep_the_cash_effect_and_carry_positive_quantities() -> None:
    (first, _) = _read(_extract(activity_csv_bytes(investment_rows(ACCOUNTS))))
    by_type = {a.type: a for a in first.activity}
    sale = by_type["sell"]
    assert sale.quantity == Decimal(4)  # printed -4: the type gives the direction
    assert sale.amount == Decimal("1039.95")
    assert sale.unit_price == Decimal("260.00")
    assert sale.fees == Decimal("0.05")  # the commission
    assert sale.settle_date == date(2026, 10, 23)
    assert sale.security is not None
    assert sale.security.symbol == "SGRX"
    assert sale.security.name == "SYNTHETIC GROWTH INDEX ETF"
    assert sale.security.asset_class == "etf"
    buys = [a for a in first.activity if a.type == "buy"]
    assert [b.amount for b in buys] == [Decimal("-2500.00"), Decimal("-1000.05")]
    assert [b.fees for b in buys] == [Decimal(0), Decimal("0.05")]
    reinvest = by_type["reinvest"]
    assert (reinvest.quantity, reinvest.amount) == (Decimal("0.05"), Decimal("-12.80"))
    assert reinvest.settle_date is None
    dividend = by_type["dividend"]
    assert dividend.quantity is None
    assert dividend.amount == Decimal("12.80")
    assert by_type["interest"].security is not None
    assert by_type["fee"].security is None
    assert by_type["fee"].amount == Decimal("-15.00")
    deposit, withdrawal = by_type["deposit"], by_type["withdrawal"]
    assert deposit.amount == Decimal("5000.00")
    assert withdrawal.amount == Decimal("-300.00")
    # The action text is the memo (a cash row's payee on the ledger).
    assert deposit.memo == "Electronic Funds Transfer Received (Cash)"
    assert withdrawal.memo == "Electronic Funds Transfer Paid (Cash)"


def test_full_account_numbers_never_leave_the_parser() -> None:
    found = read_activity_table(_extract(activity_csv_bytes(investment_rows(ACCOUNTS))))
    assert found is not None
    dumped = "".join(s.model_dump_json() for s in found.statements) + str(found.summary())
    for account in ACCOUNTS:
        assert account.number not in dumped
        assert account.number[:-4] not in dumped
        assert account.name not in dumped


def test_the_reinvestment_row_of_a_real_export_reads() -> None:
    """The shape of a real export's row (every value invented): cash type, zero exchange qty."""
    row = _row(
        "REINVESTMENT SYNTHETIC INDEX FUNDS SYNTHETIC GROWTH... (SGRX) (Cash)",
        quantity="0.015",
        price="85.44",
        amount="-1.28",
    )
    (statement,) = _read(_with(row))
    (activity,) = statement.activity
    assert activity.type == "reinvest"
    assert (activity.quantity, activity.unit_price, activity.amount) == (
        Decimal("0.015"),
        Decimal("85.44"),
        Decimal("-1.28"),
    )
    assert statement.account.last4 == "1111"


@pytest.mark.parametrize("kind", ["tsv", "xlsx"])
def test_tsv_and_xlsx_exports_read_the_same(kind: str) -> None:
    rows = investment_rows(ACCOUNTS)
    parsed = list(csv.reader(io.StringIO(activity_csv_bytes(rows).decode())))
    data = "\r\n".join("\t".join(r) for r in parsed).encode() if kind == "tsv" else workbook(parsed)
    expected = _read(_extract(activity_csv_bytes(rows)))
    assert [s.activity for s in _read(_extract(data, kind))] == [s.activity for s in expected]


# ---------------------------------------------------------------------------
# Rows that do not read
# ---------------------------------------------------------------------------


def test_unknown_actions_are_skipped_and_counted_never_guessed() -> None:
    found = read_activity_table(
        _with(
            _row("YOU BOUGHT SYNTHETIC GROWTH INDEX ETF (SGRX) (Cash)", quantity="2", amount="-5"),
            _row("JOURNALED SYNTHETIC SPECIAL ITEM (Cash)", amount="-3.00", symbol=""),
        )
    )
    assert found is not None
    assert found.unknown_activity == 1
    assert found.rows == 1
    assert found.warnings == ["activity_type_unknown"]
    assert [a.type for a in found.statements[0].activity] == ["buy"]


@pytest.mark.parametrize(
    "row",
    [
        _row(
            "YOU BOUGHT SYNTHETIC FUND (Cash)", symbol="", quantity="2", amount="-5"
        ),  # no security
        _row("YOU SOLD SYNTHETIC GROWTH INDEX ETF (SGRX)", quantity="0", amount="5"),  # no units
        _row("DIVIDEND RECEIVED SYNTHETIC GROWTH INDEX ETF (SGRX)", amount=""),  # no amount
        _row("DIVIDEND RECEIVED SYNTHETIC GROWTH INDEX ETF (SGRX)", amount="n/a"),
        _row("DIVIDEND RECEIVED SYNTHETIC GROWTH INDEX ETF (SGRX)", amount="1", run="soon"),
        _row("DIVIDEND RECEIVED SYNTHETIC GROWTH INDEX ETF (SGRX)", amount="1", currency="US$"),
        _row("STOCK SPLIT SYNTHETIC GROWTH INDEX ETF (SGRX)", amount="0", quantity="5"),
    ],
    ids=[
        "no-security",
        "no-quantity",
        "no-amount",
        "bad-amount",
        "bad-date",
        "bad-currency",
        "split-without-ratio",
    ],
)
def test_rows_missing_what_their_type_needs_are_skipped(row: list[str]) -> None:
    good = _row("INTEREST EARNED SYNTHETIC MONEY MARKET (SMMXX)", symbol="SMMXX", amount="1.00")
    found = read_activity_table(_with(good, row))
    assert found is not None
    assert (found.rows, found.skipped) == (1, 1)
    assert "rows_unreadable" in found.warnings


def test_transfers_move_units_or_cash_by_what_the_row_names() -> None:
    (statement,) = _read(
        _with(
            # Units in, no cash printed: a transfer of units that moves no cash.
            _row("TRANSFERRED FROM SYNTHETIC GROWTH INDEX ETF (SGRX)", quantity="3"),
            # No security, a cash amount: a deposit.
            _row("TRANSFERRED FROM SYNTHETIC BANK (Cash)", symbol="", amount="250.00"),
            _row("TRANSFERRED TO SYNTHETIC GROWTH INDEX ETF (SGRX)", quantity="-1", amount="-"),
        )
    )
    kinds = [(a.type, a.quantity, a.amount) for a in statement.activity]
    assert ("transfer_in", Decimal(3), Decimal(0)) in kinds
    assert ("deposit", None, Decimal("250.00")) in kinds
    # "-" is not an amount and a transfer out of units needs one only for cash.
    assert len(kinds) == 2


def test_a_cusip_in_the_symbol_column_is_the_cusip() -> None:
    (statement,) = _read(
        _with(_row("YOU BOUGHT SYNTHETIC NOTE", symbol="037833100", quantity="5", amount="-5"))
    )
    security = statement.activity[0].security
    assert security is not None
    assert (security.symbol, security.cusip) == (None, "037833100")


def test_without_an_account_column_the_table_is_one_statement() -> None:
    header = [c for c in HEADER if c not in ("Account", "Account Number")]
    keep = [i for i, c in enumerate(HEADER) if c in header]
    rows = [
        _row("YOU BOUGHT SYNTHETIC GROWTH INDEX ETF (SGRX)", quantity="2", amount="-5"),
        _row("YOU SOLD SYNTHETIC GROWTH INDEX ETF (SGRX)", quantity="-1", amount="3", account="x"),
    ]
    (statement,) = _read(_table([header, *([r[i] for i in keep] for r in rows)]))
    assert statement.account.last4 is None
    assert len(statement.activity) == 2


def test_an_account_column_with_numbers_is_the_account() -> None:
    header = ["Date", "Account", "Action", "Symbol", "Quantity", "Amount"]
    rows = [
        ["10/14/2026", "Brokerage ...4821", "Buy", "SGRX", "2", "-5.00"],
        ["10/15/2026", "IRA ...9034", "Buy", "SGRX", "1", "-2.50"],
    ]
    assert [s.account.last4 for s in _read(_table([header, *rows]))] == ["4821", "9034"]


def test_a_guessed_date_order_marks_every_row_low_confidence() -> None:
    found = read_activity_table(
        _with(
            _row(
                "YOU BOUGHT SYNTHETIC GROWTH INDEX ETF (SGRX)",
                quantity="2",
                amount="-5",
                run="10/03/2026",
            ),
            _row(
                "YOU SOLD SYNTHETIC GROWTH INDEX ETF (SGRX)",
                quantity="-1",
                amount="3",
                run="11/04/2026",
            ),
        )
    )
    assert found is not None
    assert "date_order_guessed" in found.warnings
    for activity in found.statements[0].activity:
        assert activity.low_confidence == ["trade_date"]
        assert activity.confidence == conf.GUESSED


def test_a_row_in_another_currency_than_its_account_is_skipped() -> None:
    found = read_activity_table(
        _with(
            _row("DIVIDEND RECEIVED SYNTHETIC (SGRX)", amount="1.00"),
            _row("DIVIDEND RECEIVED SYNTHETIC (SGRX)", amount="2.00", run="10/15/2026"),
            _row("DIVIDEND RECEIVED SYNTHETIC (SGRX)", amount="3.00", currency="CAD"),
            _row("DIVIDEND RECEIVED SYNTHETIC (SGRX)", amount="4.00", currency=""),
        )
    )
    assert found is not None
    (statement,) = found.statements
    assert statement.account.currency == "USD"
    assert [a.amount for a in statement.activity] == [
        Decimal("1.00"),
        Decimal("4.00"),
        Decimal("2.00"),
    ]
    assert found.skipped == 1
    assert "currency_mixed" in found.warnings


# ---------------------------------------------------------------------------
# What is not a brokerage activity table
# ---------------------------------------------------------------------------


def test_the_cash_ledger_layout_is_not_brokerage_activity() -> None:
    rows = activity_rows(8, ACCOUNTS)
    assert read_activity_table(_extract(activity_csv_bytes(rows, header=LEDGER_HEADER))) is None


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_bank_layouts_are_not_brokerage_activity(layout: str) -> None:
    stmt = layout_statement(sample_statement(3), layout)
    assert read_activity_table(_extract(csv_bytes(stmt, layout))) is None


def test_a_bank_table_typed_deposit_and_withdrawal_is_not_brokerage_activity() -> None:
    table = _table(
        [
            ["Date", "Type", "Description", "Amount"],
            ["10/14/2026", "Deposit", "Payroll", "100.00"],
            ["10/15/2026", "Withdrawal", "Rent", "-50.00"],
        ]
    )
    assert find_header(table) is None
    assert read_activity_table(table) is None


def test_a_header_whose_rows_are_mostly_untyped_is_not_brokerage_activity() -> None:
    table = _with(
        _row("YOU BOUGHT SYNTHETIC GROWTH INDEX ETF (SGRX)", quantity="2", amount="-5"),
        _row("SOMETHING ELSE", amount="1.00"),
        _row("ANOTHER THING", amount="1.00"),
    )
    assert find_header(table) is not None
    assert read_activity_table(table) is None


def test_the_header_is_found_below_a_preamble() -> None:
    rows = investment_rows(ACCOUNTS)
    data = b"Synthetic Brokerage activity\r\nAll accounts\r\n" + activity_csv_bytes(rows)
    found = read_activity_table(_extract(data))
    assert found is not None
    assert found.header.row == 2
    assert found.rows == len(rows)


def test_only_a_delimited_or_spreadsheet_files_own_table_is_read() -> None:
    table = _extract(activity_csv_bytes(investment_rows(ACCOUNTS)))
    for kind, expected in (("csv", True), ("tsv", True), ("xlsx", True), ("pdf", False)):
        result = ExtractResult(kind=kind, ok=True, tables=[table])
        assert (activity_table(result) is not None) is expected, kind


def test_activity_rows_dataclass_is_written_with_its_fees() -> None:
    row = ActivityRow(
        date(2026, 10, 20),
        ACCOUNTS[0],
        "YOU BOUGHT X (SGRX)",
        "SGRX",
        "X",
        Decimal("1.00"),
        Decimal(1),
        Decimal("-1.10"),
        None,
        fees=Decimal("0.04"),
        commission=Decimal("0.06"),
    )
    (statement,) = _read(_extract(activity_csv_bytes([row, row])))
    assert statement.activity[0].fees == Decimal("0.10")


def test_names_and_memos_stay_formula_guarded() -> None:
    """The extractor's ``'`` guard is set aside only for matching and parsing (C7)."""
    formula = '=HYPERLINK("http://synthetic.invalid/x","c")'
    rows = activity_rows(91, activity_accounts(91))
    first = next(i for i, r in enumerate(rows) if r.symbol and r.action.startswith("YOU BOUGHT"))
    row = rows[first]
    rows[first] = ActivityRow(
        row.run_date,
        row.account,
        row.action,
        row.symbol,
        formula,
        row.price,
        row.quantity,
        row.amount,
        row.settlement,
    )
    table = _extract(activity_csv_bytes(rows))
    # The extractor guarded the cell; the reader keeps the guard on the name.
    assert any(cell == "'" + formula for line in table.rows for cell in line)
    names = {
        a.security.name
        for s in _read(table)
        for a in s.activity
        if a.security is not None and a.security.symbol == row.symbol
    }
    assert "'" + formula in names
    assert formula not in names
    # An action that still types but starts like a formula is a guarded memo.
    guarded = _table(
        [
            list(HEADER),
            ["'" + c if c.startswith("+") else c for c in _row("+DIVIDEND RECEIVED", amount="3")],
        ]
    )
    (statement,) = _read(guarded)
    (activity,) = statement.activity
    assert activity.type == "dividend"
    assert activity.memo == "'+DIVIDEND RECEIVED"
    # A symbol that would be a formula is no symbol: the buy is skipped.
    found = read_activity_table(
        _table([list(HEADER), _row("YOU BOUGHT", symbol="'-SGRX", quantity="1", amount="-5")])
    )
    assert found is not None
    assert found.skipped == 1
    assert found.statements == []
