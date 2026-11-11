# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statements of several accounts: headings, the account index, one statement per account.

``app.ingestion.parsers.accounts`` and the statement parsers that use it, on
the two synthetic multi-account layouts (``tests/fixtures/synth_fidelity_report.py``:
a three-account brokerage report; ``tests/fixtures/synth_neutral_report.py``:
checking, savings and brokerage in one combined statement), on small drawn
documents and on hand-built lines. Everything is invented.
"""

from __future__ import annotations

import io
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

from app.domain.accounts.models import AccountType
from app.domain.imports import proposals
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.accounts import (
    AccountInfo,
    account_kind,
    number_last4,
    read_heading,
    read_index,
    segment,
)
from app.ingestion.parsers.base import Cell, DocumentView, ViewLine
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from tests.fixtures import synth_fidelity_report as fidr
from tests.fixtures import synth_neutral_report as neutral

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _extract(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return result


@pytest.fixture(scope="module")
def fid_parsed() -> ExtractResult:
    return parse_extraction(_extract(fidr.report_pdf()), threshold=0.85)


@pytest.fixture(scope="module")
def neutral_parsed() -> ExtractResult:
    return parse_extraction(_extract(neutral.report_pdf()), threshold=0.85)


# ---------------------------------------------------------------------------
# The Fidelity-layout report: three brokerage accounts
# ---------------------------------------------------------------------------

#: Each account's pages: its first page is the one naming it beside a dollar
#: amount (the index prints other page numbers); the endnotes page after the
#: last account has no heading and stays with it.
FID_PAGES = ((4, 12), (13, 17), (18, 19))
FID_KINDS = (
    ("Individual - TOD", "brokerage", "individual"),
    ("Traditional IRA", "retirement", "traditional_ira"),
    ("Education Account", "brokerage", "education_529"),
)


def test_fidelity_report_is_one_statement_per_account(fid_parsed: ExtractResult) -> None:
    stmt, _ = fidr.report()
    assert fid_parsed.parse is not None
    assert not fid_parsed.statements
    assert len(fid_parsed.investments) == 3
    for statement, account, pages, kind in zip(
        fid_parsed.investments, stmt.accounts, FID_PAGES, FID_KINDS, strict=True
    ):
        assert statement.account.last4 == account.last4
        assert (statement.account.name, statement.account.account_type) == kind[:2]
        assert statement.account.subtype == kind[2]
        assert (statement.first_page, statement.last_page) == pages
        assert (statement.period_start, statement.period_end) == (stmt.start, stmt.end)
        # Values are the account's own (not the next account's, nor a change in value).
        assert statement.total_value is not None
        assert statement.total_value.amount == account.total_value
        # No position of another account's; every position on its own pages.
        others = {
            key
            for other in stmt.accounts
            if other is not account
            for p in other.positions
            for key in (p.symbol, p.cusip)
            if key
        } - {key for p in account.positions for key in (p.symbol, p.cusip)}
        for position in statement.positions:
            assert position.security.symbol not in others, (account.last4, position.security)
            assert position.security.cusip not in others, (account.last4, position.security)
            assert position.page is not None
            assert pages[0] <= position.page <= pages[1]
    # No owner's name in the account names, no full account number anywhere.
    for statement in fid_parsed.investments:
        assert "Sample" not in (statement.account.name or "")
    dumped = fid_parsed.model_dump_json(include={"investments", "statements", "parse"})
    for account in stmt.accounts:
        assert account.number not in dumped


def test_the_529_is_its_own_statement(fid_parsed: ExtractResult) -> None:
    stmt, _ = fidr.report()
    education = fid_parsed.investments[2]
    expected = stmt.accounts[2]
    assert education.account.last4 == expected.last4
    # The plan's portfolios print no symbol: each is named by its name alone.
    assert sorted(p.security.name or "" for p in education.positions) == sorted(
        p.key for p in expected.positions
    )
    assert all(not p.security.identified for p in education.positions)
    ira = fid_parsed.investments[1]
    assert not {p.security.name for p in ira.positions} & {p.key for p in expected.positions}
    assert education.reconciliation is not None
    assert education.reconciliation.status == "ok"


def test_the_index_page_numbers_are_not_used() -> None:
    view = DocumentView.from_result(_extract(fidr.report_pdf()))
    found = segment(view.lines)
    assert [a.last4 for a in found.accounts] == [a.last4 for a in fidr.report()[0].accounts]
    assert [(a.first_page, a.last_page) for a in found.accounts] == list(FID_PAGES)
    # The overview pages (portfolio summary, the index, top holdings) are no account's.
    assert sorted({line.page for line in found.preamble}) == [1, 2, 3]
    assert set(found.index) == {a.last4 for a in found.accounts}


# ---------------------------------------------------------------------------
# The provider-neutral combined statement: checking, savings, brokerage
# ---------------------------------------------------------------------------


def test_combined_statement_gives_bank_and_brokerage_statements(
    neutral_parsed: ExtractResult,
) -> None:
    stmt = neutral.report()
    expected = {e.last4: e for e in neutral.expected(stmt)}
    accounts = {a.last4: a for a in stmt.accounts}
    assert neutral_parsed.parse is not None
    assert neutral_parsed.parse.parser is not None
    assert neutral_parsed.parse.parser.name == "brokerage_statement_generic"
    bank = {s.account.last4 or "": s for s in neutral_parsed.statements}
    assert list(bank) == ["4821", "5530"]
    assert [i.account.last4 for i in neutral_parsed.investments] == ["6619"]
    assert (bank["4821"].account.name, bank["4821"].account.account_type) == (
        "Everyday Checking",
        "checking",
    )
    assert (bank["5530"].account.name, bank["5530"].account.account_type) == (
        "Statement Savings",
        "savings",
    )
    assert (bank["4821"].first_page, bank["4821"].last_page) == (2, 4)
    assert (bank["5530"].first_page, bank["5530"].last_page) == (4, 4)
    for last4, statement in bank.items():
        assert statement.opening_balance is not None
        assert statement.ledger_balance is not None
        assert statement.opening_balance.amount == expected[last4].opening
        assert statement.ledger_balance.amount == expected[last4].closing
        assert (statement.period_start, statement.period_end) == (neutral.START, neutral.END)
        # The rows of its own Transaction Detail and Checks Paid tables (a
        # check paid as ``CHECK <number>``, money out), nothing of another
        # account's; each account reconciles.
        rows = [(t.posted, t.amount, t.payee) for t in statement.transactions]
        assert rows == expected[last4].bank_rows
        assert statement.reconciliation is not None
        assert statement.reconciliation.status == "ok"
    assert [a.section for a in accounts["4821"].activity].count("checks") == 2
    brokerage = neutral_parsed.investments[0]
    assert (brokerage.account.name, brokerage.account.account_type) == (
        "Self-Directed Brokerage",
        "brokerage",
    )
    assert (brokerage.first_page, brokerage.last_page) == (4, 6)
    assert brokerage.total_value is not None
    assert brokerage.total_value.amount == expected["6619"].total_value
    positions = sorted(
        (p.security.symbol or "", p.quantity, p.unit_price, p.market_value)
        for p in brokerage.positions
    )
    assert positions == expected["6619"].positions
    # Trade Activity and the Cash Activity table under it (a cash table of a
    # brokerage account: typed by its description), checked against the cash
    # the Account Value Summary prints for this period and the last.
    activity = sorted(
        (
            a.trade_date,
            a.type,
            a.security.symbol if a.security else None,
            a.quantity,
            a.amount,
        )
        for a in brokerage.activity
    )
    assert activity == sorted(expected["6619"].activity)
    assert brokerage.cash_opening is not None
    assert brokerage.cash_closing is not None
    assert brokerage.cash_opening.amount == expected["6619"].opening
    assert brokerage.cash_closing.amount == expected["6619"].closing
    assert brokerage.reconciliation is not None
    assert brokerage.reconciliation.status == "ok"
    assert brokerage.reconciliation.checks == ["positions", "holdings_total", "cash", "total_value"]


def _bank_page(
    c: canvas.Canvas,
    heading: list[str],
    rows: list[tuple[str, str, str]],
    wrapped: dict[int, str] | None = None,
) -> None:
    """One page of a bank statement; ``wrapped``: a row's description continued below it."""
    y = 750.0
    c.setFont("Helvetica", 9)
    for text in ["SAMPLE COMMUNITY TEST BANK", "Statement Period: 01/01/2026 - 01/31/2026"]:
        c.drawString(72, y, text)
        y -= 14
    for text in heading:
        c.drawString(72, y, text)
        y -= 14
    for x, text in ((72, "Date"), (150, "Description"), (480, "Amount")):
        c.drawString(x, y, text)
    y -= 14
    for number, (day, text, amount) in enumerate(rows):
        c.drawString(72, y, day)
        c.drawString(150, y, text)
        c.drawRightString(520, y, amount)
        y -= 14
        if wrapped and number in wrapped:
            c.drawString(150, y, wrapped[number])
            y -= 14
    c.drawString(72, 60, "Synthetic test document from the PerFin test suite.")
    c.showPage()


