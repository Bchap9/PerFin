# SPDX-License-Identifier: AGPL-3.0-or-later
"""A deterministic synthetic ledger for report and budget tests.

Built by code with the committing ``ledger`` factories, so a ``TestClient``
app sees it. Base currency USD. Every amount is chosen so the expected
totals can be written down exactly (``Decimal``); the test modules state
them next to their assertions.

Accounts: ``checking`` and ``card`` (USD), ``euro`` (EUR; rate EUR->USD
1.1 from 2026-03-01), ``pound`` (GBP; no rate: its amounts stay
unconverted), ``hidden`` (USD; never granted to the member), ``savings``
(USD; the other leg of a transfer).

Categories: ``food`` > ``groceries`` > ``organic``; ``food`` >
``restaurants``; ``housing`` > ``rent``; ``salary`` (income); ``moves``
(transfer kind); ``ignored`` (ignore kind).

March 2026 (the main period):

=========  ===========  ======================  ===========================
account    date         amount                  category / note
=========  ===========  ======================  ===========================
checking   03-01        +3000.00                salary
checking   03-02        -100.25                 groceries, payee ``shop``
checking   03-05        -50.10                  organic, payee ``shop``
card       03-10        -40.00                  restaurants, payee ``diner``
euro       03-11        -10.00 EUR (11.00)      restaurants
euro       03-12        -3.333333 EUR (3.666666) restaurants
checking   03-12        -200.00 split           -150 rent, -50 groceries
checking   03-15        +12.30                  groceries (a refund)
checking   03-16        -500.00 / savings +500  a transfer (left out)
checking   03-17        -20.00                  moves (transfer kind, left out)
checking   03-18        -30.00                  ignored (left out)
checking   03-20        -7.77                   uncategorised
checking   03-21        +5.55                   uncategorised
checking   03-22        -999.00                 groceries, review pending
pound      03-23        -9.00 GBP               groceries (no rate)
hidden     03-24        -1000.00                groceries (hidden account)
=========  ===========  ======================  ===========================

Earlier: checking ``2026-02-14`` -60.00 groceries; checking ``2025-03-05``
-80.00 groceries (a year before March 2026).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date

from app.auth.models import GrantLevel
from app.domain.accounts.models import Account, AccountType
from app.domain.transactions.models import Category, CategoryKind, Payee, ReviewState
from tests.fixtures.ledger import LedgerFactories


@dataclass(frozen=True)
class ReportLedger:
    household_id: uuid.UUID
    checking: Account
    card: Account
    euro: Account
    pound: Account
    hidden: Account
    savings: Account
    food: Category
    groceries: Category
    organic: Category
    restaurants: Category
    housing: Category
    rent: Category
    salary: Category
    moves: Category
    ignored: Category
    shop: Payee
    diner: Payee

    @property
    def shared_accounts(self) -> tuple[Account, ...]:
        """The accounts granted to the member (``view``) by :func:`grant_member`."""
        return (self.checking, self.card, self.euro, self.savings)


def build(ledger: LedgerFactories, household_id: uuid.UUID) -> ReportLedger:
    hid = household_id
    checking = ledger.account(hid, name="Checking")
    card = ledger.account(hid, type=AccountType.CREDIT_CARD, name="Card")
    euro = ledger.account(hid, name="Euro", currency="EUR")
    pound = ledger.account(hid, name="Pound", currency="GBP")
    hidden = ledger.account(hid, name="Hidden")
    savings = ledger.account(hid, type=AccountType.SAVINGS, name="Savings")

    food = ledger.category(hid, name="Food")
    groceries = ledger.category(hid, name="Groceries", parent=food)
    organic = ledger.category(hid, name="Organic", parent=groceries)
    restaurants = ledger.category(hid, name="Restaurants", parent=food)
    housing = ledger.category(hid, name="Housing")
    rent = ledger.category(hid, name="Rent", parent=housing)
    salary = ledger.category(hid, name="Salary", kind=CategoryKind.INCOME)
    moves = ledger.category(hid, name="Moves", kind=CategoryKind.TRANSFER)
    ignored = ledger.category(hid, name="Ignored", kind=CategoryKind.IGNORE)
    shop = ledger.payee(hid, name="Shop")
    diner = ledger.payee(hid, name="Diner")

    ledger.fx_rate(hid, base="EUR", quote="USD", rate_date=date(2026, 3, 1), rate="1.1")

    def txn(account: Account, day: date, amount: str, **kwargs: object) -> None:
        ledger.transaction(account, posted_date=day, amount=amount, **kwargs)

    march = date(2026, 3, 1)
    txn(checking, march, "3000.00", category=salary)
    txn(checking, date(2026, 3, 2), "-100.25", category=groceries, payee=shop)
    txn(checking, date(2026, 3, 5), "-50.10", category=organic, payee=shop)
    txn(card, date(2026, 3, 10), "-40.00", category=restaurants, payee=diner)
    txn(euro, date(2026, 3, 11), "-10.00", category=restaurants)
    txn(euro, date(2026, 3, 12), "-3.333333", category=restaurants)
    split = ledger.transaction(checking, posted_date=date(2026, 3, 12), amount="-200.00")
    ledger.split(split, amount="-150.00", category=rent)
    ledger.split(split, amount="-50.00", category=groceries)
    txn(checking, date(2026, 3, 15), "12.30", category=groceries)
    group = uuid.uuid4()
    txn(checking, date(2026, 3, 16), "-500.00", transfer_group_id=group)
    txn(savings, date(2026, 3, 16), "500.00", transfer_group_id=group)
    txn(checking, date(2026, 3, 17), "-20.00", category=moves)
    txn(checking, date(2026, 3, 18), "-30.00", category=ignored)
    txn(checking, date(2026, 3, 20), "-7.77")
    txn(checking, date(2026, 3, 21), "5.55")
    txn(
        checking,
        date(2026, 3, 22),
        "-999.00",
        category=groceries,
        review_state=ReviewState.PENDING,
    )
    txn(pound, date(2026, 3, 23), "-9.00", category=groceries)
    txn(hidden, date(2026, 3, 24), "-1000.00", category=groceries)
    txn(checking, date(2026, 2, 14), "-60.00", category=groceries)
    txn(checking, date(2025, 3, 5), "-80.00", category=groceries)
    return ReportLedger(
        household_id=hid,
        checking=checking,
        card=card,
        euro=euro,
        pound=pound,
        hidden=hidden,
        savings=savings,
        food=food,
        groceries=groceries,
        organic=organic,
        restaurants=restaurants,
        housing=housing,
        rent=rent,
        salary=salary,
        moves=moves,
        ignored=ignored,
        shop=shop,
        diner=diner,
    )


def grant_member(ledger: LedgerFactories, book: ReportLedger, user_id: uuid.UUID) -> None:
    """Give ``user_id`` ``view`` on the shared accounts (not ``pound``, not ``hidden``)."""
    for account in book.shared_accounts:
        ledger.grant(book.household_id, user_id, account, GrantLevel.VIEW)
