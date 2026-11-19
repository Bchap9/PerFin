# SPDX-License-Identifier: AGPL-3.0-or-later
"""Structured imports: column mappings, previews and committing statements.

The flow (INGESTION.md "Parse", "Match and deduplicate", "Commit"):

1. An upload becomes a ``document``; the worker's ``ingest_document`` job
   extracts it in the sandbox and keeps the validated output in
   ``document.extraction``.
2. :func:`import_extraction` (the pipeline's structured importer) turns that
   output into transactions **in the job's database transaction**:

   * **OFX/QFX/QIF**: each statement goes to the account named by its last
     four digits (and institution, ``app.ingestion.match.accounts``), the
     document's account for a single-statement file, or an account given by
     the person re-running the import. No match is an ``unknown_account``
     review item. Statements of one document that carry the same digits
     (two accounts under one masked number) go by the type each names
     (:func:`statement_match`).
   * **CSV/TSV**: the header fingerprint
     (``app.ingestion.parsers.structured.header_fingerprint``) finds a saved
     ``import_mapping``. Without one, the deterministic suggestion
     (``suggest_mapping``) is used when it is confident (every required
     column named exactly by its header, every row before the footer
     readable, the day/month order not a guess, an indicator column only
     direction words beside unsigned amounts), the file is a CSV, TSV or
     XLSX and the person whose document it is may save mappings (member or
     above): the suggestion is saved as a new mapping (audited
     ``import_mapping.create`` with ``origin: suggested``) so the next file
     is recognised, and the run summary says ``mapping_suggested``. The
     sign of a file is proven or the file waits: signed amounts read on
     trust (no debit/credit columns, no indicator, no running balance that
     agrees, no direction words that go with the signs) are not confident
     when the header names a card, no amount is money out or more rows
     are money in than money out (the suggestion's ``sign_unverified``
     doubt; ``auto_import: sign_unverified`` when it is the only one), and
     a sign on trust is never used to write into a liability account (a
     card export may list purchases as positive). Only that account's
     statement waits: the other accounts of a multi-account file are
     imported, and each statement held is one ``mapping_required`` item
     with ``auto_import: sign_unverified``, its ``account_id``, ``group``
     and row count (``transactions``), the ``mapping_id`` when a mapping
     exists, and a ``scope``: ``mapping`` when every statement of the table
     waits (nothing is imported, no mapping is saved; the answer is the
     layout's sign and is saved on its mapping, so a layout is asked about
     once), ``account`` when others were imported (the answer is kept for
     that account of that document, ``statement_answers``: every later run
     reads that account's rows with the answered sign and holds them no
     more; no row of another account changes). A table of securities
     trades (``investment_columns``) and direction words that contradict
     the signs (``indicator_contradicts_sign``) are never confident either. A
     table without a header row is read from the shapes of its columns
     and waits (``no_header``) unless a running balance proves which
     columns hold the money; the mapping saved for it is named by its
     column count, never after its first row. A
     statement whose account is not known yet still gets its own
     ``account_required``/``unknown_account`` item, and the mapping saved
     from a suggestion on trust is marked ``sign_unverified``: whenever it
     later meets a liability account (the re-run after a person picks the
     card, next month's file), that account's statement waits, and with a
     file whose own suggestion doubts the sign every statement does, as
     ``mapping_required`` (``auto_import: sign_unverified``) until a
     person answers for the account, saves the mapping's spec in the
     editor (which clears the mark: a mapping a person saved holds
     nothing, whatever a later file's suggestion doubts) or chooses the
     mapping explicitly for that re-run. With one account-bound mapping among the
     fitting ones, it wins over household mappings for an upload that
     names no account. Anything else is an ``unparsed``
     review item (``mapping_required``) carrying the suggestion and why it
     was not used (``auto_import``), and the table is shown by ``GET
     /imports/{document_id}/preview`` for the mapping editor. The account is
     the one given on re-run, the document's or the mapping's. A table that
     is only a header row, or a header row above nothing but total lines
     (:func:`header_only`), and a PDF or image whose
     page tables hold no dated row (``app.domain.review.layout``), have
     nothing to map: they wait as ``unparsed``/``no_rows`` (``source``:
     ``table`` or ``pages``), which the mapping editor is not offered for.
     Before either, a PDF or image whose pages the receipt, payslip,
     tax-form or loan parser was chosen for and read nothing from
     (``app.domain.review.layout.unread_kind``) waits as ``unparsed`` with
     ``receipt_unreadable``, ``payslip_unreadable``, ``tax_form_unreadable``
     or ``loan_unreadable`` (``parser``, and the parse ``warnings``, which
     say why: ``loan_bill_several_loans`` for a bill listing several
     loans, ``loan_document_too_long`` for a guide or report of many
     pages). Its page tables are never offered to the mapping editor.
   * **Brokerage activity tables** come first: when table 0 of a CSV, TSV
     or XLSX is a brokerage "Activity" export
     (``app.ingestion.parsers.brokerage_table``: a date, an action that
     ``activity_type`` types, an amount, a symbol or quantity column), its
     rows become one investment statement per account (last four digits of
     the account-number column) and go to
     ``app.domain.investments.statement_import`` like an OFX ``INVSTMTRS``:
     buys, sells, reinvestments and income as ``investment_txn`` rows, cash
     deposits and withdrawals as ledger rows on the brokerage account. The
     statements are read again from the stored table on every run (never
     stored). **Precedence**: a mapping chosen explicitly on a re-run
     (``apply-mapping`` with ``mapping_id``, the review queue's "use this
     mapping") imports the table as a cash ledger; otherwise the brokerage
     reading wins over a saved mapping for the same header and over the
     suggestion, and the run summary says so (``brokerage_table`` with the
     counts, ``mapping_bypassed`` with the saved mapping's id), so a person
     who saved a cash-ledger mapping for that header can see why it was not
     used and choose it explicitly. Rows skipped (an action no known phrase
     types, a row that does not read) raise one ``low_confidence_field`` item
     (``lines_skipped``, counts only).
   * **Brokerage positions exports** come before those: when table 0 of a
     CSV, TSV or XLSX is a "Positions" export
     (``app.ingestion.parsers.positions_table``: a symbol, a quantity and a
     market value column, no date or action), its rows become one
     investment statement per account whose positions are what the account
     holds on the day of the download, committed by
     ``app.domain.investments.statement_import`` as a positions snapshot
     (holdings brought to the export's, a price per position, the account's
     value). The run summary carries ``positions_table`` (counts). A
     mapping chosen explicitly on a re-run still reads the table as a cash
     ledger. Rows that do not read raise one ``low_confidence_field`` item
     (``lines_skipped``); their account records no balance.
   * **Multi-account CSV/TSV** (``MappingSpec.account_column``): one
     statement per account (``from_table_grouped``), each matched by the
     last four digits of its account cell exactly like an OFX statement
     (same ``match_account`` call and allowed accounts; a single-account
     file may use the document's or the mapping's account). An account
     given on re-run takes the statement it was chosen for (``group``,
     passed on by the review queue), else the statements with its own last
     four digits when it records them, else the one statement that matches
     nothing when there is only one (:func:`_chosen_groups`): one choice
     never pours several accounts into one. An account that matches none or
     several is an ``unknown_account`` item per statement (``group`` and
     ``spec_digest``, the digest of the spec that numbered the groups; never
     the digits); ``remember`` on it re-reads the group's last four digits
     from the stored table (:func:`stored_group_last4`), and an item whose
     mapping spec has changed since is stale (:func:`group_item_current`).
     The account a person puts one of those statements on is remembered
     for the document by the statement's digest (:func:`_group_key`), so
     the statements of a table split by account *names*, which no digits
     match, can be answered one after the other.
   * Deduplication (``app.ingestion.match.dedupe``): exact ``import_hash``
     matches are skipped, near matches (same date and amount) are imported as
     ``review_state = pending`` with a ``possible_duplicate`` review item:
     ``near_match`` for the one near match of a statement, one
     ``near_matches`` item listing them all (``count``, ``rows``) for two or
     more.
   * The new transactions are handed to the categorisation engine
     (``app.domain.categorisation.engine.categorise_rows``): each gets its
     merchant key; a category that came from the file (QIF ``L``) is the
     file's and protected; the household's rules run; what is still
     undecided takes its payee's default category, what the household
     taught, or the bundled dictionary's answer (a fact, or only a
     suggestion). The engine's own writes leave ``updated_at`` alone and
     the rows are marked untouched last. The closing balance (OFX
     ``LEDGERBAL``, the last running balance of a CSV) becomes a
     ``statement`` balance snapshot. The ingest worker then queues the
     transfer matcher for the document.
   * Statements read by the document parsers (PDF, scans;
     ``app.ingestion.parsers``) go the same way. A row with a field below
     the confidence threshold is written with ``review_state = pending``
     (and its ``confidence``) next to one ``low_confidence_field`` item
     (``low_confidence``: fields, transaction ids, pages); a statement that
     does not reconcile (``app.ingestion.reconcile``) has all its rows
     pending, a ``reconciliation_failed``/``reconciliation_unavailable``
     item with the figures, and writes no balance snapshot (SECURITY.md C12).
     One finding per cause: a row whose only doubt is that its statement did
     not reconcile (``reconciliation``) waits with the others but is named
     by the reconciliation item alone, never listed again in the
     ``low_confidence`` item. That item also carries the statement's index,
     the lines the parser skipped (``lines_skipped``) and, when the balances
     agree with the rows and the document's own summary or a section total
     does not, which of them (``cause``).
     The exception is a **clean read** (:func:`clean_read`): a statement
     that prints nothing to check against (reconciliation ``unavailable``
     and no balance read at all: one balance without the other is a
     doubt), whose period was printed and not inferred from its rows, whose
     rows carry no other doubt and whose document the parser neither
     skipped a line of nor warned about (:data:`CONTRADICTION_WARNINGS`).
     It is imported as posted (a row waits only as a near duplicate), no
     item is raised for it and no snapshot is written; its record says
     ``unverified`` (``statement_record.verification``; ``reconciled`` when
     a statement's own balances agreed) and the run summary counts it
     (``unverified_statements``), which the pages show as "not checked
     against a balance". One doubt anywhere and the whole statement waits.
     So does one that lands on an account of the other side of the balance
     sheet than it was read for (:func:`same_side`: a page not read as a
     card statement whose digits are a credit card's): the sign of its
     amounts is on trust, and a sign on trust is never used to write into
     a liability account unasked.
   * Records the extractor could not read (OFX, QFX and QIF: a record with
     no date or amount, an investment activity of a kind it does not read)
     are counted in its ``transaction_skipped:<n>`` warnings, one per
     statement. Their sum raises one ``low_confidence_field`` item per
     document (``lines_skipped``: ``count``, ``source`` ``extraction``), so
     the document waits in review although the rows that were read are
     imported.
   * Brokerage statements (``result.investments``: OFX ``INVSTMTRS``,
     PDFs read by ``app.ingestion.parsers.brokerage``; brokerage activity
     tables above) go to
     ``app.domain.investments.statement_import`` (securities, activity,
     lots, holdings, prices, snapshot; held back when they do not
     reconcile); their cash deposits and withdrawals come back here as
     ledger rows.
   * **A document read again** (reprocess, a re-run, a taught table): a
     row a person deleted (an import tombstone) is not written again and is
     counted (``tombstoned_rows``). Every row a run writes ends with
     ``updated_at == created_at``, after the rules and payee defaults of
     the import ran: that is "untouched since import". When the run has
     committed every statement of the document for an account
     (:func:`_finish_reading`), the rows this document produced there
     earlier that the new reading no longer holds are removed when nobody
     touched them (no tombstone; ``replaced_rows``), and kept and listed in
     one ``low_confidence_field`` item (``earlier_rows_kept``: the account,
     a count, transaction ids) otherwise (``kept_rows``); audited as
     ``import.replace``. A new row that only resembled a row removed this
     way is no near duplicate; a finding about rows that are all gone is
     dismissed. The statement records of periods the run did not record
     again are dropped. Nothing of this happens for an account a statement
     of the document waits for (an overlap, a sign question, another
     currency), nor for a document with a statement that has no account
     yet or with brokerage statements, nor for an account whose new reading
     says it may be incomplete (:func:`_incomplete`: truncated, or no
     balance vouches for it and a row or a line was skipped, a running
     balance disagrees or the parser found a contradiction): the row it no
     longer holds may be the line it could not read. Rows without an
     ``import_hash`` (added by hand), rows of other documents and rows a
     person touched are never removed here; "touched" includes a row a
     goal contribution or a loan payment was recorded from, which leaves
     the row's timestamps alone. A statement a person **moved to another
     account** (:func:`_settle_moved`): when the run committed every
     statement, an account that still holds rows of the document and got no
     statement of this reading is settled the same way, but only when every
     such row is a line the run committed elsewhere. Rows on an account the
     new reading does not account for (a statement it lost) are left as
     they are, without a finding: nothing says where they went.
   * **Answers given in the reconcile view survive a re-read**
     (:func:`_standing`, :func:`_settle_standing`). A bank statement whose
     record of this document says ``accepted``, or ``reconciled`` although
     its reading does not add up by itself (a person balanced it against
     the ledger), is not asked about again while the run changes no row of
     its account: none written, none removed as an earlier reading, none
     newly kept. Its record keeps what it said, no reconciliation finding
     is raised and no balance is recorded; a ``reconciled`` one must still
     add up against the ledger rows. When the run did change a row the
     statement is what its reading says again (``unverified``, with its
     finding). An accepted statement becomes ``reconciled``, with its
     balance recorded, only when the reading adds up and the ledger rows
     do as well. The finding names the statement by its reference
     (``statement_answers.statement_ref``), and a statement a person put on
     an account is found there again by that reference when its exact key
     no longer matches.
   * The document is ``processed`` when nothing is left open for it in the
     review queue, ``needs_review`` otherwise.

3. A person fixes what was missing (saves a mapping, picks an account) and
   ``POST /imports/{document_id}/apply-mapping`` queues
   ``imports.apply_mapping`` (:func:`run_apply_job`), which runs step 2 again
   on the **stored** extraction (nothing is re-parsed) and resolves the
   review items it answered. Running it again is harmless: every row already
   imported matches its hash, and what an earlier reading left behind is
   settled as above.

Visibility (SECURITY.md F3): statements of a member's upload only match
accounts the member can edit; a mapping tied to an account the caller cannot
see is ``404 import-mapping-not-found``; the account an import writes to must
be editable by the person who asked for it. Audit rows and events carry ids
and counts, never statement text or account numbers (H8).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy import ColumnElement, delete, func, or_, select, true
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.authz import role_at_least
from app.auth.models import HouseholdMember, HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import sum_amounts, to_storage
from app.db.queue import enqueue
from app.domain.accounts import repo as accounts_repo
from app.domain.accounts.balances import balance_as_of
from app.domain.accounts.models import (
    LIABILITY_TYPES,
    Account,
    AccountType,
    BalanceSnapshot,
    BalanceSource,
)
from app.domain.accounts.visibility import get_visible_account, visible_account_filter
from app.domain.categorisation import engine, institution
from app.domain.documents import repo as documents_repo
from app.domain.documents.models import (
    Document,
    DocumentLink,
    DocumentStatus,
    IngestJob,
    IngestStage,
    LinkTarget,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import Verification
from app.domain.documents.service import get_visible_document
from app.domain.goals.models import GoalContribution
from app.domain.imports import repo
from app.domain.imports.models import NAME_MAX, ImportMapping, StatementRecord
from app.domain.imports.payees import PayeeMatcher
from app.domain.imports.schemas import (
    ACTIVITY_COLUMNS,
    APPLY_MAPPING_JOB_TYPE,
    DEFAULT_PAGE,
    PREVIEW_DEFAULT,
    ApplyMapping,
    ApplyMappingJob,
    Assignment,
    ImportAccepted,
    ImportPreview,
    MappingCreate,
    MappingOut,
    MappingPage,
    MappingSpec,
    MappingSuggestionOut,
    MappingUpdate,
    PreviewBalance,
    PreviewStatement,
    PreviewTable,
    PreviewTransaction,
    Sign,
)
from app.domain.imports.statement_answers import (
    REF_FIELD,
    SCOPE_ACCOUNT,
    StatementAnswers,
    document_answers,
    placement,
    statement_key,
    statement_refs,
)
from app.domain.imports.statement_records import (
    StatementFacts,
    bank_facts,
    check_overlap,
    drop_stale_records,
    record_statement,
    table_facts,
    withdraw_snapshot,
)
from app.domain.investments.models import InvestmentTxn
from app.domain.investments.statement_import import import_investment_statements
from app.domain.loans.models import LoanPayment
from app.domain.networth import service as networth
from app.domain.review import repo as review_repo
from app.domain.review.layout import pages_without_rows, unread_kind, with_layout_tables
from app.domain.transactions import repo as transactions_repo
from app.domain.transactions.import_hash import HashInput, assign_import_hashes
from app.domain.transactions.models import (
    MEMO_MAX,
    PAYEE_MAX,
    ReviewState,
    Transaction,
    TransactionStatus,
)
from app.ingestion.match.accounts import (
    AccountMatch,
    importable_account_ids,
    match_account,
    match_shared_number,
    shared_numbers,
)
from app.ingestion.match.dedupe import dedupe
from app.ingestion.parsers.brokerage_table import PARSER_NAME as ACTIVITY_PARSER
from app.ingestion.parsers.brokerage_table import PARSER_VERSION as ACTIVITY_PARSER_VERSION
from app.ingestion.parsers.brokerage_table import TABLE_KINDS as ACTIVITY_TABLE_KINDS
from app.ingestion.parsers.brokerage_table import (
    ActivityTable,
    activity_dates,
    read_activity_table,
    suggest_activity_mapping,
)
from app.ingestion.parsers.confidence import DEFAULT_THRESHOLD
from app.ingestion.parsers.positions_table import PARSER_NAME as POSITIONS_PARSER
from app.ingestion.parsers.positions_table import PARSER_VERSION as POSITIONS_PARSER_VERSION
from app.ingestion.parsers.positions_table import TABLE_KINDS as POSITIONS_TABLE_KINDS
from app.ingestion.parsers.positions_table import PositionsTable, read_positions_table
from app.ingestion.parsers.structured import (
    MappingSuggestion,
    ParsedStatement,
    ParsedTransaction,
    best_date_format,
    candidate_fingerprints,
    from_statement,
    from_table_grouped,
    group_last4,
    header_fingerprint,
    spec_fingerprint,
    suggest_mapping,
)
from app.ingestion.sandbox.protocol import (
    DOCUMENT_STATEMENT_FORMATS,
    ExtractResult,
    InvestmentStatement,
    Reconciliation,
    Statement,
    Table,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: Most row numbers / ids copied into one review item, audit row or event.
IDS_MAX: Final = 50
#: Formats whose opening balance record is the balance before their first
#: row, a transaction until 1.0.0-rc.11 (:func:`_settle_openings`).
OPENING_FORMATS: Final = frozenset({"qif", "csv"})
#: Review reason: a QIF's or table's opening balance was not recorded, as
#: the account already has a balance or rows on or before its day.
OPENING_BALANCE_NOT_RECORDED: Final = "opening_balance_not_recorded"
#: Review kinds a re-run answers (resolved when the re-run goes through).
RERUN_ANSWERS: Final = frozenset({ReviewKind.UNPARSED, ReviewKind.UNKNOWN_ACCOUNT})
#: ``unparsed`` reasons a re-run does not answer: the pipeline raised them
#: about the reading itself (scanned pages OCR never read), the importer
#: never raises them again, and only the person or reading the document
#: again through the pipeline settles them.
PIPELINE_REASONS: Final = frozenset({"ocr_failed"})
#: An extractor's count of the records of one statement it could not read
#: (``app.ingestion.extract.ofx.skipped_warnings``; OFX, QFX and QIF).
#: Extraction output is untrusted: ASCII digits only, and no count longer
#: than a file could hold (anything else is not a count and is ignored).
_SKIPPED_WARNING: Final = re.compile(r"transaction_skipped:([0-9]{1,9})")
#: Statuses whose stored extraction may be (re-)imported.
IMPORTABLE_STATUSES: Final = frozenset({DocumentStatus.NEEDS_REVIEW, DocumentStatus.PROCESSED})
#: Extraction kinds whose table may be imported with a confident suggestion
#: (a PDF/image page table no parser read always waits for a person).
AUTO_MAPPING_KINDS: Final = frozenset({"csv", "tsv", "xlsx"})
#: Longest name given to a mapping saved from a suggestion (the editor's limit).
SUGGESTED_NAME_MAX: Final = 120
#: Parse warnings that say the document's own figures contradict its rows
#: (``table_statement``): a statement of such a document is never a clean
#: read (:func:`clean_read`).
CONTRADICTION_WARNINGS: Final = frozenset(
    {"summary_totals_mismatch", "section_total_mismatch", "rows_outside_account"}
)
#: Parse warnings that say a balance was printed and not used: its sign was
#: open (``Ending Balance - $992.50``) and no row settled it
#: (``table_statement``). A printed balance nothing could be checked
#: against is a doubt: never a clean read (:func:`clean_read`).
UNUSED_BALANCE_WARNINGS: Final = frozenset({"balance_sign_unsettled"})
#: The same, in the order a ``reconciliation_failed`` item names its ``cause``.
_CAUSE_ORDER: Final = ("summary_totals_mismatch", "section_total_mismatch", "rows_outside_account")
#: The one doubt a clean read may carry on a row: nothing to check it against.
#: On any row it is the statement's doubt, not the row's: the reconciliation
#: item speaks for it and the ``low_confidence`` item leaves it out.
UNCHECKED_FIELD: Final = "reconciliation"


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


def mapping_not_found() -> ProblemError:
    return ProblemError(404, "import-mapping-not-found", "Import mapping not found")


def _name_taken() -> ProblemError:
    return ProblemError(
        409, "import-mapping-name-taken", "Name taken", "Another mapping has this name."
    )


def _no_table() -> ProblemError:
    return ProblemError(
        422, "import-no-table", "No table", "The document has no extracted table to map."
    )


def _columns_mismatch(columns: int) -> ProblemError:
    return ProblemError(
        422,
        "import-mapping-mismatch",
        "Mapping does not fit",
        f"The mapping names a column the table does not have (it has {columns}).",
    )


def _not_applicable(detail: str) -> ProblemError:
    return ProblemError(409, "import-not-applicable", "Nothing to import", detail)


def _institution_not_found() -> ProblemError:
    return ProblemError(404, "institution-not-found", "Institution not found")


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or not role_at_least(principal.role, minimum):
        raise mapping_not_found()
    return principal.household_id


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------


def _visible(principal: Principal, need: str = "view") -> ColumnElement[bool]:
    """Mappings without an account, or on an account the principal may ``need``."""
    return or_(
        ImportMapping.account_id.is_(None),
        visible_account_filter(
            principal,
            cast(ColumnElement[uuid.UUID], ImportMapping.account_id),
            need="edit" if need == "edit" else "view",
        ),
    )


def _spec(mapping: ImportMapping) -> MappingSpec | None:
    try:
        return MappingSpec.model_validate(mapping.spec)
    except ValidationError:
        return None


def mapping_out(mapping: ImportMapping) -> MappingOut:
    spec = _spec(mapping)
    if spec is None:  # pragma: no cover - every write validates the spec
        raise _not_applicable("The stored mapping is not readable.")
    return MappingOut(
        id=mapping.id,
        name=mapping.name,
        institution_id=mapping.institution_id,
        account_id=mapping.account_id,
        header_fingerprint=mapping.header_fingerprint,
        spec=spec,
        created_by=mapping.created_by,
        use_count=mapping.use_count,
        last_used_at=mapping.last_used_at,
        created_at=mapping.created_at,
        updated_at=mapping.updated_at,
    )


def _facts(mapping: ImportMapping) -> dict[str, Any]:
    return {
        "name_set": True,
        "account_id": mapping.account_id,
        "institution_id": mapping.institution_id,
        "header_fingerprint": mapping.header_fingerprint,
        "spec": mapping.spec,
        "sign_unverified": mapping.sign_unverified,
    }


def _record(
    db: Session,
    principal: Principal,
    mapping_id: uuid.UUID,
    household_id: uuid.UUID,
    *,
    action: str,
    event: str,
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=action,
        target_type="import_mapping",
        target_id=mapping_id,
        before=before,
        after=after,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(db, household_id, event, {"id": str(mapping_id)})


def _get(
    db: Session,
    principal: Principal,
    mapping_id: uuid.UUID,
    *,
    need: str = "view",
    lock: bool = False,
) -> ImportMapping:
    household_id = _household(principal, HouseholdRole.VIEWER)
    mapping = repo.get_mapping(db, household_id, mapping_id, _visible(principal, need), lock=lock)
    if mapping is None:
        raise mapping_not_found()
    return mapping


def list_mappings(
    db: Session, principal: Principal, *, cursor: uuid.UUID | None, limit: int = DEFAULT_PAGE
) -> MappingPage:
    household_id = _household(principal, HouseholdRole.VIEWER)
    rows = repo.list_mappings(db, household_id, _visible(principal), after=cursor, limit=limit + 1)
    more = len(rows) > limit
    rows = rows[:limit]
    return MappingPage(
        items=[mapping_out(m) for m in rows], next_cursor=rows[-1].id if more and rows else None
    )


def get_mapping(db: Session, principal: Principal, mapping_id: uuid.UUID) -> MappingOut:
    return mapping_out(_get(db, principal, mapping_id))


def require_mapping_writer(db: Session, principal: Principal, mapping_id: uuid.UUID) -> None:
    """``404`` unless the mapping exists here and the principal may change it."""
    _get(db, principal, mapping_id, need="edit")


def _document_table(db: Session, principal: Principal, document_id: uuid.UUID) -> Table:
    document = get_visible_document(db, principal, document_id)
    result = _stored_result(document)
    if result is None or not result.tables:
        raise _no_table()
    return result.tables[0]


def _fingerprint_for(
    db: Session,
    principal: Principal,
    spec: MappingSpec,
    *,
    document_id: uuid.UUID | None,
    given: str | None,
) -> str:
    if document_id is None:
        if given is None:  # pragma: no cover - the schemas require one of them
            raise _no_table()
        return given
    table = _document_table(db, principal, document_id)
    if spec.max_column() >= table.column_count:
        raise _columns_mismatch(table.column_count)
    fingerprint = spec_fingerprint(table, spec)
    if fingerprint is None:
        raise _no_table()
    return fingerprint


def _check_refs(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    *,
    account_id: uuid.UUID | None,
    institution_id: uuid.UUID | None,
) -> None:
    if account_id is not None:
        get_visible_account(db, principal, account_id, need="edit")
    if institution_id is not None and not accounts_repo.institution_exists(
        db, household_id, institution_id
    ):
        raise _institution_not_found()


def create_mapping(
    db: Session, principal: Principal, data: MappingCreate, *, request: HTTPConnection | None
) -> MappingOut:
    """Save a mapping (member or above; an account must be editable)."""
    household_id = _household(principal, HouseholdRole.MEMBER)
    _check_refs(
        db, principal, household_id, account_id=data.account_id, institution_id=data.institution_id
    )
    fingerprint = _fingerprint_for(
        db, principal, data.spec, document_id=data.document_id, given=data.header_fingerprint
    )
    if repo.name_taken(db, household_id, data.name):
        raise _name_taken()
    mapping = ImportMapping(
        household_id=household_id,
        name=data.name,
        account_id=data.account_id,
        institution_id=data.institution_id,
        header_fingerprint=fingerprint,
        spec=data.spec.model_dump(mode="json"),
        created_by=principal.user_id,
    )
    db.add(mapping)
    db.flush()
    _record(
        db,
        principal,
        mapping.id,
        household_id,
        action="import_mapping.create",
        event="document.mapping_created",
        after={**_facts(mapping), "from_document": data.document_id},
        request=request,
    )
    return mapping_out(mapping)


def update_mapping(
    db: Session,
    principal: Principal,
    mapping_id: uuid.UUID,
    data: MappingUpdate,
    *,
    request: HTTPConnection | None,
) -> MappingOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    mapping = _get(db, principal, mapping_id, need="edit", lock=True)
    before = _facts(mapping)
    given = data.model_fields_set
    _check_refs(
        db,
        principal,
        household_id,
        account_id=data.account_id if "account_id" in given else None,
        institution_id=data.institution_id if "institution_id" in given else None,
    )
    spec = data.spec if data.spec is not None else _spec(mapping)
    if spec is None:  # pragma: no cover - every write validates the spec
        raise _not_applicable("The stored mapping is not readable.")
    if "document_id" in given or "header_fingerprint" in given:
        mapping.header_fingerprint = _fingerprint_for(
            db, principal, spec, document_id=data.document_id, given=data.header_fingerprint
        )
    if data.name is not None and data.name != mapping.name:
        if repo.name_taken(db, household_id, data.name, exclude=mapping.id):
            raise _name_taken()
        mapping.name = data.name
    if data.spec is not None:
        mapping.spec = data.spec.model_dump(mode="json")
        mapping.sign_unverified = False  # a person saved the spec, sign included
    if "account_id" in given:
        mapping.account_id = data.account_id
    if "institution_id" in given:
        mapping.institution_id = data.institution_id
    mapping.updated_at = clock.utcnow()
    db.flush()
    _record(
        db,
        principal,
        mapping.id,
        household_id,
        action="import_mapping.update",
        event="document.mapping_updated",
        before=before,
        after=_facts(mapping),
        request=request,
    )
    return mapping_out(mapping)


def delete_mapping(
    db: Session, principal: Principal, mapping_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    household_id = _household(principal, HouseholdRole.MEMBER)
    mapping = _get(db, principal, mapping_id, need="edit", lock=True)
    before = _facts(mapping)
    db.execute(
        delete(ImportMapping).where(
            ImportMapping.household_id == household_id, ImportMapping.id == mapping.id
        )
    )
    _record(
        db,
        principal,
        mapping_id,
        household_id,
        action="import_mapping.delete",
        event="document.mapping_deleted",
        before=before,
        request=request,
    )


# ---------------------------------------------------------------------------
# Choosing a mapping for a table
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MappingChoice:
    mapping: ImportMapping | None
    spec: MappingSpec | None
    #: Several fitting mappings point at different accounts.
    ambiguous: tuple[uuid.UUID, ...] = ()


def _fits(table: Table, spec: MappingSpec) -> bool:
    return spec.max_column() < max(table.column_count, 1)


def choose_mapping(
    db: Session,
    household_id: uuid.UUID,
    table: Table,
    *,
    account_id: uuid.UUID | None,
    allowed: Collection[uuid.UUID] | None,
    visible: ColumnElement[bool] | None = None,
) -> MappingChoice:
    """The saved mapping for ``table`` by header fingerprint (module doc)."""
    fingerprints = candidate_fingerprints(table)
    fitting: list[tuple[ImportMapping, MappingSpec]] = []
    for mapping in repo.mappings_by_fingerprint(db, household_id, set(fingerprints.values())):
        spec = _spec(mapping)
        if spec is None or not _fits(table, spec):
            continue
        if fingerprints.get((spec.skip_rows, spec.has_header)) != mapping.header_fingerprint:
            continue
        if (
            mapping.account_id is not None
            and allowed is not None
            and mapping.account_id not in allowed
        ):
            continue
        fitting.append((mapping, spec))
    if visible is not None and fitting:
        ids = set(
            db.execute(
                select(ImportMapping.id).where(
                    ImportMapping.id.in_([m.id for m, _ in fitting]), visible
                )
            ).scalars()
        )
        fitting = [(m, s) for m, s in fitting if m.id in ids]
    if not fitting:
        return MappingChoice(None, None)
    if account_id is not None:
        for wanted in (account_id, None):
            for mapping, spec in fitting:
                if mapping.account_id == wanted:
                    return MappingChoice(mapping, spec)
        return MappingChoice(*fitting[0])
    accounts = {m.account_id for m, _ in fitting if m.account_id is not None}
    if len(accounts) > 1:
        return MappingChoice(None, None, tuple(m.id for m, _ in fitting))
    for mapping, spec in fitting:
        # One account-bound mapping wins over household ones (a member's
        # upload may have saved one meanwhile): its account routes the file.
        if mapping.account_id is not None:
            return MappingChoice(mapping, spec)
    return MappingChoice(*fitting[0])


# ---------------------------------------------------------------------------
# Committing a document's records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImportResult:
    """What one import run did; ``summary`` is JSON (ids as strings, counts)."""

    status: DocumentStatus
    summary: dict[str, Any]


@dataclass(frozen=True)
class _Opening:
    """A QIF's or table's opening balance, settled when the run ends (:func:`_settle_openings`)."""

    as_of: date
    amount: Decimal
    statement_index: int | None


@dataclass
class _NearGroup:
    """The near duplicates of one committed statement, held back to the end of the run."""

    statement_index: int | None
    #: The row written, the rows it resembles, and whether the row waits for
    #: a doubt of its own as well.
    rows: list[tuple[Transaction, list[uuid.UUID], bool]]


@dataclass
class _Standing:
    """A statement a person settled in the reconcile view, read again.

    Its record of this document says ``accepted`` or ``reconciled`` and the
    reading alone would say otherwise (:func:`_standing`). What becomes of
    it is known when the run ends (:func:`_settle_standing`).
    """

    index: int | None
    statement: Statement
    parsed: ParsedStatement
    facts: StatementFacts
    ref: str | None
    #: What the record said before this run.
    prior: Verification
    #: The statement is a clean read (:func:`clean_read`): never asked about.
    clean: bool = False

    @property
    def vouched(self) -> bool:
        """The reading's own check passed (opening + rows = closing)."""
        check = self.parsed.reconciliation
        return check is not None and check.status == "ok"


