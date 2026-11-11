# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a brokerage statement says about its own rows (INGESTION.md "Brokerage statements").

Blocks printed side by side, the totals under each activity section, the
statement's own summary tables and, when a check fails, which rows are left
in doubt (``app.ingestion.parsers.blocks``, ``brokerage.section_checks`` and
``brokerage.localise``). Every name, merchant and figure here is invented.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from app.ingestion.parsers.base import Cell, ViewLine
from app.ingestion.parsers.blocks import unfold_blocks
from app.ingestion.parsers.brokerage import (
    FidelityStatementParser,
    _Collected,
    _total_scope,
    localise,
    reconcile_investment,
    section_rule,
)
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.sandbox.protocol import (
    Balance,
    ExtractOptions,
    ExtractResult,
    InvestmentActivity,
    InvestmentActivityType,
    InvestmentPosition,
    InvestmentStatement,
    SectionCheck,
    SecurityRef,
    StatementFigure,
)
from app.ingestion.sandbox.runner import run_extraction
from tests.fixtures import synth_fidelity as fid
from tests.fixtures import synth_fidelity_report as fidr

WIDTH = 4.5
#: The typographic minus and dash a statement prints (not ASCII ``-``).
MINUS = "\u2212"
EN_DASH = "\u2013"
PERIOD = (date(2026, 6, 1), date(2026, 6, 30))


def _line(page: int, y: float, cells: list[tuple[float, str]], index: int = 0) -> ViewLine:
    built = tuple(Cell(text, x, y, x + WIDTH * len(text), y + 9.0) for x, text in cells)
    return ViewLine(
        page=page, index=index, cells=built, text=" ".join(c.text for c in built), height=9.0
    )


def _titled(norm: str) -> bool:
    return section_rule(norm) is not None


def _texts(lines: list[ViewLine]) -> list[str]:
    return [line.text for line in lines]


# ---------------------------------------------------------------------------
# Blocks printed side by side
# ---------------------------------------------------------------------------


def test_two_blocks_side_by_side_are_read_one_after_the_other() -> None:
    lines = [
        _line(1, 100, [(32, "Core Account Cash Flow"), (392, "Income Summary")]),
        _line(1, 113, [(234, "This Period"), (585, "This Period")]),
        _line(1, 126, [(32, "Beginning Core Account Balance"), (234, "$900.00"), (392, "Taxable")]),
        # The right block's row sits a little higher: the extraction put the
        # left block's amount on its line and left the label alone under it.
        _line(1, 139, [(234, "-250.00"), (392, "Dividends"), (585, "12.40")]),
        _line(1, 141, [(32, "Securities Bought")]),
        _line(1, 154, [(32, "Ending Core Account Balance"), (234, "$650.00")]),
    ]
    # Each amount sits on its own block's row whatever line it was put on.
    lines[3] = ViewLine(
        page=1,
        index=3,
        cells=(
            Cell("-250.00", 234, 141, 234 + WIDTH * 7, 150),
            Cell("Dividends", 392, 139, 392 + WIDTH * 9, 148),
            Cell("12.40", 585, 139, 585 + WIDTH * 5, 148),
        ),
        text="-250.00 Dividends 12.40",
        height=9.0,
    )
    assert _texts(unfold_blocks(lines, _titled)) == [
        "Core Account Cash Flow",
        "This Period",
        "Beginning Core Account Balance $900.00",
        "Securities Bought -250.00",
        "Ending Core Account Balance $650.00",
        "Income Summary",
        "This Period",
        "Taxable",
        "Dividends 12.40",
    ]


def test_a_right_blocks_title_alone_on_its_line_opens_the_region() -> None:
    lines = [
        _line(1, 100, [(392, "Account Holdings")]),
        _line(1, 113, [(32, "Beginning Account Value"), (234, "$5,000.00"), (392, "Stocks")]),
        _line(1, 126, [(32, "Ending Account Value"), (234, "$5,400.00"), (392, "Bonds")]),
    ]
    assert _texts(unfold_blocks(lines, _titled)) == [
        "Beginning Account Value $5,000.00",
        "Ending Account Value $5,400.00",
        "Account Holdings",
        "Stocks",
        "Bonds",
    ]


