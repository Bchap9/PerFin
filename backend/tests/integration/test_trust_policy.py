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
* One finding per cause: rows in doubt only because their statement does not
  add up are named by the reconciliation item alone; a row with a doubt of
  its own (a date outside the period) is still listed, with that field; a
  statement whose balances agree while its summary's totals do not says so
  (``cause``); two or more near duplicates of one statement are one item.
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import select

from app.auth.principal import apply_rls_context
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
from app.domain.documents.models import (
    Document,
    DocumentStatus,
    IngestJob,
    IngestStage,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports.models import StatementRecord
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE
from app.domain.imports.service import (
    CONTRADICTION_WARNINGS,
    _Run,
    clean_read,
    import_extraction,
    liability_side,
    same_side,
    statement_match,
)
from app.domain.transactions.models import ReviewState
from app.ingestion.intake.upload import receive_upload
from app.ingestion.parsers.structured import ParsedStatement, ParsedTransaction
from app.ingestion.sandbox.protocol import (
    ExtractResult,
    Reconciliation,
    Statement,
    StatementAccount,
)
from tests.fixtures import synth_bank_layouts as bank_layouts
from tests.fixtures import synth_card_statement as card
from tests.fixtures import synth_pdf_statements as synth
from tests.fixtures import synth_trust as trust
from tests.fixtures.factories import AuthedClient
from tests.fixtures.households import API, Actors
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


def test_clean_read_is_not_posted_into_a_card_the_parser_did_not_read_as_one(env: Env) -> None:
    """The page says "Account statement"; its digits are a credit card's.

    The parser read a bank statement (no account type, amounts as printed).
    On a liability account nothing proves what a positive amount means, so
    the read is not clean: every row waits and the question is asked.
    """
    card_account = env.ledger.account(
        env.household_id, number_last4=trust.LAST4, type="credit_card", currency=trust.CURRENCY
    )
    document_id = env.ingest(trust.no_balances_statement())

    stored = ExtractResult.model_validate(env.document(document_id).extraction)
    assert stored.statements[0].account.account_type is None
    rows = env.transactions(card_account)
    assert len(rows) == 6
    assert [r.review_state for r in rows] == [ReviewState.PENDING] * 6
    assert _reasons(env, document_id) == {"reconciliation_unavailable"}
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    (record,) = _records(env, document_id)
    assert record.verification == "unverified"
    assert env.snapshots(card_account) == []


def test_the_side_a_statement_was_read_as_must_be_the_accounts() -> None:
    checking, savings, card_type = (
        AccountType.CHECKING,
        AccountType.SAVINGS,
        AccountType.CREDIT_CARD,
    )
    assert liability_side(None) is None
    assert liability_side("checking") is False
    assert liability_side("creditcard") is True
    assert liability_side("credit_card") is True
    assert liability_side("creditline") is True
    assert liability_side("LINE_OF_CREDIT") is True
    # A statement that names no type was read as money in positive: an asset.
    assert same_side(checking, None) is True
    assert same_side(savings, "checking") is True
    assert same_side(card_type, None) is False
    assert same_side(AccountType.LOAN, None) is False
    assert same_side(card_type, "creditcard") is True
    assert same_side(checking, "creditcard") is False


def test_a_row_in_another_currency_is_never_a_clean_read(env: Env) -> None:
    """One row left out for its currency: the rest of the statement waits too."""
    document_id = env.ingest(trust.no_balances_statement())
    assert _reasons(env, document_id) == {"account_not_found"}
    account = _account(env)
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        document = db.get(Document, document_id)
        assert document is not None
        assert document.extraction is not None
        stored = copy.deepcopy(document.extraction)
        stored["statements"][0]["transactions"][2]["currency"] = "EUR"
        result = ExtractResult.model_validate(stored)
        import_extraction(db, document, result, user_id=env.owner.user.id)

    rows = env.transactions(account)
    assert len(rows) == 5
    assert [r.review_state for r in rows] == [ReviewState.PENDING] * 5
    assert {"currency_mismatch", "reconciliation_unavailable"} <= _reasons(env, document_id)
    assert env.snapshots(account) == []


# ---------------------------------------------------------------------------
# A few days outside the period is no doubt; further out is (import 1d)
# ---------------------------------------------------------------------------


def test_a_row_a_few_days_outside_the_period_is_still_a_clean_read(env: Env) -> None:
    """A purchase of the last days before the cycle, printed on this statement."""
    account = _account(env)
    document_id = env.ingest(trust.no_balances_row_five_days_early())

    rows = env.transactions(account)
    assert _ledger(env, account) == _six(trust.expected(early=True))
    assert [r.review_state for r in rows] == [ReviewState.NONE] * 7
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    # The marker: imported, a document format, and not checked against a balance.
    (record,) = _records(env, document_id)
    assert (record.verification, record.source_format) == ("unverified", "pdf")
    assert (record.period_start, record.period_end) == (trust.PERIOD_START, trust.PERIOD_END)
    assert _summary(env, document_id)["unverified_statements"] == 1
    assert env.snapshots(account) == []


def test_a_row_forty_days_outside_is_not(env: Env) -> None:
    account = _account(env)
    document_id = env.ingest(trust.no_balances_with_a_stray_row())

    rows = env.transactions(account)
    assert len(rows) == 7
    assert [r.review_state for r in rows] == [ReviewState.PENDING] * 7
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    listed = [
        i
        for i in env.open_reviews(document_id)
        if i.payload.get("reason") not in ("reconciliation_unavailable",)
    ]
    # The stray row is named with its own doubt: its date.
    stray = next(r for r in rows if r.posted_date == trust.STRAY[0])
    assert any(str(stray.id) in str(i.payload) and "posted_date" in str(i.payload) for i in listed)
    assert env.snapshots(account) == []


def _section_account(env: Env) -> Account:
    institution = env.ledger.institution(env.household_id, name=bank_layouts.SECTION_BANK)
    return env.ledger.account(
        env.household_id,
        number_last4=bank_layouts.SECTION_LAST4,
        type="checking",
        currency="USD",
        institution_id=institution.id,
    )


@pytest.mark.parametrize(
    ("build", "account_of", "count", "reason"),
    [
        (trust.no_balances_with_a_stray_row, _account, 7, "reconciliation_unavailable"),
        (trust.no_balances_with_an_unread_line, _account, 6, "lines_skipped"),
        (bank_layouts.section_total_off_without_balances, _section_account, 8, None),
    ],
    ids=["stray-row", "unread-line", "section-total"],
)
def test_not_quite_clean_statements_post_nothing(
    env: Env,
    build: Callable[[], bytes],
    account_of: Callable[[Env], Account],
    count: int,
    reason: str | None,
) -> None:
    """Review Focus 1: no balance to check and one doubt: no row is posted, no balance kept."""
    account = account_of(env)
    document_id = env.ingest(build())

    rows = env.transactions(account)
    assert len(rows) == count
    assert [r for r in rows if r.review_state is ReviewState.NONE] == []
    assert env.snapshots(account) == []
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert {r.verification for r in _records(env, document_id)} == {"unverified"}
    reasons = _reasons(env, document_id)
    if reason is None:
        # The withdrawals printed do not add up to the total printed under them.
        stored = ExtractResult.model_validate(env.document(document_id).extraction)
        assert stored.parse is not None
        assert "section_total_mismatch" in stored.parse.warnings
        assert reasons & {"reconciliation_failed", "reconciliation_unavailable"}
    else:
        assert reason in reasons


# ---------------------------------------------------------------------------
# A statement that prints no account digits
# ---------------------------------------------------------------------------


def test_statement_without_digits_lands_on_the_sole_account_of_its_institution(env: Env) -> None:
    institution = env.ledger.institution(env.household_id, name=trust.INSTITUTION)
    account = env.ledger.account(
        env.household_id,
        number_last4="6058",
        type="checking",
        currency=trust.CURRENCY,
        institution_id=institution.id,
    )
    document_id = env.ingest(trust.no_balances_without_digits())

    document = env.document(document_id)
    assert document.institution_id == institution.id
    stored = ExtractResult.model_validate(document.extraction)
    assert stored.statements[0].account.last4 is None
    assert _ledger(env, account) == _six(trust.expected())
    assert env.open_reviews(document_id) == []
    assert document.status is DocumentStatus.PROCESSED


@pytest.mark.parametrize("accounts", [("credit_card",), ("checking", "savings")])
def test_statement_without_digits_is_asked_about_when_no_one_account_fits(
    env: Env, accounts: tuple[str, ...]
) -> None:
    """The bank's only account is a card, or it has two: a person says which."""
    institution = env.ledger.institution(env.household_id, name=trust.INSTITUTION)
    made = [
        env.ledger.account(
            env.household_id,
            number_last4=f"60{index}8",
            type=kind,
            currency=trust.CURRENCY,
            institution_id=institution.id,
        )
        for index, kind in enumerate(accounts)
    ]
    document_id = env.ingest(trust.no_balances_without_digits())

    assert all(env.transactions(account) == [] for account in made)
    (item,) = env.open_reviews(document_id)
    assert item.kind is ReviewKind.UNKNOWN_ACCOUNT
    assert item.payload["reason"] == "account_ambiguous"
    assert sorted(item.payload["candidates"]) == sorted(str(a.id) for a in made)


def _digitless(account_type: str | None, count: int = 1) -> ExtractResult:
    statement = Statement(
        source_format="pdf",
        account=StatementAccount(account_type=account_type),
        transactions=[],
    )
    return ExtractResult(kind="pdf", ok=True, statements=[statement] * count)


def test_a_card_statement_without_digits_stays_off_the_banks_checking_account(env: Env) -> None:
    institution = env.ledger.institution(env.household_id, name=trust.INSTITUTION)
    checking = env.ledger.account(
        env.household_id, number_last4="6058", type="checking", institution_id=institution.id
    )

    def match(result: ExtractResult, institution_id: uuid.UUID | None) -> uuid.UUID | None:
        with env.factories.transaction() as db:
            set_context(db, env.household_id)
            found = statement_match(
                db, env.household_id, result, 0, allowed=None, institution_id=institution_id
            )
            return found.account_id

    assert match(_digitless("creditcard"), institution.id) is None
    assert match(_digitless(None), institution.id) == checking.id
    assert match(_digitless("checking"), institution.id) == checking.id
    # Without the document's institution nothing is matched, as before.
    assert match(_digitless(None), None) is None
    # Two statements without digits in one document: which is whose is not known.
    assert match(_digitless(None, 2), institution.id) is None
    card_account = env.ledger.account(
        env.household_id, number_last4="6059", type="credit_card", institution_id=institution.id
    )
    assert match(_digitless("creditcard"), institution.id) == card_account.id
    # A statement that names no type could be either's: a person says which.
    assert match(_digitless(None), institution.id) is None
    assert match(_digitless("checking"), institution.id) == checking.id


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
# One finding per cause
# ---------------------------------------------------------------------------


def test_rows_in_doubt_only_for_reconciliation_are_not_listed_twice(env: Env) -> None:
    account = _account(env)
    document_id = env.ingest(trust.closing_that_disagrees())

    rows = env.transactions(account)
    assert _ledger(env, account) == _six(trust.expected())
    # Every row still waits: the statement does not add up.
    assert [r.review_state for r in rows] == [ReviewState.PENDING] * 6
    (item,) = env.open_reviews(document_id)
    assert (item.kind, item.payload["reason"]) == (
        ReviewKind.LOW_CONFIDENCE_FIELD,
        "reconciliation_failed",
    )
    assert item.payload["difference"] == f"{-trust.OFF:f}"
    assert (item.payload["rows_in_doubt"], item.payload["rows_total"]) == (6, 6)
    assert (item.payload["statement"], item.payload["lines_skipped"]) == (0, 0)
    # The statement's reference (a digest in letters, never a digit); a
    # difference that is not zero has no other cause to name.
    ref = item.payload["statement_ref"]
    assert len(ref) == 64
    assert ref.isalpha()
    assert "cause" not in item.payload
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert env.snapshots(account) == []
    assert _summary(env, document_id)["low_confidence"] == 0


def test_a_guessed_date_is_still_listed(env: Env) -> None:
    account = _account(env)
    document_id = env.ingest(trust.stray_row_against_the_balances())

    rows = env.transactions(account)
    stray = next(r for r in rows if r.posted_date == trust.STRAY[0])
    # The statement is off by exactly this row: it alone is in doubt.
    assert [r.id for r in rows if r.review_state is ReviewState.PENDING] == [stray.id]
    items = env.open_reviews(document_id)
    assert sorted(str(i.payload["reason"]) for i in items) == [
        "low_confidence",
        "reconciliation_failed",
    ]
    low = next(i for i in items if i.payload["reason"] == "low_confidence")
    assert (low.payload["count"], low.payload["fields"]) == (1, ["posted_date"])
    (entry,) = low.payload["transactions"]
    assert (entry["transaction_id"], entry["fields"]) == (str(stray.id), ["posted_date"])
    failed = next(i for i in items if i.payload["reason"] == "reconciliation_failed")
    assert (failed.payload["rows_in_doubt"], failed.payload["rows_total"]) == (1, 7)
    assert failed.payload["difference"] == f"{trust.STRAY[1]:f}"
    assert _summary(env, document_id)["low_confidence"] == 1


def test_totals_agree_but_the_summary_does_not_names_its_cause(env: Env) -> None:
    base = card.card_statement()
    stmt = card.CardStatement(
        statement_date=base.statement_date,
        previous_balance=base.previous_balance,
        rows=base.rows,
        summary_shift=Decimal("100.00"),
    )
    account = env.ledger.account(
        env.household_id, number_last4=stmt.last4, type="credit_card", currency="CAD"
    )
    document_id = env.ingest(card.card_pdf(stmt))

    rows = env.transactions(account)
    assert rows
    assert all(r.review_state is ReviewState.PENDING for r in rows)
    (item,) = env.open_reviews(document_id)
    assert item.payload["reason"] == "reconciliation_failed"
    # Previous + rows = new holds; the summary's own totals do not.
    assert Decimal(item.payload["difference"]) == 0
    assert item.payload["cause"] == "summary_totals_mismatch"
    assert (item.payload["statement"], item.payload["lines_skipped"]) == (0, 0)
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert env.snapshots(account) == []


def test_several_near_duplicates_of_a_clean_read_are_one_item(env: Env) -> None:
    account = _account(env)
    typed = [
        env.ledger.transaction(account, amount=amount, posted_date=day, raw_payee="Typed by hand")
        for day, amount, _ in trust.ROWS[1:4]
    ]
    document_id = env.ingest(trust.no_balances_statement())

    imported = [r for r in env.transactions(account) if r.id not in {t.id for t in typed}]
    assert len(imported) == 6
    pending = sorted(
        (r for r in imported if r.review_state is ReviewState.PENDING),
        key=lambda r: r.posted_date,
    )
    assert [r.posted_date for r in pending] == [day for day, _, _ in trust.ROWS[1:4]]
    (item,) = env.open_reviews(document_id)
    assert (item.kind, item.payload["reason"]) == (ReviewKind.POSSIBLE_DUPLICATE, "near_matches")
    assert (item.payload["count"], item.payload["statement"]) == (3, 0)
    assert item.payload["account_id"] == str(account.id)
    assert {r["transaction_id"]: r["candidates"] for r in item.payload["rows"]} == {
        str(row.id): [str(mine.id)] for row, mine in zip(pending, typed, strict=True)
    }
    assert "transaction_id" not in item.payload
    assert _summary(env, document_id)["possible_duplicates"] == 3


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


# ---------------------------------------------------------------------------
# Through the API, as a person would (Review Focus 4 and 5)
# ---------------------------------------------------------------------------


@pytest.fixture
def owner(env: Env, settings: Settings, tmp_path: Path) -> Iterator[AuthedClient]:
    """The household's owner, logged in to an app on the pipeline's directories."""
    local = settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )
    with Actors.open(local, env.factories) as actors:
        yield actors.login(env.owner)


