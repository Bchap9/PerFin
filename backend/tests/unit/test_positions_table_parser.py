# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brokerage positions exports -> investment statements (``app.ingestion.parsers.positions_table``).

The synthetic export of ``tests/fixtures/synth_positions_csv.py`` (the real
header, invented accounts, funds and amounts) through the real delimited
extractor, and hand-built tables for each rule of the parser's module doc.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.extract.delimited import extract_delimited
from app.ingestion.parsers.brokerage_table import read_activity_table
from app.ingestion.parsers.positions_table import (
    PARSER_NAME,
    RECONCILIATION_DOUBT,
    downloaded_on,
    find_header,
    is_positions_snapshot,
    read_number,
    read_positions_table,
)
from app.ingestion.sandbox.protocol import ExtractOptions, Table
from tests.fixtures.synth_activity_csv import activity_accounts, activity_csv_bytes, investment_rows
from tests.fixtures.synth_positions_csv import (
    BOND,
    GROWTH,
    HEADER,
    VALUE,
    Holding,
    PositionsAccount,
    positions_csv_bytes,
    two_accounts,
)

DAY = date(2026, 11, 11)


def _extract(data: bytes) -> Table:
    return extract_delimited(data, ExtractOptions(), "csv").tables[0]


def _table(rows: list[list[str]]) -> Table:
    return Table(
        encoding="utf-8",
        delimiter=",",
        has_header=True,
        rows=rows,
        column_count=max(len(r) for r in rows),
    )


def _row(
    symbol: str,
    description: str = "SYNTHETIC GROWTH INDEX FUND",
    *,
    quantity: str = "10",
    price: str = "$5.00",
    value: str = "$50.00",
    cost: str = "$40.00",
    percent: str = "",
    number: str = "Z40001111",
    name: str = "Individual",
) -> list[str]:
    return [
        number,
        name,
        symbol,
        description,
        quantity,
        price,
        "",
        value,
        "",
        "",
        "",
        "",
        percent,
        cost,
        "",
        "Cash",
    ]


def _with(
    *rows: list[str], downloaded: str | None = "Date downloaded Nov-11-2026 7:35 p.m ET"
) -> Table:
    note = [[downloaded, *[""] * (len(HEADER) - 1)]] if downloaded is not None else []
    return _table([list(HEADER), *rows, *note])


# ---------------------------------------------------------------------------
# The export as a whole
# ---------------------------------------------------------------------------


def test_reads_each_account_with_its_holdings_cash_and_date() -> None:
    accounts = two_accounts()
    found = read_positions_table(_extract(positions_csv_bytes(accounts)))
    assert found is not None
    assert found.as_of == DAY
    assert found.as_of_assumed is False
    assert (found.positions, found.cash_rows, found.skipped) == (3, 2, 0)
    assert found.notes == 3  # two disclaimers and the download line
    assert found.warnings == []
    taxable, roth = found.statements
    assert taxable.source_format == "csv"
    assert (taxable.account.last4, taxable.account.name) == ("1111", "Individual")
    assert taxable.account.account_type == "brokerage"
    assert taxable.account.institution == "Fidelity Investments"
    assert (roth.account.last4, roth.account.account_type) == ("2222", "retirement")
    assert (taxable.as_of, taxable.period_start, taxable.period_end) == (DAY, DAY, DAY)
    assert taxable.activity == []
    assert [
        (p.security.symbol, p.quantity, p.unit_price, p.market_value, p.cost_basis)
        for p in taxable.positions
    ] == [(h.symbol, h.quantity, h.price, h.value, h.cost) for h in (GROWTH, BOND)]
    assert [p.security.asset_class for p in taxable.positions] == ["mutual_fund", "mutual_fund"]
    assert [p.security.name for p in roth.positions] == [VALUE.name]
    assert all(p.price_as_of == DAY and p.low_confidence == [] for p in taxable.positions)
    # The core position is the account's cash, never a holding.
    assert taxable.cash_closing is not None
    assert taxable.cash_closing.amount == Decimal("1000.75")
    assert taxable.total_value is not None
    assert taxable.total_value.amount == accounts[0].total
    check = taxable.reconciliation
    assert check is not None
    assert (check.status, check.checks, check.failed) == ("ok", ["positions", "total_value"], [])
    # Nothing else could be in doubt: there is no activity.
    assert (check.doubtful_activity, check.doubtful_positions) == ([], [])
    assert all(is_positions_snapshot(s) for s in found.statements)
    assert found.parser.name == PARSER_NAME


