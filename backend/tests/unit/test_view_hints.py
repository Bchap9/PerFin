# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the parsers are told beyond the pages (``base.ViewHints``).

The classifier's printed period and the day the file arrived travel on the
:class:`DocumentView`, next to the day the PDF says it was made; a parser's
``parse`` signature does not change. Nothing reads them here: these tests
pin only that they arrive.
"""

from __future__ import annotations

import io
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy.orm import Session

from app.domain.documents.models import Document, DocumentKind
from app.ingestion.extract.pdf import extract_pdf
from app.ingestion.parsers.base import DocumentView, ParseOutcome, ViewHints, created_day
from app.ingestion.parsers.confidence import DEFAULT_THRESHOLD
from app.ingestion.parsers.registry import parse_extraction
from app.ingestion.parsers.table_statement import TableStatementParser
from app.ingestion.pipeline import _parse_document, _period_hint
from app.ingestion.sandbox.protocol import ExtractOptions, ExtractResult
from tests.fixtures import synth_parser_choice as pc

OPTIONS = ExtractOptions(previews=False, ocr=False)


def _extract(data: bytes) -> ExtractResult:
    result = extract_pdf(data, OPTIONS, "pdf")
    assert result.ok, result.error
    return result


def _view(*, created: date | None = None, not_after: date | None = None) -> DocumentView:
    return DocumentView(
        lines=(),
        source_format="pdf",
        truncated=False,
        page_count=0,
        hints=ViewHints(not_after=not_after),
        created=created,
    )


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("D:20260312101500+00'00'", date(2026, 3, 12)),
        ("20260312", date(2026, 3, 12)),
        ("D:20261340", None),
        ("yesterday", None),
        (None, None),
        ("D:18990101", None),
        ("", None),
        ("D:2026031", None),
        ("D:20260230", None),
    ],
)
def test_created_day(text: str | None, expected: date | None) -> None:
    assert created_day(text) == expected


def test_a_view_has_no_hints_unless_given() -> None:
    view = DocumentView(lines=(), source_format="pdf", truncated=False, page_count=0)
    assert view.hints == ViewHints()
    assert view.hints.period is None
    assert view.hints.period_confidence == 0.0
    assert view.hints.not_after is None
    assert view.created is None


def _pdf_made_now() -> bytes:
    """One page whose creation date is the clock's (the fixtures pin theirs)."""
    out = io.BytesIO()
    c = canvas.Canvas(out, pagesize=letter, invariant=0)
    c.setFont("Helvetica", 10)
    c.drawString(54, 720, "Larchmere Commons Residents Fund")
    c.drawString(54, 700, "Synthetic test document")
    c.showPage()
    c.save()
    return out.getvalue()


def test_view_carries_the_pdf_creation_day() -> None:
    view = DocumentView.from_result(_extract(_pdf_made_now()))
    assert view.created is not None
    # One day of slack: the PDF's clock and the test's may sit in different zones.
    assert abs(view.created - date.today()) <= timedelta(days=1)


def test_view_carries_the_day_a_fixture_pins() -> None:
    # reportlab's invariant mode writes D:20000101000000+00'00'.
    view = DocumentView.from_result(_extract(pc.activity_page_with_totals()))
    assert view.created == date(2000, 1, 1)


def test_ceiling_is_the_earlier_day() -> None:
    early, late = date(2026, 3, 31), date(2026, 4, 2)
    assert _view(created=late, not_after=early).ceiling == early
    assert _view(created=early, not_after=late).ceiling == early
    assert _view(created=late).ceiling == late
    assert _view(not_after=early).ceiling == early
    assert _view().ceiling is None


