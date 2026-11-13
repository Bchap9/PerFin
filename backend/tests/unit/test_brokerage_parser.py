# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brokerage statements: OFX ``INVSTMTRS`` extraction, the PDF parser, reconciliation.

Synthetic statements from ``tests/fixtures/synth_brokerage.py`` (the
fictional Placeholder Brokerage, invented instruments). PDFs are extracted
in process (``extract_pdf``), classified by the shipped fingerprints and
parsed with the classifier's hint, as the worker does.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.classify.service import classify_extraction
from app.ingestion.extract.common import ExtractFailure
from app.ingestion.extract.ofx import extract_ofx
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import confidence
from app.ingestion.parsers.brokerage import (
    ACTIVITY_TYPES,
    activity_type,
    expand_action,
    read_number,
    reconcile_investment,
    split_ratio,
)
from app.ingestion.parsers.registry import parse_extraction, reparse
from app.ingestion.sandbox.protocol import (
    Balance,
    ExtractOptions,
    ExtractResult,
    InvestmentActivity,
    InvestmentPosition,
    InvestmentStatement,
    SecurityRef,
)
from tests.fixtures import synth_brokerage as synth

OPTIONS = ExtractOptions(previews=False, ocr=False)
Row = tuple[date, str, str | None, Decimal | None, Decimal]


def _parse(data: bytes, *, hinted: bool = True) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    if not hinted:
        return parse_extraction(result, threshold=0.85)
    found = classify_extraction(result)
    return parse_extraction(
        result,
        threshold=0.85,
        institution=found.fingerprint.key if found.fingerprint else None,
        parser_name=found.parser,
        kind=found.kind,
        date_order=found.fingerprint.spec.date_order if found.fingerprint else None,
    )


def _only(result: ExtractResult) -> InvestmentStatement:
    assert len(result.investments) == 1
    return result.investments[0]


def _activity(statement: InvestmentStatement) -> list[Row]:
    rows = [
        (
            a.trade_date,
            a.type,
            a.security.symbol if a.security else None,
            a.quantity,
            a.amount,
        )
        for a in statement.activity
    ]
    return sorted(rows, key=lambda r: (r[0], r[1], r[2] or "", r[4]))


def _positions(statement: InvestmentStatement) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    out = []
    for p in statement.positions:
        assert p.security.symbol is not None
        assert p.unit_price is not None
        assert p.market_value is not None
        out.append((p.security.symbol, p.quantity, p.unit_price, p.market_value))
    return sorted(out)


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------


def test_placeholder_brokerage_pdf_is_classified_and_read_exactly() -> None:
    stmt = synth.brokerage_statement()
    result = _parse(synth.placeholder_pdf(stmt))
    found = classify_extraction(extract_pdf(synth.placeholder_pdf(stmt), OPTIONS, "pdf"))
    assert found.fingerprint is not None
    assert found.fingerprint.key == synth.INSTITUTION_KEY

    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "placeholder_brokerage"
    assert result.parse.parser.institution == synth.INSTITUTION_KEY
    assert result.statements == []  # not read as a bank statement as well
    statement = _only(result)
    assert statement.source_format == "pdf"
    assert statement.account.last4 == stmt.last4
    assert statement.account.institution == synth.INSTITUTION
    assert statement.account.currency == "USD"
    assert (statement.period_start, statement.period_end) == (stmt.start, stmt.end)
    assert statement.as_of == stmt.end
    assert _activity(statement) == synth.expected_activity(stmt)
    assert _positions(statement) == synth.expected_positions(stmt)
    assert statement.cash_opening is not None
    assert statement.cash_opening.amount == stmt.cash_opening
    assert statement.cash_closing is not None
    assert statement.cash_closing.amount == stmt.cash_closing
    assert statement.total_value is not None
    assert statement.total_value.amount == stmt.total_value

    check = statement.reconciliation
    assert check is not None
    assert check.status == "ok"
    assert check.checks == ["positions", "holdings_total", "cash", "total_value"]
    assert check.cash_difference == 0
    assert statement.confidence["reconciliation"] == 1.0
    rows: list[InvestmentActivity | InvestmentPosition] = [
        *statement.activity,
        *statement.positions,
    ]
    for row in rows:
        assert row.confidence == 1.0
        assert row.low_confidence == []
        assert row.page is not None
        assert row.bbox is not None
    # The activity table runs over two pages (header repeated).
    assert {a.page for a in statement.activity} == {1, 2}
    # The PDF names the reinvested fund's symbol; the dividend row is tied to it.
    reinvest = next(a for a in statement.activity if a.type == "reinvest")
    assert reinvest.security is not None
    assert reinvest.security.name == "Sample Bond Fund"
    assert reinvest.security.asset_class == "mutual_fund"
    # Cash rows carry no security even though their description starts in capitals.
    assert all(a.security is None for a in statement.activity if a.type in ("deposit", "fee"))


