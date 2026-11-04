# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured extractors (CSV/TSV, OFX/QFX, QIF) on synthetic files.

The extractors are called in-process here for speed; ``test_through_sandbox``
runs each format through the real sandbox once. XXE and entity bombs must be
refused (SECURITY.md C6), formulas neutralised (C7), caps applied (C2).
"""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal

import pytest

from app.core.csv_safety import is_dangerous, neutralise_formula
from app.ingestion.extract.common import ExtractFailure, decode_text, parse_amount
from app.ingestion.extract.delimited import extract_delimited
from app.ingestion.extract.ofx import extract_ofx
from app.ingestion.extract.qif import extract_qif
from app.ingestion.sandbox.protocol import ExtractOptions
from app.ingestion.sandbox.runner import run_extraction
from tests.fixtures.statements import (
    BILLION_LAUGHS_PROLOG,
    XXE_PROLOG,
    account_number,
    csv_bytes,
    ofx_sgml,
    ofx_xml,
    qif_bytes,
    synth_transactions,
)

OPTIONS = ExtractOptions()
TXNS = synth_transactions(6, seed=11)


# ---------------------------------------------------------------------------
# C7: formula neutralisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "=1+2",
        "+cmd|' /C calc'!A0",
        "-2+3+cmd|'/C calc'!A0",
        "@SUM(A1:A2)",
        "\tx",
        "\r=x",
        '=HYPERLINK("http://example.invalid","x")',
    ],
)
def test_dangerous_cells_are_neutralised(value: str) -> None:
    assert is_dangerous(value)
    assert neutralise_formula(value) == "'" + value


@pytest.mark.parametrize("value", ["-12.34", "+1,000.00", "-$5", "- 3", "-", "", "Shop", "12"])
def test_numbers_and_text_are_kept(value: str) -> None:
    assert neutralise_formula(value) == value


def test_csv_cells_are_neutralised_on_import() -> None:
    data = b'Date,Amount,Payee\n2026-01-02,-12.34,"=HYPERLINK(""x"")"\n2026-01-03,@x,+1\n'
    table = extract_delimited(data, OPTIONS, "csv").tables[0]
    assert table.rows[1] == ["2026-01-02", "-12.34", '\'=HYPERLINK("x")']
    assert table.rows[2] == ["2026-01-03", "'@x", "+1"]


# ---------------------------------------------------------------------------
# CSV / TSV
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("delimiter", [",", ";", "|", "\t"])
def test_csv_dialect_is_detected(delimiter: str) -> None:
    result = extract_delimited(csv_bytes(TXNS, delimiter=delimiter), OPTIONS, "csv")
    table = result.tables[0]
    assert result.ok
    assert table.delimiter == delimiter
    assert table.has_header
    assert table.column_count == 4
    assert len(table.rows) == len(TXNS) + 1
    assert table.rows[1][1] == str(TXNS[0].amount)


def test_tsv_is_tab_separated() -> None:
    result = extract_delimited(csv_bytes(TXNS, delimiter="\t"), OPTIONS, "tsv")
    assert result.tables[0].delimiter == "\t"


@pytest.mark.parametrize(
    ("data", "encoding"),
    [
        ("Date,Payee\n2026-01-01,Café\n".encode(), "utf-8"),
        (b"\xef\xbb\xbfDate,Payee\n2026-01-01,Caf\xc3\xa9\n", "utf-8"),
        (b"Date,Payee\n2026-01-01,Caf\xe9 \x80\n", "cp1252"),
        (b"Date,Payee\n2026-01-01,Caf\xe9 \x81\n", "latin-1"),
    ],
)
def test_encoding_detection(data: bytes, encoding: str) -> None:
    table = extract_delimited(data, OPTIONS, "csv").tables[0]
    assert table.encoding == encoding
    assert table.rows[0][0] == "Date"  # BOM stripped
    assert table.rows[1][1].startswith("Caf")


@pytest.mark.parametrize(
    "data",
    [b'a,b\n1,"unterminated\n2,3\n', b'a,b,c\n1,"x"y,2\n3,4,5\n'],
)
def test_malformed_quoting_fails_the_file(data: bytes) -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_delimited(data, OPTIONS, "csv")
    assert caught.value.code in ("malformed_csv", "no_delimiter")


def test_no_consistent_delimiter() -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_delimited(b"just words\nmore words\n", OPTIONS, "csv")
    assert caught.value.code == "no_delimiter"


def test_row_cap_truncates_and_says_so() -> None:
    data = csv_bytes(synth_transactions(50))
    result = extract_delimited(data, ExtractOptions(max_rows=10), "csv")
    assert len(result.tables[0].rows) == 10
    assert result.tables[0].truncated
    assert "rows_truncated:10" in result.warnings


def test_column_cap_is_an_error() -> None:
    data = (",".join(["c"] * 30) + "\n" + ",".join(["1"] * 30) + "\n").encode()
    with pytest.raises(ExtractFailure) as caught:
        extract_delimited(data, ExtractOptions(max_columns=20), "csv")
    assert caught.value.code == "too_many_columns"


def test_long_cells_are_cut() -> None:
    data = b"a,b\n1," + b"x" * 500 + b"\n"
    result = extract_delimited(data, ExtractOptions(max_cell_chars=100), "csv")
    assert len(result.tables[0].rows[1][1]) == 100
    assert "cells_truncated:1" in result.warnings


def test_huge_field_is_refused() -> None:
    data = b'a,b\n1,"' + b"x" * 100_000 + b'"\n'
    with pytest.raises(ExtractFailure) as caught:
        extract_delimited(data, ExtractOptions(max_cell_chars=1000), "csv")
    assert caught.value.code == "malformed_csv"


def test_binary_and_utf16_are_refused() -> None:
    with pytest.raises(ExtractFailure):
        decode_text(b"a,b\x00\n")
    with pytest.raises(ExtractFailure):
        decode_text("a,b\n".encode("utf-16"))


# ---------------------------------------------------------------------------
# OFX / QFX
# ---------------------------------------------------------------------------


def test_ofx_sgml_statement() -> None:
    acct = account_number(3)
    result = extract_ofx(ofx_sgml(TXNS, acct=acct), OPTIONS, "ofx")
    statement = result.statements[0]
    assert statement.source_format == "ofx"
    assert statement.account.last4 == acct[-4:]
    assert statement.account.account_type == "checking"
    assert statement.account.currency == "USD"
    assert statement.period_start == TXNS[0].posted
    assert statement.period_end == TXNS[-1].posted
    assert statement.ledger_balance is not None
    assert statement.ledger_balance.amount == Decimal("1234.56")
    assert [t.amount for t in statement.transactions] == [t.amount for t in TXNS]
    assert [t.fitid for t in statement.transactions] == [t.fitid for t in TXNS]
    assert statement.transactions[0].payee == TXNS[0].payee
    assert statement.transactions[0].posted == TXNS[0].posted


def test_full_account_number_never_leaves_the_extractor() -> None:
    acct = account_number(5)
    result = extract_ofx(ofx_sgml(TXNS, acct=acct), OPTIONS, "ofx")
    assert acct not in result.model_dump_json()


def test_qfx_is_ofx() -> None:
    result = extract_ofx(ofx_sgml(TXNS, qfx=True), OPTIONS, "qfx")
    assert result.statements[0].source_format == "qfx"


def test_ofx_xml_credit_card_statement() -> None:
    result = extract_ofx(ofx_xml(TXNS), OPTIONS, "ofx")
    statement = result.statements[0]
    assert statement.account.account_type == "creditcard"
    assert statement.account.currency == "EUR"
    assert len(statement.transactions) == len(TXNS)
    assert statement.ledger_balance is not None
    assert statement.ledger_balance.amount == Decimal("-200.10")


def test_ofx_text_is_neutralised() -> None:
    data = ofx_sgml(TXNS).replace(TXNS[0].payee.encode(), b"=cmd|'/C calc'!A0")
    result = extract_ofx(data, OPTIONS, "ofx")
    assert result.statements[0].transactions[0].payee == "'=cmd|'/C calc'!A0"


@pytest.mark.parametrize("prolog", [XXE_PROLOG, BILLION_LAUGHS_PROLOG])
def test_ofx_xml_with_dtd_or_entities_is_refused(prolog: str) -> None:
    data = ofx_xml(TXNS, prolog=prolog).replace(b"<MEMO>Memo 0</MEMO>", b"<MEMO>&xxe;</MEMO>")
    started = time.monotonic()
    with pytest.raises(ExtractFailure) as caught:
        extract_ofx(data, OPTIONS, "ofx")
    assert caught.value.code == "xml_forbidden"
    assert time.monotonic() - started < 2


def test_ofx_sgml_with_markup_declarations_is_refused() -> None:
    data = ofx_sgml(TXNS).replace(b"<OFX>", b'<!DOCTYPE OFX [<!ENTITY x "y">]>\n<OFX>', 1)
    with pytest.raises(ExtractFailure) as caught:
        extract_ofx(data, OPTIONS, "ofx")
    assert caught.value.code == "markup_declaration_forbidden"


def test_ofx_without_statement() -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_ofx(
            b"OFXHEADER:100\n\n<OFX><SIGNONMSGSRSV1></SIGNONMSGSRSV1></OFX>", OPTIONS, "ofx"
        )
    assert caught.value.code == "no_statements"


def test_ofx_deep_nesting_is_refused() -> None:
    data = b"OFXHEADER:100\n\n<OFX>" + b"<A>" * 200 + b"<STMTRS>"
    with pytest.raises(ExtractFailure) as caught:
        extract_ofx(data, OPTIONS, "ofx")
    assert caught.value.code == "too_deep"


def test_ofx_transaction_cap() -> None:
    result = extract_ofx(
        ofx_sgml(synth_transactions(30)), ExtractOptions(max_transactions=5), "ofx"
    )
    assert len(result.statements[0].transactions) == 5
    assert result.statements[0].truncated


def test_ofx_bad_amounts_are_skipped_with_a_warning() -> None:
    data = ofx_sgml(TXNS).replace(f"<TRNAMT>{TXNS[1].amount}".encode(), b"<TRNAMT>NaN")
    result = extract_ofx(data, OPTIONS, "ofx")
    assert len(result.statements[0].transactions) == len(TXNS) - 1
    assert "transaction_skipped_invalid_date_or_amount" in result.warnings


# ---------------------------------------------------------------------------
# QIF
# ---------------------------------------------------------------------------


def test_qif_month_first_with_apostrophe_year() -> None:
    txns = synth_transactions(5, start=date(2026, 3, 1))
    result = extract_qif(qif_bytes(txns, date_style="mdy"), OPTIONS, "qif")
    statement = result.statements[0]
    assert statement.account.name == "Account-synthetic"
    assert statement.account.account_type == "bank"
    assert [t.posted for t in statement.transactions] == [t.posted for t in txns]
    assert [t.amount for t in statement.transactions] == [t.amount for t in txns]
    assert statement.transactions[0].category == "Groceries"
    assert statement.transactions[0].cleared == "cleared"


def test_qif_day_first_is_detected() -> None:
    txns = synth_transactions(5, start=date(2026, 1, 20))
    result = extract_qif(qif_bytes(txns, date_style="dmy"), OPTIONS, "qif")
    assert result.statements[0].date_order == "dmy"
    assert result.statements[0].transactions[0].posted == date(2026, 1, 20)


def test_qif_ambiguous_dates_assume_month_first() -> None:
    txns = synth_transactions(3, start=date(2026, 2, 1))  # all components <= 12
    result = extract_qif(qif_bytes(txns, date_style="mdy"), OPTIONS, "qif")
    assert result.statements[0].date_order == "mdy_assumed"
    assert "date_order_assumed_mdy" in result.warnings


def test_qif_iso_dates() -> None:
    result = extract_qif(qif_bytes(TXNS, date_style="iso"), OPTIONS, "qif")
    assert result.statements[0].date_order == "ymd"


def test_qif_splits_and_skipped_sections() -> None:
    data = (
        b"!Type:Cat\nNGroceries\n^\n!Type:CCard\nD1/15'26\nT-30.00\nPPayee-a1\n"
        b"SFood\nEpart one\n$-10.00\nSHome\n$-20.00\n^\n!Type:Invst\nD1/16'26\n^\n"
    )
    result = extract_qif(data, OPTIONS, "qif")
    assert len(result.statements) == 1
    txn = result.statements[0].transactions[0]
    assert result.statements[0].account.account_type == "creditcard"
    assert [s.amount for s in txn.splits] == [Decimal("-10.00"), Decimal("-20.00")]
    assert txn.splits[0].memo == "part one"
    assert "section_skipped:cat" in result.warnings
    assert "section_skipped:invst" in result.warnings


def test_qif_inconsistent_dates_fail() -> None:
    data = b"!Type:Bank\nD13/01/2026\nT1\n^\nD01/13/2026\nT2\n^\n"
    with pytest.raises(ExtractFailure) as caught:
        extract_qif(data, OPTIONS, "qif")
    assert caught.value.code == "inconsistent_dates"


def test_qif_formula_payee_is_neutralised() -> None:
    data = b"!Type:Bank\nD01/13/2026\nT-1.00\nP=1+1\n^\n"
    result = extract_qif(data, OPTIONS, "qif")
    assert result.statements[0].transactions[0].payee == "'=1+1"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1,234.56", "1234.56"),
        ("-12,34", "-12.34"),
        ("+5", "5"),
        ("-.5", "-0.5"),
        ("1e9", None),
        ("NaN", None),
        ("12 345", "12345"),
        ("", None),
        ("1" * 20, None),
    ],
)
def test_parse_amount(raw: str, expected: str | None) -> None:
    value = parse_amount(raw)
    assert (None if value is None else str(value)) == (
        None if expected is None else str(Decimal(expected))
    )


# ---------------------------------------------------------------------------
# Through the real sandbox
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "data"),
    [
        ("csv", csv_bytes(TXNS)),
        ("tsv", csv_bytes(TXNS, delimiter="\t")),
        ("ofx", ofx_sgml(TXNS)),
        ("qfx", ofx_sgml(TXNS, qfx=True)),
        ("ofx", ofx_xml(TXNS)),
        ("qif", qif_bytes(TXNS)),
    ],
)
def test_through_sandbox(kind: str, data: bytes) -> None:
    result = run_extraction(kind, data)
    assert result.ok, result.error
    assert result.kind == kind
    assert result.tables or result.statements


def test_xxe_is_refused_through_sandbox() -> None:
    result = run_extraction("ofx", ofx_xml(TXNS, prolog=XXE_PROLOG))
    assert not result.ok
    assert result.error is not None
    assert result.error.code == "xml_forbidden"
