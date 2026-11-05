# SPDX-License-Identifier: AGPL-3.0-or-later
"""Page previews, word regions and review-item enrichment (INGESTION.md "Review queue").

Everything here reads what the worker already stored: the sandbox-rendered
page PNGs (encrypted blobs, ``app.domain.documents.previews``) and the
validated extraction in ``document.extraction``. Nothing opens or parses the
original file (ADR-0006/0009).

**Who sees what** (SECURITY.md I1, F3):

* ``GET /documents/{id}/preview?page=`` shows the document's content, so it
  follows the download rule: a caller who can see the document *and* is a
  member or above, or a viewer in a household whose setting
  ``documents.viewer_download`` is ``true`` (else ``403``). A document still
  in quarantine is never shown (``409 document-quarantined``). The PNG is
  checked again (plain PNG chunks only, C5) and sent inline as
  ``image/png`` with ``nosniff``, a ``sandbox`` CSP and ``no-store``; each
  page served is audited (``document.preview``).
* ``GET /documents/{id}/regions?page=`` gives the page's geometry to anyone
  who can see the document, and its words (box, line, text, confidence) only
  to callers who may see the content (the same rule as above,
  ``text_included``). Others get an empty ``words`` list: per-word boxes
  alone would show the layout of a document they may not open, such as how
  many digits each figure has (security review P34-SR-04).
* An item's ``regions`` (where a doubtful row or an account number was
  read) are boxes only. ``alternatives`` come from the ledger and the
  household (accounts, institutions, transactions the caller can see) and,
  for callers who may see the content, from the words on the row's line.
"""

from __future__ import annotations

import uuid
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.principal import Principal
from app.core.audit import audit
from app.core.crypto import DecryptionError, Keyring
from app.core.errors import ProblemError
from app.core.headers import DOWNLOAD_CSP, NO_STORE
from app.domain.accounts.models import Account, Institution
from app.domain.accounts.visibility import visible_account_filter
from app.domain.documents import previews
from app.domain.documents import repo as documents_repo
from app.domain.documents.blobstore import BlobStore, BlobStoreError
from app.domain.documents.manage import (
    document_quarantined,
    document_unreadable,
    may_download,
)
from app.domain.documents.models import BlobArea, Document, ReviewItem, ReviewKind
from app.domain.documents.service import get_visible_document
from app.domain.review.layout import page_of, row_region, stored_result, words_matching
from app.domain.review.schemas import (
    ALTERNATIVES_MAX,
    REGIONS_MAX,
    Alternative,
    DocumentRegions,
    PreviewInfo,
    Region,
    RegionWord,
)
from app.domain.transactions.models import Transaction
from app.ingestion.classify.dates import find_dates
from app.ingestion.parsers.text import find_amounts
from app.ingestion.sandbox.protocol import ExtractResult, Preview

#: Headers of a served preview (C4/C5): inline image, never sniffed or cached.
PREVIEW_HEADERS: Final = {
    "Content-Type": "image/png",
    "Content-Disposition": "inline",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": DOWNLOAD_CSP,
    "Cache-Control": NO_STORE,
    "Cross-Origin-Resource-Policy": "same-origin",
}


def preview_not_found() -> ProblemError:
    return ProblemError(
        404, "preview-not-found", "No preview", "This page of the document has no preview."
    )


def page_not_found() -> ProblemError:
    return ProblemError(
        404,
        "page-not-found",
        "No such page",
        "The document has no extracted page with that number.",
    )


def _accepted(db: Session, document: Document) -> None:
    """``409 document-quarantined`` unless the document's file left quarantine."""
    blob = documents_repo.get_blob(db, document.household_id, document.blob_id)
    if blob is None or blob.area is BlobArea.QUARANTINE:
        raise document_quarantined()


# ---------------------------------------------------------------------------
# Preview and regions
# ---------------------------------------------------------------------------


def read_preview(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    page: int,
    *,
    store: BlobStore,
    keyring: Keyring,
    request: HTTPConnection | None,
) -> bytes:
    """The checked PNG of ``page`` (module doc for the rules)."""
    document = get_visible_document(db, principal, document_id)
    if not may_download(db, principal):
        raise ProblemError(
            403,
            "forbidden",
            "Forbidden",
            "Viewers cannot see document pages in this household.",
        )
    _accepted(db, document)
    try:
        found = previews.read_preview(db, store, keyring, document, page)
    except (DecryptionError, BlobStoreError, OSError, ValueError):
        raise document_unreadable() from None
    if found is None:
        raise preview_not_found()
    ref, data = found
    try:
        # C5: only a plain PNG (IHDR/PLTE/IDAT/IEND) ever leaves the API.
        Preview(
            page=ref.page,
            width=ref.width,
            height=ref.height,
            dpi=ref.dpi,
            scale=ref.scale,
            png=data,
        )
    except ValueError:
        raise document_unreadable() from None
    audit(
        db,
        action="document.preview",
        target_type="document",
        target_id=document.id,
        after={"page": page, "size": len(data)},
        household_id=document.household_id,
        user_id=principal.user_id,
        request=request,
    )
    return data