def test_unknown_broker_is_read_by_the_generic_parser() -> None:
    stmt = synth.brokerage_statement(seed=53)
    data = synth.placeholder_pdf(stmt, institution="Riverbend Test Investments")
    result = _parse(data, hinted=False)
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "brokerage_statement_generic"
    scores = {s.name: s.score for s in result.parse.scores}
    # Above the bank-statement parser that also sees a date/description/amount table.
    assert scores["brokerage_statement_generic"] > scores["statement_generic"]
    statement = _only(result)
    assert statement.account.institution is None
    assert _activity(statement) == synth.expected_activity(stmt)
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    for row in statement.activity:
        assert row.confidence == confidence.GENERIC
        assert row.low_confidence == []


@pytest.mark.parametrize(
    ("tamper", "failed"),
    [
        ("extra_row", ["cash"]),
        ("market_value", ["positions", "total_value"]),
        ("total", ["total_value"]),
    ],
)
def test_tampered_pdf_does_not_reconcile(tamper: synth.Tamper, failed: list[str]) -> None:
    stmt = synth.brokerage_statement()
    statement = _only(_parse(synth.placeholder_pdf(stmt, tamper=tamper)))
    check = statement.reconciliation
    assert check is not None
    assert check.status == "mismatch"
    assert check.failed == failed
    if tamper == "market_value":
        assert check.position_mismatches == [0]
        assert "market_value" in statement.positions[0].low_confidence
    if tamper == "extra_row":
        assert check.cash_difference == Decimal("-777.77")
    # Only the rows the statement's figures leave in doubt are capped below
    # the threshold (SECURITY.md C12; ``localise``).
    doubted: dict[str, tuple[list[int], list[int]]] = {
        "extra_row": ([7], []),  # the one row that is the whole difference
        "market_value": ([], [0]),  # the position whose value is not qty x price
        "total": ([], []),  # holdings and cash add up; the account value alone differs
    }
    doubtful_activity, doubtful_positions = doubted[str(tamper)]
    assert check.doubtful_activity == doubtful_activity
    assert check.doubtful_positions == doubtful_positions
    for index, activity in enumerate(statement.activity):
        assert activity.confidence is not None
        assert ("reconciliation" in activity.low_confidence) == (index in doubtful_activity)
        if index in doubtful_activity:
            assert activity.confidence <= confidence.UNRECONCILED
    for index, position in enumerate(statement.positions):
        assert position.confidence is not None
        assert ("reconciliation" in position.low_confidence) == (index in doubtful_positions)
        if index in doubtful_positions:
            assert position.confidence <= confidence.UNRECONCILED
    if tamper == "extra_row":
        forged = statement.activity[7]
        assert (forged.type, forged.amount) == ("buy", Decimal("-777.77"))


def test_reparse_keeps_the_brokerage_parser_and_replaces_its_output() -> None:
    stmt = synth.brokerage_statement()
    first = _parse(synth.placeholder_pdf(stmt))
    again = reparse(first)
    assert again.parse is not None
    assert again.parse.parser is not None
    assert again.parse.parser.name == "placeholder_brokerage"
    assert len(again.investments) == 1
    assert _activity(again.investments[0]) == _activity(first.investments[0])


