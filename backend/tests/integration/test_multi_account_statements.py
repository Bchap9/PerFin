# SPDX-License-Identifier: AGPL-3.0-or-later
"""Statements of several accounts through the pipeline: each account on its own.

The synthetic combined statement (``tests/fixtures/synth_neutral_report.py``:
checking, savings and brokerage in one PDF) and the three-account
Fidelity-layout report (``tests/fixtures/synth_fidelity_report.py``) are
uploaded; each account's statement is matched by its own last four digits
and imported or held on its own, and accounts PerFin does not know are
proposed with the type and subtype the document prints. A statement a
person put on an account stays there when its balances are corrected
afterwards (``statement_answers.statement_ref``). Everything is invented.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.principal import apply_rls_context
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.documents.models import Document, DocumentKind, DocumentStatus, ReviewItem
from app.domain.imports import proposals
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE, ApplyMapping, Assignment
from app.domain.imports.service import import_extraction, request_apply
from app.domain.imports.statement_answers import REF_FIELD, statement_key
from app.domain.investments.models import Price
from app.domain.review.layout import stored_result
from app.ingestion.parsers.template import rebalance
from app.ingestion.sandbox.protocol import ExtractResult
from tests.fixtures import synth_fidelity_report as fidr
from tests.fixtures import synth_neutral_report as neutral
from tests.fixtures.documents import make_document
from tests.fixtures.imports import run_jobs
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

    # Each bank account has its own rows: the checking account's Transaction
    # Detail and Checks Paid tables, the savings account's rows.
    def amounts(account: Account) -> list[Any]:
        return sorted(t.amount for t in env.cash_rows(account))

    assert amounts(checking) == sorted(a.amount for a in by_last4["4821"].activity)
    assert amounts(savings) == sorted(a.amount for a in by_last4["5530"].activity)
    assert all(t.source_document_id == document_id for t in env.cash_rows(checking))
    # The brokerage account's holdings and activity (trades and its cash
    # table) are written; nothing lands on the bank accounts.
    assert env.holdings(brokerage) == sorted(
        (p.symbol or "", p.quantity) for p in by_last4["6619"].positions
    )
    expected = {e.last4: e for e in neutral.expected(stmt)}
    _assert_activity(env, brokerage, expected["6619"].activity)
    assert env.txns(checking) == []
    assert env.txns(savings) == []
    reasons = env.reasons(document_id)
    assert "unknown_account" not in reasons
    # Every account reconciles: nothing is held back.
    assert not reasons & {"reconciliation_failed", "investment_reconciliation_failed"}


def test_the_fidelity_layout_report_imports_every_account(env: Env) -> None:
    """Detected by its metadata, read table by table, each account reconciled and written."""
    stmt, extras = fidr.report()
    institution = env.ledger.institution(env.household_id, name="Fidelity Investments")
    individual = _account(env, institution, AccountType.BROKERAGE, "5817", "Individual")
    ira = _account(env, institution, AccountType.RETIREMENT, "0642", "Traditional IRA")
    education = _account(env, institution, AccountType.BROKERAGE, "9015", "529")
    document_id = env.ingest(fidr.report_pdf(stmt, extras), "investment-report.pdf")

    stored = ExtractResult.model_validate(env.document(document_id).extraction)
    assert stored.parse is not None
    assert stored.parse.parser is not None
    assert stored.parse.parser.name == "fidelity_investments_statement"
    assert [i.reconciliation.status for i in stored.investments if i.reconciliation] == [
        "ok",
        "ok",
        "ok",
    ]
    reasons = env.reasons(document_id)
    assert "investment_reconciliation_failed" not in reasons
    assert "investment_low_confidence" not in reasons
    for account, expected in zip((individual, ira, education), fidr.expected(stmt), strict=True):
        # Every activity row (the pending trade is none).
        _assert_activity(env, account, expected.activity)
        # The holdings, bonds by CUSIP (priced per 100), the core sweep as cash.
        assert env.holdings(account) == sorted((p[0], p[1]) for p in expected.positions)
    # Holdings printed by their name alone (the partnership, the 529's
    # portfolios) are securities without a symbol, priced from the statement.
    bare = {s.name: s for s in env.securities().values() if s.symbol is None}
    assert sorted(bare) == sorted([fidr.UNLISTED, *(p.name for p in stmt.accounts[2].positions)])
    assert all(s.cusip is None and s.isin is None for s in bare.values())
    unlisted = bare[fidr.UNLISTED]
    closes = env.query(select(Price.close).where(Price.security_id == unlisted.id))
    assert closes == [Decimal("187.500")]
    # A bond's statement price is one unit's: 98.625 per 100 of face value.
    bond = next(s for s in env.securities().values() if s.cusip == "94136RAC4")
    prices: list[Price] = env.query(select(Price).where(Price.security_id == bond.id))
    assert [p.close for p in prices] == [Decimal("0.98625")]


def _assert_activity(env: Env, account: Account, expected: list[Any]) -> None:
    """Trades, income, fees and transfers of units in ``investment_txn``; deposits
    and withdrawals of cash in the account's cash ledger."""
    cash_types = ("deposit", "withdrawal")
    assert env.activity(account) == [r for r in expected if r[1] not in cash_types]
    assert sorted(t.amount for t in env.cash_rows(account)) == sorted(
        r[4] for r in expected if r[1] in cash_types
    )
    assert all(t.source_document_id is not None for t in env.cash_rows(account))


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
    # The layout's parser knows the institution (from the PDF's metadata).
    names = [f.proposal.name for f in found.accounts if f.proposal is not None]
    assert names == [
        "Fidelity Individual - TOD 5817",
        "Fidelity Traditional IRA 0642",
        "Fidelity Education Account 9015",
    ]