def test_a_table_across_the_page_ends_the_region() -> None:
    def header(line: ViewLine) -> bool:
        return {"Date", "Description", "Amount"} <= {c.text for c in line.cells}

    lines = [
        _line(1, 100, [(32, "Core Account Cash Flow"), (392, "Income Summary")]),
        _line(1, 113, [(32, "Beginning Core Account Balance"), (234, "$900.00"), (392, "Taxable")]),
        _line(1, 140, [(32, "Date"), (120, "Description"), (585, "Amount")]),
        _line(1, 153, [(32, "10/05"), (120, "Example Deposit"), (585, "$25.00")]),
    ]
    # The header's columns lie on both sides of the cut: the table is no
    # part of either block, and its lines stay whole.
    assert _texts(unfold_blocks(lines, _titled, header)) == [
        "Core Account Cash Flow",
        "Beginning Core Account Balance $900.00",
        "Income Summary",
        "Taxable",
        "Date Description Amount",
        "10/05 Example Deposit $25.00",
    ]
    # A table of the right block alone (its whole header right of the cut)
    # belongs to that block.
    beside = [
        lines[0],
        _line(
            1,
            113,
            [(32, "Beginning Balance"), (392, "Date"), (450, "Description"), (585, "Amount")],
        ),
        _line(
            1,
            126,
            [(32, "Ending Balance"), (392, "10/05"), (450, "Example Deposit"), (585, "$25.00")],
        ),
    ]
    assert _texts(unfold_blocks(beside, _titled, header)) == [
        "Core Account Cash Flow",
        "Beginning Balance",
        "Ending Balance",
        "Income Summary",
        "Date Description Amount",
        "10/05 Example Deposit $25.00",
    ]


@pytest.mark.parametrize(
    "header",
    [
        # A table's header cell that happens to be a heading word.
        [(392.0, "Description"), (675.0, "Contributions")],
        # One word alone on a line (the last line of a stacked header).
        [(675.0, "Contributions")],
        # A row: a heading word beside an amount.
        [(32.0, "Fees"), (392.0, "Income Summary"), (600.0, "$4.00")],
    ],
)
def test_a_heading_word_in_a_table_opens_no_region(header: list[tuple[float, str]]) -> None:
    lines = [
        _line(1, 100, header),
        _line(1, 113, [(32, "Example Portfolio 2031"), (392, "Example Fund"), (675, "75%")]),
    ]
    assert unfold_blocks(lines, _titled) == lines


def test_the_region_ends_at_the_next_full_width_section() -> None:
    lines = [
        _line(1, 100, [(32, "Debit Card Summary"), (212, "Debit Card Activity")]),
        _line(1, 113, [(32, "Ending Balance"), (166, "$0.00"), (212, "6/3"), (693, "-$60.00")]),
        # Clear of the lines above, at the left margin: a section of its own,
        # whose table spans the page.
        _line(1, 160, [(32, "Bill Payments")]),
        _line(
            1, 173, [(32, "Date"), (59, "Payee"), (264, "Amount"), (392, "Date"), (419, "Payee")]
        ),
    ]
    unfolded = unfold_blocks(lines, _titled)
    assert _texts(unfolded) == [
        "Debit Card Summary",
        "Ending Balance $0.00",
        "Debit Card Activity",
        "6/3 -$60.00",
        "Bill Payments",
        "Date Payee Amount Date Payee",
    ]


# ---------------------------------------------------------------------------
# The layout's tables, read block by block
# ---------------------------------------------------------------------------

