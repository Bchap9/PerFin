# SPDX-License-Identifier: AGPL-3.0-or-later
"""Household fingerprints are bounded in number and in matching work
(security review P34-SR-R2-01; SECURITY.md C10, F8).

* A household remembers at most ``MAX_HOUSEHOLD_FINGERPRINTS`` layouts
  (institution, kind, parser); only the latest version of each is matched.
* ``POST /review/{id}/remember-layout`` with a fingerprint that is costly to
  evaluate finishes quickly: the learning check runs under the household
  budget (``422 layout-too-costly``) and the scan for similar documents
  shares a bounded budget.

Documents carry synthetic stored extractions built here.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentKind,
    DocumentStatus,
    ParserFingerprint,
    ReviewKind,
)
from app.domain.review import actions
from app.ingestion.classify import fingerprints
from app.ingestion.classify.fingerprints import (
    MAX_HOUSEHOLD_FINGERPRINTS,
    MAX_RULES,
    FingerprintLimitError,
    household_fingerprints,
    remember_layout,
)
from app.ingestion.classify.safe_regex import MAX_QUANTIFIERS, MAX_SUBJECT_CHARS
from app.ingestion.sandbox.protocol import ExtractResult
from tests.fixtures.documents import make_document, make_review_item
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, problem_type
from tests.fixtures.ledger import make_institution

pytestmark = pytest.mark.integration

REVIEW = f"{API}/review"
PDF = b"%PDF-1.7\n% synthetic placeholder, never parsed by the API\n%%EOF\n"
BANK = "Fictional Thrift Bank"
#: Each search costs ~line^2 steps on a line of "a a a ..." (never matches).
HOSTILE = "[a ]*" * MAX_QUANTIFIERS + "!"
HOSTILE_MATCH: dict[str, Any] = {
    "all": [{"header": BANK}],
    "any": [{"regex": HOSTILE + "y" * i} for i in range(MAX_RULES)],
    "min_any": 0,
}
RULES = {"all": [{"header": BANK}]}


@pytest.fixture
def doc_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )


@pytest.fixture
def actors(doc_settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(doc_settings, factories) as opened:
        yield opened


@pytest.fixture
def store(doc_settings: Settings) -> LocalFSBlobStore:
    return LocalFSBlobStore.from_settings(doc_settings)


@pytest.fixture
def owner(actors: Actors) -> AuthedClient:
    return actors.new(role="owner")


def _hh(user: AuthedClient) -> uuid.UUID:
    household_id = user.record.household_id
    assert household_id is not None
    return household_id


def _page(number: int, *, lines: list[str] | None = None, text: str = "") -> dict[str, Any]:
    words: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []
    for index, line in enumerate(lines or []):
        y = 40.0 + 20.0 * index
        words.append({"text": line, "bbox": [72.0, y, 72.0 + 6 * len(line), y + 10], "line": index})
        out.append(
            {
                "text": line,
                "bbox": [72.0, y, 72.0 + 6 * len(line), y + 10],
                "first_word": index,
                "word_count": 1,
            }
        )
    return {
        "number": number,
        "width": 612.0,
        "height": 792.0,
        "unit": "pt",
        "text_source": "text_layer",
        "text": text or "\n".join(lines or []),
        "words": words,
        "lines": out,
    }


def _extraction(*, hostile_lines: int = 0) -> dict[str, Any]:
    """A first page with the bank's header, then ``hostile_lines`` long lines."""
    pages = [_page(1, lines=[BANK, "Member Account Summary"])]
    line = ("a " * MAX_SUBJECT_CHARS)[:MAX_SUBJECT_CHARS]
    per_page = 1000
    for start in range(0, hostile_lines, per_page):
        count = min(per_page, hostile_lines - start)
        pages.append(_page(len(pages) + 1, text="\n".join([line] * count)))
    return ExtractResult.model_validate({"kind": "pdf", "ok": True, "pages": pages}).model_dump(
        mode="json"
    )


def _document(
    factories: Factories, store: LocalFSBlobStore, household_id: uuid.UUID, extraction: Any
) -> Document:
    with factories.transaction() as db:
        return make_document(
            db,
            store,
            factories.keyring,
            household_id,
            data=PDF + uuid.uuid4().hex.encode(),
            mime="application/pdf",
            filename="statement.pdf",
            extraction=extraction,
            status=DocumentStatus.NEEDS_REVIEW,
        )


def _item_url(factories: Factories, document: Document) -> str:
    with factories.transaction() as db:
        row = make_review_item(db, document, ReviewKind.UNPARSED, {"reason": "no_records"})
    return f"{REVIEW}/{row.id}/remember-layout"


