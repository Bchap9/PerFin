# SPDX-License-Identifier: AGPL-3.0-or-later
"""401(k) plan statements in the roll-forward layout (``plan_statement.py``).

The statement of ``tests/fixtures/synth_plan_statement.py`` prints dollars
per fund and no units, so it becomes one thing: the plan's balance at the
end of the period, checked by the statement's own arithmetic. Nothing is
read from a statement of two plans, and a page that only resembles the
table (a bank summary of two balances, a brokerage statement) is not one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from sqlalchemy.orm import Session

from app.domain.documents.models import Document, DocumentKind
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import BALANCE_ONLY_PARSERS, Cell, DocumentView, ViewLine
from app.ingestion.parsers.plan_statement import PARSER_NAME, RetirementPlanParser
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.pipeline import _parse_document
from app.ingestion.sandbox.protocol import (
    ExtractOptions,
    ExtractResult,
    InvestmentStatement,
)
from tests.fixtures import synth_brokerage as brokerage
from tests.fixtures import synth_fidelity as fid
from tests.fixtures import synth_pdf_statements as bank
from tests.fixtures import synth_plan_statement as synth
from tests.fixtures.synth_statements import sample_statement

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _parse(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return parse_extraction(result, threshold=0.85)


def _view(data: bytes) -> DocumentView:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return DocumentView.from_result(result)


def _only(result: ExtractResult) -> InvestmentStatement:
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == PARSER_NAME, result.parse.scores
    (statement,) = result.investments
    return statement


def _figures(statement: InvestmentStatement) -> list[tuple[str, str, Decimal | None]]:
    assert {f.group for f in statement.figures} == {"account_value"}
    return [(f.role, f.label, f.amount) for f in statement.figures]


def test_plan_statement_is_detected_and_read() -> None:
    result = _parse(synth.plan_statement_pdf())
    statement = _only(result)

    assert PARSER_NAME == "retirement_plan_statement"
    assert PARSER_NAME in BALANCE_ONLY_PARSERS
    assert statement.source_format == "pdf"
    assert statement.total_value is not None
    assert statement.total_value.amount == Decimal("32190.00")
    assert statement.total_value.as_of == date(2026, 3, 31)
    assert (statement.period_start, statement.period_end) == synth.PERIOD
    assert statement.as_of == date(2026, 3, 31)
    assert statement.account.account_type == "retirement"
    assert statement.account.institution == synth.INSTITUTION
    assert statement.account.last4 is None
    assert statement.positions == []
    assert statement.activity == []
    assert statement.cash_closing is None
    assert statement.positions_value is None

    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.checks, check.failed) == (
        "ok",
        ["roll_forward", "roll_forward_rows"],
        [],
    )
    assert (check.reported_total_value, check.total_value) == (synth.CLOSING, synth.CLOSING)
    assert _figures(statement) == [
        ("opening", "Beginning Balance", synth.OPENING),
        ("item", "Earnings/Fees", synth.MOVEMENTS[0]),
        ("item", "Contributions", synth.MOVEMENTS[1]),
        ("item", "Withdrawals", synth.MOVEMENTS[2]),
        ("item", "Transfers", synth.MOVEMENTS[3]),
        ("closing", "Ending Balance", synth.CLOSING),
    ]
    assert statement.confidence["period"] >= 0.9
    assert statement.confidence["total_value"] >= 0.9
    assert result.parse is not None
    assert result.parse.warnings == []


def test_the_outcome_counts_as_read_and_the_document_is_a_statement() -> None:
    """A balance is something read: no fallback parser is tried, the document gets its period."""
    data = synth.plan_statement_pdf()
    outcome = RetirementPlanParser().parse(_view(data), threshold=0.85)
    assert not outcome.empty

    result = extract_pdf(data, OPTIONS, "pdf")
    document = Document(
        kind=DocumentKind.UNKNOWN, confidence={}, created_at=datetime(2026, 4, 6, tzinfo=UTC)
    )
    db = MagicMock(spec=Session)
    _parse_document(db, document, result, 0.85)
    assert document.kind is DocumentKind.STATEMENT
    assert (document.period_start, document.period_end) == synth.PERIOD
    assert document.parser_version == "retirement_plan_statement/1"
    assert not db.mock_calls


def test_tampered_total_does_not_reconcile() -> None:
    statement = _only(_parse(synth.plan_statement_pdf(tamper=True)))

    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.failed) == ("mismatch", ["roll_forward"])
    # The printed ending total, and what the row's own figures come to.
    assert check.reported_total_value == synth.CLOSING
    assert check.total_value == synth.CLOSING + Decimal("100.00")
    assert statement.total_value is not None
    assert statement.confidence["total_value"] < 0.85


def test_table_by_source_is_the_same_money() -> None:
    both = _only(_parse(synth.plan_statement_pdf(by_source=True)))
    one = _only(_parse(synth.plan_statement_pdf(by_source=False)))

    assert both.model_dump(exclude={"last_page"}) == one.model_dump(exclude={"last_page"})


def test_second_plan_reads_nothing() -> None:
    result = _parse(synth.plan_statement_pdf(second_plan=True))

    assert result.investments == []
    assert result.statements == []
    assert result.parse is not None
    assert "plan_statement_several_accounts" in result.parse.warnings


def test_a_column_of_money_out_printed_without_a_sign_is_subtracted() -> None:
    """``Withdrawals 450.00`` left the plan: the total row says which way the column counts."""
    statement = _only(_parse(synth.plan_statement_pdf(subtracted=True)))

    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.failed) == ("ok", [])
    assert statement.total_value is not None
    assert statement.total_value.amount == synth.SUBTRACTED_CLOSING
    # Stored the way it counts, so the figures add up as listed.
    amounts = [amount for _, _, amount in _figures(statement)]
    assert amounts[3] == Decimal("-450.00")
    assert sum(a for a in amounts[:-1] if a is not None) == amounts[-1]


def test_brokerage_and_bank_fixtures_score_zero() -> None:
    parser = RetirementPlanParser()
    january, _ = brokerage.brokerage_series(seed=7)
    pages = [
        brokerage.placeholder_pdf(january),
        fid.fidelity_pdf(fid.fidelity_statement(seed=7)),
        bank.generic_pdf(sample_statement(seed=7)),
    ]
    for data in pages:
        assert parser.detect(_view(data)) == 0.0
    assert parser.detect(_view(synth.plan_statement_pdf())) == 0.65


def _line(index: int, *texts: str) -> ViewLine:
    y = 40.0 + 20.0 * index
    cells = tuple(
        Cell(text, 40.0 + 110.0 * at, y, 40.0 + 110.0 * at + 5.0 * len(text), y + 10.0)
        for at, text in enumerate(texts)
    )
    return ViewLine(page=1, index=index, cells=cells, text=" ".join(texts), height=10.0)


def _lines(*rows: tuple[str, ...]) -> DocumentView:
    lines = tuple(_line(index, *texts) for index, texts in enumerate(rows))
    return DocumentView(lines=lines, source_format="pdf", truncated=False, page_count=1)


def test_two_balances_with_no_movement_between_are_no_plan_table() -> None:
    """A bank summary heads two balances; a sentence names both: neither is the table."""
    parser = RetirementPlanParser()
    summary = _lines(
        ("Example Savings Bank",),
        ("Account", "Beginning Balance", "Ending Balance"),
        ("Savings", "1,000.00", "1,250.00"),
    )
    sentence = _lines(
        ("Your beginning balance plus contributions and earnings is your ending balance.",),
    )
    deposits = _lines(
        ("Account", "Beginning Balance", "Deposits", "Withdrawals", "Fees", "Ending Balance"),
        ("Checking", "1,000.00", "500.00", "240.00", "10.00", "1,250.00"),
        ("Total", "1,000.00", "500.00", "240.00", "10.00", "1,250.00"),
    )
    reversed_order = _lines(
        ("Fund", "Ending Balance", "Contributions", "Beginning Balance"),
    )
    for view in (summary, sentence, deposits, reversed_order):
        assert parser.detect(view) == 0.0
        assert parser.parse(view, threshold=0.85).investments == []


def test_no_total_row_is_unavailable_and_names_no_balance() -> None:
    view = _lines(
        ("Example Plan Services",),
        ("Statement Period: 01/01/2026 - 03/31/2026",),
        ("By Investment", "Beginning Balance", "Contributions", "Ending Balance"),
        ("Example Bond Index Fund", "8,000.00", "600.00", "8,600.00"),
    )
    parser = RetirementPlanParser()
    assert parser.detect(view) == 0.65
    outcome = parser.parse(view, threshold=0.85)

    (statement,) = outcome.investments
    assert outcome.warnings == ["plan_statement_no_total"]
    assert statement.total_value is None
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "unavailable"
    assert outcome.empty


def test_a_row_that_cannot_be_placed_fails_the_rows_check() -> None:
    """A fund row short of a figure is not guessed at: the rows no longer vouch for the total."""
    view = _lines(
        ("Example Plan Services",),
        ("Statement Period: 01/01/2026 - 03/31/2026",),
        ("By Investment", "Beginning Balance", "Contributions", "Fees", "Ending Balance"),
        ("Example Bond Index Fund", "8,000.00", "600.00", "(10.00)", "8,590.00"),
        ("Example Stable Value Fund", "2,000.00 see note", "2,000.00"),
        ("Sub Total", "10,000.00", "600.00", "(10.00)", "10,590.00"),
        ("Total", "$10,000.00", "$600.00", "($10.00)", "$10,590.00"),
    )
    outcome = RetirementPlanParser().parse(view, threshold=0.85)

    (statement,) = outcome.investments
    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.failed) == ("mismatch", ["roll_forward_rows"])


def test_an_ambiguous_period_is_in_doubt() -> None:
    """``01/02/2026 - 03/04/2026`` reads two ways: the period is kept below the threshold."""
    view = _lines(
        ("Example Plan Services",),
        ("Statement Period: 01/02/2026 - 03/04/2026",),
        ("By Investment", "Beginning Balance", "Contributions", "Ending Balance"),
        ("Example Bond Index Fund", "8,000.00", "600.00", "8,600.00"),
        ("Total", "$8,000.00", "$600.00", "$8,600.00"),
    )
    (statement,) = RetirementPlanParser().parse(view, threshold=0.85).investments

    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.confidence["period"] < 0.85


def test_a_fund_whose_name_starts_with_total_is_a_fund() -> None:
    """Only the row named ``Total`` is the total: the first fund's balance is not the plan's."""
    view = _lines(
        ("Example Plan Services",),
        ("Statement Period: 01/01/2026 - 03/31/2026",),
        ("By Investment", "Beginning Balance", "Contributions", "Ending Balance"),
        ("Total Market Example Fund", "8,000.00", "600.00", "8,600.00"),
        ("Example Stable Value Fund", "2,000.00", "0.00", "2,000.00"),
        ("Total", "$10,000.00", "$600.00", "$10,600.00"),
    )
    (statement,) = RetirementPlanParser().parse(view, threshold=0.85).investments

    assert statement.total_value is not None
    assert statement.total_value.amount == Decimal("10600.00")
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_a_table_goes_on_under_its_header_printed_again() -> None:
    view = _lines(
        ("Example Plan Services",),
        ("Statement Period: 01/01/2026 - 03/31/2026",),
        ("By Investment", "Beginning Balance", "Contributions", "Ending Balance"),
        ("Example Bond Index Fund", "8,000.00", "600.00", "8,600.00"),
        ("Page 1 of 2",),
        ("By Investment", "Beginning Balance", "Contributions", "Ending Balance"),
        ("Example Stable Value Fund", "2,000.00", "-", "2,000.00"),
        ("Total", "$10,000.00", "$600.00", "$10,600.00"),
    )
    outcome = RetirementPlanParser().parse(view, threshold=0.85)

    (statement,) = outcome.investments
    assert outcome.warnings == []
    assert outcome.skipped_lines == 0
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.total_value is not None
    assert statement.total_value.amount == Decimal("10600.00")


def test_a_blank_cell_is_placed_by_its_column() -> None:
    """A row that prints fewer figures than columns puts each under its own header."""
    lines = (
        _line(0, "Example Plan Services"),
        _line(1, "Statement Period: 01/01/2026 - 03/31/2026"),
        _line(
            2,
            "By Investment",
            "Beginning Balance",
            "Contributions",
            "Withdrawals",
            "Ending Balance",
        ),
        # No figure under Contributions: the cells keep their columns.
        ViewLine(
            page=1,
            index=3,
            cells=(
                Cell("Example Bond Index Fund", 40.0, 100.0, 140.0, 110.0),
                Cell("8,000.00", 150.0, 100.0, 190.0, 110.0),
                Cell("450.00", 370.0, 100.0, 400.0, 110.0),
                Cell("7,550.00", 480.0, 100.0, 520.0, 110.0),
            ),
            text="Example Bond Index Fund 8,000.00 450.00 7,550.00",
            height=10.0,
        ),
        _line(4, "Total", "$8,000.00", "$0.00", "$450.00", "$7,550.00"),
    )
    view = DocumentView(lines=lines, source_format="pdf", truncated=False, page_count=1)
    (statement,) = RetirementPlanParser().parse(view, threshold=0.85).investments

    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.failed) == ("ok", [])
    # 450.00 stands under Withdrawals, which is what makes the row add up.
    assert [f.amount for f in statement.figures] == [
        Decimal("8000.00"),
        Decimal("0.00"),
        Decimal("-450.00"),
        Decimal("7550.00"),
    ]


def test_two_figures_under_one_header_are_not_a_row() -> None:
    """Fewer figures than columns, two of them nearest the same header: nothing is assumed."""
    lines = (
        _line(0, "Example Plan Services"),
        _line(1, "Statement Period: 01/01/2026 - 03/31/2026"),
        _line(
            2,
            "By Investment",
            "Beginning Balance",
            "Contributions",
            "Withdrawals",
            "Ending Balance",
        ),
        ViewLine(
            page=1,
            index=3,
            cells=(
                Cell("Example Bond Index Fund", 40.0, 100.0, 140.0, 110.0),
                Cell("8,000.00", 150.0, 100.0, 190.0, 110.0),
                Cell("0.00", 470.0, 100.0, 495.0, 110.0),
                Cell("8,000.00", 500.0, 100.0, 540.0, 110.0),
            ),
            text="Example Bond Index Fund 8,000.00 0.00 8,000.00",
            height=10.0,
        ),
        _line(4, "Total", "$8,000.00", "$0.00", "$0.00", "$8,000.00"),
    )
    view = DocumentView(lines=lines, source_format="pdf", truncated=False, page_count=1)
    outcome = RetirementPlanParser().parse(view, threshold=0.85)

    (statement,) = outcome.investments
    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.failed) == ("mismatch", ["roll_forward_rows"])
    assert outcome.skipped_lines == 1


_GROUP_HEADER = (
    "By Investment",
    "Beginning Balance",
    "Contributions",
    "Earnings",
    "Ending Balance",
)
_GROUPS = (
    ("Stock Investments",),
    ("Example Large Cap Fund", "10,000.00", "500.00", "100.00", "10,600.00"),
    ("Example Small Cap Fund", "5,000.00", "200.00", "50.00", "5,250.00"),
    ("Total", "15,000.00", "700.00", "150.00", "15,850.00"),
    ("Bond Investments",),
    ("Example Bond Index Fund", "8,000.00", "300.00", "20.00", "8,320.00"),
    ("Total", "8,000.00", "300.00", "20.00", "8,320.00"),
)


def _grouped(*after: tuple[str, ...]) -> DocumentView:
    return _lines(
        ("Example Plan Services",),
        ("Statement Period: 01/01/2026 - 03/31/2026",),
        _GROUP_HEADER,
        *_GROUPS,
        *after,
    )


def test_a_total_under_a_group_of_funds_is_not_the_plans_total() -> None:
    """Funds printed in groups: the last total row is the plan's, and every fund adds up to it."""
    view = _grouped(("Grand Total", "23,000.00", "1,000.00", "170.00", "24,170.00"))
    outcome = RetirementPlanParser().parse(view, threshold=0.85)

    (statement,) = outcome.investments
    assert statement.total_value is not None
    assert statement.total_value.amount == Decimal("24170.00")
    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.failed) == ("ok", [])
    assert [(f.role, f.amount) for f in statement.figures] == [
        ("opening", Decimal("23000.00")),
        ("item", Decimal("1000.00")),
        ("item", Decimal("170.00")),
        ("closing", Decimal("24170.00")),
    ]
    assert outcome.skipped_lines == 0


