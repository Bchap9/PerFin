# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue API (API.md ``/review``; INGESTION.md "Review queue"; F3).

Documents and items come from ``tests.fixtures.documents`` (synthetic files
in a blob store under ``tmp_path``); the importer's payload shapes are the
ones ``app.domain.imports.service`` writes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.models import Job
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    BlobArea,
    Document,
    DocumentStatus,
    ReviewItem,
    ReviewItemState,
    ReviewKind,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE
from app.domain.review import repo as review_repo
from app.domain.review.models import ImportHashAlias
from app.domain.transactions.models import ReviewState, Transaction
from app.ingestion.sandbox.protocol import ExtractResult, Statement, StatementAccount
from tests.fixtures.documents import make_document, make_review_item
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, household_audit, household_events, problem_type
from tests.fixtures.ledger import DEFAULT_DATE, make_account, make_transaction

pytestmark = pytest.mark.integration

REVIEW = f"{API}/review"


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


def new_account(
    factories: Factories, household_id: uuid.UUID, grants: dict[uuid.UUID, GrantLevel]
) -> Account:
    with factories.transaction() as db:
        account = make_account(db, household_id)
    for user_id, level in grants.items():
        factories.grant(household_id, user_id, account.id, level)
    return account


def document(
    factories: Factories, store: LocalFSBlobStore, household_id: uuid.UUID, **kwargs: Any
) -> Document:
    kwargs.setdefault("status", DocumentStatus.NEEDS_REVIEW)
    with factories.transaction() as db:
        return make_document(db, store, factories.keyring, household_id, **kwargs)


def item(
    factories: Factories, doc: Document, kind: ReviewKind, payload: dict[str, Any]
) -> ReviewItem:
    with factories.transaction() as db:
        return make_review_item(db, doc, kind, payload)


def load(factories: Factories, household_id: uuid.UUID, model: Any, row_id: uuid.UUID) -> Any:
    with factories.transaction() as db:
        set_context(db, household_id)
        row = db.get(model, row_id)
        if row is not None:
            db.expunge(row)
        return row


def jobs_of(factories: Factories, household_id: uuid.UUID, job_type: str) -> list[Job]:
    with factories.transaction() as db:
        set_context(db, household_id)
        rows = list(
            db.execute(
                select(Job).where(Job.household_id == household_id, Job.type == job_type)
            ).scalars()
        )
        for row in rows:
            db.expunge(row)
        return rows


def resolve(user: AuthedClient, item_id: uuid.UUID, body: dict[str, Any]) -> Any:
    return user.post(f"{REVIEW}/{item_id}/resolve", json=body)


# ---------------------------------------------------------------------------
# Visibility
# ---------------------------------------------------------------------------


def test_queue_follows_document_and_account_visibility(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore, actors: Actors
) -> None:
    owner, member, viewer = home["owner"], home["member"], home["viewer"]
    hh = hh_of(owner)
    granted = new_account(
        factories, hh, {member.user.id: GrantLevel.EDIT, viewer.user.id: GrantLevel.VIEW}
    )
    hidden = new_account(factories, hh, {})
    visible_doc = document(factories, store, hh, account_id=granted.id)
    hidden_doc = document(factories, store, hh, account_id=hidden.id)
    seen = item(factories, visible_doc, ReviewKind.UNPARSED, {"reason": "import_pending"})
    unseen = item(factories, hidden_doc, ReviewKind.UNPARSED, {"reason": "import_pending"})
    # Visible document, but the payload names a hidden account.
    other_account = item(
        factories,
        visible_doc,
        ReviewKind.UNKNOWN_ACCOUNT,
        {"reason": "currency_mismatch", "account_id": str(hidden.id)},
    )

    def ids(user: AuthedClient, **params: Any) -> list[str]:
        response = user.get(REVIEW, params=params)
        assert response.status_code == 200, response.text
        return [i["id"] for i in response.json()["items"]]

    assert ids(owner) == [str(seen.id), str(unseen.id), str(other_account.id)]
    assert ids(member) == [str(seen.id)]
    assert ids(viewer) == [str(seen.id)]
    assert ids(owner, kind="unknown_account") == [str(other_account.id)]
    assert ids(owner, document=str(hidden_doc.id)) == [str(unseen.id)]
    page = owner.get(REVIEW, params={"limit": 2}).json()
    assert page["next_cursor"] == str(unseen.id)
    for user, row, status in (
        (member, unseen, 404),
        (member, other_account, 404),
        (viewer, seen, 200),
    ):
        assert user.get(f"{REVIEW}/{row.id}").status_code == status
    body = viewer.get(f"{REVIEW}/{seen.id}").json()
    assert body["can_resolve"] is False
    assert body["next_step"] == "reprocess"
    assert member.get(f"{REVIEW}/{seen.id}").json()["can_resolve"] is True
    outsider = actors.new(role="owner")
    response = outsider.get(f"{REVIEW}/{seen.id}")
    assert response.status_code == 404
    assert problem_type(response) == "review-item-not-found"
    assert ids(outsider) == []


