# SPDX-License-Identifier: AGPL-3.0-or-later
"""The default category set seeded for every new household (VISION.md).

:func:`seed_default_categories` runs inside household creation
(``app.domain.households.service.create_household`` and first-run setup in
``app.auth.bootstrap``), in the new household's RLS context. It is
idempotent: categories are identified by ``system_key`` and only the missing
ones are inserted, so a later release can add defaults to existing
households. Users may rename, move or delete seeded categories; a deleted
default is re-created only if the seed runs again for that household.

``system_key`` is ``<group>`` for a top-level category and
``<group>.<name>`` for a child; names are English (the SPA may translate by
key). Every child has its parent's ``kind``. Tax treatments are hints for
the tax vault, editable per household.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.transactions.models import Category, CategoryKind, TaxTreatment

_E, _I, _T, _X = (
    CategoryKind.EXPENSE,
    CategoryKind.INCOME,
    CategoryKind.TRANSFER,
    CategoryKind.IGNORE,
)


@dataclass(frozen=True)
class DefaultCategory:
    key: str
    name: str
    kind: CategoryKind
    tax: TaxTreatment | None = None
    children: tuple[DefaultCategory, ...] = field(default=())


def _group(key: str, name: str, kind: CategoryKind, *children: tuple[str, ...]) -> DefaultCategory:
    """A top-level default; each child is ``(key, name)`` or ``(key, name, tax)``."""
    built = tuple(
        DefaultCategory(
            f"{key}.{child[0]}", child[1], kind, TaxTreatment(child[2]) if len(child) > 2 else None
        )
        for child in children
    )
    return DefaultCategory(key, name, kind, None, built)


_TAXABLE = TaxTreatment.TAXABLE_INCOME
_DEDUCTIBLE = TaxTreatment.DEDUCTIBLE

DEFAULT_CATEGORIES: Final[tuple[DefaultCategory, ...]] = (
    _group(
        "income",
        "Income",
        _I,
        ("salary", "Salary and wages", _TAXABLE),
        ("bonus", "Bonus", _TAXABLE),
        ("interest", "Interest", _TAXABLE),
        ("dividends", "Dividends", _TAXABLE),
        ("refunds", "Refunds"),
        ("gifts", "Gifts received"),
        ("other", "Other income"),
    ),
    _group(
        "housing",
        "Housing",
        _E,
        ("rent", "Rent"),
        ("mortgage_interest", "Mortgage interest", _DEDUCTIBLE),
        ("property_tax", "Property tax", _DEDUCTIBLE),
        ("insurance", "Home insurance"),
        ("maintenance", "Maintenance and repairs"),
        ("fees", "HOA and service charges"),
    ),
    _group(
        "utilities",
        "Utilities",
        _E,
        ("electricity", "Electricity"),
        ("heating", "Gas and heating"),
        ("water", "Water"),
        ("internet", "Internet"),
        ("phone", "Mobile phone"),
    ),
    _group(
        "food",
        "Food and dining",
        _E,
        ("groceries", "Groceries"),
        ("restaurants", "Restaurants"),
        ("coffee", "Coffee and snacks"),
    ),
    _group(
        "transport",
        "Transport",
        _E,
        ("fuel", "Fuel"),
        ("public_transit", "Public transit"),
        ("parking", "Parking and tolls"),
        ("car_maintenance", "Car maintenance"),
        ("car_insurance", "Car insurance"),
        ("taxi", "Taxi and rideshare"),
    ),
    _group(
        "health",
        "Health",
        _E,
        ("medical", "Medical", _DEDUCTIBLE),
        ("pharmacy", "Pharmacy"),
        ("dental", "Dental"),
        ("insurance", "Health insurance"),
        ("fitness", "Fitness"),
    ),
    _group(
        "shopping",
        "Shopping",
        _E,
        ("clothing", "Clothing"),
        ("electronics", "Electronics"),
        ("household", "Household supplies"),
    ),
    _group(
        "leisure",
        "Leisure",
        _E,
        ("subscriptions", "Streaming and subscriptions"),
        ("hobbies", "Hobbies"),
        ("events", "Events and tickets"),
        ("travel", "Travel"),
    ),
    _group(
        "family",
        "Family and personal",
        _E,
        ("personal_care", "Personal care"),
        ("education", "Education"),
        ("childcare", "Childcare"),
        ("pets", "Pets"),
        ("gifts", "Gifts given"),
        ("donations", "Charitable donations", _DEDUCTIBLE),
    ),
    _group(
        "financial",
        "Financial",
        _E,
        ("bank_fees", "Bank fees"),
        ("interest_paid", "Interest paid"),
        ("taxes", "Taxes", TaxTreatment.TAX_PAYMENT),
        ("insurance", "Life insurance"),
    ),
    _group(
        "transfers",
        "Transfers",
        _T,
        ("between_accounts", "Between accounts"),
        ("credit_card_payment", "Credit card payment"),
        ("savings", "Savings contribution"),
        ("investments", "Investment contribution"),
        ("loan_payment", "Loan payment"),
    ),
    _group("excluded", "Excluded", _X, ("reimbursable", "Reimbursable")),
)


def default_category_keys() -> list[str]:
    """Every ``system_key`` of the default set, parents before children."""
    keys: list[str] = []
    for group in DEFAULT_CATEGORIES:
        keys.append(group.key)
        keys.extend(child.key for child in group.children)
    return keys


def seed_default_categories(db: Session, household_id: uuid.UUID) -> int:
    """Insert the missing default categories of ``household_id``; return how many.

    Must run in that household's RLS context (``app.db.rls.set_context``).
    """
    existing: dict[str, uuid.UUID] = {
        key: category_id
        for key, category_id in db.execute(
            select(Category.system_key, Category.id).where(
                Category.household_id == household_id, Category.system_key.is_not(None)
            )
        )
        if key is not None
    }
    # A user category may already use a default's name under the same parent.
    taken: set[tuple[uuid.UUID | None, str]] = {
        (parent_id, name)
        for parent_id, name in db.execute(
            select(Category.parent_id, Category.name).where(Category.household_id == household_id)
        )
    }
    added = 0
    for group in DEFAULT_CATEGORIES:
        parent_id = existing.get(group.key)
        if parent_id is None and (None, group.name) in taken:
            continue
        if parent_id is None:
            parent = Category(
                household_id=household_id,
                name=group.name,
                kind=group.kind,
                tax_treatment=group.tax,
                system_key=group.key,
            )
            db.add(parent)
            db.flush()
            parent_id = parent.id
            existing[group.key] = parent_id
            added += 1
        for child in group.children:
            if child.key in existing or (parent_id, child.name) in taken:
                continue
            db.add(
                Category(
                    household_id=household_id,
                    parent_id=parent_id,
                    name=child.name,
                    kind=child.kind,
                    tax_treatment=child.tax,
                    system_key=child.key,
                )
            )
            added += 1
    db.flush()
    return added
