# SPDX-License-Identifier: AGPL-3.0-or-later
"""Matching against the ledger, where it needs a database.

``app.ingestion.match.dedupe.dedupe`` loads the import tombstones of the
account it imports into, and of no other. Every row is invented.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.core.clock import utcnow
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.imports.models import ImportTombstone
from app.domain.transactions.import_hash import HashInput, assign_import_hashes
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