_CARD_HEADER: list[tuple[float, str]] = [
    (212, "Trans. Date"),
    (266, "Post Date"),
    (320, "Location Reference/ Description"),
    (640, "Transaction Total"),
]


def _read(lines: list[ViewLine]) -> _Collected:
    collected = _Collected()
    FidelityStatementParser()._read(lines, collected)
    return collected


def _activity(collected: _Collected) -> list[InvestmentActivity]:
    rows, _, _ = FidelityStatementParser()._activities(collected, ("md",), True, PERIOD, {})
    return rows


def test_two_cards_beside_their_summary_box_are_one_section() -> None:
    lines = [
        _line(1, 100, [(32, "Debit Card Summary"), (212, "Debit Card Activity")]),
        _line(
            1,
            113,
            [
                (32, "Beginning Balance"),
                (166, "$0.00"),
                (212, "SAMPLE PLATINUM CHECK CARD (ACCOUNT ...3176)"),
            ],
        ),
        # The first card's header shares its line with the box's figure.
        _line(1, 126, [(32, "Fees"), (170, "0.00"), *_CARD_HEADER]),
        _line(
            1,
            139,
            [
                (32, "Total Purchases"),
                (151, "-45.50"),
                (212, "6/3"),
                (266, "6/5"),
                (320, "ATM0001 1 EXAMPLE RD, SAMPLETOWN"),
                (660, "-$60.00"),
            ],
        ),
        _line(
            1,
            152,
            [
                (32, "Total Payments"),
                (155, "94.35"),
                (212, "6/9"),
                (266, "6/11"),
                (320, "EXAMPLE GROCER 0042, SAMPLETOWN"),
                (664, "-45.50"),
            ],
        ),
        _line(
            1,
            165,
            [
                (32, "Ending Balance"),
                (166, "$0.00"),
                (212, "Sample Platinum Check Card (...3176) Subtotal"),
                (655, "-$105.50"),
            ],
        ),
        _line(1, 190, [(212, "SAMPLE GOLD CHECK CARD (ACCOUNT ...4410)")]),
        _line(1, 203, _CARD_HEADER),
        _line(
            1,
            216,
            [(212, "6/14"), (266, "6/16"), (320, "EXAMPLE BAKERY, SAMPLETOWN"), (668, "-7.25")],
        ),
        _line(
            1,
            229,
            [
                (212, "6/20"),
                (266, "6/22"),
                (320, "EXAMPLE OUTFITTERS 12, SAMPLETOWN"),
                (668, "18.40"),
            ],
        ),
        _line(1, 242, [(212, "Sample Gold Check Card (...4410) Subtotal"), (660, "$11.15")]),
        _line(1, 255, [(212, "Total Debit Card Activity"), (655, "-$94.35")]),
    ]
    collected = _read(lines)
    rows = _activity(collected)
    assert [(a.trade_date, a.type, a.amount, a.section) for a in rows] == [
        (date(2026, 6, 3), "withdrawal", Decimal("-60.00"), "Debit Card Activity"),
        (date(2026, 6, 9), "withdrawal", Decimal("-45.50"), "Debit Card Activity"),
        # The second card's table is the section's too (its heading is worded
        # like the first card's), and a refund on it is money in.
        (date(2026, 6, 14), "withdrawal", Decimal("-7.25"), "Debit Card Activity"),
        (date(2026, 6, 20), "deposit", Decimal("18.40"), "Debit Card Activity"),
    ]
    assert rows[0].memo == "ATM0001 1 EXAMPLE RD, SAMPLETOWN"
    # The box's lines are no activity; the section's own total is kept.
    assert [(t.label, t.amount) for t in collected.totals] == [
        ("Total Debit Card Activity", Decimal("-94.35"))
    ]


