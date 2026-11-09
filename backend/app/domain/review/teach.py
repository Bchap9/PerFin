# SPDX-License-Identifier: AGPL-3.0-or-later
"""Teach PerFin: label a document's facts on its page (API.md ``POST /documents/{id}/teach``).

The review UI shows a page; the person clicks a word or drags a box and
says what it is (account number, account name, statement start or end,
opening or closing balance, institution, a transaction or holdings table
with a role per column, a line to ignore, or "Other — describe it").
:func:`teach` then:

1. **Reads each selection** from the stored words
   (:func:`app.ingestion.parsers.template.selection_text`; a value the
   person typed wins) and derives its **anchor** (the digit-free label next
   to or above it, or the header band for an unlabelled period;
   :func:`~app.ingestion.parsers.template.derive_anchor`). A value without
   a label is applied to this document only (``no_label:<field>``).
2. **Applies it to this document**: the period, the institution (one of the
   household's, or one created by name like the account dialog does), the
   account (one the caller edits) and the balances (the stored statement is
   reconciled again, :func:`app.domain.review.actions.correct_balances`). A
   labelled table is read over the whole document
   (:func:`~app.ingestion.parsers.template.read_template`) and its statement
   replaces what the parsers read in the stored extraction; when no row of
   the document was imported yet it is imported (``apply-mapping`` job,
   the normal importer: reconciliation, deduplication, account matching,
   review items).
3. With ``remember``: stores a household fingerprint (``store_layout``:
   rules derived from the header, which must match the document) **with the
   learned template** (anchors, tables, ignored lines and notes; never a
   value: labels and headers holding a digit are refused), then reads this
   document and up to 50 similar ones again through the pipeline, where the
   template fills what the parsers could not. The same layout taught again
   **adds to** what was remembered (what is taught now wins, field by field
   and table by table; ``kept`` names the rest); another layout of the same
   institution needs ``replace`` (``409 layout-conflict``), so earlier
   teaching is never dropped silently (:func:`app.domain.review.actions.store_layout`).
4. Free-text notes ("Other") are kept on the template, on the review item's
   resolution and in the audit row for the household to read, without the
   words that hold a digit (:func:`~app.ingestion.parsers.template.note_text`);
   they are never interpreted.
5. With ``resolve`` the review item is closed **only when the teaching
   answers it** (:func:`_settles`): a held statement when it now reconciles
   (its pending rows confirmed, as ``/correct`` does); any item when the
   document is read again through the pipeline; an unread document or an
   unknown account when the taught rows are imported; a new institution
   when it is set or the layout remembered; a low-confidence item when a
   fact it names changed (the period for a period item). A fact that does
   not answer the item (an institution for an unknown account or a
   possible duplicate) leaves it open (warning ``item_left_open``).

Callers: member or above who may change the document
(``can_edit_document``); an ``item_id`` must be an open item of the
document the caller may resolve. Audited as ``document.teach`` (field
names, counts, notes, ids; never the values read).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.domain.accounts.models import Institution
from app.domain.accounts.service import find_or_create_institution
from app.domain.accounts.visibility import get_visible_account
from app.domain.documents.manage import can_edit_document
from app.domain.documents.models import (
    Document,
    DocumentKind,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.service import get_visible_document
from app.domain.imports import service as imports_service
from app.domain.imports.schemas import ApplyMapping
from app.domain.review import actions, service
from app.domain.review.layout import stored_result
from app.domain.review.schemas import (
    TaughtColumn,
    TaughtField,
    TaughtTable,
    TeachOut,
    TeachRequest,
)
from app.domain.review.service import WHOLE_STATEMENT_REASONS
from app.domain.transactions.models import Transaction
from app.ingestion.parsers.base import DocumentView
from app.ingestion.parsers.confidence import DEFAULT_THRESHOLD
from app.ingestion.parsers.template import (
    MAX_IGNORED,
    MAX_NOTES,
    TEMPLATE_REF,
    FieldAnchor,
    FieldRead,
    LayoutTemplate,
    TableTemplate,
    derive_anchor,
    label_text,
    note_text,
    read_template,
    read_value,
    selection_text,
    table_from_selection,
)
from app.ingestion.sandbox.protocol import DOCUMENT_STATEMENT_FORMATS, ExtractResult, ParseInfo

#: The parser a remembered statement layout names (the template does the reading).
TAUGHT_PARSER: Final = "statement_generic"


def _invalid(slug: str, title: str, detail: str) -> ProblemError:
    return ProblemError(422, slug, title, detail)


def _document(db: Session, principal: Principal, document_id: uuid.UUID) -> Document:
    document = get_visible_document(db, principal, document_id)
    if not role_at_least(principal.role, HouseholdRole.MEMBER) or not can_edit_document(
        db, principal, document
    ):
        raise forbidden("You may not change this document.")
    return document


def _item(
    db: Session, principal: Principal, document: Document, item_id: uuid.UUID | None
) -> ReviewItem | None:
    if item_id is None:
        return None
    item = service.open_item_for_change(db, principal, item_id)
    if item.document_id != document.id:
        raise _invalid(
            "teach-item-mismatch", "Wrong review item", "The review item is about another document."
        )
    return item


def _ignore_label(text: str) -> str | None:
    """The digit-free words of a selection, as a line to ignore."""
    return label_text(" ".join(w for w in text.split() if not any(c.isdigit() for c in w)))


def _decimal(value: str | None) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


class _Lesson:
    """The labels of one request, read on the document."""

    def __init__(self, view: DocumentView, pages: set[int]) -> None:
        self.view = view
        self.pages = pages
        self.fields: list[TaughtField] = []
        self.anchors: dict[str, FieldAnchor] = {}
        self.values: dict[str, FieldRead] = {}
        self.ignore: list[str] = []
        self.notes: list[str] = []
        self.tables: list[TableTemplate] = []
        self.institution: str | None = None
        self.warnings: list[str] = []

    def page(self, number: int) -> int:
        if number not in self.pages:
            raise _invalid("page-not-found", "No such page", f"The document has no page {number}.")
        return number

    def note(self, text: str | None) -> str | None:
        clean = note_text(text)
        if clean is None:
            self.warnings.append("note_without_words")
        elif len(self.notes) < MAX_NOTES and clean not in self.notes:
            self.notes.append(clean)
        return clean

    def template(self) -> LayoutTemplate:
        return LayoutTemplate(
            fields=list(self.anchors.values()),
            tables=self.tables,
            ignore=self.ignore[:MAX_IGNORED],
            notes=self.notes,
        )


def _read_labels(lesson: _Lesson, body: TeachRequest) -> None:
    view = lesson.view
    for label in body.labels:
        page = lesson.page(label.page)
        text = selection_text(view, page, label.bbox)
        if label.field == "other":
            note = lesson.note(label.note)
            lesson.fields.append(TaughtField(field="other", value=None, page=page, note=note))
            continue
        if label.field == "ignore":
            found = _ignore_label(text)
            if found is None:
                lesson.warnings.append("ignore_not_rememberable")
            elif found not in lesson.ignore:
                lesson.ignore.append(found)
            lesson.fields.append(TaughtField(field="ignore", value=found, page=page))
            continue
        if label.field == "institution":
            name = " ".join((label.value or text).split())[:200] or None
            lesson.institution = name
            lesson.fields.append(TaughtField(field="institution", value=name, page=page))
            continue
        value = read_value(label.field, label.value or text)
        if value is None:
            lesson.warnings.append(f"unreadable:{label.field}")
        else:
            box = (label.bbox[0], label.bbox[1], label.bbox[2], label.bbox[3])
            lesson.values[label.field] = FieldRead(value, page, box, "anchor")
        anchor = derive_anchor(view, page, label.bbox, label.field)
        if anchor is None:
            lesson.warnings.append(f"no_label:{label.field}")
        else:
            lesson.anchors[label.field] = anchor
        lesson.fields.append(
            TaughtField(
                field=label.field,
                value=value,
                page=page,
                anchor=anchor.label if anchor is not None else None,
                direction=anchor.direction if anchor is not None else None,
            )
        )
    for table in body.tables:
        page = lesson.page(table.page)
        for column in table.columns:
            if column.role == "other":
                lesson.note(column.note)
        columns = [(c.x0, c.x1, "ignore" if c.role == "other" else c.role) for c in table.columns]
        try:
            lesson.tables.append(table_from_selection(view, page, table.bbox, table.kind, columns))
        except ValueError as exc:
            raise _invalid("invalid-table", "Table not understood", str(exc)[:300]) from None


def _institution(
    db: Session,
    principal: Principal,
    body: TeachRequest,
    lesson: _Lesson,
    request: HTTPConnection | None,
) -> Institution | None:
    institution_id = body.institution_id
    name = body.institution_name or lesson.institution
    if institution_id is None and name:
        institution_id = find_or_create_institution(db, principal, name, request=request)
    if institution_id is None:
        return None
    found = db.execute(
        select(Institution).where(
            Institution.household_id == principal.household_id, Institution.id == institution_id
        )
    ).scalar_one_or_none()
    if found is None:
        raise ProblemError(404, "institution-not-found", "Institution not found")
    return found


def _with_statements(result: ExtractResult, taught: Any) -> ExtractResult:
    """``result`` with the template's statements in place of the document parsers'."""
    kept = [s for s in result.statements if s.source_format not in DOCUMENT_STATEMENT_FORMATS]
    kept_investments = [
        i for i in result.investments if i.source_format not in DOCUMENT_STATEMENT_FORMATS
    ]
    info = result.parse or ParseInfo(threshold=DEFAULT_THRESHOLD)
    info = info.model_copy(
        update={
            "parser": TEMPLATE_REF,
            "warnings": [*[w for w in info.warnings if w != "no_parser"], "layout_template"][-100:],
        }
    )
    return result.model_copy(
        update={
            "statements": [*kept, *taught.statements] if taught.statements else result.statements,
            "investments": [*kept_investments, *taught.investments]
            if taught.investments
            else result.investments,
            "parse": info,
        }
    )


def _has_rows(db: Session, document: Document) -> bool:
    return (
        db.execute(
            select(Transaction.id)
            .where(
                Transaction.household_id == document.household_id,
                Transaction.source_document_id == document.id,
            )
            .limit(1)
        ).scalar_one_or_none()
        is not None
    )


#: The document facts that answer a ``low_confidence_field`` item, by the
#: field (or fields) its payload names; any other field is answered only by
#: the document being read again.
_FIELD_FACTS: Final[dict[str, frozenset[str]]] = {
    "period": frozenset({"period_start", "period_end"}),
    "period_start": frozenset({"period_start"}),
    "period_end": frozenset({"period_end"}),
    "balance": frozenset({"opening_balance", "closing_balance"}),
    "opening_balance": frozenset({"opening_balance"}),
    "closing_balance": frozenset({"closing_balance"}),
    "institution": frozenset({"institution_id"}),
    "account": frozenset({"account_id"}),
    "kind": frozenset({"kind"}),
}
#: Low-confidence reasons about how the parsers read the page: a taught
#: table whose rows are imported replaces that reading.
_READING_REASONS: Final = frozenset({"lines_skipped"})


def _item_fields(payload: dict[str, Any]) -> set[str]:
    named: list[Any] = [payload.get("field")]
    fields = payload.get("fields")
    if isinstance(fields, list):
        named.extend(fields)
    return {f for f in named if isinstance(f, str)}


def _settles(
    item: ReviewItem,
    *,
    reconciled: bool,
    rerun: str | None,
    applied: list[str],
    remembered: bool,
) -> bool:
    """Whether what was taught answers ``item`` (else it stays open).

    - A held statement (``reconciliation_*``): only balances with which it
      now adds up (its pending rows are then confirmed, as by ``/correct``).
    - The document read again through the pipeline (``rerun == "pipeline"``)
      answers any item: reprocessing dismisses the document's open items and
      raises afresh what is still wrong.
    - Rows imported from a taught table (``rerun == "import"``) answer what
      the import re-run answers (``unparsed``, ``unknown_account``:
      :data:`app.domain.imports.service.RERUN_ANSWERS`) and lines the
      parsers skipped.
    - A ``new_institution`` item: the institution set or the layout remembered.
    - A ``low_confidence_field`` item: a fact applied that is a field the
      item names (:data:`_FIELD_FACTS`: the period for a period item, the
      balances for a balance item, ...).
    - Nothing else: a fact that does not answer the item's question (an
      account set without its rows imported, an institution for a possible
      duplicate), a note or an ignored line leaves it open.
    """
    payload = item.payload or {}
    reason = payload.get("reason")
    if reason in WHOLE_STATEMENT_REASONS:
        return reconciled
    if rerun == "pipeline":
        return True
    if rerun == "import" and (
        item.kind in imports_service.RERUN_ANSWERS or reason in _READING_REASONS
    ):
        return True
    if item.kind is ReviewKind.NEW_INSTITUTION:
        return remembered or "institution_id" in applied
    if item.kind is ReviewKind.LOW_CONFIDENCE_FIELD:
        facts: set[str] = set()
        for name in _item_fields(payload):
            facts |= _FIELD_FACTS.get(name, frozenset())
        return bool(facts.intersection(applied))
    return False


def teach(
    db: Session,
    principal: Principal,
    document_id: uuid.UUID,
    body: TeachRequest,
    *,
    request: HTTPConnection | None,
) -> TeachOut:
    document = _document(db, principal, document_id)
    item = _item(db, principal, document, body.item_id)
    result = stored_result(document.extraction)
    if result is None or not result.pages:
        raise actions.layout_unavailable()
    view = DocumentView.from_result(result)
    lesson = _Lesson(view, {p.number for p in result.pages})
    _read_labels(lesson, body)
    template = lesson.template()
    taught = read_template(view, template, threshold=DEFAULT_THRESHOLD, given=lesson.values)

    applied: list[str] = []
    before: dict[str, Any] = {}
    institution = _institution(db, principal, body, lesson, request)
    if institution is not None and document.institution_id != institution.id:
        before["institution_id"] = document.institution_id
        document.institution_id = institution.id
        applied.append("institution_id")
    if body.account_id is not None:
        get_visible_account(db, principal, body.account_id, need="edit")
        if document.account_id != body.account_id:
            before["account_id"] = document.account_id
            document.account_id = body.account_id
            applied.append("account_id")
    start = (
        date.fromisoformat(lesson.values["period_start"].value)
        if ("period_start" in lesson.values)
        else None
    )
    end = (
        date.fromisoformat(lesson.values["period_end"].value)
        if ("period_end" in lesson.values)
        else None
    )
    if start is not None and end is not None and start > end:
        raise _invalid(
            "invalid-correction", "Invalid correction", "The period ends before it starts."
        )
    for name, value in (("period_start", start), ("period_end", end)):
        if value is not None and getattr(document, name) != value:
            before[name] = getattr(document, name)
            setattr(document, name, value)
            applied.append(name)

    rerun: str | None = None
    job_id: uuid.UUID | None = None
    reconciled = False
    resolution: dict[str, Any] = {"action": "teach"}
    if taught.read:
        document.extraction = _with_statements(result, taught).model_dump(mode="json")
        if document.kind is DocumentKind.UNKNOWN:
            document.kind = DocumentKind.STATEMENT
            applied.append("kind")
        applied.append("statements")
    elif template.tables:
        lesson.warnings.append("table_without_rows")
    opening = (
        _decimal(lesson.values["opening_balance"].value)
        if ("opening_balance" in lesson.values)
        else None
    )
    closing = (
        _decimal(lesson.values["closing_balance"].value)
        if ("closing_balance" in lesson.values)
        else None
    )
    if not taught.read and (opening is not None or closing is not None):
        stored = stored_result(document.extraction)
        if stored is None or not stored.statements:
            lesson.warnings.append("no_statement_for_balances")
        else:
            balanced = actions.correct_balances(
                db, document, item, opening=opening, closing=closing
            )
            applied.extend(
                name
                for name, value in (("opening_balance", opening), ("closing_balance", closing))
                if value is not None
            )
            if balanced.status != "ok":
                lesson.warnings.append("does_not_reconcile")
            elif (
                item is not None
                and body.resolve
                and ((item.payload or {}).get("reason") in WHOLE_STATEMENT_REASONS)
            ):
                resolution.update(
                    actions.accept_statement(db, principal, item, document, balanced, request)
                )
                reconciled = True

    stored_layout: actions.StoredLayout | None = None
    reprocessed: list[uuid.UUID] = []
    if body.remember:
        if institution is None:
            raise _invalid(
                "institution-required",
                "Institution required",
                "Say which institution the document is from to remember its layout.",
            )
        stored_layout = actions.store_layout(
            db,
            principal,
            document,
            institution=institution,
            kind=document.kind
            if document.kind is not DocumentKind.UNKNOWN
            else (DocumentKind.STATEMENT),
            parser=TAUGHT_PARSER,
            match=None,
            template=None
            if template.empty and not template.notes and not template.ignore
            else template.model_dump(mode="json"),
            reprocess_similar=body.reprocess_similar,
            request=request,
            replace=body.replace,
        )
        resolution["fingerprint_id"] = str(stored_layout.row.id)
    db.flush()
    if stored_layout is not None:
        reprocessed = actions.reprocess(
            db, principal, [document, *stored_layout.similar], stored_layout.row, "teach", request
        )
        rerun = "pipeline" if document.id in reprocessed else None
    elif taught.read:
        if _has_rows(db, document):
            lesson.warnings.append("rows_already_imported")
        else:
            accepted = imports_service.request_apply(
                db,
                principal,
                document.id,
                ApplyMapping(account_id=body.account_id),
                request=request,
            )
            rerun, job_id = "import", accepted.job_id

    tables_out = [
        TaughtTable(
            kind=table.kind,
            rows=sum(len(s.transactions) for s in taught.statements)
            if table.kind == "transactions"
            else sum(len(i.positions) for i in taught.investments),
            columns=[TaughtColumn(header=c.header, role=c.role) for c in table.columns],
        )
        for table in template.tables
    ]
    out_item = None
    if item is not None:
        settled = (
            body.resolve
            and "does_not_reconcile" not in lesson.warnings
            and _settles(
                item,
                reconciled=reconciled,
                rerun=rerun,
                applied=applied,
                remembered=stored_layout is not None,
            )
        )
        if settled:
            resolution.update(
                {
                    "fields": sorted({f.field for f in lesson.fields}),
                    "tables": [t.kind for t in template.tables],
                    "notes": lesson.notes,
                }
            )
            service.close_item(db, principal, item, resolution, request=request)
        elif body.resolve and item.state is ReviewItemState.OPEN:
            lesson.warnings.append("item_left_open")
        out_item = service.item_out(db, principal, item)
    audit(
        db,
        action="document.teach",
        target_type="document",
        target_id=document.id,
        before={k: str(v) if v is not None else None for k, v in before.items()} or None,
        after={
            "fields": sorted({f.field for f in lesson.fields}),
            "anchors": len(template.fields),
            "tables": [t.kind for t in template.tables],
            "ignored": len(template.ignore),
            "notes": lesson.notes,
            "applied": applied,
            "remembered": str(stored_layout.row.id) if stored_layout is not None else None,
            "item_id": str(item.id) if item is not None else None,
            "warnings": lesson.warnings[:20],
        },
        household_id=document.household_id,
        user_id=principal.user_id,
        request=request,
    )
    if applied:
        emit(
            db,
            document.household_id,
            "document.updated",
            {"id": str(document.id), "status": str(document.status)},
        )
    return TeachOut(
        document_id=document.id,
        fields=lesson.fields,
        tables=tables_out,
        ignored=len(template.ignore),
        notes=lesson.notes,
        applied=applied,
        rerun=rerun,  # type: ignore[arg-type]
        job_id=job_id,
        fingerprint_id=stored_layout.row.id if stored_layout is not None else None,
        reprocessed=reprocessed,
        merged_from=stored_layout.merged_from if stored_layout is not None else None,
        kept=list(stored_layout.kept) if stored_layout is not None else [],
        warnings=list(dict.fromkeys([*lesson.warnings, *taught.warnings]))[:50],
        item=out_item,
    )