def _comparable(payload: Mapping[str, Any]) -> dict[str, Any]:
    """A review payload without its statement reference.

    An item raised before payloads carried one is the same finding.
    """
    return {name: value for name, value in payload.items() if name != REF_FIELD}


@dataclass
class _Run:
    db: Session
    document: Document
    allowed: frozenset[uuid.UUID] | None
    user_id: uuid.UUID | None
    imported: int = 0
    duplicates: int = 0
    possible_duplicates: int = 0
    skipped_rows: int = 0
    #: Table rows after the last readable one (totals, closing lines) and
    #: rows the bank had not booked yet (``Pending``): counted, never reported.
    footer_rows: int = 0
    pending_rows: int = 0
    #: Table rows that repeat the header, and rows that only print a balance
    #: (an opening one gives the statement's opening balance): counted, never
    #: imported or reported.
    header_rows: int = 0
    balance_rows: int = 0
    skipped_currency: int = 0
    snapshots: int = 0
    #: Rows a rule categorised, and rows that took their payee's default
    #: category: the engine's counts for ``rule`` and ``payee``.
    rules_changed: int = 0
    payee_categorised: int = 0
    #: Rows the import gave a category, by how it was decided (``file``,
    #: ``rule``, ``payee``, ``learned``, ``dictionary``, ``keyword``), and
    #: rows given a suggestion only.
    categorised: Counter[str] = field(default_factory=Counter)
    suggested: int = 0
    review_items: int = 0
    statements: int = 0
    low_confidence: int = 0
    accounts: list[uuid.UUID] = field(default_factory=list)
    mapping_id: uuid.UUID | None = None
    #: The mapping was saved from the suggestion by this run.
    mapping_suggested: bool = False
    #: Table 0 read as brokerage activity (``brokerage_table`` in the summary)
    #: and the saved mapping that reading took precedence over.
    activity: ActivityTable | None = None
    mapping_bypassed: uuid.UUID | None = None
    #: Table 0 read as a brokerage positions export (``positions_table`` in the summary).
    positions: PositionsTable | None = None
    #: Accounts a person assigned to the document's statements, by
    #: ``(target, index)`` (``POST /documents/{id}/accounts/apply``).
    assigned: dict[tuple[str, int], uuid.UUID] = field(default_factory=dict)
    #: Accounts this run imports into although a statement overlaps one
    #: already imported from another document (``apply-mapping``).
    allow_overlap: frozenset[uuid.UUID] = frozenset()
    #: The document's "import anyway" / "import as read" answers, by
    #: statement key (``statement_answers``).
    answers: StatementAnswers = field(default_factory=StatementAnswers)
    #: Statements held (``statement_overlap``) or skipped as already imported.
    overlaps: int = 0
    #: What the document parsers said about the whole document
    #: (``ExtractResult.parse``): lines they counted as skipped, and their
    #: warnings. Zero and empty for a file no document parser read.
    parse_skipped: int = 0
    parse_warnings: tuple[str, ...] = ()
    #: Statements of a document format (PDF, image) committed without a
    #: balance that vouches for them: the ones shown as "not checked against
    #: a balance" (``statement_record.verification = unverified``).
    unverified_statements: int = 0
    #: Rows the files carry that a person deleted (an import tombstone):
    #: never written again.
    tombstoned: int = 0
    #: Rows of an earlier reading of the document the new reading no longer
    #: holds: removed because nobody had touched them, or kept and listed.
    replaced: int = 0
    kept: int = 0
    #: The hashes of every row this run read for an account (the document's
    #: current reading of it), by account.
    read: dict[uuid.UUID, set[str]] = field(default_factory=dict)
    #: Period starts of the bank statements this run recorded, by account.
    periods: dict[uuid.UUID, set[date]] = field(default_factory=dict)
    #: Accounts a statement of the document was not committed to in this run
    #: (held as an overlap, another currency, its sign waits for an answer).
    waiting: set[uuid.UUID] = field(default_factory=set)
    #: Accounts a statement of which this run committed although the reading
    #: says it may not hold every row (:func:`_incomplete`): a row it no
    #: longer holds is not an earlier reading's.
    incomplete: set[uuid.UUID] = field(default_factory=set)
    #: The rows of every statement this run committed, statement by
    #: statement, as they were hashed (:func:`_settle_moved`).
    committed: list[list[HashInput]] = field(default_factory=list)
    #: A statement of the document has no account yet, or the document holds
    #: brokerage statements: nothing says which rows are an earlier reading.
    unplaced: bool = False
    #: Near duplicates whose finding waits for the end of the run, by
    #: account: a candidate is a row of this very document, which the run
    #: may still remove as an earlier reading (:func:`_replace_earlier`).
    near_waiting: dict[uuid.UUID, list[_NearGroup]] = field(default_factory=dict)
    #: Accounts a row of which this run wrote, removed as an earlier reading,
    #: or kept and listed for the first time.
    changed: set[uuid.UUID] = field(default_factory=set)
    #: The bank statements of the document this run put on each account, in
    #: document order (committed or held).
    placed: dict[uuid.UUID, list[Statement]] = field(default_factory=dict)
    #: Statements a person accepted or balanced, by account, until the run
    #: knows whether it changed a row (:func:`_settle_standing`).
    standing: dict[uuid.UUID, list[_Standing]] = field(default_factory=dict)
    #: The opening balances of the QIF sections and tables this run
    #: committed, by account (:func:`_settle_openings`).
    openings: dict[uuid.UUID, list[_Opening]] = field(default_factory=dict)
    _payees: PayeeMatcher | None = None
    _categories: dict[str, uuid.UUID] | None = None
    _open: list[tuple[ReviewKind, dict[str, Any]]] | None = None

    @property
    def household_id(self) -> uuid.UUID:
        return self.document.household_id

    def review(self, kind: ReviewKind, payload: dict[str, Any]) -> None:
        """Raise a review item; an identical open one (a re-run's) is not repeated.

        Items a re-run answers (:data:`RERUN_ANSWERS`) are always raised anew:
        the re-run resolves the earlier ones.
        """
        if kind not in RERUN_ANSWERS:
            if self._open is None:
                self._open = [
                    (item.kind, _comparable(item.payload or {}))
                    for item in repo.open_review_items(
                        self.db, self.household_id, self.document.id, set(ReviewKind)
                    )
                ]
            if (kind, _comparable(payload)) in self._open:
                return
            self._open.append((kind, _comparable(payload)))
        documents_repo.add_review_item(
            self.db, self.household_id, kind, payload, document_id=self.document.id
        )
        self.review_items += 1

    def payees(self) -> PayeeMatcher:
        if self._payees is None:
            self._payees = PayeeMatcher(repo.payee_lookup(self.db, self.household_id))
        return self._payees

    def categories(self) -> dict[str, uuid.UUID]:
        if self._categories is None:
            self._categories = repo.category_lookup(self.db, self.household_id)
        return self._categories

    def summary(self) -> dict[str, Any]:
        return {
            "imported": self.imported,
            "duplicates": self.duplicates,
            "possible_duplicates": self.possible_duplicates,
            "skipped_rows": self.skipped_rows,
            "footer_rows": self.footer_rows,
            "pending_rows": self.pending_rows,
            "header_rows": self.header_rows,
            "balance_rows": self.balance_rows,
            "skipped_currency": self.skipped_currency,
            "snapshots": self.snapshots,
            "rules_changed": self.rules_changed,
            "payee_categorised": self.payee_categorised,
            "categorised": dict(sorted(self.categorised.items())),
            "suggested": self.suggested,
            "review_items": self.review_items,
            "statements": self.statements,
            "low_confidence": self.low_confidence,
            "statement_overlaps": self.overlaps,
            "unverified_statements": self.unverified_statements,
            "replaced_rows": self.replaced,
            "kept_rows": self.kept,
            "tombstoned_rows": self.tombstoned,
            "accounts": [str(a) for a in self.accounts],
            "mapping_id": str(self.mapping_id) if self.mapping_id else None,
            "mapping_suggested": self.mapping_suggested,
            **(
                {
                    "brokerage_table": self.activity.summary(),
                    "mapping_bypassed": (
                        str(self.mapping_bypassed) if self.mapping_bypassed else None
                    ),
                }
                if self.activity is not None
                else {}
            ),
            **({"positions_table": self.positions.summary()} if self.positions is not None else {}),
        }


