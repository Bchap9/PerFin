# SPDX-License-Identifier: AGPL-3.0-or-later
"""Phase 2 end to end: the real app, the real worker, the real sandbox.

One synthetic household goes through the whole ledger path over HTTP:

1. create an account (with the statement's last four digits), a category
   and a rule through the API;
2. upload an OFX statement with ``POST /documents``; the real
   :class:`app.workers.runner.Worker` claims the queued ``ingest_document``
   job and runs it (sandboxed extraction, structured import, rules, the
   follow-up transfer matcher);
3. ``GET /transactions`` shows the statement with the rule applied, and a
   live ``GET /events`` stream delivered the changes while the worker ran;
4. upload a CSV export of the same month: it waits in ``/review`` for a
   column mapping; saving one and resolving the item imports it, skipping
   exact duplicates and flagging a near duplicate in ``/review``;
5. confirming the near duplicate deletes it and records its hash, so the
   next export of that month imports only what is new;
6. a manual transaction goes through the same rules (fields typed by hand
   are protected);
7. resuming the event stream with ``Last-Event-ID`` replays the review and
   transaction events.

The worker runs only this household's jobs: other tests leave queued jobs in
the shared test database, which are held back (``run_after`` pushed into the
future) for the duration of the test and then restored.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select, text

from app.core.settings import Settings
from app.db.models import Job, JobState
from app.db.rls import queue_scope, set_context
from app.workers.handlers import ingest
from app.workers.registry import REGISTRY, discover_handlers
from app.workers.runner import Worker, WorkerConfig
from app.workers.scheduler import Schedule
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors
from tests.fixtures.synth_statements import (
    LAYOUTS,
    SynthStatement,
    SynthTxn,
    csv_bytes,
    ofx_sgml_bytes,
    sample_statement,
)

pytestmark = pytest.mark.integration

#: Longest the live stream stays open while the worker runs (seconds).
LIVE_STREAM_SECONDS = 12.0
PAYROLL = "Initech Payroll"


def sse_data(body: str) -> list[dict[str, Any]]:
    """The JSON ``data`` of every SSE message in ``body``, in order."""
    out: list[dict[str, Any]] = []
    for block in body.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                out.append(json.loads(line[len("data: ") :]))
    return out


@contextmanager
def only_household_jobs(factories: Factories, household_id: uuid.UUID) -> Iterator[None]:
    """Hold back every other queued job while the worker drains this household's."""
    with factories.transaction() as db, queue_scope(db):
        held = list(
            db.execute(
                text(
                    "UPDATE job SET run_after = run_after + interval '100 years' "
                    "WHERE state = 'queued' AND household_id IS DISTINCT FROM :h RETURNING id"
                ),
                {"h": household_id},
            ).scalars()
        )
    try:
        yield
    finally:
        if held:
            with factories.transaction() as db, queue_scope(db):
                db.execute(
                    text(
                        "UPDATE job SET run_after = run_after - interval '100 years' "
                        "WHERE id = ANY(:ids)"
                    ),
                    {"ids": held},
                )


