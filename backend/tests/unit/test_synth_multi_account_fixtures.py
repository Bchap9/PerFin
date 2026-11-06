# SPDX-License-Identifier: AGPL-3.0-or-later
"""Smoke tests for the synthetic multi-account statement fixtures.

``tests/fixtures/synth_fidelity_report.py`` (a three-account brokerage report
in the Fidelity layout) and ``tests/fixtures/synth_neutral_report.py`` (a
provider-neutral checking + savings + brokerage statement) are what the
multi-account parsing work is tested against. These tests only check the
fixtures themselves: the PDFs extract (in process, with the project's own
extractor) to the layout traits the generators promise, and the expected
results they hand to parser tests are internally consistent.

Not written here: the behaviour tests (each fixture parsed into one
reconciled statement per account with the right period, positions and
activity; detection by metadata; the duplicate-statement guard). Those
belong to the phases that implement the behaviour and are written there.
"""

from __future__ import annotations

import re
from decimal import Decimal

import pytest

from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import DocumentView
from app.ingestion.parsers.registry import parser_named
from app.ingestion.sandbox.protocol import DocumentMeta, ExtractOptions, ExtractResult
from tests.fixtures import synth_fidelity_report as fidr
from tests.fixtures import synth_neutral_report as neutral
from tests.fixtures.synth_multi_account import ZERO, Account, Report, cusip, isin

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _extract(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return result


def _meta(result: ExtractResult) -> DocumentMeta:
    assert result.meta is not None
    return result.meta


def _lines(result: ExtractResult, page: int) -> list[str]:
    return result.pages[page - 1].text.splitlines()


@pytest.fixture(scope="module")
def fid_report() -> tuple[Report, fidr.Extras]:
    return fidr.report()


@pytest.fixture(scope="module")
def fid_pdf(fid_report: tuple[Report, fidr.Extras]) -> ExtractResult:
    return _extract(fidr.report_pdf(*fid_report))


@pytest.fixture(scope="module")
def neutral_pdf() -> ExtractResult:
    return _extract(neutral.report_pdf())


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def test_cusip_and_isin_check_digits() -> None:
    from app.ingestion.parsers.brokerage import is_cusip

    assert is_cusip(cusip("94136RAC"))
    assert cusip("03783310") == "037833100"  # a published example
    assert isin("US037833100") == "US0378331005"  # a published example
    assert re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin("ZZ0000SMC01"))


def test_generators_are_deterministic() -> None:
    assert fidr.report_pdf() == fidr.report_pdf()
    assert neutral.report_pdf() == neutral.report_pdf()


# ---------------------------------------------------------------------------
# Fidelity-layout report
# ---------------------------------------------------------------------------


def test_fidelity_every_page_has_the_unlabelled_header_period(fid_pdf: ExtractResult) -> None:
    assert _meta(fid_pdf).page_count == len(fid_pdf.pages) > 15
    title, period = fidr.EXPECTED_HEADER_LINES
    assert period == "June 1 \u2013 June 30, 2019"
    for page in fid_pdf.pages:
        lines = page.text.splitlines()
        assert any(line.endswith(title) for line in lines[:3]), page.number
        assert period in lines[:4], page.number
        assert not any("period:" in line.lower() for line in lines[:4])


def test_fidelity_institution_only_in_metadata(fid_pdf: ExtractResult) -> None:
    assert _meta(fid_pdf).author == fidr.INSTITUTION
    assert fidr.INSTITUTION in (_meta(fid_pdf).title or "")
    assert all(fidr.INSTITUTION not in page.text for page in fid_pdf.pages)
    assert any(
        line.startswith(f"Brokerage services provided by {fidr.FOOTER_BROKER}")
        for line in _lines(fid_pdf, 1)
    )
    bare = _extract(fidr.report_pdf(institution_metadata=False))
    assert not _meta(bare).author
    assert fidr.INSTITUTION not in (_meta(bare).title or "")


def test_fidelity_is_no_receipt(fid_pdf: ExtractResult) -> None:
    # The layout's footnotes speak of "the statement period"; the report must not
    # look like a receipt for a reason the real layout does not have.
    for page in (1, 2):
        assert "the statement period." in _lines(fid_pdf, page)
    receipt = parser_named("receipt_basic")
    assert receipt is not None
    bare = _extract(fidr.report_pdf(institution_metadata=False))
    for result in (fid_pdf, bare):
        assert receipt.detect(DocumentView.from_result(result)) == 0.0


