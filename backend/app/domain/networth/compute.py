# SPDX-License-Identifier: AGPL-3.0-or-later
"""Net worth of a set of accounts on any day (DATA_MODEL.md "Net worth").

:meth:`NetWorthBook.load` reads everything once (up to ``until``) and
:meth:`NetWorthBook.on` evaluates a day in memory, so the daily job, the
backfill and the history of a member (recomputed over the accounts they may
see) share one implementation.

Value of an account at the end of day ``d``, in its own currency:

* **Skipped**: ``include_in_net_worth`` false, opened after ``d``
  (``opened_on``) or closed before ``d`` (``closed_on``).
* **Manual balance** accounts (manual assets and liabilities): the newer of
  the balance anchor (``app.domain.accounts.balances``) and the latest
  ``asset_valuation`` on or before ``d`` (``manual`` beats ``provider`` on a
  date; the anchor wins a tie with a valuation; a valuation is a magnitude,
  signed by ``account.is_asset``); ``0`` without either.
* **Investment** accounts (brokerage, retirement) with investment activity or
  statement lots: cash plus holdings. Holdings ``H(d)`` are the units the lot
  engine holds at the end of ``d`` times the close
  (``app.domain.investments.valuation.PriceBook``: stored price or latest
  trade price, no look-ahead) converted into the account currency on ``d``.
  Cash moves with ledger transactions and ``investment_txn.amount``. With a
  balance anchor ``A`` (a statement's or a person's figure for the whole
  account) on or before ``d``: ``anchor + H(d) - H(A.date) + cash moves in
  (A.date, d]``; without one: ``H(d) + every cash move up to d``.
* **Loan** accounts (loan, mortgage, line of credit) are kept as computed
  balance snapshots by ``app.domain.loans.service.sync_balance``; a loan
  account with no anchor yet on ``d`` falls back to ``principal_original``
  minus the principal of its ``loan_payment`` rows up to ``d`` (from the
  origination date on).
* **Everything else**: the ledger balance (anchor plus later transactions).

Each value is converted into the target (base) currency on ``d`` with the
latest rate on or before ``d`` (``app.domain.fx``). A positive value is an
asset, a negative one a liability (a credit card in credit is an asset, an
overdrawn checking account a liability), so ``assets`` and ``liabilities``
are non-negative and ``net = assets - liabilities``. An account whose
currency cannot be converted is left out of the totals and listed in
``unconverted``; a security without any price is valued at nothing and
listed in ``unpriced_security_ids``.

Classes (:data:`ACCOUNT_CLASS`): ``cash`` (checking, savings, cash),
``investments`` (brokerage, retirement), ``real_estate``/``vehicles``/
``other_assets`` (manual assets by ``asset_kind``), ``credit_cards``,
``mortgages``, ``loans`` (loan, line of credit) and ``other_liabilities``
(manual liabilities). The class names the account's nature; the side comes
from the sign of its value.
"""

from __future__ import annotations

import bisect
import uuid
from collections import defaultdict
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from typing import Any, Final, Literal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.money import calc_context, to_storage
from app.domain.accounts.balances import SOURCE_PRIORITY, Anchor
from app.domain.accounts.models import Account, AccountType, AssetKind, BalanceSnapshot
from app.domain.assets.models import AssetValuation, ValuationSource
from app.domain.fx.service import FxConverter
from app.domain.investments import lots as engine
from app.domain.investments import service as investments
from app.domain.investments.models import InvestmentTxn, Lot, Security
from app.domain.investments.valuation import PriceBook
from app.domain.loans.models import Loan, LoanPayment
from app.domain.transactions.models import Transaction

ZERO: Final = Decimal(0)
#: Version of the ``breakdown`` JSON written to ``net_worth_snapshot``.
BREAKDOWN_VERSION: Final = 1

Side = Literal["asset", "liability"]

INVESTMENT_TYPES: Final = frozenset({AccountType.BROKERAGE, AccountType.RETIREMENT})
LOAN_TYPES: Final = frozenset({AccountType.LOAN, AccountType.MORTGAGE, AccountType.LINE_OF_CREDIT})

