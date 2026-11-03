# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared ledger services on a real database: default categories, FX, balances."""

from __future__ import annotations

import contextlib
import random
import uuid
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.principal import Principal
from app.db.models import AuditLog
from app.db.rls import set_context
from app.domain.accounts.balances import (
    balance_as_of,
    balance_history,
    balances_as_of,
    running_balances,
)
from app.domain.accounts.models import BalanceSource
from app.domain.accounts.repo import upsert_snapshot
from app.domain.fx.service import (
    FxConverter,
    FxRateNotFoundError,
    convert,
    latest_rate,
    to_base,
)
from app.domain.households.schemas import HouseholdCreate
from app.domain.households.service import create_household
from app.domain.transactions.categories import (
    DEFAULT_CATEGORIES,
    default_category_keys,
    seed_default_categories,
)
from app.domain.transactions.models import Category, CategoryKind
from tests.fixtures.factories import make_household, make_user
from tests.fixtures.ledger import (
    household_context,
    make_account,
    make_category,
    make_fx_rate,
    make_snapshot,
    make_transaction,
    seed_categories,
)

pytestmark = pytest.mark.integration

D = Decimal
JAN = date(2026, 1, 1)


def day(n: int) -> date:
    return JAN + timedelta(days=n - 1)


# ---------------------------------------------------------------------------
# Default categories
# ---------------------------------------------------------------------------


def _categories(db: Session, household_id: uuid.UUID) -> list[Category]:
    set_context(db, household_id)
    return list(db.execute(select(Category).where(Category.household_id == household_id)).scalars())


def test_new_households_get_the_default_categories(db: Session) -> None:
    admin = make_user(db, role=None, is_instance_admin=True)
    principal = Principal(user=admin.user, household_id=None, role=None, auth_method="session")
    view = create_household(
        db,
        principal,
        HouseholdCreate(name="Synthetic Household", base_currency="EUR"),
        request=None,
    )
    household_id = view.household.id
    rows = _categories(db, household_id)
    assert sorted(c.system_key or "" for c in rows) == sorted(default_category_keys())
    by_key = {c.system_key: c for c in rows}
    for group in DEFAULT_CATEGORIES:
        parent = by_key[group.key]
        assert parent.parent_id is None
        assert parent.kind is group.kind
        for child in group.children:
            assert by_key[child.key].parent_id == parent.id
            assert by_key[child.key].tax_treatment == child.tax
    after = db.execute(
        select(AuditLog.after).where(
            AuditLog.household_id == household_id, AuditLog.action == "household.create"
        )
    ).scalar_one()
    assert after["default_categories"] == len(default_category_keys())


def test_seeding_is_idempotent_and_respects_user_categories(db: Session) -> None:
    household = make_household(db)
    # A user's own top-level "Income" blocks that default group (no duplicate
    # names under one parent); everything else is seeded once.
    make_category(db, household.id, name="Income", kind=CategoryKind.INCOME)
    first = seed_categories(db, household.id)
    income = next(g for g in DEFAULT_CATEGORIES if g.key == "income")
    assert first == len(default_category_keys()) - 1 - len(income.children)
    assert seed_categories(db, household.id) == 0
    rows = _categories(db, household.id)
    assert len(rows) == first + 1
    # A deleted default comes back when the seed runs again.
    set_context(db, household.id)
    leaf = next(c for c in rows if c.system_key == "food.coffee")
    db.delete(leaf)
    db.flush()
    with household_context(db, household.id):
        assert seed_default_categories(db, household.id) == 1


def test_seeding_stays_in_its_household(db: Session) -> None:
    a = make_household(db)
    b = make_household(db)
    seed_categories(db, a.id)
    set_context(db, b.id)
    assert db.execute(select(func.count()).select_from(Category)).scalar_one() == 0


# ---------------------------------------------------------------------------
# FX
# ---------------------------------------------------------------------------


def test_latest_rate_on_or_before_the_date(db: Session) -> None:
    h = make_household(db, currency="USD")
    make_fx_rate(db, h.id, base="EUR", quote="USD", rate_date=day(1), rate="1.10")
    make_fx_rate(db, h.id, base="EUR", quote="USD", rate_date=day(10), rate="1.20")
    set_context(db, h.id)
    assert latest_rate(db, h.id, "EUR", "USD", day(1) - timedelta(days=1)) is None
    for on, rate in ((day(1), "1.10"), (day(9), "1.10"), (day(10), "1.20"), (day(300), "1.20")):
        quote = latest_rate(db, h.id, "EUR", "USD", on)
        assert quote is not None
        assert (quote.stored_rate, quote.inverted) == (D(rate), False)
    assert convert(db, h.id, D("100"), "EUR", "USD", day(5)) == D("110.000000")
    assert to_base(db, h.id, D("100"), "EUR", day(15)) == D("120.000000")
    # The inverse direction divides by the stored rate (no look-ahead).
    assert convert(db, h.id, D("110"), "USD", "EUR", day(5)) == D("100.000000")
    assert convert(db, h.id, D("1"), "USD", "EUR", day(10)) == D("0.833333")
    assert convert(db, h.id, D("12.3456789"), "USD", "USD", day(10)) == D("12.345679")


def test_the_most_recent_direction_wins(db: Session) -> None:
    h = make_household(db, currency="USD")
    make_fx_rate(db, h.id, base="EUR", quote="USD", rate_date=day(1), rate="1.10")
    make_fx_rate(db, h.id, base="USD", quote="EUR", rate_date=day(5), rate="0.80")
    set_context(db, h.id)
    assert convert(db, h.id, D("100"), "EUR", "USD", day(3)) == D("110.000000")
    assert convert(db, h.id, D("100"), "EUR", "USD", day(6)) == D("125.000000")


