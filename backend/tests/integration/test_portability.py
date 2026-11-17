# SPDX-License-Identifier: AGPL-3.0-or-later
"""Full export and import end to end (docs/PORTABILITY.md; SECURITY.md C2, C3, C7, G1, G3).

The export is requested through the real app, built by the real job
handler into a blob store under ``tmp_path``, downloaded once, and imported
into a fresh household of another owner, whose ledger totals (reports,
balances, counts) must then equal the original's. Hostile archives are
derived from a real export (``tests.fixtures.portability.rezip``). All data
is synthetic.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select, text

from app.core import clock
from app.core.archive_crypto import (
    DEFAULT_KDF,
    STREAM_HEADER_BYTES,
    ArchiveHeader,
    KdfParams,
    decrypt_stream,
    is_encrypted_archive,
    new_salt,
)
from app.core.settings import Settings
from app.db.models import Job, JobState
from app.db.rls import set_context
from app.domain.accounts.balances import balances_as_of
from app.domain.accounts.models import Account
from app.domain.categorisation import service as categorisation_service
from app.domain.categorisation.models import MerchantMemory
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import Blob, Document, DocumentLink
from app.domain.portability import service
from app.domain.portability.entities import ENTITIES
from app.domain.portability.models import ArchiveState, PortabilityArchive
from app.domain.portability.schemas import (
    EXPIRE_JOB_TYPE,
    EXPORT_JOB_TYPE,
    IMPORT_JOB_TYPE,
    PASSPHRASE_HEADER,
)
from app.domain.portability.service import PortabilityDeps
from app.domain.transactions.import_hash import import_hash
from app.domain.transactions.models import (
    Category,
    Payee,
    Rule,
    ScheduledTransaction,
    ScheduleOrigin,
    Tag,
    Transaction,
    TransactionSplit,
    TransactionTag,
)
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import (
    API,
    Actors,
    household_audit,
    household_events,
    problem_type,
)
from tests.fixtures.ledger import (
    LedgerFactories,
    household_context,
    make_category,
    make_scheduled_transaction,
    make_transaction,
)
from tests.fixtures.portability import (
    CHEAP_KDF,
    DOCUMENT_BYTES,
    FORMULA_MEMO,
    FORMULA_NOTE,
    FORMULA_PAYEE,
    RichLedger,
    build_rich_ledger,
    make_ready_export,
    members_of,
    portability_deps,
    rezip,
    rows_of,
    run_portability_jobs,
    set_rows,
    zip_members,
)

pytestmark = pytest.mark.integration

EXPORT = f"{API}/export"
IMPORT = f"{API}/import"
PERIOD = {"date_from": "2025-01-01", "date_to": "2026-12-31"}


@pytest.fixture
def port_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={
            "documents_dir": tmp_path / "documents",
            "quarantine_dir": tmp_path / "quarantine",
            "import_max_mb": 8,
        }
    )


@pytest.fixture
def actors(port_settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(port_settings, factories) as opened:
        yield opened


@pytest.fixture
def store(port_settings: Settings) -> LocalFSBlobStore:
    return LocalFSBlobStore.from_settings(port_settings)


@pytest.fixture
def deps(store: LocalFSBlobStore, factories: Factories, port_settings: Settings) -> PortabilityDeps:
    return PortabilityDeps(
        store=store,
        keyring=factories.keyring,
        export_ttl=timedelta(minutes=60),
        import_max_bytes=port_settings.import_max_bytes,
    )


@pytest.fixture(autouse=True)
def cheap_kdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "EXPORT_KDF", CHEAP_KDF)


def household(user: AuthedClient) -> uuid.UUID:
    household_id = user.record.household_id
    assert household_id is not None
    return household_id


def files_in(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()) if root.exists() else []


class Jobs:
    def __init__(self, factories: Factories, deps: PortabilityDeps) -> None:
        self.factories = factories
        self.deps = deps
        self.done: set[uuid.UUID] = set()

    def run(self, household_id: uuid.UUID, job_type: str) -> list[uuid.UUID]:
        return run_portability_jobs(self.factories, household_id, job_type, self.deps, self.done)


@pytest.fixture
def jobs(factories: Factories, deps: PortabilityDeps) -> Jobs:
    return Jobs(factories, deps)


def export_archive(
    owner: AuthedClient, jobs: Jobs, *, passphrase: str | None = None
) -> tuple[dict[str, Any], bytes]:
    """Request, build and download an export; returns its status and the file."""
    body = {"passphrase": passphrase} if passphrase else {}
    queued = owner.post(EXPORT, json=body)
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]
    assert jobs.run(household(owner), EXPORT_JOB_TYPE)
    status = owner.get(f"{EXPORT}/{job_id}")
    assert status.status_code == 200, status.text
    assert status.json()["state"] == "ready", status.json()
    download = owner.get(f"{EXPORT}/{job_id}/download")
    assert download.status_code == 200, download.text
    return status.json(), download.content


def import_archive(
    owner: AuthedClient, jobs: Jobs, data: bytes, *, passphrase: str | None = None
) -> dict[str, Any]:
    headers = {PASSPHRASE_HEADER: passphrase} if passphrase else None
    queued = owner.post(
        IMPORT, files={"file": ("export.zip", data, "application/zip")}, headers=headers
    )
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]
    jobs.run(household(owner), IMPORT_JOB_TYPE)
    status = owner.get(f"{IMPORT}/{job_id}")
    assert status.status_code == 200, status.text
    out: dict[str, Any] = status.json()
    return out


def without_keys(value: Any) -> Any:
    """A report with the (household-specific) ids removed, items in a stable order."""
    if isinstance(value, dict):
        return {k: without_keys(v) for k, v in value.items() if k not in ("key",)}
    if isinstance(value, list):
        items = [without_keys(v) for v in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    return value


def reports(owner: AuthedClient) -> dict[str, Any]:
    out = {}
    for name, params in (
        ("spending", {"group_by": "category", "depth": 3}),
        ("spending_payee", {"group_by": "payee"}),
        ("spending_account", {"group_by": "account"}),
        ("income", {"group_by": "category", "depth": 3}),
        ("cashflow", {}),
    ):
        path = name.split("_")[0]
        response = owner.get(f"{API}/reports/{path}", params={**PERIOD, **params})
        assert response.status_code == 200, response.text
        out[name] = without_keys(response.json())
    return out


def ledger_fingerprint(factories: Factories, household_id: uuid.UUID) -> dict[str, Any]:
    """Per account (by name): type, currency, count, sum and balance; plus row counts."""
    with factories.transaction() as db:
        set_context(db, household_id)
        accounts = list(
            db.execute(select(Account).where(Account.household_id == household_id)).scalars()
        )
        balances = balances_as_of(db, accounts, date(2026, 12, 31))
        per_account: dict[str, Any] = {}
        for account in accounts:
            count, total = db.execute(
                select(func.count(), func.coalesce(func.sum(Transaction.amount), 0)).where(
                    Transaction.account_id == account.id
                )
            ).one()
            per_account[account.name] = {
                "type": str(account.type),
                "currency": account.currency,
                "count": int(count),
                "sum": Decimal(total),
                "balance": balances[account.id],
            }
        counts = {
            model.__tablename__: db.execute(
                select(func.count()).select_from(model).where(model.household_id == household_id)
            ).scalar_one()
            for model in (
                Transaction,
                TransactionSplit,
                TransactionTag,
                Category,
                Payee,
                Tag,
                Rule,
                Document,
                DocumentLink,
            )
        }
        groups = db.execute(
            select(func.count(func.distinct(Transaction.transfer_group_id))).where(
                Transaction.household_id == household_id
            )
        ).scalar_one()
    return {"accounts": per_account, "counts": counts, "transfer_groups": groups}


@pytest.fixture
def rich(
    actors: Actors,
    factories: Factories,
    ledger: LedgerFactories,
    store: LocalFSBlobStore,
) -> tuple[AuthedClient, RichLedger]:
    owner = actors.new(role="owner")
    return owner, build_rich_ledger(factories, ledger, store, household(owner))


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


def test_round_trip_into_a_fresh_household_keeps_every_total(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    store: LocalFSBlobStore,
    rich: tuple[AuthedClient, RichLedger],
) -> None:
    owner_a, _book = rich
    status, data = export_archive(owner_a, jobs)
    assert hashlib.sha256(data).hexdigest() == status["sha256"]
    assert len(data) == status["size"]
    assert status["counts"]["transactions"] == 23
    assert status["counts"]["document_files"] == 1

    owner_b = actors.new(role="owner")
    outcome = import_archive(owner_b, jobs, data)
    assert outcome["state"] == "imported", outcome
    assert outcome["summary"]["created"]["transactions"] == 23
    assert outcome["summary"]["import_hash_unrecoverable"] == 0

    assert reports(owner_b) == reports(owner_a)
    assert ledger_fingerprint(factories, household(owner_b)) == ledger_fingerprint(
        factories, household(owner_a)
    )
    # The uploaded archive is gone (key, row and file); only the document stays.
    with factories.transaction() as db:
        set_context(db, household(owner_b))
        blobs = db.execute(select(Blob).where(Blob.household_id == household(owner_b))).all()
        assert len(blobs) == 1
    assert files_in(port_settings_of(actors).quarantine_dir) == []


def port_settings_of(actors: Actors) -> Settings:
    settings = actors.app.state.settings
    assert isinstance(settings, Settings)
    return settings


def test_the_round_trip_compares_real_totals(rich: tuple[AuthedClient, RichLedger]) -> None:
    """Guards the round trip against a vacuous pass: the reports are not empty."""
    owner, _ = rich
    report = reports(owner)
    assert report["spending"]["items"]
    assert report["income"]["items"]
    assert report["cashflow"]["outflow"]["amount"] != "0.00"


def test_import_keeps_import_hash_dedupe_for_the_new_accounts(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    rich: tuple[AuthedClient, RichLedger],
) -> None:
    owner_a, book = rich
    _, data = export_archive(owner_a, jobs)
    owner_b = actors.new(role="owner")
    assert import_archive(owner_b, jobs, data)["state"] == "imported"
    with factories.transaction() as db:
        set_context(db, household(owner_b))
        card = db.execute(
            select(Account).where(
                Account.household_id == household(owner_b), Account.name == "Card"
            )
        ).scalar_one()
        hashes = set(
            db.execute(
                select(Transaction.import_hash).where(
                    Transaction.account_id == card.id, Transaction.import_hash.is_not(None)
                )
            ).scalars()
        )
    # What the deduplicator computes for the same statement rows imported again.
    expected = {
        import_hash(card.id, date(2026, 4, 2), Decimal("-12.00"), "COFFEE CART #12", index)
        for index in range(2)
    }
    assert hashes == expected
    assert all(row.import_hash not in hashes for row in book.hashed)


def test_passphrase_protected_round_trip(
    actors: Actors, factories: Factories, jobs: Jobs, rich: tuple[AuthedClient, RichLedger]
) -> None:
    owner_a, _ = rich
    passphrase = "correct horse battery staple"
    status, data = export_archive(owner_a, jobs, passphrase=passphrase)
    assert status["passphrase_protected"] is True
    assert is_encrypted_archive(data)
    assert not data.startswith(b"PK")
    plain = b"".join(decrypt_stream([data], passphrase))
    assert "manifest.json" in members_of(plain)

    owner_b = actors.new(role="owner")
    files = {"file": ("export.pfarch", data, "application/octet-stream")}
    missing = owner_b.post(IMPORT, files=files)
    assert (missing.status_code, problem_type(missing)) == (422, "passphrase-required")
    wrong = owner_b.post(IMPORT, files=files, headers={PASSPHRASE_HEADER: "not the passphrase"})
    assert (wrong.status_code, problem_type(wrong)) == (422, "archive-wrong-passphrase")
    outcome = import_archive(owner_b, jobs, data, passphrase=passphrase)
    assert outcome["state"] == "imported", outcome
    assert outcome["passphrase_protected"] is True
    assert reports(owner_b) == reports(owner_a)
    # The derived keys are gone once the jobs ran.
    for owner in (owner_a, owner_b):
        with factories.transaction() as db:
            set_context(db, household(owner))
            rows = db.execute(
                select(PortabilityArchive).where(
                    PortabilityArchive.household_id == household(owner)
                )
            ).scalars()
            assert all(row.passphrase_key is None for row in rows)


def test_an_archive_header_asking_for_more_memory_than_perfin_uses_is_refused(
    actors: Actors, port_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crafted header cannot make the API derive a key with up to 1 GiB of memory."""

    def no_derivation(*_: object, **__: object) -> bytes:
        raise AssertionError("the key was derived")

    monkeypatch.setattr(service, "derive_key", no_derivation)
    greedy = KdfParams(DEFAULT_KDF.opslimit, DEFAULT_KDF.memlimit * 2)
    header = ArchiveHeader(greedy, new_salt(), bytes(STREAM_HEADER_BYTES)).to_bytes()
    owner = actors.new(role="owner")
    response = owner.post(
        IMPORT,
        files={"file": ("export.pfarch", header + bytes(64), "application/octet-stream")},
        headers={PASSPHRASE_HEADER: "correct horse battery staple"},
    )
    assert (response.status_code, problem_type(response)) == (415, "unsupported-archive")
    assert files_in(port_settings.quarantine_dir) == []
    # What PerFin itself writes stays importable.
    assert DEFAULT_KDF.check(high=service.IMPORT_MAX_KDF) == DEFAULT_KDF