def _two_bank_accounts() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _bank_page(
        c,
        ["Basic Checking", "Account Number: XXXX-1357", "Beginning Balance $500.00"],
        [("01/03", "PAYROLL EXAMPLE CO", "1,200.00"), ("01/09", "SAMPLE GROCER", "-80.00")],
    )
    _bank_page(
        c,
        ["Ending Balance $1,520.00"],
        [("01/20", "TRANSFER TO SAVINGS XXXX-2468", "-100.00")],
    )
    _bank_page(
        c,
        ["High Yield Savings", "Account Number: XXXX-2468", "Beginning Balance $50.00"],
        [("01/20", "TRANSFER FROM CHECKING XXXX-1357", "100.00")],
    )
    c.save()
    return buf.getvalue()


def test_bank_statement_of_two_accounts_is_two_statements() -> None:
    result = parse_extraction(_extract(_two_bank_accounts()), threshold=0.85)
    assert result.parse is not None
    assert result.parse.parser is not None
    assert result.parse.parser.name == "statement_generic"
    assert [s.account.last4 for s in result.statements] == ["1357", "2468"]
    checking, savings = result.statements
    assert [t.amount for t in checking.transactions] == [
        Decimal("1200.00"),
        Decimal("-80.00"),
        Decimal("-100.00"),
    ]
    assert [t.amount for t in savings.transactions] == [Decimal("100.00")]
    assert (checking.first_page, checking.last_page) == (1, 2)
    assert (savings.first_page, savings.last_page) == (3, 3)
    assert checking.opening_balance is not None
    assert checking.opening_balance.amount == Decimal("500.00")
    assert checking.reconciliation is not None
    assert checking.reconciliation.status == "ok"
    assert savings.opening_balance is not None
    assert savings.opening_balance.amount == Decimal("50.00")
    assert (savings.period_start, savings.period_end) == (date(2026, 1, 1), date(2026, 1, 31))