def test_a_group_of_one_fund_before_the_account_total() -> None:
    view = _lines(
        ("Example Plan Services",),
        ("Statement Period: 01/01/2026 - 03/31/2026",),
        _GROUP_HEADER,
        ("Example Large Cap Fund", "10,000.00", "500.00", "100.00", "10,600.00"),
        ("Total", "10,000.00", "500.00", "100.00", "10,600.00"),
        ("Example Bond Index Fund", "8,000.00", "300.00", "20.00", "8,320.00"),
        ("Total", "8,000.00", "300.00", "20.00", "8,320.00"),
        ("Account Total", "18,000.00", "800.00", "120.00", "18,920.00"),
    )
    (statement,) = RetirementPlanParser().parse(view, threshold=0.85).investments

    assert statement.total_value is not None
    assert statement.total_value.amount == Decimal("18920.00")
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


def test_group_totals_with_no_total_of_the_whole_do_not_add_up() -> None:
    """Negative: nothing says what the plan holds, so no group's total stands in for it."""
    (statement,) = RetirementPlanParser().parse(_grouped(), threshold=0.85).investments

    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.failed) == ("mismatch", ["roll_forward_rows"])
    assert statement.confidence["total_value"] < 0.85


def test_a_later_total_that_cannot_be_placed_leaves_the_group_total_unvouched() -> None:
    """Negative: the plan's total cannot be placed; the group total before it is no answer."""
    view = _lines(
        ("Example Plan Services",),
        ("Statement Period: 01/01/2026 - 03/31/2026",),
        _GROUP_HEADER,
        ("Example Large Cap Fund", "10,000.00", "500.00", "100.00", "10,600.00"),
        ("Total", "10,000.00", "500.00", "100.00", "10,600.00"),
        ("Example Stable Value Fund", "0.00", "0.00", "0.00", "0.00"),
        ("Grand Total", "10,000.00", "500.00", "100.00", "0.00", "10,600.00", "9,900.00"),
    )
    outcome = RetirementPlanParser().parse(view, threshold=0.85)

    (statement,) = outcome.investments
    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.failed) == ("mismatch", ["roll_forward_rows"])
    assert outcome.skipped_lines == 1