def test_export_is_encrypted_at_rest_and_downloads_once(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    port_settings: Settings,
    rich: tuple[AuthedClient, RichLedger],
) -> None:
    owner, _ = rich
    before = set(files_in(port_settings.documents_dir))
    job_id = owner.post(EXPORT, json={}).json()["job_id"]
    jobs.run(household(owner), EXPORT_JOB_TYPE)
    new_files = set(files_in(port_settings.documents_dir)) - before
    assert len(new_files) == 1
    stored = next(iter(new_files)).read_bytes()
    assert b"PK\x03\x04" not in stored
    assert b"manifest" not in stored
    assert DOCUMENT_BYTES not in stored

    first = owner.get(f"{EXPORT}/{job_id}/download")
    assert first.status_code == 200
    assert first.headers["content-type"] == "application/zip"
    assert first.headers["content-disposition"].startswith("attachment;")
    assert first.headers["x-content-type-options"] == "nosniff"
    assert "manifest.json" in members_of(first.content)
    # One-time: the key, the blob row and the file are gone.
    assert set(files_in(port_settings.documents_dir)) == before
    again = owner.get(f"{EXPORT}/{job_id}/download")
    assert (again.status_code, problem_type(again)) == (410, "export-already-downloaded")
    assert owner.get(f"{EXPORT}/{job_id}").json()["state"] == "downloaded"
    actions = [
        "export.request",
        "export.complete",
        "export.download",
    ]
    for action in actions:
        assert household_audit(factories, household(owner), action), action


