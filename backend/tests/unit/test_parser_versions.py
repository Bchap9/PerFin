# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every parser's version, in one place.

``document.parser_version`` (``name/version``) says which rules read a stored
document. A change to what a parser reads moves its version, so a document
read by the old rules can be told apart; "Process again" reads it with the
current ones. Change a number here together with the parser and CHANGELOG.md.
"""

from __future__ import annotations

from decimal import Decimal

from app.ingestion.extract import ofx, qif
from app.ingestion.parsers import brokerage_table, positions_table, receipt, tax_forms, template
from app.ingestion.parsers.registry import PARSERS

VERSIONS = {
    "tax_forms": "2",
    "sample_national_checking": "8",
    "example_fcu_share": "8",
    "demo_card_services": "8",
    "mock_savings_and_loan": "8",
    "placeholder_brokerage": "7",
    "fidelity_investments_statement": "8",
    "brokerage_statement_generic": "9",
    "retirement_plan_statement": "1",
    "loan_statement_generic": "2",
    "payslip_basic": "2",
    "receipt_basic": "2",
    "card_statement_generic": "10",
    "statement_generic": "10",
}


def test_every_registered_parser_has_its_pinned_version() -> None:
    assert {p.name: p.version for p in PARSERS} == VERSIONS


def test_the_readers_outside_the_registry_have_their_pinned_versions() -> None:
    assert (template.PARSER_NAME, template.PARSER_VERSION) == ("layout_template", "6")
    assert (brokerage_table.PARSER_NAME, brokerage_table.PARSER_VERSION) == (
        "brokerage_activity_table",
        "2",
    )
    assert (positions_table.PARSER_NAME, positions_table.PARSER_VERSION) == (
        "brokerage_positions_table",
        "2",
    )
    assert (receipt.PARSER_NAME, receipt.PARSER_VERSION) == ("receipt_basic", "2")
    assert (tax_forms.PARSER_NAME, tax_forms.PARSER_VERSION) == ("tax_forms", "2")
    assert ofx.VERSION == "ofx/3"
    assert qif.VERSION == "qif/2"


def test_a_taught_layout_reads_an_amount_grouped_the_indian_way() -> None:
    # The amount reader a taught layout shares now reads lakh grouping: the
    # same cell reads differently, which is why ``layout_template`` moved.
    cell = [template.Word("1,14,964.36", 400.0, 100.0, 455.0, 109.0)]
    assert template._amount(cell) == Decimal("114964.36")
    # It moved again with import 1d: a direction per table, several tables,
    # continued pages and month-name dates read the same pages differently.
    # And once more with the days of grace around a statement period: a row
    # dated just outside it keeps its date's score.
    assert template.PARSER_VERSION == "6"
