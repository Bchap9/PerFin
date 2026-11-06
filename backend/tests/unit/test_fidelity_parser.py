# SPDX-License-Identifier: AGPL-3.0-or-later
"""Multi-account brokerage PDFs and the Fidelity Investments layout.

Statements come from ``tests/fixtures/synth_fidelity.py``: three invented
accounts (a brokerage account, a Roth IRA, an HSA) in one synthetic PDF in
the layout of a Fidelity "Investment Report", nothing from a real
statement. PDFs are extracted in process (``extract_pdf``), classified by
the shipped fingerprints and parsed with the classifier's hint, as the
worker does. Also: the generic parser on the same accounts under an
invented broker's name, the readers the layout needs (account headings,
header cells over several lines, CUSIPs, the action words) and the
negative cases (a tampered account, a bank statement naming the broker).
"""

from __future__ import annotations

import io
import uuid
from datetime import date
from decimal import Decimal

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.domain.documents.models import Document
from app.ingestion.classify.service import classify_extraction
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.accounts import account_heading
from app.ingestion.parsers.base import DocumentView
from app.ingestion.parsers.brokerage import (
    ACTIVITY_HEADERS,
    HOLDING_HEADERS,
    FidelityStatementParser,
    activity_type,
    header_key,
    is_cusip,
)
from app.ingestion.parsers.registry import parse_extraction, reparse
from app.ingestion.pipeline import _unlink_classified_account
from app.ingestion.sandbox.child import landlock_abi
from app.ingestion.sandbox.protocol import (
    ExtractOptions,
    ExtractResult,
    InvestmentActivity,
    InvestmentPosition,
    InvestmentStatement,
)
from app.ingestion.sandbox.runner import SandboxConfig, run_extraction
from tests.fixtures import synth_fidelity as fid

OPTIONS = ExtractOptions(previews=False, ocr=False)
Row = tuple[date, str, str | None, Decimal | None, Decimal]
CHECKS = ["positions", "holdings_total", "cash", "total_value"]
PdfCell = tuple[float, str]


