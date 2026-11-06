# SPDX-License-Identifier: AGPL-3.0-or-later
"""Reading brokerage tables: vocabulary, sections, side-by-side tables, holdings rows.

The synthetic Fidelity-layout report (``tests/fixtures/synth_fidelity_report.py``)
is read to its last row and figure by the institution parser (found by the
PDF's metadata, or by its footer without it) and by the generic parser
alike; the readers it needs are tested on hand-built lines. Everything is
invented.
"""

from __future__ import annotations

import io
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError
from reportlab.pdfgen import canvas

from app.ingestion.classify.fingerprints import (
    compile_fingerprint,
    match_fingerprint,
    parse_fingerprint_yaml,
)
from app.ingestion.classify.service import classify_extraction
from app.ingestion.classify.text import DocumentView as ClassifyView
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine
from app.ingestion.parsers.brokerage import (
    BrokerageStatementParser,
    FidelityStatementParser,
    _Collected,
    _unmarked,
    activity_type,
    reconcile_investment,
    section_rule,
    split_header_cell,
)
from app.ingestion.parsers.loan_statement import LoanStatementParser
from app.ingestion.parsers.registry import PARSERS, _fallback, parse_extraction
from app.ingestion.parsers.text import normalise
from app.ingestion.sandbox.protocol import (
    Balance,
    ExtractOptions,
    ExtractResult,
    InvestmentActivity,
    InvestmentPosition,
    InvestmentStatement,
    SecurityRef,
)
from tests.fixtures import synth_fidelity_report as fidr
from tests.fixtures import synth_neutral_report as neutral
from tests.fixtures.synth_multi_account import cusip

OPTIONS = ExtractOptions(previews=False, ocr=False)
Row = tuple[date, str, str | None, Decimal | None, Decimal]


def _extract(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return result


def _hinted(result: ExtractResult) -> ExtractResult:
    """Parsed as the worker does: with the classifier's hint."""
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
        (
            a.trade_date,
            a.type,
            (a.security.symbol or a.security.cusip or a.security.name) if a.security else None,
            a.quantity,
            a.amount,
        )
        for a in statement.activity
    ]
    return sorted(rows, key=lambda r: (r[0], r[1], r[2] or "", r[4]))


def _positions(statement: InvestmentStatement) -> list[tuple[str, Decimal, Decimal, Decimal]]:
    out: list[tuple[str, Decimal, Decimal, Decimal]] = []
    for p in statement.positions:
        key = p.security.symbol or p.security.cusip or p.security.name
        assert key is not None
        assert p.unit_price is not None
        assert p.market_value is not None
        out.append((key, p.quantity, p.unit_price, p.market_value))
    return sorted(out)


def _assert_exact(statements: list[InvestmentStatement]) -> None:
    stmt, _ = fidr.report()
    assert len(statements) == 3
    for statement, expected in zip(statements, fidr.expected(stmt), strict=True):
        assert statement.account.last4 == expected.last4
        assert (statement.period_start, statement.period_end) == (
            expected.period_start,
            expected.period_end,
        )
        assert _positions(statement) == expected.positions
        assert _activity(statement) == expected.activity
        cash = (
            statement.cash_opening.amount if statement.cash_opening else None,
            statement.cash_closing.amount if statement.cash_closing else None,
        )
        assert cash == (expected.cash_opening, expected.cash_closing)
        assert statement.positions_value is not None
        assert statement.positions_value.amount == expected.positions_value
        assert statement.total_value is not None
        assert statement.total_value.amount == expected.total_value
        check = statement.reconciliation
        assert check is not None
        assert check.status == "ok", check
        if expected.cash_opening is not None:
            assert check.checks == ["positions", "holdings_total", "cash", "total_value"]


# ---------------------------------------------------------------------------
# The Fidelity-layout report, read to the last figure
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def report() -> ExtractResult:
    return _extract(fidr.report_pdf())


def test_the_report_is_read_exactly_by_the_layouts_parser(report: ExtractResult) -> None:
    result = _hinted(report)
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "fidelity_investments_statement"
    assert result.parse.warnings == []
    assert result.parse.skipped_lines == 0
    _assert_exact(result.investments)
    individual = result.investments[0]
    # A bond is priced per 100 of face value and keyed by the CUSIP printed under it.
    bond = next(p for p in individual.positions if p.security.cusip == cusip("94136RAC"))
    assert bond.price_per_100
    assert bond.security.symbol is None
    assert bond.security.asset_class == "bond"
    assert bond.price_per_unit == Decimal("0.98625")
    # "unknown" cost basis is unknown, a footnote marker is no part of the figure.
    by_symbol = {p.security.symbol: p for p in individual.positions}
    assert by_symbol["ZZMXX"].cost_basis is None
    assert by_symbol["ZZTRB"].cost_basis == Decimal("8412.36")
    assert by_symbol["ZZPGL"].unit_price == Decimal("147.85")
    # A name wrapped onto the next line keeps its (SYMBOL); no sweep among the positions.
    assert by_symbol["ZZSHR"].security.name == "SAMPLE HEALTH CARE REIT INC COM USD0.01"
    assert "ZZSWP" not in by_symbol
    ira = {p.security.symbol: p for p in result.investments[1].positions}
    assert ira["ZZSMC"].security.isin == "ZZ0000SMC010"
    assert "ZZCRX" not in ira
    # Holdings printed by their name alone are kept and named by it: the
    # partnership (its "A" mark dropped, its sentence-case note no part of
    # its name) and the 529's portfolios ("(Example Funds)" is no ticker).
    partnership = ira[None]
    assert (partnership.security.identified, partnership.security.name) == (False, fidr.UNLISTED)
    assert (partnership.unit_price, partnership.cost_basis) == (Decimal("187.500"), None)
    income = [a for a in result.investments[1].activity if a.amount == Decimal("37.50")]
    assert [a.security for a in income] == [partnership.security]
    assert [p.security.name for p in result.investments[2].positions] == [
        "Sample College Portfolio 2031 (Example Funds)",
        "Sample Moderate Growth Port (Example Funds)",
    ]
    assert not any(p.security.identified for p in result.investments[2].positions)


