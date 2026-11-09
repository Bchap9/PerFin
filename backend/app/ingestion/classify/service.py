# SPDX-License-Identifier: AGPL-3.0-or-later
"""Classify a document: kind, institution, account, period, tax year, per-field confidence.

Order (INGESTION.md "Classify"): structured statements answer for
themselves; otherwise the household's and the shipped **fingerprints**
(:mod:`app.ingestion.classify.fingerprints`), then the **heuristic
classifier** (:mod:`app.ingestion.classify.model`); below
:data:`KIND_THRESHOLD` the kind stays ``unknown`` and the document goes to
review. No language model is consulted here; the optional local model of
ADR-0010 is asked afterwards by the pipeline (``pipeline._ml_hints``).

:func:`classify_extraction` is pure (no database); :func:`apply_classification`
writes the result on the ``document`` row, resolving the institution by
name among the household's institutions and the account by last four
digits and institution (:func:`app.ingestion.match.accounts.match_account`)
restricted to the accounts the uploader may import into (SECURITY.md F3).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.accounts.models import Account, Institution
from app.domain.documents.models import Document, DocumentKind
from app.domain.transactions.import_hash import normalize_payee
from app.ingestion.classify.accounts import Last4, detect_last4
from app.ingestion.classify.dates import DateOrder
from app.ingestion.classify.fingerprints import (
    Fingerprint,
    FingerprintMatch,
    best_match,
    builtin_fingerprints,
    household_fingerprints,
)
from app.ingestion.classify.model import KindModel, predict, shipped_model
from app.ingestion.classify.period import (
    Period,
    PrintedDate,
    TaxYear,
    detect_period,
    detect_printed_date,
    detect_tax_year,
)
from app.ingestion.classify.text import DocumentView
from app.ingestion.match.accounts import importable_account_ids, match_account
from app.ingestion.match.institutions import pick
from app.ingestion.sandbox.protocol import ExtractResult

#: Below this the classifier's kind is not believed (document stays ``unknown``).
KIND_THRESHOLD: Final = 0.6
#: Below this a detected field is not written to the document.
FIELD_THRESHOLD: Final = 0.5
CLASSIFIER_VERSION: Final = "classify-1"

_PERIOD_KINDS: Final = frozenset({"statement", "payslip", "insurance"})
_DATE_KINDS: Final = frozenset({"receipt", "invoice"})
_ACCOUNT_KINDS: Final = frozenset({"statement", "tax_form"})


@dataclass(frozen=True)
class Classification:
    kind: str | None
    method: str
    confidence: dict[str, float]
    institution: str | None = None
    fingerprint: Fingerprint | None = None
    last4: Last4 = field(default_factory=Last4)
    period: Period = field(default_factory=Period)
    printed_date: PrintedDate = field(default_factory=PrintedDate)
    tax_year: TaxYear = field(default_factory=TaxYear)
    scores: dict[str, float] = field(default_factory=dict)
    review_reasons: tuple[str, ...] = ()

    @property
    def parser(self) -> str | None:
        return self.fingerprint.spec.parser if self.fingerprint else None

    def facts(self) -> dict[str, Any]:
        """JSON summary for ``document.confidence`` and the ingest log (no account digits)."""
        out: dict[str, Any] = {
            "version": CLASSIFIER_VERSION,
            "method": self.method,
            "kind": self.kind,
            "fields": dict(self.confidence),
        }
        if self.fingerprint is not None:
            out["fingerprint"] = {
                "key": self.fingerprint.key,
                "origin": self.fingerprint.origin,
                "version": self.fingerprint.spec.version,
                "parser": self.fingerprint.spec.parser,
            }
            if self.fingerprint.fingerprint_id is not None:
                out["fingerprint"]["id"] = str(self.fingerprint.fingerprint_id)
        if self.institution:
            out["institution_name"] = self.institution
        if self.period.start is not None:
            out["period"] = {
                "source": self.period.source,
                "inferred": self.period.inferred,
                "start": self.period.start.isoformat(),
                "end": self.period.end.isoformat() if self.period.end else None,
            }
        if self.printed_date.value is not None:
            out["printed_date"] = self.printed_date.value.isoformat()
        if self.scores:
            out["classifier"] = {"model": shipped_model().model_version, "scores": self.scores}
        if self.review_reasons:
            out["review"] = list(self.review_reasons)
        return out


def _order(fingerprint: Fingerprint | None) -> DateOrder | None:
    return fingerprint.spec.date_order if fingerprint is not None else None


def classify_view(
    view: DocumentView,
    *,
    fingerprints: Sequence[Fingerprint] | None = None,
    model: KindModel | None = None,
) -> Classification:
    """Classify an already-built view (pure)."""
    confidence: dict[str, float] = {}
    reasons: list[str] = []
    fps = builtin_fingerprints() if fingerprints is None else fingerprints
    match: FingerprintMatch | None = None
    scores: dict[str, float] = {}
    institution: str | None = None
    if view.statement_last4 or view.statement_periods:
        kind: str | None = "statement"
        method = "structured"
        confidence["kind"] = 0.99
        if len(view.statement_institutions) == 1:
            institution = view.statement_institutions[0]
            confidence["institution"] = 0.9
    else:
        match, matches = best_match(fps, view)
        if match is not None:
            kind = match.fingerprint.spec.kind
            method = "fingerprint"
            institution = match.fingerprint.spec.institution
            confidence["kind"] = match.confidence
            confidence["institution"] = match.confidence
        else:
            if matches:
                reasons.append("ambiguous_fingerprint")
            prediction = predict(model or shipped_model(), view.text)
            scores = prediction.scores
            method = "classifier"
            if prediction.kind is not None and prediction.confidence >= KIND_THRESHOLD:
                kind = prediction.kind
                confidence["kind"] = prediction.confidence
            else:
                kind = None
                method = "none" if prediction.kind is None else "classifier"
                confidence["kind"] = prediction.confidence
                reasons.append("low_confidence_kind")
    fingerprint = match.fingerprint if match is not None else None
    order = _order(fingerprint)
    period = Period()
    printed = PrintedDate()
    tax_year = TaxYear()
    last4 = Last4()
    if kind in _PERIOD_KINDS:
        period = detect_period(view, order=order)
        if period.start is not None:
            confidence["period"] = round(period.confidence, 4)
        elif kind == "statement":
            reasons.append("no_period")
    if kind in _DATE_KINDS:
        printed = detect_printed_date(view, order=order)
        if printed.value is not None:
            confidence["printed_date"] = printed.confidence
    if kind == "tax_form":
        tax_year = detect_tax_year(view)
        if tax_year.value is not None:
            confidence["tax_year"] = tax_year.confidence
        else:
            reasons.append("no_tax_year")
    if kind in _ACCOUNT_KINDS:
        last4 = detect_last4(view, fingerprint.masks if fingerprint else ())
        if last4.value is not None:
            confidence["last4"] = last4.confidence
    return Classification(
        kind=kind,
        method=method,
        confidence=confidence,
        institution=institution,
        fingerprint=fingerprint,
        last4=last4,
        period=period,
        printed_date=printed,
        tax_year=tax_year,
        scores=scores,
        review_reasons=tuple(reasons),
    )


def classify_extraction(
    result: ExtractResult,
    *,
    fingerprints: Sequence[Fingerprint] | None = None,
    model: KindModel | None = None,
) -> Classification:
    """Classify a validated sandbox result (pure; see :func:`classify_view`)."""
    return classify_view(
        DocumentView.from_extraction(result), fingerprints=fingerprints, model=model
    )


def all_fingerprints(db: Session, household_id: uuid.UUID) -> list[Fingerprint]:
    """The household's own fingerprints first, then the shipped catalogue."""
    return [*household_fingerprints(db, household_id), *builtin_fingerprints()]


