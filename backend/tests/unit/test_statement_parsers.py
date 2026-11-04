# SPDX-License-Identifier: AGPL-3.0-or-later
"""Document parsers: synthetic PDFs -> pages (extractor, in process) -> statements.

Every fictional institution of the shipped fingerprints has a PDF layout in
``tests/fixtures/synth_pdf_statements.py``; each is classified, then parsed
by its institution parser, and must give back exactly the rows, balances,
period, last four digits and currency it was drawn from, reconciled and with
every field at full confidence. Tampered statements must not reconcile, an
unknown layout with an ambiguous date order must come out low-confidence,
and a payslip must be read and checked.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.classify.service import classify_extraction
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers import confidence
from app.ingestion.parsers.registry import PARSERS, parse_extraction, reparse
from app.ingestion.parsers.text import (
    find_amounts,
    guess_numeric_style,
    infer_year,
    read_amount,
    read_date,
)
from app.ingestion.reconcile import TOLERANCE, Row, reconcile, running_mismatches
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult, Statement
from tests.fixtures import synth_pdf_statements as synth
from tests.fixtures.synth_statements import SynthStatement

OPTIONS = ExtractOptions(previews=False, ocr=False)
PENDING = (synth.PendingItem(date(2026, 1, 31), Decimal("-9.99"), "Pending Cafe"),)


def _extract(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return result


def _parse(data: bytes, *, hinted: bool = True, threshold: float = 0.85) -> ExtractResult:
    """Extract, classify (as the pipeline does) and parse with the classifier's hints."""
    result = _extract(data)
    if not hinted:
        return parse_extraction(result, threshold=threshold)
    found = classify_extraction(result)
    key = found.fingerprint.key if found.fingerprint else None
    order = found.fingerprint.spec.date_order if found.fingerprint else None
    return parse_extraction(
        result,
        threshold=threshold,
        institution=key,
        parser_name=found.parser,
        kind=found.kind,
        date_order=order,
    )


def _only(result: ExtractResult) -> Statement:
    assert len(result.statements) == 1
    return result.statements[0]


def _rows(statement: Statement) -> list[tuple[date, Decimal, str | None]]:
    return sorted((t.posted, t.amount, t.payee) for t in statement.transactions)


Layout = tuple[Callable[[], SynthStatement], Callable[[SynthStatement], bytes], str]
LAYOUTS: dict[str, Layout] = {
    "sample_national": (
        synth.sample_national_statement,
        lambda s: synth.sample_national_pdf(s, rows_per_page=8, pending=PENDING),
        "sample_national_checking",
    ),
    "example_fcu": (synth.example_fcu_statement, synth.example_fcu_pdf, "example_fcu_share"),
    "demo_card": (
        synth.demo_card_statement,
        lambda s: synth.demo_card_pdf(s, pending=PENDING, rows_per_page=12),
        "demo_card_services",
    ),
    "mock_savings": (synth.mock_savings_statement, synth.mock_savings_pdf, "mock_savings_and_loan"),
}


# ---------------------------------------------------------------------------
# Every institution layout
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("hinted", [True, False], ids=["classified", "detected"])
@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_institution_layouts_parse_exactly_and_reconcile(layout: str, hinted: bool) -> None:
    make, render, parser = LAYOUTS[layout]
    stmt = make()
    result = _parse(render(stmt), hinted=hinted)
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == parser
    assert result.parse.parser.institution == stmt.institution.key
    statement = _only(result)
    assert statement.source_format == "pdf"
    assert _rows(statement) == synth.expected(stmt)
    assert statement.account.last4 == stmt.last4
    assert statement.account.currency == stmt.currency
    assert statement.account.institution == stmt.institution.name
    assert (statement.period_start, statement.period_end) == (stmt.start, stmt.end)
    assert statement.opening_balance is not None
    assert statement.ledger_balance is not None
    assert statement.opening_balance.amount == stmt.opening_balance
    assert statement.ledger_balance.amount == stmt.closing_balance
    assert statement.ledger_balance.as_of == stmt.end
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.reconciliation.difference == 0
    assert all(value == 1.0 for value in statement.confidence.values()), statement.confidence
    assert all(t.confidence == 1.0 and not t.low_confidence for t in statement.transactions)
    assert all(t.page is not None and t.bbox is not None for t in statement.transactions)
    # The stored form validates again (the worker keeps it in document.extraction).
    assert ExtractResult.model_validate(result.model_dump(mode="json")) == result


