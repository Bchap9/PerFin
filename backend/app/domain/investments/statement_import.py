# SPDX-License-Identifier: AGPL-3.0-or-later
"""Committing brokerage statements: securities, activity, openings, prices, snapshots.

Called by the structured importer (``app.domain.imports.service.import_extraction``)
for the ``investments`` of an extraction: OFX ``INVSTMTRS`` from the sandbox,
PDF/scan statements read by ``app.ingestion.parsers.brokerage`` and the
brokerage activity tables of CSV/TSV/XLSX exports read by
``app.ingestion.parsers.brokerage_table`` (``source_format = "csv"``). It runs
in the importer's transaction and RLS context (INGESTION.md "Brokerage
statements"). Per statement:

1. **Account**: the document's account (one statement), the account the
   person chose on a re-run, else the household account with the last four
   digits (institution breaks ties) among those the uploader may edit
   (``importable_account_ids``, SECURITY.md F3). A document of several
   accounts (a PDF with one statement per account,
   ``app.ingestion.parsers.brokerage``) matches each statement by its own
   last four digits; a statement answered "import anyway" or "import as
   read" goes to the account it was answered on, wherever it now sits in
   the document (``app.domain.imports.statement_answers``), unless the
   run assigns it elsewhere; a chosen account there takes only the statements with
   its own last four digits or, when it records none, the single statement
   nothing matched, so one choice never pours several accounts into one. It must be a brokerage or
   retirement account in the statement's currency; otherwise an
   ``unknown_account`` item and nothing is written.
2. **Same statement** (``app.domain.imports.statement_records``): a
   statement whose printed period overlaps one already imported for the
   account from another document is held with a ``possible_duplicate``
   item (``statement_overlap``) unless the person said to import this
   statement into this account anyway (``statement_answers``) or the run
   names the account in ``allow_overlap`` (``apply-mapping``); every
   committed statement leaves a record.
3. **Hold** (SECURITY.md C12): no row a statement's own figures do not
   vouch for is written. Investment rows have no pending state, so what is
   in doubt is held back and a ``low_confidence_field`` item says what and
   why. What is held depends on what the reconciliation says
   (:func:`app.ingestion.parsers.brokerage.reconcile_investment`:
   positions x prices vs market values, holdings total, cash, account
   value; for a document statement
   :func:`~app.ingestion.parsers.brokerage.localise` adds where a failed
   check comes from: each activity section against its printed total and
   against the statement's cash-flow summary, and the rows left in doubt):

   * **nothing in doubt, figures disagree** (the printed account value is
     not holdings + cash, a section's total is off by an amount no row
     explains): the statement is imported whole and an
     ``investment_figures_differ`` item tells the person what does not add
     up (nothing to answer);
   * **only lines moving cash are in doubt** (:data:`CASH_ONLY_TYPES`:
     dividends, interest, fees, deposits, withdrawals; whether for the
     ``reconciliation`` doubt or any other, a date outside the period, an
     amount hard to read): the positions and every other line are written
     and an ``investment_lines_held`` item lists the lines held back
     (``lines``: page, date, type, description, amount, section and the
     doubts of each) with the sections that do not add up (``sections``);
   * **anything else** (a line moving units or a position in doubt: the
     positions are reconciled against every such line; a source that does
     not say where a failed check comes from: OFX, an activity table; a
     document with nothing to check against): the statement writes
     **nothing** and an ``investment_reconciliation_failed`` /
     ``investment_low_confidence`` item names the discrepancy: its
     ``differences`` (per failed check, what the statement prints, what its
     rows add up to and the difference; see :func:`differences`), the
     ``sections`` that do not add up (:func:`section_differences`), the
     ``lines`` in doubt when the statement says which, the cash-flow lines
     no row was read for (``unread_summary_lines``), the statement's index
     and key (``statement_answers.statement_key``) and the account's
     currency.

   A row whose kind is the optional local model's suggestion (its words
   are in no vocabulary of the parser's own; ADR-0010,
   ``app.ingestion.parsers.vocabulary``) carries the ``suggested_type``
   doubt and is held like any row in doubt: alone when it moves cash, with
   its statement when it moves units. It is never written until a person
   says so.

   **Import as read** (the review queue's answer to the last two kinds of
   item, see ``app.domain.review.service``; recorded per statement key and
   account by ``app.domain.imports.statement_answers``): every later import
   of the document imports that statement, on that account only, as printed
   although it does not add up or a document gives it nothing to check
   against (the held lines of an imported statement: the rows already
   written are skipped by their hashes). Another statement, even of the
   same account, is not covered by it. For the answered statement the
   ``reconciliation`` and ``suggested_type`` doubts its rows carry
   (:data:`CONFIRMABLE_DOUBTS`; and the contradicted market
   value of a position that failed the ``positions`` check) no longer hold
   it, while any other doubt still does (a guessed date, an unread figure),
   as do the date range and every later step. An accepted statement writes
   **no balance snapshot** (step 9): its totals are the figures that did not
   add up, so the account's value is never taken from them; its audit row
   (``investment.import``) says it was accepted and which checks failed. A
   statement with any date before
   ``app.core.dates.MIN_BUSINESS_DATE`` is held the same way
   (``investment_date_out_of_range``, SECURITY.md F8). A structured
   statement (OFX, or an activity table: ``csv``, every value from a named
   column) with nothing to check is imported like an OFX bank statement; a
   document one cannot be (its rows score below the threshold). An activity
   table whose day/month order was a guess marks every row
   ``low_confidence`` (``trade_date``) and is held like any such row.
4. **Securities**: found in the household's catalogue by CUSIP, ISIN, then
   symbol, else by name (:func:`~app.domain.investments.models.security_name_key`:
   the same words, case and spacing aside; never a similar name), else
   created (quote currency = the account's). By name, only a security in
   the account's currency is taken, looked for in turn among those the
   account holds or traded, those another account at its institution holds
   or traded, and those no account holds or traded (added by hand), so a
   generic name (``Moderate Portfolio``) in another plan is never the same
   security and an account keeps the security it already holds when
   another of that name appears. A security printed by its name alone
   (``SecurityRef`` with no symbol, CUSIP or ISIN: an unlisted
   partnership, a 529 portfolio) takes, in the first of those groups with
   any security of the name, the one with no identifier, else the only
   one, else (several) a new one; with none it is created **without a
   symbol**. One printed with identifiers takes, by name, only a security
   that has none. Two positions of one statement that take one security
   by its name alone at different prices are different securities nothing
   tells apart: the statement writes nothing and an
   ``investment_same_name_securities`` item names them. Identifiers a
   later statement adds are filled in: a CUSIP or ISIN, and a symbol on a
   security without one (unless another security already has that symbol).
5. **Activity** -> ``investment_txn`` with an ``import_hash`` (account,
   trade date, type, security, quantity, amount, running index among
   identical rows), so the same statement as OFX and as PDF, or overlapping
   statements, add each row once. A row the account already records from
   another document under another date (same type, security, units and
   amount; the rows share a trade or settlement date, or one has no
   settlement date and they are at most :data:`SETTLE_WINDOW_DAYS` apart:
   Fidelity's activity CSV dates a trade by its run date, its PDF by the
   settlement date) is **not written**: investment rows have no pending
   state, so one ``low_confidence_field`` item per statement
   (``investment_possible_duplicate``: the account, the count and the
   recorded rows' ids) says so, and a trade that really happened twice is
   added by hand (``POST /investments/transactions``). Rows on the same
   date are left to the hash. Cash ``deposit``/``withdrawal`` rows are
   ledger transactions on the account (through the structured importer:
   transaction ``import_hash`` dedupe, rules, provenance).
6. **Out of order**: new rows of a statement older than activity already
   recorded for the account are not written (an opening position may
   already count them); an ``investment_out_of_order`` item says so.
   Import statements oldest first.
7. **Opening positions and lots**: the reported positions are the truth on
   ``as_of``. Units the account's history does not explain (held before
   the first imported statement) enter as one ``transfer_in`` per security
   on the period's first day (memo ``Opening position from statement``;
   cost per unit from the printed cost basis, else the price); history
   holding more than reported raises a ``position_mismatch`` item (a
   short position, a negative quantity, counts as none: the lot engine
   holds no short positions). Lots,
   ``lot_links`` and the ``holding`` cache are then written by the
   investments lot engine (``service.replay_position``, FIFO or average
   cost per account). An activity export (``csv``, no positions) that
   sells units before it adds them opens the shortfall first
   (:func:`_shortfall_openings`: one ``transfer_in`` per security on its
   first row date, no price, memo :data:`UNKNOWN_BASIS_MEMO`) with an
   ``investment_opening_basis_unknown`` item asking for the cost basis. A
   history the engine still refuses (selling units never held) rolls the
   statement back (savepoint) with an ``investment_lot_conflict`` item.
   A **positions export** (``app.ingestion.parsers.positions_table``: what
   every account holds on the day of the download, no activity) is a
   *snapshot*: its positions are the whole account on ``as_of``
   (:func:`_snapshot_adjustments`). Per security, on that day: units the
   account does not hold yet enter as one ``transfer_in`` (memo
   :data:`SNAPSHOT_IN_MEMO`) costed so that the position's cost basis is
   the one the export prints (its printed basis less what the account
   already carries; the price when it prints none, with the
   ``investment_opening_basis_unknown`` item of an opening); units it no
   longer lists leave as one ``transfer_out`` (memo
   :data:`SNAPSHOT_OUT_MEMO`; a transfer realises nothing). A position
   known only from statements and exports (every open lot opened by an
   opening or snapshot row, :data:`SYNTHETIC_MEMOS`, in a FIFO account)
   whose basis would still differ from the printed one by more than
   :data:`BASIS_TOLERANCE` is **reset**: every unit held leaves and the
   reported units enter at the printed basis, so units and basis are the
   export's exactly. A position with lots of its own (bought through
   imported activity) keeps them: only its units are brought to the
   export's, its basis stays what its history says. Nothing changes when
   the account already holds what the export lists, so a re-run, or the
   same export twice, adds nothing. An export older than investment
   activity already recorded for the account, that would change its
   units, is not applied (``investment_out_of_order``); and activity
   moving units on or before the day of a snapshot row already recorded
   is held the same way (the snapshot already counts it), so import
   activity first, then the positions.
8. **Prices**: each position's unit price on ``as_of`` becomes a ``price``
   row (``source = statement``) unless one exists for that day from another
   source. A security has one price a day: when a document lists it more
   than once for a day (a plan's position per contribution source, a
   sub-account's), the first price printed is the day's, and a later one
   that differs is left out and named in the run's summary
   (``warnings``: :data:`DUPLICATE_PRICE_IGNORED`); the units of every
   position still count. A price quoted per 100 of face value (a bond) is
   stored as one unit's (``InvestmentPosition.price_per_unit``), and so is
   an activity row's price (``InvestmentActivity.price_per_unit``: a bond trade's
   ``price`` is one unit's, never 100 times it). An opening position for
   units held before the period and gone by its end (transferred or sold
   out) is costed at the price its own rows print (one unit's).
9. **Balance snapshot**: holdings + closing cash on ``as_of``
   (``source = statement``) when every check that could run held and both
   are known; also when only the ``cash`` activity check failed on a
   statement that says where (step 3) and holdings + closing cash is the
   printed account value (:func:`_value_holds`: the lines in doubt do not
   change what the account is worth). An account that prints no cash at
   all and whose printed account value is its holdings holds none (a 529
   plan); any other statement printing no cash gives no snapshot. Never
   for a statement accepted as read (step 3). With it, when the account has
   no snapshot from the day before the period to its end, the value the
   statement **opens** with is recorded for the day before the period
   (:func:`opening_value`: only from an account-value summary that adds up
   to the closing value recorded), so an account's history starts with its
   first statement's opening value rather than its closing one.
"""

