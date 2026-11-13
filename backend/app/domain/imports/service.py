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
     review item.
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
     when the header names a card, every amount has one sign or more rows
     are money in than money out (the suggestion's ``sign_unverified``
     doubt; ``auto_import: sign_unverified`` when it is the only one), and
     a sign on trust is never used to write into a liability account (a
     card export may list purchases as positive). A table of securities
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
     card, next month's file), or a file whose own suggestion doubts the
     sign, the import waits as ``mapping_required``
     (``auto_import: sign_unverified``) until a person saves the mapping's
     spec in the editor (which clears the mark) or chooses the mapping
     explicitly for that re-run. With one account-bound mapping among the
     fitting ones, it wins over household mappings for an upload that
     names no account. Anything else is an ``unparsed``
     review item (``mapping_required``) carrying the suggestion and why it
     was not used (``auto_import``), and the table is shown by ``GET
     /imports/{document_id}/preview`` for the mapping editor. The account is
     the one given on re-run, the document's or the mapping's.
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
     ``review_state = pending`` with a ``possible_duplicate`` review item.
   * The household's rules run on the new transactions
     (``app.domain.rules.service.apply_rules_to``; categories that came from
     the file, QIF ``L``, are protected). The closing balance (OFX
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
   * The document is ``processed`` when nothing is left open for it in the
     review queue, ``needs_review`` otherwise.

3. A person fixes what was missing (saves a mapping, picks an account) and
   ``POST /imports/{document_id}/apply-mapping`` queues
   ``imports.apply_mapping`` (:func:`run_apply_job`), which runs step 2 again
   on the **stored** extraction (nothing is re-parsed) and resolves the
   review items it answered. Running it again is harmless: every row already
   imported matches its hash.

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
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy import ColumnElement, delete, or_, select
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
from app.domain.accounts.models import LIABILITY_TYPES, Account, BalanceSource
from app.domain.accounts.visibility import get_visible_account, visible_account_filter
from app.domain.documents import repo as documents_repo
from app.domain.documents.models import (
    Document,
    DocumentStatus,
    IngestJob,
    IngestStage,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.service import get_visible_document
from app.domain.imports import repo
from app.domain.imports.models import NAME_MAX, ImportMapping
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
)
from app.domain.imports.statement_answers import (
    StatementAnswers,
    document_answers,
    placement,
    statement_key,
)
from app.domain.imports.statement_records import (
    StatementFacts,
    bank_facts,
    check_overlap,
    record_statement,
    table_facts,
)
from app.domain.investments.statement_import import import_investment_statements
from app.domain.review import repo as review_repo
from app.domain.review.layout import with_layout_tables
from app.domain.rules.service import apply_rules_to
from app.domain.transactions import repo as transactions_repo
from app.domain.transactions.import_hash import HashInput
from app.domain.transactions.models import (
    MEMO_MAX,
    PAYEE_MAX,
    ReviewState,
    Transaction,
    TransactionStatus,
)
from app.ingestion.match.accounts import AccountMatch, importable_account_ids, match_account
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
from app.ingestion.parsers.positions_table import PARSER_NAME as POSITIONS_PARSER
from app.ingestion.parsers.positions_table import PARSER_VERSION as POSITIONS_PARSER_VERSION
from app.ingestion.parsers.positions_table import TABLE_KINDS as POSITIONS_TABLE_KINDS
from app.ingestion.parsers.positions_table import PositionsTable, read_positions_table
from app.ingestion.parsers.structured import (
    MappingSuggestion,
    ParsedStatement,
    ParsedTransaction,
    candidate_fingerprints,
    from_statement,
    from_table_grouped,
    group_last4,
    header_fingerprint,
    spec_fingerprint,
    suggest_mapping,
)
from app.ingestion.sandbox.protocol import ExtractResult, InvestmentStatement, Table

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: Most row numbers / ids copied into one review item, audit row or event.
IDS_MAX: Final = 50
#: Review kinds a re-run answers (resolved when the re-run goes through).
RERUN_ANSWERS: Final = frozenset({ReviewKind.UNPARSED, ReviewKind.UNKNOWN_ACCOUNT})
#: An extractor's count of the records of one statement it could not read
#: (``app.ingestion.extract.ofx.skipped_warnings``; OFX, QFX and QIF).
_SKIPPED_WARNING: Final = re.compile(r"transaction_skipped:(\d+)")
#: Statuses whose stored extraction may be (re-)imported.
IMPORTABLE_STATUSES: Final = frozenset({DocumentStatus.NEEDS_REVIEW, DocumentStatus.PROCESSED})
#: Extraction kinds whose table may be imported with a confident suggestion
#: (a PDF/image page table no parser read always waits for a person).
AUTO_MAPPING_KINDS: Final = frozenset({"csv", "tsv", "xlsx"})
#: Longest name given to a mapping saved from a suggestion (the editor's limit).
SUGGESTED_NAME_MAX: Final = 120


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
    skipped_currency: int = 0
    snapshots: int = 0
    rules_changed: int = 0
    #: Rows that took their payee's default category.
    payee_categorised: int = 0
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
    _payees: PayeeMatcher | None = None
    _payee_categories: dict[uuid.UUID, uuid.UUID] | None = None
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
                    (item.kind, item.payload)
                    for item in repo.open_review_items(
                        self.db, self.household_id, self.document.id, set(ReviewKind)
                    )
                ]
            if (kind, payload) in self._open:
                return
            self._open.append((kind, payload))
        documents_repo.add_review_item(
            self.db, self.household_id, kind, payload, document_id=self.document.id
        )
        self.review_items += 1

    def payees(self) -> PayeeMatcher:
        if self._payees is None:
            self._payees = PayeeMatcher(repo.payee_lookup(self.db, self.household_id))
        return self._payees

    def payee_categories(self) -> dict[uuid.UUID, uuid.UUID]:
        if self._payee_categories is None:
            self._payee_categories = repo.payee_default_categories(self.db, self.household_id)
        return self._payee_categories

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
            "skipped_currency": self.skipped_currency,
            "snapshots": self.snapshots,
            "rules_changed": self.rules_changed,
            "payee_categorised": self.payee_categorised,
            "review_items": self.review_items,
            "statements": self.statements,
            "low_confidence": self.low_confidence,
            "statement_overlaps": self.overlaps,
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