def test_the_report_is_detected_by_its_metadata_or_its_footer() -> None:
    stmt, extras = fidr.report()
    found = classify_extraction(_extract(fidr.report_pdf(stmt, extras)))
    assert found.fingerprint is not None
    assert found.fingerprint.key == "fidelity-investments-statement"
    # Without the metadata no page names "Fidelity Investments" (only a logo
    # would): the parser still knows its subsidiary in the footer beside the
    # layout's wording, the classifier no longer matches the layout.
    bare = _extract(fidr.report_pdf(stmt, extras, institution_metadata=False))
    assert classify_extraction(bare).fingerprint is None
    view = DocumentView.from_result(bare)
    assert FidelityStatementParser().detect(view) == 1.0
    result = parse_extraction(bare, threshold=0.85)
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "fidelity_investments_statement"
    _assert_exact(result.investments)


def test_the_generic_parser_reads_the_report_too(report: ExtractResult) -> None:
    """No knowledge of the layout: the sweep is cash because the figures say so."""
    outcome = BrokerageStatementParser().parse(DocumentView.from_result(report), threshold=0.85)
    _assert_exact(outcome.investments)


def test_a_name_in_the_metadata_alone_needs_the_layouts_wording() -> None:
    """A document titled after the institution that is no report of its is not one."""
    lines = (_line(1, 0, [(40, "INVESTMENT REPORT")]), _line(1, 1, [(40, "Some other text")]))
    view = DocumentView(
        lines=lines,
        source_format="pdf",
        truncated=False,
        page_count=1,
        metadata=("fidelity investments",),
    )
    assert FidelityStatementParser().detect(view) == 0.0


def test_the_shipped_fingerprint_takes_the_name_from_the_metadata() -> None:
    spec = parse_fingerprint_yaml(
        "schema: 1\nkey: example-layout\ninstitution: Example Broker\nkind: statement\n"
        "parser: brokerage_statement_generic\nversion: 1\n"
        "match:\n  all:\n    - header: Investment Report\n"
        "  one_of:\n    - header: Example Broker\n    - metadata: Example Broker\n"
    )
    fingerprint = compile_fingerprint(spec)
    page = ClassifyView(lines=("INVESTMENT REPORT", "Holdings"), header=("INVESTMENT REPORT",))
    assert match_fingerprint(fingerprint, page) is None
    titled = ClassifyView(
        lines=page.lines, header=page.header, metadata=("report | example broker",)
    )
    assert match_fingerprint(fingerprint, titled) is not None
    named = ClassifyView(lines=page.lines, header=("Example Broker", "INVESTMENT REPORT"))
    assert match_fingerprint(fingerprint, named) is not None


# ---------------------------------------------------------------------------
# Registry fallback
# ---------------------------------------------------------------------------


def test_the_next_best_parser_reads_what_the_chosen_one_could_not() -> None:
    result = _extract(neutral.report_pdf())
    view = DocumentView.from_result(result)
    scores = {p.name: p.detect(view) for p in PARSERS}
    chosen = LoanStatementParser()
    empty = chosen.parse(view, threshold=0.85)
    assert empty.empty
    used, outcome = _fallback(view, scores, chosen, empty, 0.85, None)
    assert used.name == "brokerage_statement_generic"
    assert not outcome.empty
    assert outcome.warnings[0] == "parser_fallback:loan_statement_generic"


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _line(page: int, index: int, cells: list[tuple[float, str]]) -> ViewLine:
    built = tuple(Cell(text, x, 0.0, x + 5.0 * len(text), 9.0) for x, text in cells)
    return ViewLine(
        page=page, index=index, cells=built, text=" ".join(c.text for c in built), height=9.0
    )


def test_a_header_cell_of_several_headers_is_split() -> None:
    cell = Cell("Transaction Description Quantity", 100.0, 0.0, 260.0, 9.0)
    assert [c.text for c in split_header_cell(cell)] == ["Transaction Description", "Quantity"]
    merged = Cell("Check Num. Post Date Code Description", 40.0, 0.0, 225.0, 9.0)
    assert [c.text for c in split_header_cell(merged)] == [
        "Check Num.",
        "Post Date",
        "Code",
        "Description",
    ]
    known = Cell("Cost Basis", 0.0, 0.0, 50.0, 9.0)
    assert split_header_cell(known) == (known,)
    unknown = Cell("Weird Words Here", 0.0, 0.0, 80.0, 9.0)
    assert split_header_cell(unknown) == (unknown,)


def test_side_by_side_tables_are_two_tables() -> None:
    header: list[tuple[float, str]] = [
        (40, "Date"),
        (70, "Description"),
        (250, "Amount"),
        (312, "Date"),
        (342, "Description"),
        (520, "Amount"),
    ]
    rows = [
        _line(1, 0, [(40, "Deposits")]),
        _line(1, 1, header),
        _line(1, 2, [(40, "6/1"), (70, "Wire in"), (250, "$100.00")]),
        _line(1, 2, [(312, "6/9"), (342, "Wire out"), (520, "-40.00")]),
    ]
    joined = ViewLine(
        page=1,
        index=2,
        cells=rows[2].cells + rows[3].cells,
        text=f"{rows[2].text} {rows[3].text}",
        height=9.0,
    )
    total = _line(1, 3, [(40, "6/2"), (70, "Deposit received"), (250, "5.00")])
    total = ViewLine(
        page=1,
        index=3,
        cells=total.cells + _line(1, 3, [(342, "Total Deposits"), (520, "$105.00")]).cells,
        text=f"{total.text} Total Deposits $105.00",
        height=9.0,
    )
    parser = FidelityStatementParser()
    header_found = parser._header_at([rows[1]], 0)
    assert header_found is not None
    assert header_found.parts is not None
    assert len(header_found.parts) == 2
    collected = _Collected()
    parser._read([rows[0], rows[1], joined, total], collected)
    period = (date(2026, 6, 1), date(2026, 6, 30))
    activity, _ = parser._activities(collected, ("md",), True, period, {})
    # Each half's own date; the right half's total is no row; a row the
    # section types takes its direction from its sign.
    assert [(a.trade_date, a.type, a.amount) for a in activity] == [
        (date(2026, 6, 1), "deposit", Decimal("100.00")),
        (date(2026, 6, 9), "withdrawal", Decimal("-40.00")),
        (date(2026, 6, 2), "deposit", Decimal("5.00")),
    ]


