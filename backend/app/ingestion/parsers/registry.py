# SPDX-License-Identifier: AGPL-3.0-or-later
"""Choosing and running a document parser for an extraction (INGESTION.md "Parse").

:data:`PARSERS` lists every document parser, institution parsers first. For
one extraction (:func:`parse_extraction`):

1. A **hint** from the classifier wins when it names a parser that sees its
   layout on the pages (``detect`` above 0): first a parser written for the
   matched fingerprint's ``key`` (the institution parsers), then the parser
   the fingerprint names (``parser``, e.g. ``statement_generic``), then the
   payslip (receipt, tax form) parser for a ``payslip`` (``receipt``,
   ``tax_form``) kind. The fingerprint's ``date_order``
   is handed to a generic parser.
   **A statement is never a receipt.** Before any choice, the receipt
   parser's score is set to 0 for a document the classifier calls a
   statement, and for one with a transaction table header on its pages
   (``statement_generic`` above 0) that the classifier does not call a
   receipt (:func:`_without_receipt`): a page of activity that ends in
   "Total" lines is not a purchase. So the receipt parser is neither
   chosen nor tried as a fallback there, and ``ParseInfo.scores`` shows 0.
   With no classifier kind the table decides, also for a receipt whose
   items sit under such a header: its rows then wait for a person, where
   a statement read as a receipt would lose its rows behind one total.
2. Otherwise every parser scores the pages (``detect``); the best score of
   at least :data:`MIN_SCORE` wins (ties: the order of :data:`PARSERS`, so
   an institution parser beats the generic one).
3. **Fallback.** When the chosen parser reads nothing usable (no
   transactions, positions, activity, payslip, receipt, loan or tax form),
   the next-best parsers scoring at least :data:`MIN_SCORE` are tried in
   turn and the first that reads something is used (warning
   ``parser_fallback:<the parser that read nothing>``).

4. **Learned layout.** With a household ``template`` (a layout taught in
   "Teach PerFin", :mod:`app.ingestion.parsers.template`), its labelled
   tables replace what the parsers read (parser ``layout_template``) and
   its anchors fill the period, the account's last four digits and the
   balances (:func:`_with_template`).

**Hints on the view.** The period the classifier read from a printed label
and the day the file arrived are put on the view every parser reads
(``DocumentView.hints``), so ``DocumentParser.parse`` keeps its signature.

The chosen parser's statements (and brokerage ``investments``) replace any
an earlier parser run left in the result (``source_format`` ``pdf``/``image``);
structured statements from the sandbox (OFX, QFX, QIF) are never touched. What happened
is recorded in ``ExtractResult.parse`` (:class:`ParseInfo`: parser and
version, all scores, the threshold, pending and skipped line counts,
warnings, a payslip), so a later parser version re-runs from the stored
pages with no new upload or extraction (:func:`reparse`).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import Final

from app.ingestion.parsers.base import DocumentParser, DocumentView, ParseOutcome, ViewHints
from app.ingestion.parsers.brokerage import BROKERAGE_PARSERS
from app.ingestion.parsers.confidence import DEFAULT_THRESHOLD
from app.ingestion.parsers.institutions import INSTITUTION_PARSERS
from app.ingestion.parsers.loan_statement import LoanStatementParser
from app.ingestion.parsers.payslip import PayslipParser
from app.ingestion.parsers.plan_statement import RetirementPlanParser
from app.ingestion.parsers.receipt import ReceiptParser
from app.ingestion.parsers.table_statement import CardStatementParser, TableStatementParser
from app.ingestion.parsers.tax_forms import TaxFormParser
from app.ingestion.parsers.template import (
    TEMPLATE_REF,
    LayoutTemplate,
    fill_statement,
    read_template,
)
from app.ingestion.parsers.vocabulary import Vocabulary
from app.ingestion.sandbox.protocol import (
    DOCUMENT_STATEMENT_FORMATS,
    IMAGE_KINDS,
    MAX_SKIPPED_REGIONS,
    ExtractResult,
    LineRegion,
    ParseInfo,
    ParserRef,
    ParserScore,
)

#: Extraction kinds the document parsers read (pages with words).
DOCUMENT_KINDS: Final = frozenset({"pdf", *IMAGE_KINDS})
MIN_SCORE: Final = 0.35

PARSERS: Final[tuple[DocumentParser, ...]] = (
    # A "Form W-2"/"Form 1099-INT" heading with its box labels is unambiguous,
    # even on a bank's letterhead (tax forms first).
    TaxFormParser(),
    *(cls() for cls in INSTITUTION_PARSERS),
    *(cls() for cls in BROKERAGE_PARSERS),
    RetirementPlanParser(),
    LoanStatementParser(),
    PayslipParser(),
    ReceiptParser(),
    CardStatementParser(),
    TableStatementParser(),
)


def parser_named(name: str) -> DocumentParser | None:
    return next((p for p in PARSERS if p.name == name), None)


def _ref(parser: DocumentParser) -> ParserRef:
    return ParserRef(
        name=parser.name, version=parser.version, institution=parser.institution, kind=parser.kind
    )


def _without_receipt(scores: dict[str, float], kind: str | None) -> dict[str, float]:
    """``scores`` with the receipt parsers at 0 where the document is a statement.

    That is: the classifier says ``statement``, or a transaction table header
    is on the pages (the generic statement parser scores above 0) and the
    classifier does not say ``receipt``. A document classified as a receipt
    keeps its receipt parser, table or not.
    """
    if kind == "receipt":
        return scores
    if kind != "statement" and scores.get(TableStatementParser.name, 0.0) <= 0:
        return scores
    receipts = {p.name for p in PARSERS if p.kind == "receipt"}
    return {name: 0.0 if name in receipts else score for name, score in scores.items()}


def _hinted(
    scores: dict[str, float],
    *,
    parser_name: str | None,
    institution: str | None,
    kind: str | None,
) -> DocumentParser | None:
    candidates: list[DocumentParser] = []
    if institution:
        candidates.extend(p for p in PARSERS if p.institution == institution)
    if parser_name:
        named = parser_named(parser_name)
        if named is not None:
            candidates.append(named)
    if kind in ("payslip", "receipt", "tax_form"):
        candidates.extend(p for p in PARSERS if p.kind == kind)
    # A hint only picks a parser that sees its layout on the pages at all.
    return next((p for p in candidates if scores.get(p.name, 0.0) > 0), None)


def _fallback(
    view: DocumentView,
    scores: dict[str, float],
    chosen: DocumentParser,
    outcome: ParseOutcome,
    threshold: float,
    date_order: str | None,
    vocabulary: Vocabulary | None = None,
) -> tuple[DocumentParser, ParseOutcome]:
    """The next-best parser's outcome when ``chosen`` read nothing usable.

    Parsers scoring at least :data:`MIN_SCORE` are tried best first (ties:
    the order of :data:`PARSERS`); the first that reads a statement, an
    investment statement, a payslip, a receipt, a loan or tax form is used,
    with ``parser_fallback:<name>`` naming the one that read nothing. None
    does: ``chosen`` and its outcome stay.
    """
    ranked = sorted(
        (p for p in PARSERS if p is not chosen and scores[p.name] >= MIN_SCORE),
        key=lambda p: -scores[p.name],
    )
    for parser in ranked:
        found = parser.parse_with(
            view, threshold=threshold, date_order=date_order, vocabulary=vocabulary
        )
        if not found.empty:
            found.warnings = [f"parser_fallback:{chosen.name}"[:64], *found.warnings]
            return parser, found
    return chosen, outcome


def _with_template(
    view: DocumentView, template: LayoutTemplate, outcome: ParseOutcome, threshold: float
) -> tuple[ParseOutcome, bool]:
    """``outcome`` read again with a learned layout; True when its tables were used."""
    taught = read_template(view, template, threshold=threshold)
    outcome.skipped_lines += taught.skipped_lines
    outcome.skipped_regions.extend(taught.skipped)
    outcome.warnings.extend(taught.warnings)
    if taught.read:
        if taught.statements:
            outcome.statements = taught.statements
        if taught.investments:
            outcome.investments = taught.investments
        outcome.warnings = [w for w in outcome.warnings if w != "no_parser"]
        outcome.warnings.append("layout_template")
        return outcome, True
    if taught.fields:
        single = len(outcome.statements) + len(outcome.investments) == 1
        outcome.statements = [
            fill_statement(s, taught.fields, single=single) for s in outcome.statements
        ]
        outcome.warnings.append("layout_template_fields")
    return outcome, False


def parse_extraction(
    result: ExtractResult,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    parser_name: str | None = None,
    institution: str | None = None,
    kind: str | None = None,
    date_order: str | None = None,
    template: LayoutTemplate | None = None,
    vocabulary: Vocabulary | None = None,
    period: tuple[date, date, float] | None = None,
    not_after: date | None = None,
) -> ExtractResult:
    """``result`` with the document parsers' statements and :class:`ParseInfo`.

    ``period`` (first day, last day, confidence) is the statement period the
    classifier read from a printed label and ``not_after`` the day the file
    arrived; both reach every parser on the view
    (:class:`~app.ingestion.parsers.base.ViewHints`).

    ``vocabulary``: the local model's suggested kinds for words a brokerage
    parser could not type on an earlier run (:mod:`app.ingestion.parsers.vocabulary`,
    ADR-0010); the words still untyped are in ``ParseInfo.unknown_actions``
    / ``unknown_headings``.
    """
    view = replace(
        DocumentView.from_result(result),
        hints=ViewHints(
            period=period[:2] if period is not None else None,
            period_confidence=period[2] if period is not None else 0.0,
            not_after=not_after,
        ),
    )
    scores = _without_receipt({p.name: p.detect(view) for p in PARSERS}, kind)
    chosen = _hinted(scores, parser_name=parser_name, institution=institution, kind=kind)
    if chosen is None:
        best = max(PARSERS, key=lambda p: scores[p.name])
        chosen = best if scores[best.name] >= MIN_SCORE else None
    outcome = ParseOutcome()
    if chosen is not None:
        outcome = chosen.parse_with(
            view, threshold=threshold, date_order=date_order, vocabulary=vocabulary
        )
        if outcome.empty:
            chosen, outcome = _fallback(
                view, scores, chosen, outcome, threshold, date_order, vocabulary
            )
    else:
        outcome.warnings.append("no_parser")
    taught = False
    if template is not None and not template.empty:
        outcome, taught = _with_template(view, template, outcome, threshold)
    ref = TEMPLATE_REF if taught else (_ref(chosen) if chosen is not None else None)
    kept = [s for s in result.statements if s.source_format not in DOCUMENT_STATEMENT_FORMATS]
    kept_investments = [
        i for i in result.investments if i.source_format not in DOCUMENT_STATEMENT_FORMATS
    ]
    info = ParseInfo(
        parser=ref,
        scores=[
            ParserScore(name=name, score=score)
            for name, score in sorted(scores.items(), key=lambda kv: -kv[1])
        ],
        threshold=threshold,
        pending_items=outcome.pending_items,
        skipped_lines=outcome.skipped_lines,
        skipped_regions=[
            LineRegion(page=page, bbox=bbox)
            for page, bbox in outcome.skipped_regions[:MAX_SKIPPED_REGIONS]
        ],
        payslip=outcome.payslip,
        receipt=outcome.receipt,
        loan=outcome.loan,
        tax_forms=outcome.tax_forms,
        warnings=outcome.warnings[:100],
        unknown_actions=outcome.unknown_actions,
        unknown_headings=outcome.unknown_headings,
        unknown_columns=outcome.unknown_columns,
        unread_accounts=outcome.unread_accounts,
    )
    return result.model_copy(
        update={
            "statements": [*kept, *outcome.statements],
            "investments": [*kept_investments, *outcome.investments],
            "parse": info,
        }
    )


def reparse(
    result: ExtractResult,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    period: tuple[date, date, float] | None = None,
    not_after: date | None = None,
) -> ExtractResult:
    """Run the parsers again on a stored extraction, keeping the earlier choice as a hint.

    A document a receipt parser read is read again as a receipt (``kind``),
    so its item table does not turn it into a statement
    (:func:`_without_receipt`). ``period`` and ``not_after`` are those of
    :func:`parse_extraction`: a caller that has them hands them on, so both
    runs read the same hints.
    """
    previous = result.parse.parser if result.parse is not None else None
    return parse_extraction(
        result,
        threshold=threshold,
        parser_name=previous.name if previous is not None else None,
        kind="receipt" if previous is not None and previous.kind == "receipt" else None,
        period=period,
        not_after=not_after,
    )