def _row_problems(run: _Run, parsed: ParsedStatement, account: Account) -> None:
    # Footers and pending rows are not problems: ``rows_skipped`` is raised
    # for the rows that should have read (``parsed.skipped_rows``) only.
    run.footer_rows += len(parsed.footer_rows)
    run.pending_rows += len(parsed.pending_rows)
    if parsed.skipped_rows:
        run.skipped_rows += len(parsed.skipped_rows)
        run.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": "rows_skipped",
                "account_id": str(account.id),
                "count": len(parsed.skipped_rows),
                "rows": parsed.skipped_rows[:IDS_MAX],
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
    if not parsed.reconciled and parsed.reconciliation is not None:
        # SECURITY.md C12: the statement's own figures disagree (or cannot be
        # checked); its rows are imported pending and no snapshot is written.
        check = parsed.reconciliation
        run.review(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {
                "reason": (
                    "reconciliation_failed"
                    if check.status == "mismatch"
                    else "reconciliation_unavailable"
                ),
                "field": "reconciliation",
                "account_id": str(account.id),
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


def _low_confidence_review(
    run: _Run,
    parsed: ParsedStatement,
    account: Account,
    low: list[tuple[Transaction, ParsedTransaction]],
) -> None:
    """One ``low_confidence_field`` item for the rows a document parser was unsure of."""
    if not low:
        return
    run.low_confidence += len(low)
    fields = sorted({name for _, txn in low for name in txn.low_confidence})
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
                    "fields": list(txn.low_confidence),
                    "confidence": txn.confidence,
                    "page": txn.page,
                }
                for row, txn in low[:IDS_MAX]
            ],
        },
    )


