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
from app.ingestion.parsers.brokerage_table import suggest_activity_mapping
from app.ingestion.parsers.structured import (
    EXACT,
    HEADER_SEARCH_ROWS,
    SUBSTRING,
    SUGGEST_SAMPLE_ROWS,
    WORD,
    MappingSuggestion,
    _match_level,
    best_date_format,
    cell_last4,
    from_table,
    from_table_grouped,
    group_last4,
    parse_cell_amount,
    parse_cell_date,
    spec_fingerprint,
    suggest_mapping,
    suggest_spec,
)
from app.ingestion.sandbox.protocol import ExtractOptions, Table
from tests.fixtures import synth_csv_layouts as layouts
from tests.fixtures import synth_sniff_cases as sniff_cases
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
    # A symbol, a quantity and a price: trades, never a confident cash ledger
    # (the brokerage activity reader takes this header first).
    assert "investment_columns" in suggestion.doubts
    assert not suggestion.confident
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
        doubts: tuple[str, ...] = ()
        if want.sign is Sign.OUTFLOW_POSITIVE:
            # Nothing in a purchases-positive file proves its sign: the
            # columns are found, and a person says which way the money went.
            assert suggestion.sign_evidence is None
            want = want.model_copy(update={"sign": Sign.INFLOW_POSITIVE})
            doubts = ("sign_unverified",)
        assert suggestion.spec == want, name
        assert suggestion.doubts == doubts, name
        assert suggestion.confident is (not doubts), name
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
        ["2026-01-14", "-1.00", "Blue Owl"],
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
        ["2026-01-14", "-1.00", "Blue Owl"],
        ["2026-01-15", "", "Share transfer"],
        ["2026-01-16", "2.00", "Maple"],
        ["Total", "1.00", ""],
    ]
    suggestion = suggest_mapping(_table(rows))
    assert suggestion is not None
    assert suggestion.failed_rows == (2,)
    assert suggestion.footer_rows == 1
    assert suggestion.parsed_rows == 2
    assert suggestion.doubts == ("rows_unreadable",)
    assert suggestion.score == pytest.approx(2 / 3, abs=0.001)


def test_a_guessed_day_month_order_is_not_confident() -> None:
    rows = [["Date", "Amount"], ["01/02/2026", "-1.00"], ["03/04/2026", "2.00"]]
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
    # The total row belongs to the empty-account group, which has a readable
    # row; after the table's last readable row and undated, it is a footer.
    assert (statements[2].skipped_rows, statements[2].footer_rows) == ([], [6])
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
    """``Type`` of DEBIT/CREDIT/CHECK next to signed amounts describes the row.

    It is not the indicator of the spec; that its words go with the signs
    is what proves "positive is money in".
    """
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
    assert suggestion.sign_evidence == "indicator_correlation"
    assert suggestion.spec.sign is Sign.INFLOW_POSITIVE
    assert suggestion.confident, suggestion.doubts
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


# ---------------------------------------------------------------------------
# Exports with lines about the account before the header, and other languages
# ---------------------------------------------------------------------------

PREAMBLE_CSV = (
    b"Account Name : Everyday Checking\n"
    b"Account Number : ****3377\n"
    b"Date Range : 01/01/2026-01/31/2026\n"
    b"\n"
    b"Posting Date,Effective Date,Transaction Type,Description,Check Number,"
    b"Withdrawals,Deposits,Running Balance\n"
    b'1/5/2026,1/5/2026,Debit,"BLUE OWL GROCERS, SPRINGFIELD",,74.19,,738.25\n'
    b"1/10/2026,1/10/2026,Debit,Transfer To Share 0000,,300.00,,438.25\n"
    b'1/15/2026,1/15/2026,Credit,INITECH PAYROLL,,,"1,610.00","2,048.25"\n'
    b'1/22/2026,1/22/2026,Check,Draft,2051,125.00,,"1,923.25"\n'
)


def _extracted(data: bytes) -> Table:
    result = EXTRACTORS["csv"](data, ExtractOptions(), "csv")
    assert result.ok, result.error
    return result.tables[0]


def test_an_export_with_a_preamble_is_mapped_and_names_its_account() -> None:
    table = _extracted(PREAMBLE_CSV)
    suggestion = suggest_mapping(table)
    assert suggestion is not None
    assert suggestion.confident
    spec = suggestion.spec
    assert (spec.skip_rows, spec.has_header) == (3, True)
    assert (spec.debit_column, spec.credit_column, spec.balance_column) == (5, 6, 7)
    parsed = from_table(table, spec)
    assert [(t.posted_date, t.amount) for t in parsed.transactions] == [
        (date(2026, 1, 5), Decimal("-74.19")),
        (date(2026, 1, 10), Decimal("-300.00")),
        (date(2026, 1, 15), Decimal("1610.00")),
        (date(2026, 1, 22), Decimal("-125.00")),
    ]
    # The account is named above the header, not in a column.
    assert parsed.last4 == "3377"
    assert not parsed.balance_mismatches


def test_a_preamble_naming_two_accounts_names_none() -> None:
    table = _extracted(
        PREAMBLE_CSV.replace(b"Date Range : 01/01/2026-01/31/2026", b"Linked account : ****9001")
    )
    spec = suggest_spec(table)
    assert spec is not None
    assert from_table(table, spec).last4 is None


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            "Buchungstag;Valuta;Auftraggeber/Empfänger;Verwendungszweck;Betrag;Währung;Saldo\n"
            "02.01.2026;02.01.2026;Stadtwerke Musterstadt;Abschlag Strom;-82,50;EUR;1.417,50\n"
            "05.01.2026;05.01.2026;Musterfirma GmbH;Gehalt Januar;2.350,00;EUR;3.767,50\n"
            "12.01.2026;12.01.2026;Vermietung Beispiel;Miete;-890,00;EUR;2.877,50\n",
            [("Stadtwerke Musterstadt", "-82.50"), ("Musterfirma GmbH", "2350.00")],
        ),
        (
            "Date opération;Libellé;Débit;Crédit;Solde\n"
            "02/01/2026;PRLV EAU DE VILLE;45,10;;954,90\n"
            "05/01/2026;VIR SALAIRE EXEMPLE SA;;2100,00;3054,90\n"
            "13/01/2026;CB BOULANGERIE;8,40;;3046,50\n",
            [("PRLV EAU DE VILLE", "-45.10"), ("VIR SALAIRE EXEMPLE SA", "2100.00")],
        ),
        (
            "Fecha,Concepto,Importe,Saldo\n"
            "2026-01-02,RECIBO LUZ EJEMPLO,-61.20,938.80\n"
            "2026-01-05,NOMINA EMPRESA EJEMPLO,1800.00,2738.80\n"
            "2026-01-14,COMPRA MERCADO,-40.00,2698.80\n",
            [("RECIBO LUZ EJEMPLO", "-61.20"), ("NOMINA EMPRESA EJEMPLO", "1800.00")],
        ),
        (
            "Datum;Naam / Omschrijving;Af Bij;Bedrag (EUR);Mededelingen\n"
            "20260102;Energie Voorbeeld;Af;82,50;Termijn januari\n"
            "20260105;Werkgever Voorbeeld BV;Bij;2350,00;Salaris\n"
            "20260113;Supermarkt Voorbeeld;Af;31,75;Pinbetaling\n",
            [("Energie Voorbeeld", "-82.50"), ("Werkgever Voorbeeld BV", "2350.00")],
        ),
    ],
    ids=["german", "french", "spanish", "dutch"],
)
def test_exports_in_other_languages_map_without_a_person(
    data: str, expected: list[tuple[str, str]]
) -> None:
    table = _extracted(data.encode("utf-8"))
    suggestion = suggest_mapping(table)
    assert suggestion is not None
    assert suggestion.confident, suggestion.doubts
    parsed = from_table(table, suggestion.spec)
    assert len(parsed.transactions) == 3
    assert [(t.raw_payee, str(t.amount)) for t in parsed.transactions[:2]] == expected
    assert not parsed.balance_mismatches