def test_one_bank_account_is_read_as_before() -> None:
    """A heading naming one account changes nothing: one statement, no pages."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _bank_page(
        c,
        ["Basic Checking", "Account Number: XXXX-1357", "Beginning Balance $500.00"],
        [("01/03", "PAYROLL EXAMPLE CO", "1,200.00"), ("01/09", "TRANSFER TO XXXX-2468", "-80.00")],
    )
    c.save()
    result = parse_extraction(_extract(buf.getvalue()), threshold=0.85)
    assert [s.account.last4 for s in result.statements] == ["1357"]
    assert result.statements[0].first_page is None
    assert result.statements[0].account.name is None


def test_a_wrapped_transfer_description_does_not_split_a_bank_statement() -> None:
    """A description continued near a page's bottom naming another account is no heading."""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=letter)
    _bank_page(
        c,
        ["Basic Checking", "Account Number: XXXX-1357", "Beginning Balance $500.00"],
        [("01/03", "PAYROLL EXAMPLE CO", "1,200.00"), ("01/09", "ONLINE TRANSFER", "-80.00")],
        wrapped={1: "TO SAVINGS ACCOUNT XXXX-2468"},
    )
    _bank_page(
        c,
        ["Ending Balance $1,570.00"],
        [("01/20", "SAMPLE GROCER", "-50.00")],
        wrapped={0: "Account XXXX-2468"},
    )
    c.save()
    result = parse_extraction(_extract(buf.getvalue()), threshold=0.85)
    (statement,) = result.statements
    assert statement.account.last4 == "1357"
    assert [t.amount for t in statement.transactions] == [
        Decimal("1200.00"),
        Decimal("-80.00"),
        Decimal("-50.00"),
    ]
    assert statement.reconciliation is not None
    assert statement.reconciliation.status == "ok"


# ---------------------------------------------------------------------------
# Headings, the index and segments on hand-built lines
# ---------------------------------------------------------------------------


