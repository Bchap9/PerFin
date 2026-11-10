# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statements in layouts nothing shipped knows (``tests/fixtures/synth_unseen_layouts``).

What the generic readers must get right on a household's first statement
from a new institution, without a fingerprint, a template or a person: who
sent it, which account it is, its balances and rows, and its currency.
"""

from __future__ import annotations

import pytest

from app.ingestion.classify.institution import detect_institution
from app.ingestion.classify.service import classify_extraction
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.accounts import summary_last4
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.table_statement import section_name
from app.ingestion.parsers.text import symbol_currency
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from tests.fixtures.synth_unseen_layouts import EXPECTED, Expected

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _extract(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return result


@pytest.mark.parametrize("expected", EXPECTED.values(), ids=EXPECTED.keys())
def test_an_unseen_statement_is_read_whole(expected: Expected) -> None:
    result = _extract(expected.make())
    found = classify_extraction(result)
    assert found.kind == "statement"
    assert found.method == "classifier"  # no fingerprint knows these layouts
    assert found.institution == expected.institution
    statements = parse_extraction(result, kind="statement").statements
    assert len(statements) == len(expected.statements)
    for statement, want in zip(statements, expected.statements, strict=True):
        assert statement.account.last4 == want.last4
        assert statement.opening_balance is not None
        assert statement.opening_balance.amount == want.opening
        assert statement.ledger_balance is not None
        assert statement.ledger_balance.amount == want.closing
        assert len(statement.transactions) == want.rows
        assert statement.reconciliation is not None
        assert statement.reconciliation.status == "ok"
        assert statement.account.currency == want.currency
        if want.name is not None:
            assert statement.account.name == want.name
        if want.account_type is not None:
            assert statement.account.account_type == want.account_type


def test_shares_of_one_member_never_take_the_member_number() -> None:
    result = _extract(EXPECTED["credit_union_shares"].make())
    parsed = parse_extraction(result, kind="statement")
    assert parsed.parse is not None
    assert "accounts_by_balance" in parsed.parse.warnings
    assert [s.account.last4 for s in parsed.statements] == ["0000", "0010"]
    # The transfer between the two shares is in both, with opposite signs.
    savings, checking = parsed.statements
    assert [t.amount for t in savings.transactions if "Transfer" in (t.payee or "")] == [300]
    assert [t.amount for t in checking.transactions if "Transfer" in (t.payee or "")] == [-300]


def test_a_balance_brought_forward_continues_its_account() -> None:
    result = _extract(EXPECTED["sterling_current_account"].make())
    parsed = parse_extraction(result, kind="statement")
    assert parsed.parse is not None
    assert len(parsed.statements) == 1
    assert "accounts_by_balance" not in parsed.parse.warnings


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("PRIMARY SAVINGS - ID 0000", ("PRIMARY SAVINGS", "0000")),
        ("FREE CHECKING - ID 0010", ("FREE CHECKING", "0010")),
        ("Regular Shares Share 01", ("Regular Shares", None)),  # too short to name an account
        ("MONEY MARKET #100482", ("MONEY MARKET", "0482")),
        ("Holiday Club", ("Holiday Club", None)),
        (None, (None, None)),
    ],
)
def test_section_titles(title: str | None, expected: tuple[str | None, str | None]) -> None:
    assert section_name(title) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Account number: 4410 0092 6654", "6654"),  # groups: the last one, not the first
        ("Account 4111 1111 1111 1111", "1111"),
        ("Account number: 4410 0092 6654 03/01/2026", "6654"),  # a date follows
        ("Account 4410 0092 6654 1,204.33", "6654"),  # an amount follows
        ("Account Number: 000000482913377", "3377"),
        ("Account number 12345678 01/02/26 to 02/01/26", "5678"),
        ("Account 12345678 2026", "5678"),  # two numbers side by side stay apart
        ("Account 1234 5 items", "1234"),
        ("Account Ending 4-71009", "1009"),
        ("Account ending in 1234", "1234"),
        ("Card ending 9921", "9921"),
        ("Account 111-111111 $103,351.18", "1111"),
        ("Member Number: ******5120", "5120"),
        ("xxxx-xxxx-xxxx-4821", "4821"),
        ("Period ending 2026-03-31", None),
        ("Statement period ending 2026", None),
        ("Account 2026-07-31", None),  # a date
        ("account 1-800-555-0100", None),  # a telephone number
        ("Account 123", None),
        ("New balance", None),
    ],
)
def test_summary_last4(line: str, expected: str | None) -> None:
    assert summary_last4(line) == expected


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Northwind Bank, N.A. January 14, 2026 through February 12, 2026", "Northwind Bank, N.A."),
        ("Issued by Meridian Card Bank", "Meridian Card Bank"),
        ("Thistle & Wren Building Society Sort code 40-11-62", "Thistle & Wren Building Society"),
        ("Prairie Lakes Credit Union Statement Period: 04/01/2026", "Prairie Lakes Credit Union"),
        ("EXAMPLE FEDERAL CREDIT UNION", "EXAMPLE FEDERAL CREDIT UNION"),
        ("Bank of Springfield", "Bank of Springfield"),
        ("First Bank of the West Statement", "First Bank of the West"),
        ("Mock Savings and Loan", "Mock Savings and Loan"),
        ("Fictional Mutual Insurance Company", "Fictional Mutual Insurance Company"),
        ("Welcome to Acme Trust Company, Inc.", "Acme Trust Company, Inc."),
        ("Acme Mortgage LLC Statement", "Acme Mortgage LLC"),
        ("Placeholder Brokerage", "Placeholder Brokerage"),
        # Headings and generic words name nobody.
        ("Your Financial Summary", None),
        ("Sample Financial Summary", None),
        ("Placeholder Brokerage Account Statement", None),
        ("Acme Mortgage Statement", None),
        ("Federal Credit Union", None),
        ("Your Bank Statement", None),
        ("Online Banking", None),
        ("CHECKING SUMMARY", None),
        ("Deposits made at the bank", None),
        ("1420 ELM ST APT 3", None),
        ("Card Purchase 01/14 Corner Grocer #204", None),
    ],
)
def test_institution_in_a_header_line(line: str, expected: str | None) -> None:
    found = detect_institution([line])
    assert (found.name if found else None) == expected


def test_a_bank_outranks_a_weaker_name_further_up() -> None:
    found = detect_institution(["Acme Financial", "Northwind Bank, N.A."])
    assert found is not None
    assert found.name == "Northwind Bank, N.A."
    assert detect_institution(["Statement of account", "Page 1 of 3"]) is None


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["Balance £1,250.00", "Money in £2,100.00"], "GBP"),
        (["Solde €1.00", "Total €2.00"], "EUR"),
        (["Balance $1,250.00", "Total $2,100.00"], None),  # which dollar?
        (["Balance £1,250.00"], None),  # one amount is not enough
        (["Total £10.00", "Purchase abroad €9.00", "Fee £1.00"], None),  # mixed
        (["Balance 1,250.00"], None),
    ],
)
def test_symbol_currency(lines: list[str], expected: str | None) -> None:
    assert symbol_currency(lines) == expected
