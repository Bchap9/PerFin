# SPDX-License-Identifier: AGPL-3.0-or-later
"""The reportable ledger as SQL, aggregation and currency conversion.

Every report (spending, income, trends, cash flow) and every budget's
"actual" column is built from the same **ledger lines**
(:func:`ledger_lines`), one row per categorised amount:

* a transaction without splits is one line (its category and amount); a
  split transaction is one line per split (the split's category and
  amount), never the transaction itself (DATA_MODEL.md "Ledger": reports
  read the splits when there are any);
* only the active household and only accounts the caller may see
  (``app.domain.accounts.visibility.visible_account_filter``: a member or
  viewer never gets amounts of an account that is not granted to them);
* transactions still waiting in the review queue (``review_state =
  pending``) are left out unless ``include_pending``
  (``app.domain.transactions.service.reportable_clause``);
* transfers between own accounts are not income or spending: legs of a
  transfer (``transfer_group_id`` set) and lines in a category of kind
  ``transfer`` or ``ignore`` are left out;
* each line has a **flow**: its category's kind (``expense`` or
  ``income``); an uncategorised line is ``expense`` when negative and
  ``income`` otherwise.

Amounts stay signed as stored (``< 0`` money out). The date is the posted
date. Aggregation runs in SQL (``GROUP BY`` with bound parameters only);
lines in the report currency are summed outright, lines in another currency
are summed per posted date (``fx_day``) so :class:`Accumulator` can convert
each daily sum with the rate of that day (``app.domain.fx.service``:
latest rate on or before the day, rounded once, half-even, to 6 decimals).
A sum whose rate is missing is not guessed: it is reported in its own
currency under ``unconverted``.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import (
    Boolean,
    ColumnElement,
    Date,
    String,
    Subquery,
    and_,
    case,
    cast,
    exists,
    func,
    literal,
    null,
    or_,
    select,
)
from sqlalchemy.orm import Session, aliased

from app.auth.principal import Principal
from app.core.money import Money, sum_amounts
from app.domain.accounts.visibility import visible_account_filter
from app.domain.fx.service import FxConverter
from app.domain.transactions.models import (
    Category,
    CategoryKind,
    ScheduledTransaction,
    Transaction,
    TransactionSplit,
)
from app.domain.transactions.service import reportable_clause

EXPENSE: Final = "expense"
INCOME: Final = "income"
#: Category kinds whose lines never count as income or spending.
EXCLUDED_KINDS: Final = (CategoryKind.TRANSFER, CategoryKind.IGNORE)
ZERO: Final = Decimal(0)


@dataclass(frozen=True)
class LedgerScope:
    """What a report reads: household, posted-date window, currency, filters."""

    household_id: uuid.UUID
    date_from: date
    date_to: date
    #: Currency the report is in: lines in it are summed without conversion.
    currency: str
    include_pending: bool = False
    #: Restrict to these accounts (already checked visible); ``None``: all visible.
    account_ids: tuple[uuid.UUID, ...] | None = None


def ledger_lines(
    principal: Principal, scope: LedgerScope, *, recurring_from: date | None = None
) -> Subquery:
    """The ledger lines of ``scope`` as a subquery (module doc).

    Columns: ``account_id``, ``posted_date``, ``month`` (first day of the
    posted month), ``fx_day`` (NULL for lines in ``scope.currency``, else the
    posted date), ``currency``, ``category_id``, ``payee_id``, ``amount``,
    ``flow`` (``'expense'`` or ``'income'``) and ``recurring``: with
    ``recurring_from``, whether a line posted on or after that date matches
    an active scheduled transaction (same account, payee and category); the
    budget forecast projects only the other lines linearly.
    """
    split = aliased(TransactionSplit)
    category_id = case((split.id.is_(None), Transaction.category_id), else_=split.category_id)
    amount = case((split.id.is_(None), Transaction.amount), else_=split.amount)
    flow = case(
        (Category.kind.is_not(None), cast(Category.kind, String)),
        (amount < 0, literal(EXPENSE)),
        else_=literal(INCOME),
    )
    fx_day = case((Transaction.currency == scope.currency, null()), else_=Transaction.posted_date)
    month = cast(func.date_trunc("month", Transaction.posted_date), Date)
    recurring: ColumnElement[bool]
    if recurring_from is None:
        recurring = cast(literal(False), Boolean)
    else:
        schedule = aliased(ScheduledTransaction)
        recurring = and_(
            Transaction.posted_date >= recurring_from,
            exists(
                select(schedule.id).where(
                    schedule.household_id == Transaction.household_id,
                    schedule.active.is_(True),
                    schedule.account_id == Transaction.account_id,
                    schedule.payee_id == Transaction.payee_id,
                    schedule.category_id == category_id,
                )
            ),
        )
    conditions: list[ColumnElement[bool]] = [
        Transaction.household_id == scope.household_id,
        Transaction.posted_date >= scope.date_from,
        Transaction.posted_date <= scope.date_to,
        Transaction.transfer_group_id.is_(None),
        visible_account_filter(principal, Transaction.account_id),
        reportable_clause(scope.include_pending),
        or_(Category.kind.is_(None), Category.kind.not_in(EXCLUDED_KINDS)),
    ]
    if scope.account_ids is not None:
        conditions.append(Transaction.account_id.in_(scope.account_ids))
    stmt = (
        select(
            Transaction.account_id.label("account_id"),
            Transaction.posted_date.label("posted_date"),
            month.label("month"),
            fx_day.label("fx_day"),
            Transaction.currency.label("currency"),
            category_id.label("category_id"),
            Transaction.payee_id.label("payee_id"),
            amount.label("amount"),
            flow.label("flow"),
            recurring.label("recurring"),
        )
        .select_from(Transaction)
        .outerjoin(
            split,
            and_(
                split.household_id == Transaction.household_id,
                split.transaction_id == Transaction.id,
            ),
        )
        .outerjoin(
            Category,
            and_(Category.household_id == Transaction.household_id, Category.id == category_id),
        )
        .where(*conditions)
    )
    return stmt.subquery("ledger_line")


@dataclass(frozen=True)
class Sum:
    """One ``GROUP BY`` row: the group's key values and its sums in ``currency``."""

    key: tuple[Any, ...]
    currency: str
    fx_day: date | None
    total: Decimal
    inflow: Decimal
    outflow: Decimal
    recurring: Decimal


