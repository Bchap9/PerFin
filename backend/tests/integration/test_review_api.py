# SPDX-License-Identifier: AGPL-3.0-or-later
"""The review queue API (API.md ``/review``; INGESTION.md "Review queue"; F3).

Documents and items come from ``tests.fixtures.documents`` (synthetic files
in a blob store under ``tmp_path``); the importer's payload shapes are the
ones ``app.domain.imports.service`` writes.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.models import Job
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType
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
from app.domain.imports.models import ImportMapping, ImportTombstone
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE, MappingSpec
from app.domain.review import repo as review_repo
from app.domain.review.models import ImportHashAlias
from app.domain.transactions.models import ReviewState, Transaction
from app.ingestion.sandbox.protocol import ExtractResult, Statement, StatementAccount
from tests.fixtures.documents import make_document, make_review_item
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, household_audit, household_events, problem_type
from tests.fixtures.imports import make_import_mapping, run_jobs, table_result
from tests.fixtures.ledger import DEFAULT_DATE, household_context, make_account, make_transaction
from tests.fixtures.synth_statements import (
    SynthStatement,
    csv_bytes,
    layout_statement,
    statement,
    synth_ledger,
)
from tests.integration import test_structured_import as structured

pytestmark = pytest.mark.integration

#: The real pipeline on temporary stores (the importer's own tests' fixture).
import_env = structured.env

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
    # The row lives on in the kept one: a confirmed duplicate leaves no tombstone.
    assert tombstones_of(factories, hh, account) == []


def tombstones_of(
    factories: Factories, household_id: uuid.UUID, account: Account
) -> list[ImportTombstone]:
    with factories.transaction() as db:
        set_context(db, household_id)
        return list(
            db.execute(
                select(ImportTombstone).where(ImportTombstone.account_id == account.id)
            ).scalars()
        )


def resembles_removed(
    factories: Factories, store: LocalFSBlobStore, household_id: uuid.UUID, account: Account
) -> tuple[Document, ReviewItem, Transaction]:
    """A pending row held because it resembles a row a person deleted: no candidate."""
    doc = document(factories, store, household_id, account_id=account.id)
    with factories.transaction() as db:
        imported = make_transaction(
            db,
            account,
            amount="-7.25",
            raw_payee="SYNTH BAKERY 0042",
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
            "candidates": [],
        },
    )
    return doc, row, imported


def test_a_row_that_resembles_a_removed_one_is_removed_again(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = hh_of(owner)
    account = new_account(factories, hh, {member.user.id: GrantLevel.EDIT})
    doc, row, imported = resembles_removed(factories, store, hh, account)

    response = resolve(member, row.id, {"kind": "possible_duplicate", "decision": "confirm"})

    assert response.status_code == 200, response.text
    resolution = response.json()["resolution"]
    assert (resolution["decision"], resolution["deleted"]) == ("confirm", True)
    assert resolution["removed_again"] is True
    assert "duplicate_of" not in resolution
    assert load(factories, hh, Transaction, imported.id) is None
    # Deleted by a person, so this spelling stays out too.
    (stone,) = tombstones_of(factories, hh, account)
    assert (stone.import_hash, stone.amount, stone.deleted_by) == (
        imported.import_hash,
        imported.amount,
        member.user.id,
    )
    assert load(factories, hh, Document, doc.id).status is DocumentStatus.PROCESSED


def test_a_row_that_resembles_a_removed_one_can_be_kept(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = hh_of(owner)
    account = new_account(factories, hh, {member.user.id: GrantLevel.EDIT})
    _, row, imported = resembles_removed(factories, store, hh, account)

    response = resolve(member, row.id, {"kind": "possible_duplicate", "decision": "reject"})

    assert response.status_code == 200, response.text
    assert load(factories, hh, Transaction, imported.id).review_state is ReviewState.CONFIRMED
    assert tombstones_of(factories, hh, account) == []


def test_removing_again_needs_edit_on_the_account(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = hh_of(owner)
    account = new_account(factories, hh, {member.user.id: GrantLevel.VIEW})
    _, row, imported = resembles_removed(factories, store, hh, account)

    response = resolve(member, row.id, {"kind": "possible_duplicate", "decision": "confirm"})

    assert response.status_code in (403, 404), response.text
    assert load(factories, hh, Transaction, imported.id) is not None
    assert tombstones_of(factories, hh, account) == []
    assert load(factories, hh, ReviewItem, row.id).state is ReviewItemState.OPEN


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


# ---------------------------------------------------------------------------
# unparsed: what a positive amount means
# ---------------------------------------------------------------------------

#: A card export that lists purchases as positive amounts (invented rows).
CARD_ROWS: list[list[str]] = [
    ["Date", "Description", "Amount"],
    ["02/03/2026", "Lantern Lane Books", "18.40"],
    ["02/05/2026", "Harbor Tea Rooms", "6.25"],
    ["02/09/2026", "Pine Hollow Hardware", "41.00"],
]
CARD_SPEC: dict[str, Any] = {
    "date_column": 0,
    "date_format": "MM/DD/YYYY",
    "payee_column": 1,
    "amount_column": 2,
}


def card_document(
    factories: Factories, store: LocalFSBlobStore, household_id: uuid.UUID
) -> tuple[Document, Account]:
    """A card CSV with its stored table, and the card account it is for."""
    with factories.transaction() as db:
        card = make_account(db, household_id, type="credit_card")
    doc = document(
        factories,
        store,
        household_id,
        account_id=card.id,
        extraction=table_result(CARD_ROWS).model_dump(mode="json"),
    )
    return doc, card


def sign_item(
    factories: Factories, doc: Document, account: Account, scope: str, **facts: Any
) -> ReviewItem:
    """The importer's question about the sign of one statement of a table."""
    return item(
        factories,
        doc,
        ReviewKind.UNPARSED,
        {
            "reason": "mapping_required",
            "table": 0,
            "column_count": 3,
            "row_count": len(CARD_ROWS),
            "candidates": [],
            "suggested_spec": MappingSpec.model_validate(CARD_SPEC).model_dump(mode="json"),
            "auto_import": "sign_unverified",
            "account_id": str(account.id),
            "group": None,
            "transactions": len(CARD_ROWS) - 1,
            "scope": scope,
            **facts,
        },
    )


