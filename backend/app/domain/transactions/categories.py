# SPDX-License-Identifier: AGPL-3.0-or-later
"""The default category set seeded for every new household (VISION.md).

:func:`seed_default_categories` runs inside household creation
(``app.domain.households.service.create_household`` and first-run setup in
``app.auth.bootstrap``), in the new household's RLS context. It is
idempotent: categories are identified by ``system_key`` and only the missing
ones are inserted. Users may rename, move or delete seeded categories; a
deleted default is re-created only if the seed runs again for that household.

The set has editions (:data:`TAXONOMY_VERSION`). A key never changes and a
leaf never moves once shipped; a later edition only adds leaves, each marked
with the edition that introduced it (``DefaultCategory.since``). An existing
household is brought up to date by :func:`add_new_defaults`, which adds only
what it has not been offered before, so a default it deleted stays deleted.
A new name for an old key (``leisure.subscriptions`` is "Streaming" from
edition 2) reaches only households seeded from then on.

A household seeded from nothing was offered every leaf of this edition, and
the seed says so in its ``categorisation.state`` setting (:data:`STATE_KEY`,
``taxonomy``). Without that its first refresh would read "edition 1" and
bring back a newer default the household had deleted in the meantime.

``system_key`` is ``<group>`` for a top-level category and
``<group>.<name>`` for a child; names are English (the SPA may translate by
key). Every child has its parent's ``kind``. Tax treatments are hints for
the tax vault, editable per household.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core import clock
from app.db.models import Setting, SettingScope
from app.domain.transactions.models import Category, CategoryKind, TaxTreatment

#: Edition of the default set. A household records the edition it was last
#: brought up to (``categorisation.state``); :func:`add_new_defaults` adds
#: what later editions introduced.
TAXONOMY_VERSION: Final = 2
#: The household setting that records it (``app.domain.categorisation.service``
#: owns the setting and reads it; that module imports this one).
STATE_KEY: Final = "categorisation.state"

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
    #: The edition that introduced it.
    since: int = 1


@dataclass(frozen=True)
class _Added:
    """A child introduced by a later edition (:func:`_v2`)."""

    key: str
    name: str
    since: int


def _v2(key: str, name: str) -> _Added:
    return _Added(key, name, 2)


def _group(
    key: str, name: str, kind: CategoryKind, *children: tuple[str, ...] | _Added
) -> DefaultCategory:
    """A top-level default.

    Each child is ``(key, name)`` or ``(key, name, tax)`` for the first
    edition, :func:`_v2` for the second.
    """
    built = tuple(
        DefaultCategory(f"{key}.{child.key}", child.name, kind, since=child.since)
        if isinstance(child, _Added)
        else DefaultCategory(
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
        _v2("improvement", "Home improvement"),
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
        _v2("other", "Other utilities"),
    ),
    _group(
        "food",
        "Food and dining",
        _E,
        ("groceries", "Groceries"),
        ("restaurants", "Restaurants"),
        ("coffee", "Coffee and snacks"),
        _v2("fast_food", "Fast food"),
        _v2("alcohol", "Alcohol and bars"),
        _v2("delivery", "Food delivery"),
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
        _v2("general", "General merchandise"),
        _v2("home", "Home and garden"),
    ),
    _group(
        "leisure",
        "Leisure",
        _E,
        # "Streaming and subscriptions" in edition 1. A subscription is a
        # series, not a category; households seeded before keep their name.
        ("subscriptions", "Streaming"),
        ("hobbies", "Hobbies"),
        ("events", "Events and tickets"),
        ("travel", "Travel"),
        _v2("games", "Games"),
        _v2("news", "News and books"),
        _v2("software", "Software and apps"),
        _v2("memberships", "Memberships"),
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
        _v2("services", "Financial and legal services"),
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
        # Cash is moved, not spent: the kind keeps it out of spending totals.
        _v2("cash", "Cash and ATM"),
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
    A household that had no default yet is recorded as seeded at this edition
    (module doc).
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
    # No default yet: this is the household's first seed, at this edition.
    fresh = not existing
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
    if fresh:
        _record_edition(db, household_id)
    return added


def _record_edition(db: Session, household_id: uuid.UUID) -> None:
    """Store "seeded at :data:`TAXONOMY_VERSION`" unless the household has a state.

    Only the edition is claimed: no clean-up and no dictionary has looked at
    the household's rows and its memory is not seeded, so its first refresh
    still does everything else.
    """
    held = db.execute(
        select(Setting.id).where(
            Setting.scope == SettingScope.HOUSEHOLD,
            Setting.scope_id == household_id,
            Setting.key == STATE_KEY,
        )
    ).first()
    if held is not None:
        return
    now = clock.utcnow()
    db.add(
        Setting(
            scope=SettingScope.HOUSEHOLD,
            scope_id=household_id,
            key=STATE_KEY,
            value={
                "cleaner": 0,
                "dictionary": 0,
                "taxonomy": TAXONOMY_VERSION,
                "memory_seeded": False,
            },
            created_at=now,
            updated_at=now,
        )
    )
    db.flush()


def add_new_defaults(db: Session, household_id: uuid.UUID, since: int) -> int:
    """Add the default leaves introduced after edition ``since``; return how many.

    For a household that was seeded at edition ``since``. Nothing it already
    has is renamed, moved or re-created, and a leaf of edition ``since`` or
    earlier is never touched: one the household deleted stays deleted. Each
    newer leaf goes under the category that carries its group's key; a group
    the household removed gets nothing. A category of theirs with the same
    name (ignoring case) under that parent and no key takes the key instead
    of getting a twin; that is not counted, only inserted rows are. Running
    it again adds nothing. Must run in that household's RLS context.
    """
    wanted = [
        (group, leaf)
        for group in DEFAULT_CATEGORIES
        for leaf in group.children
        if leaf.since > since
    ]
    if not wanted:
        return 0
    by_key = category_ids_by_key(db, household_id)
    parents = {by_key[group.key] for group, _ in wanted if group.key in by_key}
    # A child has its parent's kind, and the household may have changed it.
    kinds: dict[uuid.UUID, CategoryKind] = {}
    # Their categories under those parents, by parent and case-folded name.
    siblings: dict[tuple[uuid.UUID, str], Category] = {}
    if parents:
        kinds.update(
            (category_id, kind)
            for category_id, kind in db.execute(
                select(Category.id, Category.kind).where(
                    Category.household_id == household_id, Category.id.in_(parents)
                )
            )
        )
        for category in db.execute(
            select(Category)
            .where(Category.household_id == household_id, Category.parent_id.in_(parents))
            .order_by(Category.id)
        ).scalars():
            if category.parent_id is not None:
                siblings.setdefault((category.parent_id, category.name.lower()), category)
    added = 0
    for group, leaf in wanted:
        parent_id = by_key.get(group.key)
        if parent_id is None or leaf.key in by_key:
            continue
        same_name = siblings.get((parent_id, leaf.name.lower()))
        if same_name is not None:
            # Keyed already means it is another default: no twin, no second key.
            if same_name.system_key is None:
                same_name.system_key = leaf.key
                by_key[leaf.key] = same_name.id
            continue
        db.add(
            Category(
                household_id=household_id,
                parent_id=parent_id,
                name=leaf.name,
                kind=kinds.get(parent_id, leaf.kind),
                tax_treatment=leaf.tax,
                system_key=leaf.key,
            )
        )
        added += 1
    db.flush()
    return added


def category_ids_by_key(db: Session, household_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """``system_key`` to category id, for the keyed categories the household has.

    A household that renamed or moved a default still has its key; one that
    deleted it does not. Must run in that household's RLS context.
    """
    return {
        key: category_id
        for key, category_id in db.execute(
            select(Category.system_key, Category.id).where(
                Category.household_id == household_id, Category.system_key.is_not(None)
            )
        )
        if key is not None
    }


def resolve(by_key: Mapping[str, uuid.UUID], key: str) -> uuid.UUID | None:
    """The household's category for a default ``key``.

    The category that carries ``key``; else the one that carries its group's
    key (``food.fast_food`` falls back to ``food``); else ``None``: a
    household with its own tree gets no guess for keys it does not have.
    """
    found = by_key.get(key)
    if found is not None:
        return found
    group, dot, _ = key.partition(".")
    return by_key.get(group) if dot else None