def test_only_the_requester_sees_or_downloads_an_export(
    actors: Actors, factories: Factories, store: LocalFSBlobStore
) -> None:
    owner = actors.new(role="owner")
    hh = owner.record.household
    admin = actors.new(role="admin", household=hh)
    member = actors.new(role="member", household=hh)
    outsider = actors.new(role="owner")
    with factories.transaction() as db:
        archive = make_ready_export(db, store, factories.keyring, household(owner), owner.record.id)
    url = f"{EXPORT}/{archive.job_id}"
    for other in (admin, outsider):
        for path in (url, f"{url}/download"):
            response = other.get(path)
            assert (response.status_code, problem_type(response)) == (404, "export-not-found")
    refused = member.get(f"{url}/download")
    assert (refused.status_code, problem_type(refused)) == (403, "forbidden")
    assert owner.get(f"{url}/download").status_code == 200


def test_step_up_is_required(actors: Actors, factories: Factories, store: LocalFSBlobStore) -> None:
    stale = actors.new(role="owner", mfa_verified=False)
    response = stale.post(EXPORT, json={})
    assert (response.status_code, problem_type(response)) == (403, "step-up-required")
    with factories.transaction() as db:
        archive = make_ready_export(db, store, factories.keyring, household(stale), stale.record.id)
    download = stale.get(f"{EXPORT}/{archive.job_id}/download")
    assert (download.status_code, problem_type(download)) == (403, "step-up-required")
    upload = stale.post(
        IMPORT, files={"file": ("x.zip", b"PK\x05\x06" + bytes(18), "application/zip")}
    )
    assert (upload.status_code, problem_type(upload)) == (403, "step-up-required")


def test_expired_exports_are_destroyed(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    store: LocalFSBlobStore,
    port_settings: Settings,
) -> None:
    owner = actors.new(role="owner")
    with factories.transaction() as db:
        late = make_ready_export(
            db,
            store,
            factories.keyring,
            household(owner),
            owner.record.id,
            expires_in=-timedelta(seconds=1),
        )
    response = owner.get(f"{EXPORT}/{late.job_id}/download")
    assert (response.status_code, problem_type(response)) == (410, "export-expired")
    assert files_in(port_settings.documents_dir) == []

    job_id = owner.post(EXPORT, json={}).json()["job_id"]
    jobs.run(household(owner), EXPORT_JOB_TYPE)
    assert len(files_in(port_settings.documents_dir)) == 1
    with factories.transaction() as db:
        set_context(db, household(owner))
        row = db.execute(
            select(PortabilityArchive).where(PortabilityArchive.job_id == uuid.UUID(job_id))
        ).scalar_one()
        row.expires_at = clock.utcnow() - timedelta(seconds=1)
    jobs.run(household(owner), EXPIRE_JOB_TYPE)
    assert owner.get(f"{EXPORT}/{job_id}").json()["state"] == "expired"
    assert files_in(port_settings.documents_dir) == []
    assert household_audit(factories, household(owner), "export.expire")


def test_one_export_at_a_time_and_idempotency(actors: Actors) -> None:
    owner = actors.new(role="owner")
    key = {"Idempotency-Key": "export-once-1"}
    first = owner.post(EXPORT, json={}, headers=key)
    assert first.status_code == 202, first.text
    replay = owner.post(EXPORT, json={}, headers=key)
    assert replay.status_code == 202
    assert replay.json() == first.json()
    second = owner.post(EXPORT, json={})
    assert (second.status_code, problem_type(second)) == (409, "export-in-progress")


def _set_job(factories: Factories, household_id: uuid.UUID, job_id: str, state: str | None) -> None:
    """Put the archive's job in ``state`` (``None``: the row was pruned)."""
    with factories.transaction() as db:
        set_context(db, household_id)
        job = db.execute(select(Job).where(Job.id == uuid.UUID(job_id))).scalar_one()
        if state is None:
            db.delete(job)
        else:
            job.state = JobState(state)


def _archive(factories: Factories, household_id: uuid.UUID, job_id: str) -> PortabilityArchive:
    with factories.transaction() as db:
        set_context(db, household_id)
        row = db.execute(
            select(PortabilityArchive).where(PortabilityArchive.job_id == uuid.UUID(job_id))
        ).scalar_one()
        db.expunge(row)
        return row


@pytest.mark.parametrize("job_state", ["dead", None])
def test_a_dead_export_job_does_not_block_the_next_one(
    actors: Actors, factories: Factories, job_state: str | None
) -> None:
    owner = actors.new(role="owner")
    first = owner.post(EXPORT, json={"passphrase": "correct horse battery staple"})
    assert first.status_code == 202, first.text
    job_id = first.json()["job_id"]
    # Queued (even if not due yet): still in progress.
    blocked = owner.post(EXPORT, json={})
    assert (blocked.status_code, problem_type(blocked)) == (409, "export-in-progress")
    _set_job(factories, household(owner), job_id, job_state)
    again = owner.post(EXPORT, json={})
    assert again.status_code == 202, again.text
    stale = _archive(factories, household(owner), job_id)
    assert (stale.state, stale.error, stale.passphrase_key) == (
        ArchiveState.FAILED,
        "job-failed",
        None,
    )
    assert owner.get(f"{EXPORT}/{job_id}").json()["state"] == "failed"
    assert household_audit(factories, household(owner), "export.fail")


def test_a_dead_import_job_is_cleaned_up_without_a_new_request(
    actors: Actors,
    factories: Factories,
    port_settings: Settings,
    deps: PortabilityDeps,
    base_export: bytes,
) -> None:
    """The sweep fails the archive and shreds its upload even if nobody comes back."""
    import app.workers.handlers.portability as handlers

    owner = actors.new(role="owner")
    files = {"file": ("export.zip", base_export, "application/zip")}
    queued = owner.post(IMPORT, files=files)
    assert queued.status_code == 202, queued.text
    job_id = queued.json()["job_id"]
    assert files_in(port_settings.quarantine_dir)
    _set_job(factories, household(owner), job_id, "dead")
    with factories.transaction() as db:
        assert handlers.sweep_orphaned_archives(db, deps) >= 1
    stale = _archive(factories, household(owner), job_id)
    assert (stale.state, stale.error, stale.blob_id) == (ArchiveState.FAILED, "job-failed", None)
    assert files_in(port_settings.quarantine_dir) == []
    assert owner.post(IMPORT, files=files).status_code == 202


