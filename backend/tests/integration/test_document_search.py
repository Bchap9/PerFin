# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full-text search over extracted document text (``GET /documents?q=``; SECURITY.md I1, F3).

The worker writes ``document.extracted_text`` from the validated extraction;
the API ranks with ``websearch_to_tsquery`` and keeps the usual visibility
rules. Snippets are plain-text segments and only reach callers who may
download documents; a viewer without that right searches filenames only, so
the search is no oracle over text they may not open.
"""

from __future__ import annotations

import itertools
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.auth.models import GrantLevel
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.models import Setting, SettingScope
from app.db.rls import set_context
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.manage import VIEWER_DOWNLOAD_SETTING
from app.domain.documents.models import Document, DocumentSource, DocumentStatus
from app.domain.documents.search import (
    INDEX_TEXT_MAX,
    _segments,
    index_text,
    update_search_index,
)
from app.ingestion import pipeline
from app.ingestion.intake.upload import receive_stream
from app.ingestion.sandbox.protocol import ExtractOptions
from app.ingestion.sandbox.runner import SandboxConfig
from tests.fixtures import synth_documents as synth
from tests.fixtures.documents import make_document
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, problem_type
from tests.fixtures.ledger import make_account

pytestmark = pytest.mark.integration

DOCS = f"{API}/documents"


def _extraction(text: str, **extra: Any) -> dict[str, Any]:
    return {"format_version": 1, "kind": "pdf", "ok": True, "text": text, **extra}


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
def home(actors: Actors) -> dict[str, AuthedClient]:
    owner = actors.new(role="owner")
    hh = owner.record.household
    return {
        "owner": owner,
        "member": actors.new(role="member", household=hh),
        "viewer": actors.new(role="viewer", household=hh),
    }


def _household(user: AuthedClient) -> uuid.UUID:
    assert user.record.household_id is not None
    return user.record.household_id


def _indexed(
    factories: Factories, store: LocalFSBlobStore, household_id: uuid.UUID, text: str, **kw: Any
) -> uuid.UUID:
    with factories.transaction() as db:
        document = make_document(
            db, store, factories.keyring, household_id, extraction=_extraction(text), **kw
        )
        set_context(db, household_id)
        update_search_index(db, document)
        return document.id


@pytest.fixture
def corpus(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> dict[str, uuid.UUID]:
    """Documents on a shared account (member edit, viewer view) and one on a hidden account."""
    household_id = _household(home["owner"])
    with factories.transaction() as db:
        shared = make_account(db, household_id).id
        hidden = make_account(db, household_id).id
    factories.grant(household_id, home["member"].record.user.id, shared, GrantLevel.EDIT)
    factories.grant(household_id, home["viewer"].record.user.id, shared, GrantLevel.VIEW)
    return {
        "insurance": _indexed(
            factories,
            store,
            household_id,
            "Northwind Mutual home insurance renewal. Premium due 2026-04-01. "
            "Policy number HX-1 covers the house at Elm Street.",
            account_id=shared,
        ),
        "statement": _indexed(
            factories,
            store,
            household_id,
            "Example Savings Bank statement. Insurance premium Northwind 120.00. "
            "Grocery 45.10. Grocery 12.00.",
            account_id=shared,
        ),
        "secret": _indexed(
            factories,
            store,
            household_id,
            "Northwind insurance claim for the hidden account <script>alert(1)</script>",
            account_id=hidden,
        ),
        "plain": _indexed(factories, store, household_id, "Nothing here but groceries"),
    }


def _ids(response: Any) -> list[str]:
    assert response.status_code == 200, response.text
    return [item["id"] for item in response.json()["items"]]


def test_search_ranks_and_respects_visibility(
    home: dict[str, AuthedClient], corpus: dict[str, uuid.UUID]
) -> None:
    owner, member = home["owner"], home["member"]
    found = owner.get(DOCS, params={"q": "northwind insurance"})
    ids = _ids(found)
    assert set(ids) == {str(corpus["insurance"]), str(corpus["statement"]), str(corpus["secret"])}
    ranks = [item["rank"] for item in found.json()["items"]]
    assert ranks == sorted(ranks, reverse=True)
    assert all(r > 0 for r in ranks)
    # The member cannot see the hidden account's document, nor find it.
    assert str(corpus["secret"]) not in _ids(member.get(DOCS, params={"q": "northwind"}))
    # websearch syntax: phrases, exclusion, "or"; never a syntax error.
    assert _ids(owner.get(DOCS, params={"q": '"home insurance"'})) == [str(corpus["insurance"])]
    assert set(_ids(owner.get(DOCS, params={"q": "northwind -claim -bank"}))) == {
        str(corpus["insurance"])
    }
    assert set(_ids(owner.get(DOCS, params={"q": "elm or grocery"}))) == {
        str(corpus["insurance"]),
        str(corpus["statement"]),
    }
    for odd in ("'", '"', "&|!", "():*", "-", "a" * 200, "1 ' OR 1=1 --"):
        assert owner.get(DOCS, params={"q": odd}).status_code == 200
    assert owner.get(DOCS, params={"q": "a" * 201}).status_code == 422
    # Filters still apply with q.
    assert _ids(owner.get(DOCS, params={"q": "northwind", "status": "failed"})) == []


def test_snippets_are_plain_segments_for_members_only(
    home: dict[str, AuthedClient], corpus: dict[str, uuid.UUID], factories: Factories
) -> None:
    owner_items = home["owner"].get(DOCS, params={"q": "claim"}).json()["items"]
    (secret,) = owner_items
    snippet = secret["snippet"]
    assert any(part["match"] and part["text"].lower() == "claim" for part in snippet)
    joined = "".join(part["text"] for part in snippet)
    assert "Northwind insurance claim" in joined
    # Plain text: whatever markup the document holds stays text, nothing added.
    assert "<b>" not in joined
    assert all(set(part) == {"text", "match"} for part in snippet)
    member_items = home["member"].get(DOCS, params={"q": "premium"}).json()["items"]
    assert {i["id"] for i in member_items} == {str(corpus["insurance"]), str(corpus["statement"])}
    assert all(i["snippet"] for i in member_items)
    # A viewer who may not download never searches the extracted text (I1):
    # no token of a document they cannot open is confirmed or denied ...
    for guess in ("premium", "northwind", "HX-1", "120.00", "grocery", '"home insurance"'):
        assert _ids(home["viewer"].get(DOCS, params={"q": guess})) == []
    # ... their query matches the filename they already see, without text.
    viewer_items = home["viewer"].get(DOCS, params={"q": "statement"}).json()["items"]
    assert {i["id"] for i in viewer_items} == {str(corpus["insurance"]), str(corpus["statement"])}
    assert all(i["snippet"] is None for i in viewer_items)
    assert all(i["rank"] is not None for i in viewer_items)
    # ... unless the household lets viewers download documents.
    household_id = _household(home["owner"])
    with factories.transaction() as db:
        set_context(db, household_id)
        db.add(
            Setting(
                scope=SettingScope.HOUSEHOLD,
                scope_id=household_id,
                key=VIEWER_DOWNLOAD_SETTING,
                value=True,
            )
        )
    allowed = home["viewer"].get(DOCS, params={"q": "premium"}).json()["items"]
    assert all(i["snippet"] for i in allowed)
    # Without q there is neither rank nor snippet.
    plain = home["owner"].get(DOCS).json()["items"]
    assert all(i["rank"] is None and i["snippet"] is None for i in plain)


def test_search_pages_with_a_stable_cursor(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    household_id = _household(home["owner"])
    for n in range(7):
        _indexed(factories, store, household_id, "invoice " * (n + 1) + f"number {n}")
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"q": "invoice", "limit": 3}
        if cursor:
            params["cursor"] = cursor
        page = home["owner"].get(DOCS, params=params).json()
        seen.extend(item["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert len(seen) == len(set(seen)) == 7
    for bad in ("x", "12", "12.not-a-uuid", f"-1.{uuid.uuid4()}", "9" * 30 + f".{uuid.uuid4()}"):
        response = home["owner"].get(DOCS, params={"q": "invoice", "cursor": bad})
        assert (response.status_code, problem_type(response)) == (422, "invalid-cursor")
    # The plain list keeps its id cursor and refuses a search cursor.
    first = home["owner"].get(DOCS, params={"limit": 2}).json()
    assert first["next_cursor"] is not None
    uuid.UUID(first["next_cursor"])
    response = home["owner"].get(DOCS, params={"cursor": f"5.{uuid.uuid4()}"})
    assert (response.status_code, problem_type(response)) == (422, "invalid-cursor")


def test_other_households_never_match(
    home: dict[str, AuthedClient],
    actors: Actors,
    corpus: dict[str, uuid.UUID],
) -> None:
    stranger = actors.new(role="owner")
    assert _ids(stranger.get(DOCS, params={"q": "northwind"})) == []


def test_index_text_and_segments() -> None:
    assert index_text(None) == ""
    assert index_text({"ok": False, "text": "x"}) == ""
    text = index_text(
        {
            "ok": True,
            "text": "",
            "pages": [{"text": "page one"}, {"text": "page two"}],
            "email": {"headers": {"subject": "Your receipt"}},
            "tables": [{"rows": [["Date", "Payee"], ["2026-01-01", "Corner Grocer"]]}],
        }
    )
    assert "Your receipt" in text
    assert "page one" in text
    assert "Corner Grocer" in text
    assert "\x00" not in index_text({"ok": True, "text": "a\x00b"})
    assert len(index_text({"ok": True, "text": "x" * (INDEX_TEXT_MAX + 10)})) == INDEX_TEXT_MAX
    parts = _segments("before hit aftersecond hit\x07")
    assert parts == [
        {"text": "before ", "match": False},
        {"text": "hit", "match": True},
        {"text": " after … second ", "match": False},
        {"text": "hit", "match": True},
        {"text": " ", "match": False},
    ]
    assert _segments("no match at all") == []


def test_oversized_text_is_indexed_from_a_prefix(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    household_id = _household(home["owner"])
    # Distinct four-character CJK words (12 bytes each): about 1.6 MB of
    # lexemes, beyond PostgreSQL's 1 MB tsvector limit.
    alphabet = [chr(0x4E00 + i) for i in range(60)]
    words = " ".join(
        "".join(chars) for chars in itertools.islice(itertools.product(alphabet, repeat=4), 99_990)
    )
    text = "needle " + words
    with factories.transaction() as db:
        document = make_document(
            db, store, factories.keyring, household_id, extraction=_extraction(text)
        )
        set_context(db, household_id)
        indexed = update_search_index(db, document)
        document_id = document.id
    assert 0 < indexed < len(text)
    with factories.transaction() as db:
        set_context(db, household_id)
        size = db.execute(
            select(func.length(Document.extracted_text)).where(Document.id == document_id)
        ).scalar_one()
    assert size is not None
    assert size > 0
    assert _ids(home["owner"].get(DOCS, params={"q": "needle"})) == [str(document_id)]


def test_pipeline_indexes_extracted_text(
    factories: Factories, settings: Settings, tmp_path: Path
) -> None:
    store = LocalFSBlobStore(tmp_path / "documents", tmp_path / "quarantine")
    keyring = Keyring.from_settings(settings)
    owner = factories.user(role="owner")
    assert owner.household_id is not None
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        document_id = receive_stream(
            db,
            store=store,
            keyring=keyring,
            household_id=owner.household_id,
            chunks=(synth.statement_pdf(title="Fabrikam Credit Union"),),
            max_bytes=20 * 1024 * 1024,
            source=DocumentSource.UPLOAD,
            original_filename="statement.pdf",
            uploaded_by=owner.user.id,
        ).document_id
    deps = pipeline.PipelineDeps(
        store=store,
        keyring=keyring,
        sandbox=SandboxConfig(timeout_seconds=90),
        options=ExtractOptions(previews=False),
    )
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        status = pipeline.ingest_document(db, owner.household_id, document_id, deps)
    assert status in (DocumentStatus.NEEDS_REVIEW, DocumentStatus.PROCESSED)
    with factories.transaction() as db:
        set_context(db, owner.household_id)
        hit = db.execute(
            select(Document.id).where(
                Document.extracted_text.op("@@")(
                    func.websearch_to_tsquery("simple", "fabrikam credit")
                )
            )
        ).scalar_one_or_none()
    assert hit == document_id