def test_fidelity_account_index_and_first_pages(
    fid_pdf: ExtractResult, fid_report: tuple[Report, fidr.Extras]
) -> None:
    stmt, _ = fid_report
    page2 = _lines(fid_pdf, 2)
    assert "Accounts Included in This Report" in page2
    assert "Page Account Type/Name Account Number Beginning Value Ending Value" in page2, page2
    printed_pages = []
    for index, account in enumerate(stmt.accounts):
        row = next(line for line in page2 if account.number in line)
        assert row.endswith(f"{account.total_value:,.2f}"), row
        # The account's first page names it on the same line as a dollar amount.
        first_page = next(
            page.number
            for page in fid_pdf.pages
            if re.fullmatch(
                rf"(Account Value: )?\$[\d,]+\.\d\d Account {account.number}",
                page.text.splitlines()[3],
            )
        )
        banner = _lines(fid_pdf, first_page)[3]
        assert f"${account.total_value:,.2f}" in banner
        # The index's page numbers are wrong (module doc): never the first page.
        printed = int(row.split()[0])
        assert printed == first_page + fidr.INDEX_PAGE_SHIFT[index] != first_page
        printed_pages.append(printed)
        # Every later page of the account names it top right with the owner line under it.
        for page in fid_pdf.pages[first_page:]:
            lines = page.text.splitlines()
            if not lines[3].endswith("Account " + account.number):
                break
            assert lines[4] == account.title
    banners = {
        line
        for page in fid_pdf.pages
        for line in page.text.splitlines()
        if re.search(r"\$[\d,]+\.\d\d Account \d{3}-\d{6}$", line)
    }
    assert len(banners) == 3
    assert any(line.startswith("Account Value: $") for line in banners)
    assert printed_pages[-1] > len(fid_pdf.pages)


def test_fidelity_table_traits(fid_pdf: ExtractResult) -> None:
    text = "\n".join(page.text for page in fid_pdf.pages)
    lines = text.splitlines()
    # Two-line holdings headers: "Price" over "Per Unit".
    per_unit = [i for i, line in enumerate(lines) if "Quantity Per Unit Market Value" in line]
    assert per_unit
    assert all(lines[i - 1].startswith("Price Ending") for i in per_unit)
    # The core account heading sits above its own table.
    assert next(lines[i + 1] for i, line in enumerate(lines) if line == "Core Account").startswith(
        "Total Unrealized"
    )
    # Bonds: maturity column, per-100 prices, CUSIP continuation lines.
    assert re.search(r"^SAMPLE CORP NT CV 8/1/28 8,000\.00 \$98\.625 \$7,890\.00", text, re.M)
    assert f"CUSIP: {cusip('94136RAC')}" in lines
    # Footnote markers inside numeric cells: after a price, after a cost basis
    # (attached or standing apart between two amounts).
    assert re.search(r"^PLACEHOLDER GROUP LIMITED COM STK 60\.00 147\.85A 8,871\.00 ", text, re.M)
    assert re.search(r" \$6,630\.82t \$6,821\.52 ", text)
    assert re.search(r" \$7,184\.90 c \$3,821\.56 ", text)
    # Activity header vocabulary and year-less dates.
    assert (
        "Date Security Name CUSIP Transaction Description Quantity Price of Close Cost "
        "Transaction Total"
    ) in lines
    assert re.search(r"^6/11 MOCK SER TR HIGH YIELD BOND ZZHY You bought 120 ", text, re.M)
    # The wrapped end of a name and the note under the row share one line.
    assert "ETF Wash sale of 5/14/19: $193.62" in lines
    # Side-by-side tables: the header twice on one line.
    header = "Date Reference Transaction Description Transaction Total"
    assert f"{header} {header}" in lines
    assert any(line.endswith("Total Deposits & EFT In $6,502.50") for line in lines)
    # A cash transfer between two accounts of the report: no security, both sides.
    assert "6/28 R57-330642-1 Trans. from account 418-330642 $2,140.00" in lines
    assert "6/28 R57-205817-1 Trans. to account 418-205817 -$2,140.00" in lines
    # U+2212 minus signs in some tables, "-" in others.
    assert "2311 6/30 01 Check paid \u2212$412.86 2324 * 6/23 01 Check paid \u2212793.45" in lines
    assert any(line.startswith("Total Debit Card Activity \u2212$") for line in lines)
    assert any(line.startswith("Subtractions \u2212$") for line in lines)  # the IRA summary
    assert text.count(fidr.MINUS) > 30
    # An ending balance is one figure in both columns.
    endings = [line for line in lines if line.startswith("Ending Core Account Balance")]
    assert len(endings) == 2
    for line in endings:
        this_period, year_to_date = line.split()[-2:]
        assert this_period == year_to_date, line
    for title in (
        "Securities Transferred In",
        "Securities Transferred Out",
        "Trades Pending Settlement",
        "Checking Activity",
        "Debit Card Activity",
        "Bill Payments",
        "Taxes Withheld",
        "Daily Additions and Subtractions",
        "Estimated Cash Flow",
        "Core Account Cash Flow Income Summary",
        "Change from Last Period: -$7,173.29",
    ):
        assert any(line.startswith(title) for line in lines), title