def test_multi_page_statement_with_wrapped_lines_pending_items_fees_and_interest() -> None:
    stmt = synth.sample_national_statement()
    data = synth.sample_national_pdf(stmt, rows_per_page=6, pending=PENDING)
    result = _parse(data)
    assert len(result.pages) >= 4
    statement = _only(result)
    assert _rows(statement) == synth.expected(stmt)
    assert result.parse is not None
    # The pending item is counted, not imported, and not part of the balances.
    assert result.parse.pending_items == 1
    assert "Pending Cafe" not in {t.payee for t in statement.transactions}
    transfer = next(t for t in statement.transactions if t.payee == "Transfer to savings")
    assert transfer.memo is not None
    assert transfer.memo.startswith("Ref S")
    types = {t.payee: t.type for t in statement.transactions if t.type}
    assert types == {"Monthly service fee": "fee", "Interest paid": "int"}
    assert {t.page for t in statement.transactions} >= {1, 2, 3}


def test_card_statement_signs_sections_and_balance_owed() -> None:
    stmt = synth.demo_card_statement()
    result = _parse(synth.demo_card_pdf(stmt, pending=PENDING))
    statement = _only(result)
    assert statement.account.account_type == "creditcard"
    payment = next(t for t in statement.transactions if t.amount > 0)
    assert payment.payee == "Payment Thank You"
    assert all(t.amount < 0 for t in statement.transactions if t is not payment)
    # The printed new balance is an amount owed: negative in the ledger.
    assert statement.ledger_balance is not None
    assert statement.ledger_balance.amount < 0
    fee = next(t for t in statement.transactions if t.payee == "Late payment fee")
    assert (fee.type, fee.amount) == ("fee", Decimal("-25.00"))
    interest = next(t for t in statement.transactions if t.type == "int")
    assert interest.amount == Decimal("-8.31")
    assert result.parse is not None
    assert result.parse.pending_items == 1


def test_debit_and_credit_columns() -> None:
    stmt = synth.mock_savings_statement()
    statement = _only(_parse(synth.mock_savings_pdf(stmt)))
    assert any(t.amount > 0 for t in statement.transactions)
    assert any(t.amount < 0 for t in statement.transactions)
    assert _rows(statement) == synth.expected(stmt)


# ---------------------------------------------------------------------------
# Reconciliation (SECURITY.md C12)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_an_inserted_row_does_not_reconcile(layout: str) -> None:
    make, _, _ = LAYOUTS[layout]
    stmt = make()
    render = {
        "sample_national": synth.sample_national_pdf,
        "example_fcu": synth.example_fcu_pdf,
        "demo_card": synth.demo_card_pdf,
        "mock_savings": synth.mock_savings_pdf,
    }[layout]
    statement = _only(_parse(render(stmt, tamper="extra_row")))
    assert _rows(statement) == synth.expected(stmt, tamper="extra_row")
    check = statement.reconciliation
    assert check is not None
    assert check.status == "mismatch"
    assert check.difference == synth.FORGED.amount
    forged = next(i for i, t in enumerate(statement.transactions) if t.payee == "Forged Payee")
    if layout != "demo_card":  # card statements print no running balance
        assert check.running_mismatches == [forged]
        assert "amount" in statement.transactions[forged].low_confidence
    # Every row is held for review, not only the forged one.
    assert all("reconciliation" in t.low_confidence for t in statement.transactions)
    assert all((t.confidence or 1) <= confidence.UNRECONCILED for t in statement.transactions)
    assert statement.confidence["reconciliation"] == confidence.UNRECONCILED


def test_a_wrong_closing_balance_does_not_reconcile() -> None:
    stmt = synth.sample_national_statement()
    statement = _only(_parse(synth.sample_national_pdf(stmt, tamper="closing")))
    check = statement.reconciliation
    assert check is not None
    assert check.status == "mismatch"
    assert check.difference == Decimal("-100.00")
    assert check.running_mismatches == []
    assert all("reconciliation" in t.low_confidence for t in statement.transactions)