# ---------------------------------------------------------------------------
# OFX INVSTMTRS (sandbox extractor)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("xml", [False, True], ids=["sgml", "xml"])
def test_ofx_investment_statement_is_extracted(xml: bool) -> None:
    stmt = synth.brokerage_statement()
    data = synth.ofx_investment(stmt, xml=xml)
    result = extract_ofx(data, OPTIONS, "ofx")
    assert result.ok
    assert result.statements == []
    assert result.warnings == []
    assert not result.worker_only_fields()
    statement = _only(result)
    assert statement.source_format == "ofx"
    assert statement.account.last4 == stmt.last4
    assert statement.account.institution == synth.INSTITUTION
    assert statement.account.currency == "USD"
    assert (statement.period_start, statement.period_end) == (stmt.start, stmt.end)
    assert statement.as_of == stmt.end
    assert _activity(statement) == synth.expected_activity(stmt)
    assert _positions(statement) == synth.expected_positions(stmt)
    assert statement.cash_closing == Balance(amount=stmt.cash_closing, as_of=stmt.end)
    by_symbol = {p.security.symbol: p.security for p in statement.positions}
    assert by_symbol["PBXA"].cusip == synth.BY_SYMBOL["PBXA"].cusip
    assert by_symbol["PBXA"].asset_class == "mutual_fund"
    assert by_symbol["PBXB"].asset_class == "equity"
    sale = next(a for a in statement.activity if a.type == "sell")
    assert sale.settle_date is not None
    assert sale.fitid
    # The full account number never leaves the child; only its last four digits.
    assert stmt.account_number not in result.model_dump_json()
    check = reconcile_investment(statement)
    assert check.status == "ok"
    assert check.checks == ["positions"]


def test_ofx_with_bank_and_investment_statements_keeps_both() -> None:
    stmt = synth.brokerage_statement()
    data = synth.ofx_investment(stmt).replace(
        b"</INVSTMTMSGSRSV1>",
        b"</INVSTMTMSGSRSV1><BANKMSGSRSV1><STMTTRNRS><STMTRS><CURDEF>USD</CURDEF>"
        b"<BANKACCTFROM><ACCTID>000099991234</ACCTID><ACCTTYPE>CHECKING</ACCTTYPE>"
        b"</BANKACCTFROM><BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT</TRNTYPE>"
        b"<DTPOSTED>20260105</DTPOSTED><TRNAMT>-10.00</TRNAMT><FITID>B1</FITID>"
        b"<NAME>Example Cafe</NAME></STMTTRN></BANKTRANLIST></STMTRS></STMTTRNRS>"
        b"</BANKMSGSRSV1>",
    )
    result = extract_ofx(data, OPTIONS, "ofx")
    assert len(result.statements) == 1
    assert len(result.investments) == 1


SPLIT_OFX = (
    b"OFXHEADER:100\nDATA:OFXSGML\n\n<OFX><INVSTMTMSGSRSV1><INVSTMTTRNRS><INVSTMTRS>"
    b"<CURDEF>USD<INVACCTFROM><BROKERID>x.example<ACCTID>99887766</INVACCTFROM>"
    b"<INVTRANLIST><DTSTART>20260101<DTEND>20260131"
    b"<SPLIT><INVTRAN><FITID>S1<DTTRADE>20260110</INVTRAN>"
    b"<SECID><UNIQUEID>ZZ0000B25<UNIQUEIDTYPE>CUSIP</SECID>"
    b"<OLDUNITS>100<NEWUNITS>300<NUMERATOR>3<DENOMINATOR>1</SPLIT>"
    b"<TRANSFER><INVTRAN><FITID>T1<DTTRADE>20260112</INVTRAN>"
    b"<SECID><UNIQUEID>ZZ0000C33<UNIQUEIDTYPE>CUSIP</SECID>"
    b"<UNITS>5<TFERACTION>OUT<POSTYPE>LONG</TRANSFER>"
    b"<MARGININTEREST><INVTRAN><FITID>M1<DTTRADE>20260113</INVTRAN></MARGININTEREST>"
    b"</INVTRANLIST><INVPOSLIST><POSSTOCK><INVPOS>"
    b"<SECID><UNIQUEID>ZZ0000B25<UNIQUEIDTYPE>CUSIP</SECID>"
    b"<POSTYPE>SHORT<UNITS>20<UNITPRICE>10.5<MKTVAL>-210.00<DTPRICEASOF>20260131"
    b"</INVPOS></POSSTOCK></INVPOSLIST></INVSTMTRS></INVSTMTTRNRS></INVSTMTMSGSRSV1></OFX>"
)