def on_trust_mapping(
    factories: Factories,
    household_id: uuid.UUID,
    *,
    used: int = 0,
    account: Account | None = None,
) -> ImportMapping:
    """A mapping saved from a suggestion on trust; ``used``: imports it has read."""
    with factories.transaction() as db:
        mapping = make_import_mapping(
            db, household_id, spec=CARD_SPEC, rows=CARD_ROWS, account=account
        )
        with household_context(db, household_id):
            mapping.sign_unverified = True
            mapping.use_count = used
            db.flush()
        return mapping


@pytest.mark.parametrize("saved", [True, False])
def test_sign_answer_for_the_layout_saves_the_mapping_and_imports(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore, saved: bool
) -> None:
    """Every statement of the table waited: the answer is the layout's sign, asked once."""
    owner = home["owner"]
    hh = hh_of(owner)
    doc, card = card_document(factories, store, hh)
    mapping = on_trust_mapping(factories, hh) if saved else None
    facts = {"mapping_id": str(mapping.id)} if mapping is not None else {}
    row = sign_item(factories, doc, card, "mapping", **facts)
    response = resolve(
        owner, row.id, {"kind": "unparsed", "action": "answer_sign", "positive_is": "money_out"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "resolved"
    resolution = response.json()["resolution"]
    assert resolution["action"] == "answer_sign"
    assert (resolution["sign"], resolution["scope"]) == ("outflow_positive", "mapping")
    if mapping is not None:
        assert resolution["mapping_id"] == str(mapping.id)
    stored = load(factories, hh, ImportMapping, uuid.UUID(resolution["mapping_id"]))
    assert stored.spec["sign"] == "outflow_positive"
    assert stored.sign_unverified is False
    (job,) = jobs_of(factories, hh, APPLY_MAPPING_JOB_TYPE)
    assert resolution["job_id"] == str(job.id)
    assert job.payload["mapping_id"] == str(stored.id)
    # The rows are written as spending, and nobody is asked again.
    assert run_jobs(factories, hh, APPLY_MAPPING_JOB_TYPE, done=set())
    with factories.transaction() as db:
        set_context(db, hh)
        amounts = sorted(
            db.execute(
                select(Transaction.amount).where(Transaction.source_document_id == doc.id)
            ).scalars()
        )
        waiting = list(
            db.execute(
                select(ReviewItem).where(
                    ReviewItem.document_id == doc.id,
                    ReviewItem.kind == ReviewKind.UNPARSED,
                    ReviewItem.state == ReviewItemState.OPEN,
                )
            ).scalars()
        )
    assert amounts == [Decimal("-41.000000"), Decimal("-18.400000"), Decimal("-6.250000")]
    assert waiting == []


def test_sign_answer_for_one_account_leaves_the_mapping(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    """The table's other accounts were imported: the answer is for this account only."""
    owner, member = home["owner"], home["member"]
    hh = hh_of(owner)
    doc, card = card_document(factories, store, hh)
    mapping = on_trust_mapping(factories, hh)
    row = sign_item(factories, doc, card, "account", group=1, mapping_id=str(mapping.id))
    # A member who may not edit the account is refused, and nothing changes.
    refused = resolve(
        member, row.id, {"kind": "unparsed", "action": "answer_sign", "positive_is": "money_out"}
    )
    assert refused.status_code in (403, 404)
    assert load(factories, hh, ReviewItem, row.id).state is ReviewItemState.OPEN
    response = resolve(
        owner, row.id, {"kind": "unparsed", "action": "answer_sign", "positive_is": "money_out"}
    )
    assert response.status_code == 200, response.text
    resolution = response.json()["resolution"]
    assert resolution["action"] == "answer_sign"
    assert resolution["sign"] == "outflow_positive"
    assert resolution["scope"] == "account"
    assert resolution["account_id"] == str(card.id)
    stored = load(factories, hh, ImportMapping, mapping.id)
    assert stored.spec == mapping.spec
    assert stored.sign_unverified is True
    (job,) = jobs_of(factories, hh, APPLY_MAPPING_JOB_TYPE)
    assert resolution["job_id"] == str(job.id)
    # The re-run names no mapping: no other account's question is answered by it.
    assert job.payload.get("mapping_id") is None
    # Money in is an answer too.
    again = sign_item(factories, doc, card, "account", group=1, mapping_id=str(mapping.id))
    response = resolve(
        owner, again.id, {"kind": "unparsed", "action": "answer_sign", "positive_is": "money_in"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["resolution"]["sign"] == "inflow_positive"


@pytest.mark.parametrize("bound", [False, True])
def test_sign_answer_leaves_a_mapping_that_already_imported(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore, bound: bool
) -> None:
    """The layout's mapping read another account's file, or is another account's (``bound``)."""
    owner = home["owner"]
    hh = hh_of(owner)
    doc, card = card_document(factories, store, hh)
    other = new_account(factories, hh, {}) if bound else None
    mapping = on_trust_mapping(factories, hh, used=0 if bound else 1, account=other)
    row = sign_item(factories, doc, card, "mapping", mapping_id=str(mapping.id))
    response = resolve(
        owner, row.id, {"kind": "unparsed", "action": "answer_sign", "positive_is": "money_out"}
    )
    assert response.status_code == 200, response.text
    resolution = response.json()["resolution"]
    assert (resolution["sign"], resolution["scope"]) == ("outflow_positive", "mapping")
    # The mapping other files were read with is as it was: still on trust, its sign unchanged.
    kept = load(factories, hh, ImportMapping, mapping.id)
    assert (kept.spec, kept.sign_unverified) == (mapping.spec, True)
    assert kept.account_id == (other.id if other is not None else None)
    assert resolution["mapping_id"] != str(mapping.id)
    answered = load(factories, hh, ImportMapping, uuid.UUID(resolution["mapping_id"]))
    assert answered.spec["sign"] == "outflow_positive"
    assert (answered.account_id, answered.sign_unverified) == (card.id, False)
    assert answered.header_fingerprint == kept.header_fingerprint
    (job,) = jobs_of(factories, hh, APPLY_MAPPING_JOB_TYPE)
    assert job.payload["mapping_id"] == str(answered.id)
    assert run_jobs(factories, hh, APPLY_MAPPING_JOB_TYPE, done=set())
    with factories.transaction() as db:
        set_context(db, hh)
        amounts = sorted(
            db.execute(
                select(Transaction.amount).where(Transaction.source_document_id == doc.id)
            ).scalars()
        )
    assert amounts == [Decimal("-41.000000"), Decimal("-18.400000"), Decimal("-6.250000")]
    # Another file of the layout that waited meanwhile: the same answer is not saved twice.
    later = sign_item(factories, doc, card, "mapping", mapping_id=str(mapping.id))
    response = resolve(
        owner, later.id, {"kind": "unparsed", "action": "answer_sign", "positive_is": "money_out"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["resolution"]["mapping_id"] == str(answered.id)
    with factories.transaction() as db:
        set_context(db, hh)
        assert len(list(db.execute(select(ImportMapping.id)).scalars())) == 2


def test_sign_answer_without_an_account_never_rewrites_a_used_mapping(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    """No account to keep the answer for, and the mapping read other files: nothing is changed."""
    owner = home["owner"]
    hh = hh_of(owner)
    doc, card = card_document(factories, store, hh)
    mapping = on_trust_mapping(factories, hh, used=3)
    row = sign_item(factories, doc, card, "mapping", mapping_id=str(mapping.id), account_id=None)
    response = resolve(
        owner, row.id, {"kind": "unparsed", "action": "answer_sign", "positive_is": "money_out"}
    )
    assert response.status_code == 422, response.text
    assert problem_type(response) == "invalid-resolution"
    kept = load(factories, hh, ImportMapping, mapping.id)
    assert (kept.spec, kept.sign_unverified) == (mapping.spec, True)
    assert load(factories, hh, ReviewItem, row.id).state is ReviewItemState.OPEN
    assert jobs_of(factories, hh, APPLY_MAPPING_JOB_TYPE) == []
    with factories.transaction() as db:
        set_context(db, hh)
        assert len(list(db.execute(select(ImportMapping.id)).scalars())) == 1


def test_sign_answer_for_a_card_never_turns_the_other_accounts_files(
    import_env: structured.Env,
) -> None:
    """One layout, a checking account and a card: the card's answer is the card's alone.

    Through the real pipeline: the checking file imports with the layout's
    sign on trust; the card file of the same header waits; "money out"
    writes the card's rows as spending. The next checking file, and a
    re-run of the first, read as they did before.
    """
    env, layout = import_env, structured.TRUSTED_LAYOUT
    amounts_of, expected_of = structured._amounts, structured._expected

    def month(seed: int, start: date, end: date) -> SynthStatement:
        return layout_statement(
            statement(synth_ledger(seed, days=120), start, end, seed=seed), layout
        )

    checking = env.ledger.account(env.household_id, type="checking")
    card = env.ledger.account(env.household_id, type=AccountType.CREDIT_CARD)
    january = month(25, date(2026, 1, 1), date(2026, 1, 31))
    first = env.ingest(csv_bytes(january, layout), account=checking, name="a.csv")
    assert amounts_of(env.transactions(checking)) == expected_of(january)
    (mapping,) = structured._mappings(env)
    assert (mapping.sign_unverified, mapping.account_id) == (True, None)
    february = month(26, date(2026, 2, 1), date(2026, 2, 28))
    second = env.ingest(csv_bytes(february, layout), account=card, name="b.csv")
    (held,) = env.open_reviews(second)
    assert (held.payload["auto_import"], held.payload["scope"]) == ("sign_unverified", "mapping")
    assert held.payload["mapping_id"] == str(mapping.id)
    assert env.transactions(card) == []

    with Actors.open(env.settings, env.factories) as people:
        owner = people.login(env.owner)
        answered = resolve(
            owner,
            held.id,
            {"kind": "unparsed", "action": "answer_sign", "positive_is": "money_out"},
        )
        assert answered.status_code == 200, answered.text
        env.run(APPLY_MAPPING_JOB_TYPE)
        # The card's rows are spending: every sign of the file turned.
        assert amounts_of(env.transactions(card)) == sorted(
            (posted, -amount) for posted, amount in expected_of(february)
        )
        assert env.open_reviews(second) == []
        by_id = {m.id: m for m in structured._mappings(env)}
        assert len(by_id) == 2
        kept = by_id.pop(mapping.id)
        assert (kept.spec, kept.sign_unverified, kept.account_id) == (mapping.spec, True, None)
        (own,) = by_id.values()
        assert (own.account_id, own.sign_unverified) == (card.id, False)

        # Next month's checking file reads as January's did, and nobody is asked.
        march = month(27, date(2026, 3, 1), date(2026, 3, 31))
        third = env.ingest(csv_bytes(march, layout), account=checking, name="c.csv")
        assert env.open_reviews(third) == []
        assert amounts_of(env.transactions(checking)) == sorted(
            expected_of(january) + expected_of(march)
        )
        # January's document run again adds nothing and turns nothing.
        again = owner.post(f"{API}/imports/{first}/apply-mapping", json={})
        assert again.status_code == 202, again.text
        env.run(APPLY_MAPPING_JOB_TYPE)
        assert amounts_of(env.transactions(checking)) == sorted(
            expected_of(january) + expected_of(march)
        )
        # The card's next file is read with the card's answer: asked once.
        april = month(28, date(2026, 4, 1), date(2026, 4, 30))
        fourth = env.ingest(csv_bytes(april, layout), account=card, name="d.csv")
        assert env.open_reviews(fourth) == []
        assert amounts_of(env.transactions(card)) == sorted(
            (posted, -amount) for posted, amount in expected_of(february) + expected_of(april)
        )


def test_sign_answer_on_another_item_is_422(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    doc, card = card_document(factories, store, hh)
    columns = item(
        factories,
        doc,
        ReviewKind.UNPARSED,
        {"reason": "mapping_required", "auto_import": "not_confident"},
    )
    asked = sign_item(factories, doc, card, "mapping")
    not_asked = resolve(
        owner, columns.id, {"kind": "unparsed", "action": "answer_sign", "positive_is": "money_out"}
    )
    assert not_asked.status_code == 422
    assert problem_type(not_asked) == "invalid-resolution"
    no_answer = resolve(owner, asked.id, {"kind": "unparsed", "action": "answer_sign"})
    assert no_answer.status_code == 422
    # ``positive_is`` goes with the sign question only.
    stray = resolve(
        owner, asked.id, {"kind": "unparsed", "action": "reprocess", "positive_is": "money_out"}
    )
    assert stray.status_code == 422
    for row in (columns, asked):
        assert load(factories, hh, ReviewItem, row.id).state is ReviewItemState.OPEN
    assert jobs_of(factories, hh, APPLY_MAPPING_JOB_TYPE) == []
    assert jobs_of(factories, hh, INGEST_JOB_TYPE) == []


# ---------------------------------------------------------------------------
# possible_duplicate: several near duplicates of one statement, a skipped copy
# ---------------------------------------------------------------------------


def near_matches(
    factories: Factories,
    store: LocalFSBlobStore,
    household_id: uuid.UUID,
    account: Account,
    *,
    count: int = 3,
) -> tuple[Document, ReviewItem, list[Transaction], list[Transaction]]:
    """A statement entered by hand first: ``count`` imported rows, each beside a kept one."""
    doc = document(factories, store, household_id, account_id=account.id)
    kept: list[Transaction] = []
    imported: list[Transaction] = []
    with factories.transaction() as db:
        for index in range(count):
            amount = f"-{11 + index}.50"
            kept.append(
                make_transaction(
                    db,
                    account,
                    amount=amount,
                    raw_payee=f"Mill Pond Market {index}",
                    with_import_hash=True,
                )
            )
            imported.append(
                make_transaction(
                    db,
                    account,
                    amount=amount,
                    raw_payee=f"MILL POND MKT #{index}",
                    with_import_hash=True,
                    review_state=ReviewState.PENDING,
                    source_document_id=doc.id,
                )
            )
    row = item(
        factories,
        doc,
        ReviewKind.POSSIBLE_DUPLICATE,
        {
            "reason": "near_matches",
            "account_id": str(account.id),
            "statement": 0,
            "count": count,
            "rows": [
                {"transaction_id": str(new.id), "candidates": [str(old.id)]}
                for new, old in zip(imported, kept, strict=True)
            ],
        },
    )
    return doc, row, kept, imported


def test_near_matches_are_decided_in_one_request(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = hh_of(owner)
    account = new_account(factories, hh, {member.user.id: GrantLevel.EDIT})
    doc, row, kept, imported = near_matches(factories, store, hh, account)
    response = resolve(
        member,
        row.id,
        {
            "kind": "possible_duplicate",
            "decision": "confirm",
            "rows": [
                {"transaction_id": str(imported[0].id), "decision": "confirm"},
                {"transaction_id": str(imported[1].id), "decision": "reject"},
                {"transaction_id": str(imported[2].id), "decision": "confirm"},
            ],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "resolved"
    decided = response.json()["resolution"]["rows"]
    assert [(d["transaction_id"], d["decision"]) for d in decided] == [
        (str(imported[0].id), "confirm"),
        (str(imported[1].id), "reject"),
        (str(imported[2].id), "confirm"),
    ]
    assert load(factories, hh, Transaction, imported[0].id) is None
    assert load(factories, hh, Transaction, imported[2].id) is None
    assert load(factories, hh, Transaction, imported[1].id).review_state is ReviewState.CONFIRMED
    assert all(load(factories, hh, Transaction, old.id) is not None for old in kept)
    with factories.transaction() as db:
        set_context(db, hh)
        aliases = {
            (a.transaction_id, a.import_hash) for a in db.execute(select(ImportHashAlias)).scalars()
        }
    assert aliases == {
        (kept[0].id, imported[0].import_hash),
        (kept[2].id, imported[2].import_hash),
    }
    assert load(factories, hh, Document, doc.id).status is DocumentStatus.PROCESSED


def test_a_listed_row_that_resembles_a_removed_one_is_removed_again(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner, member = home["owner"], home["member"]
    hh = hh_of(owner)
    account = new_account(factories, hh, {member.user.id: GrantLevel.EDIT})
    _, row, kept, imported = near_matches(factories, store, hh, account)
    # The first listed row names no live row: it resembles one a person deleted.
    with factories.transaction() as db:
        set_context(db, hh)
        stored = db.get(ReviewItem, row.id)
        assert stored is not None
        rows = [dict(entry) for entry in stored.payload["rows"]]
        rows[0]["candidates"] = []
        stored.payload = {**stored.payload, "rows": rows}

    response = resolve(
        member,
        row.id,
        {
            "kind": "possible_duplicate",
            "decision": "confirm",
            "rows": [
                {"transaction_id": str(imported[0].id), "decision": "confirm"},
                {"transaction_id": str(imported[1].id), "decision": "confirm"},
                {"transaction_id": str(imported[2].id), "decision": "reject"},
            ],
        },
    )

    assert response.status_code == 200, response.text
    first, second, _ = response.json()["resolution"]["rows"]
    assert first["removed_again"] is True
    assert "removed_again" not in second
    assert load(factories, hh, Transaction, imported[0].id) is None
    assert load(factories, hh, Transaction, imported[1].id) is None
    assert load(factories, hh, Transaction, kept[0].id) is not None
    # Only the row removed again leaves a tombstone; the confirmed duplicate lives on in its twin.
    assert [t.import_hash for t in tombstones_of(factories, hh, account)] == [
        imported[0].import_hash
    ]


def test_near_matches_decided_in_part_keep_the_rest_open(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    account = new_account(factories, hh, {})
    doc, row, kept, imported = near_matches(factories, store, hh, account)
    first = resolve(
        owner,
        row.id,
        {
            "kind": "possible_duplicate",
            "decision": "confirm",
            "rows": [{"transaction_id": str(imported[1].id), "decision": "confirm"}],
        },
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["state"] == "open"
    assert body["resolution"] is None
    assert body["payload"]["count"] == 2
    assert [r["transaction_id"] for r in body["payload"]["rows"]] == [
        str(imported[0].id),
        str(imported[2].id),
    ]
    assert load(factories, hh, Transaction, imported[1].id) is None
    assert load(factories, hh, Document, doc.id).status is DocumentStatus.NEEDS_REVIEW
    # A row already decided is no longer one of the item's.
    gone = resolve(
        owner,
        row.id,
        {
            "kind": "possible_duplicate",
            "decision": "reject",
            "rows": [{"transaction_id": str(imported[1].id), "decision": "reject"}],
        },
    )
    assert gone.status_code == 422
    # One decision for every row still listed.
    rest = resolve(owner, row.id, {"kind": "possible_duplicate", "decision": "reject"})
    assert rest.status_code == 200, rest.text
    assert rest.json()["state"] == "resolved"
    for index in (0, 2):
        row_now = load(factories, hh, Transaction, imported[index].id)
        assert row_now.review_state is ReviewState.CONFIRMED
    assert all(load(factories, hh, Transaction, old.id) is not None for old in kept)
    assert load(factories, hh, Document, doc.id).status is DocumentStatus.PROCESSED


def test_a_decision_for_a_row_the_item_does_not_list_is_422(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    account = new_account(factories, hh, {})
    doc, row, kept, imported = near_matches(factories, store, hh, account)
    with factories.transaction() as db:
        other = make_transaction(
            db,
            account,
            amount="-3.00",
            raw_payee="Mill Pond Market other",
            with_import_hash=True,
            review_state=ReviewState.PENDING,
            source_document_id=doc.id,
        )
    for rows in (
        # A row of the document the item does not name; a kept row; a row named twice.
        [{"transaction_id": str(other.id), "decision": "confirm"}],
        [
            {"transaction_id": str(imported[0].id), "decision": "confirm"},
            {"transaction_id": str(kept[1].id), "decision": "confirm"},
        ],
        [{"transaction_id": str(imported[0].id), "decision": "confirm"}] * 2,
        [],
    ):
        response = resolve(
            owner, row.id, {"kind": "possible_duplicate", "decision": "confirm", "rows": rows}
        )
        assert response.status_code == 422, response.text
        assert problem_type(response) == "invalid-resolution"
    # Nothing was decided: not even the listed row named beside the stray one.
    assert load(factories, hh, ReviewItem, row.id).state is ReviewItemState.OPEN
    for row_id in (*(t.id for t in imported), *(t.id for t in kept), other.id):
        assert load(factories, hh, Transaction, row_id) is not None
    assert load(factories, hh, Transaction, imported[0].id).review_state is ReviewState.PENDING
    assert load(factories, hh, Document, doc.id).status is DocumentStatus.NEEDS_REVIEW


def skipped_copy(
    factories: Factories, store: LocalFSBlobStore, household_id: uuid.UUID
) -> tuple[Document, Document, ReviewItem]:
    """A byte-identical upload, skipped with a notice (``documents.service``)."""
    original = document(factories, store, household_id, status=DocumentStatus.PROCESSED)
    copy = document(factories, store, household_id, status=DocumentStatus.REJECTED)
    with factories.transaction() as db:
        set_context(db, household_id)
        stored = db.get(Document, copy.id)
        assert stored is not None
        stored.error = "duplicate"
    row = item(
        factories,
        copy,
        ReviewKind.POSSIBLE_DUPLICATE,
        {"reason": "same_content_skipped", "duplicate_of": str(original.id)},
    )
    return original, copy, row


def test_skipped_copy_can_be_imported_anyway(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    _, copy, row = skipped_copy(factories, store, hh)
    response = resolve(owner, row.id, {"kind": "possible_duplicate", "decision": "reject"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "resolved"
    stored = load(factories, hh, Document, copy.id)
    assert stored.status is DocumentStatus.RECEIVED
    assert stored.error is None
    (job,) = jobs_of(factories, hh, INGEST_JOB_TYPE)
    assert job.payload == {"document_id": str(copy.id)}
    assert response.json()["resolution"] == {"decision": "reject", "job_id": str(job.id)}


def test_skipped_copy_is_acknowledged(
    home: dict[str, AuthedClient], factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = home["owner"]
    hh = hh_of(owner)
    original, copy, row = skipped_copy(factories, store, hh)
    response = resolve(owner, row.id, {"kind": "possible_duplicate", "decision": "confirm"})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "resolved"
    assert response.json()["resolution"] == {
        "decision": "confirm",
        "acknowledged": True,
        "duplicate_of": str(original.id),
    }
    stored = load(factories, hh, Document, copy.id)
    assert (stored.status, stored.error) == (DocumentStatus.REJECTED, "duplicate")
    assert jobs_of(factories, hh, INGEST_JOB_TYPE) == []
    # Only the notice was closed: no ``document.updated`` says the file changed.
    updated = household_events(factories, hh, "document.updated")
    assert all(e.payload.get("id") != str(copy.id) for e in updated)