def test_fidelity_expected_results_reconcile(fid_report: tuple[Report, fidr.Extras]) -> None:
    stmt, extras = fid_report
    results = fidr.expected(stmt)
    assert [r.last4 for r in results] == ["5817", "0642", "9015"]
    assert [r.subtype for r in results] == ["brokerage", "ira", "529"]
    for account, result in zip(stmt.accounts, results, strict=True):
        assert (result.period_start.isoformat(), result.period_end.isoformat()) == (
            "2019-06-01",
            "2019-06-30",
        )
        assert result.positions_value == sum((p[3] for p in result.positions), ZERO)
        if result.cash_opening is None:
            assert result.cash_closing is None
            assert not result.activity
            assert result.total_value == result.positions_value
            continue
        assert result.cash_closing == result.cash_opening + sum(
            (row[4] for row in result.activity), ZERO
        )
        assert result.total_value == result.positions_value + result.cash_closing
        _check_positions(account)
    individual = stmt.accounts[0]
    bond = next(p for p in individual.positions if p.cusip == cusip("94136RAC"))
    assert bond.per_100
    assert bond.market_value == Decimal("7890.00")
    types = {row[1] for row in results[0].activity}
    assert types >= {"buy", "sell", "dividend", "reinvest", "interest", "deposit", "withdrawal"}
    assert {"transfer_in", "transfer_out", "fee"} <= types
    # In-kind transfers move units and no cash; a cash transfer between two
    # accounts of the report shows on both sides, as a deposit and a
    # withdrawal of cash; pending trades are not activity.
    transfers = [row for r in results for row in r.activity if row[1].startswith("transfer")]
    assert transfers
    assert all(row[4] == 0 and row[3] is not None for row in transfers)
    cash = [
        a
        for account in stmt.accounts
        for a in account.section("transferred_in", "transferred_out")
        if a.quantity is None
    ]
    assert sorted(a.type for a in cash) == ["deposit", "withdrawal"]
    assert sum((a.amount for a in cash), ZERO) == 0
    pending = extras.pending[individual.number][0]
    assert pending.day > stmt.end
    assert all(row[0] <= stmt.end for r in results for row in r.activity)


def _check_positions(account: Account) -> None:
    for p in account.positions:
        raw = p.quantity * p.price / (100 if p.per_100 else 1)
        assert abs(raw - p.market_value) < Decimal("0.005")


# ---------------------------------------------------------------------------
# Provider-neutral report
# ---------------------------------------------------------------------------


def test_neutral_header_period_and_headings(neutral_pdf: ExtractResult) -> None:
    name, period = neutral.EXPECTED_HEADER_LINES
    assert period == "Statement Period: 03/01/2024 - 03/31/2024"
    for page in neutral_pdf.pages:
        assert page.text.splitlines()[0] == f"{name} {period}", page.number
    assert _meta(neutral_pdf).author == neutral.INSTITUTION
    headings = [
        [line for line in page.text.splitlines() if line.startswith("Account Number:")]
        for page in neutral_pdf.pages
    ]
    assert headings == [
        [],
        ["Account Number: XXXX-4821"],
        [],
        ["Account Number: XXXX-5530", "Account Number: XXXX-6619"],
        [],
        [],
    ]
    page1 = _lines(neutral_pdf, 1)
    assert "Account Name Account Number Opening Balance Closing Balance" in page1
    assert "Transaction Detail (continued)" in _lines(neutral_pdf, 3)
    assert "Date Description Amount" in _lines(neutral_pdf, 3)
    assert (
        "Trade Date Activity Symbol Description Units Unit Price Commission Net Amount"
        in _lines(neutral_pdf, 5)
    )


def test_neutral_expected_results_reconcile() -> None:
    stmt = neutral.report()
    results = neutral.expected(stmt)
    assert [(r.last4, r.kind, r.subtype) for r in results] == [
        ("4821", "bank", "checking"),
        ("5530", "bank", "savings"),
        ("6619", "investment", "brokerage"),
    ]
    for result in results:
        assert result.opening is not None
        assert result.closing is not None
        if result.kind == "bank":
            assert result.closing == result.opening + sum((r[1] for r in result.bank_rows), ZERO)
            assert not result.positions
        else:
            assert result.closing == result.opening + sum((r[4] for r in result.activity), ZERO)
            assert result.total_value == result.positions_value + result.closing
    _check_positions(stmt.accounts[2])
    checking = results[0]
    assert len(checking.bank_rows) == 26
    assert checking.bank_rows[-1][2] == "CHECK 1202"