def test_valuta_is_a_date_or_a_currency_by_its_values() -> None:
    german = _extracted(
        b"Buchungstag;Valuta;Empf\xc3\xa4nger;Betrag\n"
        b"02.01.2026;03.01.2026;Beispiel AG;-10,00\n"
        b"05.01.2026;06.01.2026;Muster GmbH;20,00\n"
    )
    spec = suggest_spec(german)
    assert spec is not None
    assert (spec.date_column, spec.currency_column) == (0, None)  # the booking date wins
    italian = _extracted(
        b"Data;Descrizione;Importo;Valuta\n"
        b"02/01/2026;Bolletta esempio;-10,00;EUR\n"
        b"05/01/2026;Stipendio esempio;20,00;EUR\n"
    )
    spec = suggest_spec(italian)
    assert spec is not None
    assert (spec.date_column, spec.currency_column) == (0, 3)


# ---------------------------------------------------------------------------
# The sign of a file is proven, or the suggestion is not confident
# ---------------------------------------------------------------------------


def _suggested(table: Table) -> MappingSuggestion:
    suggestion = suggest_mapping(table)
    assert suggestion is not None
    return suggestion


def _read(table: Table, spec: MappingSpec) -> list[Decimal]:
    return [t.amount for s in from_table_grouped(table, spec) for t in s.transactions]


def test_card_like_table_without_evidence_is_not_confident() -> None:
    table = _extracted(layouts.card_positive_purchases())
    suggestion = _suggested(table)
    assert suggestion.sign_evidence is None
    assert suggestion.doubts == ("sign_unverified",)
    assert not suggestion.confident
    assert suggestion.summary()["doubts"] == ["sign_unverified"]
    # Money out on most rows and no card word in the header: taken on trust, as before.
    trusted = _suggested(_extracted(layouts.checking_signed_no_balance()))
    assert (trusted.confident, trusted.sign_evidence, trusted.doubts) == (True, None, ())


@pytest.mark.parametrize(
    ("header", "amounts"),
    [
        (["Date", "Description", "Amount"], ["12.50", "40.00", "9.99"]),  # every row positive
        (["Date", "Description", "Amount"], ["-12.50", "-40.00", "-9.99"]),  # every row negative
        (["Date", "Merchant", "Amount"], ["-12.50", "-40.00", "900.00"]),
        (["Date", "Description", "Card No.", "Amount"], ["-12.50", "-40.00", "900.00"]),
        (
            ["Transaction Date", "Post Date", "Description", "Amount"],
            ["-12.50", "-40.00", "900.00"],
        ),
        (["Date", "Description", "Amount", "Statement Balance"], ["-12.50", "-40.00", "900.00"]),
    ],
    ids=["all-positive", "all-negative", "merchant", "card-no", "post-date", "statement-balance"],
)
def test_one_signed_tables_and_card_headers_wait_for_a_person(
    header: list[str], amounts: list[str]
) -> None:
    rows = [header]
    for index, amount in enumerate(amounts):
        row = [f"2026-01-{index + 14}", f"SYNTHETIC PAYEE {index}", amount]
        if len(header) == 4 and header[1] == "Post Date":
            row = [f"2026-01-{index + 13}", f"2026-01-{index + 14}", row[1], amount]
        elif len(header) == 4 and header[3] == "Amount":
            row = [row[0], row[1], "CARD ENDING IN XXXX", amount]
        elif len(header) == 4:
            row = [*row, ""]
        rows.append(row)
    suggestion = _suggested(_table(rows))
    assert suggestion.sign_evidence is None
    assert "sign_unverified" in suggestion.doubts
    assert not suggestion.confident


def test_type_words_prove_purchases_positive() -> None:
    table = _extracted(layouts.card_with_type_column())
    suggestion = _suggested(table)
    assert suggestion.confident, suggestion.doubts
    assert suggestion.spec.sign is Sign.OUTFLOW_POSITIVE
    assert suggestion.sign_evidence == "indicator_correlation"
    # The words prove the sign; the amounts keep signing themselves.
    assert suggestion.spec.indicator_column is None
    amounts = _read(table, suggestion.spec)
    assert amounts[0] == Decimal("-54.18")  # a purchase is money out
    assert amounts[4] == Decimal("350.00")  # the payment is money in


def test_direction_words_beside_signed_amounts_are_evidence() -> None:
    table = _extracted(layouts.aggregator_debit_positive())
    suggestion = _suggested(table)
    assert suggestion.confident, suggestion.doubts
    assert suggestion.spec.sign is Sign.OUTFLOW_POSITIVE
    assert suggestion.sign_evidence == "indicator_correlation"
    amounts = _read(table, suggestion.spec)
    assert amounts[0] == Decimal("-1210.00")
    assert amounts[5] == Decimal("2840.00")


def test_direction_words_need_four_rows_and_both_directions() -> None:
    few = _typed([("DEBIT", "12.50"), ("DEBIT", "40.00"), ("CREDIT", "-900.00")])
    assert (_suggested(few).sign_evidence, _suggested(few).spec.sign) == (
        None,
        Sign.INFLOW_POSITIVE,
    )
    assert "sign_unverified" in _suggested(few).doubts  # more money in than out, unproven
    one_way = _typed([("DEBIT", "-12.50"), ("DEBIT", "-40.00"), ("DEBIT", "-9.99"), ("DEBIT", "5")])
    assert _suggested(one_way).sign_evidence is None
    assert "indicator_contradicts_sign" not in _suggested(one_way).doubts


def test_indicator_is_found_by_content() -> None:
    table = _extracted(layouts.unsigned_with_indicator_header_unknown())
    suggestion = _suggested(table)
    assert suggestion.spec.indicator_column == 3
    assert suggestion.spec.outflow_values == ["Debit"]
    assert suggestion.sign_evidence == "indicator"
    assert suggestion.confident, suggestion.doubts
    by_role = {r.role: r for r in suggestion.reasons}
    assert (by_role["indicator"].column, by_role["indicator"].match) == (3, "content")
    amounts = _read(table, suggestion.spec)
    assert amounts[0] == Decimal("-58.30")  # a debit row
    assert amounts[3] == Decimal("2840.00")  # the credit row


def test_mixed_words_and_signs_are_a_doubt() -> None:
    rows = [["Date", "Description", "Amount", "Transaction Type"]]
    debits = 0
    for day, payee, amount, word in layouts.AGGREGATOR_ROWS:
        if word == "debit":
            debits += 1
            amount = f"-{amount}" if debits % 2 else amount  # half the debit rows negative
        rows.append([day, payee, amount, word])
    suggestion = _suggested(_table(rows))
    assert "indicator_contradicts_sign" in suggestion.doubts
    assert not suggestion.confident
    assert suggestion.sign_evidence is None


def test_words_that_contradict_a_reconciling_balance_are_a_doubt() -> None:
    """A card's running balance is what is owed: it adds up with purchases as money in."""
    rows = [["Date", "Description", "Type", "Amount", "Balance"]]
    owed = Decimal(0)
    for index, (kind, amount) in enumerate(
        [("Purchase", "25.00"), ("Purchase", "40.00"), ("Payment", "-50.00"), ("Purchase", "8.00")]
    ):
        owed += Decimal(amount)
        rows.append([f"2026-01-{index + 14}", f"SYNTHETIC PAYEE {index}", kind, amount, f"{owed}"])
    suggestion = _suggested(_table(rows))
    assert "indicator_contradicts_sign" in suggestion.doubts
    assert not suggestion.confident


def test_a_running_balance_proves_nothing_under_a_card_header() -> None:
    """What is owed grows with each purchase: it reconciles with purchases as money in."""
    rows = [["Date", "Merchant", "Amount", "Balance"]]
    owed = Decimal(0)
    for index, amount in enumerate(["25.00", "40.00", "-50.00", "8.00"]):
        owed += Decimal(amount)
        rows.append([f"2026-01-{index + 14}", f"SYNTHETIC PAYEE {index}", amount, f"{owed}"])
    suggestion = _suggested(_table(rows))
    assert suggestion.sign_evidence == "balance"
    assert suggestion.doubts == ("sign_unverified",)
    # The same figures without a card word in the header: the balance is trusted, as before.
    rows[0][1] = "Description"
    plain = _suggested(_table(rows))
    assert (plain.confident, plain.sign_evidence) == (True, "balance")


