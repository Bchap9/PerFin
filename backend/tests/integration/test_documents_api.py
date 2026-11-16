# SPDX-License-Identifier: AGPL-3.0-or-later
"""The documents API end to end (API.md ``/documents``; SECURITY.md C3, C4, F3, G5, I1).

Uploads go through the real app (multipart streamed into the encrypted
quarantine area); stored documents come from ``tests.fixtures.documents``
in a blob store under ``tmp_path``. All content is synthetic.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.models import Job, Setting, SettingScope
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType, BalanceSnapshot
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.manage import VIEWER_DOWNLOAD_SETTING
from app.domain.documents.models import (
    Blob,
    BlobArea,
    Document,
    DocumentKind,
    DocumentLink,
    DocumentStatus,
    ReviewItem,
    ReviewItemState,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.documents.search import update_search_index
from app.domain.imports.models import ImportTombstone, StatementRecord
from app.domain.investments.models import InvestmentTxn
from app.domain.loans.models import LoanPayment
from app.domain.transactions.models import ReviewState, Transaction
from tests.fixtures.documents import SAMPLE_CSV, make_document, make_review_item
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, household_audit, household_events, problem_type
from tests.fixtures.ledger import make_account, make_snapshot, make_transaction
from tests.fixtures.wealth import make_investment_txn, make_loan, make_loan_payment, make_security

pytestmark = pytest.mark.integration

DOCS = f"{API}/documents"


@pytest.fixture
def doc_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={
            "documents_dir": tmp_path / "documents",
            "quarantine_dir": tmp_path / "quarantine",
            "upload_max_mb": 1,
        }
    )


@pytest.fixture
def actors(doc_settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(doc_settings, factories) as opened:
        yield opened


@pytest.fixture
def store(doc_settings: Settings) -> LocalFSBlobStore:
    return LocalFSBlobStore.from_settings(doc_settings)


def files_in(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


def household(user: AuthedClient) -> uuid.UUID:
    household_id = user.record.household_id
    assert household_id is not None
    return household_id


@pytest.fixture
def home(actors: Actors) -> dict[str, AuthedClient]:
    """An owner, a member and a viewer of one household."""
    owner = actors.new(role="owner")
    hh = owner.record.household
    return {
        "owner": owner,
        "member": actors.new(role="member", household=hh),
        "viewer": actors.new(role="viewer", household=hh),
    }


def stored(
    factories: Factories, store: LocalFSBlobStore, household_id: uuid.UUID, **kwargs: Any
) -> Document:
    with factories.transaction() as db:
        return make_document(db, store, factories.keyring, household_id, **kwargs)


def account_for(
    factories: Factories,
    household_id: uuid.UUID,
    grants: dict[uuid.UUID, GrantLevel] | None = None,
) -> uuid.UUID:
    with factories.transaction() as db:
        account = make_account(db, household_id)
    for user_id, level in (grants or {}).items():
        factories.grant(household_id, user_id, account.id, level)
    return account.id


def load(factories: Factories, household_id: uuid.UUID, model: Any, row_id: uuid.UUID) -> Any:
    with factories.transaction() as db:
        set_context(db, household_id)
        row = db.get(model, row_id)
        if row is not None:
            db.expunge(row)
        return row


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


def upload(user: AuthedClient, data: bytes, *, name: str = "export.csv", **kwargs: Any) -> Any:
    return user.post(DOCS, files={"file": (name, data, "text/html")}, **kwargs)


def test_upload_streams_into_encrypted_quarantine(
    home: dict[str, AuthedClient], factories: Factories, doc_settings: Settings
) -> None:
    owner = home["owner"]
    response = upload(owner, SAMPLE_CSV, name="../../etc/bank.csv")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "received"
    assert body["duplicate_of"] is None
    assert body["job_id"] is not None
    (path,) = files_in(doc_settings.quarantine_dir)
    assert SAMPLE_CSV not in path.read_bytes()  # ciphertext only
    assert b"Synthetic Grocer" not in path.read_bytes()
    assert files_in(doc_settings.documents_dir) == []
    document = load(factories, household(owner), Document, uuid.UUID(body["id"]))
    # The declared type (text/html) is ignored; the name is metadata only (C3).
    assert document.mime_detected == "text/csv"
    assert document.original_filename == "bank.csv"
    assert document.uploaded_by == owner.user.id
    job = load(factories, household(owner), Job, uuid.UUID(body["job_id"]))
    assert job.type == INGEST_JOB_TYPE
    assert [
        e.payload["id"] for e in household_events(factories, household(owner), "document.created")
    ] == [body["id"]]


def test_upload_idempotency_key_replays_and_detects_reuse(
    home: dict[str, AuthedClient], factories: Factories, doc_settings: Settings
) -> None:
    owner = home["owner"]
    key = {"Idempotency-Key": f"upload-{uuid.uuid4()}"}
    first = upload(owner, SAMPLE_CSV, headers=key)
    again = upload(owner, SAMPLE_CSV, headers=key)
    assert first.status_code == again.status_code == 202
    assert again.json() == first.json()
    assert again.headers["Idempotent-Replayed"] == "true"
    other = upload(owner, SAMPLE_CSV + b"2026-01-04,Other,1.00\n", headers=key)
    assert other.status_code == 422
    assert problem_type(other) == "idempotency-key-reused"
    # One document, one stored object: the replay and the refusal kept nothing.
    assert len(files_in(doc_settings.quarantine_dir)) == 1
    with factories.transaction() as db:
        set_context(db, household(owner))
        count = db.execute(
            select(func.count())
            .select_from(Document)
            .where(Document.household_id == household(owner))
        ).scalar_one()
    assert count == 1


@pytest.mark.parametrize(
    ("kwargs", "status", "ptype"),
    [
        (
            {"files": {"file": ("x.csv", b"<html><script>alert(1)</script>", "text/csv")}},
            415,
            "unsupported-file-type",
        ),
        ({"json": {"file": "x"}}, 415, "unsupported-media-type"),
        ({"files": {"other": ("x.csv", SAMPLE_CSV, "text/csv")}}, 422, "invalid-upload"),
        ({"files": {"file": ("x.csv", b"", "text/csv")}}, 415, "unsupported-file-type"),
    ],
)
def test_bad_uploads_store_nothing(
    home: dict[str, AuthedClient],
    doc_settings: Settings,
    kwargs: dict[str, Any],
    status: int,
    ptype: str,
) -> None:
    response = home["owner"].post(DOCS, **kwargs)
    assert response.status_code == status, response.text
    assert problem_type(response) == ptype
    assert files_in(doc_settings.quarantine_dir) == []


def test_upload_over_the_limit_is_413(
    home: dict[str, AuthedClient], doc_settings: Settings
) -> None:
    big = SAMPLE_CSV + b"2026-01-05,Filler,1.00\n" * 60_000  # > 1 MiB
    response = upload(home["owner"], big)
    assert response.status_code == 413
    assert files_in(doc_settings.quarantine_dir) == []


def test_upload_into_an_account_needs_edit(
    home: dict[str, AuthedClient], factories: Factories
) -> None:
    member = home["member"]
    hh = household(member)
    viewable = account_for(factories, hh, {member.user.id: GrantLevel.VIEW})
    editable = account_for(factories, hh, {member.user.id: GrantLevel.EDIT})
    refused = upload(member, SAMPLE_CSV, params={"account_id": str(viewable)})
    assert refused.status_code == 404
    assert problem_type(refused) == "account-not-found"
    accepted = upload(member, SAMPLE_CSV, params={"account_id": str(editable)})
    assert accepted.status_code == 202
    document = load(factories, hh, Document, uuid.UUID(accepted.json()["id"]))
    assert document.account_id == editable
    viewer = upload(home["viewer"], SAMPLE_CSV)
    assert viewer.status_code == 403
    assert problem_type(viewer) == "forbidden"


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_list_and_get_follow_visibility(
    home: dict[str, AuthedClient],
    factories: Factories,
    store: LocalFSBlobStore,
    actors: Actors,
) -> None:
    owner, member = home["owner"], home["member"]
    hh = household(owner)
    granted = account_for(factories, hh, {member.user.id: GrantLevel.EDIT})
    hidden = account_for(factories, hh)
    on_granted = stored(factories, store, hh, account_id=granted)
    on_hidden = stored(factories, store, hh, account_id=hidden, kind=DocumentKind.RECEIPT)
    own = stored(factories, store, hh, uploaded_by=member.user.id, status=DocumentStatus.FAILED)
    owners = stored(factories, store, hh, uploaded_by=owner.user.id)

    def ids(user: AuthedClient, **params: Any) -> set[str]:
        response = user.get(DOCS, params=params)
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["items"]}

    assert ids(owner) == {str(d.id) for d in (on_granted, on_hidden, own, owners)}
    assert ids(member) == {str(on_granted.id), str(own.id)}
    assert ids(owner, kind="receipt") == {str(on_hidden.id)}
    assert ids(owner, status="failed") == {str(own.id)}
    assert ids(member, account=str(granted)) == {str(on_granted.id)}
    refused = member.get(DOCS, params={"account": str(hidden)})
    assert refused.status_code == 404
    assert problem_type(refused) == "account-not-found"

    # Pagination: newest first, stable cursor.
    first = owner.get(DOCS, params={"limit": 3}).json()
    rest = owner.get(DOCS, params={"limit": 3, "cursor": first["next_cursor"]}).json()
    assert [i["id"] for i in first["items"]] == [str(d.id) for d in (owners, own, on_hidden)]
    assert [i["id"] for i in rest["items"]] == [str(on_granted.id)]
    assert rest["next_cursor"] is None

    for user, document, status in (
        (member, on_hidden, 404),
        (member, owners, 404),
        (member, on_granted, 200),
    ):
        response = user.get(f"{DOCS}/{document.id}")
        assert response.status_code == status, response.text
    outsider = actors.new(role="owner")
    response = outsider.get(f"{DOCS}/{on_granted.id}")
    assert response.status_code == 404
    assert problem_type(response) == "document-not-found"
    assert str(on_granted.id) not in {i["id"] for i in outsider.get(DOCS).json()["items"]}


def test_detail_is_metadata_only(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, viewer = home["owner"], home["viewer"]
    hh = household(owner)
    account = account_for(factories, hh, {viewer.user.id: GrantLevel.VIEW})
    document = stored(
        factories,
        store,
        hh,
        account_id=account,
        extraction={"kind": "csv", "ok": True, "tables": [[["Synthetic Grocer"]]]},
    )
    with factories.transaction() as db:
        make_review_item(db, document)
    body = viewer.get(f"{DOCS}/{document.id}").json()
    assert "extraction" not in body
    assert "extracted_text" not in body
    assert "Synthetic Grocer" not in str(body)
    assert body["open_review_items"] == 1
    assert body["quarantined"] is False
    assert body["downloadable"] is False
    assert body["can_edit"] is False
    owners = owner.get(f"{DOCS}/{document.id}").json()
    assert owners["downloadable"] is True
    assert owners["can_edit"] is True


# ---------------------------------------------------------------------------
# Download (C4, I1)
# ---------------------------------------------------------------------------


def test_download_headers_and_body(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = household(owner)
    document = stored(factories, store, hh, filename='Report "Q1"; Ünïcode.csv')
    response = owner.get(f"{DOCS}/{document.id}/download")
    assert response.status_code == 200, response.text
    assert response.content == SAMPLE_CSV
    headers = response.headers
    assert headers["content-type"] == "text/csv"  # the sniffed type, no charset added
    assert headers["content-length"] == str(len(SAMPLE_CSV))
    disposition = headers["content-disposition"]
    assert disposition.startswith('attachment; filename="Report _Q1__ Unicode.csv"; ')
    assert "filename*=UTF-8''Report%20_Q1__%20%C3%9Cn%C3%AFcode.csv" in disposition
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["content-security-policy"].startswith("sandbox")
    assert headers["cache-control"] == "no-store"
    audits = household_audit(factories, hh, "document.download")
    assert [a.target_id for a in audits] == [str(document.id)]


def test_download_without_a_name_uses_the_sniffed_extension(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    document = stored(factories, store, household(owner), filename=None)
    response = owner.get(f"{DOCS}/{document.id}/download")
    assert f'filename="document-{document.id}.csv"' in response.headers["content-disposition"]


def test_viewers_download_only_where_the_household_allows_it(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    viewer, member = home["viewer"], home["member"]
    hh = household(viewer)
    account = account_for(
        factories, hh, {viewer.user.id: GrantLevel.VIEW, member.user.id: GrantLevel.VIEW}
    )
    document = stored(factories, store, hh, account_id=account)
    refused = viewer.get(f"{DOCS}/{document.id}/download")
    assert refused.status_code == 403
    assert problem_type(refused) == "forbidden"
    # A member with a view grant may download (I1: member or above).
    assert member.get(f"{DOCS}/{document.id}/download").status_code == 200
    with factories.transaction() as db:
        set_context(db, hh)
        db.add(
            Setting(
                scope=SettingScope.HOUSEHOLD,
                scope_id=hh,
                key=VIEWER_DOWNLOAD_SETTING,
                value=True,
            )
        )
    allowed = viewer.get(f"{DOCS}/{document.id}/download")
    assert allowed.status_code == 200
    assert allowed.content == SAMPLE_CSV


def test_quarantined_documents_are_never_downloadable(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    document = stored(
        factories,
        store,
        household(owner),
        status=DocumentStatus.FAILED,
        area=BlobArea.QUARANTINE,
    )
    response = owner.get(f"{DOCS}/{document.id}/download")
    assert response.status_code == 409
    assert problem_type(response) == "document-quarantined"
    assert owner.get(f"{DOCS}/{document.id}").json()["quarantined"] is True


def test_a_tampered_file_is_not_served(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    document = stored(factories, store, household(owner))
    blob = load(factories, household(owner), Blob, document.blob_id)
    path = store.path(BlobArea.PERMANENT, blob.key)
    raw = bytearray(path.read_bytes())
    raw[3] ^= 0x01
    path.write_bytes(bytes(raw))
    response = owner.get(f"{DOCS}/{document.id}/download")
    assert response.status_code == 500
    assert problem_type(response) == "document-unreadable"
    assert SAMPLE_CSV not in response.content


def test_other_households_cannot_download(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore, actors: Actors
) -> None:
    document = stored(factories, store, household(home["owner"]))
    outsider = actors.new(role="owner")
    response = outsider.get(f"{DOCS}/{document.id}/download")
    assert response.status_code == 404
    assert problem_type(response) == "document-not-found"


# ---------------------------------------------------------------------------
# Reprocess, link, delete
# ---------------------------------------------------------------------------


def test_reprocess_requeues_and_closes_open_items(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = household(owner)
    view_only = account_for(factories, hh, {member.user.id: GrantLevel.VIEW})
    document = stored(
        factories, store, hh, account_id=view_only, status=DocumentStatus.NEEDS_REVIEW
    )
    with factories.transaction() as db:
        item = make_review_item(db, document)
    refused = member.post(f"{DOCS}/{document.id}/reprocess")
    assert refused.status_code == 403
    assert problem_type(refused) == "forbidden"
    response = owner.post(f"{DOCS}/{document.id}/reprocess")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "received"
    job = load(factories, hh, Job, uuid.UUID(body["job_id"]))
    assert job.type == INGEST_JOB_TYPE
    assert job.payload == {"document_id": str(document.id)}
    closed = load(factories, hh, ReviewItem, item.id)
    assert closed.state is ReviewItemState.DISMISSED
    assert closed.resolution == {"action": "superseded", "by": "document.reprocess"}
    busy = owner.post(f"{DOCS}/{document.id}/reprocess")
    assert busy.status_code == 409
    assert problem_type(busy) == "document-busy"


def test_reprocessing_the_copy_imports_it(
    home: dict[str, AuthedClient], factories: Factories
) -> None:
    owner = home["owner"]
    hh = household(owner)
    first = upload(owner, SAMPLE_CSV)
    assert first.status_code == 202, first.text
    copy = upload(owner, SAMPLE_CSV)
    assert copy.status_code == 202, copy.text
    body = copy.json()
    assert body["status"] == "rejected"
    assert body["duplicate_of"] == first.json()["id"]
    assert body["job_id"] is None
    copy_id = uuid.UUID(body["id"])
    with factories.transaction() as db:
        set_context(db, hh)
        (item,) = db.execute(select(ReviewItem).where(ReviewItem.document_id == copy_id)).scalars()
        assert item.state is ReviewItemState.OPEN
        assert item.payload == {
            "reason": "same_content_skipped",
            "duplicate_of": first.json()["id"],
        }
    # The undo: the skipped copy is terminal, so it can be sent through the pipeline.
    response = owner.post(f"{DOCS}/{copy_id}/reprocess")
    assert response.status_code == 202, response.text
    again = response.json()
    assert again["status"] == "received"
    job = load(factories, hh, Job, uuid.UUID(again["job_id"]))
    assert job.type == INGEST_JOB_TYPE
    assert job.payload == {"document_id": str(copy_id)}
    document = load(factories, hh, Document, copy_id)
    assert (document.status, document.error) == (DocumentStatus.RECEIVED, None)
    closed = load(factories, hh, ReviewItem, item.id)
    assert closed.state is ReviewItemState.DISMISSED
    assert closed.resolution == {"action": "superseded", "by": "document.reprocess"}


def test_link_to_transactions_and_accounts(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = household(owner)
    with factories.transaction() as db:
        editable_account = make_account(db, hh)
        hidden_account = make_account(db, hh)
        transaction = make_transaction(db, editable_account)
        transaction_hidden = make_transaction(db, hidden_account)
    factories.grant(hh, member.user.id, editable_account.id, GrantLevel.EDIT)
    hidden = hidden_account.id
    document = stored(factories, store, hh, account_id=editable_account.id)
    url = f"{DOCS}/{document.id}/link"
    body = {"target_type": "transaction", "target_id": str(transaction.id)}
    created = member.post(url, json=body)
    assert created.status_code == 201, created.text
    again = member.post(url, json=body)
    assert again.status_code == 200
    assert again.json()["id"] == created.json()["id"]
    detail = member.get(f"{DOCS}/{document.id}").json()
    assert [link["target_id"] for link in detail["links"]] == [str(transaction.id)]
    for target_type, target_id, ptype in (
        ("transaction", transaction_hidden.id, "transaction-not-found"),
        ("account", hidden, "account-not-found"),
    ):
        response = member.post(url, json={"target_type": target_type, "target_id": str(target_id)})
        assert response.status_code == 404
        assert problem_type(response) == ptype
    unsupported = member.post(url, json={"target_type": "tax_year", "target_id": str(uuid.uuid4())})
    assert unsupported.status_code == 422
    assert problem_type(unsupported) == "link-target-unsupported"
    assert household_audit(factories, hh, "document.link")


def test_delete_shreds_and_removes_everything(
    home: dict[str, AuthedClient],
    factories: Factories,
    store: LocalFSBlobStore,
    doc_settings: Settings,
) -> None:
    owner, member = home["owner"], home["member"]
    hh = household(owner)
    editable = account_for(factories, hh, {member.user.id: GrantLevel.EDIT})
    view_only = account_for(factories, hh, {member.user.id: GrantLevel.VIEW})
    document = stored(factories, store, hh, account_id=editable)
    kept = stored(factories, store, hh, account_id=view_only)
    with factories.transaction() as db:
        make_review_item(db, document)
    refused = member.delete(f"{DOCS}/{kept.id}")
    assert refused.status_code == 403
    assert len(files_in(doc_settings.documents_dir)) == 2
    response = member.delete(f"{DOCS}/{document.id}")
    assert response.status_code == 204, response.text
    assert member.get(f"{DOCS}/{document.id}").status_code == 404
    assert load(factories, hh, Document, document.id) is None
    assert load(factories, hh, Blob, document.blob_id) is None
    assert len(files_in(doc_settings.documents_dir)) == 1  # only the other document
    with factories.transaction() as db:
        set_context(db, hh)
        left = db.execute(
            select(func.count())
            .select_from(ReviewItem)
            .where(ReviewItem.document_id == document.id)
        ).scalar_one()
        links = db.execute(
            select(func.count())
            .select_from(DocumentLink)
            .where(DocumentLink.document_id == document.id)
        ).scalar_one()
    assert left == 0
    assert links == 0
    assert [a.target_id for a in household_audit(factories, hh, "document.delete")] == [
        str(document.id)
    ]


def imported_rows(factories: Factories, hh: uuid.UUID, document_id: uuid.UUID) -> dict[str, Any]:
    """A statement with one row of every imported kind, a child document and an unrelated row."""
    with factories.transaction() as db:
        set_context(db, hh)
        checking = make_account(db, hh)
        brokerage = make_account(db, hh, type=AccountType.BROKERAGE)
        loan = make_loan(db, hh)
        child = db.get(Document, document_id)
        assert child is not None
        txn = make_transaction(db, checking, source_document_id=document_id, with_import_hash=True)
        unrelated = make_transaction(db, checking)
        trade = make_investment_txn(
            db, brokerage, security=make_security(db, hh), source_document_id=document_id
        )
        payment = make_loan_payment(db, loan, source_document_id=document_id)
        snapshot = make_snapshot(db, checking)
        snapshot.source_document_id = document_id
        db.flush()
        return {
            "checking": checking.id,
            "transaction": txn.id,
            "unrelated": unrelated.id,
            "trade": trade.id,
            "payment": payment.id,
            "snapshot": snapshot.id,
        }


def test_delete_with_data_removes_the_imported_rows(
    home: dict[str, AuthedClient],
    factories: Factories,
    store: LocalFSBlobStore,
    doc_settings: Settings,
) -> None:
    owner = home["owner"]
    hh = household(owner)
    document = stored(factories, store, hh)
    child = stored(factories, store, hh, data=b"child,1\n", filename="attachment.csv")
    with factories.transaction() as db:
        set_context(db, hh)
        row = db.get(Document, child.id)
        assert row is not None
        row.source_meta = {"parent_document_id": str(document.id), "depth": 1}
    rows = imported_rows(factories, hh, document.id)
    with factories.transaction() as db:
        set_context(db, hh)
        checking = db.get(Transaction, rows["unrelated"])
        assert checking is not None
        child_txn = Transaction(
            household_id=hh,
            account_id=rows["checking"],
            posted_date=checking.posted_date,
            amount=checking.amount,
            currency=checking.currency,
            status=checking.status,
            review_state=checking.review_state,
            source_document_id=child.id,
        )
        db.add(child_txn)
        db.flush()
        child_txn_id = child_txn.id

    counts = owner.get(f"{DOCS}/{document.id}/imported")
    assert counts.status_code == 200, counts.text
    assert counts.json() == {
        "transactions": 2,
        "investment_transactions": 1,
        "loan_payments": 1,
        "balance_snapshots": 1,
        "child_documents": 1,
        # No statement record was stored for this hand-made document.
        "statements": [],
        "skipped_lines": 0,
    }

    response = owner.delete(f"{DOCS}/{document.id}", params={"with_data": "true"})
    assert response.status_code == 204, response.text
    assert load(factories, hh, Document, document.id) is None
    assert load(factories, hh, Document, child.id) is None
    assert load(factories, hh, Transaction, rows["transaction"]) is None
    assert load(factories, hh, Transaction, child_txn_id) is None
    assert load(factories, hh, InvestmentTxn, rows["trade"]) is None
    assert load(factories, hh, LoanPayment, rows["payment"]) is None
    assert load(factories, hh, BalanceSnapshot, rows["snapshot"]) is None
    assert load(factories, hh, Transaction, rows["unrelated"]) is not None
    assert files_in(doc_settings.documents_dir) == []
    assert len(household_audit(factories, hh, "document.delete")) == 2
    assert len(household_audit(factories, hh, "transaction.delete")) == 2
    assert len(household_audit(factories, hh, "balance_snapshot.delete")) == 1
    # Nothing is remembered as deleted by a person: the file can be uploaded again whole.
    with factories.transaction() as db:
        set_context(db, hh)
        assert list(db.execute(select(ImportTombstone)).scalars()) == []
    (deleted,) = [
        entry
        for entry in household_audit(factories, hh, "transaction.delete")
        if entry.target_id == str(rows["transaction"])
    ]
    assert deleted.before is not None
    assert deleted.before["tombstoned"] is False


def test_delete_without_data_keeps_the_imported_rows(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = household(owner)
    document = stored(factories, store, hh)
    rows = imported_rows(factories, hh, document.id)
    response = owner.delete(f"{DOCS}/{document.id}")
    assert response.status_code == 204, response.text
    assert load(factories, hh, Document, document.id) is None
    kept = load(factories, hh, Transaction, rows["transaction"])
    assert kept is not None
    assert kept.source_document_id is None
    snapshot = load(factories, hh, BalanceSnapshot, rows["snapshot"])
    assert snapshot is not None
    assert snapshot.source_document_id is None
    # The same file can be uploaded again without a duplicate hold.
    again = upload(owner, SAMPLE_CSV)
    assert again.status_code == 202, again.text
    assert again.json()["duplicate_of"] is None


def test_delete_with_data_is_all_or_nothing(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = household(owner)
    editable = account_for(factories, hh, {member.user.id: GrantLevel.EDIT})
    view_only = account_for(factories, hh, {member.user.id: GrantLevel.VIEW})
    document = stored(factories, store, hh, account_id=editable)
    with factories.transaction() as db:
        set_context(db, hh)
        mine = make_transaction(db, db.get_one(Account, editable), source_document_id=document.id)
        theirs = make_transaction(
            db, db.get_one(Account, view_only), source_document_id=document.id
        )
        ids = (mine.id, theirs.id)
    counts = member.get(f"{DOCS}/{document.id}/imported").json()
    assert counts["transactions"] == 2  # both accounts are visible to the member
    refused = member.delete(f"{DOCS}/{document.id}", params={"with_data": "true"})
    assert refused.status_code == 403, refused.text
    assert load(factories, hh, Document, document.id) is not None
    assert all(load(factories, hh, Transaction, i) is not None for i in ids)


# ---------------------------------------------------------------------------
# What a document produced (statements, accounts, how they were checked)
# ---------------------------------------------------------------------------

JANUARY = (date(2026, 1, 1), date(2026, 1, 31))
FEBRUARY = (date(2026, 2, 1), date(2026, 2, 28))


def record(
    factories: Factories,
    hh: uuid.UUID,
    document_id: uuid.UUID,
    account_id: uuid.UUID,
    *,
    period: tuple[date, date] = JANUARY,
    kind: str = "bank",
    outcome: str = "imported",
    verification: str = "unverified",
    source_format: str = "pdf",
) -> None:
    """A ``statement_record`` inserted by hand, as the importer would leave it."""
    with factories.transaction() as db:
        set_context(db, hh)
        db.add(
            StatementRecord(
                household_id=hh,
                account_id=account_id,
                document_id=document_id,
                kind=kind,
                outcome=outcome,
                period_start=period[0],
                period_end=period[1],
                period_stated=True,
                source_format=source_format,
                currency="USD",
                verification=verification,
            )
        )


def named_account(factories: Factories, hh: uuid.UUID, name: str, **kwargs: Any) -> uuid.UUID:
    with factories.transaction() as db:
        return make_account(db, hh, name=name, **kwargs).id


def rows_on(
    factories: Factories,
    hh: uuid.UUID,
    account_id: uuid.UUID,
    document_id: uuid.UUID | None,
    *days: date,
    review_state: ReviewState = ReviewState.NONE,
) -> None:
    with factories.transaction() as db:
        set_context(db, hh)
        account = db.get_one(Account, account_id)
        for day in days:
            make_transaction(
                db,
                account,
                posted_date=day,
                review_state=review_state,
                source_document_id=document_id,
            )


def test_imported_lists_the_statements_with_their_verification(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = household(owner)
    harbor = named_account(factories, hh, "Harbor Everyday")
    alder = named_account(factories, hh, "Alder Card", type=AccountType.CREDIT_CARD)
    document = stored(
        factories,
        store,
        hh,
        data=b"%PDF-1.4 synthetic test document",
        mime="application/pdf",
        filename="synthetic.pdf",
        extraction={"kind": "pdf", "ok": True, "parse": {"skipped_lines": 3}},
    )
    other = stored(factories, store, hh, data=b"another,1\n")
    # One record on Alder: every row of the document there counts, whatever its date.
    record(factories, hh, document.id, alder)
    rows_on(factories, hh, alder, document.id, date(2026, 1, 9), date(2026, 3, 2))
    # Two records on Harbor: each counts the rows dated inside its period.
    record(factories, hh, document.id, harbor, period=FEBRUARY)
    record(factories, hh, document.id, harbor, period=JANUARY)
    rows_on(factories, hh, harbor, document.id, date(2026, 1, 1), date(2026, 1, 31))
    rows_on(factories, hh, harbor, document.id, date(2026, 1, 20), review_state=ReviewState.PENDING)
    rows_on(factories, hh, harbor, document.id, date(2026, 2, 14))
    # Never counted: a row typed by hand and a row of another file on the account.
    rows_on(factories, hh, harbor, None, date(2026, 1, 12))
    rows_on(factories, hh, harbor, other.id, date(2026, 1, 13))
    record(factories, hh, other.id, harbor, period=(date(2026, 1, 2), date(2026, 1, 30)))

    response = owner.get(f"{DOCS}/{document.id}/imported")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["transactions"] == 6
    assert body["skipped_lines"] == 3
    common = {
        "kind": "bank",
        "outcome": "imported",
        "verification": "unverified",
        "source_format": "pdf",
        "unchecked": True,
    }
    assert body["statements"] == [
        {
            **common,
            "account_id": str(alder),
            "account_name": "Alder Card",
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "transactions": 2,
            "pending": 0,
        },
        {
            **common,
            "account_id": str(harbor),
            "account_name": "Harbor Everyday",
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "transactions": 3,
            "pending": 1,
        },
        {
            **common,
            "account_id": str(harbor),
            "account_name": "Harbor Everyday",
            "period_start": "2026-02-01",
            "period_end": "2026-02-28",
            "transactions": 1,
            "pending": 0,
        },
    ]


def test_reconciled_and_csv_records_are_not_unchecked(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = household(owner)
    cases: dict[str, dict[str, Any]] = {
        "A reconciled": {"verification": "reconciled"},
        "B accepted": {"verification": "accepted"},
        "C spreadsheet": {"source_format": "csv"},
        "D skipped": {"outcome": "skipped"},
        "E scanned": {"source_format": "image"},
        "F brokerage": {"kind": "investment"},
    }
    document = stored(factories, store, hh)
    accounts: dict[str, uuid.UUID] = {}
    for name, fields in cases.items():
        investment = fields.get("kind") == "investment"
        account = named_account(
            factories, hh, name, type=AccountType.BROKERAGE if investment else AccountType.CHECKING
        )
        accounts[name] = account
        record(factories, hh, document.id, account, **fields)
        if investment:
            with factories.transaction() as db:
                set_context(db, hh)
                make_investment_txn(
                    db,
                    db.get_one(Account, account),
                    security=make_security(db, hh),
                    source_document_id=document.id,
                )
    body = owner.get(f"{DOCS}/{document.id}/imported").json()
    assert body["skipped_lines"] == 0  # the stored reading carries no count
    assert [(s["account_name"], s["unchecked"]) for s in body["statements"]] == [
        ("A reconciled", False),
        ("B accepted", False),
        ("C spreadsheet", False),
        ("D skipped", False),
        ("E scanned", True),
        ("F brokerage", True),
    ]
    assert [s["verification"] for s in body["statements"]][:2] == ["reconciled", "accepted"]
    brokerage = body["statements"][-1]
    assert (brokerage["kind"], brokerage["transactions"], brokerage["pending"]) == (
        "investment",
        1,
        0,
    )
    # A document whose only records are checked, skipped or of a file format has no marker.
    plain = stored(factories, store, hh, data=b"plain,1\n")
    for name in ("A reconciled", "C spreadsheet", "D skipped"):
        record(factories, hh, plain.id, accounts[name], **cases[name])
    listed = {i["id"]: i["unchecked"] for i in owner.get(DOCS).json()["items"]}
    assert listed == {str(document.id): True, str(plain.id): False}


def test_statements_on_invisible_accounts_are_left_out(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = household(owner)
    with factories.transaction() as db:
        seen = make_account(db, hh, name="Seen Savings").id
        hidden = make_account(db, hh, name="Hidden Savings").id
    factories.grant(hh, member.user.id, seen, GrantLevel.VIEW)
    document = stored(factories, store, hh, account_id=seen)
    record(factories, hh, document.id, seen, verification="reconciled", source_format="csv")
    record(factories, hh, document.id, hidden)
    rows_on(factories, hh, seen, document.id, date(2026, 1, 5))
    rows_on(factories, hh, hidden, document.id, date(2026, 1, 6), date(2026, 1, 7))

    body = member.get(f"{DOCS}/{document.id}/imported").json()
    assert [s["account_name"] for s in body["statements"]] == ["Seen Savings"]
    assert body["statements"][0]["transactions"] == 1
    assert "Hidden Savings" not in str(body)
    assert str(hidden) not in str(body)
    # The marker of the list follows the same rule: the unchecked record is on a hidden account.
    listed = {i["id"]: i["unchecked"] for i in member.get(DOCS).json()["items"]}
    assert listed == {str(document.id): False}
    everything = owner.get(f"{DOCS}/{document.id}/imported").json()
    assert [s["account_name"] for s in everything["statements"]] == [
        "Hidden Savings",
        "Seen Savings",
    ]
    assert owner.get(DOCS).json()["items"][0]["unchecked"] is True


def test_list_marks_an_unchecked_document(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = household(owner)
    account = account_for(factories, hh)
    marked = stored(
        factories,
        store,
        hh,
        data=b"marked ledger text",
        filename="marked.txt",
        extraction={"kind": "pdf", "ok": True, "text": "marked ledger text"},
    )
    checked = stored(factories, store, hh, data=b"checked,1\n")
    bare = stored(factories, store, hh, data=b"bare,1\n")
    # Two unchecked statements and one balanced: the document is listed once, marked.
    record(factories, hh, marked.id, account, period=JANUARY)
    record(factories, hh, marked.id, account, period=FEBRUARY)
    record(
        factories,
        hh,
        marked.id,
        account,
        period=(date(2026, 3, 1), date(2026, 3, 31)),
        verification="reconciled",
    )
    record(factories, hh, checked.id, account, verification="reconciled")

    items = owner.get(DOCS).json()["items"]
    assert [(i["id"], i["unchecked"]) for i in items] == [
        (str(bare.id), False),
        (str(checked.id), False),
        (str(marked.id), True),
    ]
    paged = owner.get(DOCS, params={"limit": 2}).json()
    rest = owner.get(DOCS, params={"limit": 2, "cursor": paged["next_cursor"]}).json()
    assert [(i["id"], i["unchecked"]) for i in rest["items"]] == [(str(marked.id), True)]
    # The search listing carries the marker too.
    with factories.transaction() as db:
        set_context(db, hh)
        update_search_index(db, db.get_one(Document, marked.id))
    found = owner.get(DOCS, params={"q": "marked"}).json()["items"]
    assert [(i["id"], i["unchecked"]) for i in found] == [(str(marked.id), True)]


def test_detail_says_whether_pages_can_be_previewed(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member, viewer = home["owner"], home["member"], home["viewer"]
    hh = household(owner)
    account = account_for(
        factories, hh, {member.user.id: GrantLevel.VIEW, viewer.user.id: GrantLevel.VIEW}
    )
    pdf = {"data": b"%PDF-1.4 synthetic test document", "mime": "application/pdf"}
    sheet = stored(factories, store, hh, account_id=account)
    pages = stored(factories, store, hh, account_id=account, filename="pages.pdf", **pdf)
    scan = stored(
        factories,
        store,
        hh,
        account_id=account,
        data=b"synthetic scan",
        mime="image/png",
        filename="scan.png",
    )
    waiting = stored(
        factories,
        store,
        hh,
        account_id=account,
        data=b"%PDF-1.4 synthetic test document two",
        mime="application/pdf",
        filename="waiting.pdf",
        area=BlobArea.QUARANTINE,
        status=DocumentStatus.FAILED,
    )

    def previewable(user: AuthedClient, document: Document) -> bool:
        response = user.get(f"{DOCS}/{document.id}")
        assert response.status_code == 200, response.text
        return bool(response.json()["previewable"])

    assert previewable(member, sheet) is False  # a CSV has no pages
    assert previewable(member, pages) is True
    assert previewable(member, scan) is True
    assert previewable(owner, pages) is True
    assert previewable(member, waiting) is False  # never a quarantined file
    assert previewable(viewer, pages) is False  # I1: a viewer may not download
    with factories.transaction() as db:
        set_context(db, hh)
        db.add(
            Setting(
                scope=SettingScope.HOUSEHOLD,
                scope_id=hh,
                key=VIEWER_DOWNLOAD_SETTING,
                value=True,
            )
        )
    assert previewable(viewer, pages) is True
