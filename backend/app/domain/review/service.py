# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue: list, resolve and dismiss (API.md ``/review``).

**Who sees an item.** Owners and admins: every item of the household.
Members and viewers: items of documents they can see
(:func:`app.domain.documents.service.visible_document_filter`) whose payload,
when it names an ``account_id``, names an account they can see. Items
without a document (Phase 3 mail digests) are for owners and admins only.
Anything else is ``404 review-item-not-found``, like another household's
item (F3).

**Who resolves.** Member or above who may change the item's document
(:func:`app.domain.documents.manage.can_edit_document`); the resolution's
own targets (an account to import into, a transaction to delete) must be
editable by the caller as well. A closed item is ``409 review-item-closed``.

**What a resolution does** (body models in ``schemas``):

* ``possible_duplicate`` / ``same_content``: *confirm* -> the document is
  ``rejected`` (kept in quarantine, never downloadable); *reject* -> the
  document goes through the pipeline (``ingest_document``).
* ``possible_duplicate`` / ``statement_overlap`` (a statement whose period
  overlaps one already imported for the account from another document;
  ``app.domain.imports.statement_records``): *confirm* ("same statement")
  records the statement as skipped and writes nothing; *reject* ("import
  anyway") imports the item's statement into the item's account although
  it overlaps (below). Both need edit on the account.
* ``possible_duplicate`` / ``same_content_skipped`` (a byte-identical copy
  that was skipped at upload, a notice): *confirm* only closes the item;
  *reject* ("import it anyway") sends the copy through the pipeline, as for
  ``same_content``.
* ``possible_duplicate`` / ``near_match``: *confirm* -> the imported row is
  deleted and its ``import_hash`` recorded for the kept transaction (the
  kept row takes the hash when it has none, else an ``import_hash_alias``);
  *reject* -> the imported row is ``confirmed``.
* ``possible_duplicate`` / ``near_matches`` (several such rows of one
  statement, :func:`_resolve_near_matches`): the same decision per listed
  row, all in one request (``rows``; without it ``decision`` is for every
  listed row). A decision for a row the item does not list is ``422`` and
  nothing is decided. Rows left without a decision stay in the item, which
  stays open and lists only them; it is resolved with the last one.
* ``unknown_account``: the document is imported into the chosen account
  (``app.domain.imports.service.request_apply`` re-runs the stored
  extraction; a document without one goes through the pipeline again).
  ``remember`` saves the statement's last four digits on the account, or the
  account on the CSV mapping the item names. For one account of a
  multi-account CSV (``group``) the digits are read again from the stored
  table with the item's mapping, and the mapping itself is left alone (it
  serves every account of the file).
* ``unparsed``: ``reprocess`` (pipeline again), ``apply_mapping``
  (re-import with a saved column mapping) or ``answer_sign``
  (:func:`_answer_sign`): what a positive amount means, for an item held
  because nothing proves the sign of its amounts (``auto_import:
  sign_unverified``; any other item is ``422``). When every statement of
  the table waited (``scope: mapping``) the answer is the layout's: it is
  saved on the item's mapping (or on one made from the item's suggestion),
  which is then no longer on trust, and the document is imported with it.
  When the table's other accounts were imported (``scope: account``) the
  mapping is left alone: the resolution records the sign for that account
  of that document (``statement_answers.sign_answer``) and the importer
  reads it on the re-run. Either way no row already imported is flipped.
* ``receipt_match``: the receipt is attached to the chosen transaction
  (``app.ingestion.match.receipts.resolve_receipt_item`` through
  ``documents.manage.link``: the caller edits the document and the
  transaction), or the item records that no transaction applies.
* ``unknown_owner``: the held drop-folder document is assigned to the chosen
  member (and account) and goes through the pipeline; ``remember`` routes
  its subfolder (``app.domain.dropfolder.service.resolve_unknown_owner``).
