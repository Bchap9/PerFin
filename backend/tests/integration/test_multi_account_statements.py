# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statements of several accounts through the pipeline: each account on its own.

The synthetic combined statement (``tests/fixtures/synth_neutral_report.py``:
checking, savings and brokerage in one PDF) and the three-account
Fidelity-layout report (``tests/fixtures/synth_fidelity_report.py``) are
uploaded; each account's statement is matched by its own last four digits
and imported or held on its own, and accounts PerFin does not know are
proposed with the type and subtype the document prints. Everything is
invented.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.auth.principal import apply_rls_context
from app.domain.accounts.models import Account, AccountType
from app.domain.documents.models import DocumentKind
from app.domain.imports import proposals
from app.ingestion.sandbox.protocol import ExtractResult
from tests.fixtures import synth_fidelity_report as fidr
from tests.fixtures import synth_neutral_report as neutral
from tests.integration.test_brokerage_pipeline import Env, env

pytestmark = pytest.mark.integration
__all__ = ["env"]


def _account(env: Env, institution: Any, kind: AccountType, last4: str, name: str) -> Account:
    return env.ledger.account(
        env.household_id,
        type=kind,
        number_last4=last4,
        currency="USD",
        institution_id=institution.id,
        name=name,
    )


def test_each_account_of_a_combined_statement_lands_on_its_own_account(env: Env) -> None:
    stmt = neutral.report()
    by_last4 = {a.last4: a for a in stmt.accounts}
    bank = env.ledger.institution(env.household_id, name=neutral.INSTITUTION)
    checking = _account(env, bank, AccountType.CHECKING, "4821", "Checking")
    savings = _account(env, bank, AccountType.SAVINGS, "5530", "Savings")
    brokerage = _account(env, bank, AccountType.BROKERAGE, "6619", "Brokerage")
    document_id = env.ingest(neutral.report_pdf(stmt), "combined-statement.pdf")

    document = env.document(document_id)
    assert document.kind is DocumentKind.STATEMENT
    # Three accounts in one document: it is linked to none of them.
    assert document.account_id is None
    assert (document.period_start, document.period_end) == (neutral.START, neutral.END)
    stored = ExtractResult.model_validate(document.extraction)
    assert [s.account.last4 for s in stored.statements] == ["4821", "5530"]
    assert [i.account.last4 for i in stored.investments] == ["6619"]

    # Each bank account has its own rows (the checking account's printed
    # Checks Paid table is not read yet: its statement does not reconcile, so
    # its rows wait as pending; the savings account's rows are posted).
    def amounts(account: Account) -> list[Any]:
        return sorted(t.amount for t in env.cash_rows(account))

    detail = [a for a in by_last4["4821"].activity if a.section != "checks"]
    assert amounts(checking) == sorted(a.amount for a in detail)
    assert amounts(savings) == sorted(a.amount for a in by_last4["5530"].activity)
    assert all(t.source_document_id == document_id for t in env.cash_rows(checking))
    # The brokerage account's holdings are written; nothing lands on the bank accounts.
    assert env.holdings(brokerage) == sorted(
        (p.symbol or "", p.quantity) for p in by_last4["6619"].positions
    )
    assert env.txns(checking) == []
    assert env.txns(savings) == []
    reasons = env.reasons(document_id)
    assert "unknown_account" not in reasons


def test_unknown_accounts_are_proposed_with_their_printed_kind(env: Env) -> None:
    stmt, extras = fidr.report()
    document_id = env.ingest(fidr.report_pdf(stmt, extras), "investment-report.pdf")
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        found = proposals.detect_accounts(db, env.principal(), document_id)
    proposed = [
        (f.last4, f.proposal.type, f.proposal.subtype)
        for f in found.accounts
        if f.proposal is not None
    ]
    assert proposed == [
        ("5817", AccountType.BROKERAGE, "individual"),
        ("0642", AccountType.RETIREMENT, "traditional_ira"),
        ("9015", AccountType.BROKERAGE, "education_529"),
    ]
    names = [f.proposal.name for f in found.accounts if f.proposal is not None]
    assert names == ["Individual - TOD 5817", "Traditional IRA 0642", "Education Account 9015"]
