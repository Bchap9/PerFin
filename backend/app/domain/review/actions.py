# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review tools beyond a plain resolution (API.md "Review notes"; INGESTION.md "Review queue").

All three need an item the caller can see; changing anything needs what a
resolution needs (member or above who may change the item's document,
:func:`app.domain.review.service.open_item_for_change`).

* :func:`correct` (``POST /review/{id}/correct``): corrects the document's
  institution (one of the household's), account (one the caller edits),
  period, tax year or kind, and the values of rows imported from it
  (:func:`app.domain.review.service.apply_fixes`); with ``resolve`` the item
  is closed. Audited as ``document.correct`` (field names and ids only).
  Opening/closing balances correct a statement of the stored extraction
  (:func:`correct_balances`, reconciled again); a ``reconciliation_*`` item
  resolved that way needs the statement to add up now
  (:func:`accept_statement`: pending rows confirmed, closing balance kept
  as a statement snapshot; ``409 statement-does-not-reconcile`` otherwise).
* :func:`remember_layout` (``POST /review/{id}/remember-layout``): a
  household ``parser_fingerprint`` for the document's layout
  (:func:`store_layout`, shared with "Teach PerFin", which also stores a
  learned template). The rules come
  from the request or are derived from the header text and the table layout
  (:func:`app.domain.review.layout.derive_match`); either way they pass the
  fingerprint schema and ReDoS-safe regex check
  (:func:`app.ingestion.classify.fingerprints.remember_layout`) **and must
  hold on the document they are learned from** (``422
  layout-does-not-match``) within the household matching budget (``422
  layout-too-costly``). A household remembers at most
  :data:`app.ingestion.classify.fingerprints.MAX_HOUSEHOLD_FINGERPRINTS`
  layouts (``409 fingerprint-limit``). The parser must exist. A learned
  template of the layout's earlier version is kept, never dropped silently
  (:func:`store_layout`: ``409 layout-conflict`` for another layout unless
  ``replace``). The item is resolved,
  then the document and up to :data:`SIMILAR_MAX` other ``needs_review``
  documents the caller may change whose stored layout the new fingerprint
  matches go through the pipeline again (a new extraction in the sandbox,
  then classification with the new fingerprint). Matching the scanned
  documents shares a :data:`SIMILAR_SECONDS` budget (each document at most
  the household matching budget); the scan stops when it is spent, so the
  request's fingerprint work is bounded (security review P34-SR-R2-01).
* :func:`rule_draft` (``GET /review/{id}/rule-draft``): a rule pre-filled
  from one of the item's rows (payee text and account; the row's category,
  else its payee, else a review flag as the action), validated by the rules
  schema but not saved: the client edits it and sends it to ``POST
  /rules`` (owner or admin), so the rules service decides as for any rule.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.principal import Principal
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.core.money import to_storage
from app.domain.accounts import repo as accounts_repo
from app.domain.accounts.models import Account, BalanceSource, Institution
from app.domain.accounts.visibility import get_visible_account, visible_account_filter
from app.domain.documents.manage import can_edit_document, queue_ingestion
from app.domain.documents.models import (
    TERMINAL_STATUSES,
    Document,
    DocumentKind,
    DocumentStatus,
    ParserFingerprint,
    ReviewItem,
)
from app.domain.documents.service import visible_document_filter
from app.domain.review import service
from app.domain.review.layout import derive_match, stored_result
from app.domain.review.schemas import (
    Correction,
    RememberLayout,
    RememberLayoutOut,
    ReviewItemOut,
    RuleDraftOut,
)
from app.domain.review.service import WHOLE_STATEMENT_REASONS
from app.domain.rules.schemas import RuleCreate
from app.domain.transactions.models import NAME_MAX, Transaction
from app.ingestion.classify import fingerprints
from app.ingestion.classify.fingerprints import MatchSpec
from app.ingestion.classify.service import all_fingerprints
from app.ingestion.classify.text import DocumentView
from app.ingestion.parsers.registry import parser_named
from app.ingestion.parsers.template import merge_templates, parse_template, rebalance
from app.ingestion.sandbox.protocol import Balance, ExtractResult

