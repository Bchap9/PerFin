# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /review/cards``: one card per document, with the one thing to do next.

Documents and items are made by hand (``tests.fixtures.documents``:
synthetic files in a blob store under ``tmp_path``) with the payload shapes
the importer writes; nothing here goes through the pipeline. A document
"with a stored bank statement" carries a hand-built extraction of one
statement, as the sandbox would have returned it.

* A document's open items are one card; the card names one action and the
  item that asks for it, in a fixed order (the account before the check,
  the sign question before the column mapper).
* Notices alone only ask to be seen; a resolved item is not in a card.
* Pages count documents; an item of a document the caller cannot see makes
  no card for them.
* A file nothing can be done about is acknowledged, never sent through the
  pipeline again; a receipt its parser could not read is never offered the
  column mapper.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from app.auth.models import GrantLevel
from app.core import clock
from app.core.settings import Settings
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
from app.ingestion.sandbox.protocol import ExtractResult, Statement, StatementAccount
from tests.fixtures.documents import make_document, make_review_item
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors
from tests.fixtures.ledger import make_account

pytestmark = pytest.mark.integration

CARDS = f"{API}/review/cards"
PDF = b"%PDF-1.7\n% synthetic placeholder, never parsed by the API\n%%EOF\n"


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


def hh_of(user: AuthedClient) -> uuid.UUID:
    household_id = user.record.household_id
    assert household_id is not None
    return household_id


def bank_statement() -> dict[str, Any]:
    """A stored extraction holding one bank statement (no rows are needed here)."""
    result = ExtractResult(
        kind="pdf",
        ok=True,
        statements=[
            Statement(source_format="pdf", account=StatementAccount(last4="4410", currency="USD"))
        ],
    )
    return result.model_dump(mode="json")


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


def pdf(
    factories: Factories, store: LocalFSBlobStore, household_id: uuid.UUID, **kwargs: Any
) -> Document:
    return document(
        factories,
        store,
        household_id,
        data=PDF,
        mime="application/pdf",
        filename="statement.pdf",
        **kwargs,
    )


def item(
    factories: Factories, doc: Document, kind: ReviewKind, payload: dict[str, Any]
) -> ReviewItem:
    with factories.transaction() as db:
        return make_review_item(db, doc, kind, payload)


def cards(user: AuthedClient, **params: Any) -> dict[str, Any]:
    response = user.get(CARDS, params=params)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def only_card(user: AuthedClient) -> dict[str, Any]:
    page = cards(user)
    assert len(page["cards"]) == 1, page
    card: dict[str, Any] = page["cards"][0]
    return card


def rows_listed() -> list[dict[str, Any]]:
    return [{"transaction_id": str(uuid.uuid4()), "fields": ["amount"]}]


# ---------------------------------------------------------------------------
# One card, one action
# ---------------------------------------------------------------------------