@pytest.mark.parametrize(
    ("heading", "kind"),
    [
        ("Securities Bought & Sold", "plain"),
        ("Trades Pending Settlement", "skip"),
        ("Securities Transferred In", "transfer_in"),
        ("Securities Transferred Out", "transfer_out"),
        ("Dividends, Interest & Other Income", "income"),
        ("Core Account Cash Flow", "skip"),
        ("Daily Additions and Subtractions", "skip"),
        ("Deposits", "deposit"),
        ("Withdrawals", "withdrawal"),
        ("Checking Activity", "card"),
        ("Debit Card Activity", "card"),
        ("Bill Payments", "card"),
        ("Taxes Withheld", "fee"),
        ("Fees and Charges", "fee"),
        ("Income Summary", "skip"),
        ("Cash Activity", "cash"),
        ("Mutual Funds", None),
    ],
)
def test_activity_sections(heading: str, kind: str | None) -> None:
    assert section_rule(normalise(heading)) == kind


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Muni exempt interest", "interest"),
        ("Advisor Fee", "fee"),
        ("Federal tax paid", "fee"),
        ("State tax paid", "fee"),
        ("Trans. from account 418-330642", "deposit"),
        ("Trans. to account X81-004410", "withdrawal"),
        ("Transfer from Checking XXXX-4821", "deposit"),
    ],
)
def test_more_action_words(text: str, kind: str) -> None:
    assert activity_type(text) == kind


def test_wire_transfers_take_their_direction_from_the_amount() -> None:
    assert activity_type("Wire transfer from bank", cash_sign=1) == "deposit"
    assert activity_type("Wire transfer to bank", cash_sign=-1) == "withdrawal"


@pytest.mark.parametrize(
    ("text", "plain"),
    [
        ("147.85A", "147.85"),
        ("$8,412.36 c", "$8,412.36"),
        ("$6,630.82t", "$6,630.82"),
        ("$12,210.00 B", "$12,210.00"),
        ("125.00 CR", "125.00 CR"),
        ("unknown", "unknown"),
        ("N/A", "N/A"),
    ],
)
def test_footnote_markers_are_dropped_from_figures(text: str, plain: str) -> None:
    assert _unmarked(text) == plain


@pytest.mark.parametrize(
    ("raw", "symbol"),
    [
        ("APPLE INC (AAPL)", "AAPL"),
        ("SAMPLE CORP NT CV", None),
        ("EXAMPLE CITY INDL DEV AGY REV", None),
        ("IRA NOT COVERED BY SIPC (QPIKQ) q", "QPIKQ"),
        ("ZZ Portfolio 2030 (Example Funds)", None),
    ],
)
def test_a_holdings_symbol_is_only_the_ticker_in_parentheses(raw: str, symbol: str | None) -> None:
    assert BrokerageStatementParser._name_symbol(raw)[0] == symbol


def _statement(**fields: object) -> InvestmentStatement:
    return InvestmentStatement.model_validate({"source_format": "pdf", **fields})


def test_a_bond_priced_per_100_reconciles() -> None:
    bond = InvestmentPosition(
        security=SecurityRef(cusip=cusip("94136RAC")),
        quantity=Decimal("8000"),
        unit_price=Decimal("98.625"),
        market_value=Decimal("7890.00"),
    )
    assert reconcile_investment(_statement(positions=[bond])).status == "mismatch"
    per_100 = bond.model_copy(update={"price_per_100": True})
    assert reconcile_investment(_statement(positions=[per_100])).status == "ok"


def test_cash_without_a_closing_figure_is_the_account_value_less_the_holdings() -> None:
    position = InvestmentPosition(
        security=SecurityRef(symbol="ZZAA"), quantity=Decimal(10), market_value=Decimal("100.00")
    )
    deposit = InvestmentActivity(type="deposit", trade_date=date(2026, 1, 5), amount=Decimal(50))
    fields = {
        "positions": [position],
        "activity": [deposit],
        "cash_opening": Balance(amount=Decimal("25.00")),
        "total_value": Balance(amount=Decimal("175.00")),
    }
    check = reconcile_investment(_statement(**fields))
    assert check.status == "ok"
    assert "cash" in check.checks
    assert "total_value" not in check.checks  # the closing cash came from it
    assert check.cash_closing == Decimal("75.00")
    wrong = reconcile_investment(
        _statement(**{**fields, "total_value": Balance(amount=Decimal(170))})
    )
    assert wrong.status == "mismatch"
    assert wrong.failed == ["cash"]


# ---------------------------------------------------------------------------
# Hand-drawn one-page statements (generic parser)
# ---------------------------------------------------------------------------

Drawn = list[list[tuple[float, str]] | None]

_HOLDINGS = [(40.0, "Symbol"), (100.0, "Description"), (250.0, "Quantity"), (330.0, "Price")]
_CASH = [(30.0, "Date"), (70.0, "Description"), (220.0, "Amount")]


def _drawn_pdf(rows: Drawn, *more: Drawn) -> bytes:
    """One page per argument: each row's ``(x, text)`` cells on one line, None a gap."""
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(612, 792))
    for page in (rows, *more):
        y = 760.0
        for row in page:
            if row is None:
                y -= 14
                continue
            for x, text in row:
                pdf.setFont("Helvetica", 8)
                pdf.drawString(x, y, text)
            y -= 12
        pdf.showPage()
    pdf.save()
    return buf.getvalue()


def _generic(
    rows: Drawn,
    *more: Drawn,
    cash: tuple[str, str],
    value: str,
    running: list[tuple[float, str]] | None = None,
    cover: Drawn | None = None,
) -> InvestmentStatement:
    """A one-account statement: its summary, then ``rows``; ``more``: later pages.

    ``running``: a running page header printed at the top of every page;
    ``cover``: lines printed at the top of page 1 only.
    """
    top: Drawn = [running] if running is not None else []
    head: Drawn = [
        *(cover or []),
        *top,
        [(40, "Brokerage Account Statement")],
        [(40, "Statement Period: 03/01/2024 - 03/31/2024")],
        [(40, "Account Number: XXXX-1234")],
        [(40, "Beginning Cash Balance"), (420, cash[0])],
        [(40, "Ending Cash Balance"), (420, cash[1])],
        [(40, "Ending Account Value"), (420, value)],
        None,
    ]
    result = parse_extraction(
        _extract(_drawn_pdf([*head, *rows], *(top + page for page in more))),
        parser_name="brokerage_statement_generic",
        threshold=0.85,
    )
    assert len(result.investments) == 1
    return result.investments[0]