def test_lines_after_the_last_total_are_not_the_tables() -> None:
    """A vested balance and a note under the total change nothing."""
    view = _grouped(
        ("Grand Total", "23,000.00", "1,000.00", "170.00", "24,170.00"),
        ("Vested Balance", "24,000.00"),
        ("Example Loan Fund", "1.00", "2.00", "3.00", "4.00"),
    )
    outcome = RetirementPlanParser().parse(view, threshold=0.85)

    (statement,) = outcome.investments
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    assert statement.total_value is not None
    assert statement.total_value.amount == Decimal("24170.00")
    assert outcome.skipped_lines == 0


def test_a_year_to_date_table_gives_no_opening_balance() -> None:
    """Two tables end alike and begin differently: the balance is kept, no opening figure is."""
    header = ("By Investment", "Beginning Balance", "Contributions", "Earnings", "Ending Balance")
    view = _lines(
        ("Example Plan Services",),
        ("Statement Period: 04/01/2026 - 06/30/2026",),
        ("Year to Date",),
        header,
        ("Example Large Cap Fund", "9,000.00", "1,500.00", "100.00", "10,600.00"),
        ("Total", "9,000.00", "1,500.00", "100.00", "10,600.00"),
        ("This Period",),
        header,
        ("Example Large Cap Fund", "10,000.00", "500.00", "100.00", "10,600.00"),
        ("Total", "10,000.00", "500.00", "100.00", "10,600.00"),
    )
    outcome = RetirementPlanParser().parse(view, threshold=0.85)

    (statement,) = outcome.investments
    assert outcome.warnings == []
    assert statement.total_value is not None
    assert statement.total_value.amount == Decimal("10600.00")
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"
    # Neither 9,000.00 nor 10,000.00 is offered as the balance before the period.
    assert statement.figures == []


def test_a_cash_statement_with_a_roll_forward_summary_keeps_its_transactions() -> None:
    """Negative: a summary row over dated transactions is a statement of transactions."""
    data = synth.cash_summary_pdf()
    view = _view(data)
    parser = RetirementPlanParser()
    assert parser.detect(view) == 0.0
    assert parser.parse(view, threshold=0.85).empty

    result = _parse(data)
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "statement_generic", result.parse.scores
    assert result.investments == []
    (statement,) = result.statements
    assert [(t.posted, t.amount) for t in statement.transactions] == [
        (date(2026, 1, 15), Decimal("200.00")),
        (date(2026, 2, 3), Decimal("-50.00")),
        (date(2026, 3, 31), Decimal("0.50")),
    ]
