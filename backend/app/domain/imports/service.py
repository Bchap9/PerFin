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
     ``import_mapping``; none is an ``unparsed`` review item
     (``mapping_required``) and the table is shown by ``GET
     /imports/{document_id}/preview`` for the mapping editor. The account is
     the one given on re-run, the document's or the mapping's.
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

import uuid
from collections.abc import Collection, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import ValidationError
from sqlalchemy import ColumnElement, delete, or_, select
from sqlalchemy.orm import Session

from app.auth.authz import role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import sum_amounts, to_storage
from app.db.queue import enqueue
from app.domain.accounts import repo as accounts_repo
from app.domain.accounts.models import Account, BalanceSource
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
from app.domain.imports.models import ImportMapping
from app.domain.imports.schemas import (
    APPLY_MAPPING_JOB_TYPE,
    DEFAULT_PAGE,
    PREVIEW_DEFAULT,
    ApplyMapping,
    ApplyMappingJob,
    ImportAccepted,
    ImportPreview,
    MappingCreate,
    MappingOut,
    MappingPage,
    MappingSpec,
    MappingUpdate,
    PreviewBalance,
    PreviewStatement,
    PreviewTable,
    PreviewTransaction,
)
from app.domain.review.layout import with_layout_tables
from app.domain.rules.service import apply_rules_to
from app.domain.transactions import repo as transactions_repo
from app.domain.transactions.import_hash import HashInput, normalize_payee
from app.domain.transactions.models import (
    MEMO_MAX,
    PAYEE_MAX,
    ReviewState,
    Transaction,
    TransactionStatus,
)
from app.ingestion.match.accounts import importable_account_ids, match_account
from app.ingestion.match.dedupe import dedupe
from app.ingestion.parsers.structured import (
    ParsedStatement,
    ParsedTransaction,
    candidate_fingerprints,
    from_statement,
    from_table,
    header_fingerprint,
    spec_fingerprint,
    suggest_spec,
)
from app.ingestion.sandbox.protocol import ExtractResult, Table

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: Most row numbers / ids copied into one review item, audit row or event.
IDS_MAX: Final = 50
#: Review kinds a re-run answers (resolved when the re-run goes through).
RERUN_ANSWERS: Final = frozenset({ReviewKind.UNPARSED, ReviewKind.UNKNOWN_ACCOUNT})
#: Statuses whose stored extraction may be (re-)imported.
IMPORTABLE_STATUSES: Final = frozenset({DocumentStatus.NEEDS_REVIEW, DocumentStatus.PROCESSED})


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
    skipped_currency: int = 0
    snapshots: int = 0
    rules_changed: int = 0
    review_items: int = 0
    statements: int = 0
    low_confidence: int = 0
    accounts: list[uuid.UUID] = field(default_factory=list)
    mapping_id: uuid.UUID | None = None
    _payees: dict[str, uuid.UUID] | None = None
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

    def payees(self) -> dict[str, uuid.UUID]:
        if self._payees is None:
            self._payees = repo.payee_lookup(self.db, self.household_id)
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
            "skipped_currency": self.skipped_currency,
            "snapshots": self.snapshots,
            "rules_changed": self.rules_changed,
            "review_items": self.review_items,
            "statements": self.statements,
            "low_confidence": self.low_confidence,
            "accounts": [str(a) for a in self.accounts],
            "mapping_id": str(self.mapping_id) if self.mapping_id else None,
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
) -> None:
    """Write one statement's new transactions, review items and closing balance."""
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
        return
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
    payees = run.payees() if plan.to_insert else {}
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
            payee_id=payees.get(normalize_payee(txn.raw_payee)) if txn.raw_payee else None,
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


def _import_statements(run: _Run, result: ExtractResult, *, account_id: uuid.UUID | None) -> None:
    single = len(result.statements) == 1
    for index, statement in enumerate(result.statements):
        parsed = from_statement(statement)
        account: Account | None = None
        candidates: tuple[uuid.UUID, ...] = ()
        if single and run.document.account_id is not None and account_id is None:
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
        _commit_statement(run, parsed, account, statement_index=index)