def _held(*rows: list[tuple[float, str]]) -> Drawn:
    return [
        [(40, "Holdings")],
        [*_HOLDINGS, (420, "Market Value")],
        *rows,
        [(40, "Total Holdings"), (420, "$1,000.00")],
        None,
    ]


_ZZAB: list[tuple[float, str]] = [
    (40, "ZZAB"),
    (100, "Example Index Fund"),
    (250, "10"),
    (330, "$100.00"),
    (420, "$1,000.00"),
]


def _rows(statement: InvestmentStatement) -> list[tuple[str, str | None, Decimal, str | None]]:
    return [
        (a.type, a.security.symbol if a.security else None, a.amount, a.memo)
        for a in statement.activity
    ]


def test_a_heading_no_section_names_ends_the_section_above() -> None:
    """A table under an unknown heading is not typed by the section above it."""
    income: Drawn = [
        [(30, "Dividends and Interest")],
        _CASH,
        [(30, "03/12"), (70, "Example Index Fund"), (220, "12.50")],
        None,
    ]
    later: Drawn = [
        [(30, "Miscellaneous Items")],
        _CASH,
        [(30, "03/20"), (70, "Sent to Example Bank"), (220, "-500.00")],
    ]
    statement = _generic(
        [*_held(_ZZAB), *income, *later], cash=("$5,000.00", "$4,512.50"), value="$5,512.50"
    )
    # The dividend names its security by its name; the -500 is no dividend.
    assert _rows(statement) == [
        ("dividend", "ZZAB", Decimal("12.50"), "Example Index Fund"),
    ]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "mismatch"
    # A heading the vocabulary knows types it by its sign.
    eft: Drawn = [[(30, "Electronic Funds Transfers")], *later[1:]]
    statement = _generic(
        [*_held(_ZZAB), *income, *eft], cash=("$5,000.00", "$4,512.50"), value="$5,512.50"
    )
    assert _rows(statement)[1] == (
        "withdrawal",
        None,
        Decimal("-500.00"),
        "Sent to Example Bank",
    )
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_rows_after_a_skipped_section_under_an_unknown_heading_are_read() -> None:
    pending: Drawn = [
        [(30, "Trades Pending Settlement")],
        _CASH,
        [(30, "03/29"), (70, "Withdrawal to bank"), (220, "-100.00")],
        None,
        [(30, "Other Items")],
        _CASH,
        [(30, "03/20"), (70, "Withdrawal to bank"), (220, "-500.00")],
    ]
    statement = _generic(
        [*_held(_ZZAB), *pending], cash=("$5,000.00", "$4,500.00"), value="$5,500.00"
    )
    assert _rows(statement) == [("withdrawal", None, Decimal("-500.00"), "Withdrawal to bank")]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_a_sub_heading_right_under_a_section_keeps_its_section() -> None:
    card: Drawn = [
        [(30, "Debit Card Activity")],
        [(30, "EXAMPLE CHECK CARD (ACCOUNT ...0000)")],
        _CASH,
        [(30, "03/20"), (70, "EXAMPLE MART"), (220, "-500.00")],
    ]
    statement = _generic([*_held(_ZZAB), *card], cash=("$5,000.00", "$4,500.00"), value="$5,500.00")
    assert _rows(statement) == [("withdrawal", None, Decimal("-500.00"), "EXAMPLE MART")]


#: Lines a statement prints at the top of every page (none names a section).
_RUNNING_HEADERS = [
    "Account Number: XXXX-1234",
    "Example Brokerage",
    "Jane Example",
    "Jane Example - Account XXXX-1234",
]

_TRADES = [
    (30.0, "Trade Date"),
    (90.0, "Description"),
    (220.0, "Symbol"),
    (280.0, "Quantity"),
    (340.0, "Price"),
    (420.0, "Amount"),
]


def _trade_row(action: str, symbol: str, amount: str) -> list[tuple[float, str]]:
    return [
        (30, "03/29/2024"),
        (90, action),
        (220, symbol),
        (280, "5"),
        (340, "100.00"),
        (420, amount),
    ]


@pytest.mark.parametrize("running", _RUNNING_HEADERS)
def test_a_skipped_section_continued_on_the_next_page_stays_skipped(running: str) -> None:
    """The section's header repeated under the running page header continues it."""
    pending: Drawn = [
        [(30, "Trades Pending Settlement")],
        _TRADES,
        _trade_row("Bought", "ZZAB", "-500.00"),
    ]
    page2: Drawn = [
        _TRADES,
        _trade_row("Bought", "ZZCD", "-500.00"),
        _trade_row("Sold", "ZZAB", "500.00"),
    ]
    statement = _generic(
        [*_held(_ZZAB), *pending],
        page2,
        cash=("$5,000.00", "$5,000.00"),
        value="$6,000.00",
        running=[(40, running)],
    )
    assert _rows(statement) == []
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


@pytest.mark.parametrize("running", _RUNNING_HEADERS)
def test_a_cash_section_continued_on_the_next_page_keeps_typing_its_rows(running: str) -> None:
    deposits: Drawn = [
        [(30, "Deposits")],
        _CASH,
        [(30, "03/12"), (70, "Payroll Example Co"), (220, "1,000.00")],
    ]
    page2: Drawn = [
        _CASH,
        [(30, "03/26"), (70, "Payroll Example Co"), (220, "1,000.00")],
    ]
    statement = _generic(
        [*_held(_ZZAB), *deposits],
        page2,
        cash=("$5,000.00", "$7,000.00"),
        value="$8,000.00",
        running=[(40, running)],
    )
    assert _rows(statement) == [
        ("deposit", None, Decimal("1000.00"), "Payroll Example Co"),
        ("deposit", None, Decimal("1000.00"), "Payroll Example Co"),
    ]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


_INCOME: Drawn = [
    [(30, "Dividends and Interest")],
    _CASH,
    [(30, "03/12"), (70, "Example Index Fund"), (220, "12.50")],
]


@pytest.mark.parametrize("running", _RUNNING_HEADERS)
def test_a_continued_income_row_keeps_its_memo_and_security(running: str) -> None:
    """A running page header over the repeated header is no header line: the
    description column keeps its width."""
    page2: Drawn = [
        _CASH,
        [(30, "03/26"), (70, "Example Index Fund"), (220, "12.50")],
    ]
    statement = _generic(
        [*_held(_ZZAB), *_INCOME],
        page2,
        cash=("$5,000.00", "$5,025.00"),
        value="$6,025.00",
        running=[(30, running)],
    )
    assert _rows(statement) == [
        ("dividend", "ZZAB", Decimal("12.50"), "Example Index Fund"),
        ("dividend", "ZZAB", Decimal("12.50"), "Example Index Fund"),
    ]