def _lines(*pages: Sequence[Sequence[tuple[float, str]]]) -> list[ViewLine]:
    out: list[ViewLine] = []
    for number, page in enumerate(pages, start=1):
        for y, cells in enumerate(page):
            built = tuple(
                Cell(text, x, y * 12.0, x + 5.0 * len(text), y * 12.0 + 9) for x, text in cells
            )
            out.append(
                ViewLine(
                    page=number,
                    index=len(out),
                    cells=built,
                    text=" ".join(text for _, text in cells),
                    height=9.0,
                )
            )
    return out


def _filler(count: int) -> list[list[tuple[int, str]]]:
    return [[(40, f"Body text line {n} of the synthetic page")] for n in range(count)]


@pytest.mark.parametrize(
    ("text", "last4", "title"),
    [
        ("Account 418-205817", "5817", None),
        ("INDIVIDUAL - TOD Account # Z29-104821", "4821", "INDIVIDUAL - TOD"),
        ("HEALTH SAVINGS ACCOUNT 246-556074", "6074", "HEALTH SAVINGS"),
        ("ROTH IRA Z30-118293", "8293", "ROTH IRA"),
        ("Trans. from account 418-330642", None, None),
        ("Account Value: $62,362.06", None, None),
        # Transaction descriptions naming another account.
        ("TO SAVINGS ACCOUNT XXXX-2468", None, None),
        ("ONLINE TRANSFER TO SAVINGS ACCOUNT XXXXXX2468", None, None),
        ("ACH DEBIT 12-3456789", None, None),
        ("ONLINE TRANSFER TO ACCOUNT XXXX-2468", None, None),
        ("TRANSFER FROM CHK 1234-5678", None, None),
        # Product names holding words a description also uses.
        ("CERTIFICATE OF DEPOSIT Account Number XXXX-5678", "5678", "CERTIFICATE OF DEPOSIT"),
        ("TIME DEPOSIT Account 1234-5678", "5678", "TIME DEPOSIT"),
        ("ONLINE SAVINGS Account XXXX-1234", "1234", "ONLINE SAVINGS"),
        (
            "TRANSFER ON DEATH INDIVIDUAL Account # Z12-345678",
            "5678",
            "TRANSFER ON DEATH INDIVIDUAL",
        ),
        (
            "UNIFORM TRANSFERS TO MINORS ACT Account 123-456789",
            "6789",
            "UNIFORM TRANSFERS TO MINORS ACT",
        ),
        # Numbers that are no account's: a telephone number, a date.
        ("Account # 1-800-555-0100", None, None),
        ("Questions about your account? 888-555-0100", None, None),
        ("Account No. 2026-07-31", None, None),
        ("Account Number 07-31-2026", None, None),
    ],
)
def test_read_heading(text: str, last4: str | None, title: str | None) -> None:
    found = read_heading(text)
    assert (found.last4 if found else None) == last4
    assert (found.title if found else None) == title


def test_a_heading_beside_an_amount_starts_its_account() -> None:
    lines = _lines(
        [[(40, "Your Portfolio Value: $1,500.00")], *_filler(3)],
        [[(40, "$1,000.00"), (470, "Account 111-111111")], *_filler(10)],
        [[(40, "Holdings"), (470, "Account 111-111111")], *_filler(10)],
        [[(40, "Account Value:"), (260, "$500.00"), (470, "Account 222-222222")], *_filler(4)],
        _filler(6),
    )
    found = segment(lines)
    assert [(a.last4, a.first_page, a.last_page) for a in found.accounts] == [
        ("1111", 2, 3),
        ("2222", 4, 5),
    ]
    assert {line.page for line in found.preamble} == {1}


def test_mentions_of_another_account_do_not_switch_the_page() -> None:
    page = [
        [(40, "Activity"), (470, "Account 111-111111")],
        [(40, "Date"), (80, "Description"), (480, "Amount")],
        # A transfer row in the header band and a wrapped line under it.
        [(40, "7/31"), (80, "Trans. from account 222-222222"), (480, "$5.00")],
        [(80, "Account 222-222222")],
        *_filler(12),
        [(40, "Account 222-222222")],  # mid-page, no index lists it
        *_filler(4),
    ]
    other = [[(40, "Holdings"), (470, "Account 222-222222")], *_filler(4)]
    found = segment(_lines(page, other))
    assert [(a.last4, a.first_page, a.last_page) for a in found.accounts] == [
        ("1111", 1, 1),
        ("2222", 2, 2),
    ]
    assert len(found.accounts[0].lines) == len(page)