def test_lone_debit_or_credit_column_keeps_its_sign() -> None:
    table = _extracted(layouts.lone_inflow_column_of_negatives())
    suggestion = _suggested(table)
    assert (suggestion.spec.credit_column, suggestion.spec.debit_column) == (2, None)
    amounts = _read(table, suggestion.spec)
    assert amounts[:2] == [Decimal("-48.12"), Decimal("-19.80")]
    assert all(amount < 0 for amount in amounts)
    # Signed cells under a header that says "money in": nothing proves which is right.
    assert suggestion.sign_evidence is None
    assert "sign_unverified" in suggestion.doubts
    # A lone debit column: a positive cell is money out, a negative one comes back.
    lone = _table(
        [
            ["Date", "Description", "Debit"],
            ["2026-01-14", "Harbor Lane Grocery", "12.50"],
            ["2026-01-15", "Refund Harbor Lane Grocery", "-12.50"],
            ["2026-01-16", "Tin Roof Diner", "30.00"],
        ]
    )
    spec = MappingSpec(date_column=0, date_format="YYYY-MM-DD", payee_column=1, debit_column=2)
    assert _read(lone, spec) == [Decimal("-12.50"), Decimal("12.50"), Decimal("-30.00")]
    mixed = _suggested(lone)
    assert (mixed.spec.debit_column, mixed.sign_evidence) == (2, None)  # signed cells: on trust
    # Unsigned cells under one header: the header says which way, as before.
    unsigned = _table([r if "-" not in r[2] else [*r[:2], "7.00"] for r in lone.rows])
    assert (_suggested(unsigned).confident, _suggested(unsigned).sign_evidence) == (
        True,
        "debit_credit",
    )
    assert all(amount < 0 for amount in _read(unsigned, spec))
    # A debit column that prints every amount negative: money out, as before.
    printed = _table([lone.rows[0], *([*r[:2], f"-{r[2]}"] for r in unsigned.rows[1:])])
    assert _read(printed, spec) == _read(unsigned, spec)
    assert (_suggested(printed).confident, _suggested(printed).sign_evidence) == (
        True,
        "debit_credit",
    )


def test_both_columns_negative_value_is_the_opposite_column() -> None:
    rows = [
        ["Date", "Description", "Debit", "Credit"],
        ["2026-01-14", "Harbor Lane Grocery", "12.50", ""],
        ["2026-01-15", "Reversal Harbor Lane Grocery", "-12.50", ""],
        ["2026-01-16", "Marlow Design Payroll", "", "900.00"],
        ["2026-01-17", "Payroll correction", "", "-40.00"],
        ["2026-01-18", "Tin Roof Diner", "30.00", ""],
    ]
    spec = MappingSpec(
        date_column=0, date_format="YYYY-MM-DD", payee_column=1, debit_column=2, credit_column=3
    )
    assert _read(_table(rows), spec) == [
        Decimal("-12.50"),
        Decimal("12.50"),  # a negative debit is a credit
        Decimal("900.00"),
        Decimal("-40.00"),  # a negative credit is a debit
        Decimal("-30.00"),
    ]


def test_a_debit_column_printed_negative_throughout_is_still_money_out() -> None:
    """Exports that print every withdrawal as ``-25.00`` under ``Amount Debit``."""
    rows = [
        ["Date", "Description", "Debit", "Credit"],
        ["2026-01-14", "Harbor Lane Grocery", "-12.50", ""],
        ["2026-01-15", "Tin Roof Diner", "-30.00", ""],
        ["2026-01-16", "Marlow Design Payroll", "", "900.00"],
        ["2026-01-17", "Northgate Fuel Stop", "-41.00", ""],
    ]
    suggestion = _suggested(_table(rows))
    assert suggestion.confident, suggestion.doubts
    assert suggestion.sign_evidence == "debit_credit"
    assert _read(_table(rows), suggestion.spec) == [
        Decimal("-12.50"),
        Decimal("-30.00"),
        Decimal("900.00"),
        Decimal("-41.00"),
    ]


def test_trades_and_fund_tables_are_never_cash() -> None:
    trades = _extracted(layouts.trades_list())
    assert suggest_activity_mapping(trades) is None  # no action column: not brokerage activity
    suggestion = _suggested(trades)
    assert "investment_columns" in suggestion.doubts
    assert not suggestion.confident
    funds = _extracted(layouts.fund_transactions())
    activity = suggest_activity_mapping(funds)
    assert activity is not None  # the brokerage activity reader takes it first
    assert activity.spec.mode == "activity"
    assert activity.sign_evidence == "activity"
    # Read as a cash ledger anyway, it is never confident.
    assert "investment_columns" in _suggested(funds).doubts


def test_balance_proven_checking_stays_confident() -> None:
    table = _extracted(layouts.checking_with_running_balance())
    suggestion = _suggested(table)
    assert suggestion.confident, suggestion.doubts
    assert suggestion.sign_evidence == "balance"
    assert suggestion.spec.sign is Sign.INFLOW_POSITIVE
    amounts = _read(table, suggestion.spec)
    assert (amounts[0], amounts[3]) == (Decimal("-1210.00"), Decimal("2840.00"))


# ---------------------------------------------------------------------------
# An account column of names splits the file
# ---------------------------------------------------------------------------


def _groups(table: Table) -> list[tuple[str | None, str | None, int]]:
    """(name, last four, rows) of each statement the suggested mapping reads."""
    statements = from_table_grouped(table, _suggested(table).spec)
    return [(s.account_name, s.last4, len(s.transactions)) for s in statements]


@pytest.mark.parametrize("header", ["Account", "Account Name", "Source Account", "ACCOUNT  name"])
def test_account_name_column_groups_rows(header: str) -> None:
    table = _extracted(layouts.aggregator_two_accounts(header))
    suggestion = _suggested(table)
    statements = from_table_grouped(table, suggestion.spec)
    assert [len(s.transactions) for s in statements] == [5, 4]
    assert suggestion.spec.account_column == 1
    assert suggestion.accounts == 2
    assert suggestion.confident, suggestion.doubts
    (reason,) = [r for r in suggestion.reasons if r.role == "account"]
    assert (reason.column, reason.match, reason.fit) == (1, "exact", 1.0)
    # Each statement carries its account's name as printed and no last four.
    assert _groups(table) == [("Everyday Checking", None, 5), ("Rewards Card", None, 4)]
    checking, card = statements
    assert [t.raw_payee for t in card.transactions] == [
        row[2] for row in layouts.TWO_ACCOUNT_ROWS if row[1] == "Rewards Card"
    ]
    assert sum(t.amount for t in checking.transactions) == Decimal("1491.75")
    assert [group_last4(table, suggestion.spec, group) for group in (0, 1)] == [None, None]


def test_account_names_are_grouped_folded_and_whitespace_collapsed() -> None:
    rows = [["Date", "Account", "Description", "Amount"]]
    cells = ["Everyday Checking", "REWARDS CARD", "everyday  checking", " Rewards Card "]
    for index, cell in enumerate(cells * 2):
        rows.append([f"2026-09-{index + 1:02d}", cell, f"SYNTHETIC PAYEE {index}", "-10.00"])
    rows.append(["2026-09-20", "Everyday Checking", "SYNTHETIC PAYROLL", "900.00"])
    table = _table(rows)
    # The name is the first cell of the group, its blanks collapsed.
    assert _groups(table) == [("Everyday Checking", None, 5), ("REWARDS CARD", None, 4)]


def test_a_name_that_starts_with_a_number_sign_is_a_name() -> None:
    table = _table(
        [
            ["Date", "Account", "Amount"],
            ["2026-01-02", "#1 Joint", "-1.00"],
            ["2026-01-03", "#2 Solo", "-2.00"],
            ["2026-01-04", "#1 Joint", "3.00"],
            ["2026-01-05", "#2 Solo", "-4.00"],
        ]
    )
    assert _groups(table) == [("#1 Joint", None, 2), ("#2 Solo", None, 2)]