def test_reconcile_function() -> None:
    rows = [Row(Decimal("-10.00"), Decimal("90.00")), Row(Decimal("5.00"), Decimal("95.00"))]
    ok = reconcile(Decimal("100.00"), Decimal("95.00"), rows)
    assert (ok.status, ok.difference, ok.total) == ("ok", Decimal("0.00"), Decimal("-5.00"))
    off = reconcile(Decimal("100.00"), Decimal("95.01"), rows)
    assert off.status == "mismatch"
    assert off.difference == Decimal("-0.01")
    within = reconcile(Decimal("100.000"), Decimal("95.004"), [Row(Decimal("-5.00"))])
    assert within.status == "ok"
    assert within.difference is not None
    assert abs(within.difference) <= TOLERANCE
    unchecked = reconcile(None, None, [Row(Decimal("-5.00"))])
    assert unchecked.status == "unavailable"
    running_only = reconcile(None, None, rows)
    assert running_only.status == "ok"
    bad = [Row(Decimal("-10.00"), Decimal("90.00")), Row(Decimal("5.00"), Decimal("99.00"))]
    assert running_mismatches(Decimal("100.00"), bad) == [1]
    assert reconcile(Decimal("100.00"), Decimal("99.00"), bad).status == "mismatch"
    # A row without a printed balance carries the expected balance on.
    gap = [Row(Decimal("-10.00")), Row(Decimal("5.00"), Decimal("95.00"))]
    assert running_mismatches(Decimal("100.00"), gap) == []


# ---------------------------------------------------------------------------
# Unknown layouts and confidence
# ---------------------------------------------------------------------------


def test_unknown_layout_with_ambiguous_dates_is_low_confidence() -> None:
    stmt = synth.generic_statement()
    result = _parse(synth.generic_pdf(stmt))
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "statement_generic"
    assert "date_order_guessed" in result.parse.warnings
    statement = _only(result)
    assert statement.account.last4 == stmt.last4
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.transactions
    for txn in statement.transactions:
        assert txn.low_confidence == ["posted_date"]
        assert txn.confidence == confidence.GUESSED
    assert statement.confidence["period"] < 0.85


def test_unknown_layout_dates_resolved_by_values_or_a_fingerprint_hint() -> None:
    stmt = synth.generic_statement(days=28)  # days above 12: DD/MM is visible
    statement = _only(_parse(synth.generic_pdf(stmt)))
    assert _rows(statement) == synth.expected(stmt)
    assert all(not t.low_confidence for t in statement.transactions)
    assert all(t.confidence == confidence.GENERIC for t in statement.transactions)
    short = synth.generic_statement()
    hinted = parse_extraction(_extract(synth.generic_pdf(short)), date_order="dmy")
    assert _rows(_only(hinted)) == synth.expected(short)
    assert all(not t.low_confidence for t in _only(hinted).transactions)


def test_threshold_decides_what_is_low() -> None:
    data = synth.generic_pdf(synth.generic_statement(days=28))
    strict = _only(_parse(data, threshold=0.99))
    assert all(t.low_confidence for t in strict.transactions)
    relaxed = _only(_parse(data, threshold=0.85))
    assert not any(t.low_confidence for t in relaxed.transactions)


def test_reparse_runs_from_the_stored_pages() -> None:
    stmt = synth.example_fcu_statement()
    first = _parse(synth.example_fcu_pdf(stmt))
    stored = ExtractResult.model_validate(first.model_dump(mode="json"))
    again = reparse(stored)
    assert again.statements == first.statements
    assert again.parse is not None
    assert first.parse is not None
    assert again.parse.parser == first.parse.parser
    # Statements of an earlier run are replaced, never doubled.
    assert len(again.statements) == 1


def test_pages_without_a_table_give_no_statement() -> None:
    import io

    from reportlab.pdfgen import canvas

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    for index, line in enumerate(("Dear member,", "Your new card is on its way.", "Thanks")):
        c.drawString(72, 750 - 16 * index, line)
    c.save()
    result = parse_extraction(_extract(buf.getvalue()))
    assert result.parse is not None
    assert result.parse.parser is None
    assert "no_parser" in result.parse.warnings
    assert result.statements == []


def test_parser_names_are_unique_and_versioned() -> None:
    names = [p.name for p in PARSERS]
    assert len(names) == len(set(names))
    assert {"statement_generic", "card_statement_generic"} <= set(names)
    assert all(p.version for p in PARSERS)