from __future__ import annotations

import hashlib
import uuid
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
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
from app.domain.imports.statement_answers import StatementAnswers, statement_key
from app.domain.imports.statement_records import (
    check_overlap,
    investment_facts,
    record_statement,
)
from app.domain.investments.models import (
    AssetClass,
    CostMethod,
    Holding,
    InvestmentTxn,
    InvestmentTxnType,
    Lot,
    Price,
    Security,
    security_name_key,
)
from app.domain.investments.service import cost_method, replay_position
from app.ingestion.match.accounts import match_account
from app.ingestion.parsers.brokerage import position_value, reconcile_investment
from app.ingestion.parsers.positions_table import is_positions_snapshot
from app.ingestion.parsers.vocabulary import SUGGESTED_DOUBT
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
#: Run warning: a security listed again for a day at another price (step 8).
DUPLICATE_PRICE_IGNORED: Final = "duplicate_price_ignored"
INVESTMENT_ACCOUNT_TYPES: Final = frozenset({AccountType.BROKERAGE, AccountType.RETIREMENT})
#: Cash rows that are ledger transactions, not ``investment_txn`` rows.
CASH_TYPES: Final = frozenset({"deposit", "withdrawal"})
#: Units below this are rounding, not a position difference.
UNIT_TOLERANCE: Final = Decimal("0.000001")
QUANTUM: Final = Decimal("1e-10")
IDS_MAX: Final = 50
#: Days between two formats' dates of one trade (trade vs settlement date).
SETTLE_WINDOW_DAYS: Final = 5
OPENING_MEMO: Final = "Opening position from statement"
#: Memo of the opening a sale of units held before an activity export implies.
UNKNOWN_BASIS_MEMO: Final = "Opening position before this export (cost basis unknown)"
#: Memos of the rows a positions export writes (module doc, step 7).
SNAPSHOT_IN_MEMO: Final = "Units held per positions export"
SNAPSHOT_OUT_MEMO: Final = "Units no longer held per positions export"
#: Openings the importer made up from a statement's or export's figures: a
#: position whose open lots all come from them has no history of its own.
SYNTHETIC_MEMOS: Final = frozenset({OPENING_MEMO, UNKNOWN_BASIS_MEMO, SNAPSHOT_IN_MEMO})
#: A cost basis within this of the printed one is the printed one.
BASIS_TOLERANCE: Final = Decimal("0.01")
OPENING_TYPES: Final = frozenset(
    {InvestmentTxnType.BUY, InvestmentTxnType.REINVEST, InvestmentTxnType.TRANSFER_IN}
)
CLOSING_TYPES: Final = frozenset({InvestmentTxnType.SELL, InvestmentTxnType.TRANSFER_OUT})
#: Rows that change the units an account holds.
UNIT_ROW_TYPES: Final = (
    OPENING_TYPES
    | CLOSING_TYPES
    | frozenset({InvestmentTxnType.SPLIT, InvestmentTxnType.CORPORATE_ACTION})
)
_SEP: Final = "\x1f"

Review = Callable[[ReviewKind, dict[str, Any]], None]
CommitCash = Callable[[Statement, Account], object]


@dataclass
class InvestmentImport:
    """What one run did (counts; the importer merges them into its summary)."""

    statements: int = 0
    held: int = 0
    activity: int = 0
    duplicates: int = 0
    possible_duplicates: int = 0
    cash_rows: int = 0
    positions: int = 0
    securities_created: int = 0
    prices: int = 0
    opening_positions: int = 0
    snapshots: int = 0
    #: Held as overlapping an imported statement, or skipped as the same one.
    overlaps: int = 0
    #: Imported as read although they did not add up (an answer, step 3).
    accepted_as_read: int = 0
    #: Activity lines of imported statements held back for a person (step 3).
    lines_held: int = 0
    accounts: list[uuid.UUID] = field(default_factory=list)
    #: What the run read around rather than wrote (each code once).
    warnings: list[str] = field(default_factory=list)

    def warn(self, code: str) -> None:
        if code not in self.warnings:
            self.warnings.append(code)

    def summary(self) -> dict[str, Any]:
        return {
            "statements": self.statements,
            "held": self.held,
            "activity": self.activity,
            "duplicates": self.duplicates,
            "possible_duplicates": self.possible_duplicates,
            "cash_rows": self.cash_rows,
            "positions": self.positions,
            "securities_created": self.securities_created,
            "prices": self.prices,
            "opening_positions": self.opening_positions,
            "snapshots": self.snapshots,
            "statement_overlaps": self.overlaps,
            "accepted_as_read": self.accepted_as_read,
            "lines_held": self.lines_held,
            "warnings": list(self.warnings),
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
    """``investment_txn.import_hash`` (module doc, step 5); the same in every format."""
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
    securities: dict[tuple[uuid.UUID, str, str, str], Security] = field(default_factory=dict)
    #: The household's securities by name key (loaded on first use).
    by_name: dict[str, list[Security]] | None = None
    #: The accounts that hold or traded each security (loaded on first use).
    used_by: dict[uuid.UUID, set[uuid.UUID]] | None = None
    #: The household's accounts at each institution (loaded on first use).
    peers: dict[uuid.UUID, set[uuid.UUID]] = field(default_factory=dict)
    #: The price each (security, day) took in this run: the first one printed.
    prices: dict[tuple[uuid.UUID, date], Decimal] = field(default_factory=dict)

    def forget(self) -> None:
        """Drop what a rolled-back statement may have added to the caches."""
        self.securities.clear()
        self.by_name = None
        self.used_by = None

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
    forced: uuid.UUID | None = None,
) -> Account | None:
    account: Account | None = _usable(ctx, forced)
    candidates: tuple[uuid.UUID, ...] = ()
    if account is None and single and ctx.document.account_id is not None and account_id is None:
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