def _extract(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return result


def _parse(data: bytes, *, hinted: bool = True) -> ExtractResult:
    result = _extract(data)
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


def _activity(statement: InvestmentStatement) -> list[Row]:
    rows = [
        (a.trade_date, a.type, a.security.symbol if a.security else None, a.quantity, a.amount)
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


def _assert_exact(statement: InvestmentStatement, account: fid.FidAccount) -> None:
    assert statement.account.last4 == account.last4
    assert _positions(statement) == fid.expected_positions(account)
    assert _activity(statement) == fid.expected_activity(account)
    assert statement.cash_opening is not None
    assert statement.cash_opening.amount == account.core_opening
    assert statement.cash_closing is not None
    assert statement.cash_closing.amount == account.core_closing
    assert statement.positions_value is not None
    assert statement.positions_value.amount == account.holdings_value
    assert statement.total_value is not None
    assert statement.total_value.amount == account.total_value


# ---------------------------------------------------------------------------
# The Fidelity layout
# ---------------------------------------------------------------------------


def test_three_accounts_are_three_reconciled_statements() -> None:
    stmt = fid.fidelity_statement()
    data = fid.fidelity_pdf(stmt)
    found = classify_extraction(_extract(data))
    assert found.fingerprint is not None
    assert found.fingerprint.key == fid.INSTITUTION_KEY
    assert found.institution == fid.INSTITUTION
    # Several account numbers: the classifier names none of them.
    assert found.last4.value is None

    result = _parse(data)
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "fidelity_investments_statement"
    assert result.parse.parser.institution == fid.INSTITUTION_KEY
    assert result.parse.warnings == []
    assert result.parse.skipped_lines == 0
    assert result.statements == []
    assert len(result.investments) == 3
    for statement, account in zip(result.investments, stmt.accounts, strict=True):
        _assert_exact(statement, account)
        assert statement.source_format == "pdf"
        assert statement.account.institution == fid.INSTITUTION
        assert statement.account.currency == "USD"
        assert (statement.period_start, statement.period_end) == (stmt.start, stmt.end)
        assert statement.as_of == stmt.end
        check = statement.reconciliation
        assert check is not None
        assert check.status == "ok", check
        assert check.checks == CHECKS
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
            assert row.page >= 2
        assert statement.parser is not None
    # Only the last four digits leave the parser (SECURITY.md E1).
    dumped = "".join(i.model_dump_json() for i in result.investments)
    for account in stmt.accounts:
        assert account.number not in dumped
        assert account.number.replace("-", "") not in dumped


def test_layout_details_are_read() -> None:
    stmt = fid.fidelity_statement()
    result = _parse(fid.fidelity_pdf(stmt))
    brokerage, roth, hsa = result.investments
    by_symbol = {p.security.symbol: p.security for p in brokerage.positions}
    # The asset-class group heading types each position; the wrapped name is joined.
    assert by_symbol["ZZFTX"].asset_class == "mutual_fund"
    assert by_symbol["ZZFTX"].name == "SAMPLE TOTAL MARKET INDEX FUND"
    assert by_symbol["ZZETF"].asset_class == "etf"
    assert by_symbol["ZZIN"].asset_class == "equity"
    # The core money market is cash, not a position.
    assert fid.CORE.symbol not in by_symbol
    # Cost basis from "Total Cost Basis" (two-line header).
    costs = {p.security.symbol: p.cost_basis for p in brokerage.positions}
    assert costs == {p.symbol: p.cost for p in stmt.accounts[0].positions}
    # Action words: the description column is the action, the security name the memo.
    buy = next(a for a in brokerage.activity if a.type == "buy")
    assert buy.memo is not None
    assert buy.memo.startswith("SAMPLE TOTAL MARKET")
    deposit = next(a for a in brokerage.activity if a.type == "deposit")
    assert deposit.memo == "Electronic Funds Transfer Received"
    assert deposit.security is None
    # "Transferred From" is cash in the Roth (no security) and units in the HSA.
    assert [a.type for a in roth.activity if a.memo == "Transferred From"] == ["deposit"]
    (moved,) = [a for a in hsa.activity if a.type == "transfer_in"]
    assert moved.security is not None
    assert moved.security.symbol == "ZZBDX"
    assert moved.quantity == Decimal("3.000")
    assert moved.amount == 0
    fee = next(a for a in roth.activity if a.type == "fee")
    assert fee.memo == "Fee Charged"
    assert fee.amount == Decimal("-25.00")


def test_detected_without_the_classifiers_hint() -> None:
    stmt = fid.fidelity_statement(seed=82)
    result = _parse(fid.fidelity_pdf(stmt), hinted=False)
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "fidelity_investments_statement"
    scores = {s.name: s.score for s in result.parse.scores}
    assert scores["fidelity_investments_statement"] > scores["brokerage_statement_generic"]
    assert scores["placeholder_brokerage"] == 0
    for statement, account in zip(result.investments, stmt.accounts, strict=True):
        _assert_exact(statement, account)


def test_tables_broken_over_pages_with_the_header_repeated() -> None:
    stmt = fid.fidelity_statement(seed=83)
    data = fid.fidelity_pdf(stmt, page_bottom=470)
    result = _parse(data)
    assert len(_extract(data).pages) > 10
    assert len(result.investments) == 3
    for statement, account in zip(result.investments, stmt.accounts, strict=True):
        _assert_exact(statement, account)
        assert statement.reconciliation is not None
        assert statement.reconciliation.status == "ok"
        assert len({p.page for p in statement.positions} | {a.page for a in statement.activity}) > 1


def test_reparse_keeps_every_account() -> None:
    stmt = fid.fidelity_statement()
    first = _parse(fid.fidelity_pdf(stmt))
    again = reparse(first)
    assert again.parse is not None
    assert again.parse.parser is not None
    assert again.parse.parser.name == "fidelity_investments_statement"
    assert [_activity(s) for s in again.investments] == [_activity(s) for s in first.investments]


def test_a_single_account_report_is_one_statement() -> None:
    stmt = fid.fidelity_statement(seed=84)
    single = fid.FidStatement(stmt.start, stmt.end, stmt.accounts[:1])
    result = _parse(fid.fidelity_pdf(single))
    (statement,) = result.investments
    # The letter-prefixed number is read from its heading.
    _assert_exact(statement, single.accounts[0])
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


@pytest.mark.parametrize(
    ("tamper", "failed"),
    [
        ("total", ["total_value"]),
        ("market_value", ["positions", "total_value"]),
        ("extra_row", ["cash"]),
    ],
)
@pytest.mark.parametrize("tampered", [0, 1, 2])
def test_a_tampered_account_alone_does_not_reconcile(
    tamper: fid.Tamper, failed: list[str], tampered: int
) -> None:
    stmt = fid.fidelity_statement()
    result = _parse(fid.fidelity_pdf(stmt, tamper=tamper, tamper_account=tampered))
    assert len(result.investments) == 3
    for index, statement in enumerate(result.investments):
        check = statement.reconciliation
        assert check is not None
        assert statement.account.last4 == stmt.accounts[index].last4
        if index != tampered:
            assert check.status == "ok"
            assert all(a.confidence == 1.0 for a in statement.activity)
            continue
        assert check.status == "mismatch"
        assert check.failed == failed
        if tamper == "extra_row":
            assert check.cash_difference == Decimal("-777.77")
        rows: list[InvestmentActivity | InvestmentPosition] = [
            *statement.activity,
            *statement.positions,
        ]
        for row in rows:
            assert "reconciliation" in row.low_confidence


@pytest.mark.parametrize("ruled", [False, True], ids=["plain", "ruled"])
def test_the_sandboxed_extractor_reads_the_report(ruled: bool) -> None:
    """The real child process (seccomp, rlimits, Landlock where the kernel has it).

    The ruled variant makes PyMuPDF's table finder run inside the sandbox.
    """
    stmt = fid.fidelity_statement(seed=86)
    data = fid.fidelity_pdf(stmt, ruled=ruled)
    config = SandboxConfig(require_landlock=landlock_abi() > 0)
    result = run_extraction("pdf", data, config=config, options=ExtractOptions())
    assert result.ok, result.error
    assert not result.worker_only_fields()
    assert result.investments == []  # brokerage PDFs are read in the worker, not the child
    if ruled:
        assert any(t.source == "rulings" for p in result.pages for t in p.tables)
    parsed = parse_extraction(result, threshold=0.85, institution=fid.INSTITUTION_KEY)
    assert len(parsed.investments) == 3
    for statement, account in zip(parsed.investments, stmt.accounts, strict=True):
        _assert_exact(statement, account)
        assert statement.reconciliation is not None
        assert statement.reconciliation.status == "ok"


# ---------------------------------------------------------------------------
# The generic parser on several accounts
# ---------------------------------------------------------------------------


def test_generic_parser_splits_an_unknown_brokers_accounts() -> None:
    stmt = fid.fidelity_statement(seed=85)
    result = _parse(fid.fidelity_pdf(stmt, generic=True), hinted=False)
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "brokerage_statement_generic"
    assert len(result.investments) == 3
    for statement, account in zip(result.investments, stmt.accounts, strict=True):
        assert statement.account.last4 == account.last4
        assert statement.account.institution is None
        assert (statement.period_start, statement.period_end) == (stmt.start, stmt.end)
        # Without the layout's knowledge the core money market is a position.
        core = (
            fid.CORE.symbol,
            account.core_closing,
            Decimal("1.0000"),
            account.core_closing,
        )
        assert _positions(statement) == sorted([*fid.expected_positions(account), core])
        # Rows of the tables it can read (those naming a security).
        read = [
            (a.day, a.type, a.symbol, a.quantity, a.amount)
            for a in account.activity
            if a.table in ("trades", "income", "other_in")
        ]
        assert _activity(statement) == sorted(read, key=lambda r: (r[0], r[1], r[2] or "", r[4]))
        check = statement.reconciliation
        assert check is not None
        assert check.status == "ok"
        assert check.checks == ["positions", "holdings_total"]


def test_rows_before_any_account_and_accounts_without_tables_are_reported() -> None:
    """The generic parser: an overview page with a table, an account with none."""
    header: list[PdfCell] = [
        (72, "Symbol"),
        (140, "Quantity"),
        (260, "Price"),
        (380, "Market Value"),
    ]

    def table(symbol: str, quantity: str, price: str, value: str) -> list[list[PdfCell]]:
        row: list[PdfCell] = [(72, symbol), (140, quantity), (260, price), (380, value)]
        return [header, row, [(72, "Total holdings"), (380, value)]]

    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)

    def draw(lines: list[list[PdfCell]]) -> None:
        y = 750.0
        c.setFont("Helvetica", 9)
        for line in lines:
            for x, text in line:
                c.drawString(x, y, text)
            y -= 14
        c.showPage()

    period: list[PdfCell] = [(72, "Statement period: 10/01/2026 - 10/31/2026")]
    draw([period, *table("ZZAA", "10", "5.00", "50.00")])  # before any account
    draw([[(72, "Account # 111-111111")], period, *table("ZZBB", "4", "2.50", "10.00")])
    draw([[(72, "Account # 222-222222")], period, [(72, "No positions this period")]])
    draw([[(72, "Account # 333-333333")], period, *table("ZZCC", "3", "3.00", "9.00")])
    c.save()
    result = parse_extraction(_extract(buf.getvalue()), threshold=0.85)
    assert [i.account.last4 for i in result.investments] == ["1111", "3333"]
    assert [p.security.symbol for i in result.investments for p in i.positions] == [
        "ZZBB",
        "ZZCC",
    ]
    assert result.parse is not None
    assert "rows_outside_account" in result.parse.warnings
    assert "account_without_tables" in result.parse.warnings
    assert result.parse.skipped_lines == 1
    for statement in result.investments:
        assert statement.reconciliation is not None
        assert statement.reconciliation.status == "ok"
        assert (statement.period_start, statement.period_end) == (
            date(2026, 10, 1),
            date(2026, 10, 31),
        )


def test_bank_statement_naming_the_broker_is_not_a_fidelity_report() -> None:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 750, "Sample National Bank")
    c.setFont("Helvetica", 9)
    c.drawString(72, 734, "Checking Account Statement")
    c.drawString(72, 718, "Statement period: 10/01/2026 - 10/31/2026")
    c.drawString(72, 690, "Date")
    c.drawString(150, 690, "Description")
    c.drawRightString(540, 690, "Amount")
    c.drawString(72, 676, "10/03/2026")
    c.drawString(150, 676, "Fidelity Investments transfer")
    c.drawRightString(540, 676, "-100.00")
    c.showPage()
    c.save()
    view = DocumentView.from_result(_extract(buf.getvalue()))
    assert FidelityStatementParser().detect(view) == 0.0


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "last4"),
    [
        ("Account # Z29-104821", "4821"),
        ("INDIVIDUAL - TOD Account # Z29-104821", "4821"),
        ("Account Number: 245-718293", "8293"),
        ("Account No. 246 556074", "6074"),
        ("Account XXXX-4821", "4821"),
        ("INDIVIDUAL - TOD Z12-345678", "5678"),
        ("HEALTH SAVINGS ACCOUNT 246-556074", "6074"),
        # Not headings: overview rows with amounts, words without a number.
        ("INDIVIDUAL - TOD Z29-104821 $1,000.00 $1,100.00", None),
        ("Account Summary", None),
        ("Account Type Account Number Beginning Value Ending Value", None),
        ("Ending Account Value $107,816.05", None),
        ("Mixed Case Title 245-718293", None),
        ("Account 1099-B", None),
        ("Linked bank account XXXX-9999", None),
        ("Sweep to account 245-718293", None),
    ],
)
def test_account_headings(text: str, last4: str | None) -> None:
    assert account_heading(text) == last4