def test_a_masked_number_is_never_kept_as_a_name() -> None:
    table = _table(
        [
            ["Date", "Account Number", "Amount"],
            ["2026-01-02", "Z2xxxxxxxx", "1.00"],  # too few digits for a last four
            ["2026-01-03", "****1234", "2.00"],
            ["2026-01-04", "Joint", "3.00"],
        ]
    )
    spec = MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=2, account_column=1)
    statements = from_table_grouped(table, spec)
    assert [(s.account_name, s.last4) for s in statements] == [
        (None, None),
        (None, "1234"),
        ("Joint", None),
    ]
    assert "Z2" not in repr(statements)


def test_unique_account_cells_do_not_split() -> None:
    table = _extracted(layouts.export_with_unique_account_cells())
    suggestion = _suggested(table)
    assert "account" not in {r.role for r in suggestion.reasons}
    assert suggestion.spec.account_column is None
    assert suggestion.accounts == 1
    assert _groups(table) == [(None, None, 6)]


@pytest.mark.parametrize(
    ("accounts", "repeats", "split"),
    [
        (2, 2, True),
        (20, 2, True),
        (21, 2, False),  # too many to be one person's accounts
        (1, 6, False),  # one account: nothing to group
        (3, 1, False),  # no value repeats
    ],
)
def test_a_name_column_needs_a_few_values_that_each_repeat(
    accounts: int, repeats: int, split: bool
) -> None:
    rows = [["Date", "Account", "Description", "Amount"]]
    for index in range(accounts * repeats):
        name = f"Synthetic Pot {chr(65 + index % accounts)}"
        rows.append(["2026-03-05", name, f"SYNTHETIC PAYEE {index}", "-10.00"])
    rows.append(["2026-03-06", "Synthetic Pot A", "SYNTHETIC PAYROLL", "900.00"])
    table = _table(rows)
    assert ("account" in _roles(table)) is split
    assert len(_groups(table)) == (accounts if split else 1)
    # One value seen once among values that repeat: not a column of accounts.
    if split:
        rows.append(["2026-03-07", "Synthetic Payee Once", "SYNTHETIC PAYEE", "-1.00"])
        assert "account" not in _roles(_table(rows))


def test_payee_column_is_never_an_account_column() -> None:
    table = _extracted(layouts.export_with_payee_and_category())
    suggestion = _suggested(table)
    assert "account" not in {r.role for r in suggestion.reasons}
    assert suggestion.spec.account_column is None
    assert _groups(table) == [(None, None, 8)]
    roles = _roles(table)
    assert (roles["payee"], roles["memo"]) == (1, 2)


@pytest.mark.parametrize(
    "header",
    ["Payee", "Category", "Account Type", "From Account", "Account Owner", "Counterparty", "Name"],
)
def test_only_an_exact_account_header_names_a_column_of_names(header: str) -> None:
    # The very rows that split under "Account" stay together under any other header.
    table = _extracted(layouts.aggregator_two_accounts(header))
    suggestion = _suggested(table)
    assert suggestion.spec.account_column is None
    assert _groups(table) == [(None, None, 9)]


def test_a_number_column_wins_over_a_name_column() -> None:
    rows = [["Date", "Account", "Account Number", "Description", "Amount"]]
    for index, (name, number) in enumerate(
        [("Synthetic Joint", "****4410"), ("Synthetic Solo", "****9026")] * 3
    ):
        rows.append([f"2026-04-{index + 1:02d}", name, number, f"SYNTHETIC PAYEE {index}", "-5.00"])
    rows.append(["2026-04-09", "Synthetic Joint", "****4410", "SYNTHETIC PAYROLL", "700.00"])
    table = _table(rows)
    assert _roles(table)["account"] == 2
    # Matched by their digits like any numbered statement; the nickname is not read.
    assert _groups(table) == [(None, "4410", 4), (None, "9026", 3)]


# ---------------------------------------------------------------------------
# Words, shapes and rows that are not transactions
# ---------------------------------------------------------------------------


def _reason(suggestion: MappingSuggestion, role: str) -> tuple[int, str]:
    found = [(r.column, r.match) for r in suggestion.reasons if r.role == role]
    assert len(found) == 1, suggestion.reasons
    return found[0]


@pytest.mark.parametrize("header", ["Created", "Datetime", "Timestamp", "Date Processed"])
def test_more_headers_name_the_date_exactly(header: str) -> None:
    suggestion = _suggested(_extracted(layouts.export_dated_under(header)))
    assert _reason(suggestion, "date") == (0, "exact")
    assert suggestion.confident, suggestion.doubts
    assert suggestion.parsed_rows == len(layouts.STAMPED_ROWS)


def test_a_date_word_still_needs_cells_that_are_dates() -> None:
    rows = [
        ["Created", "Posted", "Amount"],
        ["Ada Finch", "2026-02-03", "-61.30"],
        ["Noor Vale", "2026-02-06", "-27.85"],
    ]
    assert _suggested(_table(rows)).spec.date_column == 1


def test_a_timestamp_with_a_spaced_zone_offset_parses() -> None:
    for text in (
        "2026-02-25 20:48:09 +0000",
        "2026-02-25 20:48:09 -05:00",
        "2026-02-25T20:48:09 Z",
    ):
        assert parse_cell_date(text, "YYYY-MM-DD") == date(2026, 2, 25), text
    assert parse_cell_date("2026-02-25 20:48:09 +00", "YYYY-MM-DD") is None
    table = _extracted(layouts.export_dated_under("created", zone=" +0000"))
    suggestion = _suggested(table)
    assert suggestion.confident, suggestion.doubts
    parsed = from_table(table, suggestion.spec)
    assert [t.posted_date for t in parsed.transactions][:2] == [date(2026, 2, 3), date(2026, 2, 6)]
    assert len(parsed.transactions) == len(layouts.STAMPED_ROWS)


def test_gross_is_the_amount_beside_fee_and_net() -> None:
    table = _extracted(layouts.payments_gross_fee_net())
    suggestion = _suggested(table)
    assert _reason(suggestion, "amount") == (5, "exact")
    assert suggestion.confident, suggestion.doubts
    assert _read(table, suggestion.spec)[:3] == [
        Decimal("-48.20"),
        Decimal("-26.75"),
        Decimal("120.00"),
    ]
    # A fee on a row: gross and net differ, so which of them is the amount
    # is for a person to say. Gross is offered, not imported on its own.
    with_fee = _suggested(_extracted(layouts.payments_gross_fee_net(fees=True)))
    assert _reason(with_fee, "amount") == (5, "word")
    assert with_fee.doubts == ("header_not_exact",)
    # ``Gross`` alone (a payslip's column, a price list) is no amount word.
    alone = _table(
        [["Date", "Name", "Gross"], ["2026-03-03", "A", "-1.00"], ["2026-03-04", "B", "2"]]
    )
    assert suggest_mapping(alone) is None


def test_credit_amount_and_debit_amount_are_the_credit_and_debit_columns() -> None:
    table = _extracted(layouts.credit_debit_amount_columns())
    suggestion = _suggested(table)
    assert _reason(suggestion, "credit") == (2, "exact")
    assert _reason(suggestion, "debit") == (3, "exact")
    assert suggestion.spec.amount_column is None
    assert (suggestion.confident, suggestion.sign_evidence) == (True, "debit_credit")
    assert _read(table, suggestion.spec) == [
        Decimal("1850.00"),
        Decimal("-200.00"),
        Decimal("-1500.00"),
        Decimal("920.00"),
        Decimal("-3.00"),
    ]