#: Other documents one "remember this layout" sends through the pipeline again.
SIMILAR_MAX: Final = 50
#: ``needs_review`` documents looked at for a similar layout (newest first).
SIMILAR_SCAN: Final = 500
#: Wall-clock budget for matching the new fingerprint against those documents;
#: the scan stops when it is spent (security review P34-SR-R2-01).
SIMILAR_SECONDS: Final = 2.0
#: Parser of each kind when neither the request nor the document names one.
DEFAULT_PARSERS: Final[dict[DocumentKind, str]] = {
    DocumentKind.STATEMENT: "statement_generic",
    DocumentKind.PAYSLIP: "payslip_basic",
    DocumentKind.RECEIPT: "receipt_basic",
}


def _invalid(slug: str, title: str, detail: str) -> ProblemError:
    return ProblemError(422, slug, title, detail)


def _institution(db: Session, household_id: uuid.UUID, institution_id: uuid.UUID) -> Institution:
    found = db.execute(
        select(Institution).where(
            Institution.household_id == household_id, Institution.id == institution_id
        )
    ).scalar_one_or_none()
    if found is None:
        raise ProblemError(404, "institution-not-found", "Institution not found")
    return found


# ---------------------------------------------------------------------------
# Corrections
# ---------------------------------------------------------------------------


def correct(
    db: Session,
    principal: Principal,
    item_id: uuid.UUID,
    body: Correction,
    *,
    request: HTTPConnection | None,
) -> ReviewItemOut:
    item = service.open_item_for_change(db, principal, item_id)
    document = service.item_document(db, item)
    changed: dict[str, Any] = {}
    before: dict[str, Any] = {}
    if body.document is not None:
        fix = body.document
        given = fix.model_fields_set
        if "institution_id" in given and fix.institution_id is not None:
            _institution(db, item.household_id, fix.institution_id)
        if "account_id" in given and fix.account_id is not None:
            get_visible_account(db, principal, fix.account_id, need="edit")
        start = fix.period_start if "period_start" in given else document.period_start
        end = fix.period_end if "period_end" in given else document.period_end
        if start is not None and end is not None and start > end:
            raise _invalid(
                "invalid-correction", "Invalid correction", "The period ends before it starts."
            )
        for name in ("institution_id", "account_id", "period_start", "period_end", "tax_year"):
            if name in given and getattr(document, name) != getattr(fix, name):
                before[name] = getattr(document, name)
                changed[name] = getattr(fix, name)
                setattr(document, name, getattr(fix, name))
        if "kind" in given and fix.kind is not None and document.kind != fix.kind:
            before["kind"], changed["kind"] = str(document.kind), str(fix.kind)
            document.kind = fix.kind
    balanced: BalanceOutcome | None = None
    if body.document is not None and (
        body.document.opening_balance is not None or body.document.closing_balance is not None
    ):
        balanced = correct_balances(
            db,
            document,
            item,
            opening=body.document.opening_balance,
            closing=body.document.closing_balance,
            statement=body.document.statement,
        )
        for name, value in (
            ("opening_balance", body.document.opening_balance),
            ("closing_balance", body.document.closing_balance),
        ):
            if value is not None:
                before[name] = balanced.before.get(name)
                changed[name] = value
    corrected = service.apply_fixes(db, principal, document, body.transactions, request=request)
    db.flush()
    if changed:
        audit(
            db,
            action="document.correct",
            target_type="document",
            target_id=document.id,
            before={k: str(v) if v is not None else None for k, v in before.items()},
            after={k: str(v) if v is not None else None for k, v in changed.items()},
            household_id=item.household_id,
            user_id=principal.user_id,
            request=request,
        )
        emit(
            db,
            item.household_id,
            "document.updated",
            {"id": str(document.id), "status": str(document.status)},
        )
    if body.resolve:
        resolution: dict[str, Any] = {
            "action": "correct",
            "document_fields": sorted(changed),
            "corrected": [str(i) for i in corrected],
        }
        if balanced is not None and (item.payload or {}).get("reason") in WHOLE_STATEMENT_REASONS:
            resolution.update(accept_statement(db, principal, item, document, balanced, request))
        service.close_item(db, principal, item, resolution, request=request)
    return service.item_out(db, principal, item)


# ---------------------------------------------------------------------------
# Statement balances
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BalanceOutcome:
    statement: int
    status: str
    difference: Decimal | None
    closing: Decimal | None
    as_of: date | None
    before: dict[str, Any]