class Household:
    """The test's household: its owner's client and the worker that serves it."""

    def __init__(
        self, owner: AuthedClient, factories: Factories, engine: Engine, settings: Settings
    ) -> None:
        self.owner = owner
        self.factories = factories
        self.settings = settings
        household_id = owner.record.household_id
        assert household_id is not None
        self.id = household_id
        self.worker = Worker(
            engine,
            config=WorkerConfig(listen=False, poll_interval=0.05, concurrency=1),
            registry=REGISTRY,
            schedule=Schedule(),
        )

    def drain(self) -> list[tuple[str, JobState]]:
        """Run this household's queued jobs (and the jobs they queue) to the end."""
        with only_household_jobs(self.factories, self.id):
            for _ in range(100):
                if not self.worker.run_once():
                    break
        return self.jobs()

    def jobs(self) -> list[tuple[str, JobState]]:
        with self.factories.transaction() as db:
            set_context(db, self.id)
            return [
                (job.type, job.state)
                for job in db.execute(
                    select(Job).where(Job.household_id == self.id).order_by(Job.created_at, Job.id)
                ).scalars()
            ]

    def upload(self, data: bytes, name: str, **params: str) -> dict[str, Any]:
        response = self.owner.post(
            f"{API}/documents",
            params=params,
            files={"file": (name, data, "application/octet-stream")},
        )
        assert response.status_code == 202, response.text
        body: dict[str, Any] = response.json()
        assert body["job_id"] is not None
        return body

    def transactions(self, **params: Any) -> list[dict[str, Any]]:
        response = self.owner.get(f"{API}/transactions", params={"limit": 500, **params})
        assert response.status_code == 200, response.text
        items: list[dict[str, Any]] = response.json()["items"]
        return items

    def review(self, **params: Any) -> list[dict[str, Any]]:
        response = self.owner.get(f"{API}/review", params=params)
        assert response.status_code == 200, response.text
        items: list[dict[str, Any]] = response.json()["items"]
        return items

    def document(self, document_id: str) -> dict[str, Any]:
        response = self.owner.get(f"{API}/documents/{document_id}")
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body


@pytest.fixture
def e2e_settings(settings: Settings, tmp_path: Path) -> Settings:
    return settings.model_copy(
        update={
            "documents_dir": tmp_path / "documents",
            "quarantine_dir": tmp_path / "quarantine",
            "sse_max_stream_seconds": LIVE_STREAM_SECONDS,
            "sse_heartbeat_seconds": 0.5,
            "sse_revalidate_seconds": 1.0,
        }
    )


@pytest.fixture
def household(
    e2e_settings: Settings,
    factories: Factories,
    app_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Household]:
    discover_handlers()
    # The worker's production pipeline dependencies (real sandbox, real
    # structured importer), on the same blob store directories as the API.
    monkeypatch.setattr(ingest, "get_settings", lambda: e2e_settings)
    ingest.default_deps.cache_clear()
    try:
        with Actors.open(e2e_settings, factories) as actors:
            yield Household(actors.new(role="owner"), factories, app_engine, e2e_settings)
    finally:
        ingest.default_deps.cache_clear()


def _statement() -> SynthStatement:
    stmt = sample_statement(41)
    assert any(t.payee == PAYROLL for t in stmt.transactions)
    assert any(t.payee.startswith("Quiet Bean") for t in stmt.transactions)
    return stmt


def _csv_export(stmt: SynthStatement, extra: tuple[SynthTxn, ...]) -> bytes:
    """The bank's CSV of the same month: payroll spelled differently, plus ``extra``."""
    rows = tuple(
        replace(t, payee="INITECH PAYROLL DIRECT DEPOSIT") if t.payee == PAYROLL else t
        for t in stmt.transactions
    )
    return csv_bytes(replace(stmt, transactions=tuple(sorted((*rows, *extra), key=_order))))


def _order(t: SynthTxn) -> tuple[date, str]:
    return (t.posted, t.fitid)


def _amounts(items: list[dict[str, Any]]) -> list[tuple[str, str]]:
    return sorted((t["posted_date"], t["amount"]["amount"]) for t in items)


def _expected(txns: tuple[SynthTxn, ...] | list[SynthTxn]) -> list[tuple[str, str]]:
    return sorted((t.posted.isoformat(), f"{t.amount:.2f}") for t in txns)