def _printed(last4: str, opening: str, closing: str, amount: str, payee: str) -> dict[str, Any]:
    """One account's April statement as a document parser stores it (ledger terms)."""
    return {
        "source_format": "pdf",
        "account": {"last4": last4, "currency": "USD"},
        "period_start": "2026-04-01",
        "period_end": "2026-04-30",
        "opening_balance": {"amount": opening, "as_of": "2026-04-01"},
        "ledger_balance": {"amount": closing, "as_of": "2026-04-30"},
        "transactions": [
            {
                "posted": "2026-04-06",
                "amount": amount,
                "payee": payee,
                "low_confidence": ["reconciliation"],
            }
        ],
        "confidence": {"period": 1.0},
    }


def _rerun(env: Env, document_id: uuid.UUID, data: ApplyMapping | None = None) -> None:
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        request_apply(db, env.principal(), document_id, data or ApplyMapping(), request=None)
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)


def test_a_placed_statement_keeps_its_account_after_its_balances_are_corrected(env: Env) -> None:
    """The account a person chose follows the statement, not its figures."""
    known = env.ledger.account(
        env.household_id, type=AccountType.CHECKING, number_last4="7351", currency="USD"
    )
    chosen = env.ledger.account(
        env.household_id, type=AccountType.SAVINGS, currency="USD", name="Rainy Day"
    )
    read = ExtractResult.model_validate(
        {
            "kind": "pdf",
            "ok": True,
            "statements": [
                _printed("7351", "100.00", "88.00", "-12.00", "Lamplight Books"),
                # 50.00 - 5.00 is 45.00: the closing balance was read as 48.00.
                _printed("9951", "50.00", "48.00", "-5.00", "Ferry Kiosk"),
            ],
        }
    )
    read = read.model_copy(update={"statements": [rebalance(s) for s in read.statements]})
    with env.factories.transaction() as db:
        document_id = make_document(
            db,
            env.store,
            env.keyring,
            env.household_id,
            data=b"%PDF-1.7\n% synthetic placeholder " + uuid.uuid4().hex.encode(),
            mime="application/pdf",
            filename="statement.pdf",
            uploaded_by=env.owner.user.id,
            status=DocumentStatus.NEEDS_REVIEW,
            extraction=read.model_dump(mode="json"),
        ).id
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        fresh = db.get(Document, document_id)
        assert fresh is not None
        import_extraction(db, fresh, read)
    assert env.reasons(document_id) == {"account_not_found"}
    assert len(env.cash_rows(known)) == 1

    _rerun(
        env,
        document_id,
        ApplyMapping(assignments=[Assignment(target="statement", index=1, account_id=chosen.id)]),
    )
    placed = [r.id for r in env.cash_rows(chosen)]
    assert len(placed) == 1
    assert env.reasons(document_id) == {"reconciliation_failed"}
    old_key = statement_key(read.statements[1])

    # The closing balance is corrected: the statement is no longer the one
    # the placement named exactly.
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        fresh = db.get(Document, document_id)
        assert fresh is not None
        result = stored_result(fresh.extraction)
        assert result is not None
        second = result.statements[1]
        balance = second.ledger_balance
        assert balance is not None
        corrected = rebalance(
            second.model_copy(
                update={"ledger_balance": balance.model_copy(update={"amount": Decimal("45.00")})}
            )
        )
        assert statement_key(corrected) != old_key
        fresh.extraction = result.model_copy(
            update={"statements": [result.statements[0], corrected]}
        ).model_dump(mode="json")
    _rerun(env, document_id)

    assert "account_not_found" not in env.reasons(document_id)
    assert [r.id for r in env.cash_rows(chosen)] == placed
    assert len(env.cash_rows(known)) == 1
    assert [s.balance for s in env.snapshots(chosen)] == [Decimal("45.00")]
    resolved = [
        r.resolution
        for r in env.query(select(ReviewItem).where(ReviewItem.document_id == document_id))
        if r.resolution is not None and r.resolution.get("placed") is True
    ]
    assert resolved
    assert all(isinstance(r.get(REF_FIELD), str) for r in resolved)