def regions(
    db: Session, principal: Principal, document_id: uuid.UUID, page: int
) -> DocumentRegions:
    """Geometry of ``page``; its words only for callers who may see the content."""
    document = get_visible_document(db, principal, document_id)
    _accepted(db, document)
    result = stored_result(document.extraction)
    found = page_of(result, page) if result is not None else None
    if result is None or found is None:
        raise page_not_found()
    with_text = may_download(db, principal)
    ref = next((r for r in previews.stored_previews(document.extraction) if r.page == page), None)
    return DocumentRegions(
        document_id=document.id,
        page=found.number,
        page_count=len(result.pages),
        width=found.width,
        height=found.height,
        unit=found.unit,
        text_source=found.text_source,
        text_included=with_text,
        words_truncated=found.words_truncated,
        preview=(
            PreviewInfo(width=ref.width, height=ref.height, dpi=ref.dpi, scale=ref.scale)
            if ref is not None
            else None
        ),
        words=(
            [
                RegionWord(bbox=list(w.bbox), line=w.line, text=w.text, conf=w.conf)
                for w in found.words
            ]
            if with_text
            else []
        ),
    )


# ---------------------------------------------------------------------------
# Item enrichment (GET /review/{id})
# ---------------------------------------------------------------------------


def _uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _visible_transactions(
    db: Session, principal: Principal, household_id: uuid.UUID, ids: list[uuid.UUID]
) -> dict[uuid.UUID, Transaction]:
    if not ids:
        return {}
    rows = db.execute(
        select(Transaction).where(
            Transaction.household_id == household_id,
            Transaction.id.in_(ids),
            visible_account_filter(principal, Transaction.account_id),
        )
    ).scalars()
    return {row.id: row for row in rows}


def _txn_label(row: Transaction) -> str:
    payee = (row.raw_payee or "").strip()
    return f"{row.posted_date.isoformat()} {row.amount:f} {payee}".strip()[:200]


def _row_alternatives(row: Transaction, fields: list[str], line: str) -> list[Alternative]:
    """Other readings of a doubtful row's fields from the words on its line."""
    out: list[Alternative] = []
    wanted = set(fields) or {"amount", "posted_date"}
    if "amount" in wanted:
        seen: set[str] = set()
        for amount in find_amounts(line):
            value = f"{amount.value:f}"
            if value in seen:
                continue
            seen.add(value)
            out.append(
                Alternative(field="amount", value=value, transaction_id=row.id, source="document")
            )
    if {"posted", "posted_date", "date"} & wanted:
        for hit in find_dates(line):
            if hit.value is not None:
                out.append(
                    Alternative(
                        field="posted_date",
                        value=hit.value.isoformat(),
                        transaction_id=row.id,
                        source="document",
                    )
                )
    return out


def _low_confidence(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    result: ExtractResult | None,
    with_text: bool,
) -> tuple[list[Region], list[Alternative]]:
    payload = item.payload or {}
    entries = [e for e in payload.get("transactions") or [] if isinstance(e, dict)]
    ids = [i for i in (_uuid(e.get("transaction_id")) for e in entries) if i is not None]
    rows = _visible_transactions(db, principal, item.household_id, ids)
    regions_out: list[Region] = []
    alternatives: list[Alternative] = []
    for entry in entries:
        row = rows.get(_uuid(entry.get("transaction_id")) or uuid.UUID(int=0))
        if row is None or result is None:
            continue
        page = entry.get("page") if isinstance(entry.get("page"), int) else None
        fields = [str(f) for f in entry.get("fields") or [] if isinstance(f, str)][:20]
        found = row_region(result, page=page, posted=row.posted_date, amount=row.amount)
        if found is None:
            continue
        regions_out.append(
            Region(
                page=found.page,
                bbox=list(found.bbox),
                label="transaction",
                transaction_id=row.id,
                fields=fields,
            )
        )
        if with_text:
            alternatives.extend(_row_alternatives(row, fields, found.text))
    return regions_out, alternatives