def test_header_cells_over_lines_and_with_dates() -> None:
    assert header_key(("Ending", "Market Value", "Oct 31, 2026"), HOLDING_HEADERS) == "value"
    assert header_key(("Ending Market Value Oct 31, 2026",), HOLDING_HEADERS) == "value"
    assert header_key(("Market Value as of 10/31/2026",), HOLDING_HEADERS) == "value"
    assert header_key(("Price", "Per Unit"), HOLDING_HEADERS) == "price"
    assert header_key(("Beginning", "Market Value"), HOLDING_HEADERS) == "begin"
    assert header_key(("EAI ($) /", "EY (%)"), HOLDING_HEADERS) == "other"
    # A line below that is no part of the header is left off.
    assert header_key(("Description", "Mutual Funds"), HOLDING_HEADERS) == "name"
    assert header_key(("Symbol/", "CUSIP"), ACTIVITY_HEADERS) == "symbol"
    assert header_key(("Settlement", "Date"), ACTIVITY_HEADERS) == "settle"
    assert header_key(("Transaction", "Cost"), ACTIVITY_HEADERS) == "fees"
    assert header_key(("Security Name",), ACTIVITY_HEADERS) == "name"
    assert header_key(("Unrelated",), ACTIVITY_HEADERS) is None


def _with_check_digit(base: str) -> str:
    total = 0
    for index, char in enumerate(base):
        value = int(char) if char.isdigit() else ord(char) - ord("A") + 10
        if index % 2:
            value *= 2
        total += value // 10 + value % 10
    return f"{base}{(10 - total % 10) % 10}"