def test_statements_flow_from_upload_to_ledger_review_and_events(household: Household) -> None:
    owner = household.owner
    stmt = _statement()

    # 1. Account, category and rule through the API.
    account = owner.post(
        f"{API}/accounts",
        json={
            "type": "checking",
            "name": "Synthetic everyday account",
            "currency": "USD",
            "number_last4": stmt.last4,
        },
    )
    assert account.status_code == 201, account.text
    account_id = account.json()["id"]
    coffee = owner.post(f"{API}/categories", json={"name": "Coffee shops"})
    assert coffee.status_code == 201, coffee.text
    coffee_id = coffee.json()["id"]
    rule = owner.post(
        f"{API}/rules",
        json={
            "name": "Coffee",
            "conditions": {"type": "payee", "op": "starts_with", "value": "quiet bean"},
            "actions": [{"type": "set_category", "category_id": coffee_id}],
        },
    )
    assert rule.status_code == 201, rule.text

    # 2. Upload the OFX statement (no account named: matched by last four).
    ofx = household.upload(ofx_sgml_bytes(stmt), "statement.ofx")
    assert ofx["status"] == "received"

    # ... while a live event stream is open, the worker imports it.
    worker_done: dict[str, Any] = {}

    def run_worker() -> None:
        time.sleep(0.5)
        worker_done["jobs"] = household.drain()
        worker_done["at"] = time.monotonic()

    thread = threading.Thread(target=run_worker, daemon=True)
    thread.start()
    live = owner.get(f"{API}/events")
    stream_ended = time.monotonic()
    thread.join()
    assert live.status_code == 200, live.text
    assert worker_done["at"] < stream_ended, "the worker outlived the live stream"
    jobs = worker_done["jobs"]
    assert ("ingest_document", JobState.SUCCEEDED) in jobs
    assert ("transactions.auto_pair", JobState.SUCCEEDED) in jobs
    assert all(state is JobState.SUCCEEDED for _, state in jobs), jobs
    live_types = {d["type"] for d in sse_data(live.text)}
    assert {"document.updated", "transaction.rules_applied"} <= live_types, live_types
    # Messages carry ids and types only, never statement content.
    assert stmt.last4 not in live.text
    assert PAYROLL not in live.text

    # 3. The statement is in the ledger, with the rule applied.
    assert household.document(ofx["id"])["status"] == "processed"
    ledger = household.transactions(account=account_id)
    assert _amounts(ledger) == _expected(stmt.transactions)
    assert all(t["source_document_id"] == ofx["id"] for t in ledger)
    coffee_rows = [t for t in ledger if (t["raw_payee"] or "").startswith("Quiet Bean")]
    assert coffee_rows
    assert {t["category_id"] for t in coffee_rows} == {coffee_id}
    assert all(
        t["category_id"] != coffee_id
        for t in ledger
        if not (t["raw_payee"] or "").startswith("Quiet Bean")
    )
    payroll = [t for t in ledger if t["raw_payee"] == PAYROLL]
    assert len(payroll) == 1
    assert household.review() == []

    # 4. A CSV export of the same month needs a column mapping first.
    late = SynthTxn(stmt.end, Decimal("-12.34"), "Corner Kiosk", "Card purchase", "LATE0001")
    csv = household.upload(_csv_export(stmt, (late,)), "export.csv", account_id=account_id)
    assert {state for _, state in household.drain()} == {JobState.SUCCEEDED}
    assert household.document(csv["id"])["status"] == "needs_review"
    (mapping_item,) = household.review(document=csv["id"])
    assert (mapping_item["kind"], mapping_item["next_step"]) == ("unparsed", "column_mapping")
    preview = owner.get(f"{API}/imports/{csv['id']}/preview")
    assert preview.status_code == 200, preview.text
    suggested = preview.json()["suggested_spec"]
    assert suggested["date_column"] == LAYOUTS["us_signed"].spec["date_column"]
    mapping = owner.post(
        f"{API}/imports/mappings",
        json={
            "name": "Synthetic bank CSV",
            "spec": LAYOUTS["us_signed"].spec,
            "document_id": csv["id"],
            "account_id": account_id,
        },
    )
    assert mapping.status_code == 201, mapping.text
    resolved = owner.post(
        f"{API}/review/{mapping_item['id']}/resolve",
        json={"kind": "unparsed", "action": "apply_mapping", "mapping_id": mapping.json()["id"]},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["state"] == "resolved"
    jobs = household.drain()
    assert ("imports.apply_mapping", JobState.SUCCEEDED) in jobs
    assert all(state is JobState.SUCCEEDED for _, state in jobs), jobs

    # Exact duplicates were skipped, the late row is new, the respelled
    # payroll row is a near duplicate waiting in review.
    ledger = household.transactions(account=account_id)
    assert _amounts(ledger) == _expected(
        [*stmt.transactions, late, *[t for t in stmt.transactions if t.payee == PAYROLL]]
    )
    pending = household.transactions(account=account_id, review_state="pending")
    assert [t["raw_payee"] for t in pending] == ["INITECH PAYROLL DIRECT DEPOSIT"]
    (duplicate,) = household.review(kind="possible_duplicate")
    assert duplicate["document_id"] == csv["id"]
    assert duplicate["payload"]["transaction_id"] == pending[0]["id"]
    assert duplicate["payload"]["candidates"] == [payroll[0]["id"]]
    assert duplicate["next_step"] == "confirm_duplicate"
    # Reports leave the pending row out unless asked to include it.
    window = {"date_from": stmt.start.isoformat(), "date_to": stmt.end.isoformat()}
    flows: dict[bool, Decimal] = {}
    for include in (False, True):
        report = owner.get(
            f"{API}/reports/cashflow", params={**window, "include_pending": str(include).lower()}
        )
        assert report.status_code == 200, report.text
        assert report.json()["include_pending"] is include
        flows[include] = Decimal(report.json()["inflow"]["amount"])
    assert flows[True] - flows[False] == Decimal(payroll[0]["amount"]["amount"])

    # 5. Confirming the duplicate deletes it and remembers its hash.
    confirmed = owner.post(
        f"{API}/review/{duplicate['id']}/resolve",
        json={"kind": "possible_duplicate", "decision": "confirm"},
    )
    assert confirmed.status_code == 200, confirmed.text
    resolution = confirmed.json()["resolution"]
    assert resolution["deleted"] is True
    assert resolution["duplicate_of"] == payroll[0]["id"]
    assert resolution["hash_recorded"] == "alias"
    assert household.review() == []
    assert household.document(csv["id"])["status"] == "processed"
    ledger = household.transactions(account=account_id)
    assert _amounts(ledger) == _expected([*stmt.transactions, late])

    # The next export of that month (one more row): the saved mapping is
    # found by its header, the confirmed duplicate matches its alias, only
    # the new row is imported and nothing needs review.
    later = SynthTxn(stmt.end, Decimal("-3.21"), "Night Bakery", "Card purchase", "LATE0002")
    again = household.upload(
        _csv_export(stmt, (late, later)), "export-2.csv", account_id=account_id
    )
    assert all(state is JobState.SUCCEEDED for _, state in household.drain())
    assert household.document(again["id"])["status"] == "processed"
    assert household.review() == []
    ledger = household.transactions(account=account_id)
    assert _amounts(ledger) == _expected([*stmt.transactions, late, later])

    # 6. A manual transaction goes through the rules too; a category typed
    # by hand is kept.
    manual = owner.post(
        f"{API}/transactions",
        json={
            "account_id": account_id,
            "posted_date": "2026-02-02",
            "amount": {"amount": "-3.80", "currency": "USD"},
            "raw_payee": "Quiet Bean Coffee",
        },
    )
    assert manual.status_code == 201, manual.text
    assert manual.json()["category_id"] == coffee_id
    other = owner.post(f"{API}/categories", json={"name": "Treats"})
    assert other.status_code == 201, other.text
    typed = owner.post(
        f"{API}/transactions",
        json={
            "account_id": account_id,
            "posted_date": "2026-02-03",
            "amount": {"amount": "-2.10", "currency": "USD"},
            "raw_payee": "Quiet Bean Coffee",
            "category_id": other.json()["id"],
        },
    )
    assert typed.status_code == 201, typed.text
    assert typed.json()["category_id"] == other.json()["id"]
    fetched = owner.get(f"{API}/transactions/{manual.json()['id']}")
    assert fetched.json()["category_id"] == coffee_id

    # 7. Resuming the stream replays everything since the first live event.
    first_live = sse_data(live.text)[0]["id"]
    resumed = owner.get(f"{API}/events", headers={"Last-Event-ID": first_live})
    assert resumed.status_code == 200, resumed.text
    resumed_types = [d["type"] for d in sse_data(resumed.text)]
    assert "review.resolved" in resumed_types
    assert "transaction.created" in resumed_types
    assert "document.mapping_created" in resumed_types