def _stored_result(document: Document) -> ExtractResult | None:
    if not document.extraction:
        return None
    try:
        result = ExtractResult.model_validate(document.extraction)
    except ValidationError:
        return None
    # A PDF/image no parser read offers its page table to the mapping editor.
    return with_layout_tables(result)


def _usable_account(run: _Run, account_id: uuid.UUID | None) -> Account | None:
    if account_id is None:
        return None
    if run.allowed is not None and account_id not in run.allowed:
        return None
    return accounts_repo.get_account(run.db, run.household_id, account_id, lock=True)


def _category_id(run: _Run, name: str | None) -> uuid.UUID | None:
    if not name:
        return None
    return run.categories().get(name.strip().casefold())


#: Liability account types as a statement may spell them (``creditcard``,
#: OFX ``CREDITLINE``): compared without case and underscores.
_LIABILITY_WORDS: Final = frozenset(
    {str(kind).replace("_", "") for kind in LIABILITY_TYPES} | {"creditline"}
)


def liability_side(account_type: str | None) -> bool | None:
    """Whether the type a statement names for its account is a liability.

    None when it names none: the reader then took the amounts as printed,
    money in positive (a bank statement).
    """
    if not account_type:
        return None
    return account_type.replace("_", "").lower() in _LIABILITY_WORDS


def same_side(account_type: AccountType, read_as: str | None) -> bool:
    """The account is on the side of the balance sheet its statement was read for.

    A reader that knows it has a card statement turns the printed amounts
    (a purchase is printed positive) into the ledger's signs; one that does
    not takes them as printed. So a statement read without a type, or as an
    asset account's, is a bank statement, and what a positive amount means
    on a liability account is something nothing on it proves: the table
    importer never writes into a liability account on such a sign
    (:func:`_sign_held`) and neither does a clean read.
    """
    return (account_type in LIABILITY_TYPES) is bool(liability_side(read_as))


def clean_read(run: _Run, parsed: ParsedStatement) -> bool:
    """A statement that read cleanly and that nothing contradicts (module doc).

    The owner's policy, "import clean reads, flag quietly": a PDF or image
    statement that prints nothing to check its rows against (no opening or
    closing balance, no running balance: reconciliation ``unavailable``) is
    imported as posted when that is its **only** doubt. It is one only when
    all of this holds:

    * **no balance was read at all**: neither an opening nor a closing one.
      ``unavailable`` is also what one balance without the other is, and a
      balance the statement printed and nothing could be checked against is
      a doubt, not a silence. Nor one the parser withheld because nothing
      settled its sign (:data:`UNUSED_BALANCE_WARNINGS`);
    * **the period was printed**: its confidence
      (``ParsedStatement.period_confidence``) is not below
      ``confidence.DEFAULT_THRESHOLD``. A period that is only the rows' own
      first and last day scores ``confidence.INFERRED`` at most: nothing on
      the page then says which statement this is, or that its rows are
      dated right. A statement nothing scored a period of (hand-built) is
      not refused for it;
    * it has at least one row, and every row's ``low_confidence`` is empty
      or exactly ``reconciliation`` (the parser's cap for an unchecked row);
    * the importer skipped no row of it, no running balance disagrees, it
      was not truncated and its date order was not assumed;
    * the document's parse counted no skipped line and carries no warning of
      :data:`CONTRADICTION_WARNINGS`.

    One doubt anywhere and the whole statement waits as before. A statement
    whose check failed (``mismatch``) is never one; neither is a format
    that is not a document (OFX, QIF, CSV), which was never held for this.
    Pure on its arguments: of ``run`` only the parse facts are read.
    """
    check = parsed.reconciliation
    if parsed.source_format not in DOCUMENT_STATEMENT_FORMATS:
        return False
    if check is None or check.status != "unavailable":
        return False
    if (
        check.opening is not None
        or check.closing is not None
        or parsed.opening_balance is not None
        or parsed.closing_balance is not None
    ):
        return False
    if parsed.period_confidence is not None and parsed.period_confidence < DEFAULT_THRESHOLD:
        return False
    if not parsed.transactions:
        return False
    if any(set(t.low_confidence) - {UNCHECKED_FIELD} for t in parsed.transactions):
        return False
    if (
        parsed.skipped_rows
        or parsed.balance_mismatches
        or parsed.truncated
        or parsed.date_order_assumed
    ):
        return False
    if UNUSED_BALANCE_WARNINGS.intersection(run.parse_warnings):
        return False
    return not run.parse_skipped and not CONTRADICTION_WARNINGS.intersection(run.parse_warnings)


def _incomplete(run: _Run, parsed: ParsedStatement) -> bool:
    """The reading itself says it may not hold every row of the statement.

    A truncated statement always. Else, unless its own balances vouch for
    its rows (opening + rows = closing: none is missing), one of which a
    row was skipped or a running balance disagrees, in a document whose
    parse counted a skipped line or carries a warning of
    :data:`CONTRADICTION_WARNINGS`. The rows such a reading no longer holds
    are not removed as an earlier reading's (:func:`_finish_reading`): the
    line it could not read may be one of them.
    """
    if parsed.truncated:
        return True
    check = parsed.reconciliation
    if check is not None and check.status == "ok":
        return False
    return bool(
        parsed.skipped_rows
        or parsed.balance_mismatches
        or run.parse_skipped
        or CONTRADICTION_WARNINGS.intersection(run.parse_warnings)
    )


def _contradiction(run: _Run, check: Reconciliation) -> str | None:
    """Why a statement whose balances agree with its rows still failed its check.

    A ``mismatch`` with no difference (opening + rows = closing, or no
    difference could be worked out) on a document the parser warned about
    (:data:`CONTRADICTION_WARNINGS`): the first such warning in
    :data:`_CAUSE_ORDER`. None when the difference itself is the finding.
    """
    if check.status != "mismatch" or check.difference:
        return None
    return next((name for name in _CAUSE_ORDER if name in run.parse_warnings), None)


def _row_problems(
    run: _Run,
    parsed: ParsedStatement,
    account: Account,
    *,
    clean: bool = False,
    statement_index: int | None = None,
    ref: str | None = None,
    settled: bool = False,
) -> None:
    # Footers and pending rows are not problems: ``rows_skipped`` is raised
    # for the rows that should have read (``parsed.skipped_rows``) only.
    # ``clean``: a clean read (:func:`clean_read`) is not asked about: its
    # record says ``unverified`` instead of a ``reconciliation_unavailable`` item.
    # ``statement_index``: the statement's index in the document (None: a table).
    # ``ref``: its stable reference (``statement_answers.statement_ref``).
    # ``settled``: a person accepted or balanced the statement; whether it is
    # asked about again is decided when the run ends (:func:`_settle_standing`).
    run.footer_rows += len(parsed.footer_rows)
    run.pending_rows += len(parsed.pending_rows)
    run.header_rows += len(parsed.header_rows)
    run.balance_rows += len(parsed.balance_rows)
    if parsed.skipped_rows:
        run.skipped_rows += len(parsed.skipped_rows)
        run.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "rows_skipped",
                "account_id": str(account.id),
                "count": len(parsed.skipped_rows),
                "rows": parsed.skipped_rows[:IDS_MAX],
                "header_row": parsed.header_row,
            },
        )
    if parsed.balance_mismatches:
        run.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "balance_mismatch",
                "field": "balance",
                "account_id": str(account.id),
                "count": len(parsed.balance_mismatches),
                "rows": parsed.balance_mismatches[:IDS_MAX],
            },
        )
    if parsed.date_order_assumed:
        run.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "date_order_assumed",
                "field": "posted_date",
                "assumed": "mdy",
                "account_id": str(account.id),
            },
        )
    if parsed.truncated:
        run.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {"reason": "truncated", "account_id": str(account.id)},
        )
    if not clean and not settled:
        _reconciliation_review(run, parsed, account, statement_index=statement_index, ref=ref)


def _reconciliation_review(
    run: _Run,
    parsed: ParsedStatement,
    account: Account,
    *,
    statement_index: int | None,
    ref: str | None,
) -> None:
    """The finding of a statement whose check failed or could not be made.

    SECURITY.md C12: the statement's own figures disagree (or cannot be
    checked and something else is in doubt); its rows are imported pending
    and no snapshot is written. Nothing is raised for a statement that
    reconciles or carries no check (OFX, QIF, a table).
    """
    check = parsed.reconciliation
    if parsed.reconciled or check is None:
        return
    payload: dict[str, Any] = {
        "reason": (
            "reconciliation_failed" if check.status == "mismatch" else "reconciliation_unavailable"
        ),
        "field": "reconciliation",
        "account_id": str(account.id),
        # Which statement of the document, and how many lines of the
        # document no reader could read (a likely place for a missing row).
        "statement": statement_index,
        "lines_skipped": run.parse_skipped,
    }
    if ref is not None:
        # The statement whatever its figures read as (``statement_answers``).
        payload[REF_FIELD] = ref
    cause = _contradiction(run, check)
    if cause is not None:
        # The balances agree with the rows; the statement's own summary
        # or a section total does not.
        payload["cause"] = cause
    run.review(
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {
            **payload,
            "parser": parsed.parser,
            "opening": _decimal_text(check.opening),
            "closing": _decimal_text(check.closing),
            "total": _decimal_text(check.total),
            "difference": _decimal_text(check.difference),
            "running_mismatches": len(check.running_mismatches),
            "rows": check.running_mismatches[:IDS_MAX],
            # How many of the statement's rows the failed check leaves
            # in doubt (imported pending); the others are vouched for by
            # the statement's own running balance and imported as usual.
            "rows_in_doubt": (
                len(check.doubtful) if check.doubtful is not None else len(parsed.transactions)
            ),
            "rows_total": len(parsed.transactions),
        },
    )


def _decimal_text(value: Decimal | None) -> str | None:
    return f"{value:f}" if value is not None else None


def _confidence(value: float | None) -> Decimal | None:
    return Decimal(str(round(value, 4))) if value is not None else None


def _own_doubts(txn: ParsedTransaction) -> list[str]:
    """The fields of a row in doubt for a reason of the row's own."""
    return [name for name in txn.low_confidence if name != UNCHECKED_FIELD]


def _near_duplicates_review(
    run: _Run,
    account: Account,
    near: Sequence[tuple[Transaction, Sequence[uuid.UUID]]],
    *,
    statement_index: int | None,
) -> None:
    """The ``possible_duplicate`` finding for a statement's near duplicates.

    One row: a ``near_match`` item, as always. Two or more: one
    ``near_matches`` item listing them (at most :data:`IDS_MAX`; ``count``
    is all of them), so a statement entered by hand first is one question
    instead of one per row.
    """
    if not near:
        return
    if len(near) == 1:
        row, candidates = near[0]
        run.review(
            ReviewKind.POSSIBLE_DUPLICATE,
            {
                "reason": "near_match",
                "account_id": str(account.id),
                "transaction_id": str(row.id),
                "candidates": [str(c) for c in candidates],
            },
        )
        return
    run.review(
        ReviewKind.POSSIBLE_DUPLICATE,
        {
            "reason": "near_matches",
            "account_id": str(account.id),
            "statement": statement_index,
            "count": len(near),
            "rows": [
                {"transaction_id": str(row.id), "candidates": [str(c) for c in candidates]}
                for row, candidates in near[:IDS_MAX]
            ],
        },
    )