def aggregate(
    db: Session,
    lines: Subquery,
    keys: Sequence[str],
    *,
    flow: str | None = None,
) -> list[Sum]:
    """Sums of ``lines`` grouped by the columns ``keys`` (plus currency and fx day).

    ``flow`` keeps only ``'expense'`` or ``'income'`` lines. Each row carries
    the signed total, the sum of the positive lines (``inflow``), of the
    negative lines (``outflow``, still negative) and of the ``recurring``
    lines.
    """
    key_columns = [lines.c[name] for name in keys]
    amount = lines.c.amount
    stmt = select(
        *key_columns,
        lines.c.currency,
        lines.c.fx_day,
        func.sum(amount),
        func.coalesce(func.sum(amount).filter(amount > 0), 0),
        func.coalesce(func.sum(amount).filter(amount < 0), 0),
        func.coalesce(func.sum(amount).filter(lines.c.recurring), 0),
    ).group_by(*key_columns, lines.c.currency, lines.c.fx_day)
    if flow is not None:
        stmt = stmt.where(lines.c.flow == flow)
    width = len(keys)
    result: list[Sum] = []
    for row in db.execute(stmt):
        values = tuple(row)
        result.append(
            Sum(
                key=values[:width],
                currency=values[width],
                fx_day=values[width + 1],
                total=Decimal(values[width + 2]),
                inflow=Decimal(values[width + 3]),
                outflow=Decimal(values[width + 4]),
                recurring=Decimal(values[width + 5]),
            )
        )
    return result