def _upload(env: Env, data: bytes, name: str) -> uuid.UUID:
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        received = receive_upload(
            db,
            env.principal(),
            store=env.store,
            keyring=env.keyring,
            chunks=iter([data]),
            max_bytes=MAX,
            filename=name,
            account_id=None,
        )
    assert run_jobs(env.factories, env.household_id, INGEST_JOB_TYPE, done=env.done)
    return received.document_id


def _card_of(client: AuthedClient, document_id: uuid.UUID) -> dict[str, Any]:
    response = client.get(f"{API}/review/cards")
    assert response.status_code == 200, response.text
    found = [c for c in response.json()["cards"] if c["document"]["id"] == str(document_id)]
    # One card per document, whatever it found.
    assert len(found) == 1, found
    card_out: dict[str, Any] = found[0]
    return card_out


def test_mixed_export_holds_only_the_card_and_the_answer_leaves_checking_alone(
    env: Env, owner: AuthedClient
) -> None:
    """Review Focus 5: one export, a checking account and a card, one signed column."""
    checking = env.ledger.account(
        env.household_id, type=AccountType.CHECKING, number_last4=trust.LAST4
    )
    card_account = env.ledger.account(
        env.household_id, type=AccountType.CREDIT_CARD, number_last4=trust.CSV_CARD_LAST4
    )
    document_id = _upload(env, trust.mixed_sign_accounts_csv(), "all.csv")

    before = {t.id: (t.posted_date, t.amount) for t in env.transactions(checking)}
    assert sorted(before.values()) == sorted(
        (day, amount.quantize(SIX)) for day, amount, _ in trust.CSV_CHECKING
    )
    assert env.transactions(card_account) == []
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    shown = _card_of(owner, document_id)
    assert shown["action"] == "answer_sign"
    (item,) = env.open_reviews(document_id)
    assert shown["action_item_id"] == str(item.id)
    assert item.payload["account_id"] == str(card_account.id)

    answered = owner.post(
        f"{API}/review/{item.id}/resolve",
        json={"kind": "unparsed", "action": "answer_sign", "positive_is": "money_out"},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["resolution"]["scope"] == "account"
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)

    # The checking rows are the same rows with the same amounts.
    assert {t.id: (t.posted_date, t.amount) for t in env.transactions(checking)} == before
    card_rows = env.transactions(card_account)
    assert sorted((t.posted_date, t.amount) for t in card_rows) == sorted(
        (day, (-amount).quantize(SIX)) for day, amount, _ in trust.CSV_CARD
    )
    purchases = [t for t in card_rows if "Payment" not in (t.raw_payee or "")]
    assert len(purchases) == 3
    assert all(t.amount < 0 for t in purchases)
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED


@pytest.mark.parametrize(
    ("build", "row", "named"),
    [
        (trust.stray_row_against_the_balances, trust.STRAY, True),
        (trust.early_row_against_the_balances, trust.EARLY, False),
    ],
    ids=["forty-days", "five-days"],
)
def test_only_a_row_beyond_the_grace_is_a_cause_outside_the_period(
    env: Env, owner: AuthedClient, build: Callable[[], bytes], row: trust.Row, named: bool
) -> None:
    """The reconcile view counts a row as outside the period by the parser's ten days."""
    account = _account(env)
    document_id = env.ingest(build())
    extra = next(r for r in env.transactions(account) if r.posted_date == row[0])

    read = owner.get(f"{API}/documents/{document_id}/reconciliation")
    assert read.status_code == 200, read.text
    (statement,) = read.json()["statements"]
    assert statement["status"] == "off"
    assert Decimal(statement["difference"]) == row[1]
    causes = {c["kind"]: c["transaction_ids"] for c in statement["causes"]}
    # Either way the row that explains the difference is named first.
    assert statement["causes"][0]["transaction_ids"] == [str(extra.id)]
    assert ("out_of_period" in causes) is named
    if named:
        assert causes["out_of_period"] == [str(extra.id)]


def _printed(value: Decimal) -> str:
    """A balance as a card statement prints it: owed is positive, thousands grouped."""
    return f"{-value:,.2f}"