def _low_confidence_review(
    run: _Run,
    parsed: ParsedStatement,
    account: Account,
    low: list[tuple[Transaction, ParsedTransaction]],
) -> None:
    """One ``low_confidence_field`` item for the rows a document parser was unsure of.

    Only rows with a doubt of their own: ``reconciliation`` on a row says its
    *statement* did not add up or could not be checked, which the
    ``reconciliation_failed``/``reconciliation_unavailable`` item already
    says once, with the figures. Such a row still waits (``pending``); it is
    not listed a second time here, and the field is left out of ``fields``.
    """
    low = [(row, txn) for row, txn in low if _own_doubts(txn)]
    if not low:
        return
    run.low_confidence += len(low)
    fields = sorted({name for _, txn in low for name in _own_doubts(txn)})
    run.review(
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {
            "reason": "low_confidence",
            "fields": fields,
            "account_id": str(account.id),
            "parser": parsed.parser,
            "count": len(low),
            "transactions": [
                {
                    "transaction_id": str(row.id),
                    "fields": _own_doubts(txn),
                    "confidence": txn.confidence,
                    "page": txn.page,
                }
                for row, txn in low[:IDS_MAX]
            ],
        },
    )


def _commit_statement(
    run: _Run,
    parsed: ParsedStatement,
    account: Account,
    *,
    statement_index: int | None,
    ref: str | None = None,
    standing: _Standing | None = None,
) -> Verification | None:
    """Write one statement's new transactions, review items and closing balance.

    None when nothing could be written (the statement is in another
    currency). Else how far the statement was checked, for its record:
    ``reconciled`` when a document parser's reconciliation passed,
    ``unverified`` for everything else (a statement that does not add up or
    cannot be checked; OFX, QIF and tables, which carry no reconciliation).

    A *clean read* (:func:`clean_read`, with no row left out for its
    currency, on an account of the side it was read for: :func:`same_side`)
    is imported as posted: a row of it is ``pending`` only when it
    is a near duplicate, no ``reconciliation_unavailable`` and no
    ``low_confidence`` item is raised for it, and, as for every statement
    that did not reconcile, no balance snapshot is written.

    ``standing``: a person accepted or balanced this statement in the
    reconcile view (:func:`_standing`). Its record keeps what it said, no
    reconciliation finding is raised and no snapshot is written here;
    :func:`_settle_standing` decides at the end of the run, when it is known
    whether the run changed a row of the account.
    """
    db = run.db
    if parsed.currency is not None and parsed.currency != account.currency:
        run.review(
            ReviewKind.UNKNOWN_ACCOUNT,
            {
                "reason": "currency_mismatch",
                "statement": statement_index,
                "account_id": str(account.id),
                "currency": parsed.currency,
                "account_currency": account.currency,
            },
        )
        run.waiting.add(account.id)
        return None
    run.statements += 1
    if account.id not in run.accounts:
        run.accounts.append(account.id)
    rows = [t for t in parsed.transactions if t.currency is None or t.currency == account.currency]
    foreign = len(parsed.transactions) - len(rows)
    if foreign:
        run.skipped_currency += foreign
        run.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "currency_mismatch",
                "field": "currency",
                "account_id": str(account.id),
                "count": foreign,
            },
        )
    # A row left out for its currency is a doubt of its own: not a clean read.
    # Neither is a statement read for the other side of the balance sheet
    # than its account's (:func:`same_side`): its sign is on trust.
    clean = not foreign and same_side(account.type, parsed.account_type) and clean_read(run, parsed)
    _row_problems(
        run,
        parsed,
        account,
        clean=clean,
        statement_index=statement_index,
        ref=ref,
        settled=standing is not None,
    )
    inputs = [HashInput(t.posted_date, t.amount, t.raw_payee) for t in rows]
    plan = dedupe(db, run.household_id, account.id, inputs)
    run.committed.append(inputs)
    if _incomplete(run, parsed):
        run.incomplete.add(account.id)
    run.duplicates += len(plan.exact)
    # Rows a person deleted are in none of the plan's lists to write.
    run.tombstoned += len(plan.removed)
    run.read.setdefault(account.id, set()).update(plan.hashes)
    payees = run.payees() if plan.to_insert else None
    created: list[tuple[Transaction, bool]] = []
    split_lines: dict[int, list[dict[str, Any]]] = {}
    #: The issuer's category for rows whose file category names none of the
    #: household's (by position in ``created``): a guess for the engine.
    issuer_keys: dict[int, str] = {}
    for index in plan.to_insert:
        txn = rows[index]
        category_id = None if txn.splits else _category_id(run, txn.category)
        if category_id is None and not txn.splits:
            issuer_key = institution.category_key(txn.category)
            if issuer_key is not None:
                issuer_keys[len(created)] = issuer_key
        row = Transaction(
            household_id=run.household_id,
            account_id=account.id,
            posted_date=txn.posted_date,
            amount=to_storage(txn.amount),
            currency=account.currency,
            payee_id=(
                # Behind a processor the payee is found in the descriptor's core.
                payees.match_descriptor(txn.raw_payee) if payees is not None else None
            ),
            category_id=category_id,
            raw_payee=txn.raw_payee[:PAYEE_MAX] if txn.raw_payee else None,
            memo=txn.memo[:MEMO_MAX] if txn.memo else None,
            status=TransactionStatus.CLEARED,
            source_document_id=run.document.id,
            import_hash=plan.hashes[index],
            review_state=(
                ReviewState.PENDING
                if index in plan.near or (txn.low_confidence and not clean)
                else ReviewState.NONE
            ),
            confidence=_confidence(txn.confidence),
        )
        db.add(row)
        lines = _split_lines(run, txn.amount, txn.splits)
        file_categorised = category_id is not None or bool(lines)
        if lines:
            split_lines[len(created)] = lines
        created.append((row, file_categorised))
    db.flush()
    for position, lines in split_lines.items():
        transactions_repo.replace_splits(db, created[position][0], lines)
    # Keys, the file's categories, the household's rules, then a guess for
    # what is still undecided (a payee's default, what was learned, the
    # dictionary): one pass, whose own writes leave ``updated_at`` alone.
    outcome = engine.categorise_rows(
        db,
        run.household_id,
        [row for row, _ in created],
        file_categorised={row.id for row, categorised in created if categorised},
        file_hints={created[position][0].id: key for position, key in issuer_keys.items()},
        user_id=run.user_id,
    )
    # Rows the file carries again (exact duplicates, not written) take its
    # category when nobody decided theirs: a file imported before its
    # category column was read.
    again = {
        plan.hashes[index]: engine.FileCategory(
            category_id=_category_id(run, rows[index].category),
            key=institution.category_key(rows[index].category),
        )
        for index in plan.exact
        if rows[index].category and not rows[index].splits
    }
    if again:
        refilled = engine.fill_from_file(
            db, run.household_id, account.id, again, user_id=run.user_id
        )
        run.categorised.update(refilled.by_source)
    run.rules_changed += outcome.by_source["rule"]
    run.payee_categorised += outcome.by_source["payee"]
    run.categorised.update(outcome.by_source)
    run.suggested += outcome.suggested
    positions = {index: position for position, index in enumerate(plan.to_insert)}
    near = [
        (
            created[positions[index]][0],
            list(candidates),
            bool(rows[index].low_confidence) and not clean,
        )
        for index, candidates in plan.near.items()
    ]
    if _own_rows(run, account, [c for _, candidates, _ in near for c in candidates]):
        # A row resembles a row of this very document: whether that one is
        # an earlier reading is known when the run ends.
        run.near_waiting.setdefault(account.id, []).append(_NearGroup(statement_index, near))
    else:
        _near_duplicates_review(
            run,
            account,
            [(row, candidates) for row, candidates, _ in near],
            statement_index=statement_index,
        )
    run.imported += len(created)
    run.possible_duplicates += len(plan.near)
    _low_confidence_review(
        run,
        parsed,
        account,
        [
            (created[positions[index]][0], rows[index])
            for index in plan.to_insert
            if rows[index].low_confidence and not clean
        ],
    )
    # An accepted statement whose reading adds up is checked against the
    # ledger first (:func:`_settle_standing`): its rows may have been edited.
    # A table whose own running balance does not follow from its amounts
    # vouches for no balance (its ``balance_mismatch`` item says so).
    if (
        parsed.reconciled
        and not parsed.balance_mismatches
        and not (standing is not None and standing.vouched)
    ):
        _write_snapshot(run, parsed, account)
    if (
        parsed.source_format in OPENING_FORMATS
        and parsed.opening_balance is not None
        and parsed.opening_balance_date is not None
        and not parsed.balance_mismatches
    ):
        run.openings.setdefault(account.id, []).append(
            _Opening(
                parsed.opening_balance_date,
                to_storage(parsed.opening_balance),
                statement_index,
            )
        )
    if created:
        run.changed.add(account.id)
        ids = [row.id for row, _ in created]
        audit(
            db,
            action="transaction.import",
            target_type="document",
            target_id=run.document.id,
            after={
                "account_id": account.id,
                "count": len(ids),
                "possible_duplicates": len(plan.near),
                "duplicates": len(plan.exact),
                "tombstoned": len(plan.removed),
                "transaction_ids": [str(i) for i in ids[:IDS_MAX]],
            },
            household_id=run.household_id,
            user_id=run.user_id,
        )
        emit(
            db,
            run.household_id,
            "transaction.created",
            {
                "document_id": str(run.document.id),
                "account_id": str(account.id),
                "count": len(ids),
                "ids": [str(i) for i in ids[:IDS_MAX]],
            },
        )
    # Rules ran as part of the import and moved ``updated_at`` where they
    # changed a row (the engine's own writes never do): the rows are as
    # imported, untouched until a person changes one.
    _mark_untouched(db, [row for row, _ in created])
    if standing is not None:
        standing.clean = clean
        run.standing.setdefault(account.id, []).append(standing)
        return standing.prior
    check = parsed.reconciliation
    return "reconciled" if check is not None and check.status == "ok" else "unverified"


def _write_snapshot(run: _Run, parsed: ParsedStatement, account: Account) -> None:
    """Record the statement's closing balance as the account's balance that day."""
    if parsed.closing_balance is None:
        return
    accounts_repo.upsert_snapshot(
        run.db,
        account,
        parsed.closing_balance.as_of,
        to_storage(parsed.closing_balance.amount),
        BalanceSource.STATEMENT,
        source_document_id=run.document.id,
    )
    run.snapshots += 1
    emit(run.db, run.household_id, "balance.updated", {"account_id": str(account.id)})


def _mark_untouched(db: Session, rows: Sequence[Transaction]) -> None:
    """Set ``updated_at`` of ``rows`` to their ``created_at`` ("untouched since import").

    Whatever the import itself did to a row (a rule; the engine's guesses
    are pinned writes) is flushed first, so the equal timestamps are the
    last write.
    """
    db.flush()
    changed = [row for row in rows if row.updated_at != row.created_at]
    for row in changed:
        row.updated_at = row.created_at
    if changed:
        db.flush()


def _own_rows(run: _Run, account: Account, candidates: Collection[uuid.UUID]) -> bool:
    """One of ``candidates`` is a row this document produced on the account."""
    if not candidates:
        return False
    found = run.db.execute(
        select(Transaction.id)
        .where(
            Transaction.household_id == run.household_id,
            Transaction.account_id == account.id,
            Transaction.id.in_(sorted(set(candidates))),
            Transaction.source_document_id == run.document.id,
        )
        .limit(1)
    ).first()
    return found is not None


def _record_committed(
    run: _Run,
    account: Account,
    facts: StatementFacts,
    verification: Verification,
    *,
    lower_accepted: bool = False,
) -> None:
    """Record a committed statement with how far it was checked.

    A recorded statement of a document format (PDF, image) that is
    ``unverified`` is the one shown as "not checked against a balance":
    counted for the run's summary (``unverified_statements``).
    """
    recorded = record_statement(
        run.db,
        run.document,
        account,
        facts,
        verification=verification,
        lower_accepted=lower_accepted,
    )
    if recorded and facts.kind == "bank" and facts.period_start is not None:
        run.periods.setdefault(account.id, set()).add(facts.period_start)
    if (
        recorded
        and verification == "unverified"
        and facts.source_format in DOCUMENT_STATEMENT_FORMATS
    ):
        run.unverified_statements += 1


def _guarded(
    run: _Run, account: Account, facts: StatementFacts, index: int | None, key: str
) -> bool:
    """The statement-level duplicate guard (``statement_records``); True: commit it."""
    verdict = check_overlap(
        run.db,
        run.document,
        account,
        facts,
        index=index,
        key=key,
        allow=account.id in run.allow_overlap or run.answers.overlap("statement", key, account.id),
        review=run.review,
    )
    if verdict != "import":
        run.overlaps += 1
        run.waiting.add(account.id)
        if account.id not in run.accounts:
            run.accounts.append(account.id)
        return False
    return True


def _split_lines(run: _Run, amount: Decimal, splits: Sequence[Any]) -> list[dict[str, Any]]:
    """Split rows for a file's split transaction (only when they add up exactly)."""
    if len(splits) < 2 or sum_amounts(s.amount for s in splits) != amount:
        return []
    return [
        {
            "amount": to_storage(s.amount),
            "category_id": _category_id(run, s.category),
            "memo": s.memo[:MEMO_MAX] if s.memo else None,
            "tax_flag": False,
        }
        for s in splits
    ]


def _several_kinds(result: ExtractResult) -> bool:
    """Bank and brokerage statements of different accounts in one document.

    A combined statement (checking, savings and brokerage in one PDF): one
    bank statement there is not the document's only account, so it is
    matched by its own last four digits like the others.
    """
    if not (result.statements and result.investments):
        return False
    named = {s.account.last4 for s in result.statements}
    named |= {i.account.last4 for i in result.investments}
    return len(named) > 1


def records_skipped(result: ExtractResult) -> int:
    """How many records of the file its extractor could not read.

    The OFX and QIF extractors say so in ``transaction_skipped:<n>`` warnings,
    one per statement (bank and brokerage alike); the counts add up.
    """
    found = (_SKIPPED_WARNING.fullmatch(warning) for warning in result.warnings)
    return sum(int(match.group(1)) for match in found if match is not None)


def _report_skipped(run: _Run, result: ExtractResult) -> None:
    """Raise one ``lines_skipped`` item for the records the extractor lost.

    One per document, whatever the number of statements and whether or not
    each found its account: a file that lost records never ends
    ``processed`` on the rows that were read. The warnings name no
    statement, so the item names none either.
    """
    count = records_skipped(result)
    if count:
        run.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {"reason": "lines_skipped", "count": count, "source": "extraction"},
        )


#: The parser's note that it split one document into the statements of
#: several accounts at their summaries (``table_statement``, module doc 8).
SPLIT_BY_SUMMARY: Final = "accounts_by_summary"


def statement_match(
    db: Session,
    household_id: uuid.UUID,
    result: ExtractResult,
    index: int,
    *,
    institution: str | None = None,
    allowed: Collection[uuid.UUID] | None,
    institution_id: uuid.UUID | None = None,
) -> AccountMatch:
    """The account the digits (and institution) of bank statement ``index`` name.

    A statement that prints no digits goes to the one open account at the
    document's institution (``institution_id``; ``match_account``), on the
    side of the balance sheet its type names (:func:`liability_side`): a
    card statement never lands on the bank's lone checking account. Only
    when it is the document's one statement: of two without digits nothing
    says which is whose.

    In a document the parser split at its accounts' summaries, statements
    may carry the same last four digits (two accounts printed under one
    masked number), and the digits then name none of them. A statement of
    such a document goes to the account with its digits that has the type
    its summary names (:func:`match_shared_number`): never to an account
    of another type, and to none when it names no type while another
    statement carries its digits, or when another names its type too. One
    that names no type and has digits of its own is matched by them.
    Anywhere else the same digits are the same account (two months of one
    account in an OFX file).
    """
    account = result.statements[index].account
    named = [(s.account.last4, s.account.account_type) for s in result.statements]
    split = result.parse is not None and SPLIT_BY_SUMMARY in result.parse.warnings
    shared = account.last4 in shared_numbers(named)
    if split and account.last4 and (shared or account.account_type):
        return match_shared_number(
            db,
            household_id,
            last4=account.last4,
            account_type=account.account_type,
            name=account.name,
            told_apart=named.count((account.last4, account.account_type)) == 1,
            allowed=allowed,
        )
    alone = len(result.statements) == 1 and not result.investments
    return match_account(
        db,
        household_id,
        last4=account.last4,
        institution=institution or account.institution,
        allowed=allowed,
        institution_id=institution_id if alone else None,
        liability=liability_side(account.account_type),
    )