* ``low_confidence_field``: the named rows of the item's document are
  corrected (:func:`apply_fixes`, through ``transactions.service``: edit on
  their account, audited) and, with ``confirm``, the item's pending rows
  become ``confirmed`` (a ``reconciliation_*`` item: every pending row of
  the document on the item's account). ``import_as_read`` answers a
  brokerage statement, or the lines of one, held back by its figures
  (``investment_reconciliation_failed``, or ``investment_low_confidence``
  / ``investment_lines_held`` whose only doubt is ``reconciliation``:
  :func:`app.domain.investments.statement_import.acceptable_as_read`; any
  other item is ``422``): the item's statement is imported into the item's
  account as printed, without a balance snapshot. Needs edit on the
  account. It is also the confirmation of any kind the local model
  suggested for the statement's rows, whose wording is then kept for the
  household (:func:`_remember_wording`). ``line_fixes`` on an
  ``investment_lines_held`` / ``investment_low_confidence`` item
  (:func:`_correct_held_lines`) types in what a held line should read (its
  date, amount, units or kind): the stored statement is corrected
  (``actions.correct_lines``), the doubts about exactly those fields are
  settled and the document is imported again, so a line with nothing else
  in doubt is written and any other still waits. With ``import_as_read``
  the answer is recorded for the statement as it reads after the
  correction (its key changes with it), and only if what is still in doubt
  may be imported as read.

**Importing a held statement again** ("import anyway", "import as read")
goes through one path (:func:`_import_statement_again`). The answer is
for the item's statement only: the resolution records ``import_anyway`` /
``import_as_read``, the account, the statement (``target``, its index
``statement`` and its key ``statement_key``) and the ``apply-mapping`` job
that imports it again (audited as ``review.resolve``). That re-run, and
every later import of the document, whoever starts it, applies the
document's answers by statement key
(``app.domain.imports.statement_answers``): the answered statement goes to
its account wherever it now sits in the document and is not held again for
what was answered (a statement accepted as read that then overlaps, or the
other way round, needs only the other answer); no other statement is
covered. An item without a key (raised before statements carried one) is
``409 review-item-stale``.

``GET /review/{id}`` also carries ``regions`` and ``alternatives``
(``app.domain.review.assist``); the review tools (correct, remember this
layout, rule draft) are in ``app.domain.review.actions``.

When the last open item of a ``needs_review`` document that the pipeline
accepted is closed, the document becomes ``processed``. Every resolution is
audited (``review.resolve`` / ``review.dismiss``, ids and decisions only)
and emits ``review.resolved`` / ``review.dismissed``.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Mapping, Sequence
from datetime import date
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import Boolean, ColumnElement, String, and_, cast, not_, or_, select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.domain.accounts.models import Account
from app.domain.accounts.visibility import (
    get_visible_account,
    sees_all_accounts,
    visible_account_filter,
)
from app.domain.documents import repo as documents_repo
from app.domain.documents.manage import can_edit_document, may_download, queue_ingestion
from app.domain.documents.models import (
    Blob,
    BlobArea,
    Document,
    DocumentStatus,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.service import visible_document_filter
from app.domain.dropfolder import service as dropfolder_service
from app.domain.imports import repo as imports_repo
from app.domain.imports import service as imports_service
from app.domain.imports import statement_answers, statement_records, wording
from app.domain.imports.schemas import (
    ApplyMapping,
    AssignmentTarget,
    MappingCreate,
    MappingSpec,
    MappingUpdate,
    Sign,
)
from app.domain.investments import statement_import
from app.domain.review import assist, repo
from app.domain.review.schemas import (
    CardAction,
    DuplicateDecision,
    NextStep,
    ResolveDuplicate,
    ResolveLowConfidence,
    ResolveReceiptMatch,
    ResolveUnknownAccount,
    ResolveUnknownOwner,
    ResolveUnparsed,
    ReviewCard,
    ReviewCardPage,
    ReviewDocument,
    ReviewItemOut,
    ReviewPage,
    TransactionFix,
)
from app.domain.transactions import service as transactions_service
from app.domain.transactions.models import ReviewState, Transaction
from app.domain.transactions.schemas import TransactionUpdate
from app.ingestion.match import receipts as receipts_match

#: ``unparsed`` reasons the column-mapping editor answers. A brokerage
#: activity table none of whose rows read (``activity_unreadable``) is one:
#: its columns are checked there. ``no_rows`` is not: nothing is there to map.
MAPPING_REASONS: Final = frozenset(
    {"mapping_required", "mapping_ambiguous", "mapping_failed", "activity_unreadable"}
)
#: Kinds with a typed resolution (the others can only be dismissed for now).
RESOLVABLE: Final = frozenset(
    {
        ReviewKind.POSSIBLE_DUPLICATE,
        ReviewKind.UNKNOWN_ACCOUNT,
        ReviewKind.UNPARSED,
        ReviewKind.UNKNOWN_OWNER,
        ReviewKind.RECEIPT_MATCH,
        ReviewKind.LOW_CONFIDENCE_FIELD,
    }
)
#: ``low_confidence_field`` reasons whose confirmation covers every pending
#: row of the document on the item's account (the statement as a whole).
WHOLE_STATEMENT_REASONS: Final = frozenset({"reconciliation_failed", "reconciliation_unavailable"})
#: Reasons of the findings a reconciled or accepted statement answers
#: (:func:`close_statement_items`).
STATEMENT_FINDINGS: Final = frozenset(
    {
        "reconciliation_failed",
        "reconciliation_unavailable",
        "lines_skipped",
        "low_confidence",
        "balance_mismatch",
        "earlier_rows_kept",
    }
)
#: Resolution keys of the two ways a held statement is imported again.
IMPORT_ANYWAY: Final = statement_answers.IMPORT_ANYWAY
IMPORT_AS_READ: Final = statement_answers.IMPORT_AS_READ
#: Document statuses whose stored extraction the importer can run again.
REIMPORTABLE: Final = frozenset({DocumentStatus.NEEDS_REVIEW, DocumentStatus.PROCESSED})
#: ``possible_duplicate`` reasons: several near duplicates of one statement
#: in one item; a byte-identical copy skipped at upload (a notice).
NEAR_MATCHES: Final = "near_matches"
SAME_CONTENT_SKIPPED: Final = "same_content_skipped"
#: Resolution key of a ``near_matches`` answer that left rows undecided: how
#: many. The item then stays open (:func:`resolve`).
REMAINING: Final = "remaining"
#: ``auto_import`` of an ``unparsed`` item that asks what a positive amount means.
SIGN_UNVERIFIED: Final = "sign_unverified"
#: ``scope`` of such an item: the answer is the layout's, saved on its mapping
#: (the other, ``account``, is ``statement_answers.SCOPE_ACCOUNT``).
SCOPE_MAPPING: Final = "mapping"
#: Name of a mapping made from an item's suggestion when the sign is answered.
ANSWERED_MAPPING_NAME: Final = "Layout answered in Review"
#: ``low_confidence_field`` reasons whose held brokerage lines can be corrected.
CORRECTABLE_LINES: Final = frozenset({statement_import.LINES_HELD, statement_import.LOW_CONFIDENCE})


# ---------------------------------------------------------------------------
# Problems
# ---------------------------------------------------------------------------


def item_not_found() -> ProblemError:
    return ProblemError(404, "review-item-not-found", "Review item not found")


def item_closed() -> ProblemError:
    return ProblemError(
        409, "review-item-closed", "Review item closed", "This item was already resolved."
    )


def kind_mismatch(kind: ReviewKind) -> ProblemError:
    return ProblemError(
        422,
        "review-kind-mismatch",
        "Resolution does not fit the item",
        f"This is a {kind.value} item; send a resolution of that kind.",
    )


def unsupported(kind: ReviewKind) -> ProblemError:
    return ProblemError(
        409,
        "review-resolution-unsupported",
        "Resolution not supported",
        f"{kind.value} items cannot be resolved here yet; dismiss them instead.",
    )


def needs_document() -> ProblemError:
    return ProblemError(
        409,
        "review-item-needs-document",
        "No document",
        "This item is not about a document.",
    )


def _invalid(detail: str) -> ProblemError:
    return ProblemError(422, "invalid-resolution", "Invalid resolution", detail)


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def _household(principal: Principal) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    return principal.household_id


def visible_filter(principal: Principal) -> ColumnElement[bool]:
    """SQL predicate: review items the principal may see (module doc)."""
    household_id = _household(principal)
    if sees_all_accounts(principal):
        return ReviewItem.household_id == household_id
    documents = select(Document.id).where(visible_document_filter(principal))
    accounts = select(cast(Account.id, String)).where(
        Account.household_id == household_id, visible_account_filter(principal, Account.id)
    )
    names_account = ReviewItem.payload.op("?", return_type=Boolean)("account_id")
    named = ReviewItem.payload.op("->>", return_type=String)("account_id")
    return and_(
        ReviewItem.household_id == household_id,
        ReviewItem.document_id.in_(documents),
        or_(not_(names_account), named.in_(accounts)),
    )


def _can_resolve(db: Session, principal: Principal, item: ReviewItem) -> bool:
    if not role_at_least(principal.role, HouseholdRole.MEMBER):
        return False
    if item.document_id is None:
        return sees_all_accounts(principal)
    document = documents_repo.get_document(db, item.household_id, item.document_id)
    return document is not None and can_edit_document(db, principal, document)


def _next_step(item: ReviewItem) -> NextStep | None:
    if item.state is not ReviewItemState.OPEN:
        return None
    if item.kind is ReviewKind.POSSIBLE_DUPLICATE:
        return "confirm_duplicate"
    if item.kind is ReviewKind.UNKNOWN_ACCOUNT:
        return "choose_account"
    if item.kind is ReviewKind.UNKNOWN_OWNER:
        return "choose_owner"
    if item.kind is ReviewKind.RECEIPT_MATCH:
        return "attach_receipt"
    if item.kind is ReviewKind.LOW_CONFIDENCE_FIELD:
        return "correct_fields"
    if item.kind is ReviewKind.UNPARSED:
        reason = item.payload.get("reason")
        return "column_mapping" if reason in MAPPING_REASONS else "reprocess"
    return None


#: MIME types whose pages the sandbox renders as previews.
PREVIEW_MIMES: Final = ("application/pdf", "image/")


def _documents(
    db: Session, principal: Principal, items: Sequence[ReviewItem]
) -> dict[uuid.UUID, ReviewDocument]:
    """The documents ``items`` are about, in one query (with their blob's area)."""
    ids = {item.document_id for item in items if item.document_id is not None}
    if not ids:
        return {}
    household_id = _household(principal)
    rows = db.execute(
        select(Document, Blob.area)
        .join(Blob, (Blob.household_id == Document.household_id) & (Blob.id == Document.blob_id))
        .where(Document.household_id == household_id, Document.id.in_(ids))
    ).all()
    pages = may_download(db, principal)
    return {
        document.id: ReviewDocument(
            id=document.id,
            original_filename=document.original_filename,
            kind=document.kind,
            status=document.status,
            mime_detected=document.mime_detected,
            source=document.source,
            created_at=document.created_at,
            period_start=document.period_start,
            period_end=document.period_end,
            previewable=pages
            and area is not BlobArea.QUARANTINE
            and document.mime_detected.startswith(PREVIEW_MIMES),
        )
        for document, area in rows
    }


def _out(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    *,
    detail: bool = False,
    documents: dict[uuid.UUID, ReviewDocument] | None = None,
) -> ReviewItemOut:
    regions, alternatives = assist.enrich(db, principal, item) if detail else (None, None)
    if documents is None:
        documents = _documents(db, principal, [item])
    reason = (item.payload or {}).get("reason")
    return ReviewItemOut(
        id=item.id,
        kind=item.kind,
        state=item.state,
        document_id=item.document_id,
        document=documents.get(item.document_id) if item.document_id is not None else None,
        reason=reason if isinstance(reason, str) else None,
        payload=dict(item.payload or {}),
        resolution=dict(item.resolution) if item.resolution is not None else None,
        resolved_by=item.resolved_by,
        resolved_at=item.resolved_at,
        created_at=item.created_at,
        next_step=_next_step(item),
        can_resolve=item.state is ReviewItemState.OPEN and _can_resolve(db, principal, item),
        regions=regions,
        alternatives=alternatives,
    )


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def list_items(
    db: Session,
    principal: Principal,
    *,
    state: ReviewItemState | None,
    kind: ReviewKind | None,
    document_id: uuid.UUID | None,
    cursor: uuid.UUID | None,
    limit: int,
) -> ReviewPage:
    household_id = _household(principal)
    rows = repo.list_items(
        db,
        household_id,
        visible_filter(principal),
        state=state,
        kind=kind,
        document_id=document_id,
        cursor=cursor,
        limit=limit + 1,
    )
    more = len(rows) > limit
    rows = rows[:limit]
    documents = _documents(db, principal, rows)
    return ReviewPage(
        items=[_out(db, principal, row, documents=documents) for row in rows],
        next_cursor=rows[-1].id if more and rows else None,
    )


#: Page size of ``GET /review/cards`` (it counts documents).
CARDS_DEFAULT_PAGE: Final = 25
CARDS_MAX_PAGE: Final = 100
#: Most items of no document on the first page of cards.
LOOSE_MAX: Final = 100
#: ``low_confidence_field`` reasons the reconcile view answers whatever the
#: document holds (rows an earlier reading left behind are settled there).
RECONCILE_REASONS: Final = WHOLE_STATEMENT_REASONS | {"earlier_rows_kept"}
#: Reasons the reconcile view answers when the document has a stored bank
#: statement to check against (without one their rows are checked one by one).
STATEMENT_ROW_REASONS: Final = frozenset({"lines_skipped", "low_confidence", "balance_mismatch"})
#: ``unparsed`` reasons "Teach PerFin" answers, on a document with pages to show.
TEACH_REASONS: Final = frozenset({"no_records", "no_rows"})
#: ``unparsed`` reasons nothing can be done about here: reading the file
#: again would end the same way.
UNREADABLE_REASONS: Final = frozenset(
    {"format_not_supported", "encrypted", "depth_limit", "container_incomplete"}
)


def _reason(item: ReviewItem) -> str | None:
    reason = (item.payload or {}).get("reason")
    return reason if isinstance(reason, str) else None


def is_notice(item: ReviewItem) -> bool:
    """Whether the item only tells a person something (``repo.NOTICE_REASONS``)."""
    return _reason(item) in repo.NOTICE_REASONS


def _lists_transactions(item: ReviewItem) -> bool:
    listed = (item.payload or {}).get("transactions")
    return isinstance(listed, list) and any(isinstance(entry, dict) for entry in listed)


def card_action(
    items: Sequence[ReviewItem], *, bank_statement: bool = False, previewable: bool = False
) -> tuple[CardAction, uuid.UUID]:
    """The one thing a document's open ``items`` ask for next, and the item that asks.

    ``items`` are oldest first and not empty. The first rule that an item
    meets wins, in this order (within a rule, the oldest item): where the
    rows go (the account, then the owner), how they are read (the sign,
    then the columns), whether the file is a copy, whether the statement
    adds up, single rows, a receipt's transaction, then a file that was not
    read (taught, processed again, or only acknowledged). A notice never
    comes before an item that waits on a person; anything no rule names is
    acknowledged.

    ``bank_statement``: the document's stored extraction holds a bank
    statement (the reconcile view has something to check against).
    ``previewable``: its pages can be shown (needed to teach a layout).
    """
    waiting = [item for item in items if not is_notice(item)]
    if not waiting:
        return "acknowledge", items[0].id

    def first(kind: ReviewKind, reasons: Collection[str] | None = None) -> ReviewItem | None:
        return next(
            (
                item
                for item in waiting
                if item.kind is kind and (reasons is None or _reason(item) in reasons)
            ),
            None,
        )

    if (found := first(ReviewKind.UNKNOWN_ACCOUNT)) is not None:
        return "choose_account", found.id
    if (found := first(ReviewKind.UNKNOWN_OWNER)) is not None:
        return "choose_owner", found.id
    unparsed = [item for item in waiting if item.kind is ReviewKind.UNPARSED]
    for item in unparsed:
        if (item.payload or {}).get("auto_import") == "sign_unverified":
            return "answer_sign", item.id
    if (found := first(ReviewKind.UNPARSED, MAPPING_REASONS)) is not None:
        return "map_columns", found.id
    if (found := first(ReviewKind.POSSIBLE_DUPLICATE)) is not None:
        return "confirm_duplicate", found.id
    doubts = [item for item in waiting if item.kind is ReviewKind.LOW_CONFIDENCE_FIELD]
    # The finding about the statement as a whole names the action before one
    # about its rows or lines, whichever was raised first.
    if (found := first(ReviewKind.LOW_CONFIDENCE_FIELD, RECONCILE_REASONS)) is not None:
        return "reconcile", found.id
    if bank_statement and (found := first(ReviewKind.LOW_CONFIDENCE_FIELD, STATEMENT_ROW_REASONS)):
        return "reconcile", found.id
    for item in doubts:
        if _lists_transactions(item):
            return "check_rows", item.id
    if (found := first(ReviewKind.RECEIPT_MATCH)) is not None:
        return "attach_receipt", found.id
    if previewable and (found := first(ReviewKind.UNPARSED, TEACH_REASONS)) is not None:
        return "teach", found.id
    for item in unparsed:
        if _reason(item) not in UNREADABLE_REASONS:
            return "reprocess", item.id
    return "acknowledge", waiting[0].id


def list_cards(
    db: Session, principal: Principal, *, cursor: uuid.UUID | None, limit: int
) -> ReviewCardPage:
    """The open items the caller may see, one card per document (by document id).

    ``limit`` counts documents. ``loose`` (open items of no document) is
    filled on the first page only. A card holds only the items the caller
    may see, and its action is chosen among those.
    """
    household_id = _household(principal)
    visible = visible_filter(principal)
    ids = repo.open_documents(db, household_id, visible, cursor=cursor, limit=limit + 1)
    more = len(ids) > limit
    ids = ids[:limit]
    by_document: dict[uuid.UUID, list[ReviewItem]] = {}
    for row in repo.open_items(db, household_id, visible, ids):
        if row.document_id is not None:
            by_document.setdefault(row.document_id, []).append(row)
    loose = repo.loose_items(db, household_id, visible, limit=LOOSE_MAX) if cursor is None else []
    documents = _documents(db, principal, [row for rows in by_document.values() for row in rows])
    with_statement = repo.documents_with_statements(db, household_id, ids)
    cards: list[ReviewCard] = []
    for document_id in ids:
        rows = by_document.get(document_id) or []
        document = documents.get(document_id)
        if not rows or document is None:
            continue  # resolved since the ids were read
        action, action_item_id = card_action(
            rows, bank_statement=document_id in with_statement, previewable=document.previewable
        )
        notices = sum(1 for row in rows if is_notice(row))
        cards.append(
            ReviewCard(
                document=document,
                items=[_out(db, principal, row, documents=documents) for row in rows],
                action=action,
                action_item_id=action_item_id,
                open=len(rows) - notices,
                notices=notices,
            )
        )
    return ReviewCardPage(
        cards=cards,
        loose=[_out(db, principal, row, documents={}) for row in loose],
        next_cursor=ids[-1] if more and ids else None,
    )


def _get(db: Session, principal: Principal, item_id: uuid.UUID, *, lock: bool) -> ReviewItem:
    household_id = _household(principal)
    item = repo.get_item(db, household_id, item_id, visible_filter(principal), lock=lock)
    if item is None:
        raise item_not_found()
    return item


def get_item(db: Session, principal: Principal, item_id: uuid.UUID) -> ReviewItemOut:
    return _out(db, principal, _get(db, principal, item_id, lock=False), detail=True)


def item_out(db: Session, principal: Principal, item: ReviewItem) -> ReviewItemOut:
    return _out(db, principal, item)


def visible_item(db: Session, principal: Principal, item_id: uuid.UUID) -> ReviewItem:
    """The item (``404`` if hidden), not locked."""
    return _get(db, principal, item_id, lock=False)


def open_item_for_change(db: Session, principal: Principal, item_id: uuid.UUID) -> ReviewItem:
    """Resolvable by the caller (``404``/``403``), locked, and still open (``409``)."""
    require_resolvable(db, principal, item_id)
    item = _get(db, principal, item_id, lock=True)
    if item.state is not ReviewItemState.OPEN:
        raise item_closed()
    return item


def require_resolvable(db: Session, principal: Principal, item_id: uuid.UUID) -> ReviewItem:
    """Visible (``404``) and the caller may resolve it (``403``)."""
    item = _get(db, principal, item_id, lock=False)
    if not _can_resolve(db, principal, item):
        raise forbidden("You may not resolve this review item.")
    return item


# ---------------------------------------------------------------------------
# Resolutions
# ---------------------------------------------------------------------------


def item_document(db: Session, item: ReviewItem) -> Document:
    """The item's document, locked (``409 review-item-needs-document`` without one)."""
    return _document(db, item)


def _document(db: Session, item: ReviewItem) -> Document:
    if item.document_id is None:
        raise needs_document()
    document = documents_repo.get_document(db, item.household_id, item.document_id, lock=True)
    if document is None:  # pragma: no cover - items cascade with their document
        raise needs_document()
    return document


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _resolve_duplicate(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    body: ResolveDuplicate,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    payload = item.payload or {}
    reason = payload.get("reason")
    if reason == NEAR_MATCHES:
        return _resolve_near_matches(db, principal, item, body, request)
    if body.rows is not None:
        raise _invalid("rows goes with an item that lists several rows.")
    if reason == statement_records.REASON:
        return _resolve_statement_overlap(db, principal, item, body, request)
    transaction_id = _uuid(payload.get("transaction_id"))
    if transaction_id is None:
        document = _document(db, item)
        if reason == SAME_CONTENT_SKIPPED and body.decision == "confirm":
            # The copy was skipped when it arrived: nothing is left to do but
            # to say it was seen. The document stays as it is.
            return {
                "decision": "confirm",
                "acknowledged": True,
                "duplicate_of": payload.get("duplicate_of"),
            }
        if body.decision == "reject":
            job_id = queue_ingestion(
                db,
                document,
                principal=principal,
                action="document.not_duplicate",
                request=request,
            )
            return {"decision": "reject", "job_id": str(job_id)}
        document.status = DocumentStatus.REJECTED
        document.error = "duplicate"
        db.flush()
        emit(
            db,
            item.household_id,
            "document.updated",
            {"id": str(document.id), "status": str(document.status)},
        )
        return {"decision": "confirm", "duplicate_of": payload.get("duplicate_of")}
    return _resolve_transaction_duplicate(db, principal, item, transaction_id, body, request)


def _payload_date(payload: dict[str, Any], key: str) -> date | None:
    value = payload.get(key)
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _import_statement_again(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    document: Document,
    account: Account,
    *,
    investment: bool,
    answer: str,
    request: HTTPConnection | None,
    key: str | None = None,
) -> dict[str, Any]:
    """Import the item's held statement into the item's account (module doc).

    ``answer`` is :data:`IMPORT_ANYWAY` or :data:`IMPORT_AS_READ`; the
    resolution returned records it for the item's statement key, which the
    queued re-run (and every later import of the document) applies.
    ``key``: the statement's key now, when the stored statement was
    corrected since the item was raised (:func:`_correct_held_lines`).
    """
    if document.extraction is None or document.status not in REIMPORTABLE:
        raise ProblemError(
            409,
            "document-not-importable",
            "Nothing to import",
            "The document has no stored statements to import again; reprocess it instead.",
        )
    payload = item.payload or {}
    if key is None:
        key = statement_answers.payload_key(payload)
    if key is None:
        raise _stale(
            "This item does not say which statement it holds; reprocess the document "
            "to raise a current one."
        )
    index = payload.get("statement")
    target: AssignmentTarget = "investment" if investment else "statement"
    queued = imports_service.request_apply(
        db, principal, document.id, ApplyMapping(), request=request
    )
    return {
        answer: True,
        "account_id": str(account.id),
        "target": target,
        "statement": index if isinstance(index, int) and index >= 0 else None,
        statement_answers.KEY_FIELD: key,
        "rerun": "import",
        "job_id": str(queued.job_id),
    }


def _resolve_statement_overlap(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    body: ResolveDuplicate,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    """Same statement (skip it) or import anyway (module doc)."""
    payload = item.payload or {}
    document = _document(db, item)
    account_id = _uuid(payload.get("account_id"))
    if account_id is None:
        raise _invalid("This item names no account.")
    account = get_visible_account(db, principal, account_id, need="edit")
    investment = payload.get("investment") is True
    other = payload.get("other_document_id")
    if body.decision == "confirm":
        start = _payload_date(payload, "period_start")
        end = _payload_date(payload, "period_end")
        source = payload.get("source_format")
        if start is None or end is None or start > end or not isinstance(source, str):
            raise _invalid("This item names no statement period.")
        statement_records.record_statement(
            db,
            document,
            account,
            statement_records.StatementFacts(
                kind="investment" if investment else "bank",
                source_format=source,
                period_start=start,
                period_end=end,
                stated=True,
            ),
            outcome="skipped",
        )
        db.flush()
        return {"decision": "confirm", "skipped": True, "duplicate_of": other}
    rerun = _import_statement_again(
        db,
        principal,
        item,
        document,
        account,
        investment=investment,
        answer=IMPORT_ANYWAY,
        request=request,
    )
    return {"decision": "reject", **rerun}


def _resolve_transaction_duplicate(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    transaction_id: uuid.UUID,
    body: ResolveDuplicate,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    payload = item.payload or {}
    return _decide_duplicate(
        db,
        principal,
        item.household_id,
        transaction_id,
        _ids(payload.get("candidates")),
        body.decision,
        body.duplicate_of,
        request,
    )


def _ids(values: Any) -> list[uuid.UUID]:
    return [c for c in (_uuid(v) for v in values or []) if c] if isinstance(values, list) else []


def _decide_duplicate(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    transaction_id: uuid.UUID,
    candidates: Sequence[uuid.UUID],
    decision: str,
    duplicate_of: uuid.UUID | None,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    """One imported row beside the rows it may duplicate (``candidates``): the decision.

    *confirm*: the imported row is deleted and its hash recorded for the
    kept one; *reject*: it is confirmed (module doc). Edit on the account.
    """
    try:
        imported: Transaction | None = transactions_service.get_visible_transaction(
            db, principal, transaction_id, need="edit", lock=True
        )
    except ProblemError as exc:
        if exc.slug != "transaction-not-found":
            raise
        imported = None  # removed by hand meanwhile: nothing left to decide
    if decision == "reject":
        if imported is not None and imported.review_state is ReviewState.PENDING:
            imported.review_state = ReviewState.CONFIRMED
            db.flush()
            emit(
                db,
                household_id,
                "transaction.updated",
                {"id": str(imported.id), "account_id": str(imported.account_id)},
            )
        return {"decision": "reject", "transaction_id": str(transaction_id)}
    kept_id = duplicate_of
    if kept_id is None:
        if len(candidates) != 1:
            raise _invalid("Name the transaction this row duplicates in duplicate_of.")
        kept_id = candidates[0]
    elif candidates and kept_id not in candidates:
        raise _invalid("duplicate_of must be one of the item's candidates.")
    if kept_id == transaction_id:
        raise _invalid("A transaction cannot duplicate itself.")
    kept = transactions_service.get_visible_transaction(db, principal, kept_id, need="edit")
    if imported is None:
        return {"decision": "confirm", "duplicate_of": str(kept.id), "deleted": False}
    if kept.account_id != imported.account_id:
        raise _invalid("Both transactions must be on the same account.")
    recorded = None
    if imported.import_hash and imported.import_hash != kept.import_hash:
        if kept.import_hash is None:
            kept.import_hash = imported.import_hash
            recorded = "hash"
        else:
            repo.add_alias(db, household_id, kept.account_id, kept.id, imported.import_hash)
            recorded = "alias"
    transactions_service.delete_transaction(db, principal, imported, request=request)
    db.flush()
    return {
        "decision": "confirm",
        "duplicate_of": str(kept.id),
        "deleted": True,
        "hash_recorded": recorded,
    }


def _resolve_near_matches(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    body: ResolveDuplicate,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    """Decide several near duplicates of one statement in one request (module doc).

    Every named row is checked against the item before anything is decided.
    When rows are left without a decision the item's payload is rewritten to
    list only them and the answer says how many (:data:`REMAINING`): the
    item stays open.
    """
    payload = item.payload or {}
    entries = [
        (found, entry)
        for entry in payload.get("rows") or []
        if isinstance(entry, dict) and (found := _uuid(entry.get("transaction_id")))
    ]
    listed = {found: _ids(entry.get("candidates")) for found, entry in entries}
    if not listed:
        raise _invalid("This item lists no rows.")
    decisions: list[DuplicateDecision]
    if body.rows is None:
        if body.duplicate_of is not None:
            raise _invalid("Name the transaction each row duplicates in rows.")
        decisions = [
            DuplicateDecision(transaction_id=found, decision=body.decision) for found in listed
        ]
    else:
        decisions = list(body.rows)
        if not decisions:
            raise _invalid("Name at least one of the rows this item lists.")
        named = [d.transaction_id for d in decisions]
        if len(set(named)) != len(named):
            raise _invalid("Name each row once.")
        if not set(named) <= listed.keys():
            raise _invalid("Decide only rows this item lists.")
    decided = [
        {
            "transaction_id": str(d.transaction_id),
            **_decide_duplicate(
                db,
                principal,
                item.household_id,
                d.transaction_id,
                listed[d.transaction_id],
                d.decision,
                d.duplicate_of,
                request,
            ),
        }
        for d in decisions
    ]
    done = {d.transaction_id for d in decisions}
    rest = [entry for found, entry in entries if found not in done]
    resolution: dict[str, Any] = {"rows": decided}
    if body.rows is None:
        resolution["decision"] = body.decision
    count = payload.get("count")
    total = count if isinstance(count, int) and not isinstance(count, bool) else len(listed)
    if rest:
        item.payload = {**payload, "rows": rest, "count": max(total - len(done), len(rest))}
        db.flush()
        resolution[REMAINING] = len(rest)
    elif total > len(listed):
        # More rows than an item lists: the others stay pending in the ledger.
        resolution["unlisted"] = total - len(listed)
    return resolution


def _statement_last4(document: Document, index: Any) -> str | None:
    extraction = document.extraction or {}
    statements = extraction.get("statements")
    if not isinstance(statements, list) or not isinstance(index, int):
        return None
    if not 0 <= index < len(statements) or not isinstance(statements[index], dict):
        return None
    account = statements[index].get("account")
    last4 = account.get("last4") if isinstance(account, dict) else None
    return last4 if isinstance(last4, str) and len(last4) == 4 and last4.isdigit() else None


def _reimport(
    db: Session,
    principal: Principal,
    document: Document,
    *,
    mapping_id: uuid.UUID | None,
    account_id: uuid.UUID | None,
    request: HTTPConnection | None,
    group: int | None = None,
    statement: int | None = None,
) -> dict[str, Any]:
    """Import the stored extraction again, else run the whole pipeline again."""
    if document.extraction is not None and document.status in REIMPORTABLE:
        accepted = imports_service.request_apply(
            db,
            principal,
            document.id,
            ApplyMapping(
                mapping_id=mapping_id, account_id=account_id, group=group, statement=statement
            ),
            request=request,
        )
        return {"rerun": "import", "job_id": str(accepted.job_id)}
    job_id = queue_ingestion(
        db, document, principal=principal, action="document.reprocess", request=request
    )
    return {"rerun": "pipeline", "job_id": str(job_id)}


def _resolve_account(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    body: ResolveUnknownAccount,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    document = _document(db, item)
    account = get_visible_account(db, principal, body.account_id, need="edit")
    payload = item.payload or {}
    remembered: list[str] = []
    grouped = isinstance(payload.get("group"), int)
    if grouped and not imports_service.group_item_current(db, document, payload):
        # The mapping changed since the item was raised: its group may now
        # be another account's rows (a re-import raises current items).
        raise ProblemError(
            409,
            "review-item-stale",
            "Review item is out of date",
            "The column mapping changed since this item was raised; import the document again.",
        )
    if body.remember:
        if grouped:
            last4 = imports_service.stored_group_last4(db, document, payload)
        elif payload.get("investment") is True:
            # A brokerage statement (OFX, PDF, activity table): its digits
            # from the stored extraction, never from the item.
            last4 = imports_service.stored_investment_last4(document, payload, db)
        else:
            last4 = _statement_last4(document, payload.get("statement"))
        mapping_id = None if grouped else _uuid(payload.get("mapping_id"))
        if last4 is not None:
            if account.number_last4 is None:
                account.number_last4 = last4
                remembered.append("last4")
                emit(db, item.household_id, "account.updated", {"account_id": str(account.id)})
            elif account.number_last4 != last4:
                raise ProblemError(
                    409,
                    "account-last4-conflict",
                    "Account already has other digits",
                    "The account already records different last four digits.",
                )
        if mapping_id is not None:
            imports_service.update_mapping(
                db, principal, mapping_id, MappingUpdate(account_id=account.id), request=request
            )
            remembered.append("mapping")
    if document.account_id is None:
        document.account_id = account.id
    db.flush()
    rerun = _reimport(
        db,
        principal,
        document,
        mapping_id=None,
        account_id=account.id,
        request=request,
        # A multi-account table: the account is for this item's group only.
        group=payload["group"] if grouped else None,
        # A brokerage document: the account is for this item's statement only.
        statement=payload["statement"]
        if payload.get("investment") is True and isinstance(payload.get("statement"), int)
        else None,
    )
    return {"account_id": str(account.id), "remembered": remembered, **rerun}


def _resolve_unparsed(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    body: ResolveUnparsed,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    document = _document(db, item)
    if body.action == "answer_sign":
        return _answer_sign(db, principal, item, document, body, request)
    if body.positive_is is not None:
        raise _invalid("positive_is goes with answer_sign.")
    if body.action == "reprocess":
        if body.mapping_id is not None or body.account_id is not None:
            raise _invalid("reprocess takes no mapping_id or account_id.")
        job_id = queue_ingestion(
            db, document, principal=principal, action="document.reprocess", request=request
        )
        return {"action": "reprocess", "job_id": str(job_id)}
    if document.extraction is None or document.status not in REIMPORTABLE:
        raise _no_table()
    rerun = _reimport(
        db,
        principal,
        document,
        mapping_id=body.mapping_id,
        account_id=body.account_id,
        request=request,
    )
    return {"action": "apply_mapping", **rerun}


def _no_table() -> ProblemError:
    return ProblemError(
        409,
        "document-not-importable",
        "Nothing to import",
        "The document has no extracted table to map; reprocess it instead.",
    )


def _stale(detail: str) -> ProblemError:
    return ProblemError(409, "review-item-stale", "Review item is out of date", detail)


def _answered_mapping_name(db: Session, household_id: uuid.UUID) -> str:
    """A free name for the mapping a sign answer makes (never text of the file)."""
    if not imports_repo.name_taken(db, household_id, ANSWERED_MAPPING_NAME):
        return ANSWERED_MAPPING_NAME
    for number in range(2, 100):
        candidate = f"{ANSWERED_MAPPING_NAME} ({number})"
        if not imports_repo.name_taken(db, household_id, candidate):
            return candidate
    return f"{ANSWERED_MAPPING_NAME} ({uuid.uuid4().hex[:8]})"


def _answer_sign(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    document: Document,
    body: ResolveUnparsed,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    """What a positive amount means, for an item that asks (module doc).

    Nothing already imported is touched: the answer only decides how the
    rows that waited are read when the document is imported again.
    """
    payload = item.payload or {}
    if payload.get("auto_import") != SIGN_UNVERIFIED:
        raise _invalid("This item does not ask what a positive amount means.")
    if body.positive_is is None:
        raise _invalid("Say what a positive amount means in positive_is.")
    if body.mapping_id is not None or body.account_id is not None:
        raise _invalid("answer_sign takes no mapping_id or account_id.")
    if document.extraction is None or document.status not in REIMPORTABLE:
        raise _no_table()
    sign = Sign.OUTFLOW_POSITIVE if body.positive_is == "money_out" else Sign.INFLOW_POSITIVE
    account_id = _uuid(payload.get("account_id"))
    if account_id is not None:
        # The rows that waited are written there.
        get_visible_account(db, principal, account_id, need="edit")
    if payload.get("scope") == statement_answers.SCOPE_ACCOUNT:
        if account_id is None:
            raise _invalid("This item names no account.")
        # The table's other accounts were imported with the mapping as it
        # is: it is left alone, and the re-run names no mapping (naming one
        # would answer every other account's question with it). The importer
        # reads this resolution (``statement_answers.document_answers``).
        rerun = _reimport(
            db, principal, document, mapping_id=None, account_id=None, request=request
        )
        return {
            "action": statement_answers.SIGN_ACTION,
            **statement_answers.sign_answer(
                sign.value, account_id, statement_answers.SCOPE_ACCOUNT
            ),
            **rerun,
        }
    # Every statement of the table waited: the answer is the layout's sign.
    mapping_id = _uuid(payload.get("mapping_id"))
    if mapping_id is not None:
        try:
            spec = imports_service.get_mapping(db, principal, mapping_id).spec
        except ProblemError as exc:
            if exc.status != 404:
                raise
            raise _stale(
                "The column mapping this item names is gone; import the document again."
            ) from exc
    else:
        try:
            spec = MappingSpec.model_validate(payload.get("suggested_spec"))
        except ValidationError as exc:
            raise _stale(
                "This item carries no columns to read the file with; map its columns instead."
            ) from exc
    signed = MappingSpec.model_validate({**spec.model_dump(mode="json"), "sign": sign.value})
    if mapping_id is not None:
        # Saving a spec says a person stands behind it: no longer on trust.
        imports_service.update_mapping(
            db, principal, mapping_id, MappingUpdate(spec=signed), request=request
        )
    else:
        mapping_id = imports_service.create_mapping(
            db,
            principal,
            MappingCreate(
                name=_answered_mapping_name(db, item.household_id),
                spec=signed,
                document_id=document.id,
            ),
            request=request,
        ).id
    rerun = _reimport(
        db, principal, document, mapping_id=mapping_id, account_id=None, request=request
    )
    return {
        "action": statement_answers.SIGN_ACTION,
        statement_answers.SIGN: sign.value,
        statement_answers.SCOPE: SCOPE_MAPPING,
        "mapping_id": str(mapping_id),
        **rerun,
    }


def apply_fixes(
    db: Session,
    principal: Principal,
    document: Document,
    fixes: list[TransactionFix],
    *,
    request: HTTPConnection | None,
) -> list[uuid.UUID]:
    """Correct rows imported from ``document`` (edit on their account); returns their ids."""
    if len({f.transaction_id for f in fixes}) != len(fixes):
        raise _invalid("Name each transaction once.")
    changed: list[uuid.UUID] = []
    for fix in fixes:
        row = transactions_service.get_visible_transaction(
            db, principal, fix.transaction_id, need="edit", lock=True
        )
        if row.source_document_id != document.id:
            raise _invalid("Only rows imported from this document can be corrected here.")
        update = TransactionUpdate.model_validate(
            fix.model_dump(mode="json", include=fix.model_fields_set - {"transaction_id"})
        )
        if update.model_fields_set:
            transactions_service.update_transaction(db, principal, row, update, request=request)
            changed.append(row.id)
    return changed


def _pending_rows(
    db: Session, principal: Principal, item: ReviewItem, document: Document
) -> list[Transaction]:
    payload = item.payload or {}
    if payload.get("reason") in WHOLE_STATEMENT_REASONS:
        account_id = _uuid(payload.get("account_id"))
        if account_id is None:
            return []
        ids = list(
            db.execute(
                select(Transaction.id).where(
                    Transaction.household_id == item.household_id,
                    Transaction.account_id == account_id,
                    Transaction.source_document_id == document.id,
                    Transaction.review_state == ReviewState.PENDING,
                )
            ).scalars()
        )
    else:
        ids = [
            found
            for entry in payload.get("transactions") or []
            if isinstance(entry, dict) and (found := _uuid(entry.get("transaction_id")))
        ]
    rows: list[Transaction] = []
    for transaction_id in ids:
        try:
            row = transactions_service.get_visible_transaction(
                db, principal, transaction_id, need="edit", lock=True
            )
        except ProblemError as exc:
            if exc.slug != "transaction-not-found":
                raise
            continue  # removed meanwhile
        if row.source_document_id == document.id and row.review_state is ReviewState.PENDING:
            rows.append(row)
    return rows


def confirm_pending(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    document: Document,
    *,
    request: HTTPConnection | None,
) -> int:
    """Move the item's pending rows (the statement's, for a reconciliation item) to confirmed."""
    return _confirm(db, principal, _pending_rows(db, principal, item, document), request=request)


def _confirm(
    db: Session,
    principal: Principal,
    rows: Sequence[Transaction],
    *,
    request: HTTPConnection | None,
) -> int:
    for row in rows:
        transactions_service.update_transaction(
            db,
            principal,
            row,
            TransactionUpdate(review_state=ReviewState.CONFIRMED),
            request=request,
        )
    return len(rows)


def confirm_rows(
    db: Session,
    principal: Principal,
    transaction_ids: Sequence[uuid.UUID],
    *,
    request: HTTPConnection | None,
) -> int:
    """Move those of ``transaction_ids`` that still wait to confirmed; how many did.

    Each row is read again for edit and locked; one that is gone, or no
    longer pending, is passed over.
    """
    rows: list[Transaction] = []
    for transaction_id in transaction_ids:
        try:
            row = transactions_service.get_visible_transaction(
                db, principal, transaction_id, need="edit", lock=True
            )
        except ProblemError as exc:
            if exc.slug != "transaction-not-found":
                raise
            continue  # removed meanwhile
        if row.review_state is ReviewState.PENDING:
            rows.append(row)
    return _confirm(db, principal, rows, request=request)


def _names_statement(
    payload: Mapping[str, Any],
    account_id: uuid.UUID,
    statement: int | None,
    transaction_ids: Collection[uuid.UUID] | None,
) -> bool:
    """A finding of ``account_id`` that is about the statement being closed."""
    if payload.get("reason") not in STATEMENT_FINDINGS:
        return False
    if _uuid(payload.get("account_id")) != account_id:
        return False
    index = payload.get("statement")
    if isinstance(index, int) and not isinstance(index, bool) and statement is not None:
        return index == statement
    if transaction_ids is None:
        return True
    listed = [
        found
        for entry in payload.get("transactions") or []
        if isinstance(entry, dict) and (found := _uuid(entry.get("transaction_id")))
    ]
    # A finding that lists rows is this statement's when one of them is.
    return not listed or any(found in transaction_ids for found in listed)


def close_statement_items(
    db: Session,
    principal: Principal,
    document: Document,
    account_id: uuid.UUID,
    resolution: dict[str, Any],
    *,
    statement: int | None = None,
    transaction_ids: Collection[uuid.UUID] | None = None,
    whole_document: bool = False,
    request: HTTPConnection | None,
) -> int:
    """Resolve the open findings of one statement of ``document``; how many were.

    The findings are the document's open items on ``account_id`` whose
    reason is in :data:`STATEMENT_FINDINGS`. An item that names another
    statement of the document (``statement``) is left, and so is one whose
    rows are all outside ``transaction_ids`` when that is given (the
    document holds another statement of the account). A finding about the
    whole document (``lines_skipped`` with no account: lines no reader could
    read) is closed only with ``whole_document`` (the caller knows every
    statement of the document) and with the last question: when no other
    item of the document would stay open. Every other item of the document
    (an account to choose, a duplicate) is untouched.
    """
    found = list(
        db.execute(
            select(ReviewItem)
            .where(
                ReviewItem.household_id == document.household_id,
                ReviewItem.document_id == document.id,
                ReviewItem.state == ReviewItemState.OPEN,
            )
            .order_by(ReviewItem.created_at, ReviewItem.id)
            .with_for_update()
        ).scalars()
    )
    mine = [
        item
        for item in found
        if _names_statement(item.payload or {}, account_id, statement, transaction_ids)
    ]
    if whole_document:
        wide = [
            item
            for item in found
            if (item.payload or {}).get("reason") in STATEMENT_FINDINGS
            and (item.payload or {}).get("account_id") is None
            # Rows of a brokerage table that did not read are another question.
            and "unknown_activity" not in (item.payload or {})
        ]
        settled = {item.id for item in (*mine, *wide)}
        if all(item.id in settled for item in found):
            mine.extend(wide)
    for item in mine:
        answer = dict(resolution)
        ref = (item.payload or {}).get("statement_ref")
        if isinstance(ref, str):
            answer["statement_ref"] = ref
        close_item(db, principal, item, answer, request=request)
    return len(mine)


def _chosen_lines(payload: Mapping[str, Any], lines: Sequence[int] | None) -> list[int] | None:
    """The lines of the item an answer names, checked against what it listed.

    Only an item whose lines stand alone (``selectable``: none moves units)
    can be answered line by line, and only with lines it lists.
    """
    if lines is None:
        return None
    listed = {
        entry.get("index")
        for entry in payload.get("lines") or []
        if isinstance(entry, dict) and isinstance(entry.get("index"), int)
    }
    if payload.get("selectable") is not True:
        raise _invalid("The lines of this item can only be imported together.")
    if not lines or not set(lines) <= listed:
        raise _invalid("Name at least one of the lines this item lists.")
    return sorted(set(lines))


def _import_as_read(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    request: HTTPConnection | None,
    *,
    lines: Sequence[int] | None = None,
    fields: Collection[str] | None = None,
    key: str | None = None,
) -> dict[str, Any]:
    """Import a held brokerage statement as printed (module doc).

    ``lines``: only these lines of the item (:func:`_chosen_lines`); the
    others it lists are left out, and no item is raised for them again.
    ``fields`` and ``key``: what is still in doubt, and the statement's key,
    after its lines were corrected (:func:`_correct_held_lines`); the item's
    own otherwise.
    """
    payload = item.payload or {}
    if fields is not None:
        payload = {**payload, "fields": sorted(fields)}
    if not statement_import.acceptable_as_read(payload):
        raise _invalid(
            "Only a brokerage statement held back because its figures do not add up "
            "can be imported as read."
        )
    chosen = _chosen_lines(payload, lines)
    document = _document(db, item)
    account_id = _uuid(payload.get("account_id"))
    if account_id is None:
        raise _invalid("This item names no account.")
    account = get_visible_account(db, principal, account_id, need="edit")
    rerun = _import_statement_again(
        db,
        principal,
        item,
        document,
        account,
        investment=True,
        answer=IMPORT_AS_READ,
        request=request,
        key=key,
    )
    failed = payload.get("failed")
    return {
        **rerun,
        "reason": payload.get("reason"),
        "failed": [f for f in failed if isinstance(f, str)] if isinstance(failed, list) else [],
        "snapshot": False,
        **({statement_answers.LINES_FIELD: chosen} if chosen is not None else {}),
        **_remember_wording(db, principal, document, rerun, request, lines=chosen),
    }


def _remember_wording(
    db: Session,
    principal: Principal,
    document: Document,
    answered: Mapping[str, Any],
    request: HTTPConnection | None,
    *,
    lines: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Keep the wording whose suggested kinds this answer confirmed (ADR-0010).

    "Import as read" on a statement with rows the local model typed is the
    person's confirmation of those kinds; the wording (a heading, a
    broker's action words: never a detail of the statement) is kept for the
    household's later statements of that layout
    (``app.domain.imports.wording``). ``{"wording_learned": n}`` when any was.
    """
    key = answered.get(statement_answers.KEY_FIELD)
    statement = next(
        (
            s
            for s in imports_service.stored_investments(document, db)
            if statement_answers.statement_key(s) == key
        ),
        None,
    )
    if statement is None:
        return {}
    if lines is not None:
        # Only the lines the person chose were confirmed.
        chosen = set(lines)
        statement = statement.model_copy(
            update={"activity": [a for i, a in enumerate(statement.activity) if i in chosen]}
        )
    learned = wording.remember_confirmed(db, principal, document, statement, request=request)
    return {"wording_learned": learned} if learned else {}


def _correct_held_lines(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    body: ResolveLowConfidence,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    """Correct held brokerage lines, then import the document again (module doc).

    The stored statement is rewritten, so its key changes: an "import as
    read" answer given with the correction is recorded under the new key,
    and an earlier answer about the statement as it read before (lines left
    out, say) no longer applies: what is still in doubt is asked again.
    """
    # ``actions`` imports this module: bound here, when both are loaded.
    from app.domain.review import actions

    payload = item.payload or {}
    if payload.get("reason") not in CORRECTABLE_LINES:
        raise _invalid("Only the held lines of a brokerage statement can be corrected this way.")
    if body.transactions:
        raise _invalid("line_fixes takes no transactions.")
    document = _document(db, item)
    if document.extraction is None or document.status not in REIMPORTABLE:
        raise ProblemError(
            409,
            "document-not-importable",
            "Nothing to import",
            "The document has no stored statements to import again; reprocess it instead.",
        )
    account_id = _uuid(payload.get("account_id"))
    if account_id is None:
        raise _invalid("This item names no account.")
    get_visible_account(db, principal, account_id, need="edit")
    index = payload.get("statement")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise _stale(
            "This item does not say which statement it holds; reprocess the document "
            "to raise a current one."
        )
    fixed = actions.correct_lines(db, document, index, body.line_fixes, item=item)
    statement = fixed.statement
    key = statement_answers.statement_key(statement)
    corrected = {
        "line_fixes": [
            {"line": line, "fields": list(names)} for line, names in sorted(fixed.fields.items())
        ],
        "corrected": fixed.changed,
    }
    # What is in doubt now, read from the statement itself; the item's own
    # ``reconciliation`` doubt (nothing to check a document against) stays.
    doubts = {f for position in statement.positions for f in position.low_confidence}
    doubts.update(f for line in statement.activity for f in line.low_confidence)
    listed = payload.get("fields")
    if isinstance(listed, list) and statement_import.RECONCILIATION_DOUBT in listed:
        doubts.add(statement_import.RECONCILIATION_DOUBT)
    if body.import_as_read and doubts:
        answered = _import_as_read(
            db, principal, item, request, lines=body.lines, fields=doubts, key=key
        )
        return {**answered, **corrected}
    # Nothing to accept (or nobody said to): the corrected lines are imported
    # when nothing else holds them, and whatever still does is asked again.
    _chosen_lines(payload, body.lines)
    queued = imports_service.request_apply(
        db, principal, document.id, ApplyMapping(), request=request
    )
    return {
        **corrected,
        "account_id": str(account_id),
        "target": "investment",
        "statement": index,
        statement_answers.KEY_FIELD: key,
        "rerun": "import",
        "job_id": str(queued.job_id),
    }


def _resolve_low_confidence(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    body: ResolveLowConfidence,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    if body.line_fixes:
        return _correct_held_lines(db, principal, item, body, request)
    if body.import_as_read:
        return _import_as_read(db, principal, item, request, lines=body.lines)
    document = _document(db, item)
    corrected = apply_fixes(db, principal, document, body.transactions, request=request)
    confirmed: list[uuid.UUID] = []
    if body.confirm:
        for row in _pending_rows(db, principal, item, document):
            transactions_service.update_transaction(
                db,
                principal,
                row,
                TransactionUpdate(review_state=ReviewState.CONFIRMED),
                request=request,
            )
            confirmed.append(row.id)
    db.flush()
    return {
        "corrected": [str(i) for i in corrected],
        "confirmed": len(confirmed),
        "confirm": body.confirm,
    }


def close_item(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    resolution: dict[str, Any],
    *,
    request: HTTPConnection | None,
) -> None:
    """Resolve an open item with ``resolution`` (audited, event, settles the document)."""
    _close(db, principal, item, ReviewItemState.RESOLVED, resolution, request=request)


def _close(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    state: ReviewItemState,
    resolution: dict[str, Any],
    *,
    request: HTTPConnection | None,
) -> None:
    item.state = state
    item.resolution = resolution
    item.resolved_by = principal.user_id
    item.resolved_at = clock.utcnow()
    db.flush()
    _settle_document(db, item)
    verb = "resolve" if state is ReviewItemState.RESOLVED else "dismiss"
    audit(
        db,
        action=f"review.{verb}",
        target_type="review_item",
        target_id=item.id,
        after={
            "kind": str(item.kind),
            "document_id": item.document_id,
            "resolution": resolution,
        },
        household_id=item.household_id,
        user_id=principal.user_id,
        request=request,
    )
    event: dict[str, Any] = {"id": str(item.id), "kind": str(item.kind)}
    if item.document_id is not None:
        event["document_id"] = str(item.document_id)
    account_id = (item.payload or {}).get("account_id")
    if isinstance(account_id, str):
        event["account_id"] = account_id
    emit(db, item.household_id, f"review.{state.value}", event)


def _settle_document(db: Session, item: ReviewItem) -> None:
    """``needs_review`` -> ``processed`` once an accepted document has nothing open."""
    if item.document_id is None:
        return
    document = documents_repo.get_document(db, item.household_id, item.document_id)
    if document is None or document.status is not DocumentStatus.NEEDS_REVIEW:
        return
    if repo.open_items_of(db, item.household_id, document.id):
        return
    blob = documents_repo.get_blob(db, item.household_id, document.blob_id)
    if blob is None or blob.area is not BlobArea.PERMANENT:
        return
    document.status = DocumentStatus.PROCESSED
    db.flush()
    emit(
        db,
        item.household_id,
        "document.updated",
        {"id": str(document.id), "status": str(document.status)},
    )


def resolve(
    db: Session,
    principal: Principal,
    item_id: uuid.UUID,
    body: ResolveDuplicate
    | ResolveUnknownAccount
    | ResolveUnparsed
    | ResolveUnknownOwner
    | ResolveReceiptMatch
    | ResolveLowConfidence,
    *,
    request: HTTPConnection | None,
) -> ReviewItemOut:
    require_resolvable(db, principal, item_id)
    item = _get(db, principal, item_id, lock=True)
    if item.state is not ReviewItemState.OPEN:
        raise item_closed()
    if item.kind not in RESOLVABLE:
        raise unsupported(item.kind)
    if body.kind != item.kind.value:
        raise kind_mismatch(item.kind)
    resolution: dict[str, Any]
    if isinstance(body, ResolveDuplicate):
        resolution = _resolve_duplicate(db, principal, item, body, request)
    elif isinstance(body, ResolveUnknownAccount):
        resolution = _resolve_account(db, principal, item, body, request)
    elif isinstance(body, ResolveUnparsed):
        resolution = _resolve_unparsed(db, principal, item, body, request)
    elif isinstance(body, ResolveUnknownOwner):
        resolution = dropfolder_service.resolve_unknown_owner(
            db,
            principal,
            item,
            _document(db, item),
            user_id=body.user_id,
            account_id=body.account_id,
            remember=body.remember,
            request=request,
        )
    elif isinstance(body, ResolveReceiptMatch):
        resolution = receipts_match.resolve_receipt_item(
            db, principal, item, transaction_id=body.transaction_id, request=request
        )
    elif isinstance(body, ResolveLowConfidence):
        resolution = _resolve_low_confidence(db, principal, item, body, request)
    else:  # pragma: no cover - the union is exhaustive
        raise unsupported(item.kind)
    if isinstance(body, ResolveDuplicate) and resolution.get(REMAINING):
        # Some rows of the item were decided: it stays open for the rest.
        audit(
            db,
            action="review.resolve",
            target_type="review_item",
            target_id=item.id,
            after={
                "kind": str(item.kind),
                "document_id": item.document_id,
                "resolution": resolution,
            },
            household_id=item.household_id,
            user_id=principal.user_id,
            request=request,
        )
        return _out(db, principal, item)
    _close(db, principal, item, ReviewItemState.RESOLVED, resolution, request=request)
    return _out(db, principal, item)


def dismiss(
    db: Session,
    principal: Principal,
    item_id: uuid.UUID,
    *,
    request: HTTPConnection | None,
) -> ReviewItemOut:
    require_resolvable(db, principal, item_id)
    item = _get(db, principal, item_id, lock=True)
    if item.state is not ReviewItemState.OPEN:
        raise item_closed()
    _close(db, principal, item, ReviewItemState.DISMISSED, {"action": "dismiss"}, request=request)
    return _out(db, principal, item)
