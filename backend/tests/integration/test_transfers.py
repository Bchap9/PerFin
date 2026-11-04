# SPDX-License-Identifier: AGPL-3.0-or-later
"""Transfer pairing (app.domain.transactions.transfers) and its endpoints.

Service tests run in the rolled-back ``db`` fixture with a real RLS context;
endpoint tests go through the app. All amounts and dates are synthetic.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel
from app.auth.principal import Principal, apply_rls_context
from app.core.errors import ProblemError
from app.core.settings import Settings
from app.db.models import Job
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.documents.models import DocumentStatus
from app.domain.transactions import transfers
from app.domain.transactions.models import Transaction
from app.domain.transactions.schemas import AUTO_PAIR_JOB_TYPE, AutoPairJob
from app.workers.registry import JobContext
from tests.fixtures.factories import Factories, UserRecord, grant_account, make_user
from tests.fixtures.households import API, Actors, household_audit, household_events, problem_type
from tests.fixtures.ledger import LedgerFactories, make_account, make_transaction

pytestmark = pytest.mark.integration

D = date(2026, 3, 10)
TXN = f"{API}/transactions"


def _principal(db: Session, record: UserRecord) -> Principal:
    principal = Principal(
        user=record.user, household_id=record.household_id, role=record.role, auth_method="session"
    )
    apply_rls_context(db, principal)
    return principal


def _hh(record: UserRecord) -> uuid.UUID:
    assert record.household_id is not None
    return record.household_id


def _group(db: Session, row: Transaction) -> uuid.UUID | None:
    db.refresh(row)
    return row.transfer_group_id


def _pair_ids(outcome: transfers.AutoPairOutcome) -> set[frozenset[uuid.UUID]]:
    return {frozenset((p.outflow_id, p.inflow_id)) for p in outcome.pairs}


@pytest.fixture
def owner(db: Session) -> UserRecord:
    return make_user(db, role="owner")


@pytest.fixture
def accounts(db: Session, owner: UserRecord) -> list[Account]:
    return [make_account(db, _hh(owner), name=f"Acct {i}") for i in range(4)]


# ---------------------------------------------------------------------------
# The matcher
# ---------------------------------------------------------------------------


def test_pairs_equal_and_opposite_within_window(
    db: Session, owner: UserRecord, accounts: list[Account]
) -> None:
    a, b, c, _ = accounts
    out = make_transaction(db, a, amount="-250.00", posted_date=D)
    inn = make_transaction(db, b, amount="250.00", posted_date=D + timedelta(days=2))
    # Not transfers: same account, same sign, unequal, outside the window, zero.
    make_transaction(db, a, amount="-300.00", posted_date=D)
    make_transaction(db, a, amount="300.00", posted_date=D)
    make_transaction(db, c, amount="-400.00", posted_date=D)
    make_transaction(db, b, amount="-400.00", posted_date=D)
    make_transaction(db, c, amount="-500.00", posted_date=D)
    make_transaction(db, b, amount="499.99", posted_date=D)
    make_transaction(db, c, amount="-80.00", posted_date=D)
    make_transaction(db, b, amount="80.00", posted_date=D + timedelta(days=4))
    make_transaction(db, a, amount="0", posted_date=D)
    make_transaction(db, b, amount="0", posted_date=D)
    p = _principal(db, owner)
    outcome = transfers.auto_pair(db, _hh(owner), principal=p)
    assert _pair_ids(outcome) == {frozenset((out.id, inn.id))}
    assert outcome.ambiguous == 0
    group = _group(db, out)
    assert group is not None
    assert _group(db, inn) == group
    # A second run finds nothing new.
    assert transfers.auto_pair(db, _hh(owner), principal=p).pairs == []


def test_wider_window_and_exact_bounds(
    db: Session, owner: UserRecord, accounts: list[Account]
) -> None:
    a, b, *_ = accounts
    out = make_transaction(db, a, amount="-80.00", posted_date=D)
    inn = make_transaction(db, b, amount="80.00", posted_date=D + timedelta(days=4))
    p = _principal(db, owner)
    assert transfers.auto_pair(db, _hh(owner), principal=p, window_days=3).pairs == []
    outcome = transfers.auto_pair(db, _hh(owner), principal=p, window_days=4)
    assert _pair_ids(outcome) == {frozenset((out.id, inn.id))}


def test_closest_date_wins(db: Session, owner: UserRecord, accounts: list[Account]) -> None:
    a, b, c, _ = accounts
    out = make_transaction(db, a, amount="-100.00", posted_date=D)
    near = make_transaction(db, b, amount="100.00", posted_date=D + timedelta(days=1))
    far = make_transaction(db, c, amount="100.00", posted_date=D + timedelta(days=3))
    outcome = transfers.auto_pair(db, _hh(owner), principal=_principal(db, owner))
    assert _pair_ids(outcome) == {frozenset((out.id, near.id))}
    assert _group(db, far) is None


def test_ambiguous_candidates_are_left_alone(
    db: Session, owner: UserRecord, accounts: list[Account]
) -> None:
    a, b, c, d = accounts
    out = make_transaction(db, a, amount="-100.00", posted_date=D)
    in_b = make_transaction(db, b, amount="100.00", posted_date=D)
    in_c = make_transaction(db, c, amount="100.00", posted_date=D)
    # Elsewhere, an unambiguous pair still goes through.
    out2 = make_transaction(db, d, amount="-7.00", posted_date=D)
    in2 = make_transaction(db, a, amount="7.00", posted_date=D)
    outcome = transfers.auto_pair(db, _hh(owner), principal=_principal(db, owner))
    assert _pair_ids(outcome) == {frozenset((out2.id, in2.id))}
    assert outcome.ambiguous == 1
    for row in (out, in_b, in_c):
        assert _group(db, row) is None


def test_identical_duplicates_pair_one_to_one(
    db: Session, owner: UserRecord, accounts: list[Account]
) -> None:
    """Two identical transfers the same day between the same accounts are interchangeable."""
    a, b, *_ = accounts
    outs = [make_transaction(db, a, amount="-50.00", posted_date=D) for _ in range(2)]
    ins = [make_transaction(db, b, amount="50.00", posted_date=D) for _ in range(2)]
    outcome = transfers.auto_pair(db, _hh(owner), principal=_principal(db, owner))
    assert len(outcome.pairs) == 2
    assert outcome.ambiguous == 0
    paired = {i for pair in _pair_ids(outcome) for i in pair}
    assert paired == {r.id for r in outs + ins}
    for pair in outcome.pairs:
        assert {pair.outflow_id, pair.inflow_id} & {r.id for r in outs}
        assert {pair.outflow_id, pair.inflow_id} & {r.id for r in ins}


def test_currency_must_match_for_auto_pairing(db: Session, owner: UserRecord) -> None:
    hh = _hh(owner)
    usd = make_account(db, hh, currency="USD")
    eur = make_account(db, hh, currency="EUR")
    make_transaction(db, usd, amount="-100.00", posted_date=D)
    make_transaction(db, eur, amount="100.00", posted_date=D)
    assert transfers.auto_pair(db, hh, principal=_principal(db, owner)).pairs == []


def test_already_linked_rows_are_not_repaired(
    db: Session, owner: UserRecord, accounts: list[Account]
) -> None:
    a, b, c, _ = accounts
    group = uuid.uuid4()
    make_transaction(db, a, amount="-10.00", posted_date=D, transfer_group_id=group)
    make_transaction(db, b, amount="10.00", posted_date=D, transfer_group_id=group)
    loose = make_transaction(db, c, amount="10.00", posted_date=D)
    assert transfers.auto_pair(db, _hh(owner), principal=_principal(db, owner)).pairs == []
    assert _group(db, loose) is None


def test_member_pairs_only_editable_accounts(db: Session, owner: UserRecord) -> None:
    hh = _hh(owner)
    member = make_user(db, role="member", household=owner.household)
    mine_a, mine_b = make_account(db, hh), make_account(db, hh)
    viewed, hidden = make_account(db, hh), make_account(db, hh)
    for acct in (mine_a, mine_b):
        grant_account(db, hh, member.id, acct.id, GrantLevel.EDIT)
    grant_account(db, hh, member.id, viewed.id, GrantLevel.VIEW)
    out = make_transaction(db, mine_a, amount="-30.00", posted_date=D)
    to_viewed = make_transaction(db, viewed, amount="30.00", posted_date=D)
    out2 = make_transaction(db, mine_a, amount="-40.00", posted_date=D)
    make_transaction(db, hidden, amount="40.00", posted_date=D)
    out3 = make_transaction(db, mine_a, amount="-60.00", posted_date=D)
    in3 = make_transaction(db, mine_b, amount="60.00", posted_date=D)
    outcome = transfers.auto_pair(db, hh, principal=_principal(db, member))
    assert _pair_ids(outcome) == {frozenset((out3.id, in3.id))}
    for row in (out, to_viewed, out2):
        assert _group(db, row) is None
    # Naming an account the member may only view is 404, like a hidden one.
    for acct in (viewed, hidden):
        with pytest.raises(ProblemError) as exc:
            transfers.auto_pair(db, hh, principal=_principal(db, member), account_ids=[acct.id])
        assert (exc.value.status, exc.value.slug) == (404, "account-not-found")
    # The system run (after an import) sees the whole household.
    set_context(db, hh)
    system = transfers.auto_pair(db, hh)
    assert _pair_ids(system) == {
        frozenset((out.id, to_viewed.id)),
        frozenset((out2.id, _only(db, hh, "40.00"))),
    }


def _only(db: Session, hh: uuid.UUID, amount: str) -> uuid.UUID:
    return db.execute(
        select(Transaction.id).where(
            Transaction.household_id == hh, Transaction.amount == Decimal(amount)
        )
    ).scalar_one()


def test_seeds_limit_what_is_paired(
    db: Session, owner: UserRecord, accounts: list[Account]
) -> None:
    a, b, c, d = accounts
    early_out = make_transaction(db, a, amount="-5.00", posted_date=D - timedelta(days=30))
    early_in = make_transaction(db, b, amount="5.00", posted_date=D - timedelta(days=30))
    late_out = make_transaction(db, c, amount="-6.00", posted_date=D)
    # The counterpart may fall just outside the date range (within the window).
    late_in = make_transaction(db, d, amount="6.00", posted_date=D + timedelta(days=2))
    p = _principal(db, owner)
    outcome = transfers.auto_pair(db, _hh(owner), principal=p, date_from=D, date_to=D)
    assert _pair_ids(outcome) == {frozenset((late_out.id, late_in.id))}
    outcome = transfers.auto_pair(db, _hh(owner), principal=p, account_ids=[b.id])
    assert _pair_ids(outcome) == {frozenset((early_out.id, early_in.id))}


def test_seed_by_source_document(db: Session, owner: UserRecord, accounts: list[Account]) -> None:
    from tests.fixtures.ledger import household_context

    a, b, c, d = accounts
    hh = _hh(owner)
    document_id = _document(db, hh)
    imported = make_transaction(
        db, a, amount="-9.00", posted_date=D, source_document_id=document_id
    )
    other_in = make_transaction(db, b, amount="9.00", posted_date=D)
    make_transaction(db, c, amount="-3.00", posted_date=D)
    make_transaction(db, d, amount="3.00", posted_date=D)
    with household_context(db, hh):
        outcome = transfers.auto_pair(db, hh, source_document_id=document_id)
    assert _pair_ids(outcome) == {frozenset((imported.id, other_in.id))}
    audits = [r for r in household_audit_db(db, hh) if r.action == "transaction.transfer_auto_pair"]
    assert len(audits) == 1
    assert audits[0].user_id is None
    assert audits[0].after is not None
    assert audits[0].after["paired"] == 1


def household_audit_db(db: Session, hh: uuid.UUID) -> list[Any]:
    from app.db.models import AuditLog

    set_context(db, hh)
    return list(
        db.execute(
            select(AuditLog).where(AuditLog.household_id == hh).order_by(AuditLog.seq)
        ).scalars()
    )


def _document(db: Session, hh: uuid.UUID) -> uuid.UUID:
    """A minimal document row (the FK target of ``source_document_id``)."""
    from app.domain.documents.models import (
        Blob,
        BlobArea,
        Document,
        DocumentKind,
        DocumentSource,
    )

    set_context(db, hh)
    blob = Blob(
        household_id=hh,
        store="local",
        key=uuid.uuid4().hex,
        area=BlobArea.PERMANENT,
        size=1,
        stored_size=1,
        enc_alg="test",
        key_version=1,
        nonce=b"\x00" * 24,
    )
    db.add(blob)
    db.flush()
    document = Document(
        household_id=hh,
        blob_id=blob.id,
        kind=DocumentKind.STATEMENT,
        status=DocumentStatus.PROCESSED,
        source=DocumentSource.UPLOAD,
        sha256="0" * 64,
        size=1,
        mime_detected="text/csv",
    )
    db.add(document)
    db.flush()
    return document.id


# ---------------------------------------------------------------------------
# Manual link / unlink
# ---------------------------------------------------------------------------


def test_link_rules(db: Session, owner: UserRecord) -> None:
    hh = _hh(owner)
    a, b = make_account(db, hh), make_account(db, hh)
    eur = make_account(db, hh, currency="EUR")
    out = make_transaction(db, a, amount="-100.00", posted_date=D)
    inn = make_transaction(db, b, amount="100.00", posted_date=D + timedelta(days=20))
    same_acct = make_transaction(db, a, amount="100.00", posted_date=D)
    same_sign = make_transaction(db, b, amount="-100.00", posted_date=D)
    unequal = make_transaction(db, b, amount="99.00", posted_date=D)
    fx_in = make_transaction(db, eur, amount="92.10", posted_date=D)
    p = _principal(db, owner)

    def refused(other: Transaction, status: int, slug: str) -> None:
        with pytest.raises(ProblemError) as exc:
            transfers.link(db, p, out, other.id, request=None)
        assert (exc.value.status, exc.value.slug) == (status, slug)

    refused(out, 422, "transfer-invalid")
    refused(same_acct, 422, "transfer-invalid")
    refused(same_sign, 422, "transfer-invalid")
    refused(unequal, 422, "transfer-invalid")
    # A manual link ignores the date window.
    group = transfers.link(db, p, out, inn.id, request=None)
    assert _group(db, inn) == group
    # Linking the same two again is a no-op; a third leg conflicts.
    assert transfers.link(db, p, out, inn.id, request=None) == group
    with pytest.raises(ProblemError) as exc:
        transfers.link(db, p, fx_in, out.id, request=None)
    assert (exc.value.status, exc.value.slug) == (409, "transfer-conflict")
    # Unlink dissolves both legs; then a cross-currency link (an exchange) is fine.
    assert transfers.unlink(db, p, inn, request=None) == sorted([out.id, inn.id])
    assert _group(db, out) is None
    fx_group = transfers.link(db, p, out, fx_in.id, request=None)
    assert _group(db, fx_in) == fx_group
    # Missing or foreign counterparts are 404.
    with pytest.raises(ProblemError) as exc:
        transfers.link(db, p, inn, uuid.uuid4(), request=None)
    assert (exc.value.status, exc.value.slug) == (404, "transaction-not-found")


def test_member_cannot_link_to_a_view_only_account(db: Session, owner: UserRecord) -> None:
    hh = _hh(owner)
    member = make_user(db, role="member", household=owner.household)
    mine, viewed = make_account(db, hh), make_account(db, hh)
    grant_account(db, hh, member.id, mine.id, GrantLevel.EDIT)
    grant_account(db, hh, member.id, viewed.id, GrantLevel.VIEW)
    out = make_transaction(db, mine, amount="-15.00", posted_date=D)
    inn = make_transaction(db, viewed, amount="15.00", posted_date=D)
    p = _principal(db, member)
    with pytest.raises(ProblemError) as exc:
        transfers.link(db, p, out, inn.id, request=None)
    assert (exc.value.status, exc.value.slug) == (404, "transaction-not-found")
    assert transfers.candidates(db, p, out) == []
    assert [r.id for r in transfers.candidates(db, _principal(db, owner), out)] == [inn.id]


# ---------------------------------------------------------------------------
# After imports: the job and the ingest hook
# ---------------------------------------------------------------------------


def test_request_auto_pair_enqueues_a_household_job(
    db: Session, owner: UserRecord, accounts: list[Account]
) -> None:
    hh = _hh(owner)
    set_context(db, hh)
    job_id = transfers.request_auto_pair(db, hh, account_ids=[accounts[0].id])
    job = db.get(Job, job_id)
    assert job is not None
    assert (job.type, job.household_id) == (AUTO_PAIR_JOB_TYPE, hh)
    assert AutoPairJob.model_validate(job.payload).account_ids == [accounts[0].id]


def _ctx(db: Session, hh: uuid.UUID, job_type: str) -> JobContext:
    return JobContext(
        job_id=uuid.uuid4(),
        job_type=job_type,
        attempt=1,
        max_attempts=3,
        household_id=hh,
        session=db,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def test_job_handler_pairs_the_household(
    db: Session, owner: UserRecord, accounts: list[Account]
) -> None:
    from app.workers.handlers.transfers import auto_pair_transfers

    a, b, *_ = accounts
    hh = _hh(owner)
    out = make_transaction(db, a, amount="-11.00", posted_date=D)
    inn = make_transaction(db, b, amount="11.00", posted_date=D)
    set_context(db, hh)
    auto_pair_transfers(_ctx(db, hh, AUTO_PAIR_JOB_TYPE), AutoPairJob())
    group = _group(db, out)
    assert group is not None
    assert _group(db, inn) == group


def test_after_import_run_pairs_only_accounts_the_uploader_may_edit(
    db: Session, owner: UserRecord, factories: Factories, tmp_path: Any
) -> None:
    # Review finding P2-SR-02: a member's import must not pair with (and so
    # reveal, or change) a transaction on an account hidden from the member.
    from app.domain.documents.blobstore import LocalFSBlobStore
    from app.workers.handlers.transfers import auto_pair_transfers
    from tests.fixtures.documents import make_document

    hh = _hh(owner)
    member = make_user(db, role="member", household=owner.household)
    mine, mine2, viewed, hidden = (make_account(db, hh) for _ in range(4))
    for acct in (mine, mine2):
        grant_account(db, hh, member.id, acct.id, GrantLevel.EDIT)
    grant_account(db, hh, member.id, viewed.id, GrantLevel.VIEW)
    store = LocalFSBlobStore(tmp_path / "documents", tmp_path / "quarantine")
    doc = make_document(db, store, factories.keyring, hh, account_id=mine.id, uploaded_by=member.id)
    to_hidden = make_transaction(
        db, mine, amount="-40.00", posted_date=D, source_document_id=doc.id
    )
    hidden_in = make_transaction(db, hidden, amount="40.00", posted_date=D)
    to_viewed = make_transaction(
        db, mine, amount="-30.00", posted_date=D, source_document_id=doc.id
    )
    make_transaction(db, viewed, amount="30.00", posted_date=D)
    out = make_transaction(db, mine, amount="-60.00", posted_date=D, source_document_id=doc.id)
    inn = make_transaction(db, mine2, amount="60.00", posted_date=D)
    set_context(db, hh)
    auto_pair_transfers(_ctx(db, hh, AUTO_PAIR_JOB_TYPE), AutoPairJob(source_document_id=doc.id))
    assert _group(db, out) is not None
    assert _group(db, inn) == _group(db, out)
    for row in (to_hidden, hidden_in, to_viewed):
        assert _group(db, row) is None
    # The same document uploaded by the owner pairs across the household.
    doc.uploaded_by = owner.id
    db.flush()
    auto_pair_transfers(_ctx(db, hh, AUTO_PAIR_JOB_TYPE), AutoPairJob(source_document_id=doc.id))
    assert _group(db, to_hidden) is not None
    assert _group(db, hidden_in) == _group(db, to_hidden)


@pytest.mark.parametrize(
    ("status", "enqueued"),
    [
        (DocumentStatus.PROCESSED, True),
        (DocumentStatus.NEEDS_REVIEW, True),
        (DocumentStatus.FAILED, False),
        (None, False),
    ],
)
def test_ingest_enqueues_auto_pair(
    db: Session,
    owner: UserRecord,
    monkeypatch: pytest.MonkeyPatch,
    status: DocumentStatus | None,
    enqueued: bool,
) -> None:
    from app.domain.documents.schemas import INGEST_JOB_TYPE, IngestDocumentPayload
    from app.ingestion import pipeline
    from app.workers.handlers import ingest

    hh = _hh(owner)
    document_id = uuid.uuid4()
    monkeypatch.setattr(pipeline, "ingest_document", lambda *a, **k: status)
    monkeypatch.setattr(ingest, "default_deps", lambda: None)
    set_context(db, hh)
    ingest.ingest_document(
        _ctx(db, hh, INGEST_JOB_TYPE), IngestDocumentPayload(document_id=document_id)
    )
    jobs = list(
        db.execute(
            select(Job).where(Job.household_id == hh, Job.type == AUTO_PAIR_JOB_TYPE)
        ).scalars()
    )
    if not enqueued:
        assert jobs == []
        return
    assert len(jobs) == 1
    assert AutoPairJob.model_validate(jobs[0].payload).source_document_id == document_id


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
def api(
    settings: Settings, factories: Factories, ledger: LedgerFactories
) -> Iterator[dict[str, Any]]:
    with Actors.open(settings, factories) as actors:
        owner_rec = actors.user(role="owner")
        household = owner_rec.household
        assert household is not None
        member_rec = actors.user(role="member", household=household)
        mine = ledger.account(household.id)
        mine2 = ledger.account(household.id)
        hidden = ledger.account(household.id)
        ledger.grant(household.id, member_rec.id, mine, GrantLevel.EDIT)
        ledger.grant(household.id, member_rec.id, mine2, GrantLevel.EDIT)
        yield {
            "owner": actors.login(owner_rec),
            "member": actors.login(member_rec),
            "hh": household.id,
            "mine": mine,
            "mine2": mine2,
            "hidden": hidden,
            "ledger": ledger,
            "factories": factories,
        }


def test_link_unlink_endpoints(api: dict[str, Any]) -> None:
    ledger: LedgerFactories = api["ledger"]
    member, owner = api["member"], api["owner"]
    out = ledger.transaction(api["mine"], amount="-20.00", posted_date=D)
    inn = ledger.transaction(api["mine2"], amount="20.00", posted_date=D + timedelta(days=1))
    hidden_in = ledger.transaction(api["hidden"], amount="20.00", posted_date=D)
    candidates = member.get(f"{TXN}/{out.id}/transfer-candidates")
    assert [c["id"] for c in candidates.json()] == [str(inn.id)]
    response = member.post(f"{TXN}/{out.id}/transfer", json={"counterpart_id": str(hidden_in.id)})
    assert (response.status_code, problem_type(response)) == (404, "transaction-not-found")
    response = member.post(f"{TXN}/{out.id}/transfer", json={"counterpart_id": str(inn.id)})
    assert response.status_code == 200, response.text
    transfer = response.json()["transfer"]
    assert transfer["counterpart_id"] == str(inn.id)
    assert member.get(f"{TXN}/{inn.id}").json()["transfer"] == {
        "group_id": transfer["group_id"],
        "counterpart_id": str(out.id),
    }
    linked = member.get(TXN, params={"transfer": "true"}).json()["items"]
    assert {i["id"] for i in linked} == {str(out.id), str(inn.id)}
    # A linked leg keeps its amount.
    patch = member.patch(f"{TXN}/{out.id}", json={"amount": {"amount": "-21", "currency": "USD"}})
    assert (patch.status_code, problem_type(patch)) == (409, "transfer-conflict")
    assert member.delete(f"{TXN}/{inn.id}/transfer").status_code == 204
    assert owner.get(f"{TXN}/{out.id}").json()["transfer"] is None
    events = household_events(api["factories"], api["hh"], "transaction.transfer_unlinked")
    assert len(events) == 1
    assert len(household_audit(api["factories"], api["hh"], "transaction.transfer_link")) == 1


def test_hidden_counterpart_is_not_revealed(api: dict[str, Any]) -> None:
    ledger: LedgerFactories = api["ledger"]
    out = ledger.transaction(api["mine"], amount="-70.00", posted_date=D)
    hidden_in = ledger.transaction(api["hidden"], amount="70.00", posted_date=D)
    response = api["owner"].post(
        f"{TXN}/{out.id}/transfer", json={"counterpart_id": str(hidden_in.id)}
    )
    assert response.status_code == 200
    seen = api["member"].get(f"{TXN}/{out.id}").json()["transfer"]
    assert seen["counterpart_id"] is None
    assert seen["group_id"] == response.json()["transfer"]["group_id"]


def test_member_cannot_dissolve_a_transfer_with_a_leg_it_cannot_edit(
    api: dict[str, Any],
) -> None:
    # Review finding P2-SR-02: unlinking (or deleting the visible leg) would
    # clear transfer_group_id on the hidden account's transaction.
    ledger: LedgerFactories = api["ledger"]
    member, owner = api["member"], api["owner"]
    out = ledger.transaction(api["mine"], amount="-75.00", posted_date=D)
    hidden_in = ledger.transaction(api["hidden"], amount="75.00", posted_date=D)
    linked = owner.post(f"{TXN}/{out.id}/transfer", json={"counterpart_id": str(hidden_in.id)})
    assert linked.status_code == 200
    group = linked.json()["transfer"]["group_id"]
    for response in (
        member.delete(f"{TXN}/{out.id}/transfer"),
        member.delete(f"{TXN}/{out.id}"),
        member.post(f"{TXN}/bulk", json={"ids": [str(out.id)], "action": "delete"}),
    ):
        assert (response.status_code, problem_type(response)) == (409, "transfer-conflict")
    assert owner.get(f"{TXN}/{hidden_in.id}").json()["transfer"]["group_id"] == group
    assert owner.get(f"{TXN}/{out.id}").json()["transfer"]["group_id"] == group
    assert household_events(api["factories"], api["hh"], "transaction.transfer_unlinked") == []
    # An owner may.
    assert owner.delete(f"{TXN}/{out.id}/transfer").status_code == 204
    assert owner.get(f"{TXN}/{hidden_in.id}").json()["transfer"] is None


def test_delete_dissolves_the_transfer(api: dict[str, Any]) -> None:
    ledger: LedgerFactories = api["ledger"]
    out = ledger.transaction(api["mine"], amount="-33.00", posted_date=D)
    inn = ledger.transaction(api["mine2"], amount="33.00", posted_date=D)
    api["member"].post(f"{TXN}/{out.id}/transfer", json={"counterpart_id": str(inn.id)})
    assert api["member"].delete(f"{TXN}/{out.id}").status_code == 204
    assert api["member"].get(f"{TXN}/{inn.id}").json()["transfer"] is None


def test_auto_pair_endpoint(api: dict[str, Any]) -> None:
    ledger: LedgerFactories = api["ledger"]
    out = ledger.transaction(api["mine"], amount="-44.00", posted_date=D)
    inn = ledger.transaction(api["mine2"], amount="44.00", posted_date=D)
    ledger.transaction(api["hidden"], amount="-55.00", posted_date=D)
    ledger.transaction(api["mine2"], amount="55.00", posted_date=D)
    response = api["member"].post(f"{TXN}/transfers/auto-pair", json={})
    assert response.status_code == 200, response.text
    body = response.json()
    assert [set(p["transaction_ids"]) for p in body["paired"]] == [{str(out.id), str(inn.id)}]
    assert body["ambiguous"] == 0
    for bad in (
        {"window_days": 15},
        {"window_days": -1},
        {"date_from": "2026-02-02", "date_to": "2026-02-01"},
        {"account_ids": [str(api["hidden"].id)]},
    ):
        response = api["member"].post(f"{TXN}/transfers/auto-pair", json=bad)
        assert response.status_code in (404, 422), response.text
    events = household_events(api["factories"], api["hh"], "transaction.transfers_paired")
    assert [e.payload["paired"] for e in events] == [1]