def _import_statements(run: _Run, result: ExtractResult, *, account_id: uuid.UUID | None) -> None:
    single = len(result.statements) == 1 and not _several_kinds(result)
    refs = statement_refs("statement", result.statements)
    for index, statement in enumerate(result.statements):
        parsed = from_statement(statement)
        key = statement_key(statement)
        account: Account | None = None
        candidates: tuple[uuid.UUID, ...] = ()
        # The run's own assignment, else the account an answer put it on: the
        # statement exactly as read, else the statement by its reference (its
        # balances were corrected, or a parser reads it differently now).
        assigned = run.assigned.get(("statement", index)) or run.answers.account(
            "statement", key, refs[index]
        )
        if assigned is not None:
            account = _usable_account(run, assigned)
        if (
            account is None
            and single
            and run.document.account_id is not None
            and account_id is None
        ):
            account = _usable_account(run, run.document.account_id)
        if account is None:
            match = statement_match(
                run.db,
                run.household_id,
                result,
                index,
                institution=parsed.institution,
                allowed=run.allowed,
                institution_id=run.document.institution_id,
            )
            candidates = match.candidates
            if account_id is not None and (match.account_id is None or single):
                account = _usable_account(run, account_id)
            else:
                account = _usable_account(run, match.account_id)
        if account is None:
            run.unplaced = True
            run.review(
                ReviewKind.UNKNOWN_ACCOUNT,
                {
                    "reason": "account_ambiguous" if candidates else "account_not_found",
                    "statement": index,
                    "account_type": parsed.account_type,
                    "currency": parsed.currency,
                    "transactions": len(parsed.transactions),
                    "candidates": [str(c) for c in candidates[:IDS_MAX]],
                },
            )
            continue
        run.placed.setdefault(account.id, []).append(statement)
        facts = bank_facts(statement)
        if not _guarded(run, account, facts, index, key):
            continue
        verification = _commit_statement(
            run,
            parsed,
            account,
            statement_index=index,
            ref=refs[index],
            standing=_standing(run, statement, parsed, account, facts, index, refs[index]),
        )
        if verification is not None:
            _record_committed(run, account, facts, verification)
            if verification == "unverified":
                _withdraw(run, account, statement)


def _withdraw(run: _Run, account: Account, statement: Statement) -> None:
    """Take back the balance an earlier run of the document recorded for ``statement``.

    For a statement this run records as ``unverified``: an ``unverified``
    statement of a document (PDF, image) vouches for no balance, so the
    snapshot it wrote while it was ``reconciled`` does not outlive that
    (``statement_records.withdraw_snapshot``; nothing is removed for a
    bank file or a table, and nothing another document recorded).
    """
    withdraw_snapshot(run.db, run.document, account, statement)


def _standing(
    run: _Run,
    statement: Statement,
    parsed: ParsedStatement,
    account: Account,
    facts: StatementFacts,
    index: int,
    ref: str,
) -> _Standing | None:
    """The answer a person gave about this statement in the reconcile view, if it stands.

    The document's record of the statement (this account, this period
    start) says ``accepted``, or says ``reconciled`` although the reading
    does not add up by itself (the person balanced it against the ledger:
    a row edited, a row removed). None for every other statement: one that
    was never settled, and a reconciled one whose reading vouches for
    itself.
    """
    if facts.period_start is None:
        return None
    prior = run.db.execute(
        select(StatementRecord.verification).where(
            StatementRecord.household_id == run.household_id,
            StatementRecord.account_id == account.id,
            StatementRecord.document_id == run.document.id,
            StatementRecord.kind == "bank",
            StatementRecord.outcome == "imported",
            StatementRecord.period_start == facts.period_start,
        )
    ).scalar_one_or_none()
    if prior not in ("accepted", "reconciled"):
        return None
    found = _Standing(
        index=index,
        statement=statement,
        parsed=parsed,
        facts=facts,
        ref=ref,
        prior="accepted" if prior == "accepted" else "reconciled",
    )
    if found.prior == "reconciled" and found.vouched:
        return None
    return found


def _settle_standing(run: _Run, account: Account) -> None:
    """Decide the statements of ``account`` a person had settled (decision at the run's end).

    An acceptance, and a balance a person struck in the reconcile view, are
    answers about the ledger rows as they were. They hold while the run
    changed no row of the account (``_Run.changed``: none written, none
    removed as an earlier reading, none newly kept):

    * the record stays ``accepted``: nothing is asked again, no row is made
      pending, no balance is recorded;
    * the record stays ``reconciled`` while the ledger rows still add up to
      the statement's balances
      (``app.domain.review.reconciliation.ledger_status``).

    Otherwise the statement is what its reading says again: ``unverified``,
    with the reconciliation finding unless the reading adds up or is a clean
    read. An accepted statement becomes ``reconciled``, and its closing
    balance is recorded, only when the reading adds up **and** the ledger
    rows do: a reading that balances says nothing about rows a person
    edited since.
    """
    # Imported here: the review package imports this module.
    from app.domain.review.reconciliation import ledger_status

    held = run.standing.pop(account.id, [])
    if not held:
        return
    changed = account.id in run.changed
    statements = run.placed.get(account.id, [])
    for item in held:
        position = next(i for i, s in enumerate(statements) if s is item.statement)
        balanced = ledger_status(run.db, run.document, account, statements, position) == "balanced"
        verdict: Verification
        if item.vouched:
            verdict = "reconciled" if balanced else "unverified" if changed else item.prior
        elif not changed and (item.prior == "accepted" or balanced):
            verdict = item.prior
        else:
            verdict = "unverified"
        if verdict == item.prior:
            continue
        _record_committed(run, account, item.facts, verdict, lower_accepted=True)
        if verdict == "reconciled":
            _write_snapshot(run, item.parsed, account)
            continue
        if verdict == "unverified":
            # Reconciled until this run: the balance it recorded goes with it.
            _withdraw(run, account, item.statement)
        if not item.clean:
            _reconciliation_review(
                run, item.parsed, account, statement_index=item.index, ref=item.ref
            )


class _GroupFacts(BaseModel):
    """One account's rows of a multi-account table as read, for :func:`_group_key`."""

    kind: Literal["table_group"] = "table_group"
    last4: str | None
    name: str | None
    #: (row, date, amount) of every transaction of the group.
    rows: list[tuple[int, str, str]]


def _group_key(parsed: ParsedStatement) -> str:
    """The digest of one statement of a multi-account table as read (``statement_answers``).

    Like a statement's key it is of the statement exactly as read: its
    account (last four digits or name), and the row, date and amount of
    each transaction. The same stored table read with the same mapping
    gives the same key, so the account a person put the group on is found
    again by every later run; read with another mapping the group is asked
    about anew. Only the digest is ever stored, never the name.
    """
    return statement_key(
        _GroupFacts(
            last4=parsed.last4,
            name=parsed.account_name,
            rows=[
                (t.row if t.row is not None else -1, t.posted_date.isoformat(), str(t.amount))
                for t in parsed.transactions
            ],
        )
    )


@dataclass
class _Target:
    """One statement of a table and the account it goes to (None: not known)."""

    parsed: ParsedStatement
    account: Account | None
    candidates: tuple[uuid.UUID, ...] = ()
    #: Index in ``from_table_grouped`` (multi-account tables only).
    group: int | None = None


def _table_targets(
    run: _Run,
    statements: list[ParsedStatement],
    spec: MappingSpec,
    *,
    mapping_account: uuid.UUID | None,
    account_id: uuid.UUID | None,
    group: int | None = None,
) -> list[_Target]:
    """The account of each statement of a table (module doc, CSV/TSV).

    A statement of a multi-account table goes to the account the run
    assigns to it (the Accounts dialog), else the one chosen for it on
    this run (``account_id`` with ``group``), else the one an earlier
    answer put it on (:func:`_group_key`, ``statement_answers``), else the
    one its digits match. Without the third, a file split by account
    names could never be finished one question at a time: answering one
    name would raise the other again.
    """
    if spec.account_column is None:
        target = (
            run.assigned.get(("table", 0))
            or account_id
            or run.document.account_id
            or mapping_account
        )
        return [_Target(statements[0], _usable_account(run, target))]
    readable = [s for s in statements if s.transactions]
    single = len(readable) == 1
    matches = [
        match_account(run.db, run.household_id, last4=parsed.last4, allowed=run.allowed)
        for parsed in readable
    ]
    chosen = _usable_account(run, account_id)
    takes = (
        set(range(len(readable))) if single else _chosen_groups(readable, matches, chosen, group)
    )
    targets: list[_Target] = []
    for index, parsed in enumerate(readable):
        account: Account | None = None
        candidates: tuple[uuid.UUID, ...] = ()
        assigned = run.assigned.get(("group", index))
        if assigned is None and not (chosen is not None and index in takes):
            assigned = run.answers.account("statement", _group_key(parsed))
        if assigned is not None:
            account = _usable_account(run, assigned)
            if account is not None:
                targets.append(_Target(parsed, account, (), index))
                continue
        if single and account_id is None:
            account = _usable_account(run, run.document.account_id or mapping_account)
        if account is None:
            match = matches[index]
            candidates = match.candidates
            if chosen is not None and index in takes and (match.account_id is None or single):
                account = chosen
            else:
                account = _usable_account(run, match.account_id)
        targets.append(_Target(parsed, account, candidates, index))
    return targets


def _chosen_groups(
    readable: Sequence[ParsedStatement],
    matches: Sequence[AccountMatch],
    chosen: Account | None,
    group: int | None,
) -> set[int]:
    """The statements of a multi-account table that an account chosen on re-run may take.

    The one the person chose it for (``group``, from the review item), else
    those with the chosen account's own last four digits when it records
    them, else the one statement no account matched when it is the only
    one: any other unmatched statement keeps its ``unknown_account`` item
    rather than land on an account of a different number (like
    ``app.domain.investments.statement_import``).
    """
    if chosen is None:
        return set()
    if group is not None:
        return {group}
    if chosen.number_last4 is not None:
        return {
            index for index, parsed in enumerate(readable) if parsed.last4 == chosen.number_last4
        }
    unmatched = [index for index, match in enumerate(matches) if match.account_id is None]
    return set(unmatched) if len(unmatched) == 1 else set()


def _may_save_mappings(run: _Run) -> bool:
    """Whether the person behind the run could save a mapping themselves (member+)."""
    actor = run.user_id or run.document.uploaded_by
    if actor is None:
        return False
    role = run.db.execute(
        select(HouseholdMember.role).where(
            HouseholdMember.household_id == run.household_id, HouseholdMember.user_id == actor
        )
    ).scalar_one_or_none()
    return role is not None and role_at_least(role, HouseholdRole.MEMBER)


def _held(run: _Run, result: ExtractResult, suggestion: MappingSuggestion | None) -> str | None:
    """Why a suggestion is not imported on its own (None: it may be)."""
    if suggestion is None:
        return "no_suggestion"
    if not suggestion.confident:
        # The columns are found and only the sign is unproven: the same
        # question as a sign on trust that meets a card account.
        return "sign_unverified" if suggestion.doubts == ("sign_unverified",) else "not_confident"
    if result.kind not in AUTO_MAPPING_KINDS:
        return "not_delimited"
    if not _may_save_mappings(run):
        return "no_member"
    return None


def _sign_held(on_trust: bool, targets: list[_Target], doubted: bool = False) -> list[_Target]:
    """The statements of a table that wait because nothing proves their sign.

    A sign taken on trust is never used to write into a liability account
    (a card export may list purchases as positive), and it holds back that
    account's statement only: the other accounts of the file are imported.

    ``on_trust``: a suggestion without ``sign_evidence``, or a mapping saved
    from one (``sign_unverified``). Statements whose account is not known
    yet are left to their ``unknown_account`` / ``account_required`` item;
    the mapping saved meanwhile keeps ``sign_unverified``, so the re-run
    after a person picks a liability account holds that statement here.

    ``doubted``: the suggestion for this very table doubts the sign
    (``sign_unverified``: a card-like header, no money out, more money
    in than out). A mapping still on trust is not used for such a
    file whatever the account, which also stops a mapping saved from a card
    export before those doubts existed: every statement waits.

    Empty when the sign is proven (or a person saved or chose the mapping).
    """
    if not on_trust:
        return []
    if doubted:
        return list(targets)
    return [t for t in targets if t.account is not None and t.account.type in LIABILITY_TYPES]


def _answered_signs(run: _Run, targets: list[_Target], waiting: list[_Target]) -> dict[int, Sign]:
    """The sign a person answered for a target's account, by the target's place in ``targets``.

    ``statement_answers``: "what does a positive amount mean" answered for
    one account of this document. Such a statement is never held for its
    sign again and is always read with the answered sign, whatever the
    mapping says by then, so its rows keep the hashes they were imported
    under.

    An answer counts only for a statement the question can be about: one
    of ``waiting`` (:func:`_sign_held`), or one on a liability account
    (held before a person saved the mapping). An answer naming any other
    account is ignored: that account's rows were imported as the file
    prints them, and reading them the other way round now would add every
    one of them again with its sign turned.
    """
    found: dict[int, Sign] = {}
    for index, target in enumerate(targets):
        if target.account is None:
            continue
        sign = run.answers.signs.get(target.account.id)
        if sign is None:
            continue
        if target.account.type in LIABILITY_TYPES or any(target is other for other in waiting):
            found[index] = Sign(sign)
    return found


def _read_with_sign(
    table: Table, spec: MappingSpec, parsed: ParsedStatement, sign: Sign
) -> ParsedStatement | None:
    """``parsed`` (a statement of ``table`` read with ``spec``) read with ``sign`` instead.

    The same statement is the one made of the same table rows. None when
    the other sign groups the rows differently, which no table read so far
    does: the caller holds the statement rather than guess.
    """
    rows = [t.row for t in parsed.transactions]
    for other in from_table_grouped(table, spec.model_copy(update={"sign": sign})):
        if other.last4 == parsed.last4 and [t.row for t in other.transactions] == rows:
            return other
    return None


def _suggested_name(header: Sequence[str]) -> str:
    """A mapping name from the header cells (formula guard and odd characters removed)."""
    cells = []
    for cell in header:
        text = "".join(ch for ch in cell.removeprefix("'") if ch.isprintable())
        text = " ".join(text.split())
        if text:
            cells.append(text)
    name = ", ".join(cells) or "CSV layout"
    if len(name) > SUGGESTED_NAME_MAX:
        name = name[: SUGGESTED_NAME_MAX - 1].rstrip(", ") + "\u2026"
    return name


def _unique_name(run: _Run, base: str) -> str:
    if not repo.name_taken(run.db, run.household_id, base):
        return base
    for number in range(2, 100):
        candidate = f"{base[: NAME_MAX - 6]} ({number})"
        if not repo.name_taken(run.db, run.household_id, candidate):
            return candidate
    return f"{base[: NAME_MAX - 12]} ({uuid.uuid4().hex[:8]})"


def _save_suggested(run: _Run, table: Table, suggestion: MappingSuggestion) -> ImportMapping | None:
    """Save the suggestion as a household mapping for the table's header (None: not saved).

    Without a header row the row at ``skip_rows`` is the first transaction:
    the mapping is named by the table's column count, never by those cells.
    """
    spec = suggestion.spec
    fingerprint = spec_fingerprint(table, spec)
    if fingerprint is None:  # pragma: no cover - a confident suggestion has a header row
        return None
    actor = run.user_id or run.document.uploaded_by
    name = (
        _suggested_name(table.rows[spec.skip_rows])
        if spec.has_header
        else f"No header, {table.column_count} columns"
    )
    mapping = ImportMapping(
        household_id=run.household_id,
        name=_unique_name(run, name),
        header_fingerprint=fingerprint,
        spec=spec.model_dump(mode="json"),
        created_by=actor,
        sign_unverified=suggestion.sign_evidence is None,
    )
    try:
        with run.db.begin_nested():
            run.db.add(mapping)
            run.db.flush()
    except IntegrityError:  # another import saved the same name meanwhile
        return None
    audit(
        run.db,
        action="import_mapping.create",
        target_type="import_mapping",
        target_id=mapping.id,
        after={**_facts(mapping), "from_document": run.document.id, "origin": "suggested"},
        household_id=run.household_id,
        user_id=actor,
    )
    emit(run.db, run.household_id, "document.mapping_created", {"id": str(mapping.id)})
    return mapping


#: The first filled cell of a total line, whole: a total word alone, with
#: punctuation after it, or with one of the tails a summary line prints
#: (``Total for period``, ``Total debits``). A payee that only starts with
#: the word (``Total Wine Shop``, ``Balance Transfer Fee``) is no total.
_TOTAL_LABEL: Final = re.compile(
    r"(?:totals?|(?:(?:ending|closing)\s+)?balance)"
    r"(?:\s*[:.\-]*|\s+(?:for|of|as\s+of|amount|debits?|credits?|deposits?|withdrawals?)\b.*)",
    re.IGNORECASE,
)
#: A cell that may be a date: digits around two separators, eight digits in
#: a row, or a month name beside a digit. Wide on purpose: a row with such
#: a cell is never called a total line.
_DATE_SHAPED: Final = re.compile(
    r"[0-9]{1,4}\s*[/.\-]\s*[0-9]{1,2}\s*[/.\-]\s*[0-9]{1,4}"
    r"|(?<![0-9])[0-9]{8}(?![0-9])"
    r"|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s*[0-9]"
    r"|[0-9](?:st|nd|rd|th)?\s*(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)",
    re.IGNORECASE,
)


def _date_shaped(cell: str) -> bool:
    return _DATE_SHAPED.search(cell) is not None or best_date_format([cell])[1] > 0


def _total_line(row: Sequence[str]) -> bool:
    """A row that only sums up: a total's label (:data:`_TOTAL_LABEL`) and no date."""
    cells = [cell.strip() for cell in row if cell.strip()]
    if not cells or _TOTAL_LABEL.fullmatch(cells[0]) is None:
        return False
    return not any(_date_shaped(cell) for cell in cells)


def header_only(table: Table) -> bool:
    """A table that is a header row and nothing to import under it (or nothing at all).

    One row with text, at least two cells of it filled and none with a
    digit: column names. A single row with a digit in it may be the file's
    only transaction (an export without a header) and is never called
    empty; it is asked about like any other table.

    Under such a header, rows that are all total lines (:func:`_total_line`:
    ``Total,,0.00``, ``Ending Balance,,1204.18``) are the export of a period
    without a transaction: there is no row to map either. One row with a
    date-shaped cell, or one that starts with anything else, and the table
    is asked about as before.
    """
    rows = [row for row in table.rows if any(cell.strip() for cell in row)]
    if not rows:
        return True
    cells = [cell for cell in rows[0] if cell.strip()]
    if len(cells) < 2 or any(ch.isdigit() for cell in cells for ch in cell):
        return False
    return all(_total_line(row) for row in rows[1:])