def _import_table(
    run: _Run,
    result: ExtractResult,
    *,
    mapping_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
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
    if choice.mapping is None or choice.spec is None:
        run.review(
            ReviewKind.UNPARSED,
            {
                "reason": "mapping_ambiguous" if choice.ambiguous else "mapping_required",
                "table": 0,
                "header_fingerprint": header_fingerprint(table.rows[0]) if table.rows else None,
                "column_count": table.column_count,
                "row_count": len(table.rows),
                "candidates": [str(c) for c in choice.ambiguous[:IDS_MAX]],
            },
        )
        return
    mapping, spec = choice.mapping, choice.spec
    run.mapping_id = mapping.id
    parsed = from_table(table, spec)
    if not parsed.transactions:
        run.review(
            ReviewKind.UNPARSED,
            {
                "reason": "mapping_failed",
                "table": 0,
                "mapping_id": str(mapping.id),
                "skipped_rows": len(parsed.skipped_rows),
            },
        )
        return
    account = _usable_account(run, target or mapping.account_id)
    if account is None:
        run.review(
            ReviewKind.UNKNOWN_ACCOUNT,
            {
                "reason": "account_required",
                "mapping_id": str(mapping.id),
                "currency": parsed.currency,
                "transactions": len(parsed.transactions),
            },
        )
        return
    repo.mark_used(run.db, mapping)
    if run.document.institution_id is None and mapping.institution_id is not None:
        run.document.institution_id = mapping.institution_id
    _commit_statement(run, parsed, account, statement_index=None)


def _open_items(db: Session, document: Document) -> int:
    return len(
        list(
            db.execute(
                select(ReviewItem.id).where(
                    ReviewItem.household_id == document.household_id,
                    ReviewItem.document_id == document.id,
                    ReviewItem.state == ReviewItemState.OPEN,
                )
            ).scalars()
        )
    )


def import_extraction(
    db: Session,
    document: Document,
    result: ExtractResult,
    *,
    mapping_id: uuid.UUID | None = None,
    account_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
) -> ImportResult:
    """Import the records of ``result`` for ``document`` (module doc, step 2).

    ``user_id`` is the person who asked for a re-run (else the uploader
    decides which accounts may be matched); ``mapping_id``/``account_id``
    are their choices. Runs in the caller's transaction and RLS context.
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
    )
    if result.statements:
        _import_statements(run, result, account_id=account_id)
    elif result.tables:
        _import_table(run, result, mapping_id=mapping_id, account_id=account_id)
    else:
        run.review(ReviewKind.UNPARSED, {"reason": "no_records"})
    if document.account_id is None and len(run.accounts) == 1:
        document.account_id = run.accounts[0]
    if document.institution_id is None and len(run.accounts) == 1:
        account = accounts_repo.get_account(db, document.household_id, run.accounts[0])
        if account is not None and account.institution_id is not None:
            document.institution_id = account.institution_id
    db.flush()
    status = DocumentStatus.NEEDS_REVIEW if _open_items(db, document) else DocumentStatus.PROCESSED
    return ImportResult(status, run.summary())


# ---------------------------------------------------------------------------
# Re-running an import (POST /imports/{document_id}/apply-mapping)
# ---------------------------------------------------------------------------


def _importable(document: Document) -> ExtractResult:
    if document.status not in IMPORTABLE_STATUSES:
        raise _not_applicable("The document is not ready to import.")
    result = _stored_result(document)
    if result is None or not (result.tables or result.statements):
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
    payload = ApplyMappingJob(
        document_id=document.id,
        mapping_id=data.mapping_id,
        account_id=data.account_id,
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
        user_id=payload.requested_by,
    )
    now = clock.utcnow()
    resolution = {
        "by": "apply_mapping",
        "job_id": str(job_id) if job_id else None,
        "mapping_id": outcome.summary.get("mapping_id"),
        "account_id": str(payload.account_id) if payload.account_id else None,
    }
    for item in answered:
        item.state = ReviewItemState.RESOLVED
        item.resolved_at = now
        item.resolved_by = payload.requested_by
        item.resolution = resolution
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
    suggested: MappingSpec | None = None
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
        suggested = suggest_spec(tables[0])
    return ImportPreview(
        document_id=document.id,
        status=document.status,
        kind=result.kind if result is not None else None,
        account_id=document.account_id,
        tables=[_preview_table(i, t, offset, limit) for i, t in enumerate(tables)],
        statements=[_preview_statement(i, s, offset, limit) for i, s in enumerate(statements)],
        warnings=list(result.warnings) if result is not None else [],
        matched_mapping_id=matched,
        suggested_spec=suggested,
        last_import=repo.last_import_log(db, household_id, document.id),
    )