def test_a_parenthesised_qualifier_is_a_unit_suffix() -> None:
    table = _extracted(layouts.wallet_export_with_opening_row())
    suggestion = _suggested(table)
    assert _reason(suggestion, "date") == (2, "exact")  # ``Datetime``
    assert _reason(suggestion, "amount") == (6, "exact")  # ``Amount (total)``
    assert "header_not_exact" not in suggestion.doubts
    # A part of the amount is not the amount, whichever column comes first.
    rows = [
        ["Date", "Amount (fee)", "Amount (total)", "Balance (pending)"],
        ["2026-04-02", "0.25", "-14.50", "85.50"],
        ["2026-04-05", "0.00", "-36.00", "49.50"],
    ]
    fee_first = _suggested(_table(rows))
    assert _reason(fee_first, "amount") == (2, "exact")
    assert _reason(fee_first, "balance") == (3, "exact")
    # Two columns told apart by their qualifiers alone: a person says which.
    rows[0] = ["Date", "Amount (original)", "Amount (converted)", "Balance"]
    either = _suggested(_table(rows))
    assert _reason(either, "amount") == (1, "word")
    assert "header_not_exact" in either.doubts
    # Units are read as before, also on two columns.
    rows[0] = ["Date", "Amount (USD)", "Amount (EUR)", "Balance"]
    assert _reason(_suggested(_table(rows)), "amount") == (1, "exact")


def test_a_row_without_a_date_or_an_amount_is_a_note_row() -> None:
    """The first row of a wallet export: the holder's name and ``$0``, nothing else."""
    table = _extracted(layouts.wallet_export_with_opening_row())
    suggestion = _suggested(table)
    assert suggestion.failed_rows == ()
    assert suggestion.parsed_rows == 5
    assert suggestion.confident, suggestion.doubts
    # The balance column holds that one cell: it checks nothing, so it proves nothing.
    assert suggestion.sign_evidence is None
    parsed = from_table(table, suggestion.spec)
    assert (parsed.note_rows, parsed.skipped_rows) == ([1], [])
    assert [t.amount for t in parsed.transactions][:3] == [
        Decimal("-14.50"),
        Decimal("-36.00"),
        Decimal("40.00"),
    ]
    # A row with a date and no amount is still a row that did not read.
    rows = [
        ["Date", "Amount", "Payee", "Memo"],
        ["2026-01-14", "-1.00", "Blue Owl", ""],
        ["", "", "Pending items", "see below"],
        ["2026-01-15", "", "Share transfer", ""],
        ["2026-01-16", "2.00", "Maple", ""],
    ]
    spec = MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=1, payee_column=2)
    mixed = from_table(_table(rows), spec)
    assert (mixed.note_rows, mixed.skipped_rows) == ([2], [3])


def test_a_continuation_row_is_appended_to_its_rows_memo() -> None:
    table = _extracted(sniff_cases.csv_preamble_with_continuation_rows())
    suggestion = _suggested(table)
    assert suggestion.doubts == ("date_order_guessed",)  # every day is 12 or less
    assert (suggestion.parsed_rows, suggestion.failed_rows) == (4, ())
    parsed = from_table(table, suggestion.spec)
    assert [(t.raw_payee, t.memo, t.amount) for t in parsed.transactions] == [
        ("DEBIT PURCHASE", "01/03/26 xx-0000 MOSSY OAK DELI", Decimal("-18.40")),
        ("DEBIT PURCHASE", "02/03/26 xx-0000 TERN STREET CAFE", Decimal("-6.75")),
        ("FUND TRANSFER", "from 000-0-000001 QUILLFEATHER", Decimal("250.00")),
        ("DEBIT PURCHASE", "05/03/26 xx-0000 PEBBLE LANE GROCER", Decimal("-32.10")),
    ]
    assert (parsed.note_rows, parsed.skipped_rows) == ([], [])


def test_only_a_cell_under_the_description_continues_a_row() -> None:
    spec = MappingSpec(
        date_column=0, date_format="YYYY-MM-DD", amount_column=1, payee_column=2, memo_column=3
    )
    rows = [
        ["Date", "Amount", "Payee", "Memo"],
        ["2026-01-14", "-1.00", "Blue Owl", "card 0000"],
        ["", "", "", "second line"],
        ["", "", "third line"],
        ["2026-01-15", "-2.00", "Maple", ""],
        ["Statement notes"],  # under the date column: a note, not a description
        ["2026-01-16", "3.00", "Initech", ""],
        ["", "", "", ""],
        ["", "", "not directly below a row"],
    ]
    parsed = from_table(_table(rows), spec)
    assert [t.memo for t in parsed.transactions] == [
        "card 0000 second line third line",
        None,
        None,
    ]
    assert parsed.note_rows == [5, 8]
    # A single cell after the last row of a table that has no other
    # continuation is what it always was: a closing note.
    closing = from_table(
        _table([*rows[:2], rows[4], ["", "", "Thank you for banking with us"]]), spec
    )
    assert [t.memo for t in closing.transactions] == ["card 0000", None]
    assert closing.note_rows == [3]


def test_a_short_trailing_footer_does_not_dilute_the_fit() -> None:
    table = _extracted(layouts.export_with_short_footer())
    suggestion = _suggested(table)
    assert _reason(suggestion, "date") == (0, "exact")
    assert next(r.fit for r in suggestion.reasons if r.role == "date") == 1.0
    assert (suggestion.parsed_rows, suggestion.failed_rows, suggestion.footer_rows) == (2, (), 2)
    assert (suggestion.confident, suggestion.sign_evidence) == (True, "balance")
    # Short rows that outnumber the rows before them are not a footer block:
    # the file leaves out its empty trailing cells, and every row is looked at.
    rows = [
        ["Date", "Amount", "Payee", "Memo"],
        ["03/04/2026", "-1.00", "Blue Owl", "weekly"],
        ["25/04/2026", "-2.00", "Maple"],
        ["26/04/2026", "3.00", "Initech"],
        ["27/04/2026", "-4.00", "Corner Fuel"],
    ]
    ragged = _suggested(_table(rows))
    assert (ragged.spec.date_format, ragged.parsed_rows) == ("DD/MM/YYYY", 4)
    assert ragged.confident, ragged.doubts


def test_rows_after_the_last_readable_row_are_footers() -> None:
    table = _extracted(layouts.export_with_short_footer())
    parsed = from_table(table, _suggested(table).spec)
    assert (parsed.skipped_rows, parsed.footer_rows) == ([], [3, 4])
    # A trailing row that has a date is a transaction that did not read, not a footer.
    spec = MappingSpec(date_column=0, date_format="YYYY-MM-DD", amount_column=1, payee_column=2)
    rows = [
        ["Date", "Amount", "Payee"],
        ["2026-01-14", "-1.00", "Blue Owl"],
        ["Subtotal", "-1.00", ""],
        ["2026-01-15", "2.00", "Maple"],
        ["2026-01-16", "n/a", "Initech"],
        ["Total", "1.00", ""],
    ]
    mixed = from_table(_table(rows), spec)
    assert (mixed.skipped_rows, mixed.footer_rows) == ([2, 4], [5])
    # Nothing reads: every row is reported, none is called a footer.
    none = from_table(_table([rows[0], rows[2], rows[5]]), spec)
    assert (none.skipped_rows, none.footer_rows) == ([1, 2], [])


def test_a_pending_date_cell_is_a_pending_row() -> None:
    table = _extracted(layouts.card_with_pending_rows())
    suggestion = _suggested(table)
    assert _reason(suggestion, "date") == (0, "exact")
    assert (suggestion.parsed_rows, suggestion.failed_rows) == (5, ())
    assert suggestion.confident, suggestion.doubts
    parsed = from_table(table, suggestion.spec)
    assert parsed.pending_rows == [1, 2]
    assert parsed.skipped_rows == []
    assert [t.row for t in parsed.transactions] == [3, 4, 5, 6, 7]
    assert parsed.transactions[0].posted_date == date(2026, 10, 27)


