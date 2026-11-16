# SPDX-License-Identifier: AGPL-3.0-or-later
"""Matching against the ledger, where it needs a database.

``app.ingestion.match.dedupe.dedupe`` loads the import tombstones of the
account it imports into, and of no other.
``app.ingestion.match.accounts.match_account`` puts a statement that prints no
account digits on the one open account of its institution, on the statement's
side of the balance sheet, and on none when there are several. Every row is
invented.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.core.clock import utcnow
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.imports.models import ImportTombstone
from app.domain.transactions.import_hash import HashInput, assign_import_hashes
from app.ingestion.match.accounts import AccountMatch, match_account
from app.ingestion.match.dedupe import DedupePlan, dedupe
from tests.fixtures.factories import Factories
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration

DAY = date(2026, 3, 4)
ROWS = [
    HashInput(DAY, Decimal("-7.25"), "Harbor Kiosk"),
    HashInput(DAY, Decimal("-7.25"), "Harbor Kiosk"),
    HashInput(DAY, Decimal("-31.00"), "Tin Roof Books"),
]


def _tombstone(
    factories: Factories, household_id: uuid.UUID, account: Account, digest: str
) -> None:
    with factories.transaction() as db:
        set_context(db, household_id)
        db.add(
            ImportTombstone(
                household_id=household_id,
                account_id=account.id,
                import_hash=digest,
                deleted_at=utcnow(),
                posted_date=DAY,
                amount=Decimal("-7.25"),
            )
        )


def _dedupe(factories: Factories, household_id: uuid.UUID, account: Account) -> DedupePlan:
    with factories.transaction() as db:
        set_context(db, household_id)
        return dedupe(db, household_id, account.id, ROWS)


def test_a_tombstone_of_the_account_is_loaded(
    factories: Factories, ledger: LedgerFactories
) -> None:
    household = factories.household()
    account = ledger.account(household.id, name="Everyday")
    hashes = assign_import_hashes(account.id, ROWS)
    _tombstone(factories, household.id, account, hashes[1])
    result = _dedupe(factories, household.id, account)
    assert result.hashes == hashes
    assert result.removed == [1]
    assert (result.exact, result.near, result.new) == ([], {}, [0, 2])


def test_a_tombstone_of_another_account_does_not_apply(
    factories: Factories, ledger: LedgerFactories
) -> None:
    household = factories.household()
    account = ledger.account(household.id, name="Everyday")
    other = ledger.account(household.id, name="Rainy day")
    hashes = assign_import_hashes(account.id, ROWS)
    # The same hash, remembered for another account of the household.
    _tombstone(factories, household.id, other, hashes[1])
    result = _dedupe(factories, household.id, account)
    assert result.removed == []
    assert (result.exact, result.near, result.new) == ([], {}, [0, 1, 2])


RESPELLED = [HashInput(row.posted_date, row.amount, f"{row.raw_payee} POS 0042") for row in ROWS]


def test_a_deleted_row_spelled_another_way_is_held(
    factories: Factories, ledger: LedgerFactories
) -> None:
    """The same month from another file: the tombstone's date and amount are
    read, and the row it fits waits instead of coming back as new."""
    household = factories.household()
    account = ledger.account(household.id, name="Everyday")
    _tombstone(factories, household.id, account, assign_import_hashes(account.id, ROWS)[1])
    with factories.transaction() as db:
        set_context(db, household.id)
        result = dedupe(db, household.id, account.id, RESPELLED)
    assert result.removed == []
    # One tombstone, one row held; its twin and the other purchase are new.
    assert result.near == {0: []}
    assert result.resembles_removed == [0]
    assert result.new == [1, 2]


def test_a_respelled_row_is_not_held_for_another_accounts_tombstone(
    factories: Factories, ledger: LedgerFactories
) -> None:
    household = factories.household()
    account = ledger.account(household.id, name="Everyday")
    other = ledger.account(household.id, name="Rainy day")
    _tombstone(factories, household.id, other, assign_import_hashes(account.id, ROWS)[1])
    with factories.transaction() as db:
        set_context(db, household.id)
        result = dedupe(db, household.id, account.id, RESPELLED)
    assert (result.near, result.resembles_removed, result.new) == ({}, [], [0, 1, 2])


# ---------------------------------------------------------------------------
# The account of a statement that prints no digits
# ---------------------------------------------------------------------------


def _match(factories: Factories, household_id: uuid.UUID, **kwargs: Any) -> AccountMatch:
    with factories.transaction() as db:
        set_context(db, household_id)
        return match_account(db, household_id, **kwargs)


def test_sole_account_of_the_institution_matches_without_digits(
    factories: Factories, ledger: LedgerFactories
) -> None:
    household = factories.household()
    bank = ledger.institution(household.id, name="Larkspur Mutual")
    elsewhere = ledger.institution(household.id, name="Tidewater Savings")
    account = ledger.account(household.id, name="Everyday", institution_id=bank.id)
    ledger.account(household.id, name="Rainy day", institution_id=elsewhere.id)
    ledger.account(household.id, name="Cash tin")
    for last4 in (None, ""):
        result = _match(factories, household.id, last4=last4, institution_id=bank.id)
        assert result == AccountMatch(account.id, (account.id,), by="institution")
    # No digits and no institution: nothing to go by, as before.
    assert _match(factories, household.id, last4=None) == AccountMatch(None)


def test_an_institution_of_another_household_matches_nothing(
    factories: Factories, ledger: LedgerFactories
) -> None:
    household = factories.household()
    other = factories.household()
    bank = ledger.institution(other.id, name="Larkspur Mutual")
    ledger.account(other.id, name="Everyday", institution_id=bank.id)
    result = _match(factories, household.id, last4=None, institution_id=bank.id)
    assert result == AccountMatch(None)


def test_two_accounts_of_the_institution_are_candidates_only(
    factories: Factories, ledger: LedgerFactories
) -> None:
    household = factories.household()
    bank = ledger.institution(household.id, name="Larkspur Mutual")
    first = ledger.account(household.id, name="Everyday", institution_id=bank.id)
    second = ledger.account(
        household.id, type=AccountType.SAVINGS, name="Rainy day", institution_id=bank.id
    )
    result = _match(factories, household.id, last4=None, institution_id=bank.id)
    assert result.account_id is None
    assert result.by is None
    assert set(result.candidates) == {first.id, second.id}
    assert result.candidates == tuple(sorted(result.candidates))


def test_card_statement_does_not_land_on_the_checking_account(
    factories: Factories, ledger: LedgerFactories
) -> None:
    household = factories.household()
    bank = ledger.institution(household.id, name="Larkspur Mutual")
    checking = ledger.account(household.id, name="Everyday", institution_id=bank.id)
    result = _match(factories, household.id, last4=None, institution_id=bank.id, liability=True)
    assert result == AccountMatch(None)
    # A bank statement lands on it, and a card of the same institution is no
    # second candidate for it.
    card = ledger.account(
        household.id, type=AccountType.CREDIT_CARD, name="Card", institution_id=bank.id
    )
    asset = _match(factories, household.id, last4=None, institution_id=bank.id, liability=False)
    assert asset == AccountMatch(checking.id, (checking.id,), by="institution")
    owed = _match(factories, household.id, last4=None, institution_id=bank.id, liability=True)
    assert owed == AccountMatch(card.id, (card.id,), by="institution")
    # The side unknown: both are candidates, neither is chosen.
    either = _match(factories, household.id, last4=None, institution_id=bank.id)
    assert either.account_id is None
    assert set(either.candidates) == {checking.id, card.id}


@pytest.mark.parametrize("liability", [None, False])
@pytest.mark.parametrize("kind", [AccountType.BROKERAGE, AccountType.RETIREMENT])
def test_bank_statement_does_not_land_on_the_sole_investment_account(
    factories: Factories, ledger: LedgerFactories, kind: AccountType, liability: bool | None
) -> None:
    household = factories.household()
    bank = ledger.institution(household.id, name="Larkspur Mutual")
    invest = ledger.account(household.id, type=kind, name="Long term", institution_id=bank.id)
    result = _match(
        factories, household.id, last4=None, institution_id=bank.id, liability=liability
    )
    # Named for a person to choose, never chosen.
    assert result == AccountMatch(None, (invest.id,))


@pytest.mark.parametrize(
    "kind", [AccountType.LOAN, AccountType.MORTGAGE, AccountType.LINE_OF_CREDIT]
)
def test_card_statement_does_not_land_on_the_sole_loan(
    factories: Factories, ledger: LedgerFactories, kind: AccountType
) -> None:
    household = factories.household()
    bank = ledger.institution(household.id, name="Larkspur Mutual")
    loan = ledger.account(household.id, type=kind, name="Borrowed", institution_id=bank.id)
    result = _match(factories, household.id, last4=None, institution_id=bank.id, liability=True)
    assert result == AccountMatch(None, (loan.id,))
    assert _match(factories, household.id, last4=None, institution_id=bank.id) == result


def test_side_unknown_never_chooses_a_card(factories: Factories, ledger: LedgerFactories) -> None:
    household = factories.household()
    bank = ledger.institution(household.id, name="Larkspur Mutual")
    card = ledger.account(
        household.id, type=AccountType.CREDIT_CARD, name="Card", institution_id=bank.id
    )
    args = {"last4": None, "institution_id": bank.id}
    assert _match(factories, household.id, **args) == AccountMatch(None, (card.id,))
    # Known to be a card statement, it is the match.
    owed = _match(factories, household.id, liability=True, **args)
    assert owed == AccountMatch(card.id, (card.id,), by="institution")


def test_an_account_of_another_kind_beside_it_leaves_candidates_only(
    factories: Factories, ledger: LedgerFactories
) -> None:
    household = factories.household()
    bank = ledger.institution(household.id, name="Larkspur Mutual")
    checking = ledger.account(household.id, name="Everyday", institution_id=bank.id)
    invest = ledger.account(
        household.id, type=AccountType.BROKERAGE, name="Long term", institution_id=bank.id
    )
    result = _match(factories, household.id, last4=None, institution_id=bank.id, liability=False)
    assert result.account_id is None
    assert set(result.candidates) == {checking.id, invest.id}


def test_digits_that_match_nothing_do_not_fall_back(
    factories: Factories, ledger: LedgerFactories
) -> None:
    household = factories.household()
    bank = ledger.institution(household.id, name="Larkspur Mutual")
    account = ledger.account(
        household.id, name="Everyday", institution_id=bank.id, number_last4="4410"
    )
    missed = _match(factories, household.id, last4="7302", institution_id=bank.id)
    assert missed == AccountMatch(None)
    # Digits that do match are matched by digits, as before.
    found = _match(factories, household.id, last4="4410", institution_id=bank.id)
    assert found.account_id == account.id
    assert found.by == "digits"


def test_an_account_outside_allowed_is_never_chosen(
    factories: Factories, ledger: LedgerFactories
) -> None:
    household = factories.household()
    bank = ledger.institution(household.id, name="Larkspur Mutual")
    hidden = ledger.account(household.id, name="Everyday", institution_id=bank.id)
    args = {"last4": None, "institution_id": bank.id}
    assert _match(factories, household.id, allowed=frozenset(), **args) == AccountMatch(None)
    # Two accounts, one of them the uploader may edit: that one, and the
    # other is not even named as a candidate.
    mine = ledger.account(
        household.id, type=AccountType.SAVINGS, name="Rainy day", institution_id=bank.id
    )
    result = _match(factories, household.id, allowed=frozenset({mine.id}), **args)
    assert result == AccountMatch(mine.id, (mine.id,), by="institution")
    assert hidden.id not in result.candidates


def test_closed_account_is_not_chosen(factories: Factories, ledger: LedgerFactories) -> None:
    household = factories.household()
    bank = ledger.institution(household.id, name="Larkspur Mutual")
    closed = ledger.account(
        household.id, name="Old everyday", institution_id=bank.id, closed_on=date(2025, 6, 30)
    )
    args = {"last4": None, "institution_id": bank.id}
    assert _match(factories, household.id, **args) == AccountMatch(None)
    # The closed account does not stand in the way of the open one either.
    current = ledger.account(household.id, name="Everyday", institution_id=bank.id)
    result = _match(factories, household.id, **args)
    assert result == AccountMatch(current.id, (current.id,), by="institution")
    assert closed.id not in result.candidates