def test_layout_count_is_capped_and_only_latest_versions_match(
    owner: AuthedClient, factories: Factories
) -> None:
    hh = _hh(owner)
    with factories.transaction() as db:
        bank = make_institution(db, hh, name=BANK)
    with factories.transaction() as db:
        set_context(db, hh)
        for n in range(MAX_HOUSEHOLD_FINGERPRINTS):
            remember_layout(
                db,
                hh,
                institution_id=bank.id,
                kind=DocumentKind.STATEMENT,
                match_rules=RULES,
                parser_name=f"parser_{n}",
            )
        # A new version of a known layout is still allowed and replaces it.
        again = remember_layout(
            db,
            hh,
            institution_id=bank.id,
            kind=DocumentKind.STATEMENT,
            match_rules=RULES,
            parser_name="parser_0",
        )
        assert again.version == 2
        with pytest.raises(FingerprintLimitError):
            remember_layout(
                db,
                hh,
                institution_id=bank.id,
                kind=DocumentKind.STATEMENT,
                match_rules=RULES,
                parser_name="parser_new",
            )
        fps = household_fingerprints(db, hh)
        assert len(fps) == MAX_HOUSEHOLD_FINGERPRINTS
        assert len({fp.spec.parser for fp in fps}) == MAX_HOUSEHOLD_FINGERPRINTS
        (first,) = [fp for fp in fps if fp.spec.parser == "parser_0"]
        assert first.fingerprint_id == again.id
        assert first.spec.version == 2


def test_only_the_newest_layouts_are_loaded_beyond_the_cap(
    owner: AuthedClient, factories: Factories
) -> None:
    # Rows written past the cap (for example before it existed) are not all loaded.
    hh = _hh(owner)
    with factories.transaction() as db:
        bank = make_institution(db, hh, name=BANK)
    with factories.transaction() as db:
        set_context(db, hh)
        for n in range(MAX_HOUSEHOLD_FINGERPRINTS + 20):
            db.add(
                ParserFingerprint(
                    household_id=hh,
                    institution_id=bank.id,
                    kind=DocumentKind.STATEMENT,
                    match_rules=RULES,
                    parser_name=f"parser_{n}",
                    version=1,
                )
            )
            db.flush()
        assert len(household_fingerprints(db, hh)) == MAX_HOUSEHOLD_FINGERPRINTS


def test_remember_layout_beyond_the_cap_is_refused(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = _hh(owner)
    with factories.transaction() as db:
        bank = make_institution(db, hh, name=BANK)
    with factories.transaction() as db:
        set_context(db, hh)
        for n in range(MAX_HOUSEHOLD_FINGERPRINTS):
            db.add(
                ParserFingerprint(
                    household_id=hh,
                    institution_id=bank.id,
                    kind=DocumentKind.RECEIPT,
                    match_rules=RULES,
                    parser_name=f"parser_{n}",
                    version=1,
                )
            )
    url = _item_url(factories, _document(factories, store, hh, _extraction()))
    response = owner.post(url, json={"institution_id": str(bank.id), "kind": "statement"})
    assert response.status_code == 409, response.text
    assert problem_type(response) == "fingerprint-limit"


def test_costly_rules_on_the_learning_document_are_refused_quickly(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = _hh(owner)
    with factories.transaction() as db:
        bank = make_institution(db, hh, name=BANK)
    source = _document(factories, store, hh, _extraction(hostile_lines=4000))
    url = _item_url(factories, source)
    started = time.perf_counter()
    response = owner.post(
        url, json={"institution_id": str(bank.id), "kind": "statement", "match": HOSTILE_MATCH}
    )
    assert time.perf_counter() - started < 10.0
    assert response.status_code == 422, response.text
    assert problem_type(response) == "layout-too-costly"
    with factories.transaction() as db:
        set_context(db, hh)
        assert db.execute(select(ParserFingerprint)).scalars().all() == []


def test_similar_scan_is_bounded_for_costly_rules(
    owner: AuthedClient,
    factories: Factories,
    store: LocalFSBlobStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hh = _hh(owner)
    with factories.transaction() as db:
        bank = make_institution(db, hh, name=BANK)
    source = _document(factories, store, hh, _extraction())
    hostile = [_document(factories, store, hh, _extraction(hostile_lines=4000)) for _ in range(16)]
    url = _item_url(factories, source)
    spent: list[float] = []
    real = fingerprints.match_fingerprint

    def timed(*args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return real(*args, **kwargs)
        finally:
            spent.append(time.perf_counter() - started)

    monkeypatch.setattr(fingerprints, "match_fingerprint", timed)
    started = time.perf_counter()
    response = owner.post(
        url, json={"institution_id": str(bank.id), "kind": "statement", "match": HOSTILE_MATCH}
    )
    elapsed = time.perf_counter() - started
    assert response.status_code == 200, response.text
    # Unbounded, each hostile document costs about a minute; per document
    # budgets alone would still add up (16 x the household budget). The scan
    # shares one budget, so all matching in the request stays within it.
    assert sum(spent) < actions.SIMILAR_SECONDS + 2 * fingerprints.HOUSEHOLD_MATCH_SECONDS + 0.5
    assert elapsed < actions.SIMILAR_SECONDS + 10.0, elapsed
    # None of the hostile documents was evaluated to the end, so none matched.
    assert response.json()["reprocessed"] == [str(source.id)]
    assert {str(d.id) for d in hostile}.isdisjoint(response.json()["reprocessed"])