def test_margin_interest_is_a_fee_dated_by_its_period() -> None:
    lines = [
        _line(1, 100, [(32, "Margin Activity")]),
        _line(1, 113, [(189, "Interest"), (268, "Average"), (549, "Interest"), (628, "Average")]),
        _line(
            1,
            122,
            [
                (32, "Period"),
                (100, "Period Balance"),
                (196, "Rate"),
                (240, "Daily Balance"),
                (320, "Interest Paid"),
                (392, "Period"),
                (460, "Period Balance"),
                (556, "Rate"),
                (600, "Daily Balance"),
                (680, "Interest Paid"),
            ],
        ),
        _line(
            1,
            135,
            [
                (32, f"6/1{EN_DASH}6/30"),
                (110, "12,400"),
                (191, "2.000%"),
                (250, "$12,150"),
                (327, f"{MINUS}$20.25"),
                (392, "Total this period"),
                (687, f"{MINUS}$20.25"),
            ],
        ),
        _line(1, 148, [(392, "Total year-to-date"), (687, f"{MINUS}118.40")]),
    ]
    collected = _read(lines)
    (row,) = _activity(collected)
    assert (row.trade_date, row.type, row.amount) == (date(2026, 6, 30), "fee", Decimal("-20.25"))
    assert (row.memo, row.section) == ("Margin interest", "Margin Activity")
    assert [t.label for t in collected.totals] == ["Total this period", "Total year-to-date"]


def test_a_margin_marker_before_a_holdings_name_is_dropped() -> None:
    lines = [
        _line(1, 100, [(32, "Stocks")]),
        _line(
            1,
            113,
            [
                (32, "Description"),
                (280, "Quantity"),
                (340, "Price Per Unit"),
                (430, "Ending Market Value"),
                (540, "Total Cost Basis"),
            ],
        ),
        # "M" ends where the description column starts: a marker, not the name.
        _line(
            1,
            126,
            [
                (24, "M EXAMPLE CORP DEP SHS (ZZPRM)"),
                (288, "300.00"),
                (346, "25.84"),
                (440, "7,752.00"),
                (550, "6,895.40"),
            ],
        ),
        # A name that starts with a single letter starts in the column.
        _line(
            1,
            139,
            [
                (32, "A EXAMPLE CLASS SHARES (ZZACL)"),
                (288, "10.00"),
                (346, "50.00"),
                (440, "500.00"),
                (550, "450.00"),
            ],
        ),
    ]
    collected = _read(lines)
    assert [(h.symbol, h.name) for h in collected.holdings] == [
        ("ZZPRM", "EXAMPLE CORP DEP SHS"),
        ("ZZACL", "A EXAMPLE CLASS SHARES"),
    ]


_SECURITY_HEADER: list[tuple[float, str]] = [
    (32, "Date"),
    (72, "Security Name"),
    (240, "Symbol/CUSIP"),
    (300, "Transaction Description"),
    (430, "Quantity"),
    (500, "Price"),
    (640, "Transaction Total"),
]


def test_income_and_trades_are_named_by_what_the_row_prints() -> None:
    lines = [
        _line(1, 100, [(32, "Securities Bought & Sold")]),
        _line(1, 113, _SECURITY_HEADER),
        # A bond with nothing in the symbol column: its issuer is no ticker.
        _line(
            1,
            126,
            [
                (32, "6/18"),
                (72, "EXAMPLE RESORTS INTERNATIONAL"),
                (300, "You bought"),
                (440, "5000"),
                (495, "101.00000"),
                (665, "-5,050.00"),
            ],
        ),
        _line(1, 139, [(32, "Total Securities Bought"), (655, "-$5,050.00")]),
        _line(1, 170, [(32, "Dividends, Interest & Other Income")]),
        _line(1, 183, _SECURITY_HEADER),
        _line(
            1,
            196,
            [
                (32, "6/12"),
                (72, "EXAMPLE GAS PARTNERS LP"),
                (240, "ZZGP"),
                (300, "Return of capital"),
                (675, "$79.05"),
            ],
        ),
    ]
    bought, income = _activity(_read(lines))
    assert bought.type == "buy"
    assert bought.security is not None
    assert (bought.security.symbol, bought.security.name) == (
        None,
        "EXAMPLE RESORTS INTERNATIONAL",
    )
    # Money a holding paid out is income of the account, not a deposit into it.
    assert (income.type, income.amount) == ("dividend", Decimal("79.05"))
    assert income.memo == "EXAMPLE GAS PARTNERS LP (Return of capital)"
    assert income.security is not None
    assert income.security.symbol == "ZZGP"