# ---------------------------------------------------------------------------
# Dismiss
# ---------------------------------------------------------------------------


def test_dismiss_closes_once_and_settles_the_document(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member, viewer = home["owner"], home["member"], home["viewer"]
    hh = hh_of(owner)
    view_only = new_account(
        factories, hh, {member.user.id: GrantLevel.VIEW, viewer.user.id: GrantLevel.VIEW}
    )
    doc = document(factories, store, hh, account_id=view_only.id)
    row = item(factories, doc, ReviewKind.LOW_CONFIDENCE_FIELD, {"field": "payee"})
    for user in (viewer, member):
        refused = user.post(f"{REVIEW}/{row.id}/dismiss")
        assert refused.status_code == 403
        assert problem_type(refused) == "forbidden"
    response = owner.post(f"{REVIEW}/{row.id}/dismiss")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "dismissed"
    assert body["resolved_by"] == str(owner.user.id)
    again = owner.post(f"{REVIEW}/{row.id}/dismiss")
    assert again.status_code == 409
    assert problem_type(again) == "review-item-closed"
    # The last open item of an accepted needs_review document: processed.
    assert load(factories, hh, Document, doc.id).status is DocumentStatus.PROCESSED
    assert household_audit(factories, hh, "review.dismiss")
    events = household_events(factories, hh, "review.dismissed")
    assert [e.payload for e in events] == [
        {"id": str(row.id), "kind": "low_confidence_field", "document_id": str(doc.id)}
    ]


def test_kind_mismatch_and_unsupported_kinds(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    doc = document(factories, store, hh)
    unparsed = item(factories, doc, ReviewKind.UNPARSED, {"reason": "import_pending"})
    other = item(factories, doc, ReviewKind.NEW_INSTITUTION, {"name_hint": "x"})
    wrong = resolve(owner, unparsed.id, {"kind": "possible_duplicate", "decision": "confirm"})
    assert wrong.status_code == 422
    assert problem_type(wrong) == "review-kind-mismatch"
    refused = resolve(owner, other.id, {"kind": "unparsed", "action": "reprocess"})
    assert refused.status_code == 409
    assert problem_type(refused) == "review-resolution-unsupported"
    invalid = resolve(owner, unparsed.id, {"kind": "unparsed", "action": "explode"})
    assert invalid.status_code == 422


# ---------------------------------------------------------------------------
# possible_duplicate
# ---------------------------------------------------------------------------


def test_duplicate_file_confirm_rejects_the_document(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    original = document(factories, store, hh, status=DocumentStatus.PROCESSED)
    duplicate = document(factories, store, hh, area=BlobArea.QUARANTINE)
    row = item(
        factories,
        duplicate,
        ReviewKind.POSSIBLE_DUPLICATE,
        {"reason": "same_content", "duplicate_of": str(original.id)},
    )
    response = resolve(owner, row.id, {"kind": "possible_duplicate", "decision": "confirm"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "resolved"
    assert response.json()["resolution"] == {
        "decision": "confirm",
        "duplicate_of": str(original.id),
    }
    stored = load(factories, hh, Document, duplicate.id)
    assert stored.status is DocumentStatus.REJECTED
    assert stored.error == "duplicate"


def test_duplicate_file_reject_runs_the_pipeline(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    original = document(factories, store, hh, status=DocumentStatus.PROCESSED)
    duplicate = document(factories, store, hh, area=BlobArea.QUARANTINE)
    row = item(
        factories,
        duplicate,
        ReviewKind.POSSIBLE_DUPLICATE,
        {"reason": "same_content", "duplicate_of": str(original.id)},
    )
    response = resolve(owner, row.id, {"kind": "possible_duplicate", "decision": "reject"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "resolved"
    assert load(factories, hh, Document, duplicate.id).status is DocumentStatus.RECEIVED
    (job,) = jobs_of(factories, hh, INGEST_JOB_TYPE)
    assert job.payload == {"document_id": str(duplicate.id)}
    assert response.json()["resolution"]["job_id"] == str(job.id)


def near_match(
    factories: Factories,
    store: LocalFSBlobStore,
    household_id: uuid.UUID,
    account: Account,
    *,
    kept_hash: bool,
) -> tuple[Document, ReviewItem, Transaction, Transaction]:
    doc = document(factories, store, household_id, account_id=account.id)
    with factories.transaction() as db:
        kept = make_transaction(
            db, account, amount="-9.99", raw_payee="Synthetic Cafe", with_import_hash=kept_hash
        )
        imported = make_transaction(
            db,
            account,
            amount="-9.99",
            raw_payee="SYNTH CAFE #12",
            with_import_hash=True,
            review_state=ReviewState.PENDING,
            source_document_id=doc.id,
        )
    row = item(
        factories,
        doc,
        ReviewKind.POSSIBLE_DUPLICATE,
        {
            "reason": "near_match",
            "account_id": str(account.id),
            "transaction_id": str(imported.id),
            "candidates": [str(kept.id)],
        },
    )
    return doc, row, kept, imported


@pytest.mark.parametrize("kept_hash", [False, True])
def test_near_match_confirm_deletes_and_records_the_hash(
    home: dict[str, AuthedClient],
    factories: Factories,
    store: LocalFSBlobStore,
    kept_hash: bool,
) -> None:
    owner, member = home["owner"], home["member"]
    hh = hh_of(owner)
    account = new_account(factories, hh, {member.user.id: GrantLevel.EDIT})
    doc, row, kept, imported = near_match(factories, store, hh, account, kept_hash=kept_hash)
    response = resolve(member, row.id, {"kind": "possible_duplicate", "decision": "confirm"})
    assert response.status_code == 200, response.text
    resolution = response.json()["resolution"]
    assert resolution["deleted"] is True
    assert resolution["duplicate_of"] == str(kept.id)
    assert load(factories, hh, Transaction, imported.id) is None
    kept_now = load(factories, hh, Transaction, kept.id)
    with factories.transaction() as db:
        set_context(db, hh)
        aliases = list(db.execute(select(ImportHashAlias)).scalars())
        rows = review_repo.alias_rows(db, hh, account.id, DEFAULT_DATE, DEFAULT_DATE)
    if kept_hash:
        assert resolution["hash_recorded"] == "alias"
        assert [(a.transaction_id, a.import_hash) for a in aliases] == [
            (kept.id, imported.import_hash)
        ]
        assert [(r[0], r[3]) for r in rows] == [(kept.id, imported.import_hash)]
        assert kept_now.import_hash == kept.import_hash
    else:
        assert resolution["hash_recorded"] == "hash"
        assert kept_now.import_hash == imported.import_hash
        assert aliases == []
    # The last open item: the document is processed.
    assert load(factories, hh, Document, doc.id).status is DocumentStatus.PROCESSED


def test_near_match_reject_confirms_the_imported_row(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = hh_of(owner)
    account = new_account(factories, hh, {member.user.id: GrantLevel.EDIT})
    _, row, kept, imported = near_match(factories, store, hh, account, kept_hash=True)
    response = resolve(member, row.id, {"kind": "possible_duplicate", "decision": "reject"})
    assert response.status_code == 200, response.text
    assert load(factories, hh, Transaction, imported.id).review_state is ReviewState.CONFIRMED
    assert load(factories, hh, Transaction, kept.id) is not None


def test_near_match_needs_a_candidate_and_edit_rights(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = hh_of(owner)
    account = new_account(factories, hh, {member.user.id: GrantLevel.EDIT})
    _, row, _kept, imported = near_match(factories, store, hh, account, kept_hash=True)
    outside = resolve(
        member,
        row.id,
        {"kind": "possible_duplicate", "decision": "confirm", "duplicate_of": str(uuid.uuid4())},
    )
    assert outside.status_code == 422
    assert problem_type(outside) == "invalid-resolution"
    assert load(factories, hh, Transaction, imported.id) is not None
    assert load(factories, hh, ReviewItem, row.id).state is ReviewItemState.OPEN


# ---------------------------------------------------------------------------
# unknown_account
# ---------------------------------------------------------------------------


def ofx_extraction(last4: str) -> dict[str, Any]:
    result = ExtractResult(
        kind="ofx",
        ok=True,
        statements=[
            Statement(source_format="ofx", account=StatementAccount(last4=last4, currency="USD"))
        ],
    )
    return result.model_dump(mode="json")


def test_unknown_account_assigns_remembers_and_reimports(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = hh_of(owner)
    target = new_account(factories, hh, {member.user.id: GrantLevel.EDIT})
    view_only = new_account(factories, hh, {member.user.id: GrantLevel.VIEW})
    doc = document(
        factories,
        store,
        hh,
        mime="application/x-ofx",
        uploaded_by=member.user.id,
        extraction=ofx_extraction("4321"),
    )
    row = item(
        factories,
        doc,
        ReviewKind.UNKNOWN_ACCOUNT,
        {"reason": "account_not_found", "statement": 0, "candidates": []},
    )
    hidden = resolve(member, row.id, {"kind": "unknown_account", "account_id": str(view_only.id)})
    assert hidden.status_code == 404
    assert problem_type(hidden) == "account-not-found"
    response = resolve(
        member,
        row.id,
        {"kind": "unknown_account", "account_id": str(target.id), "remember": True},
    )
    assert response.status_code == 200, response.text
    resolution = response.json()["resolution"]
    assert resolution["remembered"] == ["last4"]
    assert resolution["rerun"] == "import"
    assert load(factories, hh, Account, target.id).number_last4 == "4321"
    assert load(factories, hh, Document, doc.id).account_id == target.id
    (job,) = jobs_of(factories, hh, APPLY_MAPPING_JOB_TYPE)
    assert job.payload["account_id"] == str(target.id)
    assert resolution["job_id"] == str(job.id)
    # Audit and event carry ids and decisions, never the digits (H8).
    (audit_row,) = household_audit(factories, hh, "review.resolve")
    assert "4321" not in str(audit_row.after)


def test_unknown_account_refuses_conflicting_digits(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    with factories.transaction() as db:
        target = make_account(db, hh, number_last4="9999")
    doc = document(
        factories, store, hh, mime="application/x-ofx", extraction=ofx_extraction("4321")
    )
    row = item(
        factories, doc, ReviewKind.UNKNOWN_ACCOUNT, {"reason": "account_not_found", "statement": 0}
    )
    response = resolve(
        owner, row.id, {"kind": "unknown_account", "account_id": str(target.id), "remember": True}
    )
    assert response.status_code == 409
    assert problem_type(response) == "account-last4-conflict"
    assert load(factories, hh, ReviewItem, row.id).state is ReviewItemState.OPEN


# ---------------------------------------------------------------------------
# unparsed
# ---------------------------------------------------------------------------


def test_unparsed_reprocess_and_mapping_hint(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    doc = document(factories, store, hh, status=DocumentStatus.FAILED, area=BlobArea.QUARANTINE)
    failed = item(factories, doc, ReviewKind.UNPARSED, {"reason": "extraction_failed"})
    mapping = item(factories, doc, ReviewKind.UNPARSED, {"reason": "mapping_required"})
    assert owner.get(f"{REVIEW}/{mapping.id}").json()["next_step"] == "column_mapping"
    not_importable = resolve(owner, mapping.id, {"kind": "unparsed", "action": "apply_mapping"})
    assert not_importable.status_code == 409
    assert problem_type(not_importable) == "document-not-importable"
    response = resolve(owner, failed.id, {"kind": "unparsed", "action": "reprocess"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "resolved"
    assert load(factories, hh, Document, doc.id).status is DocumentStatus.RECEIVED
    # The other open item of the document was superseded by the new run.
    assert load(factories, hh, ReviewItem, mapping.id).state is ReviewItemState.DISMISSED
    assert len(jobs_of(factories, hh, INGEST_JOB_TYPE)) == 1


# ---------------------------------------------------------------------------
# Items name their document
# ---------------------------------------------------------------------------


def test_items_name_their_document_and_reason(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    scan = document(factories, store, hh, mime="application/pdf", filename="october-statement.pdf")
    held = document(
        factories, store, hh, mime="application/pdf", filename="held.pdf", area=BlobArea.QUARANTINE
    )
    csv_doc = document(factories, store, hh, filename="activity.csv")
    item(factories, scan, ReviewKind.UNKNOWN_ACCOUNT, {"reason": "account_not_found"})
    item(factories, held, ReviewKind.UNPARSED, {"reason": "extraction_failed"})
    item(factories, csv_doc, ReviewKind.LOW_CONFIDENCE_FIELD, {"reason": "lines_skipped"})
    listed = owner.get(f"{API}/review").json()["items"]
    by_name = {i["document"]["original_filename"]: i for i in listed}
    assert by_name["october-statement.pdf"]["reason"] == "account_not_found"
    assert by_name["october-statement.pdf"]["document"]["previewable"] is True
    assert by_name["october-statement.pdf"]["document"]["mime_detected"] == "application/pdf"
    assert by_name["held.pdf"]["document"]["previewable"] is False  # quarantined
    assert by_name["activity.csv"]["document"]["previewable"] is False  # no pages
    assert by_name["activity.csv"]["reason"] == "lines_skipped"
    one = owner.get(f"{API}/review/{by_name['october-statement.pdf']['id']}").json()
    assert one["document"]["original_filename"] == "october-statement.pdf"


def test_an_item_without_a_document_has_none(
    home: dict[str, AuthedClient], factories: Factories
) -> None:
    owner = home["owner"]
    with factories.transaction() as db:
        set_context(db, hh_of(owner))
        review_repo_item = ReviewItem(
            household_id=hh_of(owner),
            kind=ReviewKind.REJECTED_EMAIL,
            payload={"reason": "daily_digest"},
        )
        db.add(review_repo_item)
    (listed,) = [
        i for i in owner.get(f"{API}/review").json()["items"] if i["kind"] == "rejected_email"
    ]
    assert listed["document"] is None
    assert listed["reason"] == "daily_digest"


def test_the_review_report_counts_items_by_kind_and_reason(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    from app.domain.review.report import review_report

    owner = home["owner"]
    hh = hh_of(owner)
    pdf = document(factories, store, hh, mime="application/pdf", filename="private-name.pdf")
    item(factories, pdf, ReviewKind.UNKNOWN_ACCOUNT, {"reason": "account_not_found"})
    item(factories, pdf, ReviewKind.UNKNOWN_ACCOUNT, {"reason": "account_not_found"})
    item(factories, pdf, ReviewKind.UNPARSED, {"reason": "extraction_failed"})
    with factories.transaction() as db:
        report = review_report(db, hh)
    assert report["open"] == 3
    assert report["by_reason"][0] == {
        "kind": "unknown_account",
        "reason": "account_not_found",
        "state": "open",
        "count": 2,
    }
    assert report["open_by_file_type"] == {"application/pdf": 3}
    assert "private-name" not in str(report)