def _mapping_review(
    run: _Run,
    table: Table,
    choice: MappingChoice,
    suggestion: MappingSuggestion | None,
    held: str | None,
    **facts: Any,
) -> None:
    """Ask for the table's columns (or, ``held == "sign_unverified"``, its sign).

    ``facts``: the keys a sign question about one statement adds
    (:func:`_sign_review`).
    """
    run.review(
        ReviewKind.UNPARSED,
        {
            "reason": "mapping_ambiguous" if choice.ambiguous else "mapping_required",
            "table": 0,
            "header_fingerprint": header_fingerprint(table.rows[0]) if table.rows else None,
            "column_count": table.column_count,
            "row_count": len(table.rows),
            "candidates": [str(c) for c in choice.ambiguous[:IDS_MAX]],
            "suggested_spec": (
                suggestion.spec.model_dump(mode="json") if suggestion is not None else None
            ),
            "suggestion": suggestion.summary() if suggestion is not None else None,
            "auto_import": held,
            **facts,
        },
    )


def _sign_review(
    run: _Run,
    table: Table,
    choice: MappingChoice,
    suggestion: MappingSuggestion | None,
    target: _Target,
    *,
    whole: bool,
    mapping: ImportMapping | None,
) -> None:
    """One statement of the table waits until a person says what a positive amount means.

    ``scope`` says what the answer is for: ``mapping`` when every statement
    of the table waits (``whole``: the answer is the layout's sign, saved on
    its mapping, so the layout is asked about once), ``account`` when the
    others were imported (the answer is kept for this account of this
    document, ``statement_answers``). Ids and counts only.
    """
    _mapping_review(
        run,
        table,
        choice,
        suggestion,
        "sign_unverified",
        account_id=str(target.account.id) if target.account is not None else None,
        group=target.group,
        transactions=len(target.parsed.transactions),
        scope="mapping" if whole else SCOPE_ACCOUNT,
        **({"mapping_id": str(mapping.id)} if mapping is not None else {}),
    )


def _import_table(
    run: _Run,
    result: ExtractResult,
    *,
    mapping_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
    group: int | None = None,
) -> None:
    table = result.tables[0]
    target = account_id or run.document.account_id
    choice: MappingChoice | None = None
    if mapping_id is not None:
        mapping = repo.get_mapping(run.db, run.household_id, mapping_id)
        spec = _spec(mapping) if mapping is not None else None
        if mapping is not None and spec is not None and _fits(table, spec):
            choice = MappingChoice(mapping, spec)
    if choice is None:
        choice = choose_mapping(
            run.db, run.household_id, table, account_id=target, allowed=run.allowed
        )
    saved, spec = choice.mapping, choice.spec
    suggestion: MappingSuggestion | None = None
    if spec is not None and spec.mode == "activity":
        # An activity mapping that did not read the table (it no longer
        # fits): asked for again rather than read as a cash ledger.
        saved, spec = None, None
    if saved is None or spec is None:
        suggestion = (
            None
            if choice.ambiguous
            else suggest_table_mapping(table, received_on=_received_on(run.document))
        )
        held = "mapping_ambiguous" if choice.ambiguous else _held(run, result, suggestion)
        if held is None and suggestion is not None and suggestion.spec.mode == "activity":
            # A brokerage layout whose actions are mostly unknown: a person
            # confirms the activity mapping; it is never saved on a guess.
            held = "activity_unconfirmed"
        if held is not None or suggestion is None:
            _mapping_review(run, table, choice, suggestion, held)
            return
        saved, spec = None, suggestion.spec
    statements = from_table_grouped(table, spec)
    if not any(s.transactions for s in statements):
        if saved is None:  # pragma: no cover - a confident suggestion reads rows
            _mapping_review(run, table, choice, suggestion, "no_rows")
            return
        run.mapping_id = saved.id
        run.review(
            ReviewKind.UNPARSED,
            {
                "reason": "mapping_failed",
                "table": 0,
                "mapping_id": str(saved.id),
                "skipped_rows": sum(len(s.skipped_rows) for s in statements),
            },
        )
        return
    targets = _table_targets(
        run,
        statements,
        spec,
        mapping_account=saved.account_id if saved is not None else None,
        account_id=account_id,
        group=group,
    )
    asked: MappingSuggestion | None
    if saved is None:
        if suggestion is None:  # pragma: no cover - set whenever no mapping was saved
            return
        asked = suggestion
        waiting = _sign_held(suggestion.sign_evidence is None, targets)
    else:
        # A mapping saved from a trust-only suggestion is checked again
        # whenever it meets a liability account or a file whose sign the
        # suggestion doubts, unless the person chose it. One a person saved
        # (``sign_unverified`` False) is their answer for the layout: it
        # holds nothing, whatever a fresh suggestion would doubt.
        on_trust = saved.sign_unverified and mapping_id is None
        asked = suggest_mapping(table, today=_received_on(run.document)) if on_trust else None
        doubted = asked is not None and "sign_unverified" in asked.doubts
        waiting = _sign_held(on_trust, targets, doubted)
    # What a person answered for an account of this document settles its sign.
    answered = _answered_signs(run, targets, waiting)
    unsettled = [
        place
        for place, item in enumerate(targets)
        if place not in answered and any(item is other for other in waiting)
    ]
    if unsettled and len(unsettled) == len(targets):
        # Every statement waits: nothing is imported and no mapping is
        # saved; the answer is for the layout.
        for place in unsettled:
            _sign_review(run, table, choice, asked, targets[place], whole=True, mapping=saved)
        return
    if saved is None:
        mapping = _save_suggested(run, table, suggestion) if suggestion is not None else None
        if mapping is None:
            _mapping_review(run, table, choice, suggestion, "not_saved")
            return
        run.mapping_suggested = True
    else:
        mapping = saved
    run.mapping_id = mapping.id
    committed = False
    for place, item in enumerate(targets):
        if place in unsettled:
            # Only this account's rows wait; the others go on below.
            _not_committed(run, item)
            _sign_review(run, table, choice, asked, item, whole=False, mapping=mapping)
            continue
        sign = answered.get(place)
        if sign is not None and sign is not spec.sign:
            reread = _read_with_sign(table, spec, item.parsed, sign)
            if reread is None:  # pragma: no cover - the sign never regroups rows
                _not_committed(run, item)
                _sign_review(run, table, choice, asked, item, whole=False, mapping=mapping)
                continue
            item = replace(item, parsed=reread)
        if item.account is None:
            _not_committed(run, item)
        if item.account is None and item.group is None:
            run.review(
                ReviewKind.UNKNOWN_ACCOUNT,
                {
                    "reason": "account_required",
                    "mapping_id": str(mapping.id),
                    "currency": item.parsed.currency,
                    "transactions": len(item.parsed.transactions),
                },
            )
            continue
        if item.account is None:
            run.review(
                ReviewKind.UNKNOWN_ACCOUNT,
                {
                    "reason": "account_ambiguous" if item.candidates else "account_not_found",
                    "table": 0,
                    "group": item.group,
                    "mapping_id": str(mapping.id),
                    "spec_digest": spec_digest(spec),
                    "currency": item.parsed.currency,
                    "transactions": len(item.parsed.transactions),
                    "candidates": [str(c) for c in item.candidates[:IDS_MAX]],
                },
            )
            continue
        verification = _commit_statement(run, item.parsed, item.account, statement_index=None)
        if verification is not None:
            closing = item.parsed.closing_balance
            _record_committed(
                run,
                item.account,
                table_facts(
                    item.parsed.source_format,
                    item.parsed.period_start,
                    item.parsed.period_end,
                    closing.amount if closing is not None else None,
                ),
                verification,
            )
        committed = True
    if not committed:
        return
    repo.mark_used(run.db, mapping)
    if run.document.institution_id is None and mapping.institution_id is not None:
        run.document.institution_id = mapping.institution_id


def _not_committed(run: _Run, item: _Target) -> None:
    """A statement of the table this run does not commit: no row of it is replaced."""
    if item.account is None:
        run.unplaced = True
    else:
        run.waiting.add(item.account.id)


def _received_on(document: Document) -> date | None:
    """The day (UTC) the document was uploaded: no row of it is dated later."""
    received = document.created_at
    return received.astimezone(UTC).date() if received is not None else None


def suggest_table_mapping(
    table: Table, *, received_on: date | None = None
) -> MappingSuggestion | None:
    """The mapping editor's suggestion: an activity mapping for a brokerage layout, else cash.

    ``received_on`` is the day the file arrived, which settles a day/month
    order that would date rows after it (:func:`suggest_mapping`).
    """
    return suggest_activity_mapping(table) or suggest_mapping(table, today=received_on)


def spec_digest(spec: MappingSpec) -> str:
    """A digest of ``spec`` (hex), recorded on the review items that name a group of it."""
    data = spec.model_dump(mode="json")
    # Fields added after the digest was introduced count only when set, so
    # items raised before they existed keep their digest.
    for name in ("mode", *ACTIVITY_COLUMNS):
        if data.get(name) in (None, "cash"):
            data.pop(name, None)
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _item_spec(db: Session, document: Document, payload: dict[str, Any]) -> MappingSpec | None:
    """The mapping spec a grouped review item was raised with (None: gone or changed since)."""
    raw = payload.get("mapping_id")
    if not isinstance(raw, str):
        return None
    try:
        mapping_id = uuid.UUID(raw)
    except ValueError:
        return None
    mapping = repo.get_mapping(db, document.household_id, mapping_id)
    spec = _spec(mapping) if mapping is not None else None
    if spec is None or payload.get("spec_digest") != spec_digest(spec):
        return None
    return spec


def group_item_current(db: Session, document: Document, payload: dict[str, Any]) -> bool:
    """Whether a grouped ``unknown_account`` item still names the group it was raised for.

    The group index points into the statements its mapping reads; once the
    mapping's spec has changed (``spec_digest``), it may point at another
    account's rows.
    """
    return isinstance(payload.get("group"), int) and _item_spec(db, document, payload) is not None


def stored_group_last4(db: Session, document: Document, payload: dict[str, Any]) -> str | None:
    """The last four digits of a multi-account table's statement named by a review item.

    ``payload`` is an ``unknown_account`` item of :func:`_import_table`
    (``mapping_id``, ``group``, ``spec_digest``); the digits are read again
    from the stored table, so the item itself never carries them. None when
    the mapping's spec changed since the item was raised
    (:func:`group_item_current`).
    """
    group = payload.get("group")
    if not isinstance(group, int):
        return None
    spec = _item_spec(db, document, payload)
    result = _stored_result(document)
    if spec is None or result is None or not result.tables:
        return None
    return group_last4(result.tables[0], spec, group)


def activity_table(result: ExtractResult, spec: MappingSpec | None = None) -> ActivityTable | None:
    """Table 0 of a CSV/TSV/XLSX read as brokerage activity (None: it is not one).

    Only a delimited or spreadsheet file's own table qualifies (never a
    PDF's page table, nor a file that also carries statements). ``spec`` is
    an activity-mode mapping a person saved or chose for the layout; without
    one the table must be recognised on its own (``brokerage_table``).
    """
    if (
        result.statements
        or result.investments
        or not result.tables
        or result.kind not in ACTIVITY_TABLE_KINDS
    ):
        return None
    if spec is not None and (spec.mode != "activity" or not _fits(result.tables[0], spec)):
        return None
    return read_activity_table(result.tables[0], spec)


def positions_table(result: ExtractResult, document: Document) -> PositionsTable | None:
    """Table 0 of a CSV/TSV/XLSX read as a brokerage positions export (None: it is not one).

    Only a delimited or spreadsheet file's own table qualifies (never a
    PDF's page table, nor a file that also carries statements). The day the
    file was received dates the positions when the export prints no
    download date.
    """
    if (
        result.statements
        or result.investments
        or not result.tables
        or result.kind not in POSITIONS_TABLE_KINDS
    ):
        return None
    received = document.created_at.date() if document.created_at is not None else None
    return read_positions_table(result.tables[0], received_on=received)


def saved_activity_mapping(
    db: Session,
    household_id: uuid.UUID,
    table: Table,
    *,
    account_id: uuid.UUID | None,
    allowed: Collection[uuid.UUID] | None,
) -> MappingChoice | None:
    """The saved activity-mode mapping for ``table`` (None: none, or a cash one fits)."""
    choice = choose_mapping(db, household_id, table, account_id=account_id, allowed=allowed)
    if choice.mapping is None or choice.spec is None or choice.spec.mode != "activity":
        return None
    return choice


def stored_investments(document: Document, db: Session | None = None) -> list[InvestmentStatement]:
    """The investment statements an import of ``document`` reads, in the importer's order.

    The stored ones (OFX, PDF), else those of a brokerage positions export
    or activity table, read again from the stored table (they are never
    stored themselves): as recognised on its own, else (activity) through
    the household's saved activity-mode mapping for the layout (with ``db``).
    """
    result = _stored_result(document)
    if result is None:
        return []
    if result.investments:
        return list(result.investments)
    positions = positions_table(result, document)
    if positions is not None:
        return positions.statements
    found = activity_table(result)
    if found is None and db is not None and result.tables:
        choice = saved_activity_mapping(
            db,
            document.household_id,
            result.tables[0],
            account_id=document.account_id,
            allowed=None,
        )
        if choice is not None:
            found = activity_table(result, choice.spec)
    return found.statements if found is not None else []


def stored_investment_last4(
    document: Document, payload: dict[str, Any], db: Session | None = None
) -> str | None:
    """The last four digits of the investment statement an ``unknown_account`` item names.

    ``payload`` is an item of ``statement_import`` (``investment``,
    ``statement``); the digits are read again from the stored extraction, so
    the item itself never carries them.
    """
    index = payload.get("statement")
    if payload.get("investment") is not True or not isinstance(index, int):
        return None
    statements = stored_investments(document, db)
    return statements[index].account.last4 if 0 <= index < len(statements) else None


def _activity_reading(
    run: _Run,
    result: ExtractResult,
    mapping_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
) -> tuple[ActivityTable | None, ImportMapping | None]:
    """Table 0 read as brokerage activity, and the activity mapping that read it.

    A mapping chosen for this run reads the table as activity when it is an
    activity-mode mapping, as a cash ledger otherwise (None). Without one,
    the table recognised on its own wins, then a saved activity-mode mapping
    for the layout.
    """
    if mapping_id is not None:
        mapping = repo.get_mapping(run.db, run.household_id, mapping_id)
        spec = _spec(mapping) if mapping is not None else None
        if mapping is None or spec is None or spec.mode != "activity":
            return None, None
        return activity_table(result, spec), mapping
    found = activity_table(result)
    if found is not None:
        return found, None
    choice = saved_activity_mapping(
        run.db,
        run.household_id,
        result.tables[0],
        account_id=account_id or run.document.account_id,
        allowed=run.allowed,
    )
    if choice is None or choice.mapping is None:
        return None, None
    return activity_table(result, choice.spec), choice.mapping


def _import_activity(
    run: _Run,
    result: ExtractResult,
    activity: ActivityTable,
    *,
    account_id: uuid.UUID | None,
    mapping: ImportMapping | None = None,
) -> None:
    """Record what reading table 0 as brokerage activity did (module doc, precedence)."""
    run.activity = activity
    # The document says which reader made its records (the upload list then
    # offers the review queue, not the cash-ledger column mapping).
    run.document.parser_version = f"{ACTIVITY_PARSER}/{ACTIVITY_PARSER_VERSION}"
    if mapping is not None:
        run.mapping_id = mapping.id
        repo.mark_used(run.db, mapping)
    else:
        choice = choose_mapping(
            run.db,
            run.household_id,
            result.tables[0],
            account_id=account_id or run.document.account_id,
            allowed=run.allowed,
        )
        if choice.mapping is not None and (choice.spec is None or choice.spec.mode != "activity"):
            run.mapping_bypassed = choice.mapping.id
        elif choice.ambiguous:
            run.mapping_bypassed = choice.ambiguous[0]
    missed = activity.unknown_activity + activity.skipped
    if not activity.statements:
        run.review(
            ReviewKind.UNPARSED,
            {
                "reason": "activity_unreadable",
                "table": 0,
                "parser": ACTIVITY_PARSER,
                "unknown_activity": activity.unknown_activity,
                "unknown_actions": [
                    {"action": action, "rows": rows}
                    for action, rows in activity.unknown_actions.items()
                ],
                "skipped_rows": activity.skipped,
                "header_row": activity.header.row,
                "unknown_rows": activity.unknown_rows[:IDS_MAX],
                "unreadable_rows": activity.unreadable_rows[:IDS_MAX],
            },
        )
    elif missed:
        run.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "lines_skipped",
                "count": missed,
                "unknown_activity": activity.unknown_activity,
                "unknown_actions": [
                    {"action": action, "rows": rows}
                    for action, rows in activity.unknown_actions.items()
                ],
                "parser": ACTIVITY_PARSER,
                "header_row": activity.header.row,
                "unknown_rows": activity.unknown_rows[:IDS_MAX],
                "unreadable_rows": activity.unreadable_rows[:IDS_MAX],
            },
        )
    dates = activity_dates(activity.statements)
    if dates is not None and run.document.period_start is None:
        run.document.period_start, run.document.period_end = dates