ACCOUNT_CLASS: Final[Mapping[AccountType, str]] = {
    AccountType.CHECKING: "cash",
    AccountType.SAVINGS: "cash",
    AccountType.CASH: "cash",
    AccountType.BROKERAGE: "investments",
    AccountType.RETIREMENT: "investments",
    AccountType.CREDIT_CARD: "credit_cards",
    AccountType.MORTGAGE: "mortgages",
    AccountType.LOAN: "loans",
    AccountType.LINE_OF_CREDIT: "loans",
    AccountType.MANUAL_LIABILITY: "other_liabilities",
}
_MANUAL_ASSET_CLASS: Final[Mapping[AssetKind, str]] = {
    AssetKind.PROPERTY: "real_estate",
    AssetKind.VEHICLE: "vehicles",
    AssetKind.OTHER: "other_assets",
}


def account_class(account: Account) -> str:
    if account.type is AccountType.MANUAL_ASSET:
        return _MANUAL_ASSET_CLASS.get(account.asset_kind or AssetKind.OTHER, "other_assets")
    return ACCOUNT_CLASS.get(account.type, "other_assets")


def text(value: Decimal) -> str:
    """A stored-precision decimal string (``"1234.500000"``)."""
    return f"{to_storage(value):f}"


# ---------------------------------------------------------------------------
# Step series (pure)
# ---------------------------------------------------------------------------


@dataclass
class _Cumulative:
    """Running sum of dated amounts, answered with a binary search."""

    dates: list[date] = field(default_factory=list)
    totals: list[Decimal] = field(default_factory=list)

    @classmethod
    def of(cls, points: Mapping[date, Decimal]) -> _Cumulative:
        out = cls()
        running = ZERO
        for day in sorted(points):
            running += points[day]
            out.dates.append(day)
            out.totals.append(running)
        return out

    def upto(self, day: date) -> Decimal:
        """Sum of the amounts dated on or before ``day``."""
        at = bisect.bisect_right(self.dates, day)
        return self.totals[at - 1] if at else ZERO

    def between(self, after: date, until: date) -> Decimal:
        """Sum of the amounts dated in ``(after, until]``."""
        return self.upto(until) - self.upto(after)


@dataclass
class _Anchors:
    """The best anchor per date (``SOURCE_PRIORITY``), sorted."""

    dates: list[date] = field(default_factory=list)
    anchors: list[Anchor] = field(default_factory=list)

    @classmethod
    def of(cls, rows: Collection[Anchor]) -> _Anchors:
        best: dict[date, Anchor] = {}
        for anchor in rows:
            current = best.get(anchor.as_of_date)
            if current is None or SOURCE_PRIORITY[anchor.source] < SOURCE_PRIORITY[current.source]:
                best[anchor.as_of_date] = anchor
        out = cls()
        for day in sorted(best):
            out.dates.append(day)
            out.anchors.append(best[day])
        return out

    def at(self, day: date) -> Anchor | None:
        index = bisect.bisect_right(self.dates, day)
        return self.anchors[index - 1] if index else None


@dataclass
class _Valuations:
    """Valuations by date (``manual`` beats ``provider``), sorted."""

    dates: list[date] = field(default_factory=list)
    values: list[Decimal] = field(default_factory=list)

    @classmethod
    def of(cls, rows: Collection[tuple[date, Decimal, ValuationSource]]) -> _Valuations:
        best: dict[date, tuple[Decimal, ValuationSource]] = {}
        for day, value, source in rows:
            current = best.get(day)
            if current is None or (
                source is ValuationSource.MANUAL and current[1] is not ValuationSource.MANUAL
            ):
                best[day] = (value, source)
        out = cls()
        for day in sorted(best):
            out.dates.append(day)
            out.values.append(best[day][0])
        return out

    def at(self, day: date) -> tuple[date, Decimal] | None:
        index = bisect.bisect_right(self.dates, day)
        return (self.dates[index - 1], self.values[index - 1]) if index else None


@dataclass
class _Position:
    security_id: uuid.UUID
    currency: str
    dates: list[date]
    quantities: list[Decimal]

    def at(self, day: date) -> Decimal:
        index = bisect.bisect_right(self.dates, day)
        return self.quantities[index - 1] if index else ZERO


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AccountValue:
    account_id: uuid.UUID
    type: AccountType
    klass: str
    side: Side
    amount: Decimal  # signed, account currency
    currency: str
    amount_base: Decimal | None  # signed, target currency; None when unconverted