def test_the_sweep_job_fails_only_dead_archives_across_households(
    actors: Actors, factories: Factories, deps: PortabilityDeps
) -> None:
    """The instance-level handler, as the worker runs it (no household context)."""
    import app.workers.handlers.portability  # noqa: F401 - registers the handler
    from app.workers.registry import REGISTRY, JobContext

    dead_owner, live_owner = actors.new(role="owner"), actors.new(role="owner")
    dead = dead_owner.post(EXPORT, json={}).json()["job_id"]
    live = live_owner.post(EXPORT, json={}).json()["job_id"]
    _set_job(factories, household(dead_owner), dead, "dead")
    spec = REGISTRY.get("portability.sweep")
    assert spec is not None
    with factories.transaction() as db, portability_deps(deps):
        set_context(db, None)
        spec.func(
            JobContext(
                job_id=uuid.uuid4(),
                job_type="portability.sweep",
                attempt=1,
                max_attempts=spec.max_attempts,
                household_id=None,
                session=db,
                deadline=datetime.max.replace(tzinfo=UTC),
            ),
            spec.validate_payload({}),
        )
        # The handler leaves the worker's (empty) context behind it.
        assert db.execute(text("SELECT current_setting('perfin.household_id')")).scalar() == ""
    assert _archive(factories, household(dead_owner), dead).state is ArchiveState.FAILED
    assert _archive(factories, household(live_owner), live).state is ArchiveState.PENDING
    assert (live_owner.post(EXPORT, json={})).status_code == 409


def test_export_contents_and_csv_neutralisation(
    jobs: Jobs, rich: tuple[AuthedClient, RichLedger]
) -> None:
    owner, book = rich
    _, data = export_archive(owner, jobs)
    members = members_of(data)
    manifest = json.loads(members["manifest.json"])
    assert manifest["format"] == "perfin-export"
    assert manifest["format_version"] == 3
    assert manifest["household"]["base_currency"] == "USD"
    assert set(manifest["members"]) == set(members) - {"manifest.json"}
    for name, entry in manifest["members"].items():
        assert hashlib.sha256(members[name]).hexdigest() == entry["sha256"], name
    document_member = f"documents/{book.document.id}/March statement_.csv"
    assert members[document_member] == DOCUMENT_BYTES
    assert all(not name.startswith("/") and ".." not in name.split("/") for name in members)

    rows = list(csv.DictReader(io.StringIO(members["csv/transactions.csv"].decode())))
    formula = next(row for row in rows if row["id"] == str(book.formula.id))
    assert formula["memo"] == "'" + FORMULA_MEMO
    assert formula["raw_payee"] == "'" + FORMULA_PAYEE
    assert formula["notes"] == "'" + FORMULA_NOTE
    for row in rows:
        for cell in row.values():
            assert not cell.startswith(("=", "@", "\t", "\r")), cell
            if cell.startswith(("+", "-")):
                Decimal(cell)  # only plain numbers keep a leading sign
    split = next(row for row in rows if row["splits"])
    assert split["splits"] == "Housing / Rent: -150.000000; Food / Groceries: -50.000000"
    accounts = list(csv.DictReader(io.StringIO(members["csv/accounts.csv"].decode())))
    assert {row["name"] for row in accounts} >= {"Checking", "Card", "Euro", "Savings"}
    json_rows = rows_of(members, "transactions")
    assert len(json_rows) == 23
    assert any(row["memo"] == FORMULA_MEMO for row in json_rows)  # JSON keeps the raw value


def test_import_merges_into_the_existing_taxonomy(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    ledger: LedgerFactories,
    rich: tuple[AuthedClient, RichLedger],
) -> None:
    owner_a, _ = rich
    _, data = export_archive(owner_a, jobs)
    owner_b = actors.new(role="owner")
    hb = household(owner_b)
    ledger.seed_categories(hb)
    food = ledger.category(hb, name="Food")
    ledger.payee(hb, name="SHOP")
    ledger.tag(hb, name="Holiday")
    outcome = import_archive(owner_b, jobs, data)
    assert outcome["state"] == "imported", outcome
    merged = outcome["summary"]["merged"]
    assert merged["payees"] == 1
    assert merged["tags"] == 1
    assert merged["categories"] >= 1
    with factories.transaction() as db:
        set_context(db, hb)
        names = list(
            db.execute(
                select(Category.name).where(
                    Category.household_id == hb, Category.parent_id.is_(None)
                )
            ).scalars()
        )
        assert names.count("Food") == 1
        groceries = (
            db.execute(
                select(Category).where(Category.household_id == hb, Category.name == "Groceries")
            )
            .scalars()
            .all()
        )
        assert [g.parent_id for g in groceries if g.parent_id == food.id]
        assert (
            db.execute(
                select(func.count()).select_from(Payee).where(Payee.household_id == hb)
            ).scalar_one()
            == 2
        )  # SHOP (merged with Shop) and Diner
        assert (
            db.execute(
                select(func.count()).select_from(Tag).where(Tag.household_id == hb)
            ).scalar_one()
            == 2
        )


# ---------------------------------------------------------------------------
# Hostile and broken archives

# ---------------------------------------------------------------------------
# Archive format 3: how categories were decided, what was learned, series states
# ---------------------------------------------------------------------------

REFRESH_JOB = "categorise.refresh"
#: The facts of a series that format 3 added.
SERIES_FACTS = (
    "kind",
    "kind_source",
    "status",
    "status_source",
    "active",
    "merchant_key",
    "split_keys",
    "first_seen_on",
    "occurrences",
    "confidence",
    "last_amount",
    "previous_amount",
    "price_changed_on",
    "ended_on",
)


def as_format(data: bytes, version: int) -> bytes:
    """``data`` as an archive of an earlier format: without what came after it.

    Members of entities introduced later are removed and so is every column a
    later format added (``EntitySpec.added``); a format 1 archive cannot hold
    a document link to a version 2 target.
    """
    later = [spec for spec in ENTITIES if spec.since > version]
    kept_links: list[Any] = []

    def mutate(members: dict[str, bytes]) -> None:
        for spec in later:
            del members[spec.member]
        for spec in ENTITIES:
            dropped = {column for column, since in spec.added.items() if since > version}
            if spec.since > version or not dropped:
                continue
            rows = [
                {name: value for name, value in row.items() if name not in dropped}
                for row in rows_of(members, spec.name)
            ]
            set_rows(members, spec.name, rows)
        kept_links.extend(
            row
            for row in rows_of(members, "document_links")
            if version >= 2 or row["target_type"] in ("transaction", "account")
        )
        set_rows(members, "document_links", kept_links)

    def manifest(document: dict[str, Any]) -> None:
        document["format_version"] = version
        for spec in later:
            document["counts"].pop(spec.name, None)
        document["counts"]["document_links"] = len(kept_links)

    return rezip(data, mutate, manifest=manifest)