def _import_positions(run: _Run, positions: PositionsTable) -> None:
    """Record what reading table 0 as a brokerage positions export did (module doc)."""
    run.positions = positions
    run.document.parser_version = f"{POSITIONS_PARSER}/{POSITIONS_PARSER_VERSION}"
    if not positions.statements:
        run.review(
            ReviewKind.UNPARSED,
            {
                "reason": "positions_unreadable",
                "table": 0,
                "parser": POSITIONS_PARSER,
                "skipped_rows": positions.skipped,
            },
        )
    elif positions.skipped:
        run.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "lines_skipped",
                "count": positions.skipped,
                "unknown_activity": 0,
                "unknown_actions": [],
                "parser": POSITIONS_PARSER,
            },
        )
    if positions.as_of is not None and run.document.period_start is None:
        run.document.period_start = run.document.period_end = positions.as_of


# ---------------------------------------------------------------------------
# A document read again: the rows of its earlier reading
# ---------------------------------------------------------------------------

#: Resolution of an open finding the run's own replacement answered.
_SUPERSEDED: Final = {"action": "superseded", "by": "import.replace"}


def _named_rows(payload: Mapping[str, Any]) -> set[str]:
    """The transaction ids a review payload names (its own row, or the rows it lists)."""
    named: set[str] = set()
    own = payload.get("transaction_id")
    if isinstance(own, str):
        named.add(own)
    for key in ("transactions", "rows"):
        listed = payload.get(key)
        if not isinstance(listed, list):
            continue
        for entry in listed:
            found = entry.get("transaction_id") if isinstance(entry, dict) else None
            if isinstance(found, str):
                named.add(found)
    return named


def _supersede(run: _Run, answered: Callable[[ReviewItem], bool]) -> int:
    """Dismiss the document's open findings ``answered`` picks; how many."""
    found = [
        item
        for item in repo.open_review_items(
            run.db, run.household_id, run.document.id, set(ReviewKind)
        )
        if answered(item)
    ]
    now = clock.utcnow()
    for item in found:
        item.state = ReviewItemState.DISMISSED
        item.resolved_at = now
        item.resolved_by = run.user_id
        item.resolution = dict(_SUPERSEDED)
    if found:
        run.db.flush()
        # The run's memory of what is open is read again.
        run._open = None
    return len(found)


def _answered_before(run: _Run, finding: Mapping[str, Any], *, dismissed: bool = False) -> bool:
    """A resolved finding of the document says exactly what ``finding`` says.

    With ``dismissed``, one a person dismissed counts too (not one a run or
    "Process again" superseded), and the statement reference is not compared
    (:func:`_comparable`).
    """
    states = [ReviewItemState.RESOLVED] + ([ReviewItemState.DISMISSED] if dismissed else [])
    answered = run.db.execute(
        select(ReviewItem.payload, ReviewItem.resolution).where(
            ReviewItem.household_id == run.household_id,
            ReviewItem.document_id == run.document.id,
            ReviewItem.state.in_(states),
            ReviewItem.kind == ReviewKind.LOW_CONFIDENCE_FIELD,
        )
    ).all()
    if not dismissed:
        return any(payload == finding for payload, _ in answered)
    wanted = _comparable(finding)
    return any(
        _comparable(payload or {}) == wanted
        and (resolution or {}).get("action") != _SUPERSEDED["action"]
        for payload, resolution in answered
    )


def _person_linked(run: _Run, ids: Sequence[uuid.UUID]) -> set[uuid.UUID]:
    """Which of the transactions ``ids`` a person tied something to.

    A document attached to the row, a goal contribution or a loan payment
    recorded from it. None of these moves the row's ``updated_at``, and the
    last two would lose their link with the row (``ON DELETE SET NULL``).
    """
    if not ids:
        return set()
    db, household_id = run.db, run.household_id
    found = set(
        db.execute(
            select(DocumentLink.target_id).where(
                DocumentLink.household_id == household_id,
                DocumentLink.target_type == LinkTarget.TRANSACTION,
                DocumentLink.target_id.in_(ids),
            )
        ).scalars()
    )
    for model in (GoalContribution, LoanPayment):
        found.update(
            linked
            for linked in db.execute(
                select(model.transaction_id).where(
                    model.household_id == household_id, model.transaction_id.in_(ids)
                )
            ).scalars()
            if linked is not None
        )
    return found


def _replace_earlier(
    run: _Run, account: Account, *, moved: Collection[str] | None = None
) -> set[uuid.UUID]:
    """Settle the rows an earlier reading of the document left on ``account``.

    Called once per account, when every statement of the document for it was
    committed by this run. An earlier row
    (``app.domain.review.reconciliation.earlier_rows_of``: a row of this
    document with a hash the run did not read) is **removed** when nobody
    touched it: ``updated_at == created_at``, no transfer link, and nothing
    a person tied to it (:func:`_person_linked`: a document, a goal
    contribution, a loan payment). No tombstone is written: the row was
    never the person's to
    delete, and a later reading that holds it again imports it. Every other
    earlier row is **kept** and listed in one ``earlier_rows_kept`` finding
    of the account (ids only, at most :data:`IDS_MAX`), which the reconcile
    view answers; a list a person already answered is not raised again.
    Audited as ``import.replace``. Returns the ids removed.

    ``moved`` (:func:`_settle_moved`): the account got no statement of this
    reading, and these are the hashes the rows the run committed elsewhere
    would have on it. Every row of the document on the account is then an
    earlier row, and the account is settled only when each of them is one
    of ``moved``; else nothing is done.

    Never touched here: rows of another document, rows on another account
    that the run did not read again, rows without an ``import_hash`` (added
    by hand for the document).
    """
    # Imported here: the review package imports this module.
    from app.domain.review.reconciliation import EARLIER_KEPT, earlier_rows_of

    db = run.db
    read = run.read[account.id] if moved is None else set()
    earlier = earlier_rows_of(db, run.document, account.id, read, lock=True)
    if moved is not None and any(row.import_hash not in moved for row in earlier):
        return set()
    attached = _person_linked(run, [row.id for row in earlier])
    removed: list[uuid.UUID] = []
    kept: list[uuid.UUID] = []
    for row in earlier:
        untouched = (
            row.updated_at == row.created_at
            and row.transfer_group_id is None
            and row.id not in attached
        )
        if untouched:
            removed.append(row.id)
            transactions_repo.delete_transaction(db, row)
            db.expunge(row)
        else:
            kept.append(row.id)
    run.replaced += len(removed)
    run.kept += len(kept)
    finding = {
        "reason": EARLIER_KEPT,
        "account_id": str(account.id),
        "count": len(kept),
        "transactions": [{"transaction_id": str(i)} for i in kept[:IDS_MAX]],
    }
    gone = {str(i) for i in removed}

    def answered(item: ReviewItem) -> bool:
        payload = item.payload or {}
        if payload.get("reason") == EARLIER_KEPT:
            # An earlier list of kept rows that no longer says what is kept.
            return payload.get("account_id") == str(account.id) and payload != finding
        named = _named_rows(payload)
        # A finding about rows that are all gone with the earlier reading.
        return bool(named) and named <= gone

    _supersede(run, answered)
    if removed:
        run.changed.add(account.id)
    if kept and not _answered_before(run, finding):
        # Listed once: a person who accepted or balanced the statement with
        # these very rows kept is not asked about them on every later run.
        run.changed.add(account.id)
        run.review(ReviewKind.LOW_CONFIDENCE_FIELD, finding)
    if removed or kept:
        audit(
            db,
            action="import.replace",
            target_type="document",
            target_id=run.document.id,
            after={
                "account_id": account.id,
                "replaced": len(removed),
                "kept": len(kept),
                "transaction_ids": [str(i) for i in removed[:IDS_MAX]],
                "kept_ids": [str(i) for i in kept[:IDS_MAX]],
            },
            household_id=run.household_id,
            user_id=run.user_id,
        )
    if removed:
        emit(
            db,
            run.household_id,
            "transaction.deleted",
            {
                "document_id": str(run.document.id),
                "account_id": str(account.id),
                "count": len(removed),
                "ids": [str(i) for i in removed[:IDS_MAX]],
            },
        )
    return set(removed)


def _settle_near(run: _Run, account: Account, removed: Collection[uuid.UUID]) -> None:
    """Raise the near-duplicate findings held back for ``account`` (``_Run.near_waiting``).

    A row that resembled only rows the run has just removed as an earlier
    reading (the same line, read with another description) is no duplicate
    of anything: it is posted like its neighbours unless it waits for a
    doubt of its own, and no finding is raised for it. A row that resembled
    a row a person deleted (no candidate from the start) still waits.
    """
    released: list[Transaction] = []
    for group in run.near_waiting.pop(account.id, []):
        still: list[tuple[Transaction, list[uuid.UUID]]] = []
        for row, candidates, doubted in group.rows:
            left = [c for c in candidates if c not in removed]
            if candidates and not left:
                run.possible_duplicates -= 1
                if not doubted:
                    row.review_state = ReviewState.NONE
                    released.append(row)
                continue
            still.append((row, left))
        _near_duplicates_review(run, account, still, statement_index=group.statement_index)
    _mark_untouched(run.db, released)


def _finish_reading(run: _Run) -> None:
    """After every statement of the run: replace earlier readings, account by account.

    An account is settled only when the run committed a statement to it and
    none of the document's statements for it waits (``_Run.waiting``), and
    only when every statement of the document found its account
    (``_Run.unplaced``): a statement that is held or unplaced may be the
    one whose rows would look like an earlier reading. Then the rows of the
    earlier reading go or are listed (:func:`_replace_earlier`) and the
    statement records of periods the run did not record are dropped
    (``statement_records.drop_stale_records``). An account whose reading
    says it may be incomplete (``_Run.incomplete``) is not settled either:
    what it no longer holds may be what it could not read. Then the
    statements a person had accepted or balanced are decided
    (:func:`_settle_standing`). Last, the accounts a statement was moved
    away from (:func:`_settle_moved`).
    """
    for account_id in list(run.read):
        account = accounts_repo.get_account(run.db, run.household_id, account_id)
        if account is None:  # pragma: no cover - the run has just written to it
            continue
        removed: set[uuid.UUID] = set()
        settled = account_id not in run.waiting and account_id not in run.incomplete
        _settle_openings(run, account, settled=not run.unplaced and settled)
        if not run.unplaced and settled:
            removed = _replace_earlier(run, account)
            if account_id in run.periods:
                drop_stale_records(run.db, run.document, account, run.periods[account_id])
        _settle_near(run, account, removed)
        _settle_standing(run, account)
    _settle_moved(run)


def _settle_openings(run: _Run, account: Account, *, settled: bool) -> None:
    """Record the opening balances of the run's QIF sections and tables on ``account``.

    Such a figure was a transaction until 1.0.0-rc.11 (its date, its
    amount); now it is the balance at the end of the day before
    (``opening.as_of``). It is recorded as a ``statement`` snapshot on that
    day when the account has nothing on or before it: no snapshot (but
    this document's own) and no transaction (but the opening row an
    earlier reading of this document wrote: that amount, on that day or
    the next).
    That row then goes as an earlier reading's (:func:`_replace_earlier`),
    so it is only replaced when this run settles the account and nobody
    touched it; otherwise it stays as it was and no snapshot is written,
    so the opening is counted once either way. With something already on
    or before the day, nothing is written: the row of an earlier reading
    stays, and without one an item names the figure
    (:data:`OPENING_BALANCE_NOT_RECORDED`), unless the account's balance
    that day already is the figure (an account created from the document's
    proposal, the previous statement's closing balance) or a person already
    resolved or dismissed that very item (:func:`_answered_before`).
    """
    openings = run.openings.pop(account.id, [])
    if not openings:
        return
    db = run.db
    read = run.read.setdefault(account.id, set())
    wanted: list[dict[str, Any]] = []
    for opening in openings:
        old = list(
            db.execute(
                select(Transaction).where(
                    Transaction.household_id == run.household_id,
                    Transaction.account_id == account.id,
                    Transaction.source_document_id == run.document.id,
                    Transaction.amount == opening.amount,
                    Transaction.posted_date.in_([opening.as_of, opening.as_of + timedelta(days=1)]),
                    Transaction.import_hash.is_not(None),
                )
            ).scalars()
        )
        old = [row for row in old if row.import_hash not in read]
        old_ids = [row.id for row in old]
        snapshots = accounts_repo.list_snapshots(db, account, date.min, opening.as_of)
        others = [
            s
            for s in snapshots
            if not (
                s.as_of_date == opening.as_of
                and s.source is BalanceSource.STATEMENT
                and s.source_document_id == run.document.id
            )
        ]
        earlier_row = db.execute(
            select(Transaction.id)
            .where(
                Transaction.household_id == run.household_id,
                Transaction.account_id == account.id,
                Transaction.posted_date <= opening.as_of,
                Transaction.id.not_in(old_ids) if old_ids else true(),
            )
            .limit(1)
        ).first()
        if not others and earlier_row is None:
            attached = _person_linked(run, old_ids)
            untouched = all(
                row.updated_at == row.created_at
                and row.transfer_group_id is None
                and row.id not in attached
                for row in old
            )
            if not old or (settled and untouched):
                accounts_repo.upsert_snapshot(
                    db,
                    account,
                    opening.as_of,
                    opening.amount,
                    BalanceSource.STATEMENT,
                    source_document_id=run.document.id,
                )
                run.snapshots += 1
                emit(db, run.household_id, "balance.updated", {"account_id": str(account.id)})
                continue
        if old:
            # The earlier reading's row stands in for the figure: kept.
            read.update(row.import_hash for row in old if row.import_hash is not None)
            continue
        # The ledger already says so (the balance a proposal recorded, the
        # previous statement's closing balance): nothing to ask.
        if balance_as_of(db, account, opening.as_of) == opening.amount:
            continue
        wanted.append(
            {
                "reason": OPENING_BALANCE_NOT_RECORDED,
                "field": "opening_balance",
                "account_id": str(account.id),
                "statement": opening.statement_index,
                "currency": account.currency,
                "amount": _decimal_text(opening.amount),
                "as_of": opening.as_of.isoformat(),
            }
        )
    still = [_comparable(payload) for payload in wanted]

    def answered(item: ReviewItem) -> bool:
        # An earlier run's item this run no longer raises (recorded, or the
        # row of an earlier reading stands in for it).
        payload = item.payload or {}
        return (
            payload.get("reason") == OPENING_BALANCE_NOT_RECORDED
            and payload.get("account_id") == str(account.id)
            and _comparable(payload) not in still
        )

    _supersede(run, answered)
    for payload in wanted:
        if not _answered_before(run, payload, dismissed=True):
            # A person who resolved or dismissed it is not asked on every run.
            run.review(ReviewKind.LOW_CONFIDENCE_FIELD, payload)


def _settle_moved(run: _Run) -> None:
    """Settle the accounts a statement of the document was moved away from.

    A person put a statement on another account (the Accounts dialog, an
    answer, a mapping's account), and the run committed it there: the rows
    its earlier reading left on the first account are the same lines a
    second time. Only when the run committed every statement of the
    document (none waits, none is without an account, no brokerage
    statement), and only for an account the actor may import into that got
    no statement of this reading. Every row of the document there must be a
    line the run committed elsewhere (the hash it would have on this
    account is one of the run's rows); then the untouched ones are removed
    and the others kept and listed, as :func:`_replace_earlier` does, and
    with no row of the document left the document's statement records of
    the account are dropped. An account with a row the new reading does not
    hold anywhere is left as it is: a statement the reading lost is not a
    statement that moved.
    """
    if run.unplaced or run.waiting or not run.read or not any(run.committed):
        return
    db = run.db
    holding = db.execute(
        select(Transaction.account_id)
        .where(
            Transaction.household_id == run.household_id,
            Transaction.source_document_id == run.document.id,
            Transaction.import_hash.is_not(None),
            Transaction.account_id.not_in(sorted(run.read)),
        )
        .distinct()
        .order_by(Transaction.account_id)
    ).scalars()
    for account_id in list(holding):
        account = _usable_account(run, account_id)
        if account is None:
            continue
        moved = {
            value for rows in run.committed for value in assign_import_hashes(account.id, rows)
        }
        _replace_earlier(run, account, moved=moved)
        left = run.db.execute(
            select(Transaction.id)
            .where(
                Transaction.household_id == run.household_id,
                Transaction.account_id == account.id,
                Transaction.source_document_id == run.document.id,
                Transaction.import_hash.is_not(None),
            )
            .limit(1)
        ).first()
        if left is None:
            drop_stale_records(db, run.document, account, ())
            # The balances the statement recorded there went with it.
            db.execute(
                delete(BalanceSnapshot).where(
                    BalanceSnapshot.household_id == run.household_id,
                    BalanceSnapshot.account_id == account.id,
                    BalanceSnapshot.source_document_id == run.document.id,
                    BalanceSnapshot.source == BalanceSource.STATEMENT,
                )
            )


def _open_items(db: Session, document: Document) -> int:
    """The document's open items that wait on a person (a notice does not)."""
    return review_repo.open_items_of(db, document.household_id, document.id)