#: Reasons of the items a statement held back by its figures raises; both
#: can be answered "import as read" (module doc, step 3).
RECONCILIATION_FAILED: Final = "investment_reconciliation_failed"
LOW_CONFIDENCE: Final = "investment_low_confidence"
#: The statement was imported but for the cash lines its own figures do not
#: vouch for (module doc, step 3); answered "import as read" like the two above.
LINES_HELD: Final = "investment_lines_held"
#: The statement was imported whole; figures of its own disagree and no line
#: is to blame (nothing to answer but "seen").
FIGURES_DIFFER: Final = "investment_figures_differ"
#: The doubt a row no figure of a statement that did not add up vouches for
#: carries (every row, when nothing says where: an OFX statement, one that
#: could not be checked); an accepted statement no longer holds for it.
RECONCILIATION_DOUBT: Final = "reconciliation"
#: The doubts a person settles by saying "import as read": the row's figures
#: are as printed, what was in question is whether they add up
#: (``reconciliation``) or what kind of activity the row is, where that kind
#: is the optional local model's suggestion (``suggested_type``, ADR-0010:
#: the answer is the person's confirmation of it).
CONFIRMABLE_DOUBTS: Final = frozenset({RECONCILIATION_DOUBT, SUGGESTED_DOUBT})
#: Activity that moves cash only: such a line can wait for a person on its
#: own while the rest of its statement is imported.
CASH_ONLY_TYPES: Final = frozenset({"dividend", "interest", "fee", "deposit", "withdrawal"})
#: Activity that moves (or may move) units of a security. The holdings are
#: reconciled against every such line, so one in doubt waits together with
#: every other line moving that security and with its position
#: (:func:`_split`), and the rest of the statement is imported.
UNIT_TYPES: Final = frozenset(
    {"buy", "sell", "transfer_in", "transfer_out", "reinvest", "split", "corporate_action"}
)
#: Why a row no figure doubts is held all the same: its security waits.
WITH_SECURITY: Final = "held_with_security"
#: Lines and sections listed in an item's payload at most.
LINES_MAX: Final = 200
_TEXT_MAX: Final = 120


def acceptable_as_read(payload: Mapping[str, Any]) -> bool:
    """The held statement (or lines) of a review item ``payload`` may be imported as read.

    A statement that did not add up, or a document statement with nothing to
    check against, or the held lines of an imported one, whose rows carry no
    other doubt than :data:`CONFIRMABLE_DOUBTS` (``reconciliation``, a kind
    the local model suggested): a guessed date or an unread figure is never
    imported on a click.
    """
    reason = payload.get("reason")
    if reason == RECONCILIATION_FAILED:
        return True
    fields = payload.get("fields")
    failed = payload.get("failed")
    allowed = CONFIRMABLE_DOUBTS
    if isinstance(failed, list) and "positions" in failed:
        # A holding whose printed value is not quantity x price: the figure
        # that did not add up, imported as printed like the rest.
        allowed = allowed | {"market_value"}
    return (
        reason in (LOW_CONFIDENCE, LINES_HELD)
        and isinstance(fields, list)
        and bool(fields)
        and all(isinstance(f, str) and f in allowed for f in fields)
    )


def _money_text(value: Decimal) -> str:
    return f"{to_storage(value):f}"


def differences(
    statement: InvestmentStatement, check: InvestmentReconciliation
) -> list[dict[str, Any]]:
    """What did not add up, per failed check: printed, computed, difference.

    ``printed`` is the figure the statement prints and ``computed`` what its
    rows add up to; ``difference`` is computed - printed. ``positions``
    sums the positions whose quantity x price is not their printed market
    value (``count`` of them); ``holdings_total`` compares the positions'
    sum with the printed holdings total; ``cash`` the opening cash plus the
    period's activity with the closing cash; ``total_value`` the holdings
    plus closing cash with the printed account value; ``index_value`` the
    account value printed on the account's own pages (``computed``) with
    the one the document's account index prints for it (``printed``).
    """
    out: list[dict[str, Any]] = []

    def add(name: str, printed: Decimal | None, computed: Decimal | None, **extra: Any) -> None:
        if printed is None or computed is None:
            return
        out.append(
            {
                "check": name,
                "printed": _money_text(printed),
                "computed": _money_text(computed),
                "difference": _money_text(computed - printed),
                **extra,
            }
        )

    with localcontext(calc_context()):
        if "positions" in check.failed:
            printed = Decimal(0)
            computed = Decimal(0)
            count = 0
            for index in check.position_mismatches:
                position = statement.positions[index]
                if position.unit_price is None or position.market_value is None:
                    continue
                scale = 100 if position.price_per_100 else 1
                printed += position.market_value
                computed += position.quantity * position.unit_price / scale
                count += 1
            if count:
                add("positions", printed, computed, count=count)
        if "holdings_total" in check.failed:
            add("holdings_total", check.reported_positions_total, check.positions_total)
        if "cash" in check.failed and check.cash_opening is not None:
            computed_cash = (
                check.cash_opening + check.cash_activity
                if check.cash_activity is not None
                else None
            )
            add("cash", check.cash_closing, computed_cash)
        if "total_value" in check.failed:
            add("total_value", check.reported_total_value, check.total_value)
        if "index_value" in check.failed:
            # Printed twice: by the document's account index and on the
            # account's own pages ("computed" is the account's own figure).
            add("index_value", check.index_value, check.reported_total_value)
    return out


def section_differences(check: InvestmentReconciliation) -> list[dict[str, Any]]:
    """The activity sections whose rows are not their printed total, as text.

    One entry per :class:`~app.ingestion.sandbox.protocol.SectionCheck` that
    did not hold: the total's ``label`` as printed, ``printed``, what its
    ``rows`` (their count) add up to (``computed``), the ``difference``
    (computed - printed), where the total is printed (``source``: the
    section's own ``total`` line or the cash-flow ``summary``) and the page.
    """
    return [
        {
            "label": section.label[:_TEXT_MAX],
            "printed": _money_text(section.printed),
            "computed": _money_text(section.computed),
            "difference": _money_text(section.computed - section.printed),
            "rows": len(section.rows),
            "source": section.source,
            "page": section.page,
        }
        for section in check.sections
        if section.status == "mismatch" and section.printed is not None
    ][:LINES_MAX]


def _line(statement: InvestmentStatement, index: int, doubts: Sequence[str]) -> dict[str, Any]:
    """One activity row as an item's payload shows it (what a person needs to find it)."""
    row = statement.activity[index]
    security = row.security
    described = row.memo or (security.name or security.symbol if security is not None else None)
    return {
        "index": index,
        "fields": list(doubts),
        "page": row.page,
        "date": row.trade_date.isoformat(),
        "type": row.type,
        "description": described[:_TEXT_MAX] if described else None,
        "amount": _money_text(row.amount),
        "section": row.section[:_TEXT_MAX] if row.section else None,
        # Where it is printed, for the review queue's highlight on the page.
        "bbox": [round(v, 2) for v in row.bbox] if row.bbox is not None else None,
    }


def _ref_keys(ref: SecurityRef) -> set[tuple[str, str]]:
    """What two mentions of one security share: an identifier, else the same name."""
    keys: set[tuple[str, str]] = set()
    if ref.symbol:
        keys.add(("symbol", ref.symbol.upper()))
    if ref.cusip:
        keys.add(("cusip", ref.cusip))
    if ref.isin:
        keys.add(("isin", ref.isin))
    if ref.name:
        keys.add(("name", security_name_key(ref.name)))
    return keys


@dataclass(frozen=True)
class _Split:
    """Which rows of a statement wait (:func:`_split`); ``feasible``: the rest can be written."""

    activity: frozenset[int]
    positions: frozenset[int]
    feasible: bool


def _split(
    statement: InvestmentStatement,
    doubted_activity: Iterable[int],
    doubted_positions: Iterable[int],
) -> _Split:
    """The rows that wait with the rows in doubt, so that what is written stays whole.

    A line moving cash waits alone. A line moving units, or a position, in
    doubt takes its security with it: every line moving that security and
    its position wait too (two mentions are one security when they share a
    symbol, a CUSIP, an ISIN or the same name), because the opening position
    of a security is what its reported holding is less every one of its
    lines. When no position at all would be written, no line moving units
    is (nothing would say what was held). Not feasible: a line moving units
    is in doubt and names no security, or nothing would be left to write.
    """
    activity, positions = statement.activity, statement.positions
    held_activity, held_positions = set(doubted_activity), set(doubted_positions)
    keys: set[tuple[str, str]] = set()
    for index in held_activity:
        row = activity[index]
        if row.type not in UNIT_TYPES:
            continue
        if row.security is None:
            return _Split(frozenset(held_activity), frozenset(held_positions), False)
        keys |= _ref_keys(row.security)
    for index in held_positions:
        keys |= _ref_keys(positions[index].security)
    grown = bool(keys)
    while grown:
        grown = False
        for index, row in enumerate(activity):
            if index in held_activity or row.type not in UNIT_TYPES or row.security is None:
                continue
            found = _ref_keys(row.security)
            if found & keys:
                held_activity.add(index)
                grown = grown or not found <= keys
                keys |= found
        for index, position in enumerate(positions):
            found = _ref_keys(position.security)
            if index not in held_positions and found & keys:
                held_positions.add(index)
                grown = grown or not found <= keys
                keys |= found
    if positions and len(held_positions) == len(positions):
        held_activity |= {i for i, row in enumerate(activity) if row.type in UNIT_TYPES}
    feasible = len(held_activity) + len(held_positions) < len(activity) + len(positions)
    return _Split(frozenset(held_activity), frozenset(held_positions), feasible)


