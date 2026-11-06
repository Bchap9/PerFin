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
2. Otherwise every parser scores the pages (``detect``); the best score of
   at least :data:`MIN_SCORE` wins (ties: the order of :data:`PARSERS`, so
   an institution parser beats the generic one).
3. **Fallback.** When the chosen parser reads nothing usable (no
   transactions, positions, activity, payslip, receipt, loan or tax form),
   the next-best parsers scoring at least :data:`MIN_SCORE` are tried in
   turn and the first that reads something is used (warning
   ``parser_fallback:<the parser that read nothing>``).

The chosen parser's statements (and brokerage ``investments``) replace any
an earlier parser run left in the result (``source_format`` ``pdf``/``image``);
structured statements from the sandbox (OFX, QFX, QIF) are never touched. What happened
is recorded in ``ExtractResult.parse`` (:class:`ParseInfo`: parser and
version, all scores, the threshold, pending and skipped line counts,
warnings, a payslip), so a later parser version re-runs from the stored
pages with no new upload or extraction (:func:`reparse`).
"""

from __future__ import annotations

from typing import Final

from app.ingestion.parsers.base import DocumentParser, DocumentView, ParseOutcome
from app.ingestion.parsers.brokerage import BROKERAGE_PARSERS
from app.ingestion.parsers.confidence import DEFAULT_THRESHOLD
from app.ingestion.parsers.institutions import INSTITUTION_PARSERS
from app.ingestion.parsers.loan_statement import LoanStatementParser
from app.ingestion.parsers.payslip import PayslipParser
from app.ingestion.parsers.receipt import ReceiptParser
from app.ingestion.parsers.table_statement import CardStatementParser, TableStatementParser
from app.ingestion.parsers.tax_forms import TaxFormParser
from app.ingestion.sandbox.protocol import (
    DOCUMENT_STATEMENT_FORMATS,
    IMAGE_KINDS,
    ExtractResult,
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
        found = parser.parse(view, threshold=threshold, date_order=date_order)
        if not found.empty:
            found.warnings = [f"parser_fallback:{chosen.name}"[:64], *found.warnings]
            return parser, found
    return chosen, outcome


def parse_extraction(
    result: ExtractResult,
    *,
    threshold: float = DEFAULT_THRESHOLD,
    parser_name: str | None = None,
    institution: str | None = None,
    kind: str | None = None,
    date_order: str | None = None,
) -> ExtractResult:
    """``result`` with the document parsers' statements and :class:`ParseInfo`."""
    view = DocumentView.from_result(result)
    scores = {p.name: p.detect(view) for p in PARSERS}
    chosen = _hinted(scores, parser_name=parser_name, institution=institution, kind=kind)
    if chosen is None:
        best = max(PARSERS, key=lambda p: scores[p.name])
        chosen = best if scores[best.name] >= MIN_SCORE else None
    outcome = ParseOutcome()
    if chosen is not None:
        outcome = chosen.parse(view, threshold=threshold, date_order=date_order)
        if outcome.empty:
            chosen, outcome = _fallback(view, scores, chosen, outcome, threshold, date_order)
    else:
        outcome.warnings.append("no_parser")
    kept = [s for s in result.statements if s.source_format not in DOCUMENT_STATEMENT_FORMATS]
    kept_investments = [
        i for i in result.investments if i.source_format not in DOCUMENT_STATEMENT_FORMATS
    ]
    info = ParseInfo(
        parser=_ref(chosen) if chosen is not None else None,
        scores=[
            ParserScore(name=name, score=score)
            for name, score in sorted(scores.items(), key=lambda kv: -kv[1])
        ],
        threshold=threshold,
        pending_items=outcome.pending_items,
        skipped_lines=outcome.skipped_lines,
        payslip=outcome.payslip,
        receipt=outcome.receipt,
        loan=outcome.loan,
        tax_forms=outcome.tax_forms,
        warnings=outcome.warnings[:100],
    )
    return result.model_copy(
        update={
            "statements": [*kept, *outcome.statements],
            "investments": [*kept_investments, *outcome.investments],
            "parse": info,
        }
    )


def reparse(result: ExtractResult, *, threshold: float = DEFAULT_THRESHOLD) -> ExtractResult:
    """Run the parsers again on a stored extraction, keeping the earlier choice as a hint."""
    previous = result.parse.parser if result.parse is not None else None
    return parse_extraction(
        result,
        threshold=threshold,
        parser_name=previous.name if previous is not None else None,
    )