def plant_decisions(factories: Factories, rich: RichLedger) -> None:
    """A row a person chose, a suggestion, a rule's row linked to a series a
    person dismissed, a detected income series, and one learned merchant.

    Merchants, amounts and dates are invented.
    """
    book = rich.book
    hid = book.household_id
    with factories.transaction() as db, household_context(db, hid):
        rule = db.execute(
            select(Rule).where(Rule.household_id == hid, Rule.name == "Coffee")
        ).scalar_one()
        series = make_scheduled_transaction(
            db,
            book.card,
            name="Streamwell",
            origin=ScheduleOrigin.DETECTED,
            amount_min="-11.99",
            amount_max="-11.99",
        )
        series.kind = "subscription"
        series.status = "dismissed"
        series.status_source = "person"
        series.active = False
        series.merchant_key = "streamwell"
        # A re-key once split the merchant: the dismissal holds there too.
        series.split_keys = ["streamwell plus"]
        series.first_seen_on = date(2025, 11, 9)
        series.occurrences = 5
        series.confidence = Decimal("0.8700")
        series.last_amount = Decimal("-11.99")
        series.previous_amount = Decimal("-9.99")
        series.price_changed_on = date(2026, 2, 9)
        series.ended_on = date(2026, 4, 1)
        make_scheduled_transaction(
            db,
            book.checking,
            name="Pay run",
            origin=ScheduleOrigin.DETECTED,
            amount_min="2100.00",
            amount_max="2100.00",
        )
        db.flush()
        make_transaction(
            db,
            book.checking,
            posted_date=date(2026, 4, 6),
            amount="-23.40",
            raw_payee="CORNER GROCER 0417",
            category=book.groceries,
            merchant_key="corner grocer",
            category_source="person",
            category_confidence=Decimal("1"),
        )
        make_transaction(
            db,
            book.checking,
            posted_date=date(2026, 4, 7),
            amount="-18.00",
            raw_payee="SQ *LANTERN BOOKS",
            merchant_key="lantern books",
            suggested_category_id=book.restaurants.id,
            category_source="keyword",
            category_confidence=Decimal("0.6"),
        )
        make_transaction(
            db,
            book.card,
            posted_date=date(2026, 4, 9),
            amount="-11.99",
            raw_payee="STREAMWELL 8004",
            category=book.restaurants,
            merchant_key="streamwell",
            category_source="rule",
            category_confidence=Decimal("1"),
            category_rule_id=rule.id,
            scheduled_transaction_id=series.id,
        )
        db.add(
            MerchantMemory(
                household_id=hid,
                merchant_key="corner grocer",
                direction="out",
                category_id=book.groceries.id,
                decisions=3,
                last_decided_at=datetime(2026, 4, 6, 12, 30, tzinfo=UTC),
            )
        )
        db.flush()


def decisions_of(factories: Factories, household_id: uuid.UUID) -> dict[str, Any]:
    """What format 3 carries, with ids replaced by names (and the two links apart)."""
    with factories.transaction() as db:
        set_context(db, household_id)
        names: dict[uuid.UUID | None, str | None] = {
            row.id: row.name for row in db.execute(select(Category.id, Category.name))
        }
        names[None] = None
        split = set(db.execute(select(TransactionSplit.transaction_id)).scalars())
        rows = db.execute(select(Transaction).order_by(Transaction.posted_date)).scalars().all()
        transactions = sorted(
            (
                str(row.posted_date),
                str(row.amount),
                row.raw_payee or "",
                row.merchant_key or "",
                names[row.category_id] or "",
                names[row.suggested_category_id] or "",
                row.category_source or "",
                str(row.category_confidence),
                row.id in split,
            )
            for row in rows
        )
        links = sorted(
            (row.raw_payee or "", row.category_rule_id, row.scheduled_transaction_id)
            for row in rows
            if row.category_rule_id is not None or row.scheduled_transaction_id is not None
        )
        series = {
            row.name: {fact: getattr(row, fact) for fact in SERIES_FACTS}
            for row in db.execute(select(ScheduledTransaction)).scalars()
        }
        memory = sorted(
            (
                row.merchant_key,
                row.direction,
                names[row.category_id],
                row.decisions,
                row.last_decided_at,
            )
            for row in db.execute(select(MerchantMemory)).scalars()
        )
    return {"transactions": transactions, "links": links, "series": series, "memory": memory}


def refresh_jobs(factories: Factories, household_id: uuid.UUID) -> list[dict[str, Any]]:
    """The queued refreshes, by what they ask for (``force``).

    Who asked is left out: the payload has that field only where the
    worker's handler is loaded, which depends on the tests run before.
    """
    with factories.transaction() as db:
        set_context(db, household_id)
        return [
            {"force": payload["force"]}
            for payload in db.execute(
                select(Job.payload).where(Job.type == REFRESH_JOB, Job.household_id == household_id)
            ).scalars()
        ]


def test_format_3_round_trips_provenance_memory_and_series_states(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    rich: tuple[AuthedClient, RichLedger],
) -> None:
    owner_a, book = rich
    plant_decisions(factories, book)
    status, data = export_archive(owner_a, jobs)
    assert json.loads(members_of(data)["manifest.json"])["format_version"] == 3
    assert status["counts"]["merchant_memory"] == 1

    owner_b = actors.new(role="owner")
    outcome = import_archive(owner_b, jobs, data)
    assert outcome["state"] == "imported", outcome
    assert outcome["summary"]["created"]["merchant_memory"] == 1

    before = decisions_of(factories, household(owner_a))
    after = decisions_of(factories, household(owner_b))
    # The person's row, the suggestion and the rule's row say what they said.
    told = {row[2]: row[3:8] for row in after["transactions"]}
    assert told["CORNER GROCER 0417"] == ("corner grocer", "Groceries", "", "person", "1.0000")
    assert told["SQ *LANTERN BOOKS"] == ("lantern books", "", "Restaurants", "keyword", "0.6000")
    assert told["STREAMWELL 8004"] == ("streamwell", "Restaurants", "", "rule", "1.0000")
    # Equal, but for the merchant key the import worked out where the archive had none.
    assert [row[:3] + row[4:] for row in after["transactions"]] == [
        row[:3] + row[4:] for row in before["transactions"]
    ]
    for arrived, sent in zip(after["transactions"], before["transactions"], strict=True):
        assert arrived[3] == (sent[3] or arrived[3]), sent
    assert not [row for row in after["transactions"] if row[2] and not row[3]]
    # A series a person dismissed arrives dismissed, with its price history.
    assert after["series"]["Streamwell"] == {
        "kind": "subscription",
        "kind_source": "detector",
        "status": "dismissed",
        "status_source": "person",
        "active": False,
        "merchant_key": "streamwell",
        "split_keys": ["streamwell plus"],
        "first_seen_on": date(2025, 11, 9),
        "occurrences": 5,
        "confidence": Decimal("0.8700"),
        "last_amount": Decimal("-11.99"),
        "previous_amount": Decimal("-9.99"),
        "price_changed_on": date(2026, 2, 9),
        "ended_on": date(2026, 4, 1),
    }
    assert after["series"] == before["series"]
    assert after["memory"] == before["memory"]
    assert after["memory"] == [
        ("corner grocer", "out", "Groceries", 3, datetime(2026, 4, 6, 12, 30, tzinfo=UTC))
    ]
    assert reports(owner_b) == reports(owner_a)
    # Keys, links and events are derived again on the new instance.
    assert refresh_jobs(factories, household(owner_b)) == [{"force": True}]
    assert refresh_jobs(factories, household(owner_a)) == []


