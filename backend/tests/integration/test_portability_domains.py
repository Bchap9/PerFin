# SPDX-License-Identifier: AGPL-3.0-or-later
"""Export and import of every domain (docs/PORTABILITY.md, format version 2).

A household with one of everything (ledger, documents, investments with lots
and a sale of a chosen lot, a split and an average-cost account, a loan with
payments and a rate change, a manual asset, stored net worth snapshots, a tax
year with checklist, category map and form data, a learned parser
fingerprint and an AI client; ``tests.fixtures.portability.build_wealth``) is
exported through the real app and imported into a fresh household of another
owner. Every total the app reports (ledger, holdings, performance, loan
schedules, net worth, tax summary) must then be identical. A version 1
archive (no Phase 3-6 members) must still import. All data is synthetic.
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.ai.models import AiClient
from app.domain.investments.models import Holding, InvestmentTxn, Lot, Security
from app.domain.investments.statement_import import activity_hash
from app.domain.portability import service
from app.domain.portability.entities import ENTITIES, entities_for
from app.domain.portability.service import PortabilityDeps
from app.ingestion.sandbox.protocol import InvestmentActivity
from tests.fixtures.factories import AuthedClient, Factories
from tests.fixtures.households import API, Actors, household_events
from tests.fixtures.ledger import LedgerFactories
from tests.fixtures.portability import (
    CHEAP_KDF,
    RichLedger,
    Wealth,
    build_rich_ledger,
    build_wealth,
    members_of,
    rezip,
    rows_of,
    set_rows,
)
from tests.fixtures.wealth import make_security
from tests.integration.test_portability import (
    Jobs,
    export_archive,
    household,
    import_archive,
    ledger_fingerprint,
    reports,
)

pytestmark = pytest.mark.integration

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_V2_ENTITIES = [spec for spec in ENTITIES if spec.since == 2]


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
def store(port_settings: Settings) -> Any:
    from app.domain.documents.blobstore import LocalFSBlobStore

    return LocalFSBlobStore.from_settings(port_settings)


@pytest.fixture
def jobs(factories: Factories, store: Any, port_settings: Settings) -> Jobs:
    deps = PortabilityDeps(
        store=store,
        keyring=factories.keyring,
        export_ttl=timedelta(minutes=60),
        import_max_bytes=port_settings.import_max_bytes,
    )
    return Jobs(factories, deps)


@pytest.fixture(autouse=True)
def cheap_kdf(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "EXPORT_KDF", CHEAP_KDF)


@pytest.fixture
def full(
    actors: Actors,
    factories: Factories,
    ledger: LedgerFactories,
    store: Any,
) -> tuple[AuthedClient, RichLedger, Wealth]:
    owner = actors.new(role="owner")
    rich = build_rich_ledger(factories, ledger, store, household(owner))
    return owner, rich, build_wealth(factories, ledger, store, owner, rich)


# ---------------------------------------------------------------------------
# What the app shows, without household-specific ids and timestamps
# ---------------------------------------------------------------------------


def scrub(value: Any) -> Any:
    """``value`` with ids replaced, timestamps dropped and lists in a stable order."""
    if isinstance(value, dict):
        return {
            k: scrub(v)
            for k, v in value.items()
            if not k.endswith("_at") and k not in ("next_cursor", "key")
        }
    if isinstance(value, list):
        items = [scrub(v) for v in value]
        return sorted(items, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, str) and _UUID.match(value):
        return "<id>"
    return value


def get(owner: AuthedClient, path: str, **params: Any) -> Any:
    response = owner.get(f"{API}{path}", params=params)
    assert response.status_code == 200, f"{path}: {response.text}"
    return response.json()


def wealth_view(owner: AuthedClient) -> dict[str, Any]:
    """Every Phase 5/6 figure the API reports for the household."""
    view: dict[str, Any] = {
        "accounts": get(owner, "/accounts"),
        "holdings": get(owner, "/investments/holdings"),
        "investment_txns": get(owner, "/investments/transactions", limit=200),
        "loans": get(owner, "/loans"),
        "assets": get(owner, "/assets"),
        "networth": get(owner, "/networth", range="ytd", granularity="day"),
        "networth_latest": get(owner, "/networth/latest"),
        "tax_years": get(owner, "/tax-years"),
        "tax_checklist": get(owner, "/tax-years/2025/checklist"),
        "tax_forms": get(owner, "/tax-years/2025/forms"),
        "tax_summary": get(owner, "/tax-years/2025/summary"),
        "tax_category_map": get(owner, "/tax-category-map"),
    }
    # Performance of the average-cost account, and of the whole household,
    # whose brokerage account holds a sale of a chosen lot.
    (ira,) = [a for a in view["accounts"]["items"] if a["name"] == "Retirement"]
    view["performance"] = get(owner, "/investments/performance", range="ytd", account=ira["id"])
    view["performance_household"] = get(owner, "/investments/performance", range="ytd")
    securities = get(owner, "/securities")
    view["securities"] = securities
    for item in securities["items"]:
        view[f"prices {item['symbol']}"] = get(owner, f"/securities/{item['id']}/prices")
    for loan in view["loans"]["items"]:
        for part in ("schedule", "payments", "rate-changes"):
            view[f"loan {part}"] = get(owner, f"/loans/{loan['id']}/{part}")
    for asset in view["assets"]["items"]:
        view["valuations"] = get(owner, f"/assets/{asset['id']}/valuations")
    return {name: scrub(body) for name, body in view.items()}


def table_counts(factories: Factories, household_id: uuid.UUID) -> dict[str, int]:
    with factories.transaction() as db:
        set_context(db, household_id)
        return {
            spec.name: db.execute(
                select(func.count())
                .select_from(spec.table)
                .where(spec.table.c.household_id == household_id)
            ).scalar_one()
            for spec in [*ENTITIES]
        } | {
            "holdings": db.execute(
                select(func.count())
                .select_from(Holding)
                .where(Holding.household_id == household_id)
            ).scalar_one()
        }


def lot_shape(factories: Factories, household_id: uuid.UUID) -> list[Any]:
    """Lots and the lot links of every transaction, ids resolved to positions."""
    with factories.transaction() as db:
        set_context(db, household_id)
        lots = list(
            db.execute(
                select(Lot).where(Lot.household_id == household_id).order_by(Lot.open_date)
            ).scalars()
        )
        index = {lot.id: n for n, lot in enumerate(lots)}
        txns = db.execute(
            select(InvestmentTxn)
            .where(InvestmentTxn.household_id == household_id)
            .order_by(InvestmentTxn.trade_date, InvestmentTxn.type)
        ).scalars()
        return [
            [
                (
                    str(lot.open_date),
                    str(lot.quantity),
                    str(lot.remaining_quantity),
                    str(lot.cost_per_unit),
                    lot.opening_txn_id is not None,
                )
                for lot in lots
            ],
            [
                (
                    str(txn.trade_date),
                    txn.type.value,
                    txn.lots_specified,
                    sorted(
                        (index[uuid.UUID(link["lot_id"])], link["quantity"])
                        for link in txn.lot_links
                    ),
                )
                for txn in txns
            ],
        ]


# ---------------------------------------------------------------------------
# Round trip of every domain
# ---------------------------------------------------------------------------


def test_every_domain_round_trips_with_identical_totals(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    full: tuple[AuthedClient, RichLedger, Wealth],
) -> None:
    owner_a, _rich, _wealth = full
    before = wealth_view(owner_a)
    status, data = export_archive(owner_a, jobs)
    counts_a = table_counts(factories, household(owner_a))
    for spec in _V2_ENTITIES:
        # The fixture really populates every new entity (no vacuous pass).
        assert counts_a[spec.name] > 0, spec.name
        assert status["counts"][spec.name] == counts_a[spec.name], spec.name

    owner_b = actors.new(role="owner")
    outcome = import_archive(owner_b, jobs, data)
    assert outcome["state"] == "imported", outcome
    assert outcome["summary"]["import_hash_unrecoverable"] == 0
    assert table_counts(factories, household(owner_b)) == counts_a

    assert reports(owner_b) == reports(owner_a)
    assert ledger_fingerprint(factories, household(owner_b)) == ledger_fingerprint(
        factories, household(owner_a)
    )
    after = wealth_view(owner_b)
    assert wealth_view(owner_a) == before
    for name, body in before.items():
        assert after[name] == body, name
    assert lot_shape(factories, household(owner_b)) == lot_shape(factories, household(owner_a))
    # The statement row's hash was re-computed for the new account.
    with factories.transaction() as db:
        set_context(db, household(owner_b))
        hashed = db.execute(
            select(InvestmentTxn).where(
                InvestmentTxn.household_id == household(owner_b),
                InvestmentTxn.import_hash.is_not(None),
            )
        ).scalar_one()
        activity = InvestmentActivity.model_construct(
            type="interest", trade_date=hashed.trade_date, quantity=None, amount=hashed.amount
        )
        assert hashed.import_hash == activity_hash(hashed.account_id, activity, None, 0)
    # Stored snapshots came along (a computed day would say "computed").
    assert any(p.get("source") == "snapshot" for p in after["networth"]["points"]), after[
        "networth"
    ]["points"][:3]

    # AI clients: the definition, never the token; it arrives revoked.
    with factories.transaction() as db:
        set_context(db, household(owner_b))
        (client,) = db.execute(
            select(AiClient).where(AiClient.household_id == household(owner_b))
        ).scalars()
        assert client.name == "Synthetic assistant"
        assert client.custom_denylist == ["Synthetic School"]
        assert client.rounding == 10
        assert client.token_id is None
        assert client.created_by is None
        assert client.revoked_at is not None
    for type_ in ("investment.imported", "loan.imported", "asset.imported", "tax.imported"):
        assert household_events(factories, household(owner_b), type_), type_


def test_the_archive_carries_no_secret(
    jobs: Jobs, full: tuple[AuthedClient, RichLedger, Wealth]
) -> None:
    owner, _, wealth = full
    _, data = export_archive(owner, jobs)
    members = members_of(data)
    for name, content in members.items():
        assert wealth.ai_token.encode() not in content, name
    (client,) = rows_of(members, "ai_clients")
    assert "token_id" not in client
    assert "created_by" not in client
    names = {
        name.split("/")[1].removesuffix(".json") for name in members if name.startswith("data/")
    }
    for table in ("api_token", "mail_intake", "ai_call", "holding", "account_grant"):
        assert table not in names
    assert "holdings" not in names


def test_importing_twice_merges_catalogues_and_keeps_existing_rows(
    jobs: Jobs, full: tuple[AuthedClient, RichLedger, Wealth], factories: Factories
) -> None:
    """Into the same household: securities, tax years, snapshots and maps are not doubled."""
    owner, _, _ = full
    _, data = export_archive(owner, jobs)
    before = table_counts(factories, household(owner))
    outcome = import_archive(owner, jobs, data)
    assert outcome["state"] == "imported", outcome
    summary = outcome["summary"]
    assert summary["merged"]["securities"] == 2
    assert summary["merged"]["tax_years"] == 1
    assert summary["skipped"]["prices"] == before["prices"]
    assert summary["skipped"]["net_worth_snapshots"] == before["net_worth_snapshots"]
    assert summary["skipped"]["tax_category_maps"] == before["tax_category_maps"]
    assert summary["skipped"]["tax_checklist_items"] == before["tax_checklist_items"]
    assert summary["skipped"]["parser_fingerprints"] == before["parser_fingerprints"]
    after = table_counts(factories, household(owner))
    for name in ("securities", "prices", "tax_years", "net_worth_snapshots", "tax_category_maps"):
        assert after[name] == before[name], name
    # Accounts and their activity are new rows (an import never merges ledgers).
    assert after["investment_txns"] == 2 * before["investment_txns"]
    assert after["lots"] == 2 * before["lots"]


def test_securities_without_a_symbol_merge_by_name(
    jobs: Jobs, full: tuple[AuthedClient, RichLedger, Wealth], factories: Factories
) -> None:
    """Two securities without a symbol round-trip, and merge by name, never into each other."""
    owner, _, _ = full
    hid = household(owner)
    with factories.transaction() as db:
        set_context(db, hid)
        for name in ("Synthetic Plan Portfolio", "Synthetic Partners LP"):
            make_security(db, hid, name=name).symbol = None
    _, data = export_archive(owner, jobs)
    before = table_counts(factories, hid)
    outcome = import_archive(owner, jobs, data)
    assert outcome["state"] == "imported", outcome
    assert outcome["summary"]["merged"]["securities"] == 4
    assert table_counts(factories, hid)["securities"] == before["securities"]
    with factories.transaction() as db:
        set_context(db, hid)
        bare = db.execute(select(Security.name).where(Security.symbol.is_(None))).scalars()
        assert sorted(bare) == ["Synthetic Partners LP", "Synthetic Plan Portfolio"]


def _as_version_1(data: bytes) -> bytes:
    """``data`` as a format version 1 archive: no Phase 3-6 members."""
    kept_links: list[Any] = []

    def mutate(members: dict[str, bytes]) -> None:
        for spec in _V2_ENTITIES:
            del members[spec.member]
        # Links to version 2 targets cannot be in a version 1 archive.
        kept_links.extend(
            row
            for row in rows_of(members, "document_links")
            if row["target_type"] in ("transaction", "account")
        )
        set_rows(members, "document_links", kept_links)

    def manifest(document: dict[str, Any]) -> None:
        document["format_version"] = 1
        for spec in _V2_ENTITIES:
            document["counts"].pop(spec.name, None)
        document["counts"]["document_links"] = len(kept_links)

    return rezip(data, mutate, manifest=manifest)


def test_a_version_1_archive_still_imports(
    actors: Actors,
    factories: Factories,
    jobs: Jobs,
    full: tuple[AuthedClient, RichLedger, Wealth],
) -> None:
    owner_a, _, _ = full
    _, data = export_archive(owner_a, jobs)
    old = _as_version_1(data)
    assert json.loads(members_of(old)["manifest.json"])["format_version"] == 1
    owner_b = actors.new(role="owner")
    outcome = import_archive(owner_b, jobs, old)
    assert outcome["state"] == "imported", outcome
    assert reports(owner_b) == reports(owner_a)
    counts = table_counts(factories, household(owner_b))
    for spec in _V2_ENTITIES:
        assert counts[spec.name] == 0, spec.name
    assert {spec.name for spec in entities_for(1)} == {
        spec.name for spec in ENTITIES if spec.since == 1
    }


def test_a_version_1_archive_with_a_version_2_member_is_refused(
    actors: Actors, jobs: Jobs, full: tuple[AuthedClient, RichLedger, Wealth]
) -> None:
    owner_a, _, _ = full
    _, data = export_archive(owner_a, jobs)

    def add(members: dict[str, bytes]) -> None:
        members["data/securities.json"] = b"[]\n"

    outcome = import_archive(actors.new(role="owner"), jobs, rezip(_as_version_1(data), add))
    assert (outcome["state"], outcome["error"]) == ("failed", "archive-unexpected-member")


def test_a_lot_link_outside_the_archive_is_refused(
    actors: Actors, jobs: Jobs, full: tuple[AuthedClient, RichLedger, Wealth]
) -> None:
    owner_a, _, _ = full
    _, data = export_archive(owner_a, jobs)

    def mutate(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "investment_txns")
        linked = next(row for row in rows if row["lot_links"])
        linked["lot_links"][0]["lot_id"] = str(uuid.uuid4())
        set_rows(members, "investment_txns", rows)

    outcome = import_archive(actors.new(role="owner"), jobs, rezip(data, mutate))
    assert (outcome["state"], outcome["error"]) == ("failed", "archive-reference-unknown")


def test_an_invalid_fingerprint_is_refused(
    actors: Actors, jobs: Jobs, full: tuple[AuthedClient, RichLedger, Wealth]
) -> None:
    owner_a, _, _ = full
    _, data = export_archive(owner_a, jobs)

    def mutate(members: dict[str, bytes]) -> None:
        rows = rows_of(members, "parser_fingerprints")
        rows[0]["match_rules"] = {"all": [{"no_such_rule": "x"}]}
        set_rows(members, "parser_fingerprints", rows)

    outcome = import_archive(actors.new(role="owner"), jobs, rezip(data, mutate))
    assert (outcome["state"], outcome["error"]) == ("failed", "archive-invalid-value")
