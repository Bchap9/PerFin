# SPDX-License-Identifier: AGPL-3.0-or-later
"""Institution parsers: the generic table parser with the layout pinned down.

One subclass per institution layout. Each declares the institution ``key``
(the classifier fingerprint's ``key``, ``app/ingestion/classify/fingerprints``;
the classifier's match is the parser hint), the institution's name as
accounts carry it (account matching), fingerprint phrases of its first page
(its own detection when no classification is available), the row and period
date formats, the sign convention and the currency, so nothing has to be
guessed and every field read in the declared format scores 1.0.

The institutions are the fictional ones of the shipped fingerprints
(``tests/fixtures/synth_classify.py``; PDFs from
``tests/fixtures/synth_pdf_statements.py``). A contributor adds a layout the
same way (docs/CONTRIBUTING-parsers.md), with a synthetic fixture, never a
real statement.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from app.ingestion.parsers.table_statement import CardStatementParser, TableStatementParser
from app.ingestion.parsers.text import DateStyle


class SampleNationalCheckingParser(TableStatementParser):
    """Sample National Bank checking: ``MM/DD/YYYY``, signed *Amount*, running *Balance*."""

    name: ClassVar[str] = "sample_national_checking"
    version: ClassVar[str] = "4"
    institution: ClassVar[str | None] = "sample-national-bank-checking"
    display_name: ClassVar[str | None] = "Sample National Bank"
    fingerprints: ClassVar[tuple[str, ...]] = (
        "sample national bank",
        "checking account statement",
    )
    account_type: ClassVar[str | None] = "checking"
    default_currency: ClassVar[str | None] = "USD"
    date_styles: ClassVar[tuple[DateStyle, ...]] = ("mdy",)
    period_styles: ClassVar[tuple[DateStyle, ...]] = ("mdy",)
    base: ClassVar[float] = 1.0


class ExampleFcuShareParser(TableStatementParser):
    """Example Federal Credit Union: ruled table, *Withdrawals*/*Deposits*, ``MM/DD/YYYY``."""

    name: ClassVar[str] = "example_fcu_share"
    version: ClassVar[str] = "4"
    institution: ClassVar[str | None] = "example-fcu-share-statement"
    display_name: ClassVar[str | None] = "Example Federal Credit Union"
    fingerprints: ClassVar[tuple[str, ...]] = (
        "example federal credit union",
        "member share statement",
    )
    account_type: ClassVar[str | None] = "savings"
    default_currency: ClassVar[str | None] = "USD"
    date_styles: ClassVar[tuple[DateStyle, ...]] = ("mdy",)
    period_styles: ClassVar[tuple[DateStyle, ...]] = ("mdy",)
    base: ClassVar[float] = 1.0


class DemoCardParser(CardStatementParser):
    """Demo Card Services: ``MM/DD`` rows, ``Mon D, YYYY`` period, charges positive."""

    name: ClassVar[str] = "demo_card_services"
    version: ClassVar[str] = "4"
    institution: ClassVar[str | None] = "demo-card-services-card"
    display_name: ClassVar[str | None] = "Demo Card Services"
    fingerprints: ClassVar[tuple[str, ...]] = ("demo card services", "card ending in")
    account_type: ClassVar[str | None] = "creditcard"
    default_currency: ClassVar[str | None] = "USD"
    date_styles: ClassVar[tuple[DateStyle, ...]] = ("md", "mdy")
    period_styles: ClassVar[tuple[DateStyle, ...]] = ("mon_d_y", "mdy")
    base: ClassVar[float] = 1.0


class MockSavingsParser(TableStatementParser):
    """Mock Savings and Loan: ``DD/MM/YYYY``, *Paid out*/*Paid in*, GBP."""

    name: ClassVar[str] = "mock_savings_and_loan"
    version: ClassVar[str] = "4"
    institution: ClassVar[str | None] = "mock-savings-and-loan-statement"
    display_name: ClassVar[str | None] = "Mock Savings and Loan"
    fingerprints: ClassVar[tuple[str, ...]] = (
        "mock savings and loan",
        "current account statement",
    )
    account_type: ClassVar[str | None] = "checking"
    default_currency: ClassVar[str | None] = "GBP"
    date_styles: ClassVar[tuple[DateStyle, ...]] = ("dmy",)
    period_styles: ClassVar[tuple[DateStyle, ...]] = ("dmy",)
    sign: ClassVar[Literal["bank", "card"]] = "bank"
    base: ClassVar[float] = 1.0


INSTITUTION_PARSERS: tuple[type[TableStatementParser], ...] = (
    SampleNationalCheckingParser,
    ExampleFcuShareParser,
    DemoCardParser,
    MockSavingsParser,
)