def test_missing_rates_and_other_households_rates(db: Session) -> None:
    a = make_household(db, currency="USD")
    b = make_household(db, currency="USD")
    make_fx_rate(db, b.id, base="EUR", quote="USD", rate_date=day(1), rate="1.10")
    set_context(db, a.id)
    with pytest.raises(FxRateNotFoundError) as caught:
        to_base(db, a.id, D("1"), "EUR", day(5))
    assert (caught.value.status, caught.value.slug) == (422, "fx-rate-missing")
    assert FxConverter(db, a.id).try_convert(D("1"), "EUR", day(5)) is None


def test_converter_matches_convert(db: Session) -> None:
    h = make_household(db, currency="USD")
    rng = random.Random(7)
    for n in range(1, 60, 3):
        make_fx_rate(
            db,
            h.id,
            base="EUR",
            quote="USD",
            rate_date=day(n),
            rate=D(rng.randint(900, 1300)) / 1000,
        )
    for n in range(2, 60, 7):
        make_fx_rate(
            db,
            h.id,
            base="USD",
            quote="EUR",
            rate_date=day(n),
            rate=D(rng.randint(700, 1100)) / 1000,
        )
    set_context(db, h.id)
    converter = FxConverter(db, h.id)
    assert converter.target == "USD"
    for n in range(0, 70):
        amount = D(rng.randint(-100_000, 100_000)) / 100
        expected = None
        with contextlib.suppress(FxRateNotFoundError):
            expected = convert(db, h.id, amount, "EUR", "USD", day(n))
        assert converter.try_convert(amount, "EUR", day(n)) == expected
    assert converter.convert(D("5"), "USD", day(3)) == D("5.000000")


# ---------------------------------------------------------------------------
# Balances
# ---------------------------------------------------------------------------


def test_balance_from_snapshots_and_transactions(db: Session) -> None:
    h = make_household(db)
    account = make_account(db, h.id)
    make_transaction(db, account, amount="100", posted_date=day(1))
    make_transaction(db, account, amount="-30.25", posted_date=day(2))
    make_transaction(db, account, amount="-5", posted_date=day(4))
    make_snapshot(db, account, as_of_date=day(3), balance="1000", source=BalanceSource.STATEMENT)
    make_transaction(db, account, amount="7.5", posted_date=day(6))
    set_context(db, h.id)
    assert balance_as_of(db, account, day(0)) == 0
    assert balance_as_of(db, account, day(2)) == D("69.75")
    assert balance_as_of(db, account, day(3)) == D("1000")
    assert balance_as_of(db, account, day(5)) == D("995")
    assert balance_as_of(db, account, day(9)) == D("1002.5")
    # A manual correction on the same day beats the statement.
    with household_context(db, h.id):
        upsert_snapshot(db, account, day(3), D("900"), BalanceSource.MANUAL)
        upsert_snapshot(db, account, day(3), D("950"), BalanceSource.MANUAL)  # replaces
    assert balance_as_of(db, account, day(4)) == D("945")
    history = balance_history(db, account, day(1), day(6))
    assert [p.balance for p in history] == [
        D("100"),
        D("69.75"),
        D("950"),
        D("945"),
        D("945"),
        D("952.5"),
    ]
    running = running_balances(db, account, day(1), day(6))
    assert [(r.amount, r.balance) for r in running] == [
        (D("100"), D("100")),
        (D("-30.25"), D("69.75")),
        (D("-5"), D("945")),
        (D("7.5"), D("952.5")),
    ]


def test_manual_balance_accounts(db: Session) -> None:
    h = make_household(db)
    house = make_account(db, h.id, type="manual_asset", asset_kind="property")
    assert house.manual_balance
    assert house.is_asset
    make_snapshot(db, house, as_of_date=day(1), balance="300000")
    make_transaction(db, house, amount="-1000", posted_date=day(2))
    make_snapshot(db, house, as_of_date=day(10), balance="310000")
    set_context(db, h.id)
    assert balance_as_of(db, house, day(5)) == D("300000")
    assert balance_as_of(db, house, day(10)) == D("310000")
    assert balances_as_of(db, [house], day(5)) == {house.id: D("300000")}


def test_batch_balances_equal_single_balances(db: Session) -> None:
    h = make_household(db)
    rng = random.Random(11)
    accounts = [
        make_account(db, h.id, type=t)
        for t in ("checking", "savings", "credit_card", "cash", "manual_liability")
    ]
    for account in accounts:
        for _ in range(rng.randint(0, 25)):
            make_transaction(
                db,
                account,
                amount=D(rng.randint(-50_000, 50_000)) / 100,
                posted_date=day(rng.randint(1, 60)),
            )
        for _ in range(rng.randint(0, 3)):
            with household_context(db, h.id):
                upsert_snapshot(
                    db,
                    account,
                    day(rng.randint(1, 60)),
                    D(rng.randint(-100_000, 100_000)) / 100,
                    rng.choice(list(BalanceSource)),
                )
    other = make_household(db)
    make_transaction(db, make_account(db, other.id), amount="999", posted_date=day(1))
    set_context(db, h.id)
    for on in (day(0), day(15), day(30), day(61)):
        batch = balances_as_of(db, accounts, on)
        assert batch == {a.id: balance_as_of(db, a, on) for a in accounts}
        for account in accounts:
            history = balance_history(db, account, on, on)
            assert history[0].balance == batch[account.id]
    assert balances_as_of(db, [], day(1)) == {}