def test_the_summary_carries_counts_only() -> None:
    accounts = two_accounts()
    found = read_positions_table(_extract(positions_csv_bytes(accounts)))
    assert found is not None
    summary = found.summary()
    assert summary["statements"] == 2
    assert summary["as_of"] == "2026-11-11"
    assert summary["accounts"] == [
        {"account_type": "brokerage", "positions": 2},
        {"account_type": "retirement", "positions": 1},
    ]
    text = str(summary)
    for account in accounts:
        assert account.number not in text
        assert account.last4 not in text
        assert account.name not in text
    assert GROWTH.symbol not in text


def test_a_hand_edited_sample_with_masked_numbers_keeps_its_accounts_apart() -> None:
    """Every number masked alike, lower-case headings, a value that is not quantity x price."""
    header = [cell.capitalize() for cell in HEADER]
    table = _table(
        [
            header,
            _row(
                "XXX",
                "SYNTHETIC LARGE CAP GROWTH INDEX FUND",
                quantity="52.069",
                price="$45.78 ",
                value="$1,000.71 ",
                cost="$2,237.68 ",
                percent="40.32%",
                number="XXX",
            ),
            _row(
                "XXX",
                "HELD IN MONEY MARKET",
                quantity="",
                price="",
                value="$1,000.75 ",
                cost="",
                percent="100.00%",
                number="XXX",
                name="Cash Management (Individual)",
            ),
            ["Date downloaded Nov-11-2026 7:35 p.m ET", *[""] * 15],
        ]
    )
    found = read_positions_table(table)
    assert found is not None
    fund, cash = found.statements
    assert (fund.account.name, fund.account.last4) == ("Individual", None)
    assert (cash.account.name, cash.account.last4) == ("Cash Management (Individual)", None)
    # 52.069 x 45.78 is 2,383.72, not the 1,000.71 printed: that position is in doubt.
    (position,) = fund.positions
    assert position.low_confidence == [RECONCILIATION_DOUBT]
    assert fund.reconciliation is not None
    assert fund.reconciliation.failed == ["positions"]
    assert fund.reconciliation.doubtful_positions == [0]
    assert fund.cash_closing is None
    # Its one row is 40.32% of the account: the rows are not the whole of it.
    assert fund.total_value is not None
    assert fund.total_value.amount == Decimal("2481.92")
    # The cash account is whole: one row, 100% of it.
    assert cash.positions == []
    assert cash.cash_closing is not None
    assert cash.cash_closing.amount == Decimal("1000.75")
    assert cash.reconciliation is not None
    assert (cash.reconciliation.status, cash.reconciliation.checks) == ("ok", ["total_value"])


# ---------------------------------------------------------------------------
# Recognising
# ---------------------------------------------------------------------------


def test_an_activity_export_is_not_a_positions_export_and_the_reverse() -> None:
    activity = _extract(activity_csv_bytes(investment_rows(activity_accounts(5))))
    assert find_header(activity) is None
    assert read_positions_table(activity, received_on=DAY) is None
    positions = _extract(positions_csv_bytes(two_accounts()))
    assert read_activity_table(positions) is None


def test_a_table_without_the_three_columns_is_not_read() -> None:
    ledger = _table([["Date", "Description", "Amount"], ["11/01/2026", "COFFEE", "-4.50"]])
    assert read_positions_table(ledger, received_on=DAY) is None
    no_value = _table([["Symbol", "Quantity", "Price"], ["SGRX", "10", "5.00"]])
    assert read_positions_table(no_value, received_on=DAY) is None


def test_the_header_alone_never_routes_a_file() -> None:
    assert read_positions_table(_with()) is None
    # Fewer than half of the rows read: not a positions export after all.
    junk = [_row("", "", quantity="", price="", value="x", cost="") for _ in range(3)]
    assert read_positions_table(_with(_row("SGRX"), *junk)) is None


