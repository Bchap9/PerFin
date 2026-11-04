# SPDX-License-Identifier: AGPL-3.0-or-later
"""Review tools: page previews, word regions, item enrichment, corrections,
"remember this layout", rule drafts and PDF table mappings
(API.md "Review notes"/"Documents notes"; INGESTION.md "Review queue";
SECURITY.md C5, C12, F3, I1).

Documents carry a synthetic stored extraction built here (pages, words,
layout tables, parser rows with boxes); previews are real plain PNGs drawn
by Pillow and stored as encrypted blobs exactly as the worker stores them.
Nothing here is real financial data.
"""

from __future__ import annotations

import io
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from PIL import Image
from sqlalchemy import select

from app.auth.models import GrantLevel
from app.core.ids import uuid7
from app.core.settings import Settings
from app.db.models import Job, Setting, SettingScope
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.documents import previews
from app.domain.documents.blobstore import EncryptingWriter, LocalFSBlobStore
from app.domain.documents.manage import VIEWER_DOWNLOAD_SETTING
from app.domain.documents.models import (
    BlobArea,
    Document,
    DocumentStatus,
    ParserFingerprint,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE
from app.domain.imports.service import import_extraction
from app.domain.review.layout import derive_match, layout_import_tables, stored_result
from app.domain.transactions.models import ReviewState, Transaction
from app.ingestion.classify.fingerprints import household_fingerprints
from app.ingestion.classify.service import classify_extraction
from app.ingestion.extract.render import plain_png
from app.ingestion.sandbox.protocol import ExtractResult, Preview
from tests.fixtures.documents import make_document, make_review_item
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, household_audit, problem_type
from tests.fixtures.imports import run_jobs
from tests.fixtures.ledger import (
    household_context,
    make_account,
    make_institution,
    make_transaction,
)

pytestmark = pytest.mark.integration

DOCS = f"{API}/documents"
REVIEW = f"{API}/review"
PDF = b"%PDF-1.7\n% synthetic placeholder, never parsed by the API\n%%EOF\n"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


def hh_of(user: AuthedClient) -> uuid.UUID:
    household_id = user.record.household_id
    assert household_id is not None
    return household_id


def shared_account(factories: Factories, home: dict[str, AuthedClient], **fields: Any) -> Account:
    hh = hh_of(home["owner"])
    with factories.transaction() as db:
        account = make_account(db, hh, **fields)
    factories.grant(hh, home["member"].user.id, account.id, GrantLevel.EDIT)
    factories.grant(hh, home["viewer"].user.id, account.id, GrantLevel.VIEW)
    return account


def load(factories: Factories, household_id: uuid.UUID, model: Any, row_id: uuid.UUID) -> Any:
    with factories.transaction() as db:
        set_context(db, household_id)
        row = db.get(model, row_id)
        if row is not None:
            db.expunge(row)
        return row


def rows_of(factories: Factories, household_id: uuid.UUID, query: Any) -> list[Any]:
    with factories.transaction() as db:
        set_context(db, household_id)
        rows = list(db.execute(query).scalars())
        db.expunge_all()
        return rows


def allow_viewer_downloads(factories: Factories, household_id: uuid.UUID) -> None:
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


# ---------------------------------------------------------------------------
# Synthetic extractions
# ---------------------------------------------------------------------------

Line = tuple[float, list[tuple[float, str]]]


def page(
    number: int, lines: list[Line], tables: list[list[list[str]]] | None = None
) -> dict[str, Any]:
    """A page in points: each line is ``(y, [(x, word), ...])``."""
    words: list[dict[str, Any]] = []
    out_lines: list[dict[str, Any]] = []
    for index, (y, cells) in enumerate(lines):
        first = len(words)
        for x, text in cells:
            words.append(
                {"text": text, "bbox": [x, y, x + 6.0 * len(text), y + 10.0], "line": index}
            )
        x0 = cells[0][0]
        x1 = cells[-1][0] + 6.0 * len(cells[-1][1])
        out_lines.append(
            {
                "text": " ".join(t for _, t in cells),
                "bbox": [x0, y, x1, y + 10.0],
                "first_word": first,
                "word_count": len(cells),
            }
        )
    return {
        "number": number,
        "width": 612.0,
        "height": 792.0,
        "unit": "pt",
        "text_source": "text_layer",
        "text": "\n".join(line["text"] for line in out_lines),
        "words": words,
        "lines": out_lines,
        "tables": [
            {
                "source": "columns",
                "bbox": [72.0, 200.0, 540.0, 600.0],
                "rows": rows,
                "column_count": max(len(r) for r in rows),
            }
            for rows in tables or []
        ],
    }


HEADER: list[Line] = [
    (40.0, [(72.0, "Fictional"), (130.0, "Thrift"), (170.0, "Bank")]),
    (60.0, [(72.0, "Member"), (120.0, "Account"), (170.0, "Summary")]),
    (80.0, [(72.0, "Account"), (120.0, "****4821")]),
]
TABLE = [
    ["Date", "Details", "Amount"],
    ["2026-03-02", "Corner Bakery", "-4.50"],
    ["2026-03-05", "Payroll Deposit", "1200.00"],
]


def statement_extraction(
    *, last4: str = "4821", rows: list[tuple[date, str, str]] | None = None
) -> dict[str, Any]:
    """A parsed PDF statement: rows with page and box, words on their lines."""
    rows = rows or [(date(2026, 3, 2), "-4.50", "Corner Bakery")]
    lines = list(HEADER)
    transactions = []
    for index, (posted, amount, payee) in enumerate(rows):
        y = 220.0 + 14.0 * index
        lines.append(
            (y, [(72.0, posted.isoformat()), (150.0, payee), (440.0, amount), (500.0, "-4.05")])
        )
        transactions.append(
            {
                "posted": posted.isoformat(),
                "amount": amount,
                "payee": payee,
                "confidence": 0.6,
                "low_confidence": ["amount"],
                "page": 1,
                "bbox": [72.0, y, 540.0, y + 10.0],
            }
        )
    return ExtractResult.model_validate(
        {
            "kind": "pdf",
            "ok": True,
            "pages": [page(1, lines)],
            "statements": [
                {
                    "source_format": "pdf",
                    "account": {"last4": last4, "currency": "USD"},
                    "transactions": transactions,
                }
            ],
        }
    ).model_dump(mode="json")


def table_extraction(header: list[Line] = HEADER, table: list[list[str]] = TABLE) -> dict[str, Any]:
    """A PDF no parser read: header lines and a page table."""
    return ExtractResult.model_validate(
        {"kind": "pdf", "ok": True, "pages": [page(1, header, [table])]}
    ).model_dump(mode="json")


def png_bytes(width: int = 40, height: int = 52) -> bytes:
    return plain_png(Image.new("L", (width, height), color=200))


def pdf_document(
    factories: Factories,
    store: LocalFSBlobStore,
    household_id: uuid.UUID,
    *,
    extraction: dict[str, Any],
    with_preview: bool = False,
    **kwargs: Any,
) -> Document:
    kwargs.setdefault("status", DocumentStatus.NEEDS_REVIEW)
    with factories.transaction() as db:
        doc = make_document(
            db,
            store,
            factories.keyring,
            household_id,
            data=PDF + uuid.uuid4().hex.encode(),
            mime="application/pdf",
            filename="statement.pdf",
            extraction=extraction,
            **kwargs,
        )
        if with_preview:
            with household_context(db, household_id):
                stored = previews.store_previews(
                    db,
                    store,
                    factories.keyring,
                    household_id=household_id,
                    previews=[
                        Preview(page=1, width=40, height=52, dpi=100, scale=1.389, png=png_bytes())
                    ],
                )
                doc.extraction = {
                    **extraction,
                    "stored_previews": [s.model_dump(mode="json") for s in stored],
                }
                db.flush()
        return doc


def item(
    factories: Factories, doc: Document, kind: ReviewKind, payload: dict[str, Any]
) -> ReviewItem:
    with factories.transaction() as db:
        return make_review_item(db, doc, kind, payload)


# ---------------------------------------------------------------------------
# GET /documents/{id}/preview
# ---------------------------------------------------------------------------


def test_preview_is_a_sandboxed_png_for_members_only(
    home: dict[str, AuthedClient],
    factories: Factories,
    store: LocalFSBlobStore,
    actors: Actors,
    doc_settings: Settings,
) -> None:
    owner, member, viewer = home["owner"], home["member"], home["viewer"]
    hh = hh_of(owner)
    account = shared_account(factories, home)
    doc = pdf_document(
        factories,
        store,
        hh,
        extraction=statement_extraction(),
        with_preview=True,
        account_id=account.id,
    )
    response = member.get(f"{DOCS}/{doc.id}/preview", params={"page": 1})
    assert response.status_code == 200, response.text
    assert response.content == png_bytes()
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"].startswith("sandbox")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == "inline"
    # The stored blob is ciphertext.
    stored = ExtractResult.model_validate(load(factories, hh, Document, doc.id).extraction)
    ref = stored.stored_previews[0]
    files = [p for p in doc_settings.documents_dir.rglob("*") if p.is_file()]
    assert files
    assert all(png_bytes() not in p.read_bytes() for p in files)
    assert ref.page == 1
    audits = household_audit(factories, hh, "document.preview")
    assert [(a.after or {})["page"] for a in audits] == [1]

    # I1: a viewer only where the household allows downloads.
    refused = viewer.get(f"{DOCS}/{doc.id}/preview")
    assert refused.status_code == 403
    assert problem_type(refused) == "forbidden"
    allow_viewer_downloads(factories, hh)
    assert viewer.get(f"{DOCS}/{doc.id}/preview").status_code == 200

    missing = member.get(f"{DOCS}/{doc.id}/preview", params={"page": 2})
    assert missing.status_code == 404
    assert problem_type(missing) == "preview-not-found"
    assert member.get(f"{DOCS}/{doc.id}/preview", params={"page": 0}).status_code == 422
    # F3: another household's caller cannot tell the document exists.
    outsider = actors.new(role="owner")
    hidden = outsider.get(f"{DOCS}/{doc.id}/preview")
    assert hidden.status_code == 404
    assert problem_type(hidden) == "document-not-found"


def test_preview_is_never_served_for_quarantined_documents(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    doc = pdf_document(
        factories,
        store,
        hh,
        extraction=statement_extraction(),
        with_preview=True,
        area=BlobArea.QUARANTINE,
        status=DocumentStatus.REJECTED,
    )
    for path in ("preview", "regions"):
        response = owner.get(f"{DOCS}/{doc.id}/{path}")
        assert response.status_code == 409, path
        assert problem_type(response) == "document-quarantined"


def test_preview_refuses_a_stored_png_with_metadata_chunks(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    """C5: the API checks the decrypted PNG again; a text chunk is never served."""
    owner = home["owner"]
    hh = hh_of(owner)
    doc = pdf_document(factories, store, hh, extraction=statement_extraction(), with_preview=True)
    image = Image.new("L", (8, 8), color=0)
    from PIL import PngImagePlugin

    info = PngImagePlugin.PngInfo()
    info.add_text("Comment", "<script>alert(1)</script>")
    out = io.BytesIO()
    image.save(out, format="PNG", pnginfo=info)
    tampered = out.getvalue()
    # Point the page at a tampered PNG blob (as a worker bug would).
    with factories.transaction() as db, household_context(db, hh):
        with EncryptingWriter(store, factories.keyring, uuid7(), area=BlobArea.PERMANENT) as w:
            w.write(tampered)
            sealed = w.finish()
        db.add(previews._blob_row(hh, sealed))
        db.flush()
        row = db.get(Document, doc.id)
        assert row is not None
        extraction = dict(row.extraction or {})
        extraction["stored_previews"] = [
            {**extraction["stored_previews"][0], "blob_id": str(sealed.blob_id)}
        ]
        row.extraction = extraction
        db.flush()
    response = owner.get(f"{DOCS}/{doc.id}/preview")
    assert response.status_code == 500
    assert problem_type(response) == "document-unreadable"
    assert b"<script>" not in response.content


# ---------------------------------------------------------------------------
# GET /documents/{id}/regions
# ---------------------------------------------------------------------------


def test_regions_give_words_to_members_only(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore, actors: Actors
) -> None:
    owner, member, viewer = home["owner"], home["member"], home["viewer"]
    hh = hh_of(owner)
    account = shared_account(factories, home)
    doc = pdf_document(
        factories,
        store,
        hh,
        extraction=statement_extraction(),
        with_preview=True,
        account_id=account.id,
    )
    seen = member.get(f"{DOCS}/{doc.id}/regions", params={"page": 1})
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert body["text_included"] is True
    assert body["page_count"] == 1
    assert body["unit"] == "pt"
    assert body["preview"]["scale"] == pytest.approx(1.389)
    assert "Fictional" in [w["text"] for w in body["words"]]
    # A viewer who may not download gets the page geometry but no words: not
    # their text, and not their boxes either (P34-SR-04: word widths per line
    # would show e.g. how many digits each figure has).
    boxes = viewer.get(f"{DOCS}/{doc.id}/regions").json()
    assert boxes["text_included"] is False
    assert boxes["words"] == []
    assert (boxes["page_count"], boxes["width"], boxes["height"]) == (
        body["page_count"],
        body["width"],
        body["height"],
    )
    missing = member.get(f"{DOCS}/{doc.id}/regions", params={"page": 3})
    assert missing.status_code == 404
    assert problem_type(missing) == "page-not-found"
    assert actors.new(role="owner").get(f"{DOCS}/{doc.id}/regions").status_code == 404
    # A document of an account the member cannot see is hidden.
    with factories.transaction() as db:
        private = make_account(db, hh)
    other = pdf_document(
        factories, store, hh, extraction=statement_extraction(), account_id=private.id
    )
    assert member.get(f"{DOCS}/{other.id}/regions").status_code == 404


# ---------------------------------------------------------------------------
# Item enrichment and low_confidence_field resolution
# ---------------------------------------------------------------------------


def low_confidence_setup(
    factories: Factories, store: LocalFSBlobStore, home: dict[str, AuthedClient]
) -> tuple[Account, Document, Transaction, ReviewItem]:
    hh = hh_of(home["owner"])
    account = shared_account(factories, home, number_last4="4821")
    doc = pdf_document(
        factories, store, hh, extraction=statement_extraction(), account_id=account.id
    )
    with factories.transaction() as db:
        row = make_transaction(
            db,
            account,
            amount="-4.50",
            posted_date=date(2026, 3, 2),
            raw_payee="Corner Bakery",
            review_state=ReviewState.PENDING,
            source_document_id=doc.id,
        )
    payload = {
        "reason": "low_confidence",
        "fields": ["amount"],
        "account_id": str(account.id),
        "count": 1,
        "transactions": [
            {"transaction_id": str(row.id), "fields": ["amount"], "confidence": 0.6, "page": 1}
        ],
    }
    return account, doc, row, item(factories, doc, ReviewKind.LOW_CONFIDENCE_FIELD, payload)


def test_item_detail_has_regions_and_alternatives(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    member, viewer = home["member"], home["viewer"]
    _, _, row, low = low_confidence_setup(factories, store, home)
    body = member.get(f"{REVIEW}/{low.id}").json()
    assert body["next_step"] == "correct_fields"
    assert body["regions"] == [
        {
            "page": 1,
            "bbox": [72.0, 220.0, 540.0, 230.0],
            "label": "transaction",
            "transaction_id": str(row.id),
            "fields": ["amount"],
        }
    ]
    amounts = [a["value"] for a in body["alternatives"] if a["field"] == "amount"]
    assert amounts == ["-4.50", "-4.05"]
    dates = [a["value"] for a in body["alternatives"] if a["field"] == "posted_date"]
    assert dates == []  # only the doubtful field is offered
    # Viewers see where, not what the document says (I1).
    seen = viewer.get(f"{REVIEW}/{low.id}").json()
    assert seen["regions"] == body["regions"]
    assert seen["alternatives"] == []
    # The list stays light.
    listed = member.get(REVIEW).json()["items"]
    assert listed[0]["regions"] is None
    assert listed[0]["alternatives"] is None


def test_unknown_account_alternatives_rank_the_matching_account(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    with factories.transaction() as db:
        make_account(db, hh, name="Aaa other", currency="EUR")
        match = make_account(db, hh, name="Zzz match", number_last4="4821")
    doc = pdf_document(factories, store, hh, extraction=statement_extraction())
    row = item(
        factories,
        doc,
        ReviewKind.UNKNOWN_ACCOUNT,
        {"reason": "account_not_found", "statement": 0, "currency": "USD"},
    )
    body = owner.get(f"{REVIEW}/{row.id}").json()
    assert body["alternatives"][0]["value"] == str(match.id)
    assert body["alternatives"][0]["score"] == pytest.approx(0.75)
    assert [r["label"] for r in body["regions"]] == ["account_number"]


def test_low_confidence_resolution_corrects_and_confirms(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member, viewer = home["owner"], home["member"], home["viewer"]
    hh = hh_of(owner)
    account, doc, row, low = low_confidence_setup(factories, store, home)
    assert (
        viewer.post(f"{REVIEW}/{low.id}/resolve", json={"kind": "low_confidence_field"}).status_code
        == 403
    )
    # A row of another document cannot be changed through this item.
    with factories.transaction() as db:
        stray = make_transaction(db, account, amount="-1.00", review_state=ReviewState.PENDING)
    refused = member.post(
        f"{REVIEW}/{low.id}/resolve",
        json={
            "kind": "low_confidence_field",
            "transactions": [
                {"transaction_id": str(stray.id), "amount": {"amount": "-2.00", "currency": "USD"}}
            ],
        },
    )
    assert refused.status_code == 422, refused.text
    assert problem_type(refused) == "invalid-resolution"
    response = member.post(
        f"{REVIEW}/{low.id}/resolve",
        json={
            "kind": "low_confidence_field",
            "transactions": [
                {"transaction_id": str(row.id), "amount": {"amount": "-4.05", "currency": "USD"}}
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "resolved"
    assert response.json()["resolution"]["confirmed"] == 1
    fixed = load(factories, hh, Transaction, row.id)
    assert fixed.amount == Decimal("-4.05")
    assert fixed.review_state is ReviewState.CONFIRMED
    assert load(factories, hh, Transaction, stray.id).review_state is ReviewState.PENDING
    assert household_audit(factories, hh, "transaction.update")
    assert load(factories, hh, Document, doc.id).status is DocumentStatus.PROCESSED


def test_reconciliation_confirmation_covers_the_whole_statement(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    account = shared_account(factories, home)
    doc = pdf_document(
        factories, store, hh, extraction=statement_extraction(), account_id=account.id
    )
    with factories.transaction() as db:
        mine = [
            make_transaction(
                db,
                account,
                amount=f"-{n}.00",
                review_state=ReviewState.PENDING,
                source_document_id=doc.id,
            )
            for n in (1, 2)
        ]
        other = make_transaction(db, account, amount="-3.00", review_state=ReviewState.PENDING)
    row = item(
        factories,
        doc,
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {
            "reason": "reconciliation_failed",
            "field": "reconciliation",
            "account_id": str(account.id),
        },
    )
    keep = owner.post(
        f"{REVIEW}/{row.id}/resolve", json={"kind": "low_confidence_field", "confirm": False}
    )
    assert keep.status_code == 200
    assert {load(factories, hh, Transaction, t.id).review_state for t in mine} == {
        ReviewState.PENDING
    }
    row2 = item(
        factories,
        doc,
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {
            "reason": "reconciliation_failed",
            "field": "reconciliation",
            "account_id": str(account.id),
        },
    )
    done = owner.post(f"{REVIEW}/{row2.id}/resolve", json={"kind": "low_confidence_field"})
    assert done.status_code == 200, done.text
    assert done.json()["resolution"]["confirmed"] == 2
    assert {load(factories, hh, Transaction, t.id).review_state for t in mine} == {
        ReviewState.CONFIRMED
    }
    assert load(factories, hh, Transaction, other.id).review_state is ReviewState.PENDING


# ---------------------------------------------------------------------------
# POST /review/{id}/correct
# ---------------------------------------------------------------------------


def test_correct_fixes_document_facts_and_can_resolve(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore, actors: Actors
) -> None:
    owner, member, viewer = home["owner"], home["member"], home["viewer"]
    hh = hh_of(owner)
    account = shared_account(factories, home)
    doc = pdf_document(
        factories, store, hh, extraction=statement_extraction(), account_id=account.id
    )
    with factories.transaction() as db:
        institution = make_institution(db, hh, name="Fictional Thrift Bank")
    row = item(factories, doc, ReviewKind.UNPARSED, {"reason": "import_pending"})
    url = f"{REVIEW}/{row.id}/correct"
    assert viewer.post(url, json={}).status_code == 403
    foreign = actors.new(role="owner")
    with factories.transaction() as db:
        elsewhere = make_institution(db, hh_of(foreign))
    missing = member.post(url, json={"document": {"institution_id": str(elsewhere.id)}})
    assert missing.status_code == 404
    assert problem_type(missing) == "institution-not-found"
    backwards = member.post(
        url, json={"document": {"period_start": "2026-03-31", "period_end": "2026-03-01"}}
    )
    assert backwards.status_code == 422
    response = member.post(
        url,
        json={
            "document": {
                "institution_id": str(institution.id),
                "period_start": "2026-03-01",
                "period_end": "2026-03-31",
                "kind": "statement",
                "tax_year": 2026,
            }
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "open"
    corrected = load(factories, hh, Document, doc.id)
    assert corrected.institution_id == institution.id
    assert (corrected.period_start, corrected.period_end) == (date(2026, 3, 1), date(2026, 3, 31))
    assert corrected.tax_year == 2026
    (entry,) = household_audit(factories, hh, "document.correct")
    assert set(entry.after or {}) == {"institution_id", "period_start", "period_end", "tax_year"}
    closed = member.post(url, json={"document": {"tax_year": None}, "resolve": True})
    assert closed.status_code == 200
    assert closed.json()["state"] == "resolved"
    assert closed.json()["resolution"]["document_fields"] == ["tax_year"]
    assert load(factories, hh, Document, doc.id).tax_year is None
    again = member.post(url, json={})
    assert again.status_code == 409
    assert problem_type(again) == "review-item-closed"


# ---------------------------------------------------------------------------
# POST /review/{id}/remember-layout
# ---------------------------------------------------------------------------


def test_remember_layout_stores_a_validated_fingerprint_and_reprocesses_similar(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member, viewer = home["owner"], home["member"], home["viewer"]
    hh = hh_of(owner)
    account = shared_account(factories, home)
    with factories.transaction() as db:
        institution = make_institution(db, hh, name="Fictional Thrift Bank")
    source = pdf_document(
        factories, store, hh, extraction=table_extraction(), account_id=account.id
    )
    similar = pdf_document(
        factories, store, hh, extraction=table_extraction(), account_id=account.id
    )
    unrelated = pdf_document(
        factories,
        store,
        hh,
        extraction=table_extraction(
            header=[(40.0, [(72.0, "Another"), (130.0, "Lender")])],
        ),
        account_id=account.id,
    )
    with factories.transaction() as db:
        hidden_account = make_account(db, hh)
    hidden = pdf_document(
        factories, store, hh, extraction=table_extraction(), account_id=hidden_account.id
    )
    row = item(factories, source, ReviewKind.UNPARSED, {"reason": "no_records"})
    url = f"{REVIEW}/{row.id}/remember-layout"
    body = {"institution_id": str(institution.id), "kind": "statement"}
    assert viewer.post(url, json=body).status_code == 403
    # Rules must be safe and must recognise the document they come from.
    unsafe = member.post(url, json={**body, "match": {"all": [{"regex": "(a+)+$"}]}})
    assert unsafe.status_code == 422
    assert "unsafe regex" in unsafe.text
    alien = member.post(url, json={**body, "match": {"all": [{"header": "Some Other Bank"}]}})
    assert alien.status_code == 422
    assert problem_type(alien) == "layout-does-not-match"
    unknown = member.post(url, json={**body, "parser": "no_such_parser"})
    assert unknown.status_code == 422
    assert problem_type(unknown) == "parser-unknown"
    assert rows_of(factories, hh, select(ParserFingerprint)) == []

    response = member.post(url, json=body)
    assert response.status_code == 200, response.text
    out = response.json()
    assert out["parser"] == "statement_generic"
    assert out["version"] == 1
    assert out["ambiguous"] is False
    assert out["match"]["all"] == [{"header": "Fictional Thrift Bank"}]
    assert {"header": "Member Account Summary"} in out["match"]["any"]
    assert {"columns": ["date", "details", "amount"]} in out["match"]["any"]
    # Digits never become layout (the masked account number line is skipped).
    assert "4821" not in str(out["match"])
    assert out["item"]["state"] == "resolved"
    assert set(out["reprocessed"]) == {str(source.id), str(similar.id)}
    (fingerprint,) = rows_of(factories, hh, select(ParserFingerprint))
    assert fingerprint.learned_from_document_id == source.id
    assert fingerprint.institution_id == institution.id
    for doc, status in (
        (source, DocumentStatus.RECEIVED),
        (similar, DocumentStatus.RECEIVED),
        (unrelated, DocumentStatus.NEEDS_REVIEW),
        (hidden, DocumentStatus.NEEDS_REVIEW),
    ):
        assert load(factories, hh, Document, doc.id).status is status
    jobs = rows_of(factories, hh, select(Job).where(Job.type == INGEST_JOB_TYPE))
    assert {j.payload["document_id"] for j in jobs} == {str(source.id), str(similar.id)}
    assert household_audit(factories, hh, "parser_fingerprint.create")

    # The classifier now recognises the layout as the household's institution.
    with factories.transaction() as db:
        set_context(db, hh)
        own = household_fingerprints(db, hh)
    result = ExtractResult.model_validate(table_extraction())
    found = classify_extraction(result, fingerprints=own)
    assert found.fingerprint is not None
    assert found.fingerprint.origin == "household"
    assert found.fingerprint.institution_id == institution.id
    assert found.kind == "statement"


def test_remember_layout_needs_something_distinctive(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    with factories.transaction() as db:
        institution = make_institution(db, hh)
    # Only dates and numbers in the header: a column layout alone is too
    # common to name an institution.
    no_layout = table_extraction(header=[(40.0, [(72.0, "2026-03-01"), (150.0, "12345")])])
    assert derive_match(ExtractResult.model_validate(no_layout)) is None
    doc = pdf_document(factories, store, hh, extraction=no_layout)
    row = item(factories, doc, ReviewKind.UNPARSED, {"reason": "no_records"})
    response = owner.post(
        f"{REVIEW}/{row.id}/remember-layout",
        json={"institution_id": str(institution.id), "kind": "statement"},
    )
    assert response.status_code == 422
    assert problem_type(response) == "layout-not-distinctive"


# ---------------------------------------------------------------------------
# GET /review/{id}/rule-draft
# ---------------------------------------------------------------------------


def test_rule_draft_is_prefilled_and_accepted_by_the_rules_api(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    _, _, row, low = low_confidence_setup(factories, store, home)
    assert member.get(f"{REVIEW}/{low.id}/rule-draft").status_code == 403
    response = owner.get(f"{REVIEW}/{low.id}/rule-draft")
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft["transaction_id"] == str(row.id)
    rule = draft["rule"]
    assert rule["conditions"]["type"] == "all"
    assert {"type": "payee", "op": "equals", "value": "Corner Bakery"} in rule["conditions"][
        "conditions"
    ]
    assert rule["actions"] == [{"type": "flag_for_review"}]
    created = owner.post(f"{API}/rules", json=rule)
    assert created.status_code == 201, created.text
    hh = hh_of(owner)
    doc = pdf_document(factories, store, hh, extraction=statement_extraction())
    empty = item(factories, doc, ReviewKind.UNPARSED, {"reason": "no_records"})
    nothing = owner.get(f"{REVIEW}/{empty.id}/rule-draft")
    assert nothing.status_code == 409
    assert problem_type(nothing) == "rule-draft-unavailable"


# ---------------------------------------------------------------------------
# PDF tables through the Phase 2 column mapping
# ---------------------------------------------------------------------------


def test_layout_tables_join_pages_and_neutralise_formulas() -> None:
    first = page(1, HEADER, [TABLE])
    second = page(
        2, [], [[["Date", "Details", "Amount"], ["2026-03-09", "=HYPERLINK(1)", "-2.00"]]]
    )
    result = ExtractResult.model_validate({"kind": "pdf", "ok": True, "pages": [first, second]})
    (table,) = layout_import_tables(result.pages)
    assert table.rows[0] == ["Date", "Details", "Amount"]
    assert len(table.rows) == 4  # one header, three rows; the repeated header dropped
    assert not table.rows[3][1].startswith("=")


def test_pdf_table_mapping_is_reused_for_the_next_document(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    account = shared_account(factories, home)
    doc = pdf_document(factories, store, hh, extraction=table_extraction(), account_id=account.id)
    preview = owner.get(f"{API}/imports/{doc.id}/preview")
    assert preview.status_code == 200, preview.text
    (table,) = preview.json()["tables"]
    assert table["rows"][0] == ["Date", "Details", "Amount"]
    spec = {
        "date_column": 0,
        "date_format": "YYYY-MM-DD",
        "amount_column": 2,
        "sign": "inflow_positive",
        "payee_column": 1,
        "has_header": True,
    }
    created = owner.post(
        f"{API}/imports/mappings",
        json={
            "name": "Fictional Thrift PDF",
            "spec": spec,
            "document_id": str(doc.id),
            "account_id": str(account.id),
        },
    )
    assert created.status_code == 201, created.text
    mapping = created.json()
    assert mapping["header_fingerprint"] == table["header_fingerprint"]
    applied = owner.post(
        f"{API}/imports/{doc.id}/apply-mapping", json={"mapping_id": mapping["id"]}
    )
    assert applied.status_code == 202, applied.text
    assert run_jobs(factories, hh, APPLY_MAPPING_JOB_TYPE, done=set())
    rows = rows_of(
        factories, hh, select(Transaction).where(Transaction.source_document_id == doc.id)
    )
    assert sorted((r.posted_date, r.amount, r.raw_payee) for r in rows) == [
        (date(2026, 3, 2), Decimal("-4.500000"), "Corner Bakery"),
        (date(2026, 3, 5), Decimal("1200.000000"), "Payroll Deposit"),
    ]

    # The next PDF of the same layout is imported by the saved mapping, no question asked.
    later = pdf_document(
        factories,
        store,
        hh,
        extraction=table_extraction(
            table=[["Date", "Details", "Amount"], ["2026-04-02", "Corner Bakery", "-5.25"]]
        ),
        account_id=account.id,
    )
    with factories.transaction() as db:
        set_context(db, hh)
        fresh = db.get(Document, later.id)
        assert fresh is not None
        result = stored_result(fresh.extraction)
        assert result is not None
        assert not result.tables
        outcome = import_extraction(db, fresh, result)
    assert outcome.status is DocumentStatus.PROCESSED, outcome.summary
    assert outcome.summary["mapping_id"] == mapping["id"]
    later_rows = rows_of(
        factories, hh, select(Transaction).where(Transaction.source_document_id == later.id)
    )
    assert [(r.posted_date, r.amount) for r in later_rows] == [
        (date(2026, 4, 2), Decimal("-5.250000"))
    ]
    open_items = rows_of(
        factories,
        hh,
        select(ReviewItem).where(
            ReviewItem.document_id == later.id, ReviewItem.state == ReviewItemState.OPEN
        ),
    )
    assert open_items == []