@dataclass(frozen=True)
class NetWorthPoint:
    on: date
    currency: str
    assets: Decimal
    liabilities: Decimal
    accounts: tuple[AccountValue, ...]
    unpriced_security_ids: tuple[uuid.UUID, ...]

    @property
    def net(self) -> Decimal:
        return self.assets - self.liabilities

    @property
    def unconverted(self) -> list[AccountValue]:
        return [a for a in self.accounts if a.amount_base is None]

    def by_type(self) -> dict[str, Decimal]:
        """Signed base amount per account type."""
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for value in self.accounts:
            if value.amount_base is not None:
                out[value.type.value] += value.amount_base
        return dict(sorted(out.items()))

    def by_class(self) -> dict[Side, dict[str, Decimal]]:
        """Magnitudes per class on each side."""
        out: dict[Side, dict[str, Decimal]] = {"asset": {}, "liability": {}}
        for value in self.accounts:
            if value.amount_base is None:
                continue
            bucket = out[value.side]
            bucket[value.klass] = bucket.get(value.klass, ZERO) + abs(value.amount_base)
        return {side: dict(sorted(parts.items())) for side, parts in out.items()}

    def breakdown(self) -> dict[str, Any]:
        """The ``net_worth_snapshot.breakdown`` JSON (ids, types and amounts only)."""
        classes = self.by_class()
        return {
            "version": BREAKDOWN_VERSION,
            "by_class": {
                "assets": {k: text(v) for k, v in classes["asset"].items()},
                "liabilities": {k: text(v) for k, v in classes["liability"].items()},
            },
            "by_type": {k: text(v) for k, v in self.by_type().items()},
            "accounts": [
                {
                    "account_id": str(a.account_id),
                    "type": a.type.value,
                    "class": a.klass,
                    "side": a.side,
                    "amount": text(a.amount),
                    "currency": a.currency,
                    "amount_base": text(a.amount_base) if a.amount_base is not None else None,
                }
                for a in self.accounts
            ],
            "unconverted": [
                {"account_id": str(a.account_id), "amount": text(a.amount), "currency": a.currency}
                for a in self.unconverted
            ],
            "unpriced_security_ids": [str(s) for s in self.unpriced_security_ids],
        }


# ---------------------------------------------------------------------------
# The book
# ---------------------------------------------------------------------------


@dataclass
class _AccountData:
    account: Account
    anchors: _Anchors
    moves: _Cumulative
    valuations: _Valuations | None = None
    invest_moves: _Cumulative | None = None
    positions: list[_Position] = field(default_factory=list)
    loan_origination: date | None = None
    loan_principal: Decimal = ZERO
    loan_paid: _Cumulative | None = None


