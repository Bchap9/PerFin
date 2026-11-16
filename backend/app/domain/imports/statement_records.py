# SPDX-License-Identifier: AGPL-3.0-or-later
"""The statement-level duplicate guard (INGESTION.md "Match and deduplicate").

Row-level dedupe (``import_hash``) catches a row imported twice; it cannot
tell that a whole statement is already in the ledger when the second copy
reads its rows differently (a PDF and an OFX of the same month, a payee
printed another way, an investment row dated by settlement instead of
trade). So every statement the importer commits leaves a
:class:`~app.domain.imports.models.StatementRecord` (account, document,
period, source format, closing figures), and before a statement is
committed :func:`check_overlap` compares it with the account's records:

* Only a **stated** period counts (:func:`period_stated`): one an OFX/QFX
  file declares (``DTSTART``/``DTEND``) or a document parser read from the
  page with confidence (a printed period, not the span of the rows). A CSV
  export's or QIF file's period is the span of its rows: recorded, never
  compared (overlapping exports are how people download them, and the row
  hashes handle them).
* A statement whose stated period overlaps an **imported**, stated record
  of the same account and kind (bank or brokerage) from **another**
  document by at least half of the shorter of the two periods
  (:data:`MIN_OVERLAP_SHARE`) is held. A few shared days at the edges (a
  statement closing on the 15th and the next one opening on the 15th, a
  download window reaching into last month's statement) are left to the
  row hashes. Two OFX/QFX files are compared by their declared windows
  only: the same ``DTSTART``/``DTEND`` is the same statement downloaded
  again, while different windows (a rolling "last 90 days" download) are
  left to the row hashes; an OFX/QFX against a printed statement is
  compared like any other pair. Held: nothing of it is written and a
  ``possible_duplicate`` review item (``statement_overlap``) names the
  account, the statement, its period and the other documents with theirs.
  The item names the statement's key
  (:func:`app.domain.imports.statement_answers.statement_key`). Resolving
  it: *confirm* ("same statement") records the statement as ``skipped``
  and writes nothing; *reject* ("import anyway") re-runs the import, and
  every later import of the document imports that statement (that key) on
  that account although it overlaps (``statement_answers``).
* A document that already has a record for the account is never held
  again: a re-run (``apply-mapping``, reprocess) of an imported statement
  goes on (the row hashes keep it idempotent) and a ``skipped`` one stays
  skipped, silently, unless the statement was answered "import anyway" on
  the account or a re-run names the account in ``allow_overlap``
  (``apply-mapping``): that imports it, so a "same statement" answer can be
  taken back.

* A re-read may move a statement's period (a first page read another way).
  Its record is keyed by the period start, so the old one would stay as a
  ghost: :func:`drop_stale_records` removes the document's records of an
  account whose period start the run did not record again. The importer
  calls it only for an account all of whose statements the run committed.

Payloads and audit rows carry ids, dates, counts and the statement key (a
digest) only.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final, Literal

from sqlalchemy import and_, case, delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.core.events import emit
from app.core.ids import uuid7
from app.core.money import to_storage
from app.domain.accounts.models import Account, BalanceSnapshot, BalanceSource
from app.domain.documents.models import Document, ReviewKind
from app.domain.imports.models import StatementRecord
from app.ingestion.sandbox.protocol import (
    DOCUMENT_STATEMENT_FORMATS,
    InvestmentStatement,
    Statement,
)

#: A document parser's period confidence from which the period was printed
#: (a period inferred from the rows scores at most ``confidence.INFERRED``).
STATED_PERIOD_CONFIDENCE: Final = 0.9
#: Formats whose period is declared by the file itself.
DECLARED_PERIOD_FORMATS: Final = frozenset({"ofx", "qfx"})
#: Other documents named in one review item.
OTHERS_MAX: Final = 10
REASON: Final = "statement_overlap"
#: Share of the shorter period two statements must have in common to be one.
MIN_OVERLAP_SHARE: Final = 0.5

StatementKind = Literal["bank", "investment"]
Review = Callable[[ReviewKind, dict[str, Any]], None]


def period_stated(
    source_format: str,
    start: date | None,
    end: date | None,
    confidence: Mapping[str, float],
) -> bool:
    """The statement states its period (module doc), rather than spanning its rows."""
    if start is None or end is None:
        return False
    if source_format in DECLARED_PERIOD_FORMATS:
        return True
    if source_format in DOCUMENT_STATEMENT_FORMATS:
        return confidence.get("period", 0.0) >= STATED_PERIOD_CONFIDENCE
    return False


@dataclass(frozen=True)
class StatementFacts:
    """What a record keeps of one statement."""

    kind: StatementKind
    source_format: str
    period_start: date | None
    period_end: date | None
    stated: bool
    closing_balance: Decimal | None = None
    closing_cash: Decimal | None = None
    holdings_value: Decimal | None = None
    total_value: Decimal | None = None

    @property
    def period(self) -> tuple[date, date] | None:
        if self.period_start is None or self.period_end is None:
            return None
        if self.period_start > self.period_end:
            return None
        return self.period_start, self.period_end


def bank_facts(statement: Statement) -> StatementFacts:
    """The facts of a bank, card or loan-like ledger statement (OFX, QIF, PDF)."""
    return StatementFacts(
        kind="bank",
        source_format=statement.source_format,
        period_start=statement.period_start,
        period_end=statement.period_end,
        stated=period_stated(
            statement.source_format,
            statement.period_start,
            statement.period_end,
            statement.confidence,
        ),
        closing_balance=statement.ledger_balance.amount if statement.ledger_balance else None,
    )


def table_facts(
    source_format: str,
    period_start: date | None,
    period_end: date | None,
    closing_balance: Decimal | None,
) -> StatementFacts:
    """The facts of a statement read from a table (its period spans its rows)."""
    return StatementFacts(
        kind="bank",
        source_format=source_format,
        period_start=period_start,
        period_end=period_end,
        stated=False,
        closing_balance=closing_balance,
    )


def investment_facts(statement: InvestmentStatement) -> StatementFacts:
    """The facts of a brokerage statement (OFX, PDF, activity export)."""

    def amount(value: Any) -> Decimal | None:
        return value.amount if value is not None else None

    return StatementFacts(
        kind="investment",
        source_format=statement.source_format,
        period_start=statement.period_start,
        period_end=statement.period_end,
        stated=period_stated(
            statement.source_format,
            statement.period_start,
            statement.period_end,
            statement.confidence,
        ),
        closing_cash=amount(statement.cash_closing),
        holdings_value=amount(statement.positions_value),
        total_value=amount(statement.total_value),
    )


def _overlapping(
    db: Session, account: Account, kind: str, start: date, end: date
) -> list[StatementRecord]:
    return list(
        db.execute(
            select(StatementRecord)
            .where(
                StatementRecord.household_id == account.household_id,
                StatementRecord.account_id == account.id,
                StatementRecord.kind == kind,
                StatementRecord.period_start <= end,
                StatementRecord.period_end >= start,
            )
            .order_by(StatementRecord.period_start, StatementRecord.id)
        ).scalars()
    )


def _same_period(start: date, end: date, record: StatementRecord) -> bool:
    """The two periods share at least :data:`MIN_OVERLAP_SHARE` of the shorter one."""
    shared = (min(end, record.period_end) - max(start, record.period_start)).days + 1
    shorter = min((end - start).days, (record.period_end - record.period_start).days) + 1
    return shared > 0 and shared >= MIN_OVERLAP_SHARE * shorter


def _same_statement(source_format: str, start: date, end: date, record: StatementRecord) -> bool:
    """Whether ``start .. end`` is the statement ``record`` holds (module doc).

    Two OFX/QFX downloads are one statement only when they declare the same
    window: a bank's "last 90 days" export overlaps the previous one by
    design, and its rows are recognised by their hashes.
    """
    if source_format in DECLARED_PERIOD_FORMATS and record.source_format in (
        DECLARED_PERIOD_FORMATS
    ):
        return (start, end) == (record.period_start, record.period_end)
    return _same_period(start, end, record)


def check_overlap(
    db: Session,
    document: Document,
    account: Account,
    facts: StatementFacts,
    *,
    index: int | None,
    key: str | None,
    allow: bool,
    review: Review,
) -> Literal["import", "held", "skipped"]:
    """Whether to commit the statement (module doc); ``held`` raised a review item.

    ``key`` is the statement's key (``statement_answers``) and ``allow``
    whether the person said to import this statement into this account
    anyway (an answer, or ``allow_overlap`` on the re-run).
    """
    period = facts.period
    if period is None or not facts.stated:
        return "import"
    if allow:
        # "Import anyway", also after an earlier "same statement".
        return "import"
    start, end = period
    records = _overlapping(db, account, facts.kind, start, end)
    own = [r for r in records if r.document_id == document.id]
    if any(r.outcome == "skipped" for r in own):
        return "skipped"
    if own:
        return "import"
    others = [
        r
        for r in records
        if r.outcome == "imported"
        and r.period_stated
        and r.document_id != document.id
        and _same_statement(facts.source_format, start, end, r)
    ]
    if not others:
        return "import"
    first = others[0]
    review(
        ReviewKind.POSSIBLE_DUPLICATE,
        {
            "reason": REASON,
            "account_id": str(account.id),
            "statement": index,
            "statement_key": key,
            "investment": facts.kind == "investment",
            "source_format": facts.source_format,
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "other_document_id": str(first.document_id),
            "other_period_start": first.period_start.isoformat(),
            "other_period_end": first.period_end.isoformat(),
            "others": [
                {
                    "document_id": str(r.document_id),
                    "period_start": r.period_start.isoformat(),
                    "period_end": r.period_end.isoformat(),
                    "source_format": r.source_format,
                }
                for r in others[:OTHERS_MAX]
            ],
        },
    )
    return "held"


def _money(value: Decimal | None) -> Decimal | None:
    return to_storage(value) if value is not None else None


def record_statement(
    db: Session,
    document: Document,
    account: Account,
    facts: StatementFacts,
    *,
    outcome: Literal["imported", "skipped"] = "imported",
    verification: str = "unverified",
    lower_accepted: bool = False,
) -> bool:
    """Record a committed (or skipped) statement; False when it has no period.

    ``verification`` is how far the statement was checked against its own
    balances (``imports.models.STATEMENT_VERIFICATIONS``): what this run
    reached replaces what the record said, with one exception. A record a
    person ``accepted`` is never lowered to ``unverified`` by a caller that
    merely has nothing better to say: only the importer's decision that the
    acceptance no longer covers the statement does that (``lower_accepted``;
    ``imports.service._settle_standing``).
    """
    period = facts.period
    if period is None:
        return False
    values = {
        "household_id": account.household_id,
        "account_id": account.id,
        "document_id": document.id,
        "kind": facts.kind,
        "outcome": outcome,
        "verification": verification,
        "period_start": period[0],
        "period_end": period[1],
        "period_stated": facts.stated,
        "source_format": facts.source_format,
        "currency": account.currency,
        "closing_balance": _money(facts.closing_balance),
        "closing_cash": _money(facts.closing_cash),
        "holdings_value": _money(facts.holdings_value),
        "total_value": _money(facts.total_value),
    }
    statement = insert(StatementRecord).values(id=uuid7(), **values)
    update: dict[str, Any] = {k: statement.excluded[k] for k in values if k != "household_id"}
    update["updated_at"] = func.now()
    if not lower_accepted:
        update["verification"] = case(
            (
                and_(
                    StatementRecord.verification == "accepted",
                    statement.excluded.verification == "unverified",
                ),
                StatementRecord.verification,
            ),
            else_=statement.excluded.verification,
        )
    db.execute(
        statement.on_conflict_do_update(
            constraint="uq_statement_record_account_document_kind_start", set_=update
        )
    )
    return True


def withdraw_snapshot(
    db: Session, document: Document, account: Account, statement: Statement
) -> bool:
    """Take back the balance ``document`` recorded for ``statement``; True when one stood.

    A statement of a document format (PDF, image) that reconciled wrote its
    closing balance as a statement snapshot. When its record no longer says
    ``reconciled`` (accepted without balancing; ``unverified`` again after
    a re-read that does not add up, a row put back or earlier rows
    removed), no balance stands for it: the snapshot of this document,
    account and closing day is removed. So no statement snapshot of a
    document remains for a bank or card statement record of a document
    format that is ``unverified``.

    A balance another document or a person recorded is never touched, and
    neither is the balance a bank file (OFX, QFX, QIF) or a table states:
    such a file declares its balance itself, whatever its rows are.
    """
    closing = statement.ledger_balance
    as_of = (closing.as_of if closing is not None else None) or statement.period_end
    if statement.source_format not in DOCUMENT_STATEMENT_FORMATS or as_of is None:
        return False
    found = db.execute(
        select(BalanceSnapshot).where(
            BalanceSnapshot.household_id == account.household_id,
            BalanceSnapshot.account_id == account.id,
            BalanceSnapshot.source == BalanceSource.STATEMENT,
            BalanceSnapshot.source_document_id == document.id,
            BalanceSnapshot.as_of_date == as_of,
        )
    ).scalar_one_or_none()
    if found is None:
        return False
    db.delete(found)
    db.flush()
    emit(db, account.household_id, "balance.updated", {"account_id": str(account.id)})
    return True


def drop_stale_records(
    db: Session,
    document: Document,
    account: Account,
    kept_periods: Collection[date],
    *,
    kind: StatementKind = "bank",
) -> int:
    """Delete the document's records of ``account`` this run did not record; how many.

    ``kept_periods`` are the period starts the run recorded for the account
    and ``kind``. A record with another start is of an earlier reading of
    the document (module doc); records of other documents, other accounts
    and the other kind are never touched.
    """
    stale = delete(StatementRecord).where(
        StatementRecord.household_id == account.household_id,
        StatementRecord.account_id == account.id,
        StatementRecord.document_id == document.id,
        StatementRecord.kind == kind,
        # A statement answered "same statement" is no reading of the rows.
        StatementRecord.outcome == "imported",
    )
    if kept_periods:
        stale = stale.where(StatementRecord.period_start.not_in(sorted(set(kept_periods))))
    result = db.execute(stale.execution_options(synchronize_session="fetch"))
    return int(getattr(result, "rowcount", 0) or 0)