def _statement_index(
    result: ExtractResult, item: ReviewItem | None, db: Session, given: int | None
) -> int:
    statements = result.statements
    if given is not None:
        if given >= len(statements):
            raise _invalid(
                "statement-not-found", "No such statement", "The document has no such statement."
            )
        return given
    payload = item.payload if item is not None else {}
    index = (payload or {}).get("statement")
    if isinstance(index, int) and 0 <= index < len(statements):
        return index
    if len(statements) == 1:
        return 0
    account_id = (payload or {}).get("account_id")
    if isinstance(account_id, str) and item is not None:
        account = db.execute(
            select(Account).where(
                Account.household_id == item.household_id, Account.id == uuid.UUID(account_id)
            )
        ).scalar_one_or_none()
        last4 = account.number_last4 if account is not None else None
        matches = [i for i, s in enumerate(statements) if last4 and s.account.last4 == last4]
        if len(matches) == 1:
            return matches[0]
    raise _invalid(
        "statement-required",
        "Which statement?",
        "The document holds several statements; name the one to correct.",
    )


def _text(value: Decimal | None) -> str | None:
    """An amount as the importer writes it (cents when it has no finer part)."""
    if value is None:
        return None
    cents = value.quantize(Decimal("0.01"))
    return f"{cents:f}" if cents == value else f"{value:f}"


def correct_balances(
    db: Session,
    document: Document,
    item: ReviewItem | None,
    *,
    opening: Decimal | None,
    closing: Decimal | None,
    statement: int | None = None,
) -> BalanceOutcome:
    """Set a stored statement's opening/closing balance and reconcile it again.

    The corrected statement is kept in ``document.extraction`` (a re-import
    reads it); a ``reconciliation_*`` item's figures are brought up to date
    so the queue shows the new difference.
    """
    result = stored_result(document.extraction)
    if result is None or not result.statements:
        raise ProblemError(
            409,
            "statement-unavailable",
            "No statement to correct",
            "The document has no statement whose balances could be corrected.",
        )
    index = _statement_index(result, item, db, statement)
    current = result.statements[index]
    before = {
        "opening_balance": _text(current.opening_balance.amount)
        if current.opening_balance
        else None,
        "closing_balance": _text(current.ledger_balance.amount) if current.ledger_balance else None,
    }
    update: dict[str, Any] = {}
    if opening is not None:
        update["opening_balance"] = Balance(amount=opening, as_of=current.period_start)
    if closing is not None:
        update["ledger_balance"] = Balance(amount=closing, as_of=current.period_end)
    fixed = rebalance(current.model_copy(update=update))
    statements = list(result.statements)
    statements[index] = fixed
    document.extraction = result.model_copy(update={"statements": statements}).model_dump(
        mode="json"
    )
    check = fixed.reconciliation
    if check is None:  # pragma: no cover - rebalance always reconciles
        raise RuntimeError("statement not reconciled")
    if item is not None and (item.payload or {}).get("reason") in WHOLE_STATEMENT_REASONS:
        item.payload = {
            **(item.payload or {}),
            "opening": _text(check.opening),
            "closing": _text(check.closing),
            "total": _text(check.total),
            "difference": _text(check.difference),
            "status": check.status,
        }
    db.flush()
    return BalanceOutcome(
        statement=index,
        status=check.status,
        difference=check.difference,
        closing=check.closing,
        as_of=fixed.ledger_balance.as_of if fixed.ledger_balance else fixed.period_end,
        before=before,
    )