def test_an_amount_with_more_decimals_than_are_kept_is_rounded() -> None:
    assert parse_cell_amount("318,40277777778", ",") == Decimal("318.402778")
    tiny = parse_cell_amount("-0.0000005", ".")  # half to even, and never -0
    assert tiny is not None
    assert (tiny, tiny.is_signed()) == (Decimal("0"), False)
    assert str(parse_cell_amount("-48.20", ".")) == "-48.20"  # a short amount stays as printed
    assert parse_cell_amount("2.0000015", ".") == Decimal("2.000002")
    table = _extracted(layouts.export_with_long_decimals())
    suggestion = _suggested(table)
    assert suggestion.failed_rows == ()
    assert suggestion.confident, suggestion.doubts
    assert _read(table, suggestion.spec)[:2] == [Decimal("-58.30"), Decimal("318.402778")]


def test_an_amount_behind_a_lettered_currency_parses() -> None:
    lettered = layouts.LETTERED_CURRENCY
    assert parse_cell_amount(f"{lettered}18.250", ".") == Decimal("18.250")
    assert parse_cell_amount(f"-{lettered}18.250", ".") == Decimal("-18.250")
    assert parse_cell_amount(f"{lettered}-18.250", ".") == Decimal("-18.250")
    assert parse_cell_amount("1 234,50 K\u010d", ",") == Decimal("1234.50")
    assert parse_cell_amount("45,00 z\u0142", ",") == Decimal("45.00")
    assert parse_cell_amount("\u20bd 99.90", ".") == Decimal("99.90")  # a currency sign
    # Words are not currencies: no more than three letters, and not plain ASCII ones.
    words = ("Caf\u00e9s 12", "No 5", "A1", f"{lettered}", "\u062f.\u0625. twelve", "50\u00a2")
    for text in words:
        assert parse_cell_amount(text, ".") is None, text
    table = _extracted(layouts.export_with_lettered_currency())
    suggestion = _suggested(table)
    assert (suggestion.parsed_rows, suggestion.failed_rows) == (4, ())
    assert _read(table, suggestion.spec) == [
        Decimal("-18.250"),
        Decimal("-4.500"),
        Decimal("1250.000"),
        Decimal("-9.750"),
    ]


def test_month_name_dates_without_separators_or_a_century_parse() -> None:
    assert parse_cell_date("07Mar2026 00:00:00", "DDMMMYYYY") == date(2026, 3, 7)
    assert parse_cell_date("7March2026", "DDMMMYYYY") == date(2026, 3, 7)
    assert parse_cell_date("14-Aug-25", "DD-MMM-YY") == date(2025, 8, 14)
    assert parse_cell_date("14-Aug-2025", "DD-MMM-YY") is None
    assert parse_cell_date("14-Aug-25", "DD-MMM-YYYY") is None
    compact = _suggested(_extracted(layouts.export_with_month_name_dates()))
    assert (compact.spec.date_format, compact.confident) == ("DDMMMYYYY", True)
    short = _extracted(layouts.export_with_month_name_dates(short_year=True))
    suggestion = _suggested(short)
    assert (suggestion.spec.date_format, suggestion.confident) == ("DD-MMM-YY", True)
    assert from_table(short, suggestion.spec).transactions[0].posted_date == date(2025, 8, 14)


def test_a_balance_printed_once_a_day_reconciles() -> None:
    table = _extracted(layouts.checking_with_daily_balance())
    suggestion = _suggested(table)
    assert (suggestion.confident, suggestion.sign_evidence) == (True, "balance")
    parsed = from_table(table, suggestion.spec)
    assert parsed.balance_mismatches == []
    assert parsed.closing_balance is not None
    assert (parsed.closing_balance.amount, parsed.closing_balance.as_of) == (
        Decimal("4250.65"),
        date(2026, 7, 8),
    )
    # A day's balance that is not the sum of the day's rows is still caught,
    # on the row that prints it.
    rows = [list(r) for r in table.rows]
    rows[5][3] = "4317.15"
    wrong = from_table(_table(rows), suggestion.spec)
    assert wrong.balance_mismatches == [5, 6]
    # Newest first: the day's balance stands on the day's first printed row.
    newest = _table([list(table.rows[0]), *(list(r) for r in reversed(table.rows[1:]))])
    assert [r[3] for r in newest.rows[1:]] == ["4250.65", "4317.05", "", "", "1555.40", ""]
    assert from_table(newest, suggestion.spec).balance_mismatches == []


# ---------------------------------------------------------------------------
# A day/month tie that the file itself resolves is no guess
# ---------------------------------------------------------------------------

#: Month first: 3 and 9 February, 4 and 11 March. Day first: 2 March,
#: 2 September, 3 April, 3 November. No day is above 12, the days are not
#: the months and neither component is constant: the cells settle nothing.
TIED_DATES = ["02/03/2026", "02/09/2026", "03/04/2026", "03/11/2026"]


def _dates(table: Table, suggestion: MappingSuggestion) -> list[date]:
    return [t.posted_date for t in from_table(table, suggestion.spec).transactions]


def _dated(dates: list[str], today: date | None = None) -> tuple[Table, MappingSuggestion]:
    table = _extracted(layouts.checking_dated(dates))
    suggestion = suggest_mapping(table, today=today)
    assert suggestion is not None
    assert suggestion.sign_evidence == "balance"  # the date is the only question
    return table, suggestion


def test_same_day_under_both_orders_is_no_guess() -> None:
    table, suggestion = _dated(["1/1/2026", "2/2/2026", "3/3/2026"])
    assert suggestion.doubts == ()
    assert (suggestion.confident, suggestion.score) == (True, 1.0)
    assert suggestion.spec.date_format == "MM/DD/YYYY"
    assert _dates(table, suggestion) == [date(2026, 1, 1), date(2026, 2, 2), date(2026, 3, 3)]
    # One row where the day is not the month, and the orders differ again.
    _, mixed = _dated(["1/1/2026", "2/2/2026", "3/4/2026"])
    assert mixed.doubts == ("date_order_guessed",)


def test_constant_component_is_the_month() -> None:
    days = (1, 2, 3, 5, 6, 8, 9, 10)
    table, suggestion = _dated([f"06/{day:02d}/2026" for day in days])
    assert suggestion.doubts == ()
    assert (suggestion.confident, suggestion.score) == (True, 1.0)
    assert suggestion.spec.date_format == "MM/DD/YYYY"
    assert _dates(table, suggestion) == [date(2026, 6, day) for day in days]
    # Three distinct days are the least that say so.
    _, three = _dated(["06/01/2026", "06/01/2026", "06/04/2026", "06/09/2026"])
    assert (three.doubts, three.spec.date_format) == ((), "MM/DD/YYYY")
    # The sample cannot tell day from month and best_date_format still says so:
    # the rules above belong to the cash suggestion, read over the whole table.
    assert best_date_format(f"06/{day:02d}/2026" for day in days) == ("MM/DD/YYYY", 8, True)


def test_two_rows_stay_held() -> None:
    _, suggestion = _dated(["06/01/2026", "06/02/2026"])
    assert suggestion.doubts == ("date_order_guessed",)
    assert not suggestion.confident
    assert suggestion.spec.date_format == "MM/DD/YYYY"  # month first, as before
    assert suggestion.score == 0.9


@pytest.mark.parametrize(
    "dates",
    [
        # The 5th of January, February and March, or three days of May: the
        # second component is constant, so the first would be the day. The
        # order is only ever taken as proven the way it would be guessed
        # (month first); day first needs a cell that reads no other way.
        ["01/05/2026", "02/05/2026", "03/05/2026"],
        ["06/01/2026", "06/01/2026", "06/02/2026", "06/02/2026"],  # one month, two days
        ["01/05/2026", "01/06/2026", "02/05/2026", "02/06/2026"],  # two values of each
        ["06/01/2025", "06/02/2026", "06/03/2026"],  # June of two years is not one month
    ],
    ids=["same-day-three-months", "two-days", "two-by-two", "two-years"],
)
def test_same_day_of_several_months_stays_held(dates: list[str]) -> None:
    _, suggestion = _dated(dates)
    assert suggestion.doubts == ("date_order_guessed",)
    assert suggestion.spec.date_format == "MM/DD/YYYY"


