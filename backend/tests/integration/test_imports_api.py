# SPDX-License-Identifier: AGPL-3.0-or-later
"""``/imports``: column mappings, the stored-extraction preview and re-runs.

Visibility and household scoping (SECURITY.md F3): a mapping tied to an
account the caller cannot see is ``404``, like another household's; a
document the caller cannot see is ``404 document-not-found``; an import
re-run needs ``edit`` on the target account. The end-to-end flow (upload,
worker, mapping, re-run) is in ``test_structured_import.py``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.models import AuditLog, Event, Job
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.documents.models import DocumentStatus, ReviewItem, ReviewItemState, ReviewKind
from app.domain.imports.schemas import APPLY_MAPPING_JOB_TYPE
from app.domain.transactions.models import Transaction
from app.ingestion.parsers.structured import header_fingerprint
from app.ingestion.sandbox.protocol import (
    Balance,
    ExtractedTransaction,
    ExtractResult,
    Statement,
    StatementAccount,
)
from tests.fixtures.factories import Factories, UserRecord
from tests.fixtures.households import Actors, problem_type
from tests.fixtures.imports import (
    SAMPLE_ROWS,
    SAMPLE_SPEC,
    make_extracted_document,
    make_import_mapping,
    run_jobs,
    table_result,
)
from tests.fixtures.ledger import LedgerFactories

pytestmark = pytest.mark.integration

API = "/api/v1"
MAPPINGS = f"{API}/imports/mappings"


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as opened:
        yield opened


def _document(
    factories: Factories, household_id: uuid.UUID, account: Account | None = None, **kwargs: Any
) -> uuid.UUID:
    with factories.transaction() as db:
        return make_extracted_document(db, household_id, account=account, **kwargs).id


def _mapping(
    factories: Factories, household_id: uuid.UUID, account: Account | None = None, **kwargs: Any
) -> uuid.UUID:
    with factories.transaction() as db:
        return make_import_mapping(db, household_id, account=account, **kwargs).id


def _hh(record: UserRecord) -> uuid.UUID:
    assert record.household_id is not None
    return record.household_id


def _member_of(actors: Actors, owner: UserRecord, role: str = "member") -> Any:
    return actors.new(role=role, household=owner.household)


# ---------------------------------------------------------------------------
# Mappings
# ---------------------------------------------------------------------------


def test_mapping_lifecycle(actors: Actors, factories: Factories, ledger: LedgerFactories) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner.record)
    account = ledger.account(household_id)
    institution = ledger.institution(household_id)
    document_id = _document(factories, household_id, account)
    created = owner.post(
        MAPPINGS,
        json={
            "name": "Synthetic bank CSV",
            "spec": SAMPLE_SPEC,
            "document_id": str(document_id),
            "account_id": str(account.id),
            "institution_id": str(institution.id),
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["header_fingerprint"] == header_fingerprint(SAMPLE_ROWS[0])
    assert body["spec"]["date_format"] == "MM/DD/YYYY"
    assert body["spec"]["sign"] == "inflow_positive"  # defaults are filled in
    assert (body["account_id"], body["institution_id"]) == (str(account.id), str(institution.id))
    assert body["created_by"] == str(owner.user.id)
    mapping_id = body["id"]

    listed = owner.get(MAPPINGS).json()
    assert [m["id"] for m in listed["items"]] == [mapping_id]
    assert listed["next_cursor"] is None
    assert owner.get(f"{MAPPINGS}/{mapping_id}").json()["name"] == "Synthetic bank CSV"

    changed = owner.patch(
        f"{MAPPINGS}/{mapping_id}",
        json={"name": "Renamed", "account_id": None, "header_fingerprint": "a" * 64},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["account_id"] is None
    assert changed.json()["header_fingerprint"] == "a" * 64
    # A document recomputes the fingerprint with the (new) spec.
    shifted = {**SAMPLE_SPEC, "skip_rows": 1, "has_header": False}
    again = owner.patch(
        f"{MAPPINGS}/{mapping_id}", json={"spec": shifted, "document_id": str(document_id)}
    )
    assert again.status_code == 200, again.text
    assert again.json()["header_fingerprint"] == header_fingerprint(
        SAMPLE_ROWS[1], has_header=False
    )

    assert owner.delete(f"{MAPPINGS}/{mapping_id}").status_code == 204
    gone = owner.get(f"{MAPPINGS}/{mapping_id}")
    assert gone.status_code == 404
    assert problem_type(gone) == "import-mapping-not-found"
    with factories.transaction() as db:
        set_context(db, household_id)
        actions = [
            a.action
            for a in db.execute(
                select(AuditLog)
                .where(AuditLog.target_type == "import_mapping")
                .order_by(AuditLog.seq)
            ).scalars()
        ]
        events = {e.type for e in db.execute(select(Event)).scalars()}
    assert actions == [
        "import_mapping.create",
        "import_mapping.update",
        "import_mapping.update",
        "import_mapping.delete",
    ]
    assert {
        "document.mapping_created",
        "document.mapping_updated",
        "document.mapping_deleted",
    } <= events


def test_mapping_validation_and_conflicts(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner.record)
    document_id = _document(factories, household_id)
    base = {"spec": SAMPLE_SPEC, "header_fingerprint": "b" * 64}
    assert owner.post(MAPPINGS, json={"name": "One", **base}).status_code == 201
    taken = owner.post(MAPPINGS, json={"name": "One", **base})
    assert taken.status_code == 409
    assert problem_type(taken) == "import-mapping-name-taken"
    wide = owner.post(
        MAPPINGS,
        json={
            "name": "Too wide",
            "spec": {**SAMPLE_SPEC, "balance_column": 9},
            "document_id": str(document_id),
        },
    )
    assert wide.status_code == 422
    assert problem_type(wide) == "import-mapping-mismatch"
    statement_doc = _document(factories, household_id, result=_ofx_result())
    no_table = owner.post(
        MAPPINGS, json={"name": "No table", "spec": SAMPLE_SPEC, "document_id": str(statement_doc)}
    )
    assert no_table.status_code == 422
    assert problem_type(no_table) == "import-no-table"
    unknown = owner.post(MAPPINGS, json={"name": "X", **base, "institution_id": str(uuid.uuid4())})
    assert unknown.status_code == 404
    assert problem_type(unknown) == "institution-not-found"
    bad = owner.post(
        MAPPINGS,
        json={"name": "Y", "header_fingerprint": "b" * 64, "spec": {"date_column": 0}},
    )
    assert bad.status_code == 422
    extra = owner.post(MAPPINGS, json={"name": "Z", **base, "use_count": 5})
    assert extra.status_code == 422


def test_mappings_follow_account_visibility(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner.record)
    member = _member_of(actors, owner.record)
    viewer = _member_of(actors, owner.record, role="viewer")
    hidden = ledger.account(household_id)
    viewable = ledger.account(household_id)
    editable = ledger.account(household_id)
    ledger.grant(household_id, member.user.id, viewable, GrantLevel.VIEW)
    ledger.grant(household_id, member.user.id, editable, GrantLevel.EDIT)
    ledger.grant(household_id, viewer.user.id, editable, GrantLevel.VIEW)
    on_hidden = _mapping(factories, household_id, hidden)
    on_viewable = _mapping(factories, household_id, viewable)
    on_editable = _mapping(factories, household_id, editable)
    shared = _mapping(factories, household_id)

    seen = {m["id"] for m in member.get(MAPPINGS).json()["items"]}
    assert seen == {str(on_viewable), str(on_editable), str(shared)}
    assert {m["id"] for m in owner.get(MAPPINGS).json()["items"]} == seen | {str(on_hidden)}
    for mapping_id in (on_hidden,):
        response = member.get(f"{MAPPINGS}/{mapping_id}")
        assert response.status_code == 404
        assert problem_type(response) == "import-mapping-not-found"
    # Changing needs edit on the mapping's account.
    for mapping_id in (on_hidden, on_viewable):
        response = member.patch(f"{MAPPINGS}/{mapping_id}", json={"name": "Mine now"})
        assert response.status_code == 404
        assert problem_type(response) == "import-mapping-not-found"
        assert member.delete(f"{MAPPINGS}/{mapping_id}").status_code == 404
    assert member.patch(f"{MAPPINGS}/{on_editable}", json={"name": "Edited"}).status_code == 200
    # Pointing a mapping at an account needs edit there.
    for account in (hidden, viewable):
        response = member.patch(f"{MAPPINGS}/{shared}", json={"account_id": str(account.id)})
        assert response.status_code == 404
        assert problem_type(response) == "account-not-found"
        response = member.post(
            MAPPINGS,
            json={
                "name": f"On {account.id}",
                "spec": SAMPLE_SPEC,
                "header_fingerprint": "c" * 64,
                "account_id": str(account.id),
            },
        )
        assert response.status_code == 404
    # Viewers read, never write.
    assert viewer.get(f"{MAPPINGS}/{on_editable}").status_code == 200
    refused = viewer.post(
        MAPPINGS, json={"name": "V", "spec": SAMPLE_SPEC, "header_fingerprint": "d" * 64}
    )
    assert refused.status_code == 403


def test_other_households_never_see_mappings_or_documents(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    victim = actors.new(role="owner")
    household_id = _hh(victim.record)
    mapping_id = _mapping(factories, household_id)
    document_id = _document(factories, household_id, ledger.account(household_id))
    intruder = actors.new(role="owner")
    assert intruder.get(MAPPINGS).json()["items"] == []
    for method, url, body in (
        ("GET", f"{MAPPINGS}/{mapping_id}", None),
        ("PATCH", f"{MAPPINGS}/{mapping_id}", {"name": "pwned"}),
        ("DELETE", f"{MAPPINGS}/{mapping_id}", None),
    ):
        response = intruder.request(method, url, json=body)
        assert response.status_code == 404
        assert problem_type(response) == "import-mapping-not-found"
    for method, url in (
        ("GET", f"{API}/imports/{document_id}/preview"),
        ("POST", f"{API}/imports/{document_id}/apply-mapping"),
    ):
        response = intruder.request(method, url, json={} if method == "POST" else None)
        assert response.status_code == 404
        assert problem_type(response) == "document-not-found"
    # A mapping id of another household is not usable in a re-run either.
    own_doc = _document(factories, _hh(intruder.record), ledger.account(_hh(intruder.record)))
    response = intruder.post(
        f"{API}/imports/{own_doc}/apply-mapping", json={"mapping_id": str(mapping_id)}
    )
    assert response.status_code == 404
    assert problem_type(response) == "import-mapping-not-found"
    assert victim.get(f"{MAPPINGS}/{mapping_id}").status_code == 200


# ---------------------------------------------------------------------------
# Preview
# ---------------------------------------------------------------------------


def _ofx_result() -> ExtractResult:
    return ExtractResult(
        kind="ofx",
        ok=True,
        extractor_version="ofx/1",
        statements=[
            Statement(
                source_format="ofx",
                account=StatementAccount(
                    account_type="checking",
                    last4="4321",
                    currency="USD",
                    institution="Acme Synthetic Bank",
                ),
                period_start=date(2026, 1, 1),
                period_end=date(2026, 1, 31),
                ledger_balance=Balance(amount=Decimal("100.50"), as_of=date(2026, 1, 31)),
                transactions=[
                    ExtractedTransaction(
                        posted=date(2026, 1, 2 + i), amount=Decimal(f"-{i + 1}.25"), payee=f"P{i}"
                    )
                    for i in range(5)
                ],
            )
        ],
    )


def test_preview_shows_the_stored_table_matching_and_suggested_mapping(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner.record)
    account = ledger.account(household_id)
    document_id = _document(factories, household_id, account)
    first = owner.get(f"{API}/imports/{document_id}/preview?limit=2&offset=1")
    assert first.status_code == 200, first.text
    body = first.json()
    (table,) = body["tables"]
    assert table["rows"] == SAMPLE_ROWS[1:3]
    assert (table["row_count"], table["offset"], table["column_count"]) == (4, 1, 4)
    assert table["header_fingerprint"] == header_fingerprint(SAMPLE_ROWS[0])
    assert body["matched_mapping_id"] is None
    assert body["suggested_spec"]["date_column"] == 0
    assert body["suggested_spec"]["amount_column"] == 2
    assert body["suggested_spec"]["date_format"] == "MM/DD/YYYY"
    assert body["status"] == "needs_review"
    assert body["statements"] == []
    mapping_id = _mapping(factories, household_id)
    assert owner.get(f"{API}/imports/{document_id}/preview").json()["matched_mapping_id"] == str(
        mapping_id
    )
    too_many = owner.get(f"{API}/imports/{document_id}/preview?limit=501")
    assert too_many.status_code == 422


def test_preview_of_statements(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner.record)
    document_id = _document(factories, household_id, result=_ofx_result())
    body = owner.get(f"{API}/imports/{document_id}/preview?limit=3").json()
    (stmt,) = body["statements"]
    assert stmt["last4"] == "4321"
    assert stmt["institution"] == "Acme Synthetic Bank"
    assert stmt["transaction_count"] == 5
    assert [t["amount"] for t in stmt["transactions"]] == ["-1.25", "-2.25", "-3.25"]
    assert stmt["ledger_balance"] == {"amount": "100.50", "currency": "USD", "as_of": "2026-01-31"}
    assert body["tables"] == []
    assert body["suggested_spec"] is None


def test_preview_follows_document_visibility(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner.record)
    viewer = _member_of(actors, owner.record, role="viewer")
    hidden = _document(factories, household_id, ledger.account(household_id))
    granted_account = ledger.account(household_id)
    ledger.grant(household_id, viewer.user.id, granted_account, GrantLevel.VIEW)
    granted = _document(factories, household_id, granted_account)
    assert viewer.get(f"{API}/imports/{granted}/preview").status_code == 200
    response = viewer.get(f"{API}/imports/{hidden}/preview")
    assert response.status_code == 404
    assert problem_type(response) == "document-not-found"


# ---------------------------------------------------------------------------
# Re-runs
# ---------------------------------------------------------------------------


def test_apply_mapping_queues_a_job_once_per_key(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner.record)
    account = ledger.account(household_id)
    document_id = _document(factories, household_id, account)
    mapping_id = _mapping(factories, household_id)
    url = f"{API}/imports/{document_id}/apply-mapping"
    headers = {"Idempotency-Key": "apply-once-0001"}
    first = owner.post(url, json={"mapping_id": str(mapping_id)}, headers=headers)
    assert first.status_code == 202, first.text
    replay = owner.post(url, json={"mapping_id": str(mapping_id)}, headers=headers)
    assert replay.status_code == 202
    assert replay.json() == first.json()
    assert first.json()["document_id"] == str(document_id)
    with factories.transaction() as db:
        set_context(db, household_id)
        jobs = list(db.execute(select(Job).where(Job.type == APPLY_MAPPING_JOB_TYPE)).scalars())
        audits = [
            a.after
            for a in db.execute(
                select(AuditLog).where(AuditLog.action == "document.import_requested")
            ).scalars()
        ]
    assert [str(j.id) for j in jobs] == [first.json()["job_id"]]
    assert jobs[0].payload == {
        "document_id": str(document_id),
        "mapping_id": str(mapping_id),
        "account_id": None,
        "group": None,
        "statement": None,
        "requested_by": str(owner.user.id),
    }
    assert len(audits) == 1


def test_apply_mapping_refusals(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner.record)
    member = _member_of(actors, owner.record)
    viewer = _member_of(actors, owner.record, role="viewer")
    account = ledger.account(household_id)
    ledger.grant(household_id, member.user.id, account, GrantLevel.VIEW)
    ledger.grant(household_id, viewer.user.id, account, GrantLevel.VIEW)
    document_id = _document(factories, household_id, account)
    url = f"{API}/imports/{document_id}/apply-mapping"
    # Not extracted yet.
    pending = _document(factories, household_id, account, status=DocumentStatus.RECEIVED)
    response = owner.post(f"{API}/imports/{pending}/apply-mapping", json={})
    assert response.status_code == 409
    assert problem_type(response) == "import-not-applicable"
    # A mapping that names columns the table does not have.
    wide = _mapping(
        factories,
        household_id,
        spec={**SAMPLE_SPEC, "memo_column": 7},
        rows=[[*SAMPLE_ROWS[0], "a", "b", "c", "d"]],
    )
    response = owner.post(url, json={"mapping_id": str(wide)})
    assert response.status_code == 422
    assert problem_type(response) == "import-mapping-mismatch"
    # A mapping for a statement document (no table).
    statement_doc = _document(factories, household_id, account, result=_ofx_result())
    response = owner.post(
        f"{API}/imports/{statement_doc}/apply-mapping",
        json={"mapping_id": str(_mapping(factories, household_id))},
    )
    assert response.status_code == 422
    assert problem_type(response) == "import-no-table"
    # The member sees the document but may not write to its account.
    response = member.post(url, json={})
    assert response.status_code == 404
    assert problem_type(response) == "account-not-found"
    assert viewer.post(url, json={}).status_code == 403
    # Unknown fields are refused.
    assert owner.post(url, json={"mapping_id": None, "force": True}).status_code == 422


def test_apply_mapping_on_a_tsv_result_with_table_and_rerun(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    """The re-run job reads the stored table (no file, no sandbox involved)."""
    owner = actors.new(role="owner")
    household_id = _hh(owner.record)
    account = ledger.account(household_id)
    document_id = _document(factories, household_id, account, result=table_result(kind="tsv"))
    _mapping(factories, household_id)
    assert owner.post(f"{API}/imports/{document_id}/apply-mapping", json={}).status_code == 202
    assert run_jobs(factories, household_id, APPLY_MAPPING_JOB_TYPE, done=set())
    preview = owner.get(f"{API}/imports/{document_id}/preview").json()
    assert preview["status"] == "processed"
    assert preview["last_import"]["imported"] == 3
    assert preview["last_import"]["snapshots"] == 1
    transactions = owner.get(f"{API}/transactions?account_id={account.id}").json()
    amounts = sorted(t["amount"]["amount"] for t in transactions["items"])
    assert amounts == sorted(["-42.10", "1500.00", "-30.00"])


def test_rerun_does_not_repeat_open_review_items(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    household_id = _hh(owner.record)
    account = ledger.account(household_id)
    rows = [*SAMPLE_ROWS, ["Total", "1427.90", ""]]
    document_id = _document(factories, household_id, account, result=table_result(rows))
    _mapping(factories, household_id)
    done: set[uuid.UUID] = set()
    for _ in range(2):
        assert owner.post(f"{API}/imports/{document_id}/apply-mapping", json={}).status_code == 202
        assert run_jobs(factories, household_id, APPLY_MAPPING_JOB_TYPE, done=done)
    with factories.transaction() as db:
        set_context(db, household_id)
        items = [
            (item.kind, item.payload, item.state)
            for item in db.execute(
                select(ReviewItem).where(ReviewItem.document_id == document_id)
            ).scalars()
        ]
        count = len(
            list(db.execute(select(Transaction).where(Transaction.account_id == account.id)))
        )
    assert items == [
        (
            ReviewKind.LOW_CONFIDENCE_FIELD,
            {"reason": "rows_skipped", "account_id": str(account.id), "count": 1, "rows": [4]},
            ReviewItemState.OPEN,
        )
    ]
    assert count == 3
    assert owner.get(f"{API}/imports/{document_id}/preview").json()["status"] == "needs_review"