def test_a_wrapped_description_naming_an_indexed_account_is_no_heading() -> None:
    """Transfers between the user's own accounts name accounts the index lists."""
    index = [
        [(40, "Account Name"), (220, "Account Number"), (400, "Closing Balance")],
        [(40, "Basic Checking"), (220, "XXXX-1357"), (400, "$1,520.00")],
        [(40, "Rainy Day Savings"), (220, "XXXX-2468"), (400, "$150.00")],
    ]
    page = [
        [(40, "Basic Checking")],
        [(40, "Account Number: XXXX-1357")],
        [(40, "Date"), (80, "Description"), (480, "Amount")],
        [(40, "01/05"), (80, "ONLINE TRANSFER"), (480, "-100.00")],
        [(80, "TO SAVINGS ACCOUNT XXXX-2468")],
        [(40, "01/07"), (80, "ONLINE TRANSFER FROM"), (480, "40.00")],
        [(80, "Account XXXX-2468")],  # no description word: the wrap decides
        [(40, "01/09"), (80, "SAMPLE GROCER"), (480, "-80.00")],
        *_filler(6),
    ]
    found = segment(_lines(index, page))
    assert [(a.last4, len(a.lines)) for a in found.accounts] == [("1357", len(page))]


def test_a_left_aligned_heading_after_a_table_row_still_counts() -> None:
    index = [
        [(40, "Account Name"), (220, "Account Number"), (400, "Closing Balance")],
        [(40, "Basic Checking"), (220, "XXXX-1357"), (400, "$1,520.00")],
        [(40, "Rainy Day Savings"), (220, "XXXX-2468"), (400, "$150.00")],
    ]
    page = [
        [(40, "Account Number: XXXX-1357")],
        [(40, "Date"), (80, "Description"), (480, "Amount")],
        [(40, "01/09"), (80, "SAMPLE GROCER"), (480, "-80.00")],
        [(40, "Account Number: XXXX-2468")],  # straight under the row, at the margin
        [(40, "Date"), (80, "Description"), (480, "Amount")],
        [(40, "01/12"), (80, "INTEREST PAID"), (480, "0.15")],
        *_filler(6),
    ]
    found = segment(_lines(index, page))
    assert [(a.last4, len(a.lines)) for a in found.accounts] == [("1357", 3), ("2468", 9)]


_CD_INDEX = [
    [(40, "Account Name"), (220, "Account Number"), (400, "Closing Balance")],
    [(40, "Basic Checking"), (220, "XXXX-1357"), (400, "$1,520.00")],
    [(40, "Certificate of Deposit"), (220, "XXXX-5678"), (400, "$5,000.00")],
]
_CHECKING_PAGE = [
    [(40, "BASIC CHECKING Account Number XXXX-1357")],
    [(40, "Date"), (80, "Description"), (480, "Amount")],
    [(40, "01/09"), (80, "SAMPLE GROCER"), (480, "-80.00")],
    *_filler(6),
]
_CD_PAGE = [
    [(40, "CERTIFICATE OF DEPOSIT Account Number XXXX-5678")],
    [(40, "Date"), (80, "Description"), (480, "Amount")],
    [(40, "01/31"), (80, "INTEREST"), (480, "4.10")],
    *_filler(6),
]


@pytest.mark.parametrize("indexed", [True, False])
def test_a_certificate_of_deposit_heading_starts_its_account(indexed: bool) -> None:
    """``DEPOSIT`` in a product's title does not make the heading a description."""
    pages = [_CD_INDEX] if indexed else []
    found = segment(_lines(*pages, _CHECKING_PAGE, _CD_PAGE))
    first = 2 if indexed else 1
    assert [(a.last4, len(a.lines), a.first_page) for a in found.accounts] == [
        ("1357", 9, first),
        ("5678", 9, first + 1),
    ]
    # Half-way down a page, the index listing it.
    page = [*_CHECKING_PAGE[:3], *_CD_PAGE]
    found = segment(_lines(_CD_INDEX, page))
    assert [(a.last4, len(a.lines)) for a in found.accounts] == [("1357", 3), ("5678", 9)]