def import_extraction(
    db: Session,
    document: Document,
    result: ExtractResult,
    *,
    mapping_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    group: int | None = None,
    statement: int | None = None,
    assignments: Sequence[Assignment] = (),
    allow_overlap: Collection[uuid.UUID] = (),
    user_id: uuid.UUID | None = None,
) -> ImportResult:
    """Import the records of ``result`` for ``document`` (module doc, step 2).

    ``user_id`` is the person who asked for a re-run (else the uploader
    decides which accounts may be matched); ``mapping_id``/``account_id``
    are their choices, ``group`` the statement of a multi-account table
    ``account_id`` is for; ``allow_overlap`` the accounts whose statements
    this run imports although they overlap an imported one. Every run
    carries the document's "import anyway" / "import as read" answers
    (``statement_answers``), whoever started it. Runs in the caller's
    transaction and RLS context.
    """
    actor = user_id or document.uploaded_by
    earlier = _earliest_recorded(db, document)
    # A PDF/image no parser read: its page table goes through the saved
    # column mappings like a CSV (app.domain.review.layout), unless no
    # table on its pages has a dated row.
    # A receipt, payslip, tax form or loan document its own parser could
    # not read is neither: it says what it is (module doc).
    unread = unread_kind(result)
    no_rows = "pages" if pages_without_rows(result) else None
    result = with_layout_tables(result)
    if result.tables and not (result.statements or result.investments):
        no_rows = "table" if header_only(result.tables[0]) else None
    run = _Run(
        db=db,
        document=document,
        allowed=importable_account_ids(db, document.household_id, actor),
        user_id=user_id,
        assigned={(a.target, a.index): a.account_id for a in assignments},
        allow_overlap=frozenset(allow_overlap),
        answers=document_answers(db, document),
        parse_skipped=result.parse.skipped_lines if result.parse is not None else 0,
        parse_warnings=tuple(result.parse.warnings) if result.parse is not None else (),
    )
    statements: Sequence[InvestmentStatement] = result.investments
    if unread is not None and result.parse is not None and result.parse.parser is not None:
        run.review(
            ReviewKind.UNPARSED,
            {
                "reason": f"{unread}_unreadable",
                "parser": result.parse.parser.name,
                "warnings": list(result.parse.warnings),
            },
        )
    elif no_rows is not None:
        # Nothing to import and nothing to map: said as such, whatever
        # mapping fits the header (module doc).
        run.review(
            ReviewKind.UNPARSED,
            {
                "reason": "no_rows",
                "source": no_rows,
                **({"table": 0} if no_rows == "table" else {}),
            },
        )
    elif result.statements:
        _import_statements(run, result, account_id=account_id)
    elif result.tables:
        # A brokerage positions or activity export is investment data, unless
        # the person chose a cash-ledger mapping for this run (module doc).
        positions = positions_table(result, document) if mapping_id is None else None
        activity, mapping = (
            _activity_reading(run, result, mapping_id, account_id)
            if positions is None
            else (None, None)
        )
        if positions is not None:
            _import_positions(run, positions)
            statements = positions.statements
        elif activity is None:
            _import_table(run, result, mapping_id=mapping_id, account_id=account_id, group=group)
        else:
            _import_activity(run, result, activity, account_id=account_id, mapping=mapping)
            statements = activity.statements
    elif not result.investments:
        run.review(ReviewKind.UNPARSED, {"reason": "no_records"})
    _report_skipped(run, result)
    investments: dict[str, Any] | None = None
    first_price: date | None = None
    if statements:
        # Brokerage statements (app.domain.investments.statement_import);
        # their cash deposits/withdrawals are ledger rows committed here.
        done = import_investment_statements(
            db,
            document,
            statements,
            allowed=run.allowed,
            review=run.review,
            commit_cash=lambda statement, account: _commit_statement(
                run, from_statement(statement), account, statement_index=None
            ),
            account_id=account_id,
            statement_index=statement,
            assigned={i: a for (t, i), a in run.assigned.items() if t == "investment"},
            allow_overlap=run.allow_overlap,
            answers=run.answers,
            user_id=user_id,
            several=_several_kinds(result),
            unread=bool(run.parse_skipped)
            or bool(CONTRADICTION_WARNINGS.intersection(run.parse_warnings)),
        )
        run.overlaps += done.overlaps
        for investment_account in done.accounts:
            if investment_account not in run.accounts:
                run.accounts.append(investment_account)
        investments = done.summary()
        first_price = done.first_price
        # Brokerage cash rows are written from the lines a statement's own
        # checks let through: what an earlier reading wrote is left alone.
        run.unplaced = True
    _finish_reading(run)
    if document.account_id is None and len(run.accounts) == 1:
        document.account_id = run.accounts[0]
    if document.institution_id is None and len(run.accounts) == 1:
        account = accounts_repo.get_account(db, document.household_id, run.accounts[0])
        if account is not None and account.institution_id is not None:
            document.institution_id = account.institution_id
    db.flush()
    _recompute_net_worth(db, document, earlier, first_price)
    status = DocumentStatus.NEEDS_REVIEW if _open_items(db, document) else DocumentStatus.PROCESSED
    summary = run.summary()
    if investments is not None:
        summary["investments"] = investments
    return ImportResult(status, summary)


# ---------------------------------------------------------------------------
# Re-running an import (POST /imports/{document_id}/apply-mapping)
# ---------------------------------------------------------------------------


def _earliest_recorded(db: Session, document: Document) -> date | None:
    """The first day a ledger row, balance or investment row of ``document`` is on."""
    days = [
        db.execute(
            select(func.min(column)).where(
                model.household_id == document.household_id, owner == document.id
            )
        ).scalar_one_or_none()
        for model, column, owner in (
            (Transaction, Transaction.posted_date, Transaction.source_document_id),
            (BalanceSnapshot, BalanceSnapshot.as_of_date, BalanceSnapshot.source_document_id),
            (InvestmentTxn, InvestmentTxn.trade_date, InvestmentTxn.source_document_id),
        )
    ]
    found = [d for d in days if d is not None]
    return min(found) if found else None


def _recompute_net_worth(
    db: Session, document: Document, earlier: date | None, first_price: date | None = None
) -> None:
    """Have the stored net worth rewritten from the first day this run could have changed.

    What the document holds now and what it held before the run (an
    earlier reading's rows, removed or replaced), and the first price its
    brokerage statements wrote (a price names no document): a stored day
    from then on no longer says what the ledger does
    (``networth.request_recompute``).
    """
    days = (earlier, _earliest_recorded(db, document), first_price)
    found = [d for d in days if d is not None]
    if found:
        networth.request_recompute(db, document.household_id, min(found))


def _importable(document: Document) -> ExtractResult:
    if document.status not in IMPORTABLE_STATUSES:
        raise _not_applicable("The document is not ready to import.")
    result = _stored_result(document)
    if result is None or not (result.tables or result.statements or result.investments):
        raise _not_applicable("The document has no extracted records.")
    return result


def request_apply(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    data: ApplyMapping,
    *,
    request: HTTPConnection | None,
) -> ImportAccepted:
    """Check the choices and queue :data:`APPLY_MAPPING_JOB_TYPE` (member or above)."""
    household_id = _household(principal, HouseholdRole.MEMBER)
    document = get_visible_document(db, principal, document_id)
    result = _importable(document)
    mapping: ImportMapping | None = None
    if data.mapping_id is not None:
        mapping = _get(db, principal, data.mapping_id)
        if not result.tables:
            raise _no_table()
        spec = _spec(mapping)
        if spec is None or not _fits(result.tables[0], spec):
            raise _columns_mismatch(result.tables[0].column_count)
    target = data.account_id or document.account_id or (mapping.account_id if mapping else None)
    if target is not None:
        get_visible_account(db, principal, target, need="edit")
    for assignment in data.assignments:
        get_visible_account(db, principal, assignment.account_id, need="edit")
    for overlap_account in data.allow_overlap:
        get_visible_account(db, principal, overlap_account, need="edit")
    payload = ApplyMappingJob(
        document_id=document.id,
        mapping_id=data.mapping_id,
        account_id=data.account_id,
        group=data.group,
        statement=data.statement,
        assignments=data.assignments,
        allow_overlap=data.allow_overlap,
        requested_by=principal.user_id,
    )
    job_id = enqueue(db, APPLY_MAPPING_JOB_TYPE, payload, household_id=household_id)
    audit(
        db,
        action="document.import_requested",
        target_type="document",
        target_id=document.id,
        after={**payload.model_dump(mode="json"), "job_id": str(job_id)},
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(db, household_id, "job.queued", {"id": str(job_id), "type": APPLY_MAPPING_JOB_TYPE})
    return ImportAccepted(job_id=job_id, state="queued", document_id=document.id)


def _placed(
    db: Session,
    document: Document,
    result: ExtractResult,
    item: dict[str, Any],
    payload: ApplyMappingJob,
    imported_into: Collection[str],
    mapping_id: str | None = None,
) -> dict[str, Any]:
    """Where the re-run put the statement an ``unknown_account`` item asked about.

    The account the run assigned to it (the Accounts dialog's assignment,
    or the account chosen for that statement), when the run imported into
    that account: recorded with the statement's key so later imports of
    the document put it there again (``statement_answers``). The same for
    a statement of a multi-account table (:func:`_placed_group`). Empty
    for an item about anything else (an unparsed file).
    """
    index = item.get("statement")
    if not isinstance(index, int) or isinstance(index, bool):
        return _placed_group(db, document, result, item, payload, imported_into, mapping_id)
    investment = item.get("investment") is True
    target: Literal["statement", "investment"] = "investment" if investment else "statement"
    account_id = next(
        (a.account_id for a in payload.assignments if a.target == target and a.index == index),
        payload.account_id if payload.statement == index else None,
    )
    if account_id is None or str(account_id) not in imported_into:
        return {}
    statements: Sequence[Statement | InvestmentStatement] = (
        stored_investments(document, db) if investment else result.statements
    )
    if not 0 <= index < len(statements):
        return {}
    return placement(
        target,
        statement_key(statements[index]),
        account_id,
        statement_refs(target, statements)[index],
    )


def _placed_group(
    db: Session,
    document: Document,
    result: ExtractResult,
    item: dict[str, Any],
    payload: ApplyMappingJob,
    imported_into: Collection[str],
    mapping_id: str | None,
) -> dict[str, Any]:
    """Where the re-run put the group of a table an ``unknown_account`` item asked about.

    ``item`` names the group by its index under the mapping it was raised
    with (``group``, ``mapping_id``, ``spec_digest``). The placement is
    recorded when the run read the table with that very mapping
    (``mapping_id``, unchanged since), an account was assigned to or
    chosen for that group, and the run imported into it. The key is the
    group's digest (:func:`_group_key`); a table group and a bank
    statement share the ``statement`` target, their digests never meet.
    """
    group = item.get("group")
    if not isinstance(group, int) or isinstance(group, bool):
        return {}
    if mapping_id is None or item.get("mapping_id") != mapping_id:
        return {}
    account_id = next(
        (a.account_id for a in payload.assignments if a.target == "group" and a.index == group),
        payload.account_id if payload.group == group else None,
    )
    if account_id is None or str(account_id) not in imported_into:
        return {}
    spec = _item_spec(db, document, item)
    tables = with_layout_tables(result).tables
    if spec is None or not tables:
        return {}
    readable = [s for s in from_table_grouped(tables[0], spec) if s.transactions]
    if not 0 <= group < len(readable):
        return {}
    return placement("statement", _group_key(readable[group]), account_id)


def _resolved_for(db: Session, document: Document, job_id: uuid.UUID | None) -> list[ReviewItem]:
    """The ``unknown_account`` items the review queue resolved by queueing this very job.

    "Use this account" resolves its item at once and records the job that
    carries the choice out (``app.domain.review.service``). Where that job
    put the item's statement is added to the resolution when the job has
    run, as for the items it finds open (:func:`_placed`).
    """
    if job_id is None:
        return []
    rows = db.execute(
        select(ReviewItem)
        .where(
            ReviewItem.household_id == document.household_id,
            ReviewItem.document_id == document.id,
            ReviewItem.state == ReviewItemState.RESOLVED,
            ReviewItem.kind == ReviewKind.UNKNOWN_ACCOUNT,
        )
        .with_for_update()
    ).scalars()
    return [
        item
        for item in rows
        if isinstance(item.resolution, dict) and item.resolution.get("job_id") == str(job_id)
    ]


def run_apply_job(
    db: Session,
    household_id: uuid.UUID,
    payload: ApplyMappingJob,
    *,
    job_id: uuid.UUID | None = None,
    attempt: int = 1,
) -> ImportResult | None:
    """Re-import a document from its stored extraction (None: nothing to do)."""
    document = documents_repo.get_document(db, household_id, payload.document_id, lock=True)
    if document is None:
        return None
    try:
        result = _importable(document)
    except ProblemError:
        return None
    answered = [
        item
        for item in repo.open_review_items(db, household_id, document.id, RERUN_ANSWERS)
        if (item.payload or {}).get("reason") not in PIPELINE_REASONS
    ]
    started = clock.utcnow()
    outcome = import_extraction(
        db,
        document,
        result,
        mapping_id=payload.mapping_id,
        account_id=payload.account_id,
        group=payload.group,
        statement=payload.statement,
        assignments=payload.assignments,
        allow_overlap=payload.allow_overlap,
        user_id=payload.requested_by,
    )
    now = clock.utcnow()
    resolution = {
        "by": "apply_mapping",
        "job_id": str(job_id) if job_id else None,
        "mapping_id": outcome.summary.get("mapping_id"),
        "account_id": str(payload.account_id) if payload.account_id else None,
    }
    imported_into = {str(a) for a in outcome.summary.get("accounts", [])}
    for item in answered:
        item.state = ReviewItemState.RESOLVED
        item.resolved_at = now
        item.resolved_by = payload.requested_by
        item.resolution = {
            **resolution,
            **_placed(
                db,
                document,
                result,
                item.payload or {},
                payload,
                imported_into,
                resolution["mapping_id"],
            ),
        }
    for item in _resolved_for(db, document, job_id):
        placed = _placed(
            db,
            document,
            result,
            item.payload or {},
            payload,
            imported_into,
            resolution["mapping_id"],
        )
        if placed:
            item.resolution = {**(item.resolution or {}), **placed}
    db.flush()
    status = DocumentStatus.NEEDS_REVIEW if _open_items(db, document) else DocumentStatus.PROCESSED
    db.add(
        IngestJob(
            household_id=household_id,
            document_id=document.id,
            stage=IngestStage.PARSE,
            attempt=max(1, attempt),
            started_at=started,
            finished_at=now,
            log={**outcome.summary, "rerun": True, "resolved": len(answered)},
        )
    )
    document.status = status
    document.error = None
    db.flush()
    audit(
        db,
        action="document.import",
        target_type="document",
        target_id=document.id,
        after={"status": str(status), **outcome.summary, "resolved": len(answered)},
        household_id=household_id,
        user_id=payload.requested_by,
    )
    emit(db, household_id, "document.updated", {"id": str(document.id), "status": str(status)})
    return ImportResult(status, outcome.summary)


# ---------------------------------------------------------------------------
# Preview (GET /imports/{document_id}/preview)
# ---------------------------------------------------------------------------


def _preview_table(index: int, table: Table, offset: int, limit: int) -> PreviewTable:
    return PreviewTable(
        index=index,
        encoding=table.encoding,
        delimiter=table.delimiter,
        has_header=table.has_header,
        column_count=table.column_count,
        row_count=len(table.rows),
        truncated=table.truncated,
        offset=offset,
        rows=[list(r) for r in table.rows[offset : offset + limit]],
        header_fingerprint=header_fingerprint(table.rows[0]) if table.rows else None,
    )


def _preview_statement(index: int, statement: Any, offset: int, limit: int) -> PreviewStatement:
    currency = statement.account.currency
    ledger = statement.ledger_balance
    return PreviewStatement(
        index=index,
        source_format=statement.source_format,
        account_type=statement.account.account_type,
        last4=statement.account.last4,
        currency=currency,
        institution=statement.account.institution,
        period_start=statement.period_start,
        period_end=statement.period_end,
        ledger_balance=(
            PreviewBalance(amount=f"{ledger.amount:f}", currency=currency, as_of=ledger.as_of)
            if ledger is not None
            else None
        ),
        transaction_count=len(statement.transactions),
        truncated=statement.truncated,
        offset=offset,
        transactions=[
            PreviewTransaction(
                posted_date=t.posted,
                amount=f"{t.amount:f}",
                currency=t.currency or currency,
                payee=t.payee,
                memo=t.memo,
                category=t.category,
                check_number=t.check_number,
            )
            for t in statement.transactions[offset : offset + limit]
        ],
    )


def _suggestion_out(suggestion: MappingSuggestion) -> MappingSuggestionOut:
    facts = suggestion.summary()
    del facts["skip_rows"]  # in the spec
    return MappingSuggestionOut.model_validate({**facts, "spec": suggestion.spec})


def preview(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    *,
    offset: int = 0,
    limit: int = PREVIEW_DEFAULT,
) -> ImportPreview:
    """The stored sandbox output of a visible document (never re-parsed)."""
    household_id = _household(principal, HouseholdRole.VIEWER)
    document = get_visible_document(db, principal, document_id)
    result = _stored_result(document)
    tables = result.tables if result is not None else []
    statements = result.statements if result is not None else []
    matched: uuid.UUID | None = None
    suggested: MappingSuggestion | None = None
    if tables:
        choice = choose_mapping(
            db,
            household_id,
            tables[0],
            account_id=document.account_id,
            allowed=None,
            visible=_visible(principal),
        )
        matched = choice.mapping.id if choice.mapping is not None else None
        suggested = suggest_table_mapping(tables[0], received_on=_received_on(document))
    return ImportPreview(
        document_id=document.id,
        status=document.status,
        kind=result.kind if result is not None else None,
        account_id=document.account_id,
        tables=[_preview_table(i, t, offset, limit) for i, t in enumerate(tables)],
        statements=[_preview_statement(i, s, offset, limit) for i, s in enumerate(statements)],
        warnings=list(result.warnings) if result is not None else [],
        matched_mapping_id=matched,
        suggested_spec=suggested.spec if suggested is not None else None,
        suggestion=_suggestion_out(suggested) if suggested is not None else None,
        last_import=repo.last_import_log(db, household_id, document.id),
    )
