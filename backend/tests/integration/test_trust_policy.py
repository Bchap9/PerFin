# SPDX-License-Identifier: AGPL-3.0-or-later
"""The trust policy end to end: import clean reads, flag quietly (SECURITY.md C12).

upload -> worker ``ingest_document`` -> real sandbox -> document parser ->
structured importer -> ledger, as in ``test_pdf_statement_pipeline.py``.
PDFs come from ``tests/fixtures/synth_trust.py`` (an invented bank).

* A statement that prints nothing to check against (no balance of any kind)
  and that nothing contradicts is a *clean read*: its rows are posted, the
  document is ``processed``, nothing waits in review, the statement record
  says ``unverified`` and no balance snapshot is written.
* One doubt anywhere holds the whole statement as before: a row dated
  outside the period, a line the parser counted as skipped.
* A near duplicate of a row already in the ledger still waits, alone.
* A line the parser skipped on a statement whose balances agree is no
  finding: the count is kept on the document.
* What a record says: ``reconciled`` when the statement's own balances
  agreed, ``unverified`` otherwise (a statement that does not add up, a CSV).
* :func:`clean_read` itself, on hand-built statements.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy import select

from app.auth.principal import apply_rls_context
from app.domain.accounts.models import Account
from app.domain.documents.models import DocumentStatus, IngestJob, IngestStage, ReviewKind
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports.models import StatementRecord
from app.domain.imports.service import CONTRADICTION_WARNINGS, _Run, clean_read
from app.domain.transactions.models import ReviewState
from app.ingestion.intake.upload import receive_upload
from app.ingestion.parsers.structured import ParsedStatement, ParsedTransaction
from app.ingestion.sandbox.protocol import Reconciliation
from tests.fixtures import synth_pdf_statements as synth
from tests.fixtures import synth_trust as trust
from tests.fixtures.imports import run_jobs
from tests.fixtures.synth_statements import csv_bytes, sample_statement
from tests.integration.test_pdf_statement_pipeline import MAX, Env, env

__all__ = ["env"]

pytestmark = pytest.mark.integration

SIX = Decimal("0.000001")


def _account(env: Env) -> Account:
    institution = env.ledger.institution(env.household_id, name=trust.INSTITUTION)
    return env.ledger.account(
        env.household_id,
        number_last4=trust.LAST4,
        type="checking",
        currency=trust.CURRENCY,
        institution_id=institution.id,
    )


def _records(env: Env, document_id: uuid.UUID) -> list[StatementRecord]:
    found: list[StatementRecord] = env.query(
        select(StatementRecord).where(StatementRecord.document_id == document_id)
    )
    return found


def _summary(env: Env, document_id: uuid.UUID) -> dict[str, Any]:
    logs: list[IngestJob] = env.query(
        select(IngestJob).where(
            IngestJob.document_id == document_id, IngestJob.stage == IngestStage.PARSE
        )
    )
    return dict(logs[-1].log)


def _reasons(env: Env, document_id: uuid.UUID) -> set[str]:
    return {str(item.payload.get("reason")) for item in env.open_reviews(document_id)}


def _ledger(env: Env, account: Account) -> list[tuple[Any, Decimal, str | None]]:
    return sorted((t.posted_date, t.amount, t.raw_payee) for t in env.transactions(account))


def _six(rows: list[trust.Row]) -> list[tuple[Any, Decimal, str | None]]:
    return [(day, amount.quantize(SIX), payee) for day, amount, payee in rows]


# ---------------------------------------------------------------------------
# A clean read
# ---------------------------------------------------------------------------


def test_clean_read_without_balances_is_posted_and_marked(env: Env) -> None:
    account = _account(env)
    document_id = env.ingest(trust.no_balances_statement())

    rows = env.transactions(account)
    assert _ledger(env, account) == _six(trust.expected())
    assert [r.review_state for r in rows] == [ReviewState.NONE] * 6
    assert all(r.source_document_id == document_id for r in rows)
    assert env.open_reviews(document_id) == []
    assert env.reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    (record,) = _records(env, document_id)
    assert record.verification == "unverified"
    assert (record.account_id, record.source_format) == (account.id, "pdf")
    # Nothing vouches for a balance: none is written.
    assert env.snapshots(account) == []
    summary = _summary(env, document_id)
    assert summary["unverified_statements"] == 1
    assert (summary["imported"], summary["review_items"], summary["snapshots"]) == (6, 0, 0)


def test_a_near_duplicate_of_a_clean_read_still_waits(env: Env) -> None:
    account = _account(env)
    day, amount, _ = trust.ROWS[3]
    manual = env.ledger.transaction(
        account, amount=amount, posted_date=day, raw_payee="Typed by hand"
    )
    document_id = env.ingest(trust.no_balances_statement())

    imported = [r for r in env.transactions(account) if r.id != manual.id]
    assert len(imported) == 6
    pending = [r for r in imported if r.review_state is ReviewState.PENDING]
    assert [(r.posted_date, r.raw_payee) for r in pending] == [(day, trust.ROWS[3][2])]
    (item,) = env.open_reviews(document_id)
    assert (item.kind, item.payload["reason"]) == (ReviewKind.POSSIBLE_DUPLICATE, "near_match")
    assert item.payload["transaction_id"] == str(pending[0].id)
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert env.snapshots(account) == []


# ---------------------------------------------------------------------------
# One doubt holds the whole statement
# ---------------------------------------------------------------------------


def test_a_row_outside_the_period_holds_the_whole_statement(env: Env) -> None:
    account = _account(env)
    document_id = env.ingest(trust.no_balances_with_a_stray_row())

    rows = env.transactions(account)
    assert _ledger(env, account) == _six(trust.expected(stray=True))
    assert [r.review_state for r in rows] == [ReviewState.PENDING] * 7
    assert "reconciliation_unavailable" in _reasons(env, document_id)
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (record,) = _records(env, document_id)
    assert record.verification == "unverified"
    assert env.snapshots(account) == []


def test_an_unread_line_holds_the_whole_statement(env: Env) -> None:
    account = _account(env)
    document_id = env.ingest(trust.no_balances_with_an_unread_line())

    rows = env.transactions(account)
    assert _ledger(env, account) == _six(trust.expected())
    assert [r.review_state for r in rows] == [ReviewState.PENDING] * 6
    reasons = _reasons(env, document_id)
    assert {"reconciliation_unavailable", "lines_skipped"} <= reasons
    skipped = next(
        i for i in env.open_reviews(document_id) if i.payload["reason"] == "lines_skipped"
    )
    assert skipped.payload["count"] == 1
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (record,) = _records(env, document_id)
    assert record.verification == "unverified"
    assert env.snapshots(account) == []


# ---------------------------------------------------------------------------
# A skipped line on a statement that reconciles; what a record says
# ---------------------------------------------------------------------------


def test_reconciled_statement_with_an_unread_line_is_processed(env: Env) -> None:
    account = _account(env)
    document_id = env.ingest(trust.balanced_with_an_unread_line())

    assert env.open_reviews(document_id) == []
    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED
    assert document.confidence is not None
    assert document.confidence["parse"]["skipped_lines"] == 1
    assert document.confidence["parse"]["reconciliation"] == ["ok"]
    rows = env.transactions(account)
    assert _ledger(env, account) == _six(trust.expected())
    assert all(r.review_state is ReviewState.NONE for r in rows)
    (record,) = _records(env, document_id)
    assert record.verification == "reconciled"
    assert [s.balance for s in env.snapshots(account)] == [trust.CLOSING]
    summary = _summary(env, document_id)
    assert summary["unverified_statements"] == 0
    # The count stays in the stage facts.
    assert summary["skipped_lines"] == 1


def test_reconciled_statement_is_recorded_reconciled(env: Env) -> None:
    stmt = synth.generic_statement(seed=42)
    account = env.account(stmt, with_institution=False)
    document_id = env.ingest(synth.generic_pdf(stmt))

    (record,) = _records(env, document_id)
    assert record.verification == "reconciled"
    assert len(env.snapshots(account)) == 1
    assert _summary(env, document_id)["unverified_statements"] == 0


def test_statement_that_does_not_add_up_is_still_held(env: Env) -> None:
    stmt = synth.sample_national_statement(seed=41)
    account = env.account(stmt)
    document_id = env.ingest(synth.sample_national_pdf(stmt, tamper="extra_row"))

    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    rows = env.transactions(account)
    pending = [r for r in rows if r.review_state is ReviewState.PENDING]
    assert [r.raw_payee for r in pending] == ["Forged Payee"]
    assert "reconciliation_failed" in _reasons(env, document_id)
    (record,) = _records(env, document_id)
    assert record.verification == "unverified"
    assert env.snapshots(account) == []
    assert _summary(env, document_id)["unverified_statements"] == 1


def test_csv_statement_is_unverified_without_a_marker_item(env: Env) -> None:
    stmt = sample_statement(77)
    account = env.account(stmt)
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        received = receive_upload(
            db,
            env.principal(),
            store=env.store,
            keyring=env.keyring,
            chunks=iter([csv_bytes(stmt, "us_signed")]),
            max_bytes=MAX,
            filename="export.csv",
            account_id=account.id,
        )
    assert run_jobs(env.factories, env.household_id, INGEST_JOB_TYPE, done=env.done)
    document_id = received.document_id

    assert len(env.transactions(account)) == len(stmt.transactions)
    (record,) = _records(env, document_id)
    assert (record.verification, record.source_format) == ("unverified", "csv")
    # Nothing is raised because of it: a CSV carries nothing to reconcile.
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    # The marker is for documents (PDF, image) only.
    assert _summary(env, document_id)["unverified_statements"] == 0


# ---------------------------------------------------------------------------
# clean_read
# ---------------------------------------------------------------------------


def _run(*, warnings: tuple[str, ...] = (), skipped: int = 0) -> _Run:
    # ``clean_read`` reads the parse facts of the run and nothing else.
    return _Run(
        db=cast(Any, None),
        document=cast(Any, None),
        allowed=None,
        user_id=None,
        parse_skipped=skipped,
        parse_warnings=warnings,
    )


def _parsed(*low: tuple[str, ...], source_format: str = "pdf", **kwargs: Any) -> ParsedStatement:
    return ParsedStatement(
        source_format=source_format,
        transactions=[
            ParsedTransaction(
                posted_date=trust.ROWS[i][0],
                amount=trust.ROWS[i][1],
                raw_payee=trust.ROWS[i][2],
                low_confidence=fields,
            )
            for i, fields in enumerate(low)
        ],
        reconciliation=kwargs.pop(
            "reconciliation",
            Reconciliation(status="unavailable", total=Decimal("0"), tolerance=Decimal("0.01")),
        ),
        **kwargs,
    )


UNCHECKED = ("reconciliation",)


def test_contradiction_warning_is_never_a_clean_read() -> None:
    clean = _parsed(UNCHECKED, UNCHECKED, ())
    assert clean_read(_run(), clean) is True
    # Warnings that say nothing against the rows do not count.
    assert clean_read(_run(warnings=("period_from_classifier",)), clean) is True

    named = {"summary_totals_mismatch", "section_total_mismatch", "rows_outside_account"}
    assert named == CONTRADICTION_WARNINGS
    for warning in sorted(CONTRADICTION_WARNINGS):
        assert clean_read(_run(warnings=("period_from_classifier", warning)), clean) is False
    assert clean_read(_run(skipped=1), clean) is False

    assert clean_read(_run(), _parsed(UNCHECKED, ("posted_date",))) is False
    assert clean_read(_run(), _parsed(UNCHECKED, ("posted_date", "reconciliation"))) is False
    assert clean_read(_run(), _parsed()) is False
    assert clean_read(_run(), _parsed(UNCHECKED, source_format="ofx")) is False
    assert clean_read(_run(), _parsed(UNCHECKED, source_format="csv")) is False
    assert clean_read(_run(), _parsed(UNCHECKED, source_format="image")) is True

    # Only a statement that cannot be checked: one that failed its check, one
    # that passed and one that was never checked are not clean reads.
    for status in ("mismatch", "ok"):
        check = Reconciliation(
            status=cast(Any, status), total=Decimal("0"), tolerance=Decimal("0.01")
        )
        assert clean_read(_run(), _parsed(UNCHECKED, reconciliation=check)) is False
    assert clean_read(_run(), _parsed(UNCHECKED, reconciliation=None)) is False

    assert clean_read(_run(), _parsed(UNCHECKED, skipped_rows=[4])) is False
    assert clean_read(_run(), _parsed(UNCHECKED, balance_mismatches=[2])) is False
    assert clean_read(_run(), _parsed(UNCHECKED, truncated=True)) is False
    assert clean_read(_run(), _parsed(UNCHECKED, date_order_assumed=True)) is False
