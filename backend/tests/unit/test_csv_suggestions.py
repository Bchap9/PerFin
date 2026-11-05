# SPDX-License-Identifier: AGPL-3.0-or-later
"""Flexible CSV/TSV mapping: header search, global role scoring, confidence,
empty currency cells and multi-account tables (``app.ingestion.parsers.structured``).

Tables are synthetic: the bank-like layouts of ``tests/fixtures/synth_statements.py``
and the brokerage-like activity export of ``tests/fixtures/synth_activity_csv.py``
(its header is the one real activity exports use; every value is invented),
run through the real delimited extractor in-process.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.domain.imports.schemas import MappingSpec, Sign
from app.ingestion.extract.registry import builtin_extractors
from app.ingestion.parsers.structured import (
    EXACT,
    HEADER_SEARCH_ROWS,
    SUBSTRING,
    WORD,
    _match_level,
    best_date_format,
    cell_last4,
    from_table,
    from_table_grouped,
    group_last4,
    spec_fingerprint,
    suggest_mapping,
    suggest_spec,
)
from app.ingestion.sandbox.protocol import ExtractOptions, Table
from tests.fixtures.synth_activity_csv import (
    COLUMNS,
    HEADER,
    SPEC,
    ActivityRow,
    activity_accounts,
    activity_csv_bytes,
    activity_rows,
)
from tests.fixtures.synth_statements import LAYOUTS, csv_bytes, layout_statement, sample_statement

EXTRACTORS = builtin_extractors()


def _table(rows: list[list[str]]) -> Table:
    return Table(
        encoding="utf-8",
        delimiter=",",
        has_header=True,
        rows=rows,
        column_count=max(len(r) for r in rows),
    )


def _csv(data: bytes, kind: str = "csv") -> Table:
    return EXTRACTORS[kind](data, ExtractOptions(), kind).tables[0]


def _activity(seed: int = 1, count: int = 2) -> tuple[list[ActivityRow], Table]:
    rows = activity_rows(seed, activity_accounts(seed, count))
    return rows, _csv(activity_csv_bytes(rows))


def _roles(table: Table) -> dict[str, int]:
    suggestion = suggest_mapping(table)
    assert suggestion is not None
    return {r.role: r.column for r in suggestion.reasons}


# ---------------------------------------------------------------------------
# The brokerage activity header
# ---------------------------------------------------------------------------


def test_activity_header_maps_the_cash_ledger_columns() -> None:
    _, table = _activity()
    suggestion = suggest_mapping(table)
    assert suggestion is not None
    spec = suggestion.spec
    assert spec.date_column == COLUMNS["date"]  # Run Date, not Settlement Date
    assert spec.date_format == "MM/DD/YYYY"
    assert spec.amount_column == COLUMNS["amount"]
    assert spec.payee_column in (COLUMNS["description"], COLUMNS["action"])
    assert spec.currency_column == COLUMNS["currency"]  # not the empty Exchange Currency
    assert spec.account_column == COLUMNS["account"]  # Account Number, not Account
    assert spec.indicator_column is None  # "Type" holds Cash/Margin
    assert spec.sign is Sign.INFLOW_POSITIVE
    assert spec == MappingSpec.model_validate(SPEC)
    assert suggestion.confident
    assert suggestion.score == 1.0
    assert suggestion.accounts == 2
    assert suggestion.failed_rows == ()
    assert suggestion.footer_rows == 3  # two disclaimers and the download line
    assert suggestion.sign_evidence is None
    by_role = {r.role: r for r in suggestion.reasons}
    assert by_role["date"].match == "exact"
    assert by_role["account"].fit == 1.0
    assert "indicator" not in by_role
    # The facts carry roles, columns and counts, never cell text.
    facts = str(suggestion.summary())
    assert "Individual" not in facts
    assert "SYNTHETIC" not in facts


def test_activity_header_on_the_masked_example_row() -> None:
    """The column header with a row shaped like a masked real one (invented values)."""
    rows = [
        list(HEADER),
        [
            "10/14/2026",
            "Individual",
            "Z20000044",
            "REINVESTMENT SYNTHETIC GROWTH INDEX ETF (SGRX) (Cash)",
            "SGRX",
            "SYNTHETIC GROWTH INDEX ETF",
            "Cash",
            "0",
            "",
            "USD",
            "85.44",
            "0.015",
            "0",
            "",
            "",
            "",
            "-1.28",
            "",
        ],
        [
            "10/15/2026",
            "Roth IRA",
            "Y30000077",
            "DIVIDEND RECEIVED SYNTHETIC GROWTH INDEX ETF (SGRX) (Cash)",
            "SGRX",
            "SYNTHETIC GROWTH INDEX ETF",
            "Margin",
            "0",
            "",
            "USD",
            "",
            "0",
            "0",
            "",
            "",
            "",
            "1.28",
            "",
        ],
    ]
    roles = _roles(_table(rows))
    assert roles["date"] == 0
    assert roles["amount"] == 16
    assert roles["payee"] in (3, 5)
    assert roles["currency"] == 9
    assert roles["account"] == 2
    assert "indicator" not in roles
    parsed = from_table_grouped(_table(rows), MappingSpec.model_validate(SPEC))
    assert [(s.last4, [t.amount for t in s.transactions]) for s in parsed] == [
        ("0044", [Decimal("-1.28")]),
        ("0077", [Decimal("1.28")]),
    ]


# ---------------------------------------------------------------------------
# Header search and global scoring
# ---------------------------------------------------------------------------


def test_header_is_found_below_a_preamble() -> None:
    rows = [
        ["Synthetic Brokerage", "activity export"],
        ["Accounts", "all", "", ""],
        ["Date", "Description", "Amount", "Balance"],
        ["01/14/2026", "Blue Owl Grocers", "-42.10", "957.90"],
        ["01/15/2026", "Initech Payroll", "1500.00", "2457.90"],
    ]
    suggestion = suggest_mapping(_table(rows))
    assert suggestion is not None
    assert suggestion.spec.skip_rows == 2
    assert (suggestion.spec.date_column, suggestion.spec.amount_column) == (0, 2)
    assert suggestion.sign_evidence == "balance"
    assert suggestion.confident
    # The header deeper than the search window is not found.
    deep = [["note", str(i)] for i in range(HEADER_SEARCH_ROWS)] + rows[2:]
    assert suggest_mapping(_table(deep)) is None


def test_every_synthetic_layout_is_suggested_exactly_but_the_sign() -> None:
    stmt = sample_statement(9)
    for name, layout in LAYOUTS.items():
        table = _csv(csv_bytes(layout_statement(stmt, name), name), layout.kind)
        suggestion = suggest_mapping(table)
        assert suggestion is not None, name
        want = MappingSpec.model_validate(layout.spec)
        if want.sign is Sign.OUTFLOW_POSITIVE:
            # Nothing in a purchases-positive file proves its sign.
            assert suggestion.sign_evidence is None
            want = want.model_copy(update={"sign": Sign.INFLOW_POSITIVE})
        assert suggestion.spec == want, name
        assert suggestion.confident, (name, suggestion.doubts)
        assert suggest_spec(table) == suggestion.spec


def test_match_levels() -> None:
    assert _match_level("currency", ("currency",)) == EXACT
    assert _match_level("exchange currency", ("currency",)) == WORD
    assert _match_level("currencycode", ("currency",)) == SUBSTRING
    assert _match_level("ccyx", ("ccy",)) == 0  # short words only as whole words
    assert _match_level("dr/cr", ("dr/cr",)) == EXACT


def test_exact_header_beats_an_earlier_partial_one() -> None:
    rows = [
        ["Exchange Currency", "Settlement Date", "Trade Date", "Amount", "Currency", "Payee"],
        ["EUR", "01/16/2026", "01/14/2026", "-1.00", "USD", "Corner Fuel"],
        ["EUR", "01/17/2026", "01/15/2026", "2.00", "USD", "Maple Books"],
    ]
    roles = _roles(_table(rows))
    assert roles["currency"] == 4
    assert roles["date"] == 2  # a trade date over a settlement date
    assert "Exchange Currency" not in str(roles)


def test_posting_dates_beat_transaction_dates_beat_settlement_dates() -> None:
    body = ["01/14/2026", "01/15/2026", "01/16/2026", "-1.00"]
    for header, want in (
        (["Settlement Date", "Trans Date", "Post Date", "Amount"], 2),
        (["Value Date", "Date", "Run Date", "Amount"], 2),
        (["Settlement Date", "Date", "Memo", "Amount"], 1),
    ):
        rows = [header, body if header[2] != "Memo" else [*body[:2], "Fuel", body[3]]]
        assert _roles(_table(rows))["date"] == want, header


def test_an_empty_column_never_takes_a_role() -> None:
    rows = [
        ["Currency", "Date", "Amount"],
        ["", "2026-01-14", "1.00"],
        ["", "2026-01-15", "2.00"],
    ]
    suggestion = suggest_mapping(_table(rows))
    assert suggestion is not None
    assert suggestion.spec.currency_column is None
    rows = [["Date", "Amount"], ["2026-01-14", ""], ["2026-01-15", ""]]
    assert suggest_mapping(_table(rows)) is None


def test_a_column_takes_a_role_only_when_its_cells_fit() -> None:
    rows = [
        ["Date", "Amount", "Description", "Currency"],
        ["2026-01-14", "Blue Owl", "1.00", "dollars"],
        ["2026-01-15", "Maple", "2.00", "dollars"],
    ]
    assert suggest_mapping(_table(rows)) is None  # no column of amounts under "Amount"
    rows = [
        ["Date", "Amount", "Payee", "Currency"],
        ["2026-01-14", "1.00", "Blue Owl", "US dollars"],
        ["2026-01-15", "2.00", "Maple", "US dollars"],
    ]
    suggestion = suggest_mapping(_table(rows))
    assert suggestion is not None
    assert suggestion.spec.currency_column is None


def test_indicator_needs_direction_words() -> None:
    rows = [
        ["Date", "Amount", "Type"],
        ["2026-01-14", "1.00", "Cash"],
        ["2026-01-15", "2.00", "Margin"],
    ]
    assert suggest_mapping(_table(rows)) is not None
    assert "indicator" not in _roles(_table(rows))
    rows = [
        ["Date", "Amount", "Type"],
        ["2026-01-14", "1.00", "Debit"],
        ["2026-01-15", "2.00", "Credit"],
    ]
    suggestion = suggest_mapping(_table(rows))
    assert suggestion is not None
    assert suggestion.spec.indicator_column == 2
    assert suggestion.spec.outflow_values == ["Debit"]
    assert suggestion.sign_evidence == "indicator"


def test_balance_proves_an_outflow_positive_sign() -> None:
    rows = [
        ["Date", "Description", "Amount", "Balance"],
        ["2026-01-14", "Corner Fuel", "10.00", "-10.00"],
        ["2026-01-15", "Payment", "-25.00", "15.00"],
        ["2026-01-16", "Maple Books", "5.00", "10.00"],
    ]
    suggestion = suggest_mapping(_table(rows))
    assert suggestion is not None
    assert suggestion.spec.sign is Sign.OUTFLOW_POSITIVE
    assert suggestion.sign_evidence == "balance"


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


def test_partial_header_words_are_not_confident() -> None:
    rows = [
        ["Date of booking", "Amount in USD", "Payee"],
        ["2026-01-14", "1.00", "Blue Owl"],
        ["2026-01-15", "2.00", "Maple"],
    ]
    suggestion = suggest_mapping(_table(rows))
    assert suggestion is not None
    assert (suggestion.spec.date_column, suggestion.spec.amount_column) == (0, 1)
    assert suggestion.doubts == ("header_not_exact",)
    assert not suggestion.confident
    assert suggestion.score < 1.0


def test_unreadable_rows_before_the_footer_are_not_confident() -> None:
    rows = [
        ["Date", "Amount", "Payee"],
        ["2026-01-14", "1.00", "Blue Owl"],
        ["2026-01-15", "", "Share transfer"],
        ["2026-01-16", "2.00", "Maple"],
        ["Total", "3.00", ""],
    ]
    suggestion = suggest_mapping(_table(rows))
    assert suggestion is not None
    assert suggestion.failed_rows == (2,)
    assert suggestion.footer_rows == 1
    assert suggestion.parsed_rows == 2
    assert suggestion.doubts == ("rows_unreadable",)
    assert suggestion.score == pytest.approx(2 / 3, abs=0.001)


def test_a_guessed_day_month_order_is_not_confident() -> None:
    rows = [["Date", "Amount"], ["01/02/2026", "1.00"], ["03/04/2026", "2.00"]]
    suggestion = suggest_mapping(_table(rows))
    assert suggestion is not None
    assert suggestion.spec.date_format == "MM/DD/YYYY"  # month first, as before
    assert suggestion.doubts == ("date_order_guessed",)
    assert best_date_format(["01/02/2026", "13/02/2026"]) == ("DD/MM/YYYY", 2, False)
    assert best_date_format(["2026-01-02", "footer"]) == ("YYYY-MM-DD", 1, False)


# ---------------------------------------------------------------------------
# Empty currency cells
# ---------------------------------------------------------------------------


def test_empty_currency_cells_mean_the_account_currency() -> None:
    table = _table(
        [
            ["Date", "Amount", "Ccy"],
            ["2026-01-02", "1.00", ""],
            ["2026-01-03", "2.00", "  "],
            ["2026-01-04", "3.00", "EUR"],
            ["2026-01-05", "4.00", "euro"],
        ]
    )
    spec = MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=1, currency_column=2)
    parsed = from_table(table, spec)
    assert [t.currency for t in parsed.transactions] == [None, None, "EUR"]
    assert parsed.skipped_rows == [4]  # a cell that is not a code still refuses the row
    assert parsed.currency == "EUR"
    only_empty = from_table(_table(table.rows[:3]), spec)
    assert [t.currency for t in only_empty.transactions] == [None, None]
    assert only_empty.currency is None


# ---------------------------------------------------------------------------
# Multi-account tables
# ---------------------------------------------------------------------------


def test_rows_are_grouped_by_the_account_cells_last_four_digits() -> None:
    rows, table = _activity(seed=3, count=3)
    spec = MappingSpec.model_validate(SPEC)
    accounts = []
    for row in rows:
        if row.account not in accounts:
            accounts.append(row.account)
    statements = from_table_grouped(table, spec)
    assert [s.last4 for s in statements] == [a.last4 for a in accounts]
    for parsed, account in zip(statements, accounts, strict=True):
        want = sorted((r.run_date, r.amount) for r in rows if r.account == account)
        assert sorted((t.posted_date, t.amount) for t in parsed.transactions) == want
        assert parsed.currency == "USD"
        assert parsed.skipped_rows == []
        # Only the digits are kept, never the cell.
        assert account.number not in repr(parsed)
    end = len(table.rows)
    assert statements[0].note_rows == [end - 3, end - 2, end - 1]
    assert [group_last4(table, spec, i) for i in range(3)] == [a.last4 for a in accounts]
    assert group_last4(table, spec, 3) is None
    # Read together, the rows name several accounts: no last4 for the whole.
    assert from_table(table, spec).last4 is None
    assert len(from_table(table, spec).transactions) == len(rows)


def test_grouping_keeps_unnamed_accounts_apart_and_reports_unreadable_rows() -> None:
    table = _table(
        [
            ["Date", "Account Number", "Amount"],
            ["2026-01-02", "****1234", "1.00"],
            ["2026-01-03", "Z9991234", "2.00"],  # same last four: same group
            ["2026-01-04", "Joint", "3.00"],
            ["2026-01-05", "", "4.00"],
            ["2026-01-06", "Joint", "n/a"],
            ["Total", "", "10.00"],
            ["Downloaded 2026-01-07"],
        ]
    )
    spec = MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=2, account_column=1)
    statements = from_table_grouped(table, spec)
    assert [(s.last4, len(s.transactions)) for s in statements] == [
        ("1234", 2),
        (None, 1),
        (None, 1),
    ]
    assert statements[1].skipped_rows == [5]  # "Joint"'s unreadable row
    assert statements[0].skipped_rows == []
    # The total row belongs to the empty-account group, which has a readable row.
    assert statements[2].skipped_rows == [6]
    assert statements[2].note_rows == [7]  # notes follow their (empty) account cell
    unreadable = _table([["Date", "Account Number", "Amount"], ["x", "1", "y"]])
    empty = from_table_grouped(unreadable, spec)
    assert len(empty) == 1
    assert empty[0].transactions == []
    assert empty[0].skipped_rows == [1]
    single = from_table(_table(table.rows[:3]), spec)
    assert single.last4 == "1234"


def test_cell_last4() -> None:
    assert cell_last4("Z12345678") == "5678"
    assert cell_last4("xxxx-1234") == "1234"
    assert cell_last4("Z2xxxxxxxx") is None
    assert cell_last4("") is None
    assert cell_last4(None) is None


def test_account_column_needs_several_account_like_values() -> None:
    one = [
        ["Date", "Account Number", "Amount"],
        ["2026-01-14", "Z12345678", "1.00"],
        ["2026-01-15", "Z12345678", "2.00"],
    ]
    assert "account" not in _roles(_table(one))  # a single account: nothing to group
    names = [
        ["Date", "Account", "Amount"],
        ["2026-01-14", "Individual", "1.00"],
        ["2026-01-15", "Roth IRA", "2.00"],
    ]
    assert "account" not in _roles(_table(names))
    several = [
        ["Date", "Acct", "Amount"],
        ["2026-01-14", "****1234", "1.00"],
        ["2026-01-15", "****5678", "2.00"],
    ]
    assert _roles(_table(several))["account"] == 1


# ---------------------------------------------------------------------------
# Backward compatibility of stored mappings
# ---------------------------------------------------------------------------


def test_mappings_stored_without_account_column_read_as_before() -> None:
    stored = dict(LAYOUTS["us_signed"].spec)  # as saved before the field existed
    spec = MappingSpec.model_validate(stored)
    assert spec.account_column is None
    assert spec.model_dump(mode="json")["account_column"] is None
    stmt = sample_statement(5)
    table = _csv(csv_bytes(stmt, "us_signed"))
    assert [(s.last4, len(s.transactions)) for s in from_table_grouped(table, spec)] == [
        (None, len(stmt.transactions))
    ]
    # The fingerprint is the header's: the new field does not change it.
    with_field = MappingSpec.model_validate({**stored, "account_column": None})
    assert spec_fingerprint(table, spec) == spec_fingerprint(table, with_field)


def test_account_column_is_a_column_like_the_others() -> None:
    with pytest.raises(ValidationError, match="mapped once"):
        MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=1, account_column=0)
    spec = MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=1, account_column=7)
    assert spec.columns()["account"] == 7
    assert spec.max_column() == 7


def test_downloaded_line_is_not_a_date() -> None:
    rows, table = _activity(seed=5)
    last = table.rows[-1]
    assert len(last) == 1
    assert last[0].startswith("Date downloaded")
    parsed = from_table(table, MappingSpec.model_validate(SPEC))
    assert parsed.skipped_rows == []
    assert len(parsed.transactions) == len(rows)
    assert parsed.period_end == max(r.run_date for r in rows)
    assert parsed.period_end < date(2026, 11, 1)


def _typed(cells: list[tuple[str, str]]) -> Table:
    rows = [["Date", "Description", "Type", "Amount"]]
    for index, (kind, amount) in enumerate(cells):
        rows.append([f"10/{index + 13:02d}/2026", f"SYNTHETIC PAYEE {index}", kind, amount])
    return _table(rows)


def test_a_type_column_beside_signed_amounts_is_not_the_sign() -> None:
    """``Type`` of DEBIT/CREDIT/CHECK next to signed amounts describes the row."""
    table = _typed(
        [
            ("DEBIT", "-12.50"),
            ("DEBIT", "-40.00"),
            ("CREDIT", "1500.00"),
            ("DEBIT", "-9.99"),
            ("CHECK", "-250.00"),
            ("DEBIT", "-3.10"),
            ("DEBIT", "-60.00"),
            ("CREDIT", "20.00"),
        ]
    )
    suggestion = suggest_mapping(table)
    assert suggestion is not None
    assert suggestion.spec.indicator_column is None
    assert suggestion.spec.outflow_values == []
    assert suggestion.sign_evidence is None  # a signed column, read on trust
    assert "indicator" not in {r.role for r in suggestion.reasons}
    (statement,) = from_table_grouped(table, suggestion.spec)
    assert [t.amount for t in statement.transactions][4] == Decimal("-250.00")


def test_an_indicator_with_other_words_is_not_trusted() -> None:
    """Unsigned amounts whose ``Type`` is sometimes no direction word: a person decides."""
    table = _typed(
        [
            ("DEBIT", "12.50"),
            ("DEBIT", "40.00"),
            ("CREDIT", "1500.00"),
            ("DEBIT", "9.99"),
            ("CHECK", "250.00"),
            ("DEBIT", "3.10"),
            ("DEBIT", "60.00"),
            ("CREDIT", "20.00"),
        ]
    )
    suggestion = suggest_mapping(table)
    assert suggestion is not None
    assert suggestion.spec.indicator_column == 2
    assert suggestion.doubts == ("indicator_values_unknown",)
    assert suggestion.confident is False
    # Every cell a direction word: the indicator proves the sign.
    clean = _typed([("DEBIT", "12.50"), ("CREDIT", "1500.00"), ("DEBIT", "9.99")])
    trusted = suggest_mapping(clean)
    assert trusted is not None
    assert trusted.confident is True
    assert trusted.sign_evidence == "indicator"