# ---------------------------------------------------------------------------
# Which rows a total sums
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("total", "heading", "scope"),
    [
        ("total deposits eft in", "deposits", "all"),
        ("total dividends interest other income", "dividends interest other income", "all"),
        ("net securities bought sold", "securities bought sold", "all"),
        ("total securities bought", "securities bought sold", "buy"),
        ("total securities sold", "securities bought sold", "sell"),
        ("total purchases", "", "buy"),
        ("total", "", "table"),
        ("total this period", "margin activity", "table"),
        # A part of the section its words alone do not pick out.
        ("total federal taxes withheld", "taxes withheld", None),
        ("total year to date", "margin activity", None),
        ("total fees", "deposits", None),
    ],
)
def test_the_rows_a_total_line_sums(total: str, heading: str, scope: str | None) -> None:
    assert _total_scope(total, heading) == scope


# ---------------------------------------------------------------------------
# Which rows a failed check leaves in doubt
# ---------------------------------------------------------------------------


def _row(
    kind: InvestmentActivityType, amount: str, section: str, **more: object
) -> InvestmentActivity:
    security = SecurityRef(symbol="ZZETF") if kind in ("buy", "sell", "transfer_in") else None
    quantity = Decimal(1) if security is not None else None
    return InvestmentActivity.model_validate(
        {
            "type": kind,
            "trade_date": date(2026, 6, 10),
            "amount": Decimal(amount),
            "section": section,
            "security": security,
            "quantity": quantity,
            **more,
        }
    )


def _figure(label: str, amount: str | None, role: str = "item") -> StatementFigure:
    return StatementFigure.model_validate(
        {
            "group": "cash_flow",
            "label": label,
            "role": role,
            "amount": Decimal(amount) if amount is not None else None,
        }
    )


def _statement(
    rows: list[InvestmentActivity],
    *,
    opening: str = "1000.00",
    closing: str,
    figures: list[StatementFigure] | None = None,
    positions: list[InvestmentPosition] | None = None,
    holdings: str | None = None,
    value: str | None = None,
) -> InvestmentStatement:
    return InvestmentStatement(
        source_format="pdf",
        activity=rows,
        positions=positions or [],
        cash_opening=Balance(amount=Decimal(opening)),
        cash_closing=Balance(amount=Decimal(closing)),
        positions_value=Balance(amount=Decimal(holdings)) if holdings is not None else None,
        total_value=Balance(amount=Decimal(value)) if value is not None else None,
        figures=figures or [],
    )


def _section(label: str, rows: list[int], printed: str, computed: str) -> SectionCheck:
    difference = Decimal(computed) - Decimal(printed)
    return SectionCheck(
        label=label,
        rows=rows,
        status="ok" if difference == 0 else "mismatch",
        printed=Decimal(printed),
        computed=Decimal(computed),
        difference=difference,
        source="total",
    )


def test_only_the_section_that_does_not_add_up_is_in_doubt() -> None:
    rows = [
        _row("deposit", "200.00", "Deposits"),
        _row("deposit", "50.00", "Deposits"),
        _row("withdrawal", "-80.00", "Checking Activity"),
        _row("withdrawal", "-40.00", "Checking Activity"),
        _row("dividend", "12.00", "Dividends"),
    ]
    statement = _statement(rows, closing="1112.00")  # 30.00 of checks unaccounted for
    check = localise(
        statement,
        reconcile_investment(statement),
        [
            _section("Total Deposits", [0, 1], "250.00", "250.00"),
            _section("Total Checking Activity", [2, 3], "-150.00", "-120.00"),
            _section("Total Dividends", [4], "12.00", "12.00"),
        ],
    )
    assert check.failed == ["cash"]
    assert check.doubtful_activity == [2, 3]
    assert check.doubtful_positions == []