def test_ofx_split_transfer_short_position_and_unknown_kinds() -> None:
    result = extract_ofx(SPLIT_OFX, OPTIONS, "ofx")
    statement = _only(result)
    split, transfer = statement.activity
    assert split.type == "split"
    assert split.split_ratio == (Decimal(300), Decimal(100))
    assert split.quantity is None
    # No SECLIST: the CUSIP is the instrument's only name.
    assert split.security == SecurityRef(cusip="ZZ0000B25", asset_class="other")
    assert transfer.type == "transfer_out"
    assert transfer.quantity == 5
    assert transfer.amount == 0
    (short,) = statement.positions
    assert short.quantity == -20
    assert short.security.asset_class == "equity"
    # The margin interest is not read: its kind is named and the record counted.
    assert result.warnings == ["investment_activity_kind_unsupported", "transaction_skipped:1"]
    assert statement.as_of == date(2026, 1, 31)  # DTEND when the list has no DTASOF


def test_ofx_without_any_statement_is_still_refused() -> None:
    with pytest.raises(ExtractFailure) as caught:
        extract_ofx(
            b"OFXHEADER:100\n\n<OFX><INVSTMTMSGSRSV1></INVSTMTMSGSRSV1></OFX>", OPTIONS, "ofx"
        )
    assert caught.value.code == "no_statements"


def test_worker_only_investment_fields_are_refused_from_the_child() -> None:
    stmt = synth.brokerage_statement()
    plain = extract_ofx(synth.ofx_investment(stmt), OPTIONS, "ofx")
    assert not plain.worker_only_fields()
    statement = plain.investments[0]
    forged = [
        statement.model_copy(update={"source_format": "pdf"}),
        # An activity table's statements are the worker's own (brokerage_table).
        statement.model_copy(update={"source_format": "csv"}),
        statement.model_copy(update={"reconciliation": reconcile_investment(statement)}),
        statement.model_copy(update={"confidence": {"period": 1.0}}),
        statement.model_copy(
            update={"activity": [statement.activity[0].model_copy(update={"confidence": 1.0})]}
        ),
        statement.model_copy(
            update={
                "positions": [
                    statement.positions[0].model_copy(update={"low_confidence": ["quantity"]})
                ]
            }
        ),
    ]
    for bad in forged:
        assert plain.model_copy(update={"investments": [bad]}).worker_only_fields()


# ---------------------------------------------------------------------------
# Reconciliation and readers
# ---------------------------------------------------------------------------


def _position(quantity: str, price: str, value: str) -> InvestmentPosition:
    return InvestmentPosition(
        security=SecurityRef(symbol="PBXA"),
        quantity=Decimal(quantity),
        unit_price=Decimal(price),
        market_value=Decimal(value),
    )


def test_position_tolerance_follows_the_printed_price_precision() -> None:
    # 1000 x 41.2345 = 41234.50; printed 41.23 (2 decimals) may hide 5 per 1000 units.
    loose = InvestmentStatement(
        source_format="ofx", positions=[_position("1000", "41.23", "41234.50")]
    )
    assert reconcile_investment(loose).status == "ok"
    tight = InvestmentStatement(
        source_format="ofx", positions=[_position("1000", "41.2300", "41234.50")]
    )
    check = reconcile_investment(tight)
    assert check.status == "mismatch"
    assert check.position_mismatches == [0]


def test_nothing_to_check_is_unavailable() -> None:
    statement = InvestmentStatement(
        source_format="ofx",
        activity=[
            InvestmentActivity(type="fee", trade_date=date(2026, 1, 3), amount=Decimal("-5.00"))
        ],
    )
    check = reconcile_investment(statement)
    assert check.status == "unavailable"
    assert check.checks == []