def test_transfer_on_death_and_custodial_titles_are_headings() -> None:
    tod = [[(40, "TRANSFER ON DEATH INDIVIDUAL Account # Z12-345678")], *_filler(8)]
    utma = [[(40, "UNIFORM TRANSFERS TO MINORS ACT Account 123-456789")], *_filler(8)]
    ira = [[(40, "ROTH IRA Account # Z12-999999")], *_filler(8)]
    found = segment(_lines(tod, utma, ira))
    assert [(a.last4, a.info.name, a.first_page) for a in found.accounts] == [
        ("5678", "TRANSFER ON DEATH INDIVIDUAL", 1),
        ("6789", "UNIFORM TRANSFERS TO MINORS ACT", 2),
        ("9999", "ROTH IRA", 3),
    ]
    assert found.preamble == []


def test_a_heading_centred_under_a_table_row_still_counts() -> None:
    """Only a line starting at one of the row's text columns continues the row."""
    index = [
        [(40, "Account Name"), (220, "Account Number"), (400, "Closing Balance")],
        [(40, "Basic Checking"), (220, "XXXX-1357"), (400, "$1,520.00")],
        [(40, "Rainy Day Savings"), (220, "XXXX-2468"), (400, "$150.00")],
    ]
    page = [
        [(40, "Account Number: XXXX-1357")],
        [(40, "Date"), (80, "Description"), (480, "Amount")],
        [(40, "01/09"), (80, "SAMPLE GROCER"), (480, "-80.00")],
        [(200, "Account Number: XXXX-2468")],  # at no column of the row above
        [(40, "Date"), (80, "Description"), (480, "Amount")],
        [(40, "01/31"), (80, "INTEREST"), (480, "0.10")],
        *_filler(6),
    ]
    found = segment(_lines(index, page))
    assert [(a.last4, len(a.lines)) for a in found.accounts] == [("1357", 3), ("2468", 9)]


def test_a_footer_heading_right_under_the_last_row_counts() -> None:
    first = [
        [(40, "Account 111-111111")],
        *_filler(3),
        [(40, "Date"), (80, "Description"), (480, "Amount")],
        *[[(40, f"01/{day:02d}"), (80, "BUY"), (480, "-1.00")] for day in range(1, 8)],
    ]
    second = [
        [(40, "Page 2")],
        [(40, "Date"), (80, "Description"), (480, "Amount")],
        *[[(40, f"02/{day:02d}"), (80, "BUY"), (480, "-1.00")] for day in range(1, 8)],
        [(400, "Account 222-222222")],  # the page's footer, right-aligned
    ]
    found = segment(_lines(first, second))
    assert [(a.last4, a.first_page, a.last_page) for a in found.accounts] == [
        ("1111", 1, 1),
        ("2222", 2, 2),
    ]


def test_a_mid_page_heading_counts_when_the_index_lists_it() -> None:
    index = [
        [(40, "Account Name"), (220, "Account Number"), (400, "Closing Balance")],
        [(40, "Basic Checking"), (220, "XXXX-1357"), (400, "$1,520.00")],
        [(40, "Rainy Day Savings"), (220, "XXXX-2468"), (400, "$150.00")],
        [(40, "Total"), (400, "$1,670.00")],
    ]
    page = [
        [(40, "Basic Checking")],
        [(40, "Account Number: XXXX-1357")],
        *_filler(12),
        [(40, "Rainy Day Savings")],
        [(40, "Account Number: XXXX-2468")],
        *_filler(3),
    ]
    found = segment(_lines(index, page))
    assert [a.info for a in found.accounts] == [
        AccountInfo("1357", "Basic Checking", "checking", None),
        AccountInfo("2468", "Rainy Day Savings", "savings", None),
    ]
    # The title line above the second heading still ends the first account.
    assert [len(a.lines) for a in found.accounts] == [14, 4]
    # Without the index the second heading is a mid-page mention.
    alone = segment(_lines(page))
    assert [a.last4 for a in alone.accounts] == ["1357"]


def test_an_overview_of_accounts_with_amounts_is_no_heading() -> None:
    overview = [
        [(40, "Account # 111-111111"), (400, "$1,000.00")],
        [(40, "Account # 222-222222"), (400, "$2,000.00")],
        *_filler(3),
    ]
    first = [[(40, "Account # 111-111111")], *_filler(3)]
    found = segment(_lines(overview, first))
    assert [(a.last4, a.first_page) for a in found.accounts] == [("1111", 2)]
    assert {line.page for line in found.preamble} == {1}