@dataclass
class Accumulator:
    """Adds amounts converted into one currency, per key.

    Amounts in the target currency are added as they are; others are
    converted with the rate of their day (:class:`FxConverter`). An amount
    without a rate is not added: its source-currency value is kept in
    :attr:`unconverted` so the response can say what is missing.
    """

    converter: FxConverter
    values: dict[Hashable, Decimal] = field(default_factory=dict)
    unconverted: dict[str, Decimal] = field(default_factory=dict)

    @property
    def currency(self) -> str:
        return self.converter.target

    def convert(self, amount: Decimal, currency: str, on: date | None) -> Decimal | None:
        """``amount`` in the target currency; ``None`` (and recorded) without a rate."""
        if currency == self.converter.target:
            return amount
        if on is None:  # pragma: no cover - fx_day is set for every foreign line
            raise ValueError("a foreign-currency amount needs its date")
        converted = self.converter.try_convert(amount, currency, on)
        if converted is None:
            self.unconverted[currency] = sum_amounts((self.unconverted.get(currency, ZERO), amount))
        return converted

    def convert_parts(
        self, parts: Sequence[Decimal], currency: str, on: date | None, *, missing: Decimal
    ) -> list[Decimal] | None:
        """Convert several amounts of one currency and day with one rate.

        Without a rate, ``missing`` (the amount the caller would have shown,
        in ``currency``) is recorded in :attr:`unconverted` and ``None`` is
        returned.
        """
        if currency == self.converter.target:
            return list(parts)
        if on is None:  # pragma: no cover - fx_day is set for every foreign line
            raise ValueError("a foreign-currency amount needs its date")
        quote = self.converter.quote(currency, on)
        if quote is None:
            self.unconverted[currency] = sum_amounts(
                (self.unconverted.get(currency, ZERO), missing)
            )
            return None
        return [quote.apply(part) for part in parts]

    def add(self, key: Hashable, amount: Decimal, currency: str, on: date | None) -> None:
        converted = self.convert(amount, currency, on)
        if converted is None:
            return
        self.put(key, converted)

    def put(self, key: Hashable, converted: Decimal) -> None:
        """Add an amount already in the target currency."""
        self.values[key] = sum_amounts((self.values.get(key, ZERO), converted))

    def get(self, key: Hashable) -> Decimal:
        return self.values.get(key, ZERO)

    def unconverted_out(self) -> list[Money]:
        return [
            Money.of(amount, currency)
            for currency, amount in sorted(self.unconverted.items())
            if amount != 0
        ]


def accumulate(
    sums: Iterable[Sum],
    acc: Accumulator,
    *,
    key: Callable[[Sum], Hashable],
    value: Callable[[Sum], Decimal],
) -> Accumulator:
    """Add ``value(row)`` of every row under ``key(row)``."""
    for row in sums:
        acc.add(key(row), value(row), row.currency, row.fx_day)
    return acc


# ---------------------------------------------------------------------------
# Category hierarchy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CategoryNode:
    id: uuid.UUID
    parent_id: uuid.UUID | None
    name: str
    kind: CategoryKind


class CategoryTree:
    """The household's categories (at most three levels) for roll-ups.

    :meth:`at_depth` maps a category to its ancestor at ``depth`` (1 = top
    level); a category shallower than ``depth`` maps to itself.
    """

    def __init__(self, nodes: Iterable[CategoryNode]) -> None:
        self.nodes: dict[uuid.UUID, CategoryNode] = {n.id: n for n in nodes}

    @classmethod
    def load(cls, db: Session, household_id: uuid.UUID) -> CategoryTree:
        rows = db.execute(
            select(Category.id, Category.parent_id, Category.name, Category.kind).where(
                Category.household_id == household_id
            )
        )
        return cls(CategoryNode(r.id, r.parent_id, r.name, r.kind) for r in rows)

    def path(self, category_id: uuid.UUID) -> list[uuid.UUID]:
        """Ids from the top-level ancestor down to ``category_id``."""
        chain: list[uuid.UUID] = []
        current: uuid.UUID | None = category_id
        while current is not None and current in self.nodes and current not in chain:
            chain.append(current)
            current = self.nodes[current].parent_id
        chain.reverse()
        return chain

    def at_depth(self, category_id: uuid.UUID | None, depth: int) -> uuid.UUID | None:
        if category_id is None:
            return None
        chain = self.path(category_id)
        if not chain:
            return category_id
        return chain[min(depth, len(chain)) - 1]

    def name(self, category_id: uuid.UUID | None) -> str | None:
        node = self.nodes.get(category_id) if category_id is not None else None
        return node.name if node is not None else None

    def nearest(
        self, category_id: uuid.UUID | None, wanted: Mapping[uuid.UUID, Any]
    ) -> uuid.UUID | None:
        """The closest of ``category_id`` and its ancestors that is in ``wanted``."""
        if category_id is None:
            return None
        for candidate in reversed(self.path(category_id)):
            if candidate in wanted:
                return candidate
        return None
