# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full-text search over extracted document text (ARCHITECTURE.md, API.md ``GET /documents?q=``).

* **Index**: ``document.extracted_text`` (``tsvector``, GIN index
  ``ix_document_extracted_text``) is written by the worker from the
  validated extraction (:func:`update_search_index`, called by the
  pipeline after every extraction; a re-extraction rewrites it, a failed one
  clears it). The text is the document's pages (or the mail's subject and
  body) plus the cells of structured tables, at most
  :data:`INDEX_TEXT_MAX` characters, through ``to_tsvector('simple', ...)``
  (no stemming or stop words: statements mix languages, names and numbers).
  The value is a bound parameter; a text whose vector would exceed
  PostgreSQL's 1 MB limit is indexed from a shorter prefix, inside a
  savepoint so the pipeline's transaction survives.
* **Query**: ``websearch_to_tsquery('simple', :q)`` (quoted phrases, ``or``,
  ``-word``; never a syntax error), ranked by ``ts_rank_cd``; the keyset
  cursor is the integer score and the id (:func:`encode_cursor`).
* **Metadata-only search** (I1): a caller who may not download documents
  (a viewer without the household's download grant) never searches the
  extracted text: their ``q`` matches and ranks only the document's
  ``original_filename`` (:func:`metadata_matches`, :func:`metadata_score`),
  metadata they already see in the list. Otherwise every query would be a
  yes/no oracle over the text of documents they may not open (account
  numbers, tax ids, salaries guessed token by token).
* **Snippets** (I1): only for callers who may download the document
  (member or above, or a viewer where the household allows downloads):
  ``ts_headline`` over the stored text with private-use markers, turned into
  plain-text segments ``{"text", "match"}``. Never HTML: the client renders
  the segments as text, so nothing in a document can become markup.
  Computed only for the rows of the returned page.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import BigInteger, ColumnElement, Text, cast, func, literal, select, update
from sqlalchemy.dialects.postgresql import REGCONFIG
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.core.errors import ProblemError
from app.domain.documents.models import Document

SEARCH_CONFIG: Final = "simple"
#: Characters of extracted text indexed per document (a prefix beyond).
INDEX_TEXT_MAX: Final = 500_000
#: Fallback lengths tried when a vector exceeds PostgreSQL's size limit.
_FALLBACK_LENGTHS: Final = (100_000, 20_000)
#: Characters of stored text ``ts_headline`` looks at.
HEADLINE_TEXT_MAX: Final = 200_000
SNIPPET_MAX: Final = 400
Q_MAX: Final = 200
#: Rank to integer score (keyset cursor; equality is exact on integers).
_SCORE_SCALE: Final = 1_000_000

_START, _STOP, _FRAGMENT = "\ue000", "\ue001", "\ue002"
_MARKERS: Final = re.compile(f"[{_START}{_STOP}{_FRAGMENT}]")
_CONTROL: Final = re.compile("[\x00-\x08\x0b-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2066-\u2069]")
_WS: Final = re.compile(r"\s+")
_HEADLINE_OPTIONS: Final = (
    f"StartSel={_START}, StopSel={_STOP}, MaxWords=24, MinWords=8, "
    f"MaxFragments=2, FragmentDelimiter={_FRAGMENT}"
)


def _config() -> ColumnElement[Any]:
    return cast(literal(SEARCH_CONFIG), REGCONFIG)


# ---------------------------------------------------------------------------
# Index (worker)
# ---------------------------------------------------------------------------


def _table_text(extraction: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for table in extraction.get("tables") or []:
        for row in table.get("rows") or []:
            out.append(" ".join(str(cell) for cell in row if cell))
    return out


def index_text(extraction: dict[str, Any] | None) -> str:
    """The text indexed for a stored extraction (``document.extraction``)."""
    if not extraction or not extraction.get("ok"):
        return ""
    parts: list[str] = []
    email = extraction.get("email") or {}
    subject = (email.get("headers") or {}).get("subject")
    if subject:
        parts.append(str(subject))
    text = extraction.get("text") or ""
    if not text:
        text = "\f".join(str(page.get("text") or "") for page in extraction.get("pages") or [])
    parts.append(str(text))
    parts.extend(_table_text(extraction))
    joined = "\n".join(part for part in parts if part)
    # PostgreSQL text cannot hold NUL; the markers are reserved for snippets.
    return _MARKERS.sub(" ", joined.replace("\x00", " "))[:INDEX_TEXT_MAX]


def update_search_index(db: Session, document: Document) -> int:
    """Rewrite ``document.extracted_text`` from its extraction; return the characters indexed."""
    text = index_text(document.extraction)
    where = (Document.household_id == document.household_id) & (Document.id == document.id)
    if not text.strip():
        db.execute(
            update(Document).where(where).values(extracted_text=None),
            execution_options={"synchronize_session": False},
        )
        return 0
    for length in (len(text), *(n for n in _FALLBACK_LENGTHS if n < len(text))):
        try:
            with db.begin_nested():
                db.execute(
                    update(Document)
                    .where(where)
                    .values(extracted_text=func.to_tsvector(_config(), text[:length])),
                    execution_options={"synchronize_session": False},
                )
        except DBAPIError:
            continue  # "string is too long for tsvector": try a shorter prefix
        return length
    db.execute(
        update(Document).where(where).values(extracted_text=None),
        execution_options={"synchronize_session": False},
    )
    return 0


def clear_search_index(db: Session, document: Document) -> None:
    db.execute(
        update(Document)
        .where((Document.household_id == document.household_id) & (Document.id == document.id))
        .values(extracted_text=None),
        execution_options={"synchronize_session": False},
    )


# ---------------------------------------------------------------------------
# Query (API)
# ---------------------------------------------------------------------------


def ts_query(q: str) -> ColumnElement[Any]:
    return func.websearch_to_tsquery(_config(), q)


def matches(q: str) -> ColumnElement[bool]:
    return Document.extracted_text.op("@@")(ts_query(q))


def score(q: str) -> ColumnElement[int]:
    rank = func.ts_rank_cd(Document.extracted_text, ts_query(q))
    return cast(rank * _SCORE_SCALE, BigInteger)


def _metadata_vector() -> ColumnElement[Any]:
    # "statement_2026-04.pdf" -> "statement 2026 04 pdf": the parser would
    # otherwise keep a filename as one "file" token.
    words = func.regexp_replace(
        func.coalesce(Document.original_filename, ""), "[^[:alnum:]]+", " ", "g"
    )
    return func.to_tsvector(_config(), words)


def metadata_matches(q: str) -> ColumnElement[bool]:
    """``q`` against the original filename only (callers who may not download, I1)."""
    return _metadata_vector().op("@@")(ts_query(q))


def metadata_score(q: str) -> ColumnElement[int]:
    rank = func.ts_rank_cd(_metadata_vector(), ts_query(q))
    return cast(rank * _SCORE_SCALE, BigInteger)


def invalid_cursor() -> ProblemError:
    return ProblemError(422, "invalid-cursor", "Invalid cursor", "Start again without a cursor.")


def encode_cursor(points: int | None, row_id: uuid.UUID) -> str:
    """``<id>`` for the date-ordered list, ``<score>.<id>`` for a search."""
    return str(row_id) if points is None else f"{points}.{row_id}"


def decode_cursor(token: str, *, ranked: bool) -> tuple[int | None, uuid.UUID]:
    """The cursor's ``(score, id)`` (score None when not ``ranked``); else ``422``."""
    head, dot, tail = token.partition(".")
    try:
        if ranked:
            if not dot or not head.isdigit() or len(head) > 18:
                raise ValueError
            return int(head), uuid.UUID(tail)
        if dot:
            raise ValueError
        return None, uuid.UUID(token)
    except ValueError:
        raise invalid_cursor() from None


def _segments(headline: str) -> list[dict[str, Any]]:
    """Plain-text segments of a ``ts_headline`` result (markers -> ``match`` flags)."""
    out: list[dict[str, Any]] = []
    used = 0

    def add(text: str, match: bool) -> None:
        nonlocal used
        if not text or used >= SNIPPET_MAX:
            return
        text = text[: SNIPPET_MAX - used]
        used += len(text)
        if out and out[-1]["match"] == match:
            out[-1]["text"] += text
        else:
            out.append({"text": text, "match": match})

    for f_index, fragment in enumerate(headline.split(_FRAGMENT)):
        if f_index:
            add(" … ", False)
        match = False
        for piece in re.split(f"([{_START}{_STOP}])", fragment):
            if piece in (_START, _STOP):
                match = piece == _START
                continue
            add(_WS.sub(" ", _CONTROL.sub(" ", piece)), match)
    if not any(part["match"] for part in out):
        return []
    return out


def snippets(
    db: Session, household_id: uuid.UUID, ids: Sequence[uuid.UUID], q: str
) -> dict[uuid.UUID, list[dict[str, Any]]]:
    """Highlighted plain-text snippets for the documents ``ids`` (callers check I1 first)."""
    if not ids:
        return {}
    stored = func.left(cast(Document.extraction["text"].astext, Text), HEADLINE_TEXT_MAX)
    cleaned = func.translate(stored, _START + _STOP + _FRAGMENT, "")
    headline = func.ts_headline(_config(), cleaned, ts_query(q), _HEADLINE_OPTIONS)
    rows = db.execute(
        select(Document.id, headline).where(
            Document.household_id == household_id, Document.id.in_(list(ids))
        )
    ).all()
    return {row_id: _segments(text or "") for row_id, text in rows}