def resolve_institution(
    db: Session, household_id: uuid.UUID, classification: Classification
) -> uuid.UUID | None:
    """The household's institution the classification names (by id or by name)."""
    fingerprint = classification.fingerprint
    if fingerprint is not None and fingerprint.institution_id is not None:
        return fingerprint.institution_id
    if not normalize_payee(classification.institution):
        return None
    rows = db.execute(
        select(Institution.id, Institution.name).where(Institution.household_id == household_id)
    ).all()
    # Exact name first, else the single institution the name means (match.institutions).
    return pick(classification.institution, [(i, name) for i, name in rows])


def apply_classification(
    db: Session, document: Document, classification: Classification
) -> dict[str, Any]:
    """Write ``classification`` on ``document``; returns facts for the ingest log.

    Fields below :data:`FIELD_THRESHOLD` are not written. An explicit
    ``account_id`` given at upload is never replaced. A printed period is
    stored with its source; an inferred one (transaction or line dates) is
    only a hint in ``document.confidence`` (``period``), never the
    document's period: the parsed statements set that (pipeline).
    ``document.confidence`` carries per-field confidence.
    """
    c = classification
    facts: dict[str, Any] = {"method": c.method, "kind": c.kind}
    if c.kind is not None:
        document.kind = DocumentKind(c.kind)
    institution_id = None
    if c.institution and c.confidence.get("institution", 0.0) >= FIELD_THRESHOLD:
        institution_id = resolve_institution(db, document.household_id, c)
        if institution_id is not None and document.institution_id is None:
            document.institution_id = institution_id
        facts["institution_known"] = institution_id is not None
    if (
        c.period.start is not None
        and not c.period.inferred
        and c.period.confidence >= FIELD_THRESHOLD
    ):
        document.period_start, document.period_end = c.period.start, c.period.end
    elif c.printed_date.value is not None and c.printed_date.confidence >= FIELD_THRESHOLD:
        document.period_start = document.period_end = c.printed_date.value
    if c.tax_year.value is not None and c.tax_year.confidence >= FIELD_THRESHOLD:
        document.tax_year = c.tax_year.value
    confidence = dict(c.confidence)
    if document.account_id is None and c.last4.value is not None:
        name = c.institution
        match = match_account(
            db,
            document.household_id,
            last4=c.last4.value,
            institution=name,
            allowed=importable_account_ids(db, document.household_id, document.uploaded_by),
        )
        facts["account_candidates"] = max(len(match.candidates), 1 if match.account_id else 0)
        if match.account_id is not None:
            document.account_id = match.account_id
            # Unique by last four digits alone, or only together with the institution.
            confidence["account"] = round(
                c.last4.confidence * (0.95 if not match.candidates else 0.9), 4
            )
            if institution_id is None and document.institution_id is None:
                account_institution = _account_institution(db, document)
                if account_institution is not None:
                    document.institution_id = account_institution
    summary = c.facts()
    summary["fields"] = confidence
    document.confidence = summary
    facts["fields"] = confidence
    if c.review_reasons:
        facts["review"] = list(c.review_reasons)
    return facts


def _account_institution(db: Session, document: Document) -> uuid.UUID | None:
    return db.execute(
        select(Account.institution_id).where(
            Account.household_id == document.household_id, Account.id == document.account_id
        )
    ).scalar_one_or_none()


def classify_document(db: Session, document: Document, result: ExtractResult) -> dict[str, Any]:
    """Classify ``result`` with the household's and shipped fingerprints and apply it."""
    classification = classify_extraction(
        result, fingerprints=all_fingerprints(db, document.household_id)
    )
    return apply_classification(db, document, classification)