@pytest.mark.parametrize(
    "above",
    [
        "Account Number: XXXX-1234",
        "Jane Example - Account XXXX-1234",
        "Statement Page 2 of 2",
    ],
)
def test_a_line_naming_the_account_or_page_continues_the_section(above: str) -> None:
    """Printed on the later page only, a line naming the account or the page is
    that page's header, not a heading."""
    page2: Drawn = [
        [(30, above)],
        _CASH,
        [(30, "03/26"), (70, "Example Index Fund"), (220, "12.50")],
    ]
    statement = _generic(
        [*_held(_ZZAB), *_INCOME], page2, cash=("$5,000.00", "$5,025.00"), value="$6,025.00"
    )
    assert [r[0] for r in _rows(statement)] == ["dividend", "dividend"]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def _withdrawal_under(heading: str) -> Drawn:
    return [
        [(30, heading)],
        _CASH,
        [(30, "03/20"), (70, "ATM Example Street"), (220, "-500.00")],
    ]


def _assert_held(statement: InvestmentStatement) -> None:
    """The -500.00 under a heading no section names is no dividend: it is held."""
    assert _rows(statement) == [("dividend", "ZZAB", Decimal("12.50"), "Example Index Fund")]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "mismatch"


_UNKNOWN_HEADINGS = ["Miscellaneous Items", "Other Transactions"]
#: Unknown headings holding a word a table header also uses.
_HEADER_WORD_HEADINGS = ["ATM Withdrawals", "Wire Transfers", "Checks"]


@pytest.mark.parametrize("heading", _UNKNOWN_HEADINGS + _HEADER_WORD_HEADINGS)
@pytest.mark.parametrize("running", [None, "Jane Example", "Jane Example - Account XXXX-1234"])
def test_an_unknown_heading_on_the_next_page_ends_the_section_too(
    heading: str, running: str | None
) -> None:
    """A heading no section names at the top of a later page, over the section's
    own columns and under the running page header (or none), ends the section
    exactly as it does on the same page."""
    top = [(30.0, running)] if running is not None else None
    cash, value = ("$5,000.00", "$4,512.50"), "$5,512.50"
    same = _generic(
        [*_held(_ZZAB), *_INCOME, None, *_withdrawal_under(heading)],
        cash=cash,
        value=value,
        running=top,
    )
    _assert_held(same)
    later = _generic(
        [*_held(_ZZAB), *_INCOME], _withdrawal_under(heading), cash=cash, value=value, running=top
    )
    _assert_held(later)
    # Under a heading the vocabulary knows the row is typed, and its whole
    # description is its memo (the heading is no top line of the header).
    known = _generic(
        [*_held(_ZZAB), *_INCOME],
        _withdrawal_under("Electronic Funds Transfers"),
        cash=cash,
        value=value,
        running=top,
    )
    assert _rows(known)[1] == ("withdrawal", None, Decimal("-500.00"), "ATM Example Street")
    assert known.reconciliation is not None
    assert known.reconciliation.status == "ok"


def test_a_heading_repeated_on_later_pages_is_no_running_header() -> None:
    """A heading printed at the top of pages 2 and 3 (its table running on) but
    not of the page the section above is on is a heading, not a page header."""
    page2 = _withdrawal_under("Other Transactions")
    page3: Drawn = [
        [(30, "Other Transactions")],
        _CASH,
        [(30, "03/21"), (70, "ATM Example Street"), (220, "-100.00")],
    ]
    statement = _generic(
        [*_held(_ZZAB), *_INCOME],
        page2,
        page3,
        cash=("$5,000.00", "$4,412.50"),
        value="$5,412.50",
        running=[(30, "Jane Example")],
    )
    _assert_held(statement)


def _cells(text: str) -> list[tuple[float, str]]:
    """``A|B``: cells ``A`` and ``B`` on one baseline."""
    return [(30.0 + 300.0 * i, part) for i, part in enumerate(text.split("|"))]


@pytest.mark.parametrize(
    ("cover", "block"),
    [
        # A running header of two lines naming the account or the page, page 1
        # a cover page without it.
        ("", "Jane Example - Account XXXX-1234//March 1, 2024 - March 31, 2024"),
        ("", "Page 2 of 4//Jane Example"),
        ("", "Account XXXX-1234//Jane Example"),
        ("", "Jane Example|Account XXXX-1234//Individual Brokerage Account"),
        ("Page 1 of 4", "Page 2 of 4//Jane Example"),
        # A line saying the table runs on.
        ("", "Account Activity (continued)"),
        # One cell of a page-1 band line of two cells.
        ("Example Brokerage|Statement for March 2024", "Example Brokerage"),
        # The statement's title holding a list word, page 1 a cover page
        # without it.
        (
            "Example Brokerage",
            "Example Brokerage//Activity Statement//Jane Example - Account XXXX-1234",
        ),
        ("", "Monthly Activity Statement//Jane Example - Account XXXX-1234"),
        ("Jane Example", "Activity Statement - Account XXXX-1234"),
        ("", "Account Activity for XXXX-1234"),
        ("", "Account Transactions - Page 2 of 2"),
        ("Jane Example", "Jane Example//Statement of Transactions - Account XXXX-1234"),
        # A value box printed at the top of every later page.
        ("", "Total Account Value|$6,025.00//Jane Example - Account XXXX-1234"),
        ("Jane Example", "Jane Example//Total Account Value|$6,025.00//Account XXXX-1234"),
    ],
)
def test_a_running_header_of_several_lines_continues_the_section(cover: str, block: str) -> None:
    """Every line between the page's top and the repeated header is the page's
    running header: the section runs on and reconciles."""
    page2: Drawn = [
        *(_cells(line) for line in block.split("//")),
        _CASH,
        [(30, "03/26"), (70, "Example Index Fund"), (220, "12.50")],
    ]
    statement = _generic(
        [*_held(_ZZAB), *_INCOME],
        page2,
        cash=("$5,000.00", "$5,025.00"),
        value="$6,025.00",
        cover=[_cells(line) for line in cover.split("//")] if cover else None,
    )
    assert [r[0] for r in _rows(statement)] == ["dividend", "dividend"]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