def test_institution_parsers_cover_the_shipped_statement_fingerprints() -> None:
    from app.ingestion.classify.fingerprints import builtin_fingerprints

    keys = {p.institution for p in PARSERS if p.institution}
    names = {p.name for p in PARSERS}
    for fingerprint in builtin_fingerprints():
        # Brokerage statements (holdings, trades) get their parser in Phase 5.
        if fingerprint.spec.kind != "statement" or "brokerage" in fingerprint.spec.parser:
            continue
        assert fingerprint.spec.parser in names, fingerprint.key
        assert fingerprint.key in keys, fingerprint.key


# ---------------------------------------------------------------------------
# Payslip
# ---------------------------------------------------------------------------


def test_payslip_is_read_and_checked() -> None:
    result = _parse(synth.payslip_pdf())
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "payslip_basic"
    assert result.statements == []
    payslip = result.parse.payslip
    assert payslip is not None
    assert payslip.employer == "Initech Example Ltd"
    assert (payslip.period_start, payslip.period_end) == (date(2026, 1, 1), date(2026, 1, 31))
    assert payslip.pay_date == date(2026, 1, 30)
    assert (payslip.gross, payslip.net) == (Decimal("4250.00"), Decimal("2847.50"))
    assert payslip.total_deductions == Decimal("1402.50")
    assert [(d.label, d.amount) for d in payslip.deductions] == list(synth.DEDUCTIONS)
    assert [(e.label, e.amount) for e in payslip.earnings] == list(synth.EARNINGS)
    assert payslip.currency == "USD"
    assert payslip.adds_up is True
    assert payslip.low_confidence == []


def test_payslip_that_does_not_add_up_is_low_confidence() -> None:
    result = _parse(synth.payslip_pdf(net_override=Decimal("3847.50")))
    assert result.parse is not None
    payslip = result.parse.payslip
    assert payslip is not None
    assert payslip.adds_up is False
    assert {"gross", "net", "deductions"} <= set(payslip.low_confidence)


# ---------------------------------------------------------------------------
# Text helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "value", "credit"),
    [
        ("1,234.56", "1234.56", None),
        ("-1,234.56", "-1234.56", None),
        ("$1,234.56", "1234.56", None),
        ("-$12.00", "-12.00", None),
        ("+$25.00", "25.00", None),
        ("(12.00)", "-12.00", None),
        ("12.00-", "-12.00", None),
        ("500.00 CR", "500.00", True),
        ("500.00DR", "500.00", False),
        ("£3.10", "3.10", None),
    ],
)
def test_read_amount(text: str, value: str, credit: bool | None) -> None:
    amount = read_amount(text)
    assert amount is not None
    assert amount.value == Decimal(value)
    assert amount.credit is credit


@pytest.mark.parametrize("text", ["01/02/2026", "1234", "12.5", "(-1.00)", "1,23.00", "abc", ""])
def test_not_amounts(text: str) -> None:
    assert read_amount(text) is None


def test_find_amounts_and_long_input() -> None:
    assert [a.value for a in find_amounts("on 01/31/2026 $5,514.90 and 3.00 CR")] == [
        Decimal("5514.90"),
        Decimal("3.00"),
    ]
    # Bounded work on a hostile line.
    assert find_amounts("9" * 100_000) == []
    assert read_amount("1," * 50_000 + "00.00") is None


def test_dates() -> None:
    assert read_date("01/02/2026", "mdy") == date(2026, 1, 2)
    assert read_date("01/02/2026", "dmy") == date(2026, 2, 1)
    assert read_date("31/01/2026", "mdy") is None
    assert read_date("03 Jan 2026", "d_mon_y") == date(2026, 1, 3)
    assert read_date("Jan 3, 2026", "mon_d_y") == date(2026, 1, 3)
    assert read_date("2026-01-03", "ymd") == date(2026, 1, 3)
    window = (date(2025, 12, 15), date(2026, 1, 14))
    assert read_date("12/20", "md", window=window) == date(2025, 12, 20)
    assert read_date("01/05", "md", window=window) == date(2026, 1, 5)
    assert read_date("01/05", "md") is None
    assert infer_year(2, 30, window) is None
    assert guess_numeric_style(["13/01/2026", "02/01/2026"]) == ("dmy", True)
    assert guess_numeric_style(["01/13/2026"]) == ("mdy", True)
    assert guess_numeric_style(["01/02/2026"]) == ("mdy", False)