def test_cusip_check_digit() -> None:
    valid = _with_check_digit("ZZ0000A1")
    assert is_cusip(valid)
    wrong = valid[:8] + str((int(valid[8]) + 1) % 10)
    assert not is_cusip(wrong)
    assert not is_cusip("ZZFTX")
    assert not is_cusip("ABCDEFGH1")  # no digit among the first eight


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("You Bought", "buy"),
        ("YOU BOUGHT PROSPECTUS UNDER SEPARATE COVER", "buy"),
        ("You Sold", "sell"),
        ("Dividend Received", "dividend"),
        ("Reinvestment", "reinvest"),
        ("REINVESTMENT SAMPLE TOTAL MARKET INDEX FUND (ZZFTX) (Cash)", "reinvest"),
        ("Interest Earned", "interest"),
        ("LONG-TERM CAP GAIN EXAMPLE FUND (ZZGRX) (Margin)", "dividend"),
        ("Short-Term Cap Gain", "dividend"),
        ("Direct Deposit", "deposit"),
        ("Electronic Funds Transfer Received (Cash)", "deposit"),
        ("ELECTRONIC FUNDS TRANSFER PAID", "withdrawal"),
        ("Foreign Tax Paid", "fee"),
        ("Fee Charged", "fee"),
        ("Transferred From", "deposit"),
        ("TRANSFERRED TO VS 245-718293", "withdrawal"),
        ("Journaled", None),
    ],
)
def test_action_words(text: str, kind: str | None) -> None:
    assert activity_type(text) == kind


def test_transfers_of_units_are_transfers_in_and_out() -> None:
    assert activity_type("Transferred From", moves_security=True) == "transfer_in"
    assert activity_type("TRANSFERRED TO (Cash)", moves_security=True) == "transfer_out"
    # The existing words keep their meaning whatever moved.
    assert activity_type("Transfer in", moves_security=False) == "transfer_in"


# ---------------------------------------------------------------------------
# The pipeline's account link
# ---------------------------------------------------------------------------


def test_a_classifier_account_link_is_undone_an_uploaders_is_kept() -> None:
    account = uuid.uuid4()
    classified = Document(account_id=account, confidence={"fields": {"account": 0.81, "kind": 1}})
    _unlink_classified_account(classified)
    assert classified.account_id is None
    assert classified.confidence == {"fields": {"kind": 1}}
    chosen = Document(account_id=account, confidence={"fields": {"kind": 1}})
    _unlink_classified_account(chosen)
    assert chosen.account_id == account