def test_one_row_too_many_in_a_section_is_the_only_row_in_doubt() -> None:
    rows = [
        _row("deposit", "200.00", "Deposits"),
        _row("deposit", "77.77", "Deposits"),
        _row("deposit", "50.00", "Deposits"),
    ]
    statement = _statement(rows, closing="1250.00")
    check = localise(
        statement,
        reconcile_investment(statement),
        [_section("Total Deposits", [0, 1, 2], "250.00", "327.77")],
    )
    assert check.doubtful_activity == [1]


def test_every_section_adds_up_and_the_cash_does_not() -> None:
    """A row its section's total counts but the cash does not: that row, else all."""
    rows = [
        _row("deposit", "200.00", "Deposits"),
        _row("buy", "-77.77", "Securities Bought & Sold"),
        _row("buy", "-100.00", "Securities Bought & Sold"),
    ]
    sections = [
        _section("Total Deposits", [0], "200.00", "200.00"),
        _section("Total Securities Bought", [1, 2], "-177.77", "-177.77"),
    ]
    forged = _statement(rows, closing="1100.00")
    assert localise(forged, reconcile_investment(forged), sections).doubtful_activity == [1]
    # No single row is the difference: nothing says which rows to trust.
    unexplained = _statement(rows, closing="1090.00")
    assert localise(unexplained, reconcile_investment(unexplained), sections).doubtful_activity == [
        0,
        1,
        2,
    ]


def test_cash_flow_the_statement_names_and_no_row_was_read_for() -> None:
    """The statement's own cash-flow summary says what is missing: the rows read stand."""
    rows = [_row("deposit", "200.00", "Deposits")]
    statement = _statement(
        rows,
        closing="1179.75",
        figures=[
            _figure("Beginning Core Account Balance", "1000.00", "opening"),
            _figure("Deposits", "200.00"),
            _figure("Margin Interest", "-20.25"),
            _figure("Ending Core Account Balance", "1179.75", "closing"),
        ],
    )
    check = localise(
        statement,
        reconcile_investment(statement),
        [_section("Total Deposits", [0], "200.00", "200.00")],
    )
    assert check.failed == ["cash"]
    assert check.summary_cash_difference == Decimal("0.00")
    assert check.unread_summary_lines == ["Margin Interest"]
    assert check.doubtful_activity == []


def test_cash_the_summary_does_not_list_is_in_doubt() -> None:
    """Value moved in from another account under *Securities Transferred In*
    that the statement's cash flow does not count as cash."""
    rows = [
        _row("deposit", "200.00", "Deposits"),
        _row("deposit", "3950.00", "Securities Transferred In"),
        _row("transfer_in", "0", "Securities Transferred In"),
    ]
    figures = [
        _figure("Beginning Core Account Balance", "1000.00", "opening"),
        _figure("Deposits", "200.00"),
        _figure("Ending Core Account Balance", "1200.00", "closing"),
    ]
    statement = _statement(rows, closing="1200.00", figures=figures)
    check = localise(
        statement,
        reconcile_investment(statement),
        [
            _section("Total Deposits", [0], "200.00", "200.00"),
            _section("Total Securities Transferred In", [1, 2], "4979.60", "4979.60"),
        ],
    )
    assert check.failed == ["cash"]
    # Its own section's total agrees, the cash flow has no line for it; the
    # units moved beside it move no cash and are not in doubt.
    assert check.doubtful_activity == [1]
    # A summary that lists transfers vouches for the same row.
    listed = _statement(
        rows,
        closing="5150.00",
        figures=[
            figures[0],
            figures[1],
            _figure("Transfers In", "3950.00"),
            _figure("Ending Core Account Balance", "5150.00", "closing"),
        ],
    )
    assert reconcile_investment(listed).status == "ok"