def test_card_statement_is_reconciled_from_the_card(env: Env, owner: AuthedClient) -> None:
    """Review Focus 4: a card statement with a row its balances do not account for."""
    stmt = synth.demo_card_statement(seed=58)
    account = env.account(stmt)
    document_id = env.ingest(synth.demo_card_pdf(stmt, tamper="extra_row"))
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert env.snapshots(account) == []
    forged = next(r for r in env.transactions(account) if r.amount == synth.FORGED.amount)
    assert forged.review_state is ReviewState.PENDING

    shown = _card_of(owner, document_id)
    assert shown["action"] == "reconcile"
    read = owner.get(f"{API}/documents/{document_id}/reconciliation")
    assert read.status_code == 200, read.text
    (statement,) = read.json()["statements"]
    assert (statement["status"], statement["printed_sign"]) == ("off", "owed")
    assert Decimal(statement["difference"]) == synth.FORGED.amount
    assert statement["causes"][0]["kind"] == "extra_row"
    assert statement["causes"][0]["transaction_ids"] == [str(forged.id)]
    # Balances are shown as the card prints them: what is owed is positive.
    assert statement["closing"] == f"{-stmt.closing_balance:.2f}"

    refused = owner.post(
        f"{API}/documents/{document_id}/reconciliation/confirm", json={"statement": 0}
    )
    assert refused.status_code == 409, refused.text
    assert owner.delete(f"{API}/transactions/{forged.id}").status_code == 204
    confirmed = owner.post(
        f"{API}/documents/{document_id}/reconciliation/confirm",
        json={
            "statement": 0,
            "opening_balance": _printed(stmt.opening_balance),
            "closing_balance": _printed(stmt.closing_balance),
        },
    )
    assert confirmed.status_code == 200, confirmed.text
    got = confirmed.json()
    assert got["snapshot"] is True
    assert (got["statement"]["status"], got["statement"]["verification"]) == (
        "balanced",
        "reconciled",
    )

    rows = env.transactions(account)
    assert len(rows) == len(stmt.transactions)
    assert ReviewState.PENDING not in {r.review_state for r in rows}
    (snapshot,) = env.snapshots(account)
    assert snapshot.balance == stmt.closing_balance < 0
    assert snapshot.source_document_id == document_id
    assert [r.verification for r in _records(env, document_id)] == ["reconciled"]
    assert env.open_reviews(document_id) == []
    assert env.document(document_id).status is DocumentStatus.PROCESSED
    cards = owner.get(f"{API}/review/cards").json()["cards"]
    assert str(document_id) not in {c["document"]["id"] for c in cards}