_LABELLED_HEADINGS = [
    "Other Transactions (Page 2)",
    "Other Transactions - Page 2 of 2",
    "Miscellaneous Items, page 2",
    "Other Transactions|Page 2 of 2",
    "Other Transactions|Account XXXX-1234",
    "Other Transactions|XXXX-1234",
    "Page 2 of 4//Other Transactions",
    "Page 2 of 4//Wire Transfers",
    # Beside the account number, words for one kind of entries are no title.
    "Miscellaneous Items - Account XXXX-1234",
    "Securities Lending Activity - Account XXXX-1234",
    "Monthly Activity Statement//Other Transactions",
    # A heading that also mentions the statement or a report is no title.
    "Miscellaneous Items Report - Account XXXX-1234",
    "Other Transactions Not on This Statement - Account XXXX-1234",
    "Account XXXX-1234//Other Transactions Report",
    "Page 2 of 2//Checks Outstanding Since Last Statement",
    "Page 2 of 2//Transactions After Statement Date",
]


@pytest.mark.parametrize("block", _LABELLED_HEADINGS)
def test_a_heading_beside_the_page_or_account_still_ends_the_section(block: str) -> None:
    """A line naming the page or the account names no running header when the
    rest of it, or a line under it, is a heading: the row is held."""
    page2: Drawn = [
        *(_cells(line) for line in block.split("//")),
        _CASH,
        [(30, "03/20"), (70, "ATM Example Street"), (220, "-500.00")],
    ]
    statement = _generic(
        [*_held(_ZZAB), *_INCOME], page2, cash=("$5,000.00", "$4,512.50"), value="$5,512.50"
    )
    _assert_held(statement)


@pytest.mark.parametrize("block", ["Other Items (Page 2)", "Other Items"])
def test_a_heading_beside_the_page_label_after_a_skipped_section_is_held(block: str) -> None:
    """Under a skipped section the row is held with a warning, not dropped."""
    pending: Drawn = [
        [(30, "Trades Pending Settlement")],
        _CASH,
        [(30, "03/29"), (70, "Withdrawal to bank"), (220, "-100.00")],
    ]
    page2: Drawn = [
        _cells(block),
        _CASH,
        [(30, "03/20"), (70, "Sent to Example Bank"), (220, "-500.00")],
    ]
    result = parse_extraction(
        _extract(
            _drawn_pdf(
                [
                    [(40, "Brokerage Account Statement")],
                    [(40, "Statement Period: 03/01/2024 - 03/31/2024")],
                    [(40, "Account Number: XXXX-1234")],
                    [(40, "Beginning Cash Balance"), (420, "$5,000.00")],
                    [(40, "Ending Cash Balance"), (420, "$4,500.00")],
                    [(40, "Ending Account Value"), (420, "$5,500.00")],
                    None,
                    *_held(_ZZAB),
                    *pending,
                ],
                page2,
            )
        ),
        parser_name="brokerage_statement_generic",
        threshold=0.85,
    )
    assert result.parse is not None
    assert "activity_type_unknown" in result.parse.warnings
    statement = result.investments[0]
    assert _rows(statement) == []
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "mismatch"


def test_a_heading_that_headed_a_table_on_the_sections_page_is_no_running_header() -> None:
    """Page 2 opens with a table under a heading, then the section's table;
    page 3 opens with the same heading over the same columns: the heading is
    in both pages' header bands, but it headed a table of its own on page 2,
    so it is a heading there and on page 3, not a running header."""
    page2: Drawn = [
        *_withdrawal_under("Other Transactions"),
        None,
        [(30, "Dividends and Interest")],
        _CASH,
        [(30, "03/12"), (70, "Example Index Fund"), (220, "12.50")],
    ]
    page3: Drawn = [
        [(30, "Other Transactions")],
        _CASH,
        [(30, "03/21"), (70, "Sent to Example Bank"), (220, "-100.00")],
    ]
    statement = _generic(
        _held(_ZZAB),
        page2,
        page3,
        cash=("$5,000.00", "$4,412.50"),
        value="$5,412.50",
        running=[(30, "Jane Example")],
    )
    _assert_held(statement)


@pytest.mark.parametrize("running", _RUNNING_HEADERS)
def test_a_section_continued_over_two_later_pages_keeps_typing_its_rows(running: str) -> None:
    """The running header that sat over the section's table on page 2 is no
    heading there: page 3 continues the section too."""
    page: Drawn = [_CASH, [(30, "03/26"), (70, "Example Index Fund"), (220, "12.50")]]
    statement = _generic(
        [*_held(_ZZAB), *_INCOME],
        page,
        page,
        cash=("$5,000.00", "$5,037.50"),
        value="$6,037.50",
        running=[(30, running)],
    )
    assert [r[0] for r in _rows(statement)] == ["dividend"] * 3
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


@pytest.mark.parametrize("heading", ["Dividends and Interest", "Dividends", "Income"])
def test_a_heading_under_an_unidentified_holding_is_no_part_of_its_name(heading: str) -> None:
    """Without a ``(SYMBOL)`` ending it, a heading-worded line is the heading."""
    rows: Drawn = [
        [(40, "Holdings")],
        [(40, "Security"), (250, "Quantity"), (330, "Price"), (420, "Market Value")],
        [(40, "ZZAB Example Index Fund"), (250, "10"), (330, "$100.00"), (420, "$1,000.00")],
        [(40, "ZZCD Example Bond Fund"), (250, "20"), (330, "$50.00"), (420, "$1,000.00")],
        [(40, heading)],
        [(40, "Date"), (100, "Description"), (420, "Amount")],
        [(40, "03/12"), (100, "Example Index Fund"), (420, "12.50")],
    ]
    statement = _generic(rows, cash=("$1,000.00", "$1,012.50"), value="$3,012.50")
    assert [(p.security.symbol, p.security.name) for p in statement.positions] == [
        ("ZZAB", "Example Index Fund"),
        ("ZZCD", "Example Bond Fund"),
    ]
    assert _rows(statement) == [("dividend", "ZZAB", Decimal("12.50"), "Example Index Fund")]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