def test_rule_links_and_series_links_are_not_exported(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    rich: tuple[AuthedClient, RichLedger],
) -> None:
    owner_a, book = rich
    plant_decisions(factories, book)
    assert [row[0] for row in decisions_of(factories, household(owner_a))["links"]] == [
        "STREAMWELL 8004"
    ]
    _, data = export_archive(owner_a, jobs)
    exported = rows_of(members_of(data), "transactions")
    assert exported
    for row in exported:
        assert "category_rule_id" not in row
        assert "scheduled_transaction_id" not in row
        assert {"merchant_key", "category_source", "category_confidence"} <= set(row)
    assert "data/subscription_event.json" not in members_of(data)

    owner_b = actors.new(role="owner")
    assert import_archive(owner_b, jobs, data)["state"] == "imported"
    after = decisions_of(factories, household(owner_b))
    assert after["links"] == []
    # The row still says a rule decided it: nothing guesses over it.
    assert [row[6] for row in after["transactions"] if row[2] == "STREAMWELL 8004"] == ["rule"]


def test_a_format_2_archive_imports_with_legacy_sources(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    rich: tuple[AuthedClient, RichLedger],
) -> None:
    owner_a, book = rich
    plant_decisions(factories, book)
    _, data = export_archive(owner_a, jobs)
    old = as_format(data, 2)
    members = members_of(old)
    assert json.loads(members["manifest.json"])["format_version"] == 2
    assert "data/merchant_memory.json" not in members
    assert "category_source" not in rows_of(members, "transactions")[0]
    assert "status" not in rows_of(members, "scheduled_transactions")[0]

    owner_b = actors.new(role="owner")
    outcome = import_archive(owner_b, jobs, old)
    assert outcome["state"] == "imported", outcome
    assert outcome["summary"]["created"]["merchant_memory"] == 0
    assert reports(owner_b) == reports(owner_a)

    after = decisions_of(factories, household(owner_b))
    assert len(after["transactions"]) == 26
    sources = {"legacy": 0, "": 0}
    for row in after["transactions"]:
        _, _, _, merchant_key, category, suggested, source, confidence, split = row
        # As migration 0014 did for rows that were there before it.
        assert source == ("legacy" if category or split else ""), row
        assert (suggested, confidence) == ("", "None"), row
        # The archive names no merchant: the key is worked out from the descriptor.
        assert bool(merchant_key) == bool(row[2]), row
        sources[source] += 1
    keys = {row[2]: row[3] for row in after["transactions"]}
    assert keys["CORNER GROCER 0417"] == "corner grocer"
    assert keys["STREAMWELL 8004"] == "streamwell"
    assert sources["legacy"] >= 10
    assert sources[""] >= 3
    assert any(row[8] and not row[4] for row in after["transactions"])  # a split row

    def stands(name: str) -> tuple[Any, ...]:
        facts = after["series"][name]
        return (facts["kind"], facts["status"], facts["status_source"], facts["active"])

    # Entered by hand and on: a person confirmed it. Off: a person paused it
    # (the archive cannot say "dismissed"). Detected and on: still the detector's.
    assert stands("Rent") == ("other", "confirmed", "person", True)
    assert stands("Streamwell") == ("other", "paused", "person", False)
    assert stands("Pay run") == ("income", "active", "detector", True)
    for facts in after["series"].values():
        assert facts["kind_source"] == "detector"
        assert facts["occurrences"] == 0
        assert facts["merchant_key"] is None
        assert facts["split_keys"] == []
        assert facts["last_amount"] is None
    assert after["memory"] == []
    assert refresh_jobs(factories, household(owner_b)) == [{"force": True}]


def run_refresh(factories: Factories, household_id: uuid.UUID, *, force: bool) -> bool:
    """``categorise.refresh`` as the worker runs it; whether it did anything."""
    with factories.transaction() as db:
        set_context(db, household_id)
        return categorisation_service.refresh(db, household_id, force=force).ran


@pytest.mark.parametrize("version", [2, 3])
@pytest.mark.parametrize("up_to_date", [True, False])
def test_decided_rows_have_their_merchant_key_after_the_queued_refresh(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    rich: tuple[AuthedClient, RichLedger],
    version: int,
    up_to_date: bool,
) -> None:
    """Whatever the household's categorisation state was before the import.

    ``up_to_date``: the hourly dispatcher has been to the (empty) household,
    so the queued refresh computes no key again and seeds no memory.
    """
    owner_a, book = rich
    plant_decisions(factories, book)
    _, data = export_archive(owner_a, jobs)
    if version == 2:
        data = as_format(data, 2)
    owner_b = actors.new(role="owner")
    hb = household(owner_b)
    if up_to_date:
        assert run_refresh(factories, hb, force=False)
        assert not run_refresh(factories, hb, force=False)

    assert import_archive(owner_b, jobs, data)["state"] == "imported"
    assert refresh_jobs(factories, hb) == [{"force": True}]
    assert run_refresh(factories, hb, force=True)

    after = decisions_of(factories, hb)
    keys = {row[2]: row[3] for row in after["transactions"]}
    assert keys["CORNER GROCER 0417"] == "corner grocer"
    assert keys["STREAMWELL 8004"] == "streamwell"
    # Every row somebody decided can be found by its merchant (a merchant
    # rule matches it, the preview counts it).
    assert not [
        row
        for row in after["transactions"]
        if row[2] and not row[3] and row[6] in ("legacy", "person", "file", "rule", "payee")
    ]
    if version == 3:
        # What was learned is the archive's, and nothing is counted on top of it.
        assert after["memory"] == [
            ("corner grocer", "out", "Groceries", 3, datetime(2026, 4, 6, 12, 30, tzinfo=UTC))
        ]
        return
    # An older archive holds nothing learned: its categories count as
    # decisions, each row once, as the first refresh after the upgrade did.
    counted = [row for row in after["transactions"] if row[6] == "legacy" and row[3] and row[4]]
    assert len(counted) >= 5
    assert sum(entry[3] for entry in after["memory"]) == len(counted)
    assert ("corner grocer", "out", "Groceries", 1) in [entry[:4] for entry in after["memory"]]
    assert ("streamwell", "out", "Restaurants", 1) in [entry[:4] for entry in after["memory"]]

    # Negative: a second refresh, forced or not, counts nothing twice.
    run_refresh(factories, hb, force=True)
    assert decisions_of(factories, hb)["memory"] == after["memory"]


