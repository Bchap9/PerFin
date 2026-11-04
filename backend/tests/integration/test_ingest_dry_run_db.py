# SPDX-License-Identifier: AGPL-3.0-or-later
"""``perfin ingest --dry-run --household``: account matching reads, nothing is written."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from sqlalchemy import Engine, func, select, text
from sqlalchemy.exc import DBAPIError

from app.db.models import AuditLog, Event, Job
from app.db.session import make_sessionmaker
from app.domain.documents.models import Blob, Document, IngestJob, ReviewItem
from app.ingestion.dryrun import DryRun
from tests.fixtures.factories import Factories
from tests.fixtures.ledger import make_account
from tests.fixtures.statements import ofx_sgml, synth_transactions

pytestmark = pytest.mark.integration

COUNTED = (Document, Blob, IngestJob, ReviewItem, Job, Event, AuditLog)


def _counts(engine: Engine) -> dict[str, int]:
    # Counted as the superuser: the app role without an RLS context sees nothing.
    with engine.connect() as conn:
        return {
            model.__tablename__: conn.execute(select(func.count()).select_from(model)).scalar_one()
            for model in COUNTED
        }


def test_dry_run_matches_the_account_and_writes_nothing(
    factories: Factories, app_engine: Engine, admin_engine: Engine, tmp_path: Path
) -> None:
    owner = factories.user(role="owner")
    household_id = owner.household_id
    assert household_id is not None
    with factories.transaction() as db:
        account_id = make_account(db, household_id, number_last4="9876").id
    path = tmp_path / "statement.ofx"
    path.write_bytes(ofx_sgml(synth_transactions(3, seed=9), acct="0000009876"))
    before = _counts(admin_engine)
    out = io.StringIO()
    run = DryRun(
        path=path,
        out=out,
        household_id=household_id,
        session_factory=make_sessionmaker(app_engine),
    )
    assert run.run() == 0
    stages = [json.loads(line) for line in out.getvalue().splitlines()]
    match = stages[-1]
    assert match == {"stage": "match", "account_id": str(account_id), "candidates": []}
    assert stages[3]["household_fingerprints"] is True
    assert _counts(admin_engine) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["statement.ofx"]


def test_the_dry_run_transaction_is_read_only(
    factories: Factories, app_engine: Engine, tmp_path: Path
) -> None:
    owner = factories.user(role="owner")
    run = DryRun(
        path=tmp_path / "unused",
        out=io.StringIO(),
        household_id=owner.household_id,
        session_factory=make_sessionmaker(app_engine),
    )
    with run._read_only() as db:
        assert db is not None
        with pytest.raises(DBAPIError, match="read-only transaction"):
            db.execute(text("UPDATE household SET name = name"))