def test_the_account_index() -> None:
    lines = _lines(
        [
            [(40, "Accounts Included in This Report")],
            [(40, "Page Account Type/Name"), (300, "Account Number"), (420, "Ending Value")],
            [(40, "GENERAL INVESTMENTS")],
            [
                (40, "3"),
                (66, "Alex Example - Individual - TOD"),
                (300, "Z11-100001"),
                (420, "$5.00"),
            ],
            [(40, "PERSONAL RETIREMENT")],
            [(40, "9"), (66, "Alex Example - Rollover IRA"), (300, "Z11-100002"), (420, "$6.00")],
            [(40, "EDUCATION (529) ACCOUNTS")],
            [(40, "12"), (66, "Alex Example - Education Account"), (300, "Z11-100003")],
            [(40, "Total Portfolio"), (420, "$11.00")],
            [(40, "Account Z11-100009 is not in this table")],
        ]
    )
    infos, used = read_index(lines)
    assert infos == {
        "0001": AccountInfo(
            "0001", "Individual - TOD", "brokerage", "individual", ending_value=Decimal("5.00")
        ),
        "0002": AccountInfo(
            "0002", "Rollover IRA", "retirement", "rollover_ira", ending_value=Decimal("6.00")
        ),
        # A row that prints no value has none (never the next row's).
        "0003": AccountInfo("0003", "Education Account", "brokerage", "education_529"),
    }
    assert {lines[i].text for i in used} >= {"GENERAL INVESTMENTS", "EDUCATION (529) ACCOUNTS"}
    # A shared first part that names a kind of account is not an owner's name.
    kinds = _lines(
        [
            [(40, "Account Type"), (220, "Account Number")],
            [(40, "INDIVIDUAL - TOD"), (220, "Z11-100001")],
            [(40, "INDIVIDUAL - JOINT"), (220, "Z11-100002")],
        ]
    )
    names = [i.name for i in read_index(kinds)[0].values()]
    assert names == ["INDIVIDUAL - TOD", "INDIVIDUAL - JOINT"]


@pytest.mark.parametrize(
    ("texts", "kind", "subtype"),
    [
        (("Everyday Checking",), "checking", None),
        (("Joint Checking",), "checking", "joint"),
        (("Statement Savings",), "savings", None),
        (("Health Savings Account",), "retirement", "hsa"),
        (("529 College Savings Plan",), "brokerage", "education_529"),
        (("Traditional IRA", "PERSONAL RETIREMENT"), "retirement", "traditional_ira"),
        (("Education Account", "EDUCATION (529) ACCOUNTS"), "brokerage", "education_529"),
        (("Individual - TOD", "GENERAL INVESTMENTS"), "brokerage", "individual"),
        (("Self-Directed Brokerage",), "brokerage", None),
        (("Jordan Q. Sample",), None, None),
        ((None,), None, None),
    ],
)
def test_account_kind(texts: tuple[str | None, ...], kind: str | None, subtype: str | None) -> None:
    assert account_kind(*texts) == (kind, subtype)


@pytest.mark.parametrize(
    ("text", "last4"),
    [
        ("418-205817", "5817"),
        ("XXXX-4821", "4821"),
        ("Z29-104821", "4821"),
        ("123456789", "6789"),
        ("22", None),
        ("2019", None),
        ("2019-06-30", None),
        ("06/30/2019", None),
        ("$1,234.56", None),
        ("Jordan Q. Sample", None),
    ],
)
def test_number_last4(text: str, last4: str | None) -> None:
    assert number_last4(text) == last4


def test_a_proposal_takes_the_subtype_the_document_prints() -> None:
    found = proposals._Found(
        target="investment",
        index=2,
        last4="9015",
        name="Education Account",
        institution=None,
        account_type="brokerage",
        currency="USD",
        period_start=date(2019, 6, 1),
        period_end=date(2019, 6, 30),
        opening_balance=None,
        closing_balance=Decimal("27143.49"),
        transactions=0,
        subtype="education_529",
    )
    proposal = proposals._proposal(found, None, "USD")
    assert (proposal.type, proposal.subtype) == (AccountType.BROKERAGE, "education_529")
    # A subtype of another type is not taken.
    other = proposals._proposal(
        proposals._Found(**{**found.__dict__, "subtype": "traditional_ira"}), None, "USD"
    )
    assert (other.type, other.subtype) == (AccountType.BROKERAGE, None)
