# SPDX-License-Identifier: AGPL-3.0-or-later
"""The refresh, what was learned, and the summary (``/categorisation``).

Design note, sections 2, 4 and 8: when the bundled dictionary, the clean-up
or the default categories move, a refresh looks again at the rows nobody
decided and at nothing else, without moving ``updated_at``; the household's
memory is seeded once from the categories it set before provenance existed;
what was learned can be listed and forgotten; every count and every list is
limited to the accounts the caller may see.

The dictionary and clean-up are stubbed (a few invented merchants and a fold
to lower-case tokens), so these tests pin the service and not the data.
Every descriptor is made up; none is a line of a statement.
"""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import GrantLevel
from app.core.settings import Settings
from app.db.base import Base
from app.db.models import AuditLog, Job, Setting, SettingScope
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.categorisation import cleanup, dictionary, engine, memory, service
from app.domain.categorisation.cleanup import Cleaned
from app.domain.categorisation.dictionary import Hit, Merchant
from app.domain.categorisation.memory import Direction
from app.domain.categorisation.models import MerchantMemory
from app.domain.transactions import categories
from app.domain.transactions.models import Category, Transaction
from app.workers.registry import JobContext
from tests.fixtures.factories import AuthedClient, Factories, make_household
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
    make_account,
    make_category,
    make_transaction,
)

pytestmark = pytest.mark.integration

CATEGORISATION = f"{API}/categorisation"
STATE_KEY = "categorisation.state"
REFRESH = "categorise.refresh"
DISPATCH = "categorise.dispatch"

#: When the rows were created, and when a person last touched some of them.
T0 = datetime(2026, 4, 6, 9, 15, tzinfo=UTC)
T1 = T0 + timedelta(days=3, hours=2)

GROCER = Merchant(key="corner grocer", name="Corner Grocer", category="food.groceries")
CINEMA = Merchant(key="lamplight cinema", name="Lamplight Cinema", category="leisure.cinema")
#: Not in the dictionary until a test "ships a new version" that names it.
PICTURES = Merchant(
    key="velvet picture house", name="Velvet Picture House", category="leisure.cinema"
)

_KEPT = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def _clean(raw: str | None) -> Cleaned:
    folded = "".join(ch if ch in _KEPT else " " for ch in (raw or "").lower())
    return Cleaned(core=" ".join(folded.split()))


def _lookup(known: dict[str, Merchant], core: str) -> Hit | None:
    for key, merchant in known.items():
        if core == key:
            return Hit(merchant, "exact")
        if core.startswith(key + " "):
            return Hit(merchant, "prefix")
    return None


@pytest.fixture(autouse=True)
def known(monkeypatch: pytest.MonkeyPatch) -> dict[str, Merchant]:
    """The dictionary holds two merchants; a test may add to the mapping."""
    held = {merchant.key: merchant for merchant in (GROCER, CINEMA)}
    monkeypatch.setattr(cleanup, "clean", _clean)
    monkeypatch.setattr(dictionary, "lookup", lambda core: _lookup(held, core))
    monkeypatch.setattr(dictionary, "keyword", lambda core, via: None)
    monkeypatch.setattr(dictionary, "name_for", lambda key: held[key].name if key in held else None)
    return held


@pytest.fixture
def actors(settings: Settings, factories: Factories) -> Iterator[Actors]:
    with Actors.open(settings, factories) as built:
        yield built


def _versions() -> dict[str, Any]:
    """What a household that is up to date has stored."""
    return {
        "cleaner": cleanup.CLEANER_VERSION,
        "dictionary": dictionary.version(),
        "taxonomy": categories.TAXONOMY_VERSION,
        "memory_seeded": True,
    }


def _store_state(db: Session, household_id: uuid.UUID, value: dict[str, Any]) -> None:
    db.add(Setting(scope=SettingScope.HOUSEHOLD, scope_id=household_id, key=STATE_KEY, value=value))
    db.flush()


def _state(db: Session, household_id: uuid.UUID) -> Any:
    db.flush()
    return db.execute(
        select(Setting.value).where(
            Setting.scope == SettingScope.HOUSEHOLD,
            Setting.scope_id == household_id,
            Setting.key == STATE_KEY,
        )
    ).scalar_one_or_none()


def _keyed_category(db: Session, household_id: uuid.UUID, key: str) -> Category:
    found = db.execute(
        select(Category).where(Category.household_id == household_id, Category.system_key == key)
    ).scalar_one_or_none()
    return found or make_category(db, household_id, system_key=key)