def test_a_summary_line_vouches_where_the_section_prints_no_total() -> None:
    rows = [
        _row("fee", "-20.25", "Margin Activity"),
        _row("withdrawal", "-80.00", "Checking Activity"),
    ]
    statement = _statement(
        rows,
        closing="850.00",  # 49.75 short of what the rows say
        figures=[
            _figure("Beginning Core Account Balance", "1000.00", "opening"),
            _figure("Checking Activity", "-129.75"),
            _figure("Margin Interest", "-20.25"),
            _figure("Ending Core Account Balance", "850.00", "closing"),
        ],
    )
    check = localise(statement, reconcile_investment(statement))
    by_label = {s.label: s for s in check.sections}
    assert (by_label["Margin Interest"].status, by_label["Margin Interest"].source) == (
        "ok",
        "summary",
    )
    assert by_label["Checking Activity"].status == "mismatch"
    assert by_label["Checking Activity"].difference == Decimal("49.75")
    assert check.doubtful_activity == [1]


def _position(value: str, *, price: str = "10.00") -> InvestmentPosition:
    return InvestmentPosition(
        security=SecurityRef(symbol="ZZETF"),
        quantity=Decimal(10),
        unit_price=Decimal(price),
        market_value=Decimal(value),
    )


def test_positions_are_in_doubt_only_when_no_figure_vouches_for_them() -> None:
    rows = [_row("deposit", "200.00", "Deposits")]
    good = [_position("100.00"), _position("100.00")]
    # The account value alone is wrong: the holdings total vouches for the positions.
    value = _statement(rows, closing="1200.00", positions=good, holdings="200.00", value="1500.00")
    check = localise(value, reconcile_investment(value))
    assert check.failed == ["total_value"]
    assert (check.doubtful_activity, check.doubtful_positions) == ([], [])
    # No holdings total printed: nothing says the positions are all there.
    bare = _statement(rows, closing="1200.00", positions=good, value="1500.00")
    assert localise(bare, reconcile_investment(bare)).doubtful_positions == [0, 1]
    # One position's value is not quantity x price: that one.
    wrong = [_position("100.00"), _position("190.00")]
    one = _statement(rows, closing="1200.00", positions=wrong, holdings="290.00", value="1490.00")
    check = localise(one, reconcile_investment(one))
    assert check.failed == ["positions"]
    assert check.doubtful_positions == [1]
    # They do not add up to the printed holdings total: all of them.
    short = _statement(rows, closing="1200.00", positions=good, holdings="350.00", value="1550.00")
    assert localise(short, reconcile_investment(short)).doubtful_positions == [0, 1]


def test_a_statement_that_reconciles_keeps_its_sections_and_doubts_nothing() -> None:
    rows = [_row("deposit", "200.00", "Deposits")]
    statement = _statement(rows, closing="1200.00")
    check = localise(
        statement,
        reconcile_investment(statement),
        [_section("Total Deposits", [0], "200.00", "200.00")],
    )
    assert check.status == "ok"
    assert [s.label for s in check.sections] == ["Total Deposits"]
    assert (check.doubtful_activity, check.doubtful_positions) == (None, None)


# ---------------------------------------------------------------------------
# The synthetic reports, end to end through the parser
# ---------------------------------------------------------------------------


def _parse(data: bytes) -> ExtractResult:
    result = run_extraction("pdf", data, options=ExtractOptions())
    assert result.ok, result.error
    return parse_extraction(result)


@pytest.fixture(scope="module")
def report() -> ExtractResult:
    return _parse(fidr.report_pdf())