@pytest.mark.parametrize("word", ["INCOME", "DIVIDENDS", "FEES", "TAXES", "TRADES", "POSITIONS"])
def test_a_wrapped_name_worded_like_a_heading_is_the_name(word: str) -> None:
    rows: Drawn = [
        [(40, "Holdings")],
        [(40, "Description"), (250, "Quantity"), (330, "Price"), (420, "Market Value")],
        [(40, "EXAMPLE STRATEGIC"), (250, "10"), (330, "$100.00"), (420, "$1,000.00")],
        [(40, f"{word} FUND (ZZSIX)")],
        [(40, "EXAMPLE GROWTH (ZZGRX)"), (250, "10"), (330, "$100.00"), (420, "$1,000.00")],
        [(40, "Total Holdings"), (420, "$2,000.00")],
    ]
    statement = _generic(rows, cash=("$1,000.00", "$1,000.00"), value="$3,000.00")
    assert [(p.security.symbol, p.security.name) for p in statement.positions] == [
        ("ZZSIX", f"EXAMPLE STRATEGIC {word} FUND"),
        ("ZZGRX", "EXAMPLE GROWTH"),
    ]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_a_leading_ticker_names_a_holding_nothing_else_identifies() -> None:
    rows: Drawn = [
        [(40, "Holdings")],
        [(40, "Security"), (250, "Quantity"), (330, "Price"), (420, "Market Value")],
        [(40, "ZZAB Example Index Fund"), (250, "10"), (330, "$100.00"), (420, "$1,000.00")],
        [(40, "ZZCD Example Bond Fund"), (250, "20"), (330, "$50.00"), (420, "$1,000.00")],
        [(40, "Total Holdings"), (420, "$2,000.00")],
    ]
    statement = _generic(rows, cash=("$1,000.00", "$1,000.00"), value="$3,000.00")
    assert [(p.security.symbol, p.security.name) for p in statement.positions] == [
        ("ZZAB", "Example Index Fund"),
        ("ZZCD", "Example Bond Fund"),
    ]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


@pytest.mark.parametrize(
    ("raw", "symbol"),
    [
        ("ZZAB Example Index Fund", "ZZAB"),
        ("ZZB.A Example Class A", "ZZB.A"),
        # The whole name in capitals: the first word is the issuer's.
        ("ZZQ NET LEASE INC & GROWTH LP UNIT", None),
        # Parentheses that hold no ticker: the layout prints none in front.
        ("ZZ Portfolio 2030 (Example Funds)", None),
        ("Example Index Fund", None),
        ("ZZAB", None),
    ],
)
def test_the_leading_ticker_rule(raw: str, symbol: str | None) -> None:
    from app.ingestion.parsers.brokerage import _HoldingRow, _leading_ticker

    row = _HoldingRow(
        page=1,
        bbox=(0.0, 0.0, 1.0, 1.0),
        symbol=None,
        name=raw,
        quantity=Decimal(10),
        price=None,
        value=None,
        cost=None,
        ocr=None,
        raw_name=raw,
    )
    assert _leading_ticker(row)[0] == symbol


_BY_NAME = [(40.0, "Description"), (250.0, "Quantity"), (330.0, "Price"), (420.0, "Market Value")]


def test_a_holding_printed_by_its_name_alone_is_kept_by_that_name() -> None:
    """No symbol, CUSIP or ISIN: the name names the holding; it counts in the total."""
    rows: Drawn = [
        [(40, "Holdings")],
        _BY_NAME,
        [(40, "EXAMPLE GROWTH FUND (ZZGRX)"), (250, "10"), (330, "$100.00"), (420, "$1,000.00")],
        [(40, "EXAMPLE ROYALTY"), (250, "4.000"), (330, "125.000A"), (420, "500.00")],
        [(40, "PARTNERS LP IX-Z")],
        [(40, "Estimated from an appraisal")],
        [
            (40, "Example Plan Portfolio 2040 (Example Funds)"),
            (250, "20"),
            (330, "25.00"),
            (420, "500.00"),
        ],
        [(40, "Total Holdings"), (420, "$2,000.00")],
    ]
    statement = _generic(rows, cash=("$1,000.00", "$1,000.00"), value="$3,000.00")
    assert [(p.security.symbol, p.security.name) for p in statement.positions] == [
        ("ZZGRX", "EXAMPLE GROWTH FUND"),
        # Wrapped in capitals: the name; the sentence-case line under it: a note.
        (None, "EXAMPLE ROYALTY PARTNERS LP IX-Z"),
        # Parentheses holding words are part of the name, not a ticker.
        (None, "Example Plan Portfolio 2040 (Example Funds)"),
    ]
    assert statement.positions[1].unit_price == Decimal("125.000")
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert "holdings_total" in statement.reconciliation.checks


def test_a_trade_is_named_by_its_security_name_column_alone() -> None:
    """A sold-out security printed without a symbol is named by its Security Name;
    a description column never names a security that way."""
    header = [
        (40.0, "Date"),
        (90.0, "Activity"),
        (160.0, "Security Name"),
        (330.0, "Quantity"),
        (390.0, "Price"),
        (460.0, "Amount"),
    ]
    rows: Drawn = [
        [(40, "Holdings")],
        _BY_NAME,
        [(40, "EXAMPLE GROWTH FUND (ZZGRX)"), (250, "10"), (330, "$100.00"), (420, "$1,000.00")],
        [(40, "Total Holdings"), (420, "$1,000.00")],
        None,
        [(40, "Activity")],
        header,
        [
            (40, "03/12/2024"),
            (90, "Sold"),
            (160, "Example Private Fund LP"),
            (330, "5"),
            (390, "100.00"),
            (460, "500.00"),
        ],
        [(40, "03/14/2024"), (90, "Dividend"), (160, "Example Growth Fund"), (460, "12.00")],
    ]
    statement = _generic(rows, cash=("$1,000.00", "$1,512.00"), value="$2,512.00")
    sold, dividend = statement.activity
    assert sold.type == "sell"
    assert sold.security is not None
    assert (sold.security.identified, sold.security.name) == (False, "Example Private Fund LP")
    # Income names a security only by a held one's name (here the fund's).
    assert dividend.security is not None
    assert dividend.security.symbol == "ZZGRX"
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    described = _generic(
        [
            *rows[:6],
            [(c[0], "Description" if c[1] == "Security Name" else c[1]) for c in header],
            rows[7],
        ],
        cash=("$1,000.00", "$1,500.00"),
        value="$2,500.00",
    )
    assert [a.security for a in described.activity] == [None]