def _account_alternatives(
    db: Session, principal: Principal, item: ReviewItem, document: Document | None
) -> list[Alternative]:
    """Accounts the caller edits, best fit first (last four digits, institution, currency)."""
    payload = item.payload or {}
    result = stored_result(document.extraction) if document is not None else None
    last4: str | None = None
    index = payload.get("statement")
    if result is not None and isinstance(index, int) and 0 <= index < len(result.statements):
        last4 = result.statements[index].account.last4
    currency = payload.get("currency") if isinstance(payload.get("currency"), str) else None
    institution_id = document.institution_id if document is not None else None
    accounts = db.execute(
        select(Account)
        .where(
            Account.household_id == item.household_id,
            Account.closed_on.is_(None),
            visible_account_filter(principal, Account.id, need="edit"),
        )
        .order_by(Account.name, Account.id)
        .limit(500)
    ).scalars()
    scored: list[tuple[float, Account]] = []
    for account in accounts:
        score = 0.0
        if last4 and account.number_last4 == last4:
            score += 0.6
        if institution_id is not None and account.institution_id == institution_id:
            score += 0.25
        if currency and account.currency == currency:
            score += 0.15
        scored.append((round(score, 4), account))
    scored.sort(key=lambda pair: -pair[0])
    return [
        Alternative(
            field="account_id",
            value=str(account.id),
            label=account.name,
            score=score,
            source="ledger",
        )
        for score, account in scored
    ]


def _transaction_alternatives(
    db: Session, principal: Principal, item: ReviewItem
) -> list[Alternative]:
    """The candidate transactions a duplicate or receipt item names (visible ones)."""
    payload = item.payload or {}
    scores: dict[uuid.UUID, float | None] = {}
    for value in payload.get("candidates") or []:
        found = _uuid(value)
        if found is not None:
            scores.setdefault(found, None)
    for entry in payload.get("suggestions") or []:
        if isinstance(entry, dict) and (found := _uuid(entry.get("transaction_id"))):
            score = entry.get("score")
            scores.setdefault(found, float(score) if isinstance(score, int | float) else None)
    rows = _visible_transactions(db, principal, item.household_id, list(scores))
    field = "duplicate_of" if item.kind is ReviewKind.POSSIBLE_DUPLICATE else "transaction_id"
    return [
        Alternative(
            field=field,
            value=str(tid),
            label=_txn_label(rows[tid]),
            score=scores[tid],
            transaction_id=tid,
            source="ledger",
        )
        for tid in scores
        if tid in rows
    ]


def _institution_alternatives(db: Session, item: ReviewItem) -> list[Alternative]:
    rows = db.execute(
        select(Institution.id, Institution.name)
        .where(Institution.household_id == item.household_id)
        .order_by(Institution.name, Institution.id)
        .limit(ALTERNATIVES_MAX)
    ).all()
    return [
        Alternative(field="institution_id", value=str(i), label=name, source="household")
        for i, name in rows
    ]


def enrich(
    db: Session, principal: Principal, item: ReviewItem
) -> tuple[list[Region], list[Alternative]]:
    """``regions`` and ``alternatives`` of one item (module doc)."""
    document = (
        documents_repo.get_document(db, item.household_id, item.document_id)
        if item.document_id is not None
        else None
    )
    result = stored_result(document.extraction) if document is not None else None
    with_text = document is not None and may_download(db, principal)
    regions_out: list[Region] = []
    alternatives: list[Alternative] = []
    payload = item.payload or {}
    if item.kind is ReviewKind.LOW_CONFIDENCE_FIELD:
        regions_out, alternatives = _low_confidence(db, principal, item, result, with_text)
    elif item.kind is ReviewKind.UNKNOWN_ACCOUNT:
        alternatives = _account_alternatives(db, principal, item, document)
        index = payload.get("statement")
        if result is not None and isinstance(index, int) and 0 <= index < len(result.statements):
            last4 = result.statements[index].account.last4
            for shown in result.pages[:3]:
                for box in words_matching(shown, [last4] if last4 else []):
                    regions_out.append(
                        Region(page=shown.number, bbox=list(box), label="account_number")
                    )
    elif item.kind in (ReviewKind.POSSIBLE_DUPLICATE, ReviewKind.RECEIPT_MATCH):
        alternatives = _transaction_alternatives(db, principal, item)
    elif item.kind is ReviewKind.NEW_INSTITUTION:
        alternatives = _institution_alternatives(db, item)
    if not regions_out and result is not None and isinstance(payload.get("page"), int):
        whole = page_of(result, payload["page"])
        if whole is not None:
            regions_out.append(
                Region(page=whole.number, bbox=[0.0, 0.0, whole.width, whole.height], label="page")
            )
    return regions_out[:REGIONS_MAX], alternatives[:ALTERNATIVES_MAX]