def test_parse_extraction_hands_hints_to_the_view(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[DocumentView] = []

    def capture(self: TableStatementParser, view: DocumentView, **_: Any) -> ParseOutcome:
        seen.append(view)
        return ParseOutcome()

    monkeypatch.setattr(TableStatementParser, "parse_with", capture)
    start, end, received = date(2026, 3, 1), date(2026, 3, 31), date(2026, 4, 6)
    parse_extraction(
        _extract(pc.activity_page_with_totals()),
        period=(start, end, 0.95),
        not_after=received,
    )
    assert seen
    view = seen[0]
    assert view.hints.period == (start, end)
    assert view.hints.period_confidence == 0.95
    assert view.hints.not_after == received
    assert view.created is not None
    assert view.lines


def test_parse_extraction_without_hints_gives_an_empty_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[DocumentView] = []

    def capture(self: TableStatementParser, view: DocumentView, **_: Any) -> ParseOutcome:
        seen.append(view)
        return ParseOutcome()

    monkeypatch.setattr(TableStatementParser, "parse_with", capture)
    parse_extraction(_extract(pc.activity_page_with_totals()))
    assert seen
    assert seen[0].hints == ViewHints()


def _facts(**period: Any) -> dict[str, Any]:
    printed = {
        "inferred": False,
        "start": "2026-03-01",
        "end": "2026-03-31",
        "source": "label",
    }
    return {"period": {**printed, **period}, "fields": {"period": 0.95}}


def test_period_hint_reads_only_a_printed_period() -> None:
    assert _period_hint(Document(confidence=_facts())) == (
        date(2026, 3, 1),
        date(2026, 3, 31),
        0.95,
    )
    assert _period_hint(Document(confidence=_facts(inferred=True))) is None
    assert _period_hint(Document(confidence=_facts(end=None))) is None
    weak = _facts()
    weak["fields"]["period"] = 0.4
    assert _period_hint(Document(confidence=weak)) is None


@pytest.mark.parametrize(
    "confidence",
    [
        None,
        {},
        {"fields": {"period": 0.95}},
        {"period": "2026-03", "fields": {"period": 0.95}},
        _facts(start="03/01/2026"),
        _facts(start="2026-04-01"),
        _facts(inferred=None),
        {"period": _facts()["period"]},
        {"period": _facts()["period"], "fields": {"period": "high"}},
        {"period": _facts()["period"], "fields": {"period": True}},
    ],
)
def test_period_hint_is_none_for_anything_else(confidence: dict[str, Any] | None) -> None:
    assert _period_hint(Document(confidence=confidence)) is None


class _SeenError(Exception):
    """Raised by the capturing parser: the view is all these tests need."""


def _seen_by_the_pipeline(monkeypatch: pytest.MonkeyPatch, document: Document) -> DocumentView:
    """The view ``_parse_document`` hands a parser for ``document``.

    The parse stops at the first view, before anything is stored, so no
    database is needed: the session is a mock that must stay unused.
    """
    seen: list[DocumentView] = []

    def capture(self: TableStatementParser, view: DocumentView, **_: Any) -> ParseOutcome:
        seen.append(view)
        raise _SeenError

    monkeypatch.setattr(TableStatementParser, "parse_with", capture)
    db = MagicMock(spec=Session)
    with pytest.raises(_SeenError):
        _parse_document(db, document, _extract(pc.activity_page_with_totals()), DEFAULT_THRESHOLD)
    assert not db.mock_calls
    (view,) = seen
    return view


def test_the_pipeline_hands_the_period_and_the_day_of_receipt_to_the_parsers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = Document(
        kind=DocumentKind.STATEMENT,
        confidence=_facts(),
        created_at=datetime(2026, 4, 6, 9, 30, tzinfo=UTC),
    )
    view = _seen_by_the_pipeline(monkeypatch, document)
    assert view.hints.period == (date(2026, 3, 1), date(2026, 3, 31))
    assert view.hints.period_confidence == 0.95
    assert view.hints.not_after == date(2026, 4, 6)
    # The fixture pins its creation day far in the past: the earlier day wins.
    assert view.ceiling == date(2000, 1, 1)


def test_the_pipeline_hands_no_period_it_did_not_read_from_a_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    document = Document(
        kind=DocumentKind.STATEMENT,
        confidence=_facts(inferred=True),
        created_at=datetime(2026, 4, 6, 9, 30, tzinfo=UTC),
    )
    view = _seen_by_the_pipeline(monkeypatch, document)
    assert view.hints == ViewHints(not_after=date(2026, 4, 6))