def test_an_older_archive_does_not_teach_over_what_the_household_learned(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    rich: tuple[AuthedClient, RichLedger],
) -> None:
    owner_a, book = rich
    plant_decisions(factories, book)
    _, data = export_archive(owner_a, jobs)
    owner_b = actors.new(role="owner")
    hb = household(owner_b)
    assert run_refresh(factories, hb, force=False)
    decided_at = datetime(2026, 5, 2, 9, 0, tzinfo=UTC)
    with factories.transaction() as db, household_context(db, hb):
        elsewhere = make_category(db, hb, name="Household supplies")
        db.add(
            MerchantMemory(
                household_id=hb,
                merchant_key="corner grocer",
                direction="out",
                category_id=elsewhere.id,
                decisions=2,
                last_decided_at=decided_at,
            )
        )
        db.flush()

    assert import_archive(owner_b, jobs, as_format(data, 2))["state"] == "imported"
    memory = decisions_of(factories, hb)["memory"]
    # The household's own answer for the merchant stands alone; others are seeded.
    assert [entry for entry in memory if entry[0] == "corner grocer"] == [
        ("corner grocer", "out", "Household supplies", 2, decided_at)
    ]
    assert ("streamwell", "out", "Restaurants", 1) in [entry[:4] for entry in memory]


def test_a_merchant_key_the_archive_gives_is_kept(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    rich: tuple[AuthedClient, RichLedger],
) -> None:
    owner_a, book = rich
    plant_decisions(factories, book)
    _, data = export_archive(owner_a, jobs)

    def change(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "transactions")
        for row in rows:
            if row["raw_payee"] == "STREAMWELL 8004":
                row["merchant_key"] = "streamwell plus"
        set_rows(members, "transactions", rows)

    owner_b = actors.new(role="owner")
    assert import_archive(owner_b, jobs, rezip(data, change))["state"] == "imported"
    keys = {row[2]: row[3] for row in decisions_of(factories, household(owner_b))["transactions"]}
    # Only a missing key is worked out; one the archive names is the archive's.
    assert keys["STREAMWELL 8004"] == "streamwell plus"
    assert keys["CORNER GROCER 0417"] == "corner grocer"