def accept_statement(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    document: Document,
    balanced: BalanceOutcome,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    """A held statement that now reconciles: its pending rows confirmed, its balance recorded.

    ``409 statement-does-not-reconcile`` while it still does not add up.
    """
    if balanced.status != "ok":
        raise ProblemError(
            409,
            "statement-does-not-reconcile",
            "Still does not add up",
            "With these balances the rows differ from the closing balance by "
            f"{balanced.difference}; correct the rows or the balances.",
        )
    confirmed = service.confirm_pending(db, principal, item, document, request=request)
    account_id = (item.payload or {}).get("account_id")
    snapshot = False
    if isinstance(account_id, str) and balanced.closing is not None and balanced.as_of:
        account = get_visible_account(db, principal, uuid.UUID(account_id), need="edit")
        accounts_repo.upsert_snapshot(
            db,
            account,
            balanced.as_of,
            to_storage(balanced.closing),
            BalanceSource.STATEMENT,
            source_document_id=document.id,
        )
        emit(db, item.household_id, "balance.updated", {"account_id": str(account.id)})
        snapshot = True
    return {"reconciled": True, "confirmed": confirmed, "snapshot": snapshot}


# ---------------------------------------------------------------------------
# Remember this layout
# ---------------------------------------------------------------------------


def _parser_for(body: RememberLayout, kind: DocumentKind, document: Document) -> str:
    result = stored_result(document.extraction)
    name = body.parser
    if name is None and result is not None and result.parse is not None and result.parse.parser:
        used = result.parse.parser
        # An institution parser is tied to a shipped layout; a remembered one
        # gets the generic parser of its kind unless the request names one.
        name = used.name if used.institution is None else None
    name = name or DEFAULT_PARSERS.get(kind)
    if name is None:
        raise _invalid(
            "parser-required", "Parser required", "Name the parser that reads this layout."
        )
    if parser_named(name) is None:
        raise _invalid("parser-unknown", "Unknown parser", f"There is no parser named {name}.")
    return name


def _similar(
    db: Session,
    principal: Principal,
    source: Document,
    fingerprint: fingerprints.Fingerprint,
) -> list[Document]:
    rows = db.execute(
        select(Document)
        .where(
            Document.household_id == source.household_id,
            visible_document_filter(principal),
            Document.status == DocumentStatus.NEEDS_REVIEW,
            Document.id != source.id,
            Document.extraction.is_not(None),
        )
        .order_by(Document.created_at.desc(), Document.id)
        .limit(SIMILAR_SCAN)
    ).scalars()
    out: list[Document] = []
    budget = fingerprints.MatchBudget(SIMILAR_SECONDS)
    for document in rows:
        if budget.spent():
            break
        result = stored_result(document.extraction)
        if result is None or not result.pages or not can_edit_document(db, principal, document):
            continue
        view = DocumentView.from_extraction(result)
        each = budget.child(fingerprints.HOUSEHOLD_MATCH_SECONDS)
        if fingerprints.match_fingerprint(fingerprint, view, each):
            out.append(document)
            if len(out) >= SIMILAR_MAX:
                break
    return out


@dataclass(frozen=True)
class StoredLayout:
    row: ParserFingerprint
    ambiguous: bool
    similar: list[Document]
    #: The version whose learned template was carried into this one.
    merged_from: int | None = None
    #: What was kept from that template (field names, ``table:<kind>``).
    kept: tuple[str, ...] = ()


def layout_unavailable() -> ProblemError:
    return ProblemError(
        409,
        "layout-unavailable",
        "No layout to remember",
        "The document has no extracted pages; reprocess it first.",
    )


def store_layout(
    db: Session,
    principal: Principal,
    document: Document,
    *,
    institution: Institution,
    kind: DocumentKind,
    parser: str,
    match: MatchSpec | None,
    template: dict[str, Any] | None,
    reprocess_similar: bool,
    request: HTTPConnection | None,
    replace: bool = False,
) -> StoredLayout:
    """A household fingerprint for ``document``'s layout (module doc), audited.

    ``match`` defaults to the rules derived from the document; they must
    hold on it. ``template`` is a learned layout ("Teach PerFin").

    A new version of a layout replaces the earlier one (one version per
    institution, kind and parser is used). When the earlier version carries
    a learned template it is not dropped silently: if its rules still
    recognise this document (the same layout) the template is carried into
    the new version, what is taught now winning
    (:func:`app.ingestion.parsers.template.merge_templates`); if they do not
    (another layout of the same institution), ``409 layout-conflict``.
    ``replace`` starts the template afresh in both cases.
    """
    household_id = document.household_id
    result = stored_result(document.extraction)
    if result is None or not (result.pages or result.text):
        raise layout_unavailable()
    match = match if match is not None else derive_match(result)
    if match is None:
        raise _invalid(
            "layout-not-distinctive",
            "Nothing to recognise",
            "The document's header has no text that could recognise this layout; "
            "send the rules in match.",
        )
    spec = fingerprints.FingerprintSpec.model_validate(
        {
            "schema": fingerprints.SCHEMA_VERSION,
            "key": "household-new",
            "institution": institution.name,
            "kind": kind.value,
            "parser": parser,
            "version": 1,
            "match": match,
        }
    )
    compiled = fingerprints.compile_fingerprint(
        spec, origin="household", institution_id=institution.id
    )
    view = DocumentView.from_extraction(result)
    budget = fingerprints.MatchBudget(fingerprints.HOUSEHOLD_MATCH_SECONDS)
    if fingerprints.match_fingerprint(compiled, view, budget) is None:
        if budget.exhausted:
            raise _invalid(
                "layout-too-costly",
                "Rules too costly",
                "The rules take too long to evaluate; use fewer or simpler patterns.",
            )
        raise _invalid(
            "layout-does-not-match",
            "Rules do not match",
            "The rules must recognise the document they are learned from.",
        )
    fingerprints.lock_layouts(db, household_id)
    merged_from: int | None = None
    kept: list[str] = []
    if not replace:
        earlier_row = fingerprints.latest_layout(
            db, household_id, institution_id=institution.id, kind=kind, parser_name=parser
        )
        earlier = parse_template(earlier_row.template) if earlier_row is not None else None
        if earlier_row is not None and earlier is not None:
            if not _same_layout(earlier_row, institution, view):
                raise ProblemError(
                    409,
                    "layout-conflict",
                    "Another layout is remembered",
                    f"PerFin already remembers a taught layout of {institution.name} that "
                    "does not recognise this document. Replace it to remember this one "
                    "instead (the earlier teaching is then no longer used).",
                )
            merged, kept = merge_templates(
                earlier, parse_template(template) if template is not None else None
            )
            template = merged.model_dump(mode="json")
            merged_from = earlier_row.version
    try:
        row = fingerprints.remember_layout(
            db,
            household_id,
            institution_id=institution.id,
            kind=kind,
            match_rules=match.model_dump(mode="json", exclude_defaults=True),
            parser_name=parser,
            document_id=document.id,
            template=template,
        )
    except fingerprints.FingerprintLimitError as exc:
        raise ProblemError(
            409, "fingerprint-limit", "Too many remembered layouts", str(exc)[:500]
        ) from None
    except fingerprints.FingerprintError as exc:
        raise _invalid("invalid-fingerprint", "Invalid layout rules", str(exc)[:500]) from None
    top, _ = fingerprints.best_match(all_fingerprints(db, household_id), view)
    ambiguous = top is None or top.fingerprint.fingerprint_id != row.id
    similar = _similar(db, principal, document, compiled) if reprocess_similar else []
    after: dict[str, Any] = {
        "institution_id": institution.id,
        "kind": str(kind),
        "parser": parser,
        "version": row.version,
        "rules": len(match.all) + len(match.any),
        "learned_from_document_id": document.id,
    }
    if template is not None:
        after["template"] = {
            "fields": len(template.get("fields") or []),
            "tables": len(template.get("tables") or []),
        }
    if merged_from is not None:
        after["merged_from_version"] = merged_from
        after["kept"] = kept
    if replace:
        after["replaced"] = True
    audit(
        db,
        action="parser_fingerprint.create",
        target_type="parser_fingerprint",
        target_id=row.id,
        after=after,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    return StoredLayout(row, ambiguous, similar, merged_from, tuple(kept))


def _same_layout(row: ParserFingerprint, institution: Institution, view: DocumentView) -> bool:
    """Whether the stored version ``row`` still recognises the document in ``view``."""
    earlier = fingerprints.compile_row(row, institution.name, institution.country)
    if earlier is None:
        return False
    budget = fingerprints.MatchBudget(fingerprints.HOUSEHOLD_MATCH_SECONDS)
    return fingerprints.match_fingerprint(earlier, view, budget) is not None


def remember_layout(
    db: Session,
    principal: Principal,
    item_id: uuid.UUID,
    body: RememberLayout,
    *,
    request: HTTPConnection | None,
) -> RememberLayoutOut:
    item = service.open_item_for_change(db, principal, item_id)
    document = service.item_document(db, item)
    result = stored_result(document.extraction)
    if result is None or not (result.pages or result.text):
        raise layout_unavailable()
    institution = _institution(db, item.household_id, body.institution_id)
    kind = body.kind or document.kind
    if kind is DocumentKind.UNKNOWN:
        raise _invalid("kind-required", "Kind required", "Say what kind of document this is.")
    parser = _parser_for(body, kind, document)
    # A given ``match`` was validated by the request model (schema, ReDoS-safe regexes).
    stored = store_layout(
        db,
        principal,
        document,
        institution=institution,
        kind=kind,
        parser=parser,
        match=body.match,
        template=None,
        reprocess_similar=body.reprocess_similar,
        request=request,
        replace=body.replace,
    )
    row, similar = stored.row, stored.similar
    service.close_item(
        db,
        principal,
        item,
        {
            "action": "remember_layout",
            "fingerprint_id": str(row.id),
            "version": row.version,
            "reprocessed": len(similar) + 1,
        },
        request=request,
    )
    reprocessed = reprocess(db, principal, [document, *similar], row, "remember_layout", request)
    return RememberLayoutOut(
        fingerprint_id=row.id,
        version=row.version,
        institution_id=institution.id,
        kind=kind,
        parser=parser,
        match=dict(row.match_rules),
        ambiguous=stored.ambiguous,
        reprocessed=reprocessed,
        item=service.item_out(db, principal, item),
        merged_from=stored.merged_from,
        kept=list(stored.kept),
    )


def reprocess(
    db: Session,
    principal: Principal,
    documents: list[Document],
    row: ParserFingerprint,
    by: str,
    request: HTTPConnection | None,
) -> list[uuid.UUID]:
    """Send ``documents`` through the pipeline again with the new fingerprint."""
    out: list[uuid.UUID] = []
    for target in documents:
        if target.status not in TERMINAL_STATUSES:
            continue
        queue_ingestion(
            db,
            target,
            principal=principal,
            action="document.reprocess",
            request=request,
            facts={"by": by, "fingerprint_id": str(row.id)},
        )
        out.append(target.id)
    return out


# ---------------------------------------------------------------------------
# Create a rule from this
# ---------------------------------------------------------------------------


def _item_transaction_ids(item: ReviewItem) -> list[uuid.UUID]:
    payload = item.payload or {}
    raw: list[Any] = [payload.get("transaction_id")]
    raw.extend(
        e.get("transaction_id") for e in payload.get("transactions") or [] if isinstance(e, dict)
    )
    raw.extend(
        e.get("transaction_id") for e in payload.get("suggestions") or [] if isinstance(e, dict)
    )
    raw.extend(payload.get("candidates") or [])
    out: list[uuid.UUID] = []
    for value in raw:
        try:
            found = uuid.UUID(str(value))
        except (TypeError, ValueError):
            continue
        if found not in out:
            out.append(found)
    return out


def _rule_from(row: Transaction) -> RuleCreate:
    payee = " ".join((row.raw_payee or "").split())[:200]
    conditions: list[dict[str, Any]] = []
    if payee:
        conditions.append({"type": "payee", "op": "equals", "value": payee})
    elif row.payee_id is not None:
        conditions.append({"type": "payee_is", "payee_ids": [str(row.payee_id)]})
    else:
        raise ProblemError(
            409,
            "rule-draft-unavailable",
            "Nothing to match on",
            "The row has no payee to build a rule from.",
        )
    conditions.append({"type": "account", "account_ids": [str(row.account_id)]})
    conditions.append({"type": "sign", "sign": "outflow" if row.amount < 0 else "inflow"})
    if row.category_id is not None:
        action: dict[str, Any] = {"type": "set_category", "category_id": str(row.category_id)}
    elif row.payee_id is not None and payee:
        action = {"type": "set_payee", "payee_id": str(row.payee_id)}
    else:
        action = {"type": "flag_for_review"}
    name = f"From review: {payee or 'payee'}"[:NAME_MAX]
    return RuleCreate.model_validate(
        {
            "name": name,
            "enabled": True,
            "stop_processing": False,
            "conditions": {"type": "all", "conditions": conditions},
            "actions": [action],
        }
    )


def rule_draft(db: Session, principal: Principal, item_id: uuid.UUID) -> RuleDraftOut:
    item = service.visible_item(db, principal, item_id)
    ids = _item_transaction_ids(item)
    rows = (
        {
            r.id: r
            for r in db.execute(
                select(Transaction).where(
                    Transaction.household_id == item.household_id,
                    Transaction.id.in_(ids),
                    visible_account_filter(principal, Transaction.account_id),
                )
            ).scalars()
        }
        if ids
        else {}
    )
    row = next((rows[i] for i in ids if i in rows), None)
    if row is None:
        raise ProblemError(
            409,
            "rule-draft-unavailable",
            "Nothing to build a rule from",
            "This item names no transaction you can see.",
        )
    return RuleDraftOut(item_id=item.id, transaction_id=row.id, rule=_rule_from(row))
