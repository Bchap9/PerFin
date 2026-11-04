# SPDX-License-Identifier: AGPL-3.0-or-later
"""Committing brokerage statements: securities, activity, openings, prices, snapshots.

Called by the structured importer (``app.domain.imports.service.import_extraction``)
for the ``investments`` of an extraction: OFX ``INVSTMTRS`` from the sandbox
and PDF/scan statements read by ``app.ingestion.parsers.brokerage``. It runs
in the importer's transaction and RLS context (INGESTION.md "Brokerage
statements"). Per statement:

1. **Account**: the document's account (one statement), the account the
   person chose on a re-run, else the household account with the last four
   digits (institution breaks ties) among those the uploader may edit
   (``importable_account_ids``, SECURITY.md F3). It must be a brokerage or
   retirement account in the statement's currency; otherwise an
   ``unknown_account`` item and nothing is written.
2. **Hold** (SECURITY.md C12): a statement whose figures do not reconcile
   (:func:`app.ingestion.parsers.brokerage.reconcile_investment`: positions
   x prices vs market values, holdings total, cash, account value), or with
   any row or position read below the confidence threshold, writes
   **nothing**: investment rows have no pending state, so they are held
   back with a ``low_confidence_field`` item
   (``investment_reconciliation_failed`` / ``investment_low_confidence``)
   naming the discrepancy. A statement with any date before
   ``app.core.dates.MIN_BUSINESS_DATE`` is held the same way
   (``investment_date_out_of_range``, SECURITY.md F8). A structured (OFX) statement with nothing to
   check is imported like an OFX bank statement; a document one cannot be
   (its rows score below the threshold).
3. **Securities**: found in the household's catalogue by CUSIP, ISIN, then
   symbol, else created (quote currency = the account's); identifiers a
   later statement adds are filled in.
4. **Activity** -> ``investment_txn`` with an ``import_hash`` (account,
   trade date, type, security, quantity, amount, running index among
   identical rows), so the same statement as OFX and as PDF, or overlapping
   statements, add each row once. Cash ``deposit``/``withdrawal`` rows are
   ledger transactions on the account (through the structured importer:
   transaction ``import_hash`` dedupe, rules, provenance).
5. **Out of order**: new rows of a statement older than activity already
   recorded for the account are not written (an opening position may
   already count them); an ``investment_out_of_order`` item says so.
   Import statements oldest first.
6. **Opening positions and lots**: the reported positions are the truth on
   ``as_of``. Units the account's history does not explain (held before
   the first imported statement) enter as one ``transfer_in`` per security
   on the period's first day (memo ``Opening position from statement``;
   cost per unit from the printed cost basis, else the price); history
   holding more than reported raises a ``position_mismatch`` item. Lots,
   ``lot_links`` and the ``holding`` cache are then written by the
   investments lot engine (``service.replay_position``, FIFO or average
   cost per account). A history the engine refuses (selling units never
   held) rolls the statement back (savepoint) with an
   ``investment_lot_conflict`` item.
7. **Prices**: each position's unit price on ``as_of`` becomes a ``price``
   row (``source = statement``) unless one exists for that day from another
   source.
8. **Balance snapshot**: holdings + closing cash on ``as_of``
   (``source = statement``) when every check that could run held and both
   are known.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, localcontext
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.dates import MIN_BUSINESS_DATE, in_business_range
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.ids import uuid7
from app.core.money import calc_context, to_storage
from app.domain.accounts import repo as accounts_repo
from app.domain.accounts.models import Account, AccountType, BalanceSource
from app.domain.documents.models import Document, ReviewKind
from app.domain.investments.models import (
    AssetClass,
    InvestmentTxn,
    InvestmentTxnType,
    Lot,
    Price,
    Security,
)
from app.domain.investments.service import replay_position
from app.ingestion.match.accounts import match_account
from app.ingestion.parsers.brokerage import position_value, reconcile_investment
from app.ingestion.sandbox.protocol import (
    DOCUMENT_STATEMENT_FORMATS,
    ExtractedTransaction,
    InvestmentActivity,
    InvestmentPosition,
    InvestmentReconciliation,
    InvestmentStatement,
    SecurityRef,
    Statement,
)

HASH_VERSION: Final = "perfin-invest-v1"
STATEMENT_PRICE_SOURCE: Final = "statement"
INVESTMENT_ACCOUNT_TYPES: Final = frozenset({AccountType.BROKERAGE, AccountType.RETIREMENT})
#: Cash rows that are ledger transactions, not ``investment_txn`` rows.
CASH_TYPES: Final = frozenset({"deposit", "withdrawal"})
#: Units below this are rounding, not a position difference.
UNIT_TOLERANCE: Final = Decimal("0.000001")
QUANTUM: Final = Decimal("1e-10")
IDS_MAX: Final = 50
OPENING_MEMO: Final = "Opening position from statement"
OPENING_TYPES: Final = frozenset(
    {InvestmentTxnType.BUY, InvestmentTxnType.REINVEST, InvestmentTxnType.TRANSFER_IN}
)
CLOSING_TYPES: Final = frozenset({InvestmentTxnType.SELL, InvestmentTxnType.TRANSFER_OUT})
_SEP: Final = "\x1f"

Review = Callable[[ReviewKind, dict[str, Any]], None]
CommitCash = Callable[[Statement, Account], None]


@dataclass
class InvestmentImport:
    """What one run did (counts; the importer merges them into its summary)."""

    statements: int = 0
    held: int = 0
    activity: int = 0
    duplicates: int = 0
    cash_rows: int = 0
    positions: int = 0
    securities_created: int = 0
    prices: int = 0
    opening_positions: int = 0
    snapshots: int = 0
    accounts: list[uuid.UUID] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "statements": self.statements,
            "held": self.held,
            "activity": self.activity,
            "duplicates": self.duplicates,
            "cash_rows": self.cash_rows,
            "positions": self.positions,
            "securities_created": self.securities_created,
            "prices": self.prices,
            "opening_positions": self.opening_positions,
            "snapshots": self.snapshots,
        }


def _text(value: Decimal | None) -> str | None:
    return f"{value:f}" if value is not None else None


def _units(value: Decimal) -> str:
    return f"{value.quantize(QUANTUM):f}"


def activity_hash(
    account_id: uuid.UUID,
    activity: InvestmentActivity,
    security_id: uuid.UUID | None,
    index: int,
) -> str:
    """``investment_txn.import_hash`` (module doc, step 4); the same in every format."""
    parts = (
        HASH_VERSION,
        str(account_id),
        activity.trade_date.isoformat(),
        activity.type,
        str(security_id) if security_id else "",
        _units(activity.quantity) if activity.quantity is not None else "",
        f"{to_storage(activity.amount):f}",
        str(index),
    )
    return hashlib.sha256(_SEP.join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# Accounts
# ---------------------------------------------------------------------------


@dataclass
class _Context:
    db: Session
    document: Document
    allowed: Collection[uuid.UUID] | None
    review: Review
    commit_cash: CommitCash
    user_id: uuid.UUID | None
    result: InvestmentImport
    securities: dict[tuple[str, str], Security] = field(default_factory=dict)

    @property
    def household_id(self) -> uuid.UUID:
        return self.document.household_id


def _usable(ctx: _Context, account_id: uuid.UUID | None) -> Account | None:
    if account_id is None:
        return None
    if ctx.allowed is not None and account_id not in ctx.allowed:
        return None
    return accounts_repo.get_account(ctx.db, ctx.household_id, account_id, lock=True)


def _account(
    ctx: _Context,
    statement: InvestmentStatement,
    index: int,
    *,
    single: bool,
    account_id: uuid.UUID | None,
) -> Account | None:
    account: Account | None = None
    candidates: tuple[uuid.UUID, ...] = ()
    if single and ctx.document.account_id is not None and account_id is None:
        account = _usable(ctx, ctx.document.account_id)
    if account is None:
        match = match_account(
            ctx.db,
            ctx.household_id,
            last4=statement.account.last4,
            institution=statement.account.institution,
            allowed=ctx.allowed,
        )
        candidates = match.candidates
        if account_id is not None and (match.account_id is None or single):
            account = _usable(ctx, account_id)
        else:
            account = _usable(ctx, match.account_id)
    base = {
        "statement": index,
        "investment": True,
        "positions": len(statement.positions),
        "activity": len(statement.activity),
    }
    if account is None:
        ctx.review(
            ReviewKind.UNKNOWN_ACCOUNT,
            {
                **base,
                "reason": "account_ambiguous" if candidates else "account_not_found",
                "account_type": statement.account.account_type,
                "currency": statement.account.currency,
                "candidates": [str(c) for c in candidates[:IDS_MAX]],
            },
        )
        return None
    if account.type not in INVESTMENT_ACCOUNT_TYPES:
        ctx.review(
            ReviewKind.UNKNOWN_ACCOUNT,
            {
                **base,
                "reason": "account_type_mismatch",
                "account_id": str(account.id),
                "account_type": str(account.type),
            },
        )
        return None
    currency = statement.account.currency
    if currency is not None and currency != account.currency:
        ctx.review(
            ReviewKind.UNKNOWN_ACCOUNT,
            {
                **base,
                "reason": "currency_mismatch",
                "account_id": str(account.id),
                "currency": currency,
                "account_currency": account.currency,
            },
        )
        return None
    return account


# ---------------------------------------------------------------------------
# Holding a statement back (C12)
# ---------------------------------------------------------------------------


def _parser(statement: InvestmentStatement) -> str | None:
    parser = statement.parser
    return f"{parser.name}/{parser.version}" if parser is not None else None


def _statement_dates(statement: InvestmentStatement) -> Iterable[date | None]:
    yield statement.period_start
    yield statement.period_end
    yield statement.as_of
    for position in statement.positions:
        yield position.price_as_of
    for activity in statement.activity:
        yield activity.trade_date
        yield activity.settle_date


def _held(
    ctx: _Context,
    statement: InvestmentStatement,
    check: InvestmentReconciliation,
    account: Account,
) -> bool:
    if any(d is not None and not in_business_range(d) for d in _statement_dates(statement)):
        # SECURITY.md F8: a date before MIN_BUSINESS_DATE is a misread (or a
        # crafted file); it would make every performance read walk centuries.
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "investment_date_out_of_range",
                "field": "date",
                "account_id": str(account.id),
                "parser": _parser(statement),
                "earliest_allowed": MIN_BUSINESS_DATE.isoformat(),
            },
        )
        return True
    if check.status == "mismatch":
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "investment_reconciliation_failed",
                "field": "reconciliation",
                "account_id": str(account.id),
                "parser": _parser(statement),
                "failed": list(check.failed),
                "checks": list(check.checks),
                "positions": [
                    {
                        "index": i,
                        "symbol": statement.positions[i].security.symbol,
                        "quantity": _text(statement.positions[i].quantity),
                        "unit_price": _text(statement.positions[i].unit_price),
                        "market_value": _text(statement.positions[i].market_value),
                        "page": statement.positions[i].page,
                    }
                    for i in check.position_mismatches[:IDS_MAX]
                ],
                "positions_total": _text(check.positions_total),
                "reported_positions_total": _text(check.reported_positions_total),
                "cash_opening": _text(check.cash_opening),
                "cash_closing": _text(check.cash_closing),
                "cash_activity": _text(check.cash_activity),
                "cash_difference": _text(check.cash_difference),
                "total_value": _text(check.total_value),
                "reported_total_value": _text(check.reported_total_value),
            },
        )
        return True
    low_positions = [(i, p) for i, p in enumerate(statement.positions) if p.low_confidence]
    low_activity = [(i, a) for i, a in enumerate(statement.activity) if a.low_confidence]
    unchecked_document = (
        check.status == "unavailable" and statement.source_format in DOCUMENT_STATEMENT_FORMATS
    )
    if not (low_positions or low_activity or unchecked_document):
        return False
    fields = sorted(
        {f for _, p in low_positions for f in p.low_confidence}
        | {f for _, a in low_activity for f in a.low_confidence}
        | ({"reconciliation"} if unchecked_document else set())
    )
    ctx.review(
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {
            "reason": "investment_low_confidence",
            "fields": fields,
            "account_id": str(account.id),
            "parser": _parser(statement),
            "reconciliation": check.status,
            "positions": [
                {"index": i, "fields": list(p.low_confidence), "page": p.page}
                for i, p in low_positions[:IDS_MAX]
            ],
            "activity": [
                {"index": i, "fields": list(a.low_confidence), "page": a.page}
                for i, a in low_activity[:IDS_MAX]
            ],
        },
    )
    return True


# ---------------------------------------------------------------------------
# Securities and prices
# ---------------------------------------------------------------------------


def _find_security(ctx: _Context, ref: SecurityRef) -> Security | None:
    db, household_id = ctx.db, ctx.household_id
    for column, value in (
        (Security.cusip, ref.cusip),
        (Security.isin, ref.isin),
        (Security.symbol, ref.symbol),
    ):
        if value is None:
            continue
        query = select(Security).where(Security.household_id == household_id, column == value)
        if column is Security.symbol:
            query = query.where(Security.exchange.is_(None))
        found = db.execute(query.order_by(Security.created_at, Security.id).limit(1))
        security = found.scalar_one_or_none()
        if security is not None:
            return security
    return None


def _security(ctx: _Context, ref: SecurityRef, account: Account) -> Security:
    key = (ref.cusip or ref.isin or "", ref.symbol or "")
    cached = ctx.securities.get(key)
    if cached is not None:
        return cached
    security = _find_security(ctx, ref)
    if security is None:
        symbol = ref.symbol or ref.cusip or ref.isin or ""
        security = Security(
            id=uuid7(),
            household_id=ctx.household_id,
            symbol=symbol,
            cusip=ref.cusip,
            isin=ref.isin,
            name=(ref.name or symbol)[:200],
            asset_class=AssetClass(ref.asset_class or AssetClass.OTHER),
            currency=account.currency,
        )
        ctx.db.add(security)
        ctx.db.flush()
        ctx.result.securities_created += 1
    else:
        if security.cusip is None and ref.cusip is not None:
            security.cusip = ref.cusip
        if security.isin is None and ref.isin is not None:
            security.isin = ref.isin
    ctx.securities[key] = security
    return security


def _record_price(
    ctx: _Context, security: Security, position: InvestmentPosition, on: date | None
) -> None:
    day = position.price_as_of or on
    price = position.unit_price
    if day is None or price is None or price <= 0:
        return
    existing = ctx.db.execute(
        select(Price).where(
            Price.household_id == ctx.household_id,
            Price.security_id == security.id,
            Price.price_date == day,
        )
    ).scalar_one_or_none()
    if existing is None:
        ctx.db.add(
            Price(
                household_id=ctx.household_id,
                security_id=security.id,
                price_date=day,
                close=price,
                currency=security.currency,
                source=STATEMENT_PRICE_SOURCE,
            )
        )
        ctx.result.prices += 1
    elif existing.source == STATEMENT_PRICE_SOURCE and existing.close != price:
        existing.close = price
        ctx.result.prices += 1


# ---------------------------------------------------------------------------
# Activity, opening positions and the lot engine
# ---------------------------------------------------------------------------

#: Same-day order of the lot engine (``app.domain.investments.lots``).
_DAY_RANK: Final = {"split": 0, "buy": 1, "reinvest": 1, "transfer_in": 1}


def _existing_hashes(ctx: _Context, account: Account) -> set[str]:
    return {
        value
        for value in ctx.db.execute(
            select(InvestmentTxn.import_hash).where(
                InvestmentTxn.household_id == ctx.household_id,
                InvestmentTxn.account_id == account.id,
                InvestmentTxn.import_hash.is_not(None),
            )
        ).scalars()
        if value is not None
    }


def _later_activity(ctx: _Context, account: Account, on: date) -> bool:
    """Whether the account already has investment activity after ``on``."""
    found = ctx.db.execute(
        select(InvestmentTxn.id)
        .where(
            InvestmentTxn.household_id == ctx.household_id,
            InvestmentTxn.account_id == account.id,
            InvestmentTxn.trade_date > on,
        )
        .limit(1)
    ).scalar_one_or_none()
    return found is not None


def _held_units(ctx: _Context, account: Account) -> dict[uuid.UUID, Decimal]:
    rows = ctx.db.execute(
        select(Lot.security_id, func.sum(Lot.remaining_quantity))
        .where(
            Lot.household_id == ctx.household_id,
            Lot.account_id == account.id,
            Lot.remaining_quantity > 0,
        )
        .group_by(Lot.security_id)
    ).all()
    return {security_id: Decimal(quantity) for security_id, quantity in rows}


def _units_after(start: Decimal, rows: Sequence[InvestmentTxn]) -> Decimal:
    """Units of one security after ``rows`` (engine order), starting from ``start``."""
    units = start
    with localcontext(calc_context()):
        for row in sorted(
            rows, key=lambda r: (r.trade_date, _DAY_RANK.get(str(r.type), 2), r.id.bytes)
        ):
            if row.type is InvestmentTxnType.SPLIT and row.quantity and row.price:
                units = units * row.quantity / row.price
            elif row.type in OPENING_TYPES:
                units += row.quantity or Decimal(0)
            elif row.type in CLOSING_TYPES:
                units -= row.quantity or Decimal(0)
    return units


def _txn(
    ctx: _Context,
    account: Account,
    activity: InvestmentActivity,
    security_id: uuid.UUID | None,
    digest: str,
) -> InvestmentTxn:
    quantity, price = activity.quantity, activity.unit_price
    if activity.type == "split" and activity.split_ratio is not None:
        # The lot engine's convention: ``quantity`` new units per ``price`` old.
        quantity, price = activity.split_ratio
    return InvestmentTxn(
        id=uuid7(),
        household_id=ctx.household_id,
        account_id=account.id,
        security_id=security_id,
        type=InvestmentTxnType(activity.type),
        trade_date=activity.trade_date,
        settle_date=activity.settle_date,
        quantity=quantity,
        price=price,
        amount=to_storage(activity.amount),
        fees=to_storage(abs(activity.fees)),
        currency=account.currency,
        lot_links=[],
        memo=activity.memo[:1000] if activity.memo else None,
        import_hash=digest,
        source_document_id=ctx.document.id,
    )


def _openings(
    ctx: _Context,
    account: Account,
    statement: InvestmentStatement,
    positions: Sequence[tuple[InvestmentPosition, Security]],
    created: Sequence[InvestmentTxn],
    on: date,
    existing: set[str],
) -> list[InvestmentTxn]:
    """``transfer_in`` rows for units the account's history does not explain.

    The statement's positions are the truth on ``on``. Units held before the
    first imported statement enter as one opening ``transfer_in`` per
    security on the period's first day (cost: the printed cost basis per
    unit, else the price); lots holding more than reported raise a
    ``position_mismatch`` item and are left as they are.
    """
    held = _held_units(ctx, account)
    reported: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    sample: dict[uuid.UUID, InvestmentPosition] = {}
    for position, security in positions:
        if position.quantity > 0:
            reported[security.id] += position.quantity
            sample.setdefault(security.id, position)
    by_security: dict[uuid.UUID, list[InvestmentTxn]] = defaultdict(list)
    for row in created:
        if row.security_id is not None:
            by_security[row.security_id].append(row)
    opened_on = statement.period_start or on
    first_trade = min((row.trade_date for row in created), default=opened_on)
    opened_on = min(opened_on, first_trade)
    openings: list[InvestmentTxn] = []
    differences: list[dict[str, Any]] = []
    for security_id in sorted(set(reported) | set(held) | set(by_security), key=lambda u: u.bytes):
        start = held.get(security_id, Decimal(0))
        rows = by_security.get(security_id, [])
        with localcontext(calc_context()):
            base = _units_after(start, rows)
            scale = _units_after(start + 1, rows) - base
            want = reported.get(security_id, Decimal(0))
            missing = (want - base) / scale if scale > 0 else Decimal(0)
        if missing < -UNIT_TOLERANCE or (scale <= 0 and want - base > UNIT_TOLERANCE):
            differences.append(
                {
                    "security_id": str(security_id),
                    "history": _units(base),
                    "reported": _units(want),
                }
            )
            continue
        if missing <= UNIT_TOLERANCE:
            continue
        position = sample[security_id]
        with localcontext(calc_context()):
            if position.cost_basis is not None and position.quantity > 0:
                cost = abs(position.cost_basis) / position.quantity
            else:
                cost = position.unit_price or Decimal(0)
        digest = hashlib.sha256(
            _SEP.join(
                (HASH_VERSION, str(account.id), "opening", str(security_id), opened_on.isoformat())
            ).encode()
        ).hexdigest()
        if digest in existing:
            continue
        existing.add(digest)
        row = InvestmentTxn(
            id=uuid7(),
            household_id=ctx.household_id,
            account_id=account.id,
            security_id=security_id,
            type=InvestmentTxnType.TRANSFER_IN,
            trade_date=opened_on,
            quantity=missing.quantize(QUANTUM),
            price=cost.quantize(QUANTUM),
            amount=Decimal(0),
            fees=Decimal(0),
            currency=account.currency,
            lot_links=[],
            memo=OPENING_MEMO,
            import_hash=digest,
            source_document_id=ctx.document.id,
        )
        ctx.db.add(row)
        openings.append(row)
    if differences:
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "position_mismatch",
                "field": "quantity",
                "account_id": str(account.id),
                "as_of": on.isoformat(),
                "securities": differences[:IDS_MAX],
            },
        )
    return openings


def _cash_statement(
    statement: InvestmentStatement, rows: Iterable[InvestmentActivity]
) -> Statement:
    return Statement(
        source_format=statement.source_format,
        account=statement.account,
        period_start=statement.period_start,
        period_end=statement.period_end,
        transactions=[
            ExtractedTransaction(
                posted=row.trade_date,
                amount=row.amount,
                payee=row.memo,
                fitid=row.fitid,
                type=row.type,
                confidence=row.confidence,
                page=row.page,
                bbox=row.bbox,
            )
            for row in rows
        ],
    )


@dataclass
class _Written:
    created: list[InvestmentTxn]
    cash_rows: list[InvestmentActivity]
    positions: list[tuple[InvestmentPosition, Security]]
    duplicates: int
    openings: int
    replayed: int


def _write(
    ctx: _Context, statement: InvestmentStatement, account: Account, on: date | None
) -> _Written | None:
    """Securities, activity, openings and the lot replay; None: held back (out of order)."""
    db = ctx.db
    positions = [(p, _security(ctx, p.security, account)) for p in statement.positions]
    existing = _existing_hashes(ctx, account)
    seen: dict[tuple[Any, ...], int] = defaultdict(int)
    created: list[InvestmentTxn] = []
    cash_rows: list[InvestmentActivity] = []
    duplicates = 0
    for activity in sorted(statement.activity, key=lambda a: a.trade_date):
        if activity.type in CASH_TYPES:
            cash_rows.append(activity)
            continue
        security = (
            _security(ctx, activity.security, account) if activity.security is not None else None
        )
        security_id = security.id if security is not None else None
        identity = (
            activity.trade_date,
            activity.type,
            security_id,
            activity.quantity,
            activity.split_ratio,
            to_storage(activity.amount),
        )
        index = seen[identity]
        seen[identity] += 1
        digest = activity_hash(account.id, activity, security_id, index)
        if digest in existing:
            duplicates += 1
            continue
        existing.add(digest)
        created.append(_txn(ctx, account, activity, security_id, digest))
    if on is not None and created and _later_activity(ctx, account, on):
        # Its rows predate activity already recorded, which an opening
        # position may already count: adding them could double units.
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "investment_out_of_order",
                "account_id": str(account.id),
                "as_of": on.isoformat(),
                "rows": len(created),
            },
        )
        return None
    db.add_all(created)
    db.flush()
    openings: list[InvestmentTxn] = []
    if on is not None and statement.positions:
        openings = _openings(ctx, account, statement, positions, created, on, existing)
        db.flush()
    touched = {r.security_id for r in (*created, *openings) if r.security_id is not None}
    touched |= {security.id for _, security in positions}
    for security_id in sorted(touched, key=lambda u: u.bytes):
        replay_position(db, account, security_id, conflict_status=409)
    return _Written(created, cash_rows, positions, duplicates, len(openings), len(touched))


def _commit(
    ctx: _Context, statement: InvestmentStatement, account: Account, check: InvestmentReconciliation
) -> bool:
    """Write one reconciled statement; False when it had to be held back."""
    db = ctx.db
    result = ctx.result
    on = statement.as_of or statement.period_end
    created_before = result.securities_created
    try:
        with db.begin_nested():
            written = _write(ctx, statement, account, on)
    except ProblemError as exc:
        # The lot engine refused the history (e.g. a sale of more units than
        # held): nothing of the statement is kept.
        ctx.securities.clear()
        result.securities_created = created_before
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "investment_lot_conflict",
                "account_id": str(account.id),
                "code": exc.slug,
            },
        )
        return False
    if written is None:
        return False
    result.positions += len(written.positions)
    result.activity += len(written.created)
    result.duplicates += written.duplicates
    result.opening_positions += written.openings

    for position, security in written.positions:
        if security.currency == account.currency:
            _record_price(ctx, security, position, on)
    db.flush()

    if written.cash_rows:
        ctx.commit_cash(_cash_statement(statement, written.cash_rows), account)
        result.cash_rows += len(written.cash_rows)

    values = [position_value(p) for p in statement.positions]
    cash = statement.cash_closing.amount if statement.cash_closing is not None else None
    if (
        check.status == "ok"
        and on is not None
        and cash is not None
        and all(v is not None for v in values)
    ):
        total = sum((v for v in values if v is not None), Decimal(0)) + cash
        accounts_repo.upsert_snapshot(
            db,
            account,
            on,
            to_storage(total),
            BalanceSource.STATEMENT,
            source_document_id=ctx.document.id,
        )
        result.snapshots += 1
        emit(db, ctx.household_id, "balance.updated", {"account_id": str(account.id)})

    audit(
        db,
        action="investment.import",
        target_type="document",
        target_id=ctx.document.id,
        after={
            "account_id": str(account.id),
            "activity": len(written.created),
            "opening_positions": written.openings,
            "positions": len(written.positions),
            "transaction_ids": [str(t.id) for t in written.created[:IDS_MAX]],
        },
        household_id=ctx.household_id,
        user_id=ctx.user_id,
    )
    emit(
        db,
        ctx.household_id,
        "investment.imported",
        {
            "document_id": str(ctx.document.id),
            "account_id": str(account.id),
            "count": len(written.created),
        },
    )
    return True


def import_investment_statements(
    db: Session,
    document: Document,
    statements: Sequence[InvestmentStatement],
    *,
    allowed: Collection[uuid.UUID] | None,
    review: Review,
    commit_cash: CommitCash,
    account_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> InvestmentImport:
    """Commit the brokerage statements of ``document`` (module doc)."""
    ctx = _Context(
        db=db,
        document=document,
        allowed=allowed,
        review=review,
        commit_cash=commit_cash,
        user_id=user_id,
        result=InvestmentImport(),
    )
    single = len(statements) == 1
    for index, statement in enumerate(statements):
        account = _account(ctx, statement, index, single=single, account_id=account_id)
        if account is None:
            continue
        if account.id not in ctx.result.accounts:
            ctx.result.accounts.append(account.id)
        check = statement.reconciliation or reconcile_investment(statement)
        if _held(ctx, statement, check, account):
            ctx.result.held += 1
            continue
        if _commit(ctx, statement, account, check):
            ctx.result.statements += 1
        else:
            ctx.result.held += 1
    return ctx.result