class NetWorthBook:
    """Everything needed to value ``accounts`` on any day up to ``until``."""

    def __init__(
        self,
        db: Session,
        household_id: uuid.UUID,
        target: str,
        until: date,
        data: list[_AccountData],
        prices: PriceBook,
    ) -> None:
        self.household_id = household_id
        self.target = target
        self.until = until
        self._data = data
        self._prices = prices
        self._to_target = FxConverter(db, household_id, target, until=until)
        self._to_account: dict[str, FxConverter] = {}
        self._db = db

    # -- loading -------------------------------------------------------------

    @classmethod
    def empty(cls, db: Session, household_id: uuid.UUID, target: str, until: date) -> NetWorthBook:
        return cls(
            db, household_id, target, until, [], PriceBook(db, household_id, (), until=until)
        )

    @classmethod
    def load(
        cls,
        db: Session,
        household_id: uuid.UUID,
        account_ids: Collection[uuid.UUID] | None,
        *,
        target: str,
        until: date,
    ) -> NetWorthBook:
        """Read the accounts (``None``: every account of the household) once."""
        stmt = select(Account).where(
            Account.household_id == household_id, Account.include_in_net_worth.is_(True)
        )
        if account_ids is not None:
            if not account_ids:
                return cls(
                    db,
                    household_id,
                    target,
                    until,
                    [],
                    PriceBook(db, household_id, (), until=until),
                )
            stmt = stmt.where(Account.id.in_(list(account_ids)))
        accounts = list(db.execute(stmt.order_by(Account.id)).scalars())
        if not accounts:
            return cls(
                db, household_id, target, until, [], PriceBook(db, household_id, (), until=until)
            )
        ids = [a.id for a in accounts]

        anchors: dict[uuid.UUID, list[Anchor]] = defaultdict(list)
        for account_id, day, balance, source in db.execute(
            select(
                BalanceSnapshot.account_id,
                BalanceSnapshot.as_of_date,
                BalanceSnapshot.balance,
                BalanceSnapshot.source,
            ).where(
                BalanceSnapshot.household_id == household_id,
                BalanceSnapshot.account_id.in_(ids),
                BalanceSnapshot.as_of_date <= until,
            )
        ):
            anchors[account_id].append(Anchor(day, balance, source))

        moves: dict[uuid.UUID, dict[date, Decimal]] = defaultdict(dict)
        for account_id, day, total in db.execute(
            select(Transaction.account_id, Transaction.posted_date, func.sum(Transaction.amount))
            .where(
                Transaction.household_id == household_id,
                Transaction.account_id.in_(ids),
                Transaction.posted_date <= until,
            )
            .group_by(Transaction.account_id, Transaction.posted_date)
        ):
            moves[account_id][day] = total

        manual_ids = [a.id for a in accounts if a.manual_balance]
        valuations: dict[uuid.UUID, list[tuple[date, Decimal, ValuationSource]]] = defaultdict(list)
        if manual_ids:
            for account_id, day, value, valuation_source in db.execute(
                select(
                    AssetValuation.account_id,
                    AssetValuation.valued_on,
                    AssetValuation.value,
                    AssetValuation.source,
                ).where(
                    AssetValuation.household_id == household_id,
                    AssetValuation.account_id.in_(manual_ids),
                    AssetValuation.valued_on <= until,
                )
            ):
                valuations[account_id].append((day, value, valuation_source))

        loan_ids = [a.id for a in accounts if a.type in LOAN_TYPES]
        loans: dict[uuid.UUID, Loan] = {}
        paid: dict[uuid.UUID, dict[date, Decimal]] = defaultdict(dict)
        if loan_ids:
            for loan_row in db.execute(
                select(Loan).where(Loan.household_id == household_id, Loan.account_id.in_(loan_ids))
            ).scalars():
                loans[loan_row.account_id] = loan_row
            for account_id, day, principal in db.execute(
                select(Loan.account_id, LoanPayment.paid_on, func.sum(LoanPayment.principal))
                .join(
                    Loan,
                    (Loan.household_id == LoanPayment.household_id)
                    & (Loan.id == LoanPayment.loan_id),
                )
                .where(
                    LoanPayment.household_id == household_id,
                    Loan.account_id.in_(loan_ids),
                    LoanPayment.paid_on <= until,
                )
                .group_by(Loan.account_id, LoanPayment.paid_on)
            ):
                paid[account_id][day] = principal

        invest = [a for a in accounts if a.type in INVESTMENT_TYPES]
        invest_ids = [a.id for a in invest]
        txns: list[InvestmentTxn] = []
        seeds: list[Lot] = []
        if invest_ids:
            txns = list(
                db.execute(
                    select(InvestmentTxn)
                    .where(
                        InvestmentTxn.household_id == household_id,
                        InvestmentTxn.account_id.in_(invest_ids),
                        InvestmentTxn.trade_date <= until,
                    )
                    .order_by(InvestmentTxn.trade_date, InvestmentTxn.id)
                ).scalars()
            )
            seeds = list(
                db.execute(
                    select(Lot).where(
                        Lot.household_id == household_id,
                        Lot.account_id.in_(invest_ids),
                        Lot.opening_txn_id.is_(None),
                        Lot.open_date <= until,
                    )
                ).scalars()
            )
        security_ids = {t.security_id for t in txns if t.security_id is not None} | {
            s.security_id for s in seeds
        }
        securities = {
            s.id: s
            for s in (
                db.execute(
                    select(Security).where(
                        Security.household_id == household_id,
                        Security.id.in_(list(security_ids)),
                    )
                ).scalars()
                if security_ids
                else []
            )
        }
        positions = _replay_positions(db, household_id, invest, txns, seeds, securities)
        invest_moves: dict[uuid.UUID, dict[date, Decimal]] = defaultdict(dict)
        for txn in txns:
            by_day = invest_moves[txn.account_id]
            by_day[txn.trade_date] = by_day.get(txn.trade_date, ZERO) + txn.amount

        data: list[_AccountData] = []
        for account in accounts:
            entry = _AccountData(
                account=account,
                anchors=_Anchors.of(anchors.get(account.id, [])),
                moves=_Cumulative.of(moves.get(account.id, {})),
            )
            if account.manual_balance:
                entry.valuations = _Valuations.of(valuations.get(account.id, []))
            if account.type in INVESTMENT_TYPES and (
                positions.get(account.id) or account.id in invest_moves
            ):
                entry.positions = positions.get(account.id, [])
                entry.invest_moves = _Cumulative.of(invest_moves.get(account.id, {}))
            loan = loans.get(account.id)
            if loan is not None:
                entry.loan_origination = loan.origination_date
                entry.loan_principal = loan.principal_original
                entry.loan_paid = _Cumulative.of(paid.get(account.id, {}))
            data.append(entry)
        prices = PriceBook(db, household_id, security_ids, until=until, trades=txns)
        return cls(db, household_id, target, until, data, prices)

    # -- evaluation ----------------------------------------------------------

    @property
    def account_ids(self) -> list[uuid.UUID]:
        return [d.account.id for d in self._data]

    def first_day(self) -> date | None:
        """The earliest day any loaded account has data (anchor, move, valuation, loan)."""
        days: list[date] = []
        for entry in self._data:
            days.extend(entry.anchors.dates[:1])
            days.extend(entry.moves.dates[:1])
            if entry.valuations is not None:
                days.extend(entry.valuations.dates[:1])
            if entry.invest_moves is not None:
                days.extend(entry.invest_moves.dates[:1])
            days.extend(p.dates[0] for p in entry.positions if p.dates)
            if entry.loan_origination is not None:
                days.append(entry.loan_origination)
        return min(days) if days else None

    def _converter(self, currency: str) -> FxConverter:
        found = self._to_account.get(currency)
        if found is None:
            found = FxConverter(self._db, self.household_id, currency, until=self.until)
            self._to_account[currency] = found
        return found

    def _holdings(self, entry: _AccountData, day: date, unpriced: set[uuid.UUID]) -> Decimal:
        """Market value of the account's positions at the end of ``day`` (account currency)."""
        total = ZERO
        fx = self._converter(entry.account.currency)
        with localcontext(calc_context()):
            for position in entry.positions:
                quantity = position.at(day)
                if quantity == 0:
                    continue
                close = self._prices.close(position.security_id, day)
                if close is None:
                    unpriced.add(position.security_id)
                    continue
                value = fx.try_convert(quantity * close.close, position.currency, day)
                if value is None:
                    unpriced.add(position.security_id)
                    continue
                total += value
        return total

    def _value(self, entry: _AccountData, day: date, unpriced: set[uuid.UUID]) -> Decimal:
        account = entry.account
        anchor = entry.anchors.at(day)
        if entry.valuations is not None:
            valuation = entry.valuations.at(day)
            sign = 1 if account.is_asset else -1
            if valuation is not None and (anchor is None or valuation[0] > anchor.as_of_date):
                return sign * valuation[1]
            if anchor is not None:
                return anchor.balance
        elif entry.invest_moves is not None:
            holdings = self._holdings(entry, day, unpriced)
            if anchor is None:
                return holdings + entry.moves.upto(day) + entry.invest_moves.upto(day)
            then = self._holdings(entry, anchor.as_of_date, unpriced)
            moved = entry.moves.between(anchor.as_of_date, day) + entry.invest_moves.between(
                anchor.as_of_date, day
            )
            return anchor.balance + holdings - then + moved
        elif anchor is not None:
            return anchor.balance + entry.moves.between(anchor.as_of_date, day)
        if (
            entry.loan_origination is not None
            and entry.loan_paid is not None
            and entry.loan_origination <= day
        ):
            owed = max(ZERO, entry.loan_principal - entry.loan_paid.upto(day))
            return owed if account.is_asset else -owed
        if entry.valuations is not None:
            return ZERO
        return entry.moves.upto(day)

    def on(self, day: date) -> NetWorthPoint:
        """The net worth at the end of ``day`` (module doc)."""
        if day > self.until:
            raise ValueError("day after the book's 'until'")
        assets = liabilities = ZERO
        values: list[AccountValue] = []
        unpriced: set[uuid.UUID] = set()
        for entry in self._data:
            account = entry.account
            if account.opened_on is not None and account.opened_on > day:
                continue
            if account.closed_on is not None and account.closed_on < day:
                continue
            amount = to_storage(self._value(entry, day, unpriced))
            if amount == 0:
                continue
            side: Side = "asset" if amount > 0 else "liability"
            base = self._to_target.try_convert(amount, account.currency, day)
            if base is not None:
                if base > 0:
                    assets += base
                else:
                    liabilities -= base
            values.append(
                AccountValue(
                    account_id=account.id,
                    type=account.type,
                    klass=account_class(account),
                    side=side,
                    amount=amount,
                    currency=account.currency,
                    amount_base=base,
                )
            )
        return NetWorthPoint(
            on=day,
            currency=self.target,
            assets=to_storage(assets),
            liabilities=to_storage(liabilities),
            accounts=tuple(values),
            unpriced_security_ids=tuple(sorted(unpriced, key=lambda u: u.bytes)),
        )