def test_a_generic_holdings_layout_reads_without_an_account_column() -> None:
    table = _table(
        [
            ["Ticker", "Security Name", "Shares", "Share Price", "Total Value", "Cost Basis"],
            ["SGRX", "SYNTHETIC GROWTH INDEX FUND", "10", "5.00", "50.00", "41.00"],
            ["SVAL", "SYNTHETIC VALUE INDEX ETF", "2", "80.00", "160.00", "--"],
        ]
    )
    assert read_positions_table(table) is None  # no download date, none given
    found = read_positions_table(table, received_on=DAY)
    assert found is not None
    assert (found.as_of, found.as_of_assumed, found.warnings) == (DAY, True, ["as_of_assumed"])
    (statement,) = found.statements
    assert (statement.account.last4, statement.account.name) == (None, None)
    assert statement.account.institution is None
    assert [(p.security.symbol, p.cost_basis) for p in statement.positions] == [
        ("SGRX", Decimal("41.00")),
        ("SVAL", None),
    ]
    # No cash row and no percentages: nothing says what the account is worth.
    assert (statement.cash_closing, statement.total_value) == (None, None)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("$1,000.71 ", Decimal("1000.71")),
        ("+$53.11", Decimal("53.11")),
        ("-$250.00", Decimal("-250.00")),
        ("$-250.00", Decimal("-250.00")),
        ("(12.50)", Decimal("-12.50")),
        ("2.27%", Decimal("2.27")),
        ("0.00012345", Decimal("0.00012345")),
        (".5", Decimal("0.5")),
        ("n/a", None),
        ("--", None),
        ("", None),
        ("1.2.3", None),
        ("=1+1", None),
    ],
)
def test_read_number(text: str, value: Decimal | None) -> None:
    assert read_number(text) == value


@pytest.mark.parametrize(
    ("note", "day"),
    [
        ("Date downloaded Nov-11-2026 7:35 p.m ET", DAY),
        ("Date downloaded 11/11/2026 10:04 am", DAY),
        ("Date downloaded: 2026-11-11", DAY),
        ("Date downloaded November 11, 2026", DAY),
        ("Date downloaded Xyz-11-2026", None),
        ("Date downloaded 02/30/2026", None),
        ("Downloaded sometime", None),
    ],
)
def test_the_download_note_dates_the_positions(note: str, day: date | None) -> None:
    assert downloaded_on([["a", "b"], [note, "", ""]]) == day


def test_cash_rows_are_cash_whatever_they_are_called() -> None:
    found = read_positions_table(
        _with(
            _row("SGRX"),
            _row("SCASH**", "HELD IN MONEY MARKET", quantity="", price="", value="$10.00", cost=""),
            _row("CORE**", "INSURED DEPOSIT SWEEP", quantity="", price="", value="$5.00", cost=""),
            _row("Pending Activity", "", quantity="", price="", value="-$2.50", cost=""),
            # A money market fund held like any fund (units and a price) is a holding.
            _row("SMMF", "SYNTHETIC MONEY MARKET FUND", quantity="7", price="$1.00", value="$7.00"),
        )
    )
    assert found is not None
    (statement,) = found.statements
    assert statement.cash_closing is not None
    assert statement.cash_closing.amount == Decimal("12.50")
    assert [p.security.symbol for p in statement.positions] == ["SGRX", "SMMF"]
    assert statement.positions[1].security.asset_class == "cash_equivalent"
    assert (found.positions, found.cash_rows) == (2, 3)


def test_bonds_options_and_short_positions() -> None:
    found = read_positions_table(
        _with(
            # A bond: quantity is face value, the price is per 100 of it.
            _row(
                "912828YK0",
                "SYNTHETIC TREASURY NOTE",
                quantity="10000",
                price="$99.50",
                value="$9,950.00",
                cost="$9,900.00",
            ),
            # An option: one contract is 100 shares.
            _row(
                "-SGRX270115C50",
                "SGRX JAN 15 2027 50 CALL",
                quantity="2",
                price="$1.25",
                value="$250.00",
                cost="$180.00",
            ),
            # A short position.
            _row(
                "SVAL",
                "SYNTHETIC VALUE INDEX ETF",
                quantity="-3",
                price="$80.00",
                value="-$240.00",
                cost="$250.00",
            ),
        )
    )
    assert found is not None
    bond, option, short = found.statements[0].positions
    assert (bond.security.cusip, bond.security.symbol) == ("912828YK0", None)
    assert (bond.price_per_100, bond.price_per_unit) == (True, Decimal("0.995"))
    assert (option.unit_price, option.price_per_100) == (Decimal("125.00"), False)
    assert (short.quantity, short.market_value) == (Decimal(-3), Decimal("-240.00"))
    check = found.statements[0].reconciliation
    assert check is not None
    assert check.failed == []