def test_a_document_with_three_findings_is_one_card(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = hh_of(owner)
    account = new_account(factories, hh, {})
    doc = pdf(factories, store, hh, account_id=account.id, extraction=bank_statement())
    skipped = item(
        factories,
        doc,
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {"reason": "lines_skipped", "count": 2},
    )
    doubtful = item(
        factories,
        doc,
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {"reason": "low_confidence", "account_id": str(account.id), "transactions": rows_listed()},
    )
    failed = item(
        factories,
        doc,
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {
            "reason": "reconciliation_failed",
            "account_id": str(account.id),
            "statement": 0,
            "difference": "12.40",
        },
    )

    page = cards(owner)

    assert page["loose"] == []
    assert page["next_cursor"] is None
    (card,) = page["cards"]
    assert card["document"]["id"] == str(doc.id)
    assert [i["id"] for i in card["items"]] == [str(skipped.id), str(doubtful.id), str(failed.id)]
    assert card["action"] == "reconcile"
    assert card["action_item_id"] == str(failed.id)
    assert (card["open"], card["notices"]) == (3, 0)
    assert all(i["can_resolve"] for i in card["items"])


def test_skipped_lines_without_a_bank_statement_are_not_reconciled(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    """The negative of the rule above: no stored statement, nothing to reconcile against."""
    hh = hh_of(owner)
    doc = pdf(factories, store, hh)
    doubtful = item(
        factories,
        doc,
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {"reason": "low_confidence", "transactions": rows_listed()},
    )
    card = only_card(owner)
    assert (card["action"], card["action_item_id"]) == ("check_rows", str(doubtful.id))

    other = pdf(factories, store, hh)
    skipped = item(
        factories, other, ReviewKind.LOW_CONFIDENCE_FIELD, {"reason": "lines_skipped", "count": 1}
    )
    by_document = {c["document"]["id"]: c for c in cards(owner)["cards"]}
    # Nothing is listed to check and there is no statement: it can only be seen.
    assert by_document[str(other.id)]["action"] == "acknowledge"
    assert by_document[str(other.id)]["action_item_id"] == str(skipped.id)
    assert by_document[str(other.id)]["open"] == 1


def test_account_comes_before_reconciliation(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = hh_of(owner)
    doc = pdf(factories, store, hh, extraction=bank_statement())
    item(
        factories,
        doc,
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {"reason": "reconciliation_failed", "statement": 0, "difference": "3.00"},
    )
    unknown = item(
        factories,
        doc,
        ReviewKind.UNKNOWN_ACCOUNT,
        {"reason": "no_match", "target": "statement", "statement": 0},
    )

    card = only_card(owner)

    assert card["action"] == "choose_account"
    assert card["action_item_id"] == str(unknown.id)
    assert len(card["items"]) == 2


def test_sign_question_is_the_action(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = hh_of(owner)
    account = new_account(factories, hh, {})
    doc = document(factories, store, hh)
    sign = item(
        factories,
        doc,
        ReviewKind.UNPARSED,
        {
            "reason": "mapping_required",
            "table": 0,
            "column_count": 4,
            "row_count": 9,
            "candidates": [],
            "auto_import": "sign_unverified",
            "scope": "account",
            "account_id": str(account.id),
            "group": 1,
        },
    )
    card = only_card(owner)
    assert (card["action"], card["action_item_id"]) == ("answer_sign", str(sign.id))

    # The same reason without the sign doubt is a question about the columns.
    other = document(factories, store, hh)
    columns = item(
        factories,
        other,
        ReviewKind.UNPARSED,
        {"reason": "mapping_required", "table": 0, "candidates": [], "auto_import": None},
    )
    by_document = {c["document"]["id"]: c for c in cards(owner)["cards"]}
    assert by_document[str(other.id)]["action"] == "map_columns"
    assert by_document[str(other.id)]["action_item_id"] == str(columns.id)


def test_notices_alone_ask_only_to_be_acknowledged(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = hh_of(owner)
    doc = pdf(factories, store, hh, status=DocumentStatus.PROCESSED)
    figures = item(
        factories,
        doc,
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {"reason": "investment_figures_differ", "transactions": rows_listed()},
    )
    copy = document(factories, store, hh, status=DocumentStatus.REJECTED, area=BlobArea.QUARANTINE)
    skipped = item(
        factories,
        copy,
        ReviewKind.POSSIBLE_DUPLICATE,
        {"reason": "same_content_skipped", "duplicate_of": str(doc.id)},
    )

    by_document = {c["document"]["id"]: c for c in cards(owner)["cards"]}

    assert set(by_document) == {str(doc.id), str(copy.id)}
    for document_id, asked in ((doc.id, figures), (copy.id, skipped)):
        card = by_document[str(document_id)]
        assert card["action"] == "acknowledge"
        assert card["action_item_id"] == str(asked.id)
        assert (card["open"], card["notices"]) == (0, 1)

    # A notice never hides what waits on a person, whichever came first.
    waiting = item(factories, copy, ReviewKind.UNPARSED, {"reason": "import_pending"})
    card = {c["document"]["id"]: c for c in cards(owner)["cards"]}[str(copy.id)]
    assert (card["action"], card["action_item_id"]) == ("reprocess", str(waiting.id))
    assert (card["open"], card["notices"]) == (1, 1)


def test_a_duplicate_that_waits_is_decided_not_acknowledged(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = hh_of(owner)
    doc = document(factories, store, hh, area=BlobArea.QUARANTINE)
    same = item(
        factories,
        doc,
        ReviewKind.POSSIBLE_DUPLICATE,
        {"reason": "same_content", "duplicate_of": str(uuid.uuid4())},
    )
    card = only_card(owner)
    assert (card["action"], card["action_item_id"]) == ("confirm_duplicate", str(same.id))
    assert (card["open"], card["notices"]) == (1, 0)


# ---------------------------------------------------------------------------
# Pages, visibility, closed items
# ---------------------------------------------------------------------------


def test_cards_page_by_document(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = hh_of(owner)
    docs = [document(factories, store, hh) for _ in range(3)]
    for doc in docs:
        item(factories, doc, ReviewKind.UNPARSED, {"reason": "import_pending"})
        item(factories, doc, ReviewKind.UNPARSED, {"reason": "extraction_failed"})
    with factories.transaction() as db:
        set_context(db, hh)
        db.add(
            ReviewItem(
                household_id=hh, kind=ReviewKind.REJECTED_EMAIL, payload={"reason": "daily_digest"}
            )
        )
    ordered = sorted(str(doc.id) for doc in docs)

    first = cards(owner, limit=2)

    assert [c["document"]["id"] for c in first["cards"]] == ordered[:2]
    assert all(len(c["items"]) == 2 for c in first["cards"])
    assert first["next_cursor"] == ordered[1]
    assert [i["kind"] for i in first["loose"]] == ["rejected_email"]
    assert first["loose"][0]["document"] is None

    second = cards(owner, limit=2, cursor=first["next_cursor"])

    assert [c["document"]["id"] for c in second["cards"]] == ordered[2:]
    assert second["next_cursor"] is None
    assert second["loose"] == []  # the first page only
    assert owner.get(CARDS, params={"limit": 101}).status_code == 422


def test_items_of_documents_the_caller_cannot_see_make_no_card(
    actors: Actors, owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = hh_of(owner)
    member = actors.new(role="member", household=owner.record.household)
    viewer = actors.new(role="viewer", household=owner.record.household)
    granted = new_account(
        factories, hh, {member.user.id: GrantLevel.EDIT, viewer.user.id: GrantLevel.VIEW}
    )
    hidden = new_account(factories, hh, {})
    seen_doc = document(factories, store, hh, account_id=granted.id)
    hidden_doc = document(factories, store, hh, account_id=hidden.id)
    seen = item(factories, seen_doc, ReviewKind.UNPARSED, {"reason": "import_pending"})
    item(factories, hidden_doc, ReviewKind.UNPARSED, {"reason": "import_pending"})
    # A visible document, but the finding names an account the member cannot see.
    item(
        factories,
        seen_doc,
        ReviewKind.UNKNOWN_ACCOUNT,
        {"reason": "currency_mismatch", "account_id": str(hidden.id)},
    )
    with factories.transaction() as db:
        set_context(db, hh)
        db.add(
            ReviewItem(
                household_id=hh, kind=ReviewKind.REJECTED_EMAIL, payload={"reason": "daily_digest"}
            )
        )

    assert {c["document"]["id"] for c in cards(owner)["cards"]} == {
        str(seen_doc.id),
        str(hidden_doc.id),
    }
    assert len(cards(owner)["loose"]) == 1
    for user, may_resolve in ((member, True), (viewer, False)):
        page = cards(user)
        (card,) = page["cards"]
        assert card["document"]["id"] == str(seen_doc.id)
        assert [i["id"] for i in card["items"]] == [str(seen.id)]
        # The hidden finding does not choose the action either.
        assert (card["action"], card["action_item_id"]) == ("reprocess", str(seen.id))
        assert card["open"] == 1
        assert card["items"][0]["can_resolve"] is may_resolve
        assert page["loose"] == []  # items of no document are for owners and admins
    outsider = actors.new(role="owner")
    assert cards(outsider) == {"cards": [], "loose": [], "next_cursor": None}
    assert actors.anonymous().get(CARDS).status_code == 401


def test_resolved_items_are_not_in_the_card(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = hh_of(owner)
    doc = pdf(factories, store, hh, extraction=bank_statement())
    unknown = item(factories, doc, ReviewKind.UNKNOWN_ACCOUNT, {"reason": "no_match"})
    failed = item(
        factories,
        doc,
        ReviewKind.LOW_CONFIDENCE_FIELD,
        {"reason": "reconciliation_unavailable", "statement": 0},
    )
    done = document(factories, store, hh)
    closed = item(factories, done, ReviewKind.UNPARSED, {"reason": "import_pending"})
    with factories.transaction() as db:
        set_context(db, hh)
        for row_id, state in (
            (unknown.id, ReviewItemState.RESOLVED),
            (closed.id, ReviewItemState.DISMISSED),
        ):
            row = db.get(ReviewItem, row_id)
            assert row is not None
            row.state = state
            row.resolved_at = clock.utcnow()

    card = only_card(owner)  # the document whose every item is closed has no card

    assert card["document"]["id"] == str(doc.id)
    assert [i["id"] for i in card["items"]] == [str(failed.id)]
    assert (card["action"], card["action_item_id"]) == ("reconcile", str(failed.id))


# ---------------------------------------------------------------------------
# Files that were not read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reason", ["encrypted", "format_not_supported", "depth_limit", "container_incomplete"]
)
def test_encrypted_file_is_acknowledged_not_reprocessed(
    reason: str, owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = hh_of(owner)
    doc = pdf(factories, store, hh)
    locked = item(factories, doc, ReviewKind.UNPARSED, {"reason": reason})
    card = only_card(owner)
    assert (card["action"], card["action_item_id"]) == ("acknowledge", str(locked.id))
    # It waits on a person all the same: it is not a notice.
    assert (card["open"], card["notices"]) == (1, 0)


@pytest.mark.parametrize(
    ("reason", "mime", "area", "action"),
    [
        ("no_records", "application/pdf", BlobArea.PERMANENT, "teach"),
        ("no_rows", "image/png", BlobArea.PERMANENT, "teach"),
        # Nothing to show a person the page of: only the pipeline can try again.
        ("no_records", "text/csv", BlobArea.PERMANENT, "reprocess"),
        ("no_records", "application/pdf", BlobArea.QUARANTINE, "reprocess"),
        ("extraction_failed", "application/pdf", BlobArea.PERMANENT, "reprocess"),
        # A receipt, payslip, tax form or loan document its own parser could
        # not read is never offered the column mapper or the teach dialog.
        ("receipt_unreadable", "application/pdf", BlobArea.PERMANENT, "reprocess"),
        ("payslip_unreadable", "application/pdf", BlobArea.PERMANENT, "reprocess"),
        ("tax_form_unreadable", "application/pdf", BlobArea.PERMANENT, "reprocess"),
        ("loan_unreadable", "application/pdf", BlobArea.PERMANENT, "reprocess"),
        ("mapping_failed", "text/csv", BlobArea.PERMANENT, "map_columns"),
        ("activity_unreadable", "application/pdf", BlobArea.PERMANENT, "map_columns"),
    ],
)
def test_an_unread_file_is_taught_mapped_or_processed_again(
    reason: str,
    mime: str,
    area: BlobArea,
    action: str,
    owner: AuthedClient,
    factories: Factories,
    store: LocalFSBlobStore,
) -> None:
    hh = hh_of(owner)
    doc = document(factories, store, hh, data=PDF, mime=mime, filename=None, area=area)
    unread = item(factories, doc, ReviewKind.UNPARSED, {"reason": reason})
    card = only_card(owner)
    assert (card["action"], card["action_item_id"]) == (action, str(unread.id))


def test_other_findings_have_their_one_action(
    owner: AuthedClient, factories: Factories, store: LocalFSBlobStore
) -> None:
    hh = hh_of(owner)

    def action_of(kind: ReviewKind, payload: dict[str, Any], **kwargs: Any) -> str:
        doc = pdf(factories, store, hh, **kwargs)
        asked = item(factories, doc, kind, payload)
        card = {c["document"]["id"]: c for c in cards(owner, limit=100)["cards"]}[str(doc.id)]
        assert card["action_item_id"] == str(asked.id)
        action: str = card["action"]
        return action

    assert action_of(ReviewKind.UNKNOWN_OWNER, {"reason": "drop_folder"}) == "choose_owner"
    assert action_of(ReviewKind.RECEIPT_MATCH, {"candidates": []}) == "attach_receipt"
    assert (
        action_of(ReviewKind.POSSIBLE_DUPLICATE, {"reason": "near_matches", "count": 2})
        == "confirm_duplicate"
    )
    # Rows an earlier reading left behind are settled in the reconcile view,
    # with or without balances to check against.
    assert (
        action_of(ReviewKind.LOW_CONFIDENCE_FIELD, {"reason": "earlier_rows_kept", "count": 1})
        == "reconcile"
    )
    assert (
        action_of(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {"reason": "balance_mismatch", "transactions": rows_listed()},
            extraction=bank_statement(),
        )
        == "reconcile"
    )
    assert (
        action_of(
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {"reason": "year_assumed", "transactions": rows_listed()},
            extraction=bank_statement(),
        )
        == "check_rows"
    )
    # A balance-only plan statement that does not add up lists no row.
    assert (
        action_of(ReviewKind.LOW_CONFIDENCE_FIELD, {"reason": "plan_statement_does_not_add_up"})
        == "acknowledge"
    )