@dataclass(frozen=True)
class _Kept:
    """What of a statement is written now (:func:`_held`)."""

    statement: InvestmentStatement
    #: Rows left out for a person: activity lines and positions.
    lines_held: int = 0
    positions_held: int = 0
    #: Lines the person chose to leave out for good (an answer naming lines).
    lines_left_out: int = 0
    #: The securities that wait whole: nothing of them is opened or compared.
    waiting: tuple[SecurityRef, ...] = ()


def _evidence(check: InvestmentReconciliation) -> dict[str, Any]:
    """What the statement's own figures say beyond the four checks (payload keys)."""
    return {
        "sections": section_differences(check),
        "unread_summary_lines": [label[:_TEXT_MAX] for label in check.unread_summary_lines][
            :IDS_MAX
        ],
    }


def _doubts(fields: Sequence[str], ignored: frozenset[str]) -> list[str]:
    return [f for f in fields if f not in ignored]


def _held(
    ctx: _Context,
    statement: InvestmentStatement,
    check: InvestmentReconciliation,
    account: Account,
    *,
    index: int,
    key: str,
    accepted: bool = False,
    accepted_lines: frozenset[int] | None = None,
) -> _Kept | None:
    """What of the statement may be written now (module doc, step 3); None: nothing.

    The statement itself when nothing holds it; the statement without the
    rows that wait (:func:`_split`: a cash line in doubt alone, a security
    in doubt whole) when the rest can be written (an item lists them); None
    when it is held back whole (an item says why).

    ``accepted``: the person said to import it as read, so its figures not
    adding up, their not being checkable, or a kind being the local model's
    suggestion no longer hold it or any line; nothing else changes.
    ``accepted_lines``: that answer named the lines to import (their
    indices); the other lines it was asked about are left out for good, with
    no item raised for them again.
    """
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
        return None
    ignored = CONFIRMABLE_DOUBTS if accepted else frozenset()
    contradicted = frozenset({"market_value"}) | ignored
    mismatched = set(check.position_mismatches) if accepted else set()
    low_positions = [
        (i, doubts)
        for i, p in enumerate(statement.positions)
        if (doubts := _doubts(p.low_confidence, contradicted if i in mismatched else ignored))
    ]
    left_out: set[int] = set()
    low_activity: list[tuple[int, list[str]]] = []
    for i, row in enumerate(statement.activity):
        if accepted_lines is not None and i not in accepted_lines:
            # A line the answer did not name: left out when it was in doubt.
            if _doubts(row.low_confidence, frozenset()):
                left_out.add(i)
            continue
        doubts = _doubts(row.low_confidence, ignored)
        if doubts:
            low_activity.append((i, doubts))
    if left_out:
        statement = statement.model_copy(
            update={"activity": [a for i, a in enumerate(statement.activity) if i not in left_out]}
        )
        # Indices below are the shortened statement's.
        shift = sorted(left_out)
        low_activity = [(i - sum(1 for gone in shift if gone < i), d) for i, d in low_activity]
    #: The parser said which rows the failed checks leave in doubt (``localise``).
    localised = check.doubtful_activity is not None and check.doubtful_positions is not None
    failing = check.status == "mismatch" and not accepted
    unchecked_document = (
        check.status == "unavailable"
        and statement.source_format in DOCUMENT_STATEMENT_FORMATS
        and not accepted
    )
    doubted = bool(low_positions or low_activity)
    if not doubted and not unchecked_document and not (failing and not localised):
        if failing:
            # Every row is vouched for; figures of the statement itself
            # disagree (its account value, a section's total). Imported, and
            # a person is told what does not add up.
            ctx.review(
                ReviewKind.LOW_CONFIDENCE_FIELD,
                {
                    "reason": FIGURES_DIFFER,
                    "field": "reconciliation",
                    "account_id": str(account.id),
                    "statement": index,
                    "statement_key": key,
                    "currency": account.currency,
                    "parser": _parser(statement),
                    "failed": list(check.failed),
                    "checks": list(check.checks),
                    "differences": differences(statement, check),
                    **_evidence(check),
                },
            )
        return _Kept(statement, lines_left_out=len(left_out))
    split = _split(statement, (i for i, _ in low_activity), (i for i, _ in low_positions))
    doubts_of = dict(low_activity)
    lines = [
        _line(statement, i, doubts_of.get(i, [WITH_SECURITY]))
        for i in sorted(split.activity)[:LINES_MAX]
    ]
    fields = sorted(
        {f for _, doubts in low_positions for f in doubts}
        | {f for _, doubts in low_activity for f in doubts}
        | ({RECONCILIATION_DOUBT} if unchecked_document else set())
    )
    position_doubts = dict(low_positions)
    held_positions = [
        {
            "index": i,
            "symbol": statement.positions[i].security.symbol,
            "name": statement.positions[i].security.name,
            "fields": position_doubts.get(i, [WITH_SECURITY]),
            "page": statement.positions[i].page,
            "bbox": (
                [round(v, 2) for v in box]
                if (box := statement.positions[i].bbox) is not None
                else None
            ),
        }
        for i in sorted(split.positions)[:IDS_MAX]
    ]
    partial = doubted and split.feasible and not unchecked_document and (localised or not failing)
    if partial:
        # The rows in doubt wait (a cash line alone, a security whole); the
        # rest of the statement is written.
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": LINES_HELD,
                "fields": fields,
                "account_id": str(account.id),
                "statement": index,
                "statement_key": key,
                "currency": account.currency,
                "parser": _parser(statement),
                "reconciliation": check.status,
                "failed": list(check.failed),
                "checks": list(check.checks),
                "differences": differences(statement, check),
                **_evidence(check),
                "lines": lines,
                "positions": held_positions,
                "held": len(split.activity),
                "imported": len(statement.activity) - len(split.activity),
                # Each line may be answered on its own when none moves units
                # (a security's lines and its position go together).
                "selectable": not split.positions
                and all(statement.activity[i].type in CASH_ONLY_TYPES for i in split.activity),
            },
        )
        ctx.result.lines_held += len(split.activity)
        waiting = [statement.positions[i].security for i in sorted(split.positions)]
        waiting += [
            row.security
            for i in sorted(split.activity)
            if (row := statement.activity[i]).type in UNIT_TYPES and row.security is not None
        ]
        kept = statement.model_copy(
            update={
                "activity": [
                    a for i, a in enumerate(statement.activity) if i not in split.activity
                ],
                "positions": [
                    p for i, p in enumerate(statement.positions) if i not in split.positions
                ],
            }
        )
        return _Kept(
            kept,
            lines_held=len(split.activity),
            positions_held=len(split.positions),
            lines_left_out=len(left_out),
            waiting=tuple(waiting),
        )
    if failing:
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": RECONCILIATION_FAILED,
                "field": "reconciliation",
                "account_id": str(account.id),
                "statement": index,
                "statement_key": key,
                "currency": account.currency,
                "parser": _parser(statement),
                "failed": list(check.failed),
                "checks": list(check.checks),
                "differences": differences(statement, check),
                **_evidence(check),
                # The rows in doubt, when the statement's figures say which.
                "lines": lines if localised else [],
                "positions": [
                    {
                        "index": i,
                        "symbol": statement.positions[i].security.symbol,
                        "name": statement.positions[i].security.name,
                        # Why it is listed: its value is not quantity x price.
                        "fields": ["market_value"],
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
        return None
    ctx.review(
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {
            "reason": LOW_CONFIDENCE,
            "fields": fields,
            "account_id": str(account.id),
            "statement": index,
            "statement_key": key,
            "parser": _parser(statement),
            "reconciliation": check.status,
            "positions": [
                {"index": i, "fields": doubts, "page": statement.positions[i].page}
                for i, doubts in low_positions[:IDS_MAX]
            ],
            "activity": [
                {"index": i, "fields": doubts, "page": statement.activity[i].page}
                for i, doubts in low_activity[:IDS_MAX]
            ],
            "lines": [_line(statement, i, doubts) for i, doubts in low_activity[:LINES_MAX]],
        },
    )
    return None


# ---------------------------------------------------------------------------
# Securities and prices
# ---------------------------------------------------------------------------


def _named(ctx: _Context) -> dict[str, list[Security]]:
    """The household's securities by :func:`security_name_key`, oldest first."""
    if ctx.by_name is None:
        found = ctx.db.execute(
            select(Security)
            .where(Security.household_id == ctx.household_id)
            .order_by(Security.created_at, Security.id)
        )
        ctx.by_name = defaultdict(list)
        for security in found.scalars():
            ctx.by_name[security_name_key(security.name)].append(security)
    return ctx.by_name


def _unidentified(security: Security) -> bool:
    return security.symbol is None and security.cusip is None and security.isin is None


def _used(ctx: _Context) -> dict[uuid.UUID, set[uuid.UUID]]:
    """The accounts holding or having traded each of the household's securities."""
    if ctx.used_by is None:
        held = select(Holding.security_id, Holding.account_id).where(
            Holding.household_id == ctx.household_id
        )
        traded = select(InvestmentTxn.security_id, InvestmentTxn.account_id).where(
            InvestmentTxn.household_id == ctx.household_id,
            InvestmentTxn.security_id.is_not(None),
        )
        ctx.used_by = defaultdict(set)
        for security_id, account_id in ctx.db.execute(held.union(traded)):
            if security_id is not None:
                ctx.used_by[security_id].add(account_id)
    return ctx.used_by


def _peers(ctx: _Context, account: Account) -> set[uuid.UUID]:
    """The household's accounts at ``account``'s institution (none when it has none)."""
    institution = account.institution_id
    if institution is None:
        return set()
    if institution not in ctx.peers:
        found = ctx.db.execute(
            select(Account.id).where(
                Account.household_id == ctx.household_id,
                Account.institution_id == institution,
            )
        )
        ctx.peers[institution] = set(found.scalars())
    return ctx.peers[institution]


def _by_name(ctx: _Context, ref: SecurityRef, account: Account) -> Security | None:
    """The security ``ref`` names by its name (module doc, step 4).

    Only a security in the account's currency, looked for in turn among the
    securities the account itself holds or traded, those another account at
    its institution holds or traded, and those no account holds or traded
    (added by hand): never one of another plan's securities that merely
    shares a generic name. A reference printed by its name alone takes, in
    the first of these groups with any security of the name, the one
    without identifiers, else the only one (several: none, a new security
    is made); one printed with identifiers takes only a security without
    any.
    """
    key = security_name_key(ref.name or "")
    if not key:
        return None
    same = [s for s in _named(ctx).get(key, []) if s.currency == account.currency]
    if not same:
        return None
    used = _used(ctx)
    peers = _peers(ctx, account)
    groups = (
        [s for s in same if account.id in used.get(s.id, ())],
        [s for s in same if used.get(s.id, set()) & peers],
        [s for s in same if not used.get(s.id)],
    )
    for group in groups:
        if not group:
            continue
        bare = [s for s in group if _unidentified(s)]
        if bare:
            return bare[0]
        if not ref.identified:
            return group[0] if len(group) == 1 else None
    return None


def _find_security(ctx: _Context, ref: SecurityRef, account: Account) -> Security | None:
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
    return _by_name(ctx, ref, account)


def _symbol_free(ctx: _Context, symbol: str) -> bool:
    taken = select(Security.id).where(
        Security.household_id == ctx.household_id,
        Security.symbol == symbol,
        Security.exchange.is_(None),
    )
    return ctx.db.execute(taken.limit(1)).first() is None


def _security(ctx: _Context, ref: SecurityRef, account: Account) -> Security:
    # Per account: which security a name names depends on the account.
    key = (
        account.id,
        ref.cusip or ref.isin or "",
        ref.symbol or "",
        "" if ref.identified else security_name_key(ref.name or ""),
    )
    cached = ctx.securities.get(key)
    if cached is not None:
        return cached
    security = _find_security(ctx, ref, account)
    if security is None:
        symbol = ref.symbol or ref.cusip or ref.isin
        security = Security(
            id=uuid7(),
            household_id=ctx.household_id,
            symbol=symbol,
            cusip=ref.cusip,
            isin=ref.isin,
            name=(ref.name or symbol or "")[:200],
            asset_class=AssetClass(ref.asset_class or AssetClass.OTHER),
            currency=account.currency,
        )
        ctx.db.add(security)
        ctx.db.flush()
        ctx.result.securities_created += 1
        _named(ctx)[security_name_key(security.name)].append(security)
    else:
        if security.cusip is None and ref.cusip is not None:
            security.cusip = ref.cusip
        if security.isin is None and ref.isin is not None:
            security.isin = ref.isin
        if security.symbol is None and ref.symbol is not None and _symbol_free(ctx, ref.symbol):
            security.symbol = ref.symbol
    ctx.securities[key] = security
    _used(ctx)[security.id].add(account.id)
    return security


def _record_price(
    ctx: _Context, security: Security, position: InvestmentPosition, on: date | None
) -> None:
    day = position.price_as_of or on
    # One unit's price (a bond's price per 100 of face value, divided by 100).
    price = position.price_per_unit
    if day is None or price is None or price <= 0:
        return
    first = ctx.prices.get((security.id, day))
    if first is not None:
        # Listed again for the day (module doc, step 8). The row of the first
        # may not be flushed yet: looking it up would miss it and add another.
        if first != price:
            ctx.result.warn(DUPLICATE_PRICE_IGNORED)
        return
    ctx.prices[(security.id, day)] = price
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


@dataclass(frozen=True)
class _Recorded:
    """An ``investment_txn`` already recorded for the account (near-duplicate check)."""

    id: uuid.UUID
    trade_date: date
    settle_date: date | None
    document_id: uuid.UUID | None


def _near_key(
    kind: str, security_id: uuid.UUID | None, quantity: Decimal | None, amount: Decimal
) -> tuple[str, uuid.UUID | None, str, str]:
    return (
        kind,
        security_id,
        _units(quantity) if quantity is not None else "",
        f"{to_storage(amount):f}",
    )


def _recorded(ctx: _Context, account: Account) -> dict[tuple[Any, ...], list[_Recorded]]:
    """The account's recorded rows from other documents, by type, security, units and amount."""
    rows = ctx.db.execute(
        select(
            InvestmentTxn.id,
            InvestmentTxn.type,
            InvestmentTxn.security_id,
            InvestmentTxn.quantity,
            InvestmentTxn.amount,
            InvestmentTxn.trade_date,
            InvestmentTxn.settle_date,
            InvestmentTxn.source_document_id,
        ).where(
            InvestmentTxn.household_id == ctx.household_id,
            InvestmentTxn.account_id == account.id,
            InvestmentTxn.type != InvestmentTxnType.SPLIT,
        )
    ).all()
    found: dict[tuple[Any, ...], list[_Recorded]] = defaultdict(list)
    for txn_id, kind, security_id, quantity, amount, trade, settle, document_id in rows:
        if document_id == ctx.document.id:
            continue  # a re-run of this document: its rows match by hash
        key = _near_key(str(kind), security_id, quantity, amount)
        found[key].append(_Recorded(txn_id, trade, settle, document_id))
    return found


def _same_trade(recorded: _Recorded, activity: InvestmentActivity) -> bool:
    """Whether two rows of the same security, units and amount date the same trade.

    One format dates a row by its trade date, another by its settlement date
    (Fidelity's activity CSV by ``Run Date``, its PDF's activity table by
    ``Settlement Date`` alone): the rows share a date, or one of them has no
    settlement date and they are at most :data:`SETTLE_WINDOW_DAYS` apart.
    """
    ours = {activity.trade_date, activity.settle_date} - {None}
    theirs = {recorded.trade_date, recorded.settle_date} - {None}
    if recorded.trade_date == activity.trade_date:
        return False  # the same date: only the hash decides (identical rows are allowed)
    if ours & theirs:
        return True
    gap = abs((recorded.trade_date - activity.trade_date).days)
    return (recorded.settle_date is None or activity.settle_date is None) and (
        gap <= SETTLE_WINDOW_DAYS
    )


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
    quantity, price = activity.quantity, activity.price_per_unit
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
    waiting: Collection[uuid.UUID] = (),
) -> list[InvestmentTxn]:
    """``transfer_in`` rows for units the account's history does not explain.

    The statement's positions are the truth on ``on``. Units held before the
    first imported statement enter as one opening ``transfer_in`` per
    security on the period's first day (cost: the printed cost basis per
    unit, else the price); lots holding more than reported raise a
    ``position_mismatch`` item and are left as they are. ``waiting``: the
    securities whose position and lines wait for a person (:func:`_split`);
    nothing is opened for them or compared with their history. An opening
    whose cost the statement prints for its other positions but not for
    this one (``unknown``) is costed at the price, which shows no gain at
    all, so an ``investment_opening_basis_unknown`` item asks for it (not
    for a position priced at exactly 1: a money market fund's cost is its
    value; and not for a statement that prints no cost basis at all, an OFX
    file or a 529 plan's, which is opened at the price as it always was).
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
    prints_cost = any(position.cost_basis is not None for position, _ in positions)
    unknown_basis: list[InvestmentTxn] = []
    differences: list[dict[str, Any]] = []
    for security_id in sorted(set(reported) | set(held) | set(by_security), key=lambda u: u.bytes):
        if security_id in waiting:
            continue
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
        held_at_end = sample.get(security_id)
        with localcontext(calc_context()):
            if held_at_end is None:
                # Held before the period and gone by its end (transferred or
                # sold out): the price its own rows print, else unknown.
                cost = next((r.price for r in rows if r.price), None) or Decimal(0)
            elif held_at_end.cost_basis is not None and held_at_end.quantity > 0:
                cost = abs(held_at_end.cost_basis) / held_at_end.quantity
            else:
                cost = held_at_end.price_per_unit or Decimal(0)
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
        if (
            prints_cost
            and held_at_end is not None
            and held_at_end.cost_basis is None
            and held_at_end.price_per_unit != Decimal(1)
        ):
            unknown_basis.append(row)
    if unknown_basis:
        ctx.db.flush()
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "investment_opening_basis_unknown",
                "account_id": str(account.id),
                # Opened at the statement's price (not at zero, as an
                # activity export's shortfall is): no gain is shown yet.
                "costed_at": "price",
                "securities": [
                    {
                        "security_id": str(row.security_id),
                        "quantity": _units(row.quantity or Decimal(0)),
                        "trade_date": row.trade_date.isoformat(),
                        "transaction_id": str(row.id),
                    }
                    for row in unknown_basis[:IDS_MAX]
                ],
            },
        )
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


def _snapshot_since(ctx: _Context, account: Account, on: date) -> bool:
    """Whether a positions export already says what the account held on or after ``on``."""
    found = ctx.db.execute(
        select(InvestmentTxn.id)
        .where(
            InvestmentTxn.household_id == ctx.household_id,
            InvestmentTxn.account_id == account.id,
            InvestmentTxn.trade_date >= on,
            InvestmentTxn.memo.in_((SNAPSHOT_IN_MEMO, SNAPSHOT_OUT_MEMO)),
        )
        .limit(1)
    ).scalar_one_or_none()
    return found is not None


@dataclass(frozen=True)
class _Held:
    """What the lot engine holds of one security in an account."""

    units: Decimal
    basis: Decimal
    #: Every open lot was opened by a row of :data:`SYNTHETIC_MEMOS`.
    synthetic: bool


def _held_positions(ctx: _Context, account: Account) -> dict[uuid.UUID, _Held]:
    lots = ctx.db.execute(
        select(Lot.security_id, Lot.remaining_quantity, Lot.cost_per_unit, Lot.opening_txn_id)
        .where(
            Lot.household_id == ctx.household_id,
            Lot.account_id == account.id,
            Lot.remaining_quantity > 0,
        )
        .order_by(Lot.security_id, Lot.open_date, Lot.id)
    ).all()
    openings = {row.opening_txn_id for row in lots if row.opening_txn_id is not None}
    synthetic: set[uuid.UUID] = set()
    if openings:
        synthetic = set(
            ctx.db.execute(
                select(InvestmentTxn.id).where(
                    InvestmentTxn.household_id == ctx.household_id,
                    InvestmentTxn.id.in_(openings),
                    InvestmentTxn.type == InvestmentTxnType.TRANSFER_IN,
                    InvestmentTxn.memo.in_(SYNTHETIC_MEMOS),
                )
            ).scalars()
        )
    units: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    basis: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    own: set[uuid.UUID] = set()
    with localcontext(calc_context()):
        for security_id, remaining, cost_per_unit, opening in lots:
            units[security_id] += remaining
            basis[security_id] += remaining * cost_per_unit
            if opening not in synthetic:
                own.add(security_id)
    # The basis as money (the ``holding`` cache's scale): the cost derived for
    # new units is then free of the lots' per-unit rounding.
    return {
        security_id: _Held(
            units[security_id], to_storage(basis[security_id]), security_id not in own
        )
        for security_id in units
    }


def _snapshot_adjustments(
    ctx: _Context,
    account: Account,
    positions: Sequence[tuple[InvestmentPosition, Security]],
    on: date,
    existing: set[str],
    waiting: Collection[uuid.UUID] = (),
) -> list[InvestmentTxn] | None:
    """The rows that bring the account's holdings to a positions export's (module doc, step 7).

    None: the export is older than activity already recorded and would
    change the account's units (an ``investment_out_of_order`` item says
    so); nothing of it is written. ``waiting``: securities whose position
    waits for a person; they are left as they are.
    """
    held = _held_positions(ctx, account)
    units: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
    costs: dict[uuid.UUID, Decimal | None] = {}
    sample: dict[uuid.UUID, InvestmentPosition] = {}
    securities: dict[uuid.UUID, Security] = {}
    for position, security in positions:
        if position.quantity <= 0:
            continue
        units[security.id] += position.quantity
        sample.setdefault(security.id, position)
        securities[security.id] = security
        known = costs.get(security.id, Decimal(0))
        costs[security.id] = (
            known + abs(position.cost_basis)
            if known is not None and position.cost_basis is not None
            else None
        )
    prints_cost = any(position.cost_basis is not None for position, _ in positions)
    resettable = cost_method(ctx.db, ctx.household_id, account.id) is CostMethod.FIFO
    #: (type, security, units, unit price or None)
    plan: list[tuple[InvestmentTxnType, uuid.UUID, Decimal, Decimal | None]] = []
    unknown_basis: set[uuid.UUID] = set()
    with localcontext(calc_context()):
        for security_id in sorted(set(units) | set(held), key=lambda u: u.bytes):
            if security_id in waiting:
                continue
            have = held.get(security_id, _Held(Decimal(0), Decimal(0), synthetic=True))
            want = units.get(security_id, Decimal(0))
            shown = sample.get(security_id)
            price = shown.price_per_unit if shown is not None else None
            if want <= UNIT_TOLERANCE:
                if have.units > UNIT_TOLERANCE:
                    plan.append((InvestmentTxnType.TRANSFER_OUT, security_id, have.units, None))
                continue
            security = securities[security_id]
            # The printed basis is in the account's currency; a security
            # quoted in another one keeps the basis its history gives it.
            cost = costs.get(security_id) if security.currency == account.currency else None
            delta = want - have.units
            reset = (
                cost is not None and resettable and have.synthetic and have.units > UNIT_TOLERANCE
            )
            if delta > UNIT_TOLERANCE and cost is not None and cost >= have.basis - BASIS_TOLERANCE:
                # More units, and a basis that grew with them.
                unit_cost = max(cost - have.basis, Decimal(0)) / delta
                plan.append((InvestmentTxnType.TRANSFER_IN, security_id, delta, unit_cost))
            elif cost is not None and abs(delta) <= UNIT_TOLERANCE:
                if reset and abs(cost - have.basis) > BASIS_TOLERANCE:
                    plan.append((InvestmentTxnType.TRANSFER_OUT, security_id, have.units, price))
                    plan.append((InvestmentTxnType.TRANSFER_IN, security_id, want, cost / want))
            elif reset and cost is not None:
                # Fewer units, or more units at a lower basis: only the
                # export says what the position costs now.
                plan.append((InvestmentTxnType.TRANSFER_OUT, security_id, have.units, price))
                plan.append((InvestmentTxnType.TRANSFER_IN, security_id, want, cost / want))
            elif delta > UNIT_TOLERANCE:
                plan.append((InvestmentTxnType.TRANSFER_IN, security_id, delta, price))
                if (
                    prints_cost
                    and shown is not None
                    and shown.cost_basis is None
                    and price != Decimal(1)
                ):
                    unknown_basis.add(security_id)
            elif delta < -UNIT_TOLERANCE:
                plan.append((InvestmentTxnType.TRANSFER_OUT, security_id, -delta, price))
    if not plan:
        return []
    if _later_activity(ctx, account, on):
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "investment_out_of_order",
                "account_id": str(account.id),
                "as_of": on.isoformat(),
                "rows": len(plan),
            },
        )
        return None
    rows: list[InvestmentTxn] = []
    asked: list[InvestmentTxn] = []
    for index, (kind, security_id, quantity, unit_price) in enumerate(plan):
        digest = hashlib.sha256(
            _SEP.join(
                (
                    HASH_VERSION,
                    str(account.id),
                    "snapshot",
                    str(ctx.document.id),
                    str(security_id),
                    on.isoformat(),
                    kind.value,
                    _units(quantity),
                    str(index),
                )
            ).encode()
        ).hexdigest()
        existing.add(digest)
        opening = kind is InvestmentTxnType.TRANSFER_IN
        row = InvestmentTxn(
            id=uuid7(),
            household_id=ctx.household_id,
            account_id=account.id,
            security_id=security_id,
            type=kind,
            trade_date=on,
            quantity=quantity.quantize(QUANTUM),
            price=unit_price.quantize(QUANTUM) if unit_price is not None else None,
            amount=Decimal(0),
            fees=Decimal(0),
            currency=account.currency,
            lot_links=[],
            memo=SNAPSHOT_IN_MEMO if opening else SNAPSHOT_OUT_MEMO,
            import_hash=digest,
            source_document_id=ctx.document.id,
        )
        ctx.db.add(row)
        rows.append(row)
        if opening and security_id in unknown_basis:
            asked.append(row)
    if asked:
        ctx.db.flush()
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "investment_opening_basis_unknown",
                "account_id": str(account.id),
                "costed_at": "price",
                "securities": [
                    {
                        "security_id": str(row.security_id),
                        "quantity": _units(row.quantity or Decimal(0)),
                        "trade_date": row.trade_date.isoformat(),
                        "transaction_id": str(row.id),
                    }
                    for row in asked[:IDS_MAX]
                ],
            },
        )
    return rows


def _shortfall_openings(
    ctx: _Context,
    account: Account,
    created: Sequence[InvestmentTxn],
    existing: set[str],
) -> list[InvestmentTxn]:
    """Openings for units an activity-only statement sells before it ever adds them.

    An activity export starts on a date, not with the account's first trade:
    a sale of units bought earlier would make the lot engine refuse the whole
    statement. For each security whose running units (from the lots the
    account already holds) would go below zero, one ``transfer_in`` of the
    shortfall enters on the security's first row date, with no price (its
    cost basis is unknown: the lot opens at zero cost until a person enters
    it). A security with a split among the new rows is left to the engine
    (the shortfall before and after the split differ).
    """
    held = _held_units(ctx, account)
    by_security: dict[uuid.UUID, list[InvestmentTxn]] = defaultdict(list)
    for row in created:
        if row.security_id is not None:
            by_security[row.security_id].append(row)
    openings: list[InvestmentTxn] = []
    for security_id in sorted(by_security, key=lambda u: u.bytes):
        rows = sorted(
            by_security[security_id],
            key=lambda r: (r.trade_date, _DAY_RANK.get(str(r.type), 2), r.id.bytes),
        )
        if any(r.type is InvestmentTxnType.SPLIT for r in rows):
            continue
        units = held.get(security_id, Decimal(0))
        lowest = units
        with localcontext(calc_context()):
            for row in rows:
                if row.type in OPENING_TYPES:
                    units += row.quantity or Decimal(0)
                elif row.type in CLOSING_TYPES:
                    units -= row.quantity or Decimal(0)
                lowest = min(lowest, units)
        if lowest >= -UNIT_TOLERANCE:
            continue
        opened_on = rows[0].trade_date
        digest = hashlib.sha256(
            _SEP.join(
                (
                    HASH_VERSION,
                    str(account.id),
                    "shortfall",
                    str(security_id),
                    opened_on.isoformat(),
                )
            ).encode()
        ).hexdigest()
        if digest in existing:
            continue
        existing.add(digest)
        openings.append(
            InvestmentTxn(
                id=uuid7(),
                household_id=ctx.household_id,
                account_id=account.id,
                security_id=security_id,
                type=InvestmentTxnType.TRANSFER_IN,
                trade_date=opened_on,
                quantity=(-lowest).quantize(QUANTUM),
                price=None,
                amount=Decimal(0),
                fees=Decimal(0),
                currency=account.currency,
                lot_links=[],
                memo=UNKNOWN_BASIS_MEMO,
                import_hash=digest,
                source_document_id=ctx.document.id,
            )
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


class _NameClashError(Exception):
    """Positions that name one security by its name print different prices."""

    def __init__(self, names: list[str]) -> None:
        super().__init__("positions named alike are different securities")
        self.names = names


def _name_clashes(positions: Sequence[tuple[InvestmentPosition, Security]]) -> list[str]:
    """Names of securities that positions printed by name alone take at two prices.

    One security has one price on a statement's date: two positions of the
    same name at different prices are different securities the statement
    tells apart by nothing the importer can read (module doc, step 4).
    """
    prices: dict[uuid.UUID, set[Decimal]] = defaultdict(set)
    by_name: set[uuid.UUID] = set()
    names: dict[uuid.UUID, str] = {}
    for position, security in positions:
        names[security.id] = security.name
        if not position.security.identified:
            by_name.add(security.id)
        price = position.price_per_unit
        if price is not None:
            prices[security.id].add(price.normalize())
    return [names[i] for i in names if i in by_name and len(prices[i]) > 1]


@dataclass
class _Written:
    created: list[InvestmentTxn]
    cash_rows: list[InvestmentActivity]
    positions: list[tuple[InvestmentPosition, Security]]
    duplicates: int
    openings: int
    replayed: int
    #: Rows not written: the account already records the same trade under
    #: another date (ids of the recorded rows).
    possible_duplicates: list[uuid.UUID] = field(default_factory=list)


def _write(
    ctx: _Context,
    statement: InvestmentStatement,
    account: Account,
    on: date | None,
    *,
    waiting: Sequence[SecurityRef] = (),
    had_positions: bool = False,
) -> _Written | None:
    """Securities, activity, openings and the lot replay; None: held back (out of order).

    ``waiting``: securities left out of ``statement`` whole (:func:`_split`);
    ``had_positions``: the statement reports positions, though none may be
    written now.
    """
    db = ctx.db
    # Securities that wait and are already in the catalogue: their history is
    # not compared with a holding that is not being written.
    waiting_ids = {
        found.id for ref in waiting if (found := _find_security(ctx, ref, account)) is not None
    }
    positions = [(p, _security(ctx, p.security, account)) for p in statement.positions]
    clashes = _name_clashes(positions)
    if clashes:
        raise _NameClashError(clashes)
    existing = _existing_hashes(ctx, account)
    recorded = _recorded(ctx, account)
    near: list[uuid.UUID] = []
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
        if activity.type != "split":
            candidates = recorded.get(
                _near_key(activity.type, security_id, activity.quantity, activity.amount), []
            )
            match = next((r for r in candidates if _same_trade(r, activity)), None)
            if match is not None:
                # The same trade from another format (trade vs settlement
                # date): not written twice, a person is told.
                candidates.remove(match)
                near.append(match.id)
                continue
        existing.add(digest)
        created.append(_txn(ctx, account, activity, security_id, digest))
    moved = [r.trade_date for r in created if r.type in UNIT_ROW_TYPES]
    if (
        on is not None
        and created
        and (
            _later_activity(ctx, account, on)
            or (bool(moved) and _snapshot_since(ctx, account, min(moved)))
        )
    ):
        # Its rows predate activity already recorded, which an opening
        # position (or a positions export) may already count: adding them
        # could double units.
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
    if on is not None and is_positions_snapshot(statement):
        # The export is the whole account on that day (module doc, step 7).
        adjusted = _snapshot_adjustments(ctx, account, positions, on, existing, waiting_ids)
        if adjusted is None:
            return None
        openings = adjusted
        db.flush()
    elif on is not None and (statement.positions or had_positions):
        openings = _openings(ctx, account, statement, positions, created, on, existing, waiting_ids)
        db.flush()
    elif not statement.positions and statement.source_format == "csv":
        # An activity export: units sold before it ever bought them were
        # held before its first row (cost basis unknown, a person is told).
        openings = _shortfall_openings(ctx, account, created, existing)
        if openings:
            db.add_all(openings)
            db.flush()
            ctx.review(
                ReviewKind.LOW_CONFIDENCE_FIELD,
                {
                    "reason": "investment_opening_basis_unknown",
                    "account_id": str(account.id),
                    "securities": [
                        {
                            "security_id": str(row.security_id),
                            "quantity": _units(row.quantity or Decimal(0)),
                            "trade_date": row.trade_date.isoformat(),
                            "transaction_id": str(row.id),
                        }
                        for row in openings[:IDS_MAX]
                    ],
                },
            )
    touched = {r.security_id for r in (*created, *openings) if r.security_id is not None}
    touched |= {security.id for _, security in positions}
    for security_id in sorted(touched, key=lambda u: u.bytes):
        replay_position(db, account, security_id, conflict_status=409)
    return _Written(created, cash_rows, positions, duplicates, len(openings), len(touched), near)


def _commit(
    ctx: _Context,
    statement: InvestmentStatement,
    account: Account,
    check: InvestmentReconciliation,
    *,
    accepted: bool = False,
    kept: _Kept | None = None,
    reported: Sequence[InvestmentPosition] = (),
) -> bool:
    """Write one statement; False: held back.

    ``kept``: what :func:`_held` left of it (rows that wait are not in
    ``statement``); ``reported``: every position the statement reports.
    """
    db = ctx.db
    lines_held = kept.lines_held if kept is not None else 0
    positions_held = kept.positions_held if kept is not None else 0
    waiting = kept.waiting if kept is not None else ()
    result = ctx.result
    on = statement.as_of or statement.period_end
    created_before = result.securities_created
    # The lot engine holds no short position (a negative quantity). Such a
    # security is kept out of it whole: its position and the lines moving
    # its units are not written (a short sale would read as selling units
    # never held, a cover as a purchase of new ones); the statement's value
    # still counts it (the snapshot below is taken from every position).
    shorts = [p for p in statement.positions if p.quantity < 0]
    shorted = {key for p in shorts for key in _ref_keys(p.security)}
    writable = statement
    short_lines = 0
    if shorts:
        activity = [
            a
            for a in statement.activity
            if not (
                a.type in UNIT_TYPES and a.security is not None and shorted & _ref_keys(a.security)
            )
        ]
        short_lines = len(statement.activity) - len(activity)
        writable = statement.model_copy(
            update={
                "positions": [p for p in statement.positions if p.quantity >= 0],
                "activity": activity,
            }
        )
    try:
        with db.begin_nested():
            written = _write(
                ctx, writable, account, on, waiting=waiting, had_positions=bool(reported)
            )
    except _NameClashError as exc:
        # Two holdings of one name at different prices: which is which next
        # month cannot be told, so nothing of the statement is kept.
        ctx.forget()
        result.securities_created = created_before
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "investment_same_name_securities",
                "field": "security",
                "account_id": str(account.id),
                "names": exc.names[:IDS_MAX],
            },
        )
        return False
    except ProblemError as exc:
        # The lot engine refused the history (e.g. a sale of more units than
        # held): nothing of the statement is kept.
        ctx.forget()
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
    result.possible_duplicates += len(written.possible_duplicates)
    result.opening_positions += written.openings
    if written.possible_duplicates:
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "investment_possible_duplicate",
                "account_id": str(account.id),
                "rows": len(written.possible_duplicates),
                "duplicate_of": [str(i) for i in written.possible_duplicates[:IDS_MAX]],
            },
        )

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
        cash is None
        and statement.cash_opening is None
        and check.reported_total_value is not None
        and check.positions_total is not None
        and abs(check.reported_total_value - check.positions_total) <= check.tolerance
    ):
        # An account that prints no cash at all and whose account value is
        # its holdings (a 529 plan, a fund account): it holds none.
        cash = Decimal(0)
    # Never from figures that did not add up, even accepted as read (module
    # doc, step 9): the account's value is not taken from them. Cash activity
    # that does not add up leaves the value alone when the statement's own
    # holdings and closing cash are its printed account value.
    if shorts:
        # In the statement's value (and the snapshot below), not among the
        # holdings shown; its trades were left out with it.
        ctx.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "investment_short_position",
                "field": "quantity",
                "account_id": str(account.id),
                "currency": account.currency,
                "as_of": on.isoformat() if on is not None else None,
                # Lines moving the shorted securities' units, left out with them.
                "lines_left_out": short_lines,
                "positions": [
                    {
                        "symbol": p.security.symbol,
                        "name": p.security.name,
                        "quantity": _text(p.quantity),
                        "market_value": _text(position_value(p)),
                        "page": p.page,
                    }
                    for p in shorts[:IDS_MAX]
                ],
            },
        )
    if (
        _value_holds(check, accepted=accepted)
        and not positions_held
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
        before = opening_value(statement, total, check.tolerance)
        if before is not None and all(
            # Nothing else is known of those days (its own, from an earlier
            # run of this document, is written again).
            s.as_of_date == before[0]
            and s.source is BalanceSource.STATEMENT
            and s.source_document_id == ctx.document.id
            for s in accounts_repo.list_snapshots(db, account, before[0], on - timedelta(days=1))
        ):
            # The value the statement opens with, on the day before its
            # period: where the account's history starts when nothing
            # earlier is known (module doc, step 9).
            accounts_repo.upsert_snapshot(
                db,
                account,
                before[0],
                to_storage(before[1]),
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
            **(
                {
                    "accepted_as_read": True,
                    "reconciliation": check.status,
                    "failed": list(check.failed),
                    "snapshot": False,
                }
                if accepted and check.status != "ok"
                else {}
            ),
            **({"lines_held": lines_held} if lines_held else {}),
            **({"positions_held": positions_held} if positions_held else {}),
            **(
                {"lines_left_out": kept.lines_left_out}
                if kept is not None and kept.lines_left_out
                else {}
            ),
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


def opening_value(
    statement: InvestmentStatement, closing: Decimal, tolerance: Decimal
) -> tuple[date, Decimal] | None:
    """``(day before the period, the account value the statement opens with)``.

    From the statement's own account-value summary (``Beginning Account
    Value`` ... ``Ending Account Value``), and only when that summary is
    this account's and adds up: its closing line is ``closing`` (the value
    the import records for the period's end) and its opening line plus the
    lines between is its closing line. None otherwise: a figure that cannot
    be checked is not recorded as a balance.
    """
    if statement.period_start is None or statement.as_of is None:
        return None
    if statement.period_start > statement.as_of:
        return None
    lines = [f for f in statement.figures if f.group == "account_value"]
    first = next((f.amount for f in lines if f.role == "opening"), None)
    last = next((f.amount for f in lines if f.role == "closing"), None)
    between = [
        f.amount for f in lines if f.role == "item" and not f.detail and f.amount is not None
    ]
    if first is None or last is None or not between:
        return None
    with localcontext(calc_context()):
        if abs(last - closing) > tolerance:
            return None
        if abs(first + sum(between, Decimal(0)) - last) > tolerance:
            return None
    return statement.period_start - timedelta(days=1), first


def _value_holds(check: InvestmentReconciliation, *, accepted: bool) -> bool:
    """The statement's holdings and closing cash may be the account's balance.

    Every check held; or (not for a statement accepted as read) the parser
    said where the failure lies (``localise``), only the ``cash`` activity
    check failed and holdings + closing cash is the printed account value.
    """
    if check.status == "ok":
        return True
    return (
        not accepted
        and check.doubtful_activity is not None
        and check.failed == ["cash"]
        and "total_value" in check.checks
    )


def _several_accounts(statements: Sequence[InvestmentStatement]) -> bool:
    """The statements name two or more accounts.

    Different last four digits, or (an activity export whose account
    numbers are masked) different account names.
    """
    if len({s.account.last4 for s in statements if s.account.last4}) > 1:
        return True
    return len(statements) > 1 and len({(s.account.last4, s.account.name) for s in statements}) > 1


def _chosen_takes(
    ctx: _Context, statements: Sequence[InvestmentStatement], account_id: uuid.UUID
) -> set[int]:
    """The statements of a multi-account document that a chosen account may take.

    An account that records its last four digits takes the statements with
    those digits (its choice settles an ambiguous match) and nothing else;
    one that records none takes the one statement no account matched when
    it is the only one. Any other unmatched statement stays unmatched rather
    than land on an account of a different number.
    """
    chosen = accounts_repo.get_account(ctx.db, ctx.household_id, account_id)
    if chosen is None:
        return set()
    if chosen.number_last4 is not None:
        return {
            index
            for index, statement in enumerate(statements)
            if statement.account.last4 == chosen.number_last4
        }
    takes: set[int] = set()
    unmatched = [
        index
        for index, statement in enumerate(statements)
        if match_account(
            ctx.db,
            ctx.household_id,
            last4=statement.account.last4,
            institution=statement.account.institution,
            allowed=ctx.allowed,
        ).account_id
        is None
    ]
    if len(unmatched) == 1:
        takes.add(unmatched[0])
    return takes


def import_investment_statements(
    db: Session,
    document: Document,
    statements: Sequence[InvestmentStatement],
    *,
    allowed: Collection[uuid.UUID] | None,
    review: Review,
    commit_cash: CommitCash,
    account_id: uuid.UUID | None = None,
    statement_index: int | None = None,
    assigned: Mapping[int, uuid.UUID] | None = None,
    allow_overlap: Collection[uuid.UUID] = (),
    answers: StatementAnswers | None = None,
    user_id: uuid.UUID | None = None,
    several: bool = False,
) -> InvestmentImport:
    """Commit the brokerage statements of ``document`` (module doc).

    ``account_id`` is the account a person chose on a re-run; ``statement_index``
    names the one statement it is for (the review item's), else
    :func:`_chosen_takes` decides on a multi-account document.
    ``assigned`` are the accounts the run puts statements on, by index;
    ``allow_overlap`` accounts the run imports into despite an overlapping
    statement (``apply-mapping``); ``answers`` the document's "import
    anyway" / "import as read" answers, by statement key (module doc, steps
    1-3). ``several``: the document also holds bank statements of other
    accounts (a combined statement), so even a
    single brokerage statement finds its account by its own digits.
    """
    ctx = _Context(
        db=db,
        document=document,
        allowed=allowed,
        review=review,
        commit_cash=commit_cash,
        user_id=user_id,
        result=InvestmentImport(),
    )
    single = len(statements) == 1 and not several
    takes: Collection[int] = range(len(statements))
    if account_id is not None and statement_index is not None:
        takes = {statement_index} if 0 <= statement_index < len(statements) else set()
    elif account_id is not None and _several_accounts(statements):
        takes = _chosen_takes(ctx, statements, account_id)
    answered = answers or StatementAnswers()
    for index, statement in enumerate(statements):
        key = statement_key(statement)
        account = _account(
            ctx,
            statement,
            index,
            single=single,
            account_id=account_id if index in takes else None,
            forced=(assigned or {}).get(index) or answered.account("investment", key),
        )
        if account is None:
            continue
        if account.id not in ctx.result.accounts:
            ctx.result.accounts.append(account.id)
        facts = investment_facts(statement)
        verdict = check_overlap(
            db,
            document,
            account,
            facts,
            index=index,
            key=key,
            allow=account.id in allow_overlap or answered.overlap("investment", key, account.id),
            review=review,
        )
        if verdict != "import":
            ctx.result.overlaps += 1
            continue
        check = statement.reconciliation or reconcile_investment(statement)
        accepted = answered.as_read("investment", key, account.id)
        kept = _held(
            ctx,
            statement,
            check,
            account,
            index=index,
            key=key,
            accepted=accepted,
            accepted_lines=answered.lines("investment", key, account.id) if accepted else None,
        )
        if kept is None:
            ctx.result.held += 1
            continue
        if _commit(
            ctx,
            kept.statement,
            account,
            check,
            accepted=accepted,
            kept=kept,
            reported=statement.positions,
        ):
            ctx.result.statements += 1
            if accepted and check.status != "ok":
                ctx.result.accepted_as_read += 1
            record_statement(db, document, account, facts)
        else:
            ctx.result.held += 1
    return ctx.result
