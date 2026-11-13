# SPDX-License-Identifier: AGPL-3.0-or-later
"""Every parser's version, in one place.

``document.parser_version`` (``name/version``) says which rules read a stored
document. A change to what a parser reads moves its version, so a document
read by the old rules can be told apart; "Process again" reads it with the
current ones. Change a number here together with the parser and CHANGELOG.md.
"""

from __future__ import annotations

from app.ingestion.extract import ofx
from app.ingestion.parsers import brokerage_table, positions_table, receipt, tax_forms, template
from app.ingestion.parsers.registry import PARSERS

VERSIONS = {
    "tax_forms": "2",
    "sample_national_checking": "6",
    "example_fcu_share": "6",
    "demo_card_services": "6",
    "mock_savings_and_loan": "6",
    "placeholder_brokerage": "7",
    "fidelity_investments_statement": "8",
    "brokerage_statement_generic": "9",
    "retirement_plan_statement": "1",
    "loan_statement_generic": "2",
    "payslip_basic": "2",
    "receipt_basic": "2",
    "card_statement_generic": "8",
    "statement_generic": "8",
}


def test_every_registered_parser_has_its_pinned_version() -> None:
    assert {p.name: p.version for p in PARSERS} == VERSIONS


def test_the_readers_outside_the_registry_have_their_pinned_versions() -> None:
    assert (template.PARSER_NAME, template.PARSER_VERSION) == ("layout_template", "3")
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
