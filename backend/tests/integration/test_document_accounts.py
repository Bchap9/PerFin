# SPDX-License-Identifier: AGPL-3.0-or-later
"""A document's accounts: matched, proposed, created and imported in one step.

``app.domain.imports.proposals`` on synthetic brokerage activity exports
(every value invented): accounts PerFin knows are matched, the others come
with a proposal, and applying the choices creates the accounts and imports
the file into them.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.principal import apply_rls_context
from app.core.errors import ProblemError
from app.domain.accounts.models import Account, AccountType, Institution
from app.domain.accounts.schemas import AccountCreate
from app.domain.documents.models import DocumentStatus, ReviewKind
from app.domain.imports import proposals
from app.domain.imports.schemas import AccountChoice, DocumentAccounts, DocumentAccountsApply
from app.domain.transactions.models import Transaction
from tests.fixtures import synth_unseen_layouts as unseen
from tests.fixtures.synth_activity_csv import (
    ActivityAccount,
    activity_accounts,
    activity_csv_bytes,
    investment_rows,
)
from tests.integration.test_activity_table_import import Env, _expected, env

pytestmark = pytest.mark.integration
__all__ = ["env"]


def _masked(synthetic: tuple[ActivityAccount, ...]) -> tuple[ActivityAccount, ...]:
    return tuple(
        ActivityAccount(a.name, a.number[:1] + str(i + 2) + "x" * (len(a.number) - 2))
        for i, a in enumerate(synthetic)
    )


def _detect(env: Env, document_id: Any) -> DocumentAccounts:
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        return proposals.detect_accounts(db, env.principal(), document_id)


def _apply(env: Env, document_id: Any, *choices: AccountChoice) -> list[Any]:
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        applied = proposals.apply_accounts(
            db,
            env.principal(),
            document_id,
            DocumentAccountsApply(items=list(choices)),
            request=None,
        )
    env.run_apply()
    return applied.created


def _create(found: Any) -> AccountChoice:
    proposal = found.proposal
    assert proposal is not None
    return AccountChoice(
        target=found.target,
        index=found.index,
        create=AccountCreate(
            type=proposal.type,
            subtype=proposal.subtype,
            name=proposal.name,
            currency=proposal.currency or "USD",
        ),
        institution_name=proposal.institution_name,
    )


def test_masked_accounts_are_proposed_created_and_imported(env: Env) -> None:
    synthetic = _masked(activity_accounts(91))
    rows = investment_rows(synthetic)
    document_id = env.ingest(activity_csv_bytes(rows))
    found = _detect(env, document_id).accounts
    assert [(a.target, a.index, a.status) for a in found] == [
        ("investment", 0, "new"),
        ("investment", 1, "new"),
    ]
    first, second = found
    assert first.proposal is not None
    assert second.proposal is not None
    assert first.proposal.name == f"Fidelity {synthetic[0].name}"
    assert (first.proposal.type, second.proposal.type) == (
        AccountType.BROKERAGE,
        AccountType.RETIREMENT,
    )
    assert first.proposal.institution_name == "Fidelity Investments"
    assert (first.proposal.subtype, second.proposal.subtype) == ("individual", "roth_ira")
    assert first.review_item_id is not None
    created = _apply(env, document_id, _create(first), _create(second))
    assert len(created) == 2
    accounts = {a.id: a for a in env.query(select(Account).where(Account.id.in_(created)))}
    one, two = (accounts[i] for i in created)
    assert env.activity(one) == _expected(rows, synthetic[0])
    assert env.activity(two) == _expected(rows, synthetic[1])
    (institution,) = env.query(select(Institution))
    assert one.institution_id == two.institution_id == institution.id
    assert (one.subtype, two.subtype) == ("individual", "roth_ira")
    assert not [i for i in env.open_reviews(document_id) if i.kind is ReviewKind.UNKNOWN_ACCOUNT]
    assert [a.status for a in _detect(env, document_id).accounts] == ["done", "done"]


def test_known_and_new_accounts_of_one_file(env: Env) -> None:
    synthetic = activity_accounts(92)
    rows = investment_rows(synthetic)
    (known, _) = env.accounts(synthetic, only=(0,))
    assert known is not None
    document_id = env.ingest(activity_csv_bytes(rows))
    first, second = _detect(env, document_id).accounts
    assert (first.status, first.account_id, first.proposal) == ("matched", known.id, None)
    assert second.status == "new"
    assert second.last4 == synthetic[1].last4
    assert second.proposal is not None
    assert second.proposal.number_last4 == synthetic[1].last4
    (created,) = _apply(env, document_id, _create(second))
    (account,) = env.query(select(Account).where(Account.id == created))
    assert account.number_last4 == synthetic[1].last4  # the next file matches on its own
    assert env.activity(account) == _expected(rows, synthetic[1])
    assert env.activity(known) == _expected(rows, synthetic[0])


def test_an_existing_account_remembers_the_digits(env: Env) -> None:
    synthetic = activity_accounts(93)
    rows = investment_rows(synthetic)
    document_id = env.ingest(activity_csv_bytes(rows))
    plain = env.ledger.account(
        env.household_id, type=AccountType.BROKERAGE, currency="USD", name="Unnumbered"
    )
    _, second = _detect(env, document_id).accounts
    _apply(
        env,
        document_id,
        AccountChoice(target=second.target, index=second.index, account_id=plain.id),
    )
    (account,) = env.query(select(Account).where(Account.id == plain.id))
    assert account.number_last4 == synthetic[1].last4
    assert env.activity(account) == _expected(rows, synthetic[1])


def test_choices_that_do_not_fit_are_refused(env: Env) -> None:
    synthetic = activity_accounts(94)
    document_id = env.ingest(activity_csv_bytes(investment_rows(synthetic)))
    with pytest.raises(ProblemError) as missing:
        _apply(
            env,
            document_id,
            AccountChoice(
                target="investment",
                index=7,
                create=AccountCreate(type=AccountType.BROKERAGE, name="Nowhere"),
            ),
        )
    assert missing.value.slug == "invalid-account-choice"
    with pytest.raises(ProblemError) as wrong:
        _apply(
            env,
            document_id,
            AccountChoice(
                target="investment",
                index=0,
                create=AccountCreate(type=AccountType.CHECKING, name="Not a brokerage"),
            ),
        )
    assert wrong.value.slug == "invalid-account-choice"
    assert env.query(select(Account).where(Account.name == "Not a brokerage")) == []


def test_a_bank_statement_proposal_carries_its_opening_balance() -> None:
    from datetime import date
    from decimal import Decimal

    found = proposals._Found(
        target="statement",
        index=0,
        last4="4321",
        name=None,
        institution="Synthetic Savings Bank",
        account_type="CHECKING",
        currency="USD",
        period_start=date(2026, 10, 1),
        period_end=date(2026, 10, 31),
        opening_balance=Decimal("120.50"),
        closing_balance=Decimal("98.10"),
        transactions=4,
    )
    proposal = proposals._proposal(found, found.institution, "USD")
    assert proposal.name == "Synthetic Checking 4321"
    assert proposal.type is AccountType.CHECKING
    assert (proposal.opening_balance, proposal.opening_balance_date) == (
        "120.50",
        date(2026, 10, 1),
    )
    card = proposals._proposal(
        proposals._Found(**{**found.__dict__, "account_type": "CREDITCARD", "institution": None}),
        None,
        "EUR",
    )
    assert (card.name, card.type, card.currency) == (
        "Credit card 4321",
        AccountType.CREDIT_CARD,
        "USD",
    )


# ---------------------------------------------------------------------------
# A bank PerFin has never seen (tests/fixtures/synth_unseen_layouts.py)
# ---------------------------------------------------------------------------


def _bank_rows(env: Env, account_id: Any) -> list[tuple[Any, Any]]:
    rows = env.query(select(Transaction).where(Transaction.account_id == account_id))
    return sorted((t.posted_date, t.amount) for t in rows)


def test_a_new_banks_first_statement_sets_it_up_and_the_next_imports_itself(env: Env) -> None:
    first_id = env.ingest(unseen.summary_block_checking(), "january.pdf")
    # Nothing shipped knows the layout: the header names the sender.
    confidence = env.document(first_id).confidence
    assert confidence is not None
    assert confidence["method"] == "classifier"
    assert confidence["institution_name"] == "Northwind Bank, N.A."
    (found,) = _detect(env, first_id).accounts
    assert (found.status, found.last4) == ("new", "3377")
    assert found.institution_name == "Northwind Bank, N.A."
    assert found.proposal is not None
    assert found.proposal.name == "Northwind Checking 3377"
    assert found.proposal.institution_name == "Northwind Bank, N.A."
    assert found.proposal.number_last4 == "3377"
    assert found.proposal.opening_balance == "2418.77"

    (account_id,) = _apply(env, first_id, _create(found))
    (institution,) = env.query(select(Institution))
    assert institution.name == "Northwind Bank, N.A."
    (account,) = env.query(select(Account).where(Account.id == account_id))
    assert (account.number_last4, account.institution_id) == ("3377", institution.id)
    assert len(_bank_rows(env, account_id)) == 9
    assert not env.open_reviews(first_id)

    # The next month's statement: the bank and the account are known now.
    second_id = env.ingest(unseen.summary_block_checking_next(), "february.pdf")
    second = env.document(second_id)
    assert second.status is DocumentStatus.PROCESSED, env.open_reviews(second_id)
    assert (second.account_id, second.institution_id) == (account_id, institution.id)
    assert not env.open_reviews(second_id)
    assert len(_bank_rows(env, account_id)) == 12


def test_shares_of_one_member_become_two_accounts(env: Env) -> None:
    document_id = env.ingest(unseen.credit_union_shares(), "april.pdf")
    savings, checking = _detect(env, document_id).accounts
    assert [(a.status, a.last4, a.name) for a in (savings, checking)] == [
        ("new", "0000", "PRIMARY SAVINGS"),
        ("new", "0010", "FREE CHECKING"),
    ]
    assert savings.proposal is not None
    assert checking.proposal is not None
    assert (savings.proposal.type, checking.proposal.type) == (
        AccountType.SAVINGS,
        AccountType.CHECKING,
    )
    assert savings.proposal.institution_name == "Prairie Lakes Credit Union"
    assert (savings.proposal.opening_balance, checking.proposal.opening_balance) == (
        "5000.00",
        "812.44",
    )
    one, two = _apply(env, document_id, _create(savings), _create(checking))
    assert [amount for _, amount in _bank_rows(env, one)] == [Decimal("300"), Decimal("1.09")]
    assert len(_bank_rows(env, two)) == 4
    assert not env.open_reviews(document_id)