def test_every_section_of_the_report_is_checked_against_its_total(report: ExtractResult) -> None:
    individual, ira, _ = report.investments
    for statement in (individual, ira):
        check = statement.reconciliation
        assert check is not None
        assert check.status == "ok"
        assert check.sections
        assert {s.status for s in check.sections} == {"ok"}
        # Every row is under a heading, and a total of the statement covers it.
        assert all(a.section for a in statement.activity)
        covered = {i for s in check.sections for i in s.rows}
        assert covered == set(range(len(statement.activity)))
        # Its own cash-flow summary bridges opening to closing cash and
        # names nothing that was not read.
        assert check.summary_cash_difference == Decimal("0.00")
        assert check.unread_summary_lines == []
    labels = {s.label for s in individual.reconciliation.sections}  # type: ignore[union-attr]
    assert {
        "Total Securities Bought",
        "Total Securities Sold",
        "Net Securities Bought & Sold",
        "Total Deposits & EFT In",
        "Total Checking Activity",
        "Total Debit Card Activity",
        "Total Taxes Withheld",
    } <= labels


def test_the_reports_summary_tables_are_read_as_figures(report: ExtractResult) -> None:
    individual, _, education = report.investments
    by_group: dict[str, list[StatementFigure]] = {}
    for figure in individual.figures:
        by_group.setdefault(figure.group, []).append(figure)
    assert set(by_group) == {"account_value", "cash_flow", "income"}
    # The account-value bridge, from the beginning value to the ending value.
    value = by_group["account_value"]
    assert (value[0].label, value[0].role) == ("Beginning Account Value", "opening")
    assert (value[-1].label, value[-1].role) == ("Ending Account Value", "closing")
    assert individual.total_value is not None
    assert value[-1].amount == individual.total_value.amount
    # Indented lines break the line above them down and are not added again.
    assert {f.label for f in value if f.detail} >= {"Deposits", "Withdrawals"}
    # The cash flow: the core position's opening and closing balance and each line.
    flow = by_group["cash_flow"]
    assert individual.cash_opening is not None
    assert individual.cash_closing is not None
    assert (flow[0].role, flow[0].amount) == ("opening", individual.cash_opening.amount)
    assert (flow[-1].role, flow[-1].amount) == ("closing", individual.cash_closing.amount)
    assert {"Total Investment Activity", "Total Cash Management Activity"} <= {
        f.label for f in flow if f.role == "total"
    }
    # The income summary printed beside it is read on its own.
    income = by_group["income"]
    assert (income[0].label, income[-1].label, income[-1].role) == ("Taxable", "Total", "total")
    # None of these lines is activity.
    assert not {f.label for f in individual.figures} & {a.memo for a in individual.activity}
    # A 529 prints only the account-value bridge.
    assert {f.group for f in education.figures} == {"account_value"}


@pytest.mark.parametrize("tampered", [0, 1, 2])
def test_a_misprinted_row_alone_is_in_doubt(tampered: int) -> None:
    stmt = fid.fidelity_statement()
    result = _parse(fid.fidelity_pdf(stmt, tamper="misprinted_row", tamper_account=tampered))
    statement = result.investments[tampered]
    check = statement.reconciliation
    assert check is not None
    assert (check.status, check.failed) == ("mismatch", ["cash"])
    assert check.cash_difference == fid.MISPRINT
    (wrong,) = [s for s in check.sections if s.status == "mismatch"]
    assert (wrong.label, wrong.difference) == ("Total Deposits", fid.MISPRINT)
    (index,) = wrong.rows
    assert check.doubtful_activity == [index]
    assert check.doubtful_positions == []
    for number, row in enumerate(statement.activity):
        assert ("reconciliation" in row.low_confidence) == (number == index)
    assert all(p.low_confidence == [] for p in statement.positions)
    # The other accounts of the report are untouched.
    for other, untouched in enumerate(result.investments):
        if other != tampered:
            assert untouched.reconciliation is not None
            assert untouched.reconciliation.status == "ok"