def test_a_format_2_archive_without_account_subtype(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    """A finding recorded, not fixed: such an archive is refused.

    ``account.subtype`` came with revision 0009, inside format 2 and without
    a format change, and the reader wants a row's fields to be exactly the
    columns of its format. A format 2 archive written before that revision
    has no ``subtype`` and is refused whole; nothing is imported. (The
    upgrade note says what to do: export again from an instance at 0009 or
    later.)
    """
    old = as_format(base_export, 2)
    assert all("subtype" in row for row in rows_of(members_of(old), "accounts"))

    def drop(members: dict[str, bytes]) -> None:
        rows = [
            {name: value for name, value in row.items() if name != "subtype"}
            for row in rows_of(members, "accounts")
        ]
        set_rows(members, "accounts", rows)

    outcome = assert_refused(
        actors, factories, jobs, port_settings, rezip(old, drop), "archive-invalid-value"
    )
    assert outcome["error_detail"] == "data/accounts.json: a row has unexpected fields"


@pytest.mark.parametrize(
    ("version", "column", "present"),
    [
        # An older archive that carries a column of a later format.
        (2, "category_source", True),
        (1, "merchant_key", True),
        # A format 3 archive that leaves one of its columns out.
        (3, "category_source", False),
        (3, "suggested_category_id", False),
    ],
)
def test_a_row_must_have_exactly_the_columns_of_its_format(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    port_settings: Settings,
    base_export: bytes,
    version: int,
    column: str,
    present: bool,
) -> None:
    def change(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "transactions")
        for row in rows:
            if present:
                row[column] = None
            else:
                del row[column]
        set_rows(members, "transactions", rows)

    data = rezip(as_format(base_export, version), change)
    outcome = assert_refused(actors, factories, jobs, port_settings, data, "archive-invalid-value")
    assert outcome["error_detail"] == "data/transactions.json: a row has unexpected fields"


def test_a_suggestion_must_name_a_category_of_the_archive(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    def dangle(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "transactions")
        row = next(row for row in rows if row["category_id"] is None)
        row["suggested_category_id"] = str(uuid.uuid4())
        set_rows(members, "transactions", rows)

    assert_refused(
        actors,
        factories,
        jobs,
        port_settings,
        rezip(base_export, dangle),
        "archive-reference-unknown",
    )


def test_an_unknown_source_or_a_suggestion_beside_a_category_is_refused(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    """The database's own rules hold for what an archive claims."""

    def unknown(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "transactions")
        rows[0]["category_source"] = "oracle"
        set_rows(members, "transactions", rows)

    def both(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "transactions")
        row = next(row for row in rows if row["category_id"] is not None)
        row["suggested_category_id"] = row["category_id"]
        set_rows(members, "transactions", rows)

    for mutate in (unknown, both):
        assert_refused(
            actors,
            factories,
            jobs,
            port_settings,
            rezip(base_export, mutate),
            "archive-inconsistent",
        )


def test_a_merchant_key_must_have_the_form_of_one(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    port_settings: Settings,
    rich: tuple[AuthedClient, RichLedger],
) -> None:
    owner_a, book = rich
    plant_decisions(factories, book)
    _, data = export_archive(owner_a, jobs)
    for entity in ("transactions", "scheduled_transactions", "merchant_memory"):

        def shout(members: dict[str, bytes], entity: str = entity) -> None:
            rows = rows_of(members, entity)
            row = next(row for row in rows if row["merchant_key"] is not None)
            row["merchant_key"] = "Streamwell  8004"
            set_rows(members, entity, rows)

        outcome = assert_refused(
            actors, factories, jobs, port_settings, rezip(data, shout), "archive-invalid-value"
        )
        assert outcome["error_detail"] == f"{entity}.merchant_key: not a merchant key"


def test_what_the_household_already_learned_wins_over_the_archive(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    rich: tuple[AuthedClient, RichLedger],
) -> None:
    """Importing the same archive again does not count a decision twice."""
    owner_a, book = rich
    plant_decisions(factories, book)
    _, data = export_archive(owner_a, jobs)
    owner_b = actors.new(role="owner")
    assert import_archive(owner_b, jobs, data)["state"] == "imported"
    again = import_archive(owner_b, jobs, data)
    assert again["state"] == "imported", again
    assert again["summary"]["skipped"]["merchant_memory"] == 1
    assert again["summary"]["created"]["merchant_memory"] == 0
    assert decisions_of(factories, household(owner_b))["memory"] == [
        ("corner grocer", "out", "Groceries", 3, datetime(2026, 4, 6, 12, 30, tzinfo=UTC))
    ]


# ---------------------------------------------------------------------------


@pytest.fixture
def base_export(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    ledger: LedgerFactories,
    store: LocalFSBlobStore,
) -> bytes:
    owner = actors.new(role="owner")
    build_rich_ledger(factories, ledger, store, household(owner))
    return export_archive(owner, jobs)[1]


def assert_refused(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    port_settings: Settings,
    data: bytes,
    slug: str,
) -> dict[str, Any]:
    owner = actors.new(role="owner")
    before = ledger_fingerprint(factories, household(owner))
    stored_before = files_in(port_settings.documents_dir)
    outcome = import_archive(owner, jobs, data)
    assert (outcome["state"], outcome["error"]) == ("failed", slug), outcome
    # All or nothing: not a row, and no stored file or blob is left behind.
    assert ledger_fingerprint(factories, household(owner)) == before
    with factories.transaction() as db:
        set_context(db, household(owner))
        assert (
            db.execute(
                select(func.count()).select_from(Blob).where(Blob.household_id == household(owner))
            ).scalar_one()
            == 0
        )
        assert (
            db.execute(
                select(func.count())
                .select_from(Account)
                .where(Account.household_id == household(owner))
            ).scalar_one()
            == 0
        )
    assert files_in(port_settings.quarantine_dir) == []
    assert files_in(port_settings.documents_dir) == stored_before
    assert household_audit(factories, household(owner), "import.fail")
    return outcome


def _bump_version(document: dict[str, Any]) -> None:
    document["format_version"] = 99


def test_unknown_format_version_is_refused(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    data = rezip(base_export, lambda members: None, manifest=_bump_version)
    outcome = assert_refused(
        actors, factories, jobs, port_settings, data, "archive-version-unsupported"
    )
    assert "99" in outcome["error_detail"]


def test_changed_member_is_refused(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    def tamper(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "transactions")
        rows[0]["amount"] = "-1000000.00"
        set_rows(members, "transactions", rows)

    data = rezip(base_export, tamper, fix_manifest=False)
    assert_refused(actors, factories, jobs, port_settings, data, "archive-hash-mismatch")


@pytest.mark.parametrize(
    "name",
    ["../evil.json", "/etc/passwd", "documents/..\\..\\evil", "C:/evil.txt", "data/./x.json"],
)
def test_unsafe_member_names_are_refused(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    port_settings: Settings,
    base_export: bytes,
    name: str,
) -> None:
    def add(members: dict[str, bytes]) -> None:
        members[name] = b"{}"

    assert_refused(
        actors, factories, jobs, port_settings, rezip(base_export, add), "archive-unsafe"
    )


def test_unknown_member_is_refused(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    def add(members: dict[str, bytes]) -> None:
        members["data/secrets.json"] = b"[]"

    assert_refused(
        actors, factories, jobs, port_settings, rezip(base_export, add), "archive-unexpected-member"
    )


def test_zip_bomb_is_refused_before_expansion(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    def add(members: dict[str, bytes]) -> None:
        members["documents/bomb.bin"] = bytes(64 * 1024 * 1024)

    data = rezip(base_export, add)
    assert len(data) < 8 * 1024 * 1024  # it fits the upload cap only because it is a bomb
    assert_refused(actors, factories, jobs, port_settings, data, "archive-unsafe")


def test_reference_outside_the_archive_is_refused(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    def dangle(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "transactions")
        rows[-1]["account_id"] = str(uuid.uuid4())
        set_rows(members, "transactions", rows)

    assert_refused(
        actors,
        factories,
        jobs,
        port_settings,
        rezip(base_export, dangle),
        "archive-reference-unknown",
    )


def test_a_late_failure_rolls_everything_back(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    """Accounts, transactions and the document original were written before the
    broken link: all of it is undone, including the stored document file."""

    def break_link(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "document_links")
        assert rows
        rows[0]["target_id"] = str(uuid.uuid4())
        set_rows(members, "document_links", rows)

    assert_refused(
        actors,
        factories,
        jobs,
        port_settings,
        rezip(base_export, break_link),
        "archive-reference-unknown",
    )


def test_database_rules_still_apply(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    def other_currency(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "transactions")
        rows[0]["currency"] = "JPY" if rows[0]["currency"] != "JPY" else "USD"
        set_rows(members, "transactions", rows)

    assert_refused(
        actors,
        factories,
        jobs,
        port_settings,
        rezip(base_export, other_currency),
        "archive-inconsistent",
    )


def test_markup_document_is_refused(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'

    def swap(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "documents")
        name = next(n for n in members if n.startswith(f"documents/{rows[0]['id']}/"))
        members[name] = svg
        rows[0]["sha256"] = hashlib.sha256(svg).hexdigest()
        rows[0]["size"] = len(svg)
        rows[0]["mime_detected"] = "text/csv"
        set_rows(members, "documents", rows)

    assert_refused(
        actors,
        factories,
        jobs,
        port_settings,
        rezip(base_export, swap),
        "archive-document-rejected",
    )


def test_bad_values_are_refused(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings, base_export: bytes
) -> None:
    def bad(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "transactions")
        rows[0]["amount"] = 12.5  # a float, not a decimal string
        set_rows(members, "transactions", rows)

    assert_refused(
        actors, factories, jobs, port_settings, rezip(base_export, bad), "archive-invalid-value"
    )


def test_damaged_zip_is_refused(
    actors: Actors, factories: Factories, jobs: Jobs, port_settings: Settings
) -> None:
    assert_refused(
        actors, factories, jobs, port_settings, b"PK\x03\x04" + bytes(200), "archive-not-zip"
    )


def test_other_files_are_refused_at_upload(actors: Actors, port_settings: Settings) -> None:
    owner = actors.new(role="owner")
    for content in (b"Date,Amount\n2026-01-01,1\n", b"%PDF-1.7\n", zip_members({})[:3]):
        response = owner.post(IMPORT, files={"file": ("x.csv", content, "text/csv")})
        assert (response.status_code, problem_type(response)) == (415, "unsupported-archive")
    assert files_in(port_settings.quarantine_dir) == []
    too_big = owner.post(
        IMPORT, files={"file": ("x.zip", b"PK\x03\x04" + bytes(9 * 1024 * 1024), "application/zip")}
    )
    assert too_big.status_code == 413
    assert files_in(port_settings.quarantine_dir) == []


def test_one_import_at_a_time(actors: Actors, base_export: bytes) -> None:
    owner = actors.new(role="owner")
    files = {"file": ("export.zip", base_export, "application/zip")}
    assert owner.post(IMPORT, files=files).status_code == 202
    second = owner.post(IMPORT, files=files)
    assert (second.status_code, problem_type(second)) == (409, "import-in-progress")


def test_import_is_audited_and_announced(
    actors: Actors, factories: Factories, jobs: Jobs, base_export: bytes
) -> None:
    owner = actors.new(role="owner")
    assert import_archive(owner, jobs, base_export)["state"] == "imported"
    for action in ("import.request", "import.complete"):
        rows = household_audit(factories, household(owner), action)
        assert rows, action
        assert rows[0].user_id == owner.record.id, action
    for type_ in (
        "import.complete",
        "transaction.imported",
        "account.imported",
        "document.imported",
    ):
        assert household_events(factories, household(owner), type_), type_
