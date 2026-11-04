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
* ``possible_duplicate`` / ``near_match``: *confirm* -> the imported row is
  deleted and its ``import_hash`` recorded for the kept transaction (the
  kept row takes the hash when it has none, else an ``import_hash_alias``);
  *reject* -> the imported row is ``confirmed``.
* ``unknown_account``: the document is imported into the chosen account
  (``app.domain.imports.service.request_apply`` re-runs the stored
  extraction; a document without one goes through the pipeline again).
  ``remember`` saves the statement's last four digits on the account, or the
  account on the CSV mapping the item names.
* ``unparsed``: ``reprocess`` (pipeline again) or ``apply_mapping``
  (re-import with a saved column mapping).

When the last open item of a ``needs_review`` document that the pipeline
accepted is closed, the document becomes ``processed``. Every resolution is
audited (``review.resolve`` / ``review.dismiss``, ids and decisions only)
and emits ``review.resolved`` / ``review.dismissed``.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

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
from app.domain.documents.manage import can_edit_document, queue_ingestion
from app.domain.documents.models import (
    BlobArea,
    Document,
    DocumentStatus,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.service import visible_document_filter
from app.domain.imports import service as imports_service
from app.domain.imports.schemas import ApplyMapping, MappingUpdate
from app.domain.review import repo
from app.domain.review.schemas import (
    NextStep,
    ResolveDuplicate,
    ResolveUnknownAccount,
    ResolveUnparsed,
    ReviewItemOut,
    ReviewPage,
)
from app.domain.transactions import service as transactions_service
from app.domain.transactions.models import ReviewState, Transaction

#: ``unparsed`` reasons the column-mapping editor answers.
MAPPING_REASONS: Final = frozenset({"mapping_required", "mapping_ambiguous", "mapping_failed"})
#: Kinds with a typed resolution (the others can only be dismissed for now).
RESOLVABLE: Final = frozenset(
    {ReviewKind.POSSIBLE_DUPLICATE, ReviewKind.UNKNOWN_ACCOUNT, ReviewKind.UNPARSED}
)
#: Document statuses whose stored extraction the importer can run again.
REIMPORTABLE: Final = frozenset({DocumentStatus.NEEDS_REVIEW, DocumentStatus.PROCESSED})


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
    if item.kind is ReviewKind.UNPARSED:
        reason = item.payload.get("reason")
        return "column_mapping" if reason in MAPPING_REASONS else "reprocess"
    return None


def _out(db: Session, principal: Principal, item: ReviewItem) -> ReviewItemOut:
    return ReviewItemOut(
        id=item.id,
        kind=item.kind,
        state=item.state,
        document_id=item.document_id,
        payload=dict(item.payload or {}),
        resolution=dict(item.resolution) if item.resolution is not None else None,
        resolved_by=item.resolved_by,
        resolved_at=item.resolved_at,
        created_at=item.created_at,
        next_step=_next_step(item),
        can_resolve=item.state is ReviewItemState.OPEN and _can_resolve(db, principal, item),
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
    return ReviewPage(
        items=[_out(db, principal, row) for row in rows],
        next_cursor=rows[-1].id if more and rows else None,
    )


def _get(db: Session, principal: Principal, item_id: uuid.UUID, *, lock: bool) -> ReviewItem:
    household_id = _household(principal)
    item = repo.get_item(db, household_id, item_id, visible_filter(principal), lock=lock)
    if item is None:
        raise item_not_found()
    return item


def get_item(db: Session, principal: Principal, item_id: uuid.UUID) -> ReviewItemOut:
    return _out(db, principal, _get(db, principal, item_id, lock=False))


def require_resolvable(db: Session, principal: Principal, item_id: uuid.UUID) -> ReviewItem:
    """Visible (``404``) and the caller may resolve it (``403``)."""
    item = _get(db, principal, item_id, lock=False)
    if not _can_resolve(db, principal, item):
        raise forbidden("You may not resolve this review item.")
    return item


# ---------------------------------------------------------------------------
# Resolutions
# ---------------------------------------------------------------------------


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
    transaction_id = _uuid(payload.get("transaction_id"))
    if transaction_id is None:
        document = _document(db, item)
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


def _resolve_transaction_duplicate(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    transaction_id: uuid.UUID,
    body: ResolveDuplicate,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    payload = item.payload or {}
    candidates = [c for c in (_uuid(v) for v in payload.get("candidates") or []) if c]
    try:
        imported: Transaction | None = transactions_service.get_visible_transaction(
            db, principal, transaction_id, need="edit", lock=True
        )
    except ProblemError as exc:
        if exc.slug != "transaction-not-found":
            raise
        imported = None  # removed by hand meanwhile: nothing left to decide
    if body.decision == "reject":
        if imported is not None and imported.review_state is ReviewState.PENDING:
            imported.review_state = ReviewState.CONFIRMED
            db.flush()
            emit(
                db,
                item.household_id,
                "transaction.updated",
                {"id": str(imported.id), "account_id": str(imported.account_id)},
            )
        return {"decision": "reject", "transaction_id": str(transaction_id)}
    kept_id = body.duplicate_of
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
            repo.add_alias(db, item.household_id, kept.account_id, kept.id, imported.import_hash)
            recorded = "alias"
    transactions_service.delete_transaction(db, principal, imported, request=request)
    db.flush()
    return {
        "decision": "confirm",
        "duplicate_of": str(kept.id),
        "deleted": True,
        "hash_recorded": recorded,
    }


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
) -> dict[str, Any]:
    """Import the stored extraction again, else run the whole pipeline again."""
    if document.extraction is not None and document.status in REIMPORTABLE:
        accepted = imports_service.request_apply(
            db,
            principal,
            document.id,
            ApplyMapping(mapping_id=mapping_id, account_id=account_id),
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
    if body.remember:
        last4 = _statement_last4(document, payload.get("statement"))
        mapping_id = _uuid(payload.get("mapping_id"))
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
        db, principal, document, mapping_id=None, account_id=account.id, request=request
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
    if body.action == "reprocess":
        if body.mapping_id is not None or body.account_id is not None:
            raise _invalid("reprocess takes no mapping_id or account_id.")
        job_id = queue_ingestion(
            db, document, principal=principal, action="document.reprocess", request=request
        )
        return {"action": "reprocess", "job_id": str(job_id)}
    if document.extraction is None or document.status not in REIMPORTABLE:
        raise ProblemError(
            409,
            "document-not-importable",
            "Nothing to import",
            "The document has no extracted table to map; reprocess it instead.",
        )
    rerun = _reimport(
        db,
        principal,
        document,
        mapping_id=body.mapping_id,
        account_id=body.account_id,
        request=request,
    )
    return {"action": "apply_mapping", **rerun}


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
    body: ResolveDuplicate | ResolveUnknownAccount | ResolveUnparsed,
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
    else:  # pragma: no cover - the union is exhaustive
        raise unsupported(item.kind)
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