def test_a_security_without_a_symbol_is_named_by_its_description() -> None:
    found = read_positions_table(
        _with(
            _row("", "SYNTHETIC STABLE VALUE POOL"), _row("NOT A SYMBOL", "SYNTHETIC TARGET 2050")
        )
    )
    assert found is not None
    first, second = found.statements[0].positions
    assert (first.security.symbol, first.security.name) == (None, "SYNTHETIC STABLE VALUE POOL")
    assert (second.security.symbol, second.security.name) == (None, "SYNTHETIC TARGET 2050")


def test_a_formula_is_never_a_symbol_and_names_stay_guarded() -> None:
    found = read_positions_table(_with(_row("'=HYPERLINK(1)", "'=cmd|' /C calc'!A0"), _row("SGRX")))
    assert found is not None
    position = found.statements[0].positions[0]
    assert position.security.symbol is None
    assert position.security.name is not None
    assert position.security.name.startswith("'=")


def test_a_row_that_does_not_read_leaves_its_account_without_a_value() -> None:
    whole = PositionsAccount("240002222", "ROTH IRA", (VALUE,), Decimal("25.00"))
    broken = PositionsAccount(
        "Z40001111",
        "Individual",
        (GROWTH,),
        Decimal("10.00"),
        extra=(tuple(_row("SBND", "SYNTHETIC TOTAL BOND FUND", quantity="", value="$410.00")),),
    )
    found = read_positions_table(_extract(positions_csv_bytes([broken, whole])))
    assert found is not None
    assert (found.skipped, found.warnings) == (1, ["rows_unreadable"])
    first, second = found.statements
    assert [p.security.symbol for p in first.positions] == [GROWTH.symbol]
    assert (first.cash_closing, first.total_value) == (None, None)
    # The other account is untouched by it.
    assert second.cash_closing is not None
    assert second.total_value is not None


def test_percentages_that_do_not_add_up_fail_the_total_check() -> None:
    account = PositionsAccount("Z40001111", "Individual", (GROWTH, BOND), Decimal("100.00"))
    table = _extract(positions_csv_bytes([account]))
    # The bond fund's row is removed from the file: the rest is 85% of the account.
    cut = table.model_copy(update={"rows": [r for r in table.rows if BOND.symbol not in r]})
    found = read_positions_table(cut)
    assert found is not None
    (statement,) = found.statements
    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.failed) == ("mismatch", ["total_value"])
    assert check.reported_total_value is not None
    # What the percentages imply is close to the whole account, not to what is left.
    assert abs(check.reported_total_value - account.total) < Decimal("1.00")
    assert check.total_value == account.total - BOND.value
    # No position is to blame for it.
    assert check.doubtful_positions == []
    assert all(p.low_confidence == [] for p in statement.positions)


def test_two_rows_of_one_security_and_many_decimals() -> None:
    tiny = Holding("SBTC", "SYNTHETIC COIN", Decimal("0.00012345"), Decimal("64000"), None)
    account = PositionsAccount("Z40001111", "Individual", (GROWTH, GROWTH, tiny), Decimal("1.00"))
    found = read_positions_table(_extract(positions_csv_bytes([account])))
    assert found is not None
    (statement,) = found.statements
    assert [p.quantity for p in statement.positions] == [
        GROWTH.quantity,
        GROWTH.quantity,
        Decimal("0.00012345"),
    ]
    assert statement.positions[2].cost_basis is None
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_a_statement_from_another_reader_is_not_a_snapshot() -> None:
    found = read_positions_table(_extract(positions_csv_bytes(two_accounts())))
    assert found is not None
    statement = found.statements[0]
    assert is_positions_snapshot(statement)
    assert not is_positions_snapshot(statement.model_copy(update={"parser": None}))
    assert not is_positions_snapshot(statement.model_copy(update={"source_format": "ofx"}))
    assert not is_positions_snapshot(statement.model_copy(update={"as_of": None}))