def test_cash_and_total_checks() -> None:
    statement = InvestmentStatement(
        source_format="pdf",
        positions=[_position("10", "5.00", "50.00")],
        activity=[
            InvestmentActivity(type="deposit", trade_date=date(2026, 1, 3), amount=Decimal(100)),
        ],
        cash_opening=Balance(amount=Decimal(20)),
        cash_closing=Balance(amount=Decimal(120)),
        positions_value=Balance(amount=Decimal(50)),
        total_value=Balance(amount=Decimal(170)),
    )
    assert reconcile_investment(statement).status == "ok"
    off = statement.model_copy(update={"cash_closing": Balance(amount=Decimal(121))})
    check = reconcile_investment(off)
    assert check.failed == ["cash", "total_value"]


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("1,234.5678", Decimal("1234.5678")),
        ("$41.20", Decimal("41.20")),
        ("37", Decimal(37)),
        ("(2.5)", Decimal("-2.5")),
        ("-0.125", Decimal("-0.125")),
        ("12/31", None),
        ("abc", None),
        ("(2.5", None),
    ],
)
def test_read_number(text: str, value: Decimal | None) -> None:
    assert read_number(text) == value


def test_activity_words_and_split_ratios() -> None:
    assert activity_type("Buy") == "buy"
    assert activity_type("Dividend Reinvestment") == "reinvest"
    assert activity_type("Transfer in") == "transfer_in"
    assert activity_type("Qualified dividend") == "dividend"
    assert activity_type("Journal") is None
    assert split_ratio("PBXB 3-for-1 stock split") == (Decimal(3), Decimal(1))
    assert split_ratio("reverse split 1:10") == (Decimal(1), Decimal(10))
    assert split_ratio("split") is None


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Pr Yr Cash Div", "dividend"),
        ("Special Non Qual Div", "dividend"),
        ("Non-Qualified Div", "dividend"),
        ("Qual Div Reinvest", "dividend"),
        ("MUNI EXEMPT INT EXAMPLE CITY BONDS", "interest"),
        ("Long Term Cap Gain", "dividend"),
        ("ADR Mgmt Fee", "fee"),
        ("Service Fee", "fee"),
        ("NRA Withholding", "fee"),
        ("Bank Interest", "interest"),
        ("Payment in Lieu", "dividend"),
        ("Spin-off", "corporate_action"),
    ],
)
def test_abbreviated_actions_are_expanded(text: str, kind: str) -> None:
    assert activity_type(text) == kind


@pytest.mark.parametrize(
    "text",
    [
        "MoneyLink Transfer",
        "MoneyLink Deposit",
        "Wire Sent",
        "Wire Received",
        "Internal Transfer",
        "Funds Received",
    ],
)
def test_cash_movements_take_their_direction_from_the_amount(text: str) -> None:
    assert activity_type(text, cash_sign=1) == "deposit"
    assert activity_type(text, cash_sign=-1) == "withdrawal"
    assert activity_type(text, cash_sign=0) is None


def test_security_transfer_moves_units_by_sign() -> None:
    assert activity_type("Security Transfer", moves_security=True, units_sign=-1) == "transfer_out"
    assert activity_type("Security Transfer", moves_security=True, units_sign=1) == "transfer_in"


def test_expansion_leaves_known_phrases_alone() -> None:
    for key, kind in ACTIVITY_TYPES.items():
        assert activity_type(key) == kind, key
    assert activity_type("REINVESTMENT EXAMPLE FUND (EXMPL) (Cash)") == "reinvest"
    assert expand_action("interest earned") == "interest earned"


@pytest.mark.parametrize(
    "text",
    [
        # A fund's name standing in the action column names no action.
        "Int'l Example Equity Fund",
        "Example Mid Cap Fund",
        # Not income: a distribution that is no dividend is not typed as one.
        "Non Dividend Distribution",
        # A qualifier with no dividend after it drops nothing.
        "Prior Year Adjustment",
        "Pr Example Holdings",
        "Special Example Fund",
    ],
)
def test_expansion_types_nothing_it_cannot_name(text: str) -> None:
    assert activity_type(text) is None