@pytest.mark.parametrize(
    ("headers", "dates", "date_format", "expected"),
    [
        (
            # Post Date is the date column and fits both orders; a
            # transaction on the 30th says which.
            ("Transaction Date", "Post Date"),
            [
                ("01/30/2026", "02/02/2026"),
                ("02/03/2026", "02/04/2026"),
                ("03/04/2026", "03/05/2026"),
                ("03/08/2026", "03/09/2026"),
            ],
            "MM/DD/YYYY",
            [date(2026, 2, 2), date(2026, 2, 4), date(2026, 3, 5), date(2026, 3, 9)],
        ),
        (
            ("Transaction Date", "Post Date"),
            [
                ("30/01/2026", "02/02/2026"),
                ("03/02/2026", "04/02/2026"),
                ("04/03/2026", "05/03/2026"),
                ("08/03/2026", "09/03/2026"),
            ],
            "DD/MM/YYYY",
            [date(2026, 2, 2), date(2026, 2, 4), date(2026, 3, 5), date(2026, 3, 9)],
        ),
        (
            # Transaction Date is the date column; a settlement on the 13th decides.
            ("Transaction Date", "Settlement Date"),
            [
                ("02/03/2026", "02/05/2026"),
                ("02/11/2026", "02/13/2026"),
                ("03/04/2026", "03/06/2026"),
                ("03/09/2026", "03/11/2026"),
            ],
            "MM/DD/YYYY",
            [date(2026, 2, 3), date(2026, 2, 11), date(2026, 3, 4), date(2026, 3, 9)],
        ),
    ],
    ids=["post-date-by-transaction-date", "day-first", "transaction-date-by-settlement-date"],
)
def test_unambiguous_second_date_column_decides(
    headers: tuple[str, str],
    dates: list[tuple[str, str]],
    date_format: str,
    expected: list[date],
) -> None:
    table = _extracted(layouts.export_with_two_date_columns(dates, headers=headers))
    suggestion = _suggested(table)
    assert suggestion.spec.date_column == (1 if headers[1] == "Post Date" else 0)
    assert suggestion.doubts == ()
    assert suggestion.spec.date_format == date_format
    assert _dates(table, suggestion) == expected


def test_a_second_date_column_that_fits_both_orders_decides_nothing() -> None:
    dates = [
        ("02/03/2026", "02/04/2026"),
        ("02/09/2026", "02/10/2026"),
        ("03/04/2026", "03/05/2026"),
    ]
    suggestion = _suggested(_extracted(layouts.export_with_two_date_columns(dates)))
    assert suggestion.doubts == ("date_order_guessed",)
    # Nor does one whose cells do not agree with each other.
    mixed = [
        ("01/30/2026", "02/02/2026"),
        ("30/01/2026", "02/04/2026"),
        ("03/04/2026", "03/05/2026"),
    ]
    suggestion = _suggested(_extracted(layouts.export_with_two_date_columns(mixed)))
    assert suggestion.doubts == ("date_order_guessed",)
    # A column of dates under a header that names no date is not asked.
    unnamed = layouts.export_with_two_date_columns(
        [("01/30/2026", "02/02/2026"), ("02/13/2026", "02/04/2026"), ("03/04/2026", "03/05/2026")],
        headers=("Reference", "Post Date"),
    )
    assert _suggested(_extracted(unnamed)).doubts == ("date_order_guessed",)


def test_reading_that_dates_rows_after_upload_is_rejected() -> None:
    # Uploaded on 20 March: day first would date rows in April, September and November.
    table, suggestion = _dated(TIED_DATES, today=date(2026, 3, 20))
    assert suggestion.doubts == ()
    assert suggestion.spec.date_format == "MM/DD/YYYY"
    assert _dates(table, suggestion)[-1] == date(2026, 3, 11)
    # The same cells written day first (3 and 9 February, 4 and 11 March).
    swapped = ["/".join((d, m, y)) for m, d, y in (cell.split("/") for cell in TIED_DATES)]
    table, suggestion = _dated(swapped, today=date(2026, 3, 20))
    assert suggestion.doubts == ()
    assert suggestion.spec.date_format == "DD/MM/YYYY"
    assert _dates(table, suggestion) == [
        date(2026, 2, 3),
        date(2026, 2, 9),
        date(2026, 3, 4),
        date(2026, 3, 11),
    ]
    # A row dated the day after the upload is the same day somewhere else on
    # the globe: it does not rule its reading out.
    _, eve = _dated(TIED_DATES, today=date(2026, 3, 10))
    assert (eve.doubts, eve.spec.date_format) == ((), "MM/DD/YYYY")


@pytest.mark.parametrize(
    "today",
    [None, date(2026, 12, 31), date(2026, 2, 5)],
    ids=["no-upload-day", "both-in-the-past", "both-after-upload"],
)
def test_an_upload_day_that_rules_out_neither_or_both_orders_decides_nothing(
    today: date | None,
) -> None:
    _, suggestion = _dated(TIED_DATES, today=today)
    assert suggestion.doubts == ("date_order_guessed",)
    assert suggestion.spec.date_format == "MM/DD/YYYY"


def test_proofs_of_the_date_order_that_disagree_decide_nothing() -> None:
    # The transaction on the 28th says month first. The upload day says day
    # first: month first dates the postings up to 2 May, after the upload,
    # and day first dates none later than 5 February.
    dates = [
        ("02/28/2026", "03/01/2026"),
        ("03/30/2026", "04/01/2026"),
        ("04/30/2026", "05/02/2026"),
    ]
    table = _extracted(layouts.export_with_two_date_columns(dates))
    suggestion = suggest_mapping(table, today=date(2026, 3, 20))
    assert suggestion is not None
    assert suggestion.doubts == ("date_order_guessed",)
    assert suggestion.spec.date_format == "MM/DD/YYYY"
    # Days of one month that are all after the upload, whichever way they are
    # read: the month is not taken as proven for a file that cannot be right.
    _, june = _dated(["06/07/2026", "06/08/2026", "06/09/2026"], today=date(2026, 5, 20))
    assert june.doubts == ("date_order_guessed",)


def test_the_rows_below_the_sample_count_too() -> None:
    sample = [f"06/{1 + index // 17:02d}/2026" for index in range(204)]  # 1 to 12 June
    assert len(sample) > SUGGEST_SAMPLE_ROWS
    # A row on the 13th, below the rows the roles are assigned from, reads one way only.
    _, suggestion = _dated([*sample, "06/13/2026", "06/14/2026"])
    assert (suggestion.doubts, suggestion.spec.date_format) == ((), "MM/DD/YYYY")
    assert suggestion.parsed_rows == 206
    # Rows of July down there: the first component is not constant after all.
    _, suggestion = _dated([*sample, "07/01/2026", "07/02/2026"])
    assert suggestion.doubts == ("date_order_guessed",)


def test_card_file_is_still_held_for_its_sign() -> None:
    table = _extracted(layouts.card_positive_purchases(low_days=True))
    suggestion = _suggested(table)
    assert suggestion.spec.date_format == "MM/DD/YYYY"
    assert suggestion.doubts == ("sign_unverified",)  # the dates are proven, the sign is not
    assert not suggestion.confident
    assert suggestion.sign_evidence is None


# ---------------------------------------------------------------------------
# A table without a header gets a suggestion
# ---------------------------------------------------------------------------


def _columns(suggestion: MappingSuggestion) -> dict[str, int]:
    return {r.role: r.column for r in suggestion.reasons}


def test_headerless_table_with_reconciling_balance_is_confident() -> None:
    table = _extracted(layouts.headerless_with_balance())
    suggestion = _suggested(table)
    spec = suggestion.spec
    assert (spec.has_header, spec.skip_rows) == (False, 0)
    # The check numbers are numbers too: only the debit, credit and balance add up.
    assert _columns(suggestion) == {"date": 0, "payee": 2, "debit": 3, "credit": 4, "balance": 5}
    assert {r.match for r in suggestion.reasons} == {"content"}
    assert suggestion.doubts == ()
    assert suggestion.confident
    assert suggestion.sign_evidence == "balance"
    assert spec.date_format == "MM/DD/YYYY"
    # The first line is a transaction, not a header: every row reads.
    assert (suggestion.parsed_rows, suggestion.failed_rows, suggestion.footer_rows) == (8, (), 0)
    amounts = _read(table, spec)
    assert amounts[:3] == [Decimal("2840.00"), Decimal("-1210.00"), Decimal("-74.60")]
    (statement,) = from_table_grouped(table, spec)
    assert statement.balance_mismatches == []
    assert statement.closing_balance is not None
    assert statement.closing_balance.amount == Decimal("4516.35")
    assert suggestion.score == 1.0