class Home:
    """A household with an account and the categories the merchants name."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.household_id = make_household(db).id
        self.account: Account = make_account(db, self.household_id)
        self.groceries = _keyed_category(db, self.household_id, "food.groceries")
        self.cinema = _keyed_category(db, self.household_id, "leisure.cinema")
        #: A category of the household's own that no merchant names.
        self.other = make_category(db, self.household_id)
        set_context(db, self.household_id)

    def row(self, raw_payee: str, **fields: Any) -> Transaction:
        """A row created at :data:`T0` and untouched since."""
        fields.setdefault("created_at", T0)
        fields.setdefault("updated_at", fields["created_at"])
        return make_transaction(self.db, self.account, raw_payee=raw_payee, **fields)


@pytest.fixture
def home(db: Session) -> Home:
    return Home(db)


def _stored(db: Session, row_id: uuid.UUID) -> dict[str, Any]:
    """Every column of the row as the database holds it."""
    table = Base.metadata.tables[Transaction.__tablename__]
    db.flush()
    return dict(db.execute(select(table).where(table.c.id == row_id)).mappings().one())


def _audits(db: Session, household_id: uuid.UUID, action: str) -> list[AuditLog]:
    return list(
        db.execute(
            select(AuditLog)
            .where(AuditLog.household_id == household_id, AuditLog.action == action)
            .order_by(AuditLog.seq)
        ).scalars()
    )


def _memory_rows(db: Session, household_id: uuid.UUID) -> list[tuple[str, str, uuid.UUID, int]]:
    return [
        (row.merchant_key, row.direction, row.category_id, row.decisions)
        for row in db.execute(
            select(MerchantMemory).where(MerchantMemory.household_id == household_id)
        ).scalars()
    ]


def _handlers() -> Any:
    return importlib.import_module("app.workers.handlers.categorise")


def _ctx(
    db: Session, household_id: uuid.UUID | None, job_type: str, job_id: uuid.UUID | None = None
) -> JobContext:
    return JobContext(
        job_id=job_id or uuid.uuid4(),
        job_type=job_type,
        attempt=1,
        max_attempts=3,
        household_id=household_id,
        session=db,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def _flat(value: Any) -> list[Any]:
    """Every key and leaf of a JSON value."""
    if isinstance(value, dict):
        return [part for key, item in value.items() for part in (key, *_flat(item))]
    if isinstance(value, list):
        return [part for item in value for part in _flat(item)]
    return [value]


# ---------------------------------------------------------------------------
# The refresh
# ---------------------------------------------------------------------------


def test_refresh_is_a_no_op_when_versions_match(db: Session, home: Home) -> None:
    _store_state(db, home.household_id, _versions())
    # The dictionary knows this merchant, but nothing moved: it is not looked at.
    row = home.row("CORNER GROCER 0417")
    before = _stored(db, row.id)

    result = service.refresh(db, home.household_id)

    assert result.ran is False
    assert _stored(db, row.id) == before
    assert _state(db, home.household_id) == _versions()
    assert _audits(db, home.household_id, "categorise.refresh") == []
    assert _audits(db, home.household_id, "transaction.categorise") == []


def test_a_new_dictionary_version_reconsiders_only_undecided_rows(
    db: Session, home: Home, known: dict[str, Merchant], monkeypatch: pytest.MonkeyPatch
) -> None:
    _store_state(db, home.household_id, _versions())
    shipped = dictionary.version()
    undecided = home.row("VELVET PICTURE HOUSE 03", merchant_key="velvet picture house")
    mine = home.row(
        "VELVET PICTURE HOUSE 03",
        merchant_key="velvet picture house",
        category=home.other,
        category_source="person",
        category_confidence=Decimal(1),
        updated_at=T1,
    )
    mine_before = _stored(db, mine.id)

    # The next release names the merchant.
    known[PICTURES.key] = PICTURES
    monkeypatch.setattr(dictionary, "version", lambda: shipped + 1)
    result = service.refresh(db, home.household_id)

    assert result.ran is True
    stored = _stored(db, undecided.id)
    assert (stored["category_id"], stored["category_source"], stored["category_confidence"]) == (
        home.cinema.id,
        "dictionary",
        Decimal("0.90"),
    )
    # A person's row of the same merchant is exactly as it was.
    assert _stored(db, mine.id) == mine_before
    # Neither looks touched.
    assert stored["updated_at"] == stored["created_at"] == T0
    assert _stored(db, mine.id)["updated_at"] == T1
    assert _state(db, home.household_id) == {**_versions(), "dictionary": shipped + 1}

    [entry] = _audits(db, home.household_id, "categorise.refresh")
    assert entry.before is not None
    assert entry.after is not None
    assert entry.before["dictionary"] == shipped
    assert entry.after["versions"]["dictionary"] == shipped + 1
    assert entry.after["by_source"] == {"dictionary": 1}
    assert entry.after["changed"] == 1
    # Ids, versions and counts only: no descriptor, no merchant key, no name.
    text = [part for part in _flat([entry.before, entry.after]) if isinstance(part, str)]
    assert not [part for part in text if "velvet" in part.lower() or "picture" in part.lower()]

    # Asked again, nothing is behind any more.
    assert service.refresh(db, home.household_id).ran is False
    assert len(_audits(db, home.household_id, "categorise.refresh")) == 1


def test_the_first_refresh_adds_new_defaults_and_seeds_the_memory_once(
    db: Session, home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[uuid.UUID, int]] = []

    def add_new_defaults(session: Session, household_id: uuid.UUID, since: int) -> int:
        del session
        calls.append((household_id, since))
        return 13

    monkeypatch.setattr(categories, "add_new_defaults", add_new_defaults)
    # Categorised before this release: a category, the source ``legacy``, no key yet.
    legacy = [
        home.row(
            "MOSSY BOOKSHOP",
            amount=amount,
            category=home.other,
            category_source="legacy",
            updated_at=T1,
        )
        for amount in ("-8.10", "-14.25", "-9.60")
    ]
    assert _state(db, home.household_id) is None

    result = service.refresh(db, home.household_id)

    assert result.ran is True
    assert (result.categories_added, result.seeded) == (13, 1)
    assert calls == [(home.household_id, 1)]
    assert _memory_rows(db, home.household_id) == [("mossy bookshop", "out", home.other.id, 3)]
    for row in legacy:
        stored = _stored(db, row.id)
        # Keyed, and otherwise as the household left it.
        assert (stored["merchant_key"], stored["category_id"], stored["category_source"]) == (
            "mossy bookshop",
            home.other.id,
            "legacy",
        )
        assert stored["updated_at"] == T1
    assert _state(db, home.household_id) == _versions()
    [entry] = _audits(db, home.household_id, "categorise.refresh")
    assert entry.after is not None
    assert (entry.after["categories_added"], entry.after["memory_seeded"]) == (13, 1)

    again = service.refresh(db, home.household_id)

    assert again.ran is False
    assert calls == [(home.household_id, 1)]
    assert _memory_rows(db, home.household_id) == [("mossy bookshop", "out", home.other.id, 3)]

    # The household forgets the merchant, and an admin asks for another look:
    # the defaults are not added again and what was forgotten is not seeded back.
    [entry_id] = db.execute(
        select(MerchantMemory.id).where(MerchantMemory.household_id == home.household_id)
    ).scalars()
    assert memory.forget(db, home.household_id, entry_id) == 1

    forced = service.refresh(db, home.household_id, force=True)

    assert forced.ran is True
    assert (forced.categories_added, forced.seeded) == (0, 0)
    assert calls == [(home.household_id, 1)]
    assert _memory_rows(db, home.household_id) == []


def test_what_the_seeded_memory_knows_is_used_in_the_same_refresh(db: Session, home: Home) -> None:
    for amount in ("-8.10", "-14.25", "-9.60"):
        home.row("MOSSY BOOKSHOP", amount=amount, category=home.other, category_source="legacy")
    fresh = home.row("MOSSY BOOKSHOP")

    result = service.refresh(db, home.household_id)

    stored = _stored(db, fresh.id)
    assert (stored["category_id"], stored["category_source"], stored["category_confidence"]) == (
        home.other.id,
        "learned",
        Decimal("0.95"),
    )
    assert stored["updated_at"] == stored["created_at"]
    assert result.summary.by_source == {"learned": 1}


def test_a_forced_refresh_looks_again_although_nothing_moved(db: Session, home: Home) -> None:
    _store_state(db, home.household_id, _versions())
    row = home.row("CORNER GROCER 0417", merchant_key="corner grocer")
    # Keyed and taught under a key today's clean-up would not give.
    mine = home.row(
        "MOSSY BOOKSHOP 12",
        merchant_key="mossy bookshop",
        category=home.other,
        category_source="person",
        category_confidence=Decimal(1),
        updated_at=T1,
    )
    memory.record(db, home.household_id, "mossy bookshop", "out", home.other.id, at=T1)
    mine_before = _stored(db, mine.id)

    result = service.refresh(db, home.household_id, force=True)

    assert result.ran is True
    assert _stored(db, row.id)["category_id"] == home.groceries.id
    assert len(_audits(db, home.household_id, "categorise.refresh")) == 1
    # No version moved: no key is computed again, and nothing learned moves.
    assert _stored(db, mine.id) == mine_before
    assert _memory_rows(db, home.household_id) == [("mossy bookshop", "out", home.other.id, 1)]
    assert (result.summary.keyed, result.summary.memory_moved) == (0, 0)


def test_a_stored_state_that_is_not_one_counts_as_none(db: Session, home: Home) -> None:
    _store_state(db, home.household_id, {"cleaner": "one", "dictionary": -4, "taxonomy": True})
    row = home.row("CORNER GROCER 0417")

    assert service.refresh(db, home.household_id).ran is True

    assert _stored(db, row.id)["category_source"] == "dictionary"
    assert _state(db, home.household_id) == _versions()


# ---------------------------------------------------------------------------
# What was learned goes with a merchant to its new key
# ---------------------------------------------------------------------------

#: The next release's name for the cinema: every core that starts with the
#: old name is read as this merchant.
RENAMED = Merchant(key="velvet pictures", name="Velvet Pictures", category="leisure.cinema")


def _ship_a_release_that_renames_the_cinema(
    known: dict[str, Merchant], monkeypatch: pytest.MonkeyPatch
) -> None:
    shipped = dictionary.version()
    known[RENAMED.key] = RENAMED

    def lookup(core: str) -> Hit | None:
        if core == PICTURES.key or core.startswith(PICTURES.key + " "):
            return Hit(RENAMED, "prefix")
        return _lookup(known, core)

    monkeypatch.setattr(dictionary, "lookup", lookup)
    monkeypatch.setattr(dictionary, "version", lambda: shipped + 1)


def _learned_as(db: Session, household_id: uuid.UUID) -> set[tuple[str, str, uuid.UUID, int]]:
    return set(_memory_rows(db, household_id))


def test_what_the_household_taught_survives_a_new_name_for_the_merchant(
    db: Session, home: Home, known: dict[str, Merchant], monkeypatch: pytest.MonkeyPatch
) -> None:
    hh = home.household_id
    _store_state(db, hh, _versions())
    # Nobody decided this one; the dictionary does not know the merchant yet.
    early = home.row("VELVET PICTURE HOUSE 03")
    engine.categorise_rows(db, hh, [early])
    assert _stored(db, early.id)["merchant_key"] == "velvet picture house 03"
    # Three times the household files it under a category of its own.
    for _ in range(3):
        row = home.row("VELVET PICTURE HOUSE 03")
        engine.categorise_rows(db, hh, [row])
        was = (row.category_id, row.category_source)
        row.category_id = home.other.id
        engine.person_decided(db, row, before_category_id=was[0], before_source=was[1], at=T1)
        db.flush()
    assert _learned_as(db, hh) == {("velvet picture house 03", "out", home.other.id, 3)}

    _ship_a_release_that_renames_the_cinema(known, monkeypatch)
    result = service.refresh(db, hh)

    # The rows have the dictionary's key now, and so has what was learned.
    assert result.summary.memory_moved == 1
    assert _learned_as(db, hh) == {("velvet pictures", "out", home.other.id, 3)}
    # The row nobody decided is filed where the household files this
    # merchant, not where the dictionary would: in the same refresh.
    stored = _stored(db, early.id)
    assert (stored["merchant_key"], stored["category_id"], stored["category_source"]) == (
        "velvet pictures",
        home.other.id,
        "learned",
    )
    assert stored["category_confidence"] == Decimal("0.95")
    assert stored["updated_at"] == stored["created_at"] == T0
    # And so is the next one to arrive.
    later = home.row("VELVET PICTURE HOUSE 03")
    engine.categorise_rows(db, hh, [later])
    stored = _stored(db, later.id)
    assert (stored["merchant_key"], stored["category_id"], stored["category_source"]) == (
        "velvet pictures",
        home.other.id,
        "learned",
    )

    [entry] = _audits(db, hh, "categorise.refresh")
    assert entry.after is not None
    assert entry.after["memory_moved"] == 1
    text = [part for part in _flat([entry.before, entry.after]) if isinstance(part, str)]
    assert not [part for part in text if "velvet" in part.lower() or "picture" in part.lower()]


def test_learned_entries_meet_at_the_new_key_and_one_without_rows_follows_the_dictionary(
    db: Session, home: Home, known: dict[str, Merchant], monkeypatch: pytest.MonkeyPatch
) -> None:
    hh = home.household_id
    _store_state(db, hh, _versions())
    home.row(
        "VELVET PICTURE HOUSE 03",
        merchant_key="velvet picture house 03",
        category=home.other,
        category_source="legacy",
    )
    for step in range(2):
        memory.record(
            db, hh, "velvet picture house 03", "out", home.other.id, at=T0 + timedelta(hours=step)
        )
    # Already taught under the name the next release uses, later and elsewhere too.
    memory.record(db, hh, RENAMED.key, "out", home.other.id, at=T1)
    memory.record(db, hh, RENAMED.key, "out", home.groceries.id, at=T0)
    # Taught on rows that were deleted since: no row carries these keys.
    memory.record(db, hh, PICTURES.key, "in", home.other.id, at=T0)
    memory.record(db, hh, "mossy bookshop", "out", home.other.id, at=T0)

    _ship_a_release_that_renames_the_cinema(known, monkeypatch)
    result = service.refresh(db, hh)

    assert result.summary.memory_moved == 2
    assert _learned_as(db, hh) == {
        # Two and one for the same category are three; the other category stays its own.
        (RENAMED.key, "out", home.other.id, 3),
        (RENAMED.key, "out", home.groceries.id, 1),
        (RENAMED.key, "in", home.other.id, 1),
        # A merchant the dictionary still does not know stays where it is.
        ("mossy bookshop", "out", home.other.id, 1),
    }
    merged = db.execute(
        select(MerchantMemory.last_decided_at).where(
            MerchantMemory.household_id == hh,
            MerchantMemory.merchant_key == RENAMED.key,
            MerchantMemory.direction == "out",
            MerchantMemory.category_id == home.other.id,
        )
    ).scalar_one()
    assert merged == T1


def test_when_a_key_splits_only_the_decisions_of_the_rows_that_moved_go_with_them(
    db: Session, home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    hh = home.household_id
    _store_state(db, hh, _versions())

    def mine(raw_payee: str, key: str, category: Category) -> Transaction:
        return home.row(
            raw_payee,
            merchant_key=key,
            category=category,
            category_source="person",
            category_confidence=Decimal(1),
            updated_at=T1,
        )

    # An older clean-up read all three as one merchant; today's tells the
    # branches apart. Each row is one decision of the memory.
    mine("PEBBLE MARKET NORTH", "pebble market", home.other)
    mine("PEBBLE MARKET SOUTH", "pebble market", home.other)
    mine("PEBBLE MARKET", "pebble market", home.groceries)
    for _ in range(2):
        memory.record(db, hh, "pebble market", "out", home.other.id, at=T1)
    memory.record(db, hh, "pebble market", "out", home.groceries.id, at=T1)
    # Two rows moved, but the memory holds one decision (the rest was
    # forgotten or evicted): one goes, none is made up.
    mine("AMBER ORCHARD ONE", "amber orchard", home.other)
    mine("AMBER ORCHARD TWO", "amber orchard", home.other)
    mine("AMBER ORCHARD", "amber orchard", home.other)
    memory.record(db, hh, "amber orchard", "out", home.other.id, at=T1)
    # The household forgot this merchant: its rows teach nothing again.
    mine("TIN KETTLE EAST", "tin kettle", home.other)
    mine("TIN KETTLE", "tin kettle", home.other)

    monkeypatch.setattr(cleanup, "CLEANER_VERSION", cleanup.CLEANER_VERSION + 1)
    result = service.refresh(db, hh)

    assert result.summary.keyed == 5
    assert _learned_as(db, hh) == {
        ("pebble market north", "out", home.other.id, 1),
        ("pebble market south", "out", home.other.id, 1),
        ("pebble market", "out", home.groceries.id, 1),
        ("amber orchard one", "out", home.other.id, 1),
    }
    assert result.summary.memory_moved == 2


def test_dispatch_queues_only_households_behind(db: Session) -> None:
    behind = make_household(db).id
    current = make_household(db).id
    set_context(db, current)
    _store_state(db, current, _versions())
    handlers = _handlers()

    def queued(household_id: uuid.UUID) -> list[Job]:
        set_context(db, household_id)
        return list(
            db.execute(
                select(Job).where(Job.household_id == household_id, Job.type == REFRESH)
            ).scalars()
        )

    set_context(db, None)
    handlers.dispatch(_ctx(db, None, DISPATCH), handlers.DispatchPayload())

    [job] = queued(behind)
    assert job.payload == {"force": False, "requested_by": None}
    assert queued(current) == []

    # The next hour finds the same household still behind: no second job.
    set_context(db, None)
    handlers.dispatch(_ctx(db, None, DISPATCH), handlers.DispatchPayload())
    assert [found.id for found in queued(behind)] == [job.id]

    # The job brings the household up to date.
    set_context(db, behind)
    handlers.refresh(
        _ctx(db, behind, REFRESH, job.id), service.RefreshJob.model_validate(job.payload)
    )
    assert _state(db, behind) == _versions()


# ---------------------------------------------------------------------------
# Over HTTP
# ---------------------------------------------------------------------------


def _hid(client: AuthedClient) -> uuid.UUID:
    household = client.record.household
    assert household is not None
    return household.id


def _counts(body: dict[str, Any]) -> dict[str | None, int]:
    return {item["source"]: item["transactions"] for item in body["by_source"]}


def _record(
    db: Session,
    household_id: uuid.UUID,
    key: str,
    category_id: uuid.UUID,
    times: int,
    direction: Direction,
) -> None:
    with household_context(db, household_id):
        for step in range(times):
            memory.record(
                db, household_id, key, direction, category_id, at=T0 + timedelta(minutes=step)
            )


def _teach(
    ledger: LedgerFactories,
    household_id: uuid.UUID,
    key: str,
    category: Category,
    times: int,
    direction: Direction = "out",
) -> None:
    """``times`` decisions of the household for ``category`` on the merchant ``key``."""
    ledger.run(_record, household_id, key, category.id, times, direction)


def test_summary_counts_by_source_for_visible_accounts(
    actors: Actors, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    viewer = actors.new(role="viewer", household=owner.record.household)
    shared = ledger.account(hh)
    private = ledger.account(hh)
    ledger.grant(hh, member.user.id, shared, GrantLevel.VIEW)
    chosen = ledger.category(hh)
    sure = Decimal(1)

    # The shared account: one a person chose, one from the merchant list, one
    # suggestion, one nobody decided.
    ledger.transaction(shared, category=chosen, category_source="person", category_confidence=sure)
    ledger.transaction(
        shared, category=chosen, category_source="dictionary", category_confidence=Decimal("0.90")
    )
    ledger.transaction(
        shared,
        suggested_category_id=chosen.id,
        category_source="dictionary",
        category_confidence=Decimal("0.60"),
    )
    ledger.transaction(shared)
    # The private one: two more of the person's, one more undecided, and one
    # the person cleared (a decision, and still without a category).
    for _ in range(2):
        ledger.transaction(
            private, category=chosen, category_source="person", category_confidence=sure
        )
    ledger.transaction(private)
    ledger.transaction(private, category_source="person", category_confidence=sure)
    _teach(ledger, hh, "corner grocer", chosen, 1)

    response = owner.get(f"{CATEGORISATION}/summary")
    assert response.status_code == 200, response.text
    body = response.json()
    assert _counts(body) == {"person": 4, "dictionary": 1, None: 3}
    assert (body["uncategorised"], body["suggestions"], body["learned_merchants"]) == (4, 1, 1)
    assert body["dictionary_version"] == dictionary.version()
    assert body["cleaner_version"] == cleanup.CLEANER_VERSION
    assert body["dictionary_merchants"] == len(dictionary.merchants())
    # No refresh has run for this household yet.
    assert body["up_to_date"] is False

    seen = member.get(f"{CATEGORISATION}/summary").json()
    assert _counts(seen) == {"person": 1, "dictionary": 1, None: 2}
    assert (seen["uncategorised"], seen["suggestions"]) == (2, 1)
    # The one learned merchant is on no row the member may see.
    assert seen["learned_merchants"] == 0

    nothing = viewer.get(f"{CATEGORISATION}/summary").json()
    assert (nothing["by_source"], nothing["uncategorised"], nothing["suggestions"]) == ([], 0, 0)


def test_learned_list_offers_a_rule_at_three_unanimous_decisions(
    actors: Actors, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    shared = ledger.account(hh)
    private = ledger.account(hh)
    ledger.grant(hh, member.user.id, shared, GrantLevel.EDIT)
    food = ledger.category(hh)
    fun = ledger.category(hh)

    # Three times, always the same category: worth a rule.
    _teach(ledger, hh, "corner grocer", food, 3)
    ledger.transaction(shared, raw_payee="CORNER GROCER 0417", merchant_key="corner grocer")
    newest = ledger.transaction(
        shared,
        raw_payee="CORNER GROCER 0417",
        merchant_key="corner grocer",
        posted_date=T1.date(),
    )
    # Unanimous, but only twice.
    _teach(ledger, hh, "lamplight cinema", fun, 2)
    hidden = ledger.transaction(
        private, raw_payee="LAMPLIGHT CINEMA", merchant_key="lamplight cinema"
    )
    # Three times, in two places.
    _teach(ledger, hh, "mossy bookshop", fun, 2)
    _teach(ledger, hh, "mossy bookshop", food, 1)
    # Three times and unanimous, but money coming in.
    _teach(ledger, hh, "pebble payroll", food, 3, direction="in")
    # Three times and unanimous, and it has its rule already.
    _teach(ledger, hh, "tin kettle cafe", food, 3)
    rule = ledger.rule(
        hh,
        conditions={"type": "merchant", "key": "tin kettle cafe"},
        actions=[{"type": "set_category", "category_id": str(food.id)}],
    )

    response = owner.get(f"{CATEGORISATION}/learned")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["next_cursor"] is None
    items = {item["merchant_key"]: item for item in body["items"]}
    assert list(items) == [
        "corner grocer",
        "lamplight cinema",
        "mossy bookshop",
        "pebble payroll",
        "tin kettle cafe",
    ]
    grocer = items["corner grocer"]
    assert grocer["merchant"] == "Corner Grocer"
    assert (grocer["direction"], grocer["category_id"]) == ("out", str(food.id))
    assert (grocer["decisions"], grocer["total"], grocer["share"]) == (3, 3, "1.00")
    assert (grocer["offer_rule"], grocer["rule_id"]) == (True, None)
    assert grocer["example_transaction_id"] == str(newest.id)

    cinema = items["lamplight cinema"]
    assert (cinema["decisions"], cinema["offer_rule"]) == (2, False)
    assert cinema["example_transaction_id"] == str(hidden.id)
    bookshop = items["mossy bookshop"]
    # A merchant the dictionary does not know is shown by its key.
    assert bookshop["merchant"] == "Mossy Bookshop"
    assert (bookshop["category_id"], bookshop["decisions"], bookshop["total"]) == (
        str(fun.id),
        2,
        3,
    )
    assert (bookshop["share"], bookshop["offer_rule"]) == ("0.67", False)
    assert bookshop["example_transaction_id"] is None
    assert (items["pebble payroll"]["direction"], items["pebble payroll"]["offer_rule"]) == (
        "in",
        False,
    )
    assert (items["tin kettle cafe"]["rule_id"], items["tin kettle cafe"]["offer_rule"]) == (
        str(rule.id),
        False,
    )

    # Two a page, in key order, to the end.
    first = owner.get(f"{CATEGORISATION}/learned", params={"limit": 2}).json()
    assert [item["merchant_key"] for item in first["items"]] == [
        "corner grocer",
        "lamplight cinema",
    ]
    assert first["next_cursor"] == first["items"][-1]["id"]
    second = owner.get(
        f"{CATEGORISATION}/learned", params={"limit": 2, "cursor": first["next_cursor"]}
    ).json()
    assert [item["merchant_key"] for item in second["items"]] == [
        "mossy bookshop",
        "pebble payroll",
    ]
    third = owner.get(
        f"{CATEGORISATION}/learned", params={"limit": 2, "cursor": second["next_cursor"]}
    ).json()
    assert [item["merchant_key"] for item in third["items"]] == ["tin kettle cafe"]
    assert third["next_cursor"] is None
    assert owner.get(f"{CATEGORISATION}/learned", params={"limit": 201}).status_code == 422

    # A member is shown what was learned about merchants on rows they may
    # see, and no merchant of an account they may not.
    seen = member.get(f"{CATEGORISATION}/learned").json()
    assert [item["merchant_key"] for item in seen["items"]] == ["corner grocer"]
    assert seen["items"][0]["example_transaction_id"] == str(newest.id)


def test_the_example_is_a_row_the_caller_may_see_and_a_divided_merchant_gets_no_rule_offer(
    actors: Actors, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    shared = ledger.account(hh)
    private = ledger.account(hh)
    ledger.grant(hh, member.user.id, shared, GrantLevel.VIEW)
    food = ledger.category(hh)
    fun = ledger.category(hh)

    # The merchant's newest row is on an account the member has no grant for.
    _teach(ledger, hh, "amber orchard", food, 3)
    older = ledger.transaction(
        shared, raw_payee="AMBER ORCHARD", merchant_key="amber orchard", posted_date=T0.date()
    )
    newest = ledger.transaction(
        private, raw_payee="AMBER ORCHARD", merchant_key="amber orchard", posted_date=T1.date()
    )
    # Three decisions for one category, and a fourth for another.
    _teach(ledger, hh, "birch hardware", food, 3)
    _teach(ledger, hh, "birch hardware", fun, 1)
    ledger.transaction(shared, raw_payee="BIRCH HARDWARE", merchant_key="birch hardware")

    def listed(client: AuthedClient) -> dict[str, Any]:
        response = client.get(f"{CATEGORISATION}/learned")
        assert response.status_code == 200, response.text
        return {item["merchant_key"]: item for item in response.json()["items"]}

    for_owner = listed(owner)
    for_member = listed(member)
    assert list(for_owner) == list(for_member) == ["amber orchard", "birch hardware"]

    assert for_owner["amber orchard"]["example_transaction_id"] == str(newest.id)
    # Never the id of a row on somebody else's account.
    assert for_member["amber orchard"]["example_transaction_id"] == str(older.id)
    assert for_member["amber orchard"]["offer_rule"] is True

    for seen in (for_owner, for_member):
        birch = seen["birch hardware"]
        assert (birch["category_id"], birch["decisions"], birch["total"]) == (str(food.id), 3, 4)
        # Three, but not every time: no rule is offered.
        assert (birch["share"], birch["offer_rule"]) == ("0.75", False)


def test_forget_removes_the_entry_and_is_404_for_another_household(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    member = actors.new(role="member", household=owner.record.household)
    stranger = actors.new(role="owner")
    food = ledger.category(hh)
    fun = ledger.category(hh)
    _teach(ledger, hh, "mossy bookshop", fun, 2)
    _teach(ledger, hh, "mossy bookshop", food, 1)
    _teach(ledger, hh, "corner grocer", food, 1)

    listed = owner.get(f"{CATEGORISATION}/learned").json()["items"]
    entry = next(item for item in listed if item["merchant_key"] == "mossy bookshop")

    refused = stranger.delete(f"{CATEGORISATION}/learned/{entry['id']}")
    assert (refused.status_code, problem_type(refused)) == (404, "memory-not-found")
    missing = owner.delete(f"{CATEGORISATION}/learned/{uuid.uuid4()}")
    assert (missing.status_code, problem_type(missing)) == (404, "memory-not-found")
    assert len(owner.get(f"{CATEGORISATION}/learned").json()["items"]) == 2

    gone = member.delete(f"{CATEGORISATION}/learned/{entry['id']}")
    assert gone.status_code == 204, gone.text

    left = owner.get(f"{CATEGORISATION}/learned").json()["items"]
    assert [item["merchant_key"] for item in left] == ["corner grocer"]
    # Both categories of the merchant went, and that is all the record says.
    [record] = household_audit(factories, hh, "memory.forget")
    assert (record.target_type, record.target_id) == ("merchant_memory", entry["id"])
    assert (record.after, record.before) == ({"removed": 2}, None)
    assert record.user_id == member.user.id
    again = member.delete(f"{CATEGORISATION}/learned/{entry['id']}")
    assert (again.status_code, problem_type(again)) == (404, "memory-not-found")


def test_viewer_cannot_forget_and_member_cannot_refresh(
    actors: Actors, factories: Factories, ledger: LedgerFactories
) -> None:
    owner = actors.new(role="owner")
    hh = _hid(owner)
    admin = actors.new(role="admin", household=owner.record.household)
    member = actors.new(role="member", household=owner.record.household)
    viewer = actors.new(role="viewer", household=owner.record.household)
    account = ledger.account(hh)
    food = ledger.category(hh, system_key="food.groceries")
    _teach(ledger, hh, "mossy bookshop", food, 1)
    row = ledger.transaction(account, raw_payee="CORNER GROCER 0417")
    [entry] = owner.get(f"{CATEGORISATION}/learned").json()["items"]
    # A viewer may read the list (here: nothing, with no account to see).
    assert viewer.get(f"{CATEGORISATION}/learned").json() == {"items": [], "next_cursor": None}

    assert viewer.delete(f"{CATEGORISATION}/learned/{entry['id']}").status_code == 403
    assert len(owner.get(f"{CATEGORISATION}/learned").json()["items"]) == 1
    for refused in (viewer, member):
        assert refused.post(f"{CATEGORISATION}/refresh").status_code == 403
    with factories.transaction() as db:
        set_context(db, hh)
        assert db.execute(select(Job.id).where(Job.type == REFRESH)).all() == []

    accepted = admin.post(f"{CATEGORISATION}/refresh")
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["state"] == "queued"
    job_id = uuid.UUID(accepted.json()["job_id"])
    status = viewer.get(f"{API}/jobs/{job_id}")
    assert (status.status_code, status.json()["type"]) == (200, REFRESH)
    assert [event.payload for event in household_events(factories, hh, "job.queued")] == [
        {"id": str(job_id), "type": REFRESH}
    ]

    handlers = _handlers()
    with factories.transaction() as db:
        set_context(db, hh)
        job = db.get(Job, job_id)
        assert job is not None
        assert job.payload == {"force": True, "requested_by": str(admin.user.id)}
        handlers.refresh(
            _ctx(db, hh, REFRESH, job.id), service.RefreshJob.model_validate(job.payload)
        )

    with factories.transaction() as db:
        set_context(db, hh)
        found = db.get(Transaction, row.id)
        assert found is not None
        assert (found.merchant_key, found.category_id, found.category_source) == (
            "corner grocer",
            food.id,
            "dictionary",
        )
        # The system wrote it: it does not look touched.
        assert found.updated_at == row.updated_at
    [record] = household_audit(factories, hh, "categorise.refresh")
    assert record.user_id == admin.user.id
    assert record.after is not None
    assert record.after["forced"] is True
    assert owner.get(f"{CATEGORISATION}/summary").json()["up_to_date"] is True