def _replay_positions(
    db: Session,
    household_id: uuid.UUID,
    accounts: list[Account],
    txns: list[InvestmentTxn],
    seeds: list[Lot],
    securities: Mapping[uuid.UUID, Security],
) -> dict[uuid.UUID, list[_Position]]:
    """Units held over time per (account, security), replayed in memory (read only)."""
    if not accounts:
        return {}
    by_account = {a.id: a for a in accounts}
    by_pair: dict[tuple[uuid.UUID, uuid.UUID], list[InvestmentTxn]] = defaultdict(list)
    for txn in txns:
        if txn.security_id is not None:
            by_pair[(txn.account_id, txn.security_id)].append(txn)
    seeds_by_pair: dict[tuple[uuid.UUID, uuid.UUID], list[Lot]] = defaultdict(list)
    for lot in seeds:
        seeds_by_pair[(lot.account_id, lot.security_id)].append(lot)
        by_pair.setdefault((lot.account_id, lot.security_id), [])
    methods = investments.cost_methods(db, household_id, sorted(by_account, key=lambda u: u.bytes))
    lot_ids = investments.stored_lot_ids(db, household_id, list(by_account))
    out: dict[uuid.UUID, list[_Position]] = defaultdict(list)
    fx: dict[str, FxConverter] = {}
    for (account_id, security_id), rows in sorted(
        by_pair.items(), key=lambda kv: (kv[0][0].bytes, kv[0][1].bytes)
    ):
        account = by_account[account_id]
        security = securities.get(security_id)
        if security is None:  # pragma: no cover - the foreign key keeps it
            continue
        converter = fx.setdefault(account.currency, FxConverter(db, household_id, account.currency))

        def convert(
            amount: Decimal, currency: str, on: date, converter: FxConverter = converter
        ) -> Decimal:
            # Costs do not change quantities: an unknown rate keeps the amount.
            converted = converter.try_convert(amount, currency, on)
            return converted if converted is not None else amount

        pair_seeds = investments.seed_txns(
            seeds_by_pair.get((account_id, security_id), []), rows, strict=False
        )
        try:
            result = engine.replay(
                [
                    investments.engine_txn(r, investments.open_cost(r, security.currency, convert))
                    for r in rows
                ]
                + (pair_seeds or []),
                method=methods[account_id],
                lot_ids=lot_ids,
            )
        except engine.LotError:
            # Writes are replayed first, so stored history always replays; a
            # broken pair is left out rather than failing every figure.
            continue
        out[account_id].append(
            _Position(
                security_id,
                security.currency,
                [d for d, _ in result.positions],
                [q for _, q in result.positions],
            )
        )
    return out