def test_headerless_signed_amount_is_suggested_not_confident() -> None:
    table = _extracted(layouts.headerless_five_quoted_columns())
    suggestion = _suggested(table)
    spec = suggestion.spec
    assert (spec.has_header, spec.skip_rows) == (False, 0)
    # The asterisk column and the empty one take no role.
    assert _columns(suggestion) == {"date": 0, "amount": 1, "payee": 4}
    assert suggestion.doubts == ("no_header",)
    assert not suggestion.confident
    assert suggestion.sign_evidence is None
    assert suggestion.summary()["doubts"] == ["no_header"]
    assert (spec.date_format, spec.sign) == ("MM/DD/YYYY", Sign.INFLOW_POSITIVE)
    assert suggestion.parsed_rows == len(layouts.HEADERLESS_SIGNED_ROWS)
    (statement,) = from_table_grouped(table, spec)
    first = statement.transactions[0]
    assert (first.posted_date, first.amount) == (date(2026, 9, 2), Decimal("-1210.00"))
    assert first.raw_payee == layouts.HEADERLESS_SIGNED_ROWS[0][2]
    assert 0 < suggestion.score < 1


def test_headerless_with_direction_cell_uses_it() -> None:
    table = _extracted(layouts.headerless_with_direction_cell())
    suggestion = _suggested(table)
    spec = suggestion.spec
    assert spec.has_header is False
    assert _columns(suggestion) == {"date": 0, "payee": 1, "amount": 3, "indicator": 4}
    assert spec.outflow_values == ["debit"]
    assert suggestion.sign_evidence == "indicator"
    amounts = _read(table, spec)
    assert (amounts[0], amounts[5]) == (Decimal("-1210.00"), Decimal("2840.00"))
    # The words decide the sign; that the fourth column is the amount is
    # still read from its shape alone, so a person looks before it imports.
    assert suggestion.doubts == ("no_header",)
    # Amounts that carry their own sign are compared with the words instead.
    signed_table = _extracted(layouts.headerless_with_direction_cell(signed=True))
    signed = _suggested(signed_table)
    assert _columns(signed) == {"date": 0, "payee": 1, "amount": 3}
    assert signed.spec.indicator_column is None
    assert (signed.spec.sign, signed.sign_evidence) == (
        Sign.OUTFLOW_POSITIVE,
        "indicator_correlation",
    )
    assert signed.doubts == ("no_header",)
    assert _read(signed_table, signed.spec) == amounts


def test_headerless_export_of_two_accounts_is_never_read_as_one_on_its_own() -> None:
    """No header says which column of names is the account: a person does."""
    rows = [
        [day, account, text, amount.removeprefix("-"), "debit" if amount[0] == "-" else "credit"]
        for day, account, text, amount in layouts.TWO_ACCOUNT_ROWS
    ]
    suggestion = _suggested(_table(rows))
    assert suggestion.sign_evidence == "indicator"
    assert suggestion.spec.account_column is None
    assert suggestion.accounts == 1
    assert suggestion.doubts == ("no_header",)
    assert not suggestion.confident


def test_one_row_file_gets_no_suggestion() -> None:
    row = ["09/02/2026", "-1210.00", "*", "", "WILLOW COURT RENT"]
    assert suggest_mapping(_table([row])) is None
    # A title line above it does not make it a table either.
    assert suggest_mapping(_table([["Synthetic export"], row])) is None
    # Two transactions are the least a column's shape can be read from.
    second = ["09/15/2026", "2840.00", "*", "", "MARLOW DESIGN PAYROLL"]
    two = _suggested(_table([row, second]))
    assert two.doubts == ("no_header",)
    assert two.parsed_rows == 2


def test_a_first_row_of_words_nothing_knows_is_never_read_as_data() -> None:
    rows = [
        ["Paivamaara", "Summa", "Selite"],
        ["09/14/2026", "-48.15", "HARBOR LANE GROCERY"],
        ["09/15/2026", "-12.00", "TIN ROOF DINER"],
        ["09/16/2026", "250.00", "MARLOW DESIGN PAYROLL"],
    ]
    # An unknown header is still a header: its words may say what the shapes cannot.
    assert suggest_mapping(_table(rows)) is None
    # A column that mixes dates and words is not the date column.
    assert suggest_mapping(_table([*rows[1:], ["n/a", "-3.00", "SILVER FERN CAFE"]])) is None


def test_headerless_balance_owed_is_not_taken_for_an_accounts_balance() -> None:
    """A card's balance owed adds up with the purchases read as money in."""
    table = _extracted(layouts.headerless_card_with_balance_owed())
    suggestion = _suggested(table)
    assert not suggestion.confident
    assert "sign_unverified" in suggestion.doubts
    assert suggestion.parsed_rows == len(layouts.CARD_ROWS)


def test_headerless_balance_that_two_readings_fit_proves_no_column() -> None:
    rows = []
    balance = Decimal("900.00")
    for day, (_, amount) in enumerate(layouts.DATED_ROWS, start=13):
        balance += Decimal(amount)
        # The amount is printed twice (as booked and as converted).
        rows.append([f"2026-09-{day}", "Synthetic payee", amount, amount, str(balance)])
    suggestion = _suggested(_table(rows))
    assert suggestion.doubts == ("no_header",)
    assert suggestion.sign_evidence is None
    assert suggestion.spec.balance_column is None
    assert suggestion.spec.amount_column == 2


def test_headerless_debit_and_credit_columns_without_a_balance_are_a_guess() -> None:
    rows = [
        [day, text, debit, credit]
        for day, _, text, debit, credit in layouts.HEADERLESS_BALANCE_ROWS
    ]
    suggestion = _suggested(_table(rows))
    spec = suggestion.spec
    assert (spec.debit_column, spec.credit_column, spec.balance_column) == (2, 3, None)
    # Which of the two is the money out is the usual order, not a fact of the file.
    assert suggestion.doubts == ("no_header",)
    assert suggestion.sign_evidence is None
    assert not suggestion.confident


def test_headerless_whole_numbers_are_not_the_amount_beside_priced_cells() -> None:
    rows = [
        [day, str(70412 + index), text, amount]
        for index, (day, amount, text) in enumerate(layouts.HEADERLESS_SIGNED_ROWS)
    ]
    suggestion = _suggested(_table(rows))
    assert _columns(suggestion) == {"date": 0, "payee": 2, "amount": 3}
    assert suggestion.doubts == ("no_header",)


def test_headerless_day_month_tie_is_still_a_guess() -> None:
    rows = [
        [day, amount, text]
        for day, (text, amount) in zip(
            ("02/03/2026", "02/09/2026", "03/04/2026", "03/11/2026", "04/05/2026", "04/12/2026"),
            layouts.DATED_ROWS,
            strict=True,
        )
    ]
    suggestion = _suggested(_table(rows))
    assert suggestion.doubts == ("no_header", "date_order_guessed")
    assert suggestion.spec.date_format == "MM/DD/YYYY"
    # The day the file arrived settles it for a table without a header too.
    settled = suggest_mapping(_table(rows), today=date(2026, 4, 20))
    assert settled is not None
    assert settled.doubts == ("no_header",)


def test_a_table_with_a_header_is_never_read_as_headerless() -> None:
    suggestion = _suggested(_extracted(layouts.checking_with_running_balance()))
    assert (suggestion.spec.has_header, suggestion.doubts) == (True, ())
    assert {r.match for r in suggestion.reasons} == {"exact"}