def _commit_statement(
    run: _Run, parsed: ParsedStatement, account: Account, *, statement_index: int | None
) -> bool:
    """Write one statement's new transactions, review items and closing balance.

    False when nothing could be written (the statement is in another currency).
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
        return False
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
    _row_problems(run, parsed, account)
    plan = dedupe(
        db,
        run.household_id,
        account.id,
        [HashInput(t.posted_date, t.amount, t.raw_payee) for t in rows],
    )
    run.duplicates += len(plan.exact)
    payees = run.payees() if plan.to_insert else None
    created: list[tuple[Transaction, bool]] = []
    split_lines: dict[int, list[dict[str, Any]]] = {}
    for index in plan.to_insert:
        txn = rows[index]
        category_id = None if txn.splits else _category_id(run, txn.category)
        row = Transaction(
            household_id=run.household_id,
            account_id=account.id,
            posted_date=txn.posted_date,
            amount=to_storage(txn.amount),
            currency=account.currency,
            payee_id=payees.match(txn.raw_payee) if payees is not None else None,
            category_id=category_id,
            raw_payee=txn.raw_payee[:PAYEE_MAX] if txn.raw_payee else None,
            memo=txn.memo[:MEMO_MAX] if txn.memo else None,
            status=TransactionStatus.CLEARED,
            source_document_id=run.document.id,
            import_hash=plan.hashes[index],
            review_state=(
                ReviewState.PENDING
                if index in plan.near or txn.low_confidence
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
    for protected in (False, True):
        batch = [row for row, categorised in created if categorised is protected]
        if batch:
            outcome = apply_rules_to(
                db,
                run.household_id,
                batch,
                protect=("categorization",) if protected else (),
                user_id=run.user_id,
            )
            run.rules_changed += outcome.changed
    # What neither the file nor a rule categorised takes its payee's default
    # category (set under "Categories, payees and tags").
    defaults = run.payee_categories() if created else {}
    open_rows = [
        row
        for row, categorised in created
        if not categorised and row.category_id is None and row.payee_id in defaults
    ]
    if open_rows:
        # A rule may have split a row: its lines carry the categories.
        split = transactions_repo.splits_for(db, run.household_id, [row.id for row in open_rows])
        for row in open_rows:
            if row.payee_id is not None and not split.get(row.id):
                row.category_id = defaults[row.payee_id]
                run.payee_categorised += 1
    positions = {index: position for position, index in enumerate(plan.to_insert)}
    for index, candidates in plan.near.items():
        position = positions[index]
        run.review(
            ReviewKind.POSSIBLE_DUPLICATE,
            {
                "reason": "near_match",
                "account_id": str(account.id),
                "transaction_id": str(created[position][0].id),
                "candidates": [str(c) for c in candidates],
            },
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
            if rows[index].low_confidence
        ],
    )
    if parsed.closing_balance is not None and parsed.reconciled:
        accounts_repo.upsert_snapshot(
            db,
            account,
            parsed.closing_balance.as_of,
            to_storage(parsed.closing_balance.amount),
            BalanceSource.STATEMENT,
            source_document_id=run.document.id,
        )
        run.snapshots += 1
        emit(db, run.household_id, "balance.updated", {"account_id": str(account.id)})
    if created:
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
    return True


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


def _import_statements(run: _Run, result: ExtractResult, *, account_id: uuid.UUID | None) -> None:
    single = len(result.statements) == 1 and not _several_kinds(result)
    for index, statement in enumerate(result.statements):
        parsed = from_statement(statement)
        key = statement_key(statement)
        account: Account | None = None
        candidates: tuple[uuid.UUID, ...] = ()
        # The run's own assignment, else the account an answer put it on.
        assigned = run.assigned.get(("statement", index)) or run.answers.account("statement", key)
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
            match = match_account(
                run.db,
                run.household_id,
                last4=parsed.last4,
                institution=parsed.institution,
                allowed=run.allowed,
            )
            candidates = match.candidates
            if account_id is not None and (match.account_id is None or single):
                account = _usable_account(run, account_id)
            else:
                account = _usable_account(run, match.account_id)
        if account is None:
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
        facts = bank_facts(statement)
        if not _guarded(run, account, facts, index, key):
            continue
        if _commit_statement(run, parsed, account, statement_index=index):
            record_statement(run.db, run.document, account, facts)


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


def _sign_held(on_trust: bool, targets: list[_Target], doubted: bool = False) -> str | None:
    """A sign taken on trust is never used to write into a liability account.

    ``on_trust``: a suggestion without ``sign_evidence``, or a mapping saved
    from one (``sign_unverified``). Statements whose account is not known
    yet are left to their ``unknown_account`` / ``account_required`` item;
    the mapping saved meanwhile keeps ``sign_unverified``, so the re-run
    after a person picks a liability account is held here again.

    ``doubted``: the suggestion for this very table doubts the sign
    (``sign_unverified``: a card-like header, amounts of one sign, more
    money in than out). A mapping still on trust is not used for such a
    file whatever the account, which also stops a mapping saved from a card
    export before those doubts existed.
    """
    if not on_trust:
        return None
    if doubted or any(t.account is not None and t.account.type in LIABILITY_TYPES for t in targets):
        return "sign_unverified"
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


def _mapping_review(
    run: _Run,
    table: Table,
    choice: MappingChoice,
    suggestion: MappingSuggestion | None,
    held: str | None,
) -> None:
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
        },
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
    if saved is None:
        if suggestion is None:  # pragma: no cover - set whenever no mapping was saved
            return
        held = _sign_held(suggestion.sign_evidence is None, targets)
        mapping = None if held is not None else _save_suggested(run, table, suggestion)
        if mapping is None:
            _mapping_review(run, table, choice, suggestion, held or "not_saved")
            return
        run.mapping_suggested = True
    else:
        # A mapping saved from a trust-only suggestion is checked again
        # whenever it meets a liability account or a file whose sign the
        # suggestion doubts, unless the person chose it.
        on_trust = saved.sign_unverified and mapping_id is None
        fresh = suggest_mapping(table, today=_received_on(run.document)) if on_trust else None
        doubted = fresh is not None and "sign_unverified" in fresh.doubts
        if _sign_held(on_trust, targets, doubted) is not None:
            _mapping_review(run, table, choice, fresh, "sign_unverified")
            return
        mapping = saved
    run.mapping_id = mapping.id
    committed = False
    for item in targets:
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
        if _commit_statement(run, item.parsed, item.account, statement_index=None):
            closing = item.parsed.closing_balance
            record_statement(
                run.db,
                run.document,
                item.account,
                table_facts(
                    item.parsed.source_format,
                    item.parsed.period_start,
                    item.parsed.period_end,
                    closing.amount if closing is not None else None,
                ),
            )
        committed = True
    if not committed:
        return
    repo.mark_used(run.db, mapping)
    if run.document.institution_id is None and mapping.institution_id is not None:
        run.document.institution_id = mapping.institution_id


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
    # A PDF/image no parser read: its page table goes through the saved
    # column mappings like a CSV (app.domain.review.layout).
    result = with_layout_tables(result)
    run = _Run(
        db=db,
        document=document,
        allowed=importable_account_ids(db, document.household_id, actor),
        user_id=user_id,
        assigned={(a.target, a.index): a.account_id for a in assignments},
        allow_overlap=frozenset(allow_overlap),
        answers=document_answers(db, document),
    )
    statements: Sequence[InvestmentStatement] = result.investments
    if result.statements:
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
        )
        run.overlaps += done.overlaps
        for investment_account in done.accounts:
            if investment_account not in run.accounts:
                run.accounts.append(investment_account)
        investments = done.summary()
    if document.account_id is None and len(run.accounts) == 1:
        document.account_id = run.accounts[0]
    if document.institution_id is None and len(run.accounts) == 1:
        account = accounts_repo.get_account(db, document.household_id, run.accounts[0])
        if account is not None and account.institution_id is not None:
            document.institution_id = account.institution_id
    db.flush()
    status = DocumentStatus.NEEDS_REVIEW if _open_items(db, document) else DocumentStatus.PROCESSED
    summary = run.summary()
    if investments is not None:
        summary["investments"] = investments
    return ImportResult(status, summary)


# ---------------------------------------------------------------------------
# Re-running an import (POST /imports/{document_id}/apply-mapping)
# ---------------------------------------------------------------------------


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
    statements: Sequence[BaseModel] = (
        stored_investments(document, db) if investment else result.statements
    )
    if not 0 <= index < len(statements):
        return {}
    return placement(target, statement_key(statements[index]), account_id)


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
    answered = repo.open_review_items(db, household_id, document.id, RERUN_ANSWERS)
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