def test_a_security_named_by_its_name_alone_needs_a_word() -> None:
    assert SecurityRef(name="  Example   Fund LP ").name == "Example Fund LP"
    assert SecurityRef(name="=HYPERLINK(1)").name == "'=HYPERLINK(1)"
    assert not SecurityRef(name="Example Fund LP").identified
    for name in (None, "", " - ", "***"):
        with pytest.raises(ValidationError):
            SecurityRef(name=name)


def _trade(
    day: str, symbol: str, name: str, quantity: str, price: str, amount: str
) -> list[tuple[float, str]]:
    return [
        (40, day),
        (90, "Bought"),
        (160, symbol),
        (210, name),
        (330, quantity),
        (390, price),
        (460, amount),
    ]


def test_a_bond_trade_is_priced_per_100_like_its_position() -> None:
    activity_header = [
        (40.0, "Date"),
        (90.0, "Activity"),
        (160.0, "Symbol"),
        (210.0, "Description"),
        (330.0, "Quantity"),
        (390.0, "Price"),
        (460.0, "Amount"),
    ]
    rows: Drawn = [
        [(40, "Holdings")],
        [*_HOLDINGS, (420, "Market Value")],
        _ZZAB,
        [
            (40, "ZZBND"),
            (100, "Example Corp 5% 2030"),
            (250, "10,000"),
            (330, "98.625"),
            (420, "$9,862.50"),
        ],
        [(40, "Total Holdings"), (420, "$10,862.50")],
        None,
        [(40, "Activity")],
        activity_header,
        _trade("03/05/2024", "ZZBND", "Example Corp 5% 2030", "2,000", "99.00", "1,980.00"),
        _trade("03/12/2024", "ZZAB", "Example Index Fund", "5", "100.00", "500.00"),
    ]
    statement = _generic(rows, cash=("$5,000.00", "$2,520.00"), value="$13,382.50")
    bond, fund = statement.activity
    assert bond.price_per_100
    assert bond.unit_price == Decimal("99.00")
    assert bond.price_per_unit == Decimal("0.99")
    assert not fund.price_per_100
    assert fund.price_per_unit == Decimal("100.00")


def test_a_bond_trade_is_per_100_by_its_figures_without_a_position() -> None:
    from app.ingestion.parsers.brokerage import _activity_per_100, _ActivityRow
    from app.ingestion.parsers.text import Amount

    def row(quantity: str, price: str, amount: str) -> _ActivityRow:
        return _ActivityRow(
            page=1,
            bbox=(0.0, 0.0, 1.0, 1.0),
            date_text="03/05/2024",
            settle_text="",
            type="sell",
            description="",
            symbol=None,
            quantity=Decimal(quantity),
            price=Decimal(price),
            amount=Amount(value=Decimal(amount)),
            fees=None,
            ocr=None,
        )

    assert _activity_per_100(row("5000", "101.25", "5094.10"), False)  # accrued interest in it
    assert not _activity_per_100(row("100", "37.18", "3725.85"), True)  # per unit by its figures
    assert _activity_per_100(row("5000", "101.25", "0"), True)  # no cash: as its position
    assert not _activity_per_100(row("5000", "101.25", "0"), False)


def test_securities_transferred_in_without_a_symbol_column_move_units() -> None:
    rows: Drawn = [
        [(40, "Holdings")],
        [*_HOLDINGS, (420, "Market Value")],
        [(40, "ZZAB"), (100, "Example Index Fund (ZZAB)"), *_ZZAB[2:]],
        [(40, "Total Holdings"), (420, "$1,000.00")],
        None,
        [(30, "Securities Transferred In")],
        [(30, "Date"), (70, "Description"), (250, "Quantity"), (330, "Amount")],
        [(30, "03/12"), (70, "Example Index Fund"), (250, "10"), (330, "1,000.00")],
    ]
    statement = _generic(rows, cash=("$5,000.00", "$5,000.00"), value="$6,000.00")
    (moved,) = statement.activity
    assert (moved.type, moved.quantity, moved.amount) == ("transfer_in", Decimal(10), Decimal(0))
    assert moved.security is not None
    assert moved.security.symbol == "ZZAB"
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_dated_cash_balance_lines_are_the_opening_and_closing_cash() -> None:
    result = parse_extraction(
        _extract(
            _drawn_pdf(
                [
                    [(40, "Brokerage Account Statement")],
                    [(40, "Statement Period: 03/01/2024 - 03/31/2024")],
                    [(40, "Account Number: XXXX-1234")],
                    [(40, "Cash balance on 03/01/2024"), (420, "$5,000.00")],
                    [(40, "Cash balance on 03/31/2024"), (420, "$4,500.00")],
                    [(40, "Ending Account Value"), (420, "$5,500.00")],
                    None,
                    *_held(_ZZAB),
                    [(40, "Activity")],
                    [(40, "Date"), (90, "Activity"), (210, "Description"), (460, "Amount")],
                    [(40, "03/20/2024"), (90, "Withdrawal"), (210, "To bank"), (460, "500.00")],
                ]
            )
        ),
        parser_name="brokerage_statement_generic",
        threshold=0.85,
    )
    (statement,) = result.investments
    assert statement.cash_opening is not None
    assert statement.cash_closing is not None
    assert (statement.cash_opening.amount, statement.cash_closing.amount) == (
        Decimal("5000.00"),
        Decimal("4500.00"),
    )
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


@pytest.mark.parametrize("heading", _HEADER_WORD_HEADINGS)
@pytest.mark.parametrize("next_page", [False, True])
@pytest.mark.parametrize("running", ["Jane Example", "Jane Example - Account XXXX-1234"])
def test_a_heading_holding_a_header_word_is_no_header_line(
    heading: str, next_page: bool, running: str
) -> None:
    """Under the running page header, at the top of a later page or not, such a
    heading is a heading: the description column keeps its width."""
    table: Drawn = [
        [(30, heading)],
        _CASH,
        [(30, "03/20"), (70, "Withdrawal to Example Street Bank"), (220, "-500.00")],
    ]
    rows, more = (
        ([*_held(_ZZAB), *_INCOME], [table])
        if next_page
        else (
            [*_held(_ZZAB), *_INCOME, None, *table],
            [],
        )
    )
    statement = _generic(
        rows,
        *more,
        cash=("$5,000.00", "$4,512.50"),
        value="$5,512.50",
        running=[(30, running)],
    )
    assert _rows(statement)[1] == (
        "withdrawal",
        None,
        Decimal("-500.00"),
        "Withdrawal to Example Street Bank",
    )
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
