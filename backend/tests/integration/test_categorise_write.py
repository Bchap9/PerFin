# SPDX-License-Identifier: AGPL-3.0-or-later
"""The engine's write path: new rows, a person's decision, a second look.

Design note, section 4: every stored category says how it was decided, a
guess below the line is only a suggestion, a person's choice is never
overwritten by a guess, and nothing the engine writes moves ``updated_at``
(the importer reads ``updated_at == created_at`` as "untouched").

The dictionary and clean-up are stubbed here: three invented merchants and a
fold to lower-case tokens, so these tests pin the write path and not the
data. Every descriptor is made up; none is a line of a statement.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import AuditLog, Event
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.categorisation import cleanup, dictionary, engine, memory
from app.domain.categorisation.cleanup import Cleaned
from app.domain.categorisation.dictionary import Hit, Merchant
from app.domain.categorisation.models import MerchantMemory
from app.domain.transactions.models import Category, Transaction
from tests.fixtures.factories import make_household
from tests.fixtures.ledger import (
    make_account,
    make_category,
    make_payee,
    make_rule,
    make_split,
    make_transaction,
)

pytestmark = pytest.mark.integration

#: When the rows were created, and when a person last touched some of them.
T0 = datetime(2026, 4, 6, 9, 15, tzinfo=UTC)
T1 = T0 + timedelta(days=3, hours=2)

GROCER = Merchant(key="corner grocer", name="Corner Grocer", category="food.groceries")
BARN = Merchant(
    key="everything barn", name="Everything Barn", category="shopping.general", mixed=True
)
CINEMA = Merchant(key="lamplight cinema", name="Lamplight Cinema", category="leisure.cinema")
MERCHANTS = {merchant.key: merchant for merchant in (GROCER, BARN, CINEMA)}

_KEPT = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")


def _clean(raw: str | None) -> Cleaned:
    """Lower-case tokens; a line that starts with the payment service is person-to-person."""
    folded = "".join(ch if ch in _KEPT else " " for ch in (raw or "").lower())
    core = " ".join(folded.split())
    if core.startswith("zelle"):
        return Cleaned(core="zelle", kind="p2p")
    return Cleaned(core=core)


def _lookup(known: dict[str, Merchant], core: str) -> Hit | None:
    for key, merchant in known.items():
        if core == key:
            return Hit(merchant, "exact")
        if core.startswith(key + " "):
            return Hit(merchant, "prefix")
    return None


@pytest.fixture(autouse=True)
def known(monkeypatch: pytest.MonkeyPatch) -> dict[str, Merchant]:
    """The dictionary holds three merchants; a test may add to or empty the mapping."""
    held = dict(MERCHANTS)
    monkeypatch.setattr(cleanup, "clean", _clean)
    monkeypatch.setattr(dictionary, "lookup", lambda core: _lookup(held, core))
    monkeypatch.setattr(dictionary, "keyword", lambda core, via: None)
    monkeypatch.setattr(dictionary, "name_for", lambda key: held[key].name if key in held else None)
    return held


class Home:
    """A household with an account and the categories the three merchants name."""

    def __init__(self, db: Session) -> None:
        self.db = db
        self.household_id = make_household(db).id
        self.account: Account = make_account(db, self.household_id)
        self.groceries = self._category("food.groceries")
        self.shopping = self._category("shopping.general")
        self.cinema = self._category("leisure.cinema")
        #: A category of the household's own that no merchant names.
        self.other = make_category(db, self.household_id)
        self.another = make_category(db, self.household_id)
        set_context(db, self.household_id)

    def _category(self, key: str) -> Category:
        found = self.db.execute(
            select(Category).where(
                Category.household_id == self.household_id, Category.system_key == key
            )
        ).scalar_one_or_none()
        return found or make_category(self.db, self.household_id, system_key=key)

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


def _provenance(db: Session, row: Transaction) -> tuple[Any, ...]:
    stored = _stored(db, row.id)
    return (
        stored["category_id"],
        stored["suggested_category_id"],
        stored["category_source"],
        stored["category_confidence"],
    )


def _memory_rows(db: Session) -> list[tuple[str, str, uuid.UUID, int]]:
    return [
        (row.merchant_key, row.direction, row.category_id, row.decisions)
        for row in db.execute(select(MerchantMemory)).scalars()
    ]


# ---------------------------------------------------------------------------
# New rows
# ---------------------------------------------------------------------------


def test_new_rows_get_a_key_and_a_guess_and_stay_untouched(db: Session, home: Home) -> None:
    grocer = home.row("CORNER GROCER 0417")
    barn = home.row("EVERYTHING BARN 22")
    unknown = home.row("MOSSY BOOKSHOP")
    for row in (grocer, barn, unknown):
        assert _stored(db, row.id)["updated_at"] == _stored(db, row.id)["created_at"]

    summary = engine.categorise_rows(db, home.household_id, [grocer, barn, unknown])

    assert _stored(db, grocer.id)["merchant_key"] == "corner grocer"
    assert _provenance(db, grocer) == (home.groceries.id, None, "dictionary", Decimal("0.90"))
    # A store that sells everything: a suggestion, never a category.
    assert _stored(db, barn.id)["merchant_key"] == "everything barn"
    assert _provenance(db, barn) == (None, home.shopping.id, "dictionary", Decimal("0.60"))
    assert _stored(db, unknown.id)["merchant_key"] == "mossy bookshop"
    assert _provenance(db, unknown) == (None, None, None, None)
    # The ORM rows the caller holds show what was written.
    assert (grocer.merchant_key, grocer.category_id, grocer.category_source) == (
        "corner grocer",
        home.groceries.id,
        "dictionary",
    )
    assert (barn.category_id, barn.suggested_category_id) == (None, home.shopping.id)
    assert unknown.merchant_key == "mossy bookshop"
    # Nothing here looks like a person's edit.
    for row in (grocer, barn, unknown):
        stored = _stored(db, row.id)
        assert stored["updated_at"] == stored["created_at"] == T0

    assert (summary.scanned, summary.keyed, summary.suggested) == (3, 3, 1)
    assert dict(summary.by_source) == {"dictionary": 1}
    assert summary.changed_accounts == {home.account.id}
    audits = db.execute(
        select(AuditLog.after, AuditLog.before, AuditLog.target_id).where(
            AuditLog.household_id == home.household_id,
            AuditLog.action == "transaction.categorise",
        )
    ).all()
    # Counts only: no descriptor, no merchant key, no name.
    assert [tuple(found) for found in audits] == [
        ({"count": 2, "by_source": {"dictionary": 1}, "suggested": 1}, None, None)
    ]
    events = db.execute(
        select(Event.payload).where(
            Event.household_id == home.household_id, Event.type == "transaction.categorised"
        )
    ).scalars()
    assert list(events) == [{"account_ids": [str(home.account.id)], "count": 2}]


def test_rows_nobody_can_be_guessed_for_write_no_audit_row(db: Session, home: Home) -> None:
    unknown = home.row("MOSSY BOOKSHOP")

    summary = engine.categorise_rows(db, home.household_id, [unknown])

    assert (summary.keyed, summary.changed, summary.changed_accounts) == (1, 0, set())
    assert unknown.merchant_key == "mossy bookshop"
    audits = db.execute(
        select(AuditLog.id).where(
            AuditLog.household_id == home.household_id,
            AuditLog.action == "transaction.categorise",
        )
    ).all()
    assert audits == []


def test_a_file_category_is_kept_and_marked_file(db: Session, home: Home) -> None:
    filed = home.row("CORNER GROCER 0417", category=home.other)
    # The file split this one: its lines carry the categories.
    split = home.row("LAMPLIGHT CINEMA", amount="-30.00")
    make_split(db, split, amount="-10.00", category=home.other)
    make_split(db, split, amount="-20.00", category=home.another)

    summary = engine.categorise_rows(
        db, home.household_id, [filed, split], file_categorised={filed.id, split.id}
    )

    assert _provenance(db, filed) == (home.other.id, None, "file", Decimal("0.95"))
    assert _stored(db, filed.id)["merchant_key"] == "corner grocer"
    assert _provenance(db, split) == (None, None, "file", Decimal("0.95"))
    assert dict(summary.by_source) == {"file": 2}
    for row in (filed, split):
        stored = _stored(db, row.id)
        assert stored["updated_at"] == stored["created_at"]


def test_a_rule_wins_over_a_guess_and_is_marked_rule(db: Session, home: Home) -> None:
    rule = make_rule(
        db,
        home.household_id,
        conditions={"type": "payee", "op": "contains", "value": "corner grocer"},
        actions=[{"type": "set_category", "category_id": str(home.other.id)}],
    )
    set_context(db, home.household_id)
    ruled = home.row("CORNER GROCER 0417")
    guessed = home.row("LAMPLIGHT CINEMA")
    # The file's category is not the rule's to change.
    filed = home.row("CORNER GROCER 0090", category=home.another)

    summary = engine.categorise_rows(
        db, home.household_id, [ruled, guessed, filed], file_categorised=[filed.id]
    )

    stored = _stored(db, ruled.id)
    assert (stored["category_id"], stored["category_source"]) == (home.other.id, "rule")
    assert stored["category_confidence"] == Decimal(1)
    assert stored["suggested_category_id"] is None
    assert stored["category_rule_id"] == rule.id
    assert _provenance(db, guessed) == (home.cinema.id, None, "dictionary", Decimal("0.90"))
    assert _provenance(db, filed) == (home.another.id, None, "file", Decimal("0.95"))
    assert dict(summary.by_source) == {"rule": 1, "dictionary": 1, "file": 1}


def test_a_manual_row_keeps_the_fields_its_caller_protects(db: Session, home: Home) -> None:
    make_rule(
        db,
        home.household_id,
        conditions={"type": "payee", "op": "contains", "value": "corner grocer"},
        actions=[{"type": "set_category", "category_id": str(home.other.id)}],
    )
    set_context(db, home.household_id)
    # A person typed this row with its category: neither a rule nor a guess decides it.
    typed = home.row("CORNER GROCER 0417", category=home.another)

    summary = engine.categorise_rows(
        db, home.household_id, [typed], protect={typed.id: ["categorization"]}
    )

    stored = _stored(db, typed.id)
    assert (stored["category_id"], stored["category_source"]) == (home.another.id, None)
    assert stored["merchant_key"] == "corner grocer"
    assert (dict(summary.by_source), summary.suggested) == ({}, 0)


def test_a_payee_default_wins_over_the_dictionary(db: Session, home: Home) -> None:
    payee = make_payee(db, home.household_id, default_category=home.other)
    set_context(db, home.household_id)
    row = home.row("CORNER GROCER 0417", payee=payee)

    summary = engine.categorise_rows(db, home.household_id, [row])

    assert _provenance(db, row) == (home.other.id, None, "payee", Decimal("0.95"))
    assert dict(summary.by_source) == {"payee": 1}


def test_a_learned_merchant_is_categorised_from_memory(db: Session, home: Home) -> None:
    memory.record(db, home.household_id, "mossy bookshop", "out", home.other.id, at=T0)
    row = home.row("MOSSY BOOKSHOP")
    # Money coming back from the shop is another question the household never answered.
    back = home.row("MOSSY BOOKSHOP", amount="8.00")

    summary = engine.categorise_rows(db, home.household_id, [row, back])

    assert _provenance(db, row) == (home.other.id, None, "learned", Decimal("0.80"))
    assert _provenance(db, back) == (None, None, None, None)
    assert dict(summary.by_source) == {"learned": 1}
    stored = _stored(db, row.id)
    assert stored["updated_at"] == stored["created_at"]


def test_what_was_learned_under_a_shop_s_shorter_name_answers_and_the_key_stays(
    db: Session, home: Home, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Clean-up leaves "<shop> <town or second word> <state>" open.
    monkeypatch.setattr(
        cleanup,
        "clean",
        lambda raw: Cleaned(core=_clean(raw).core, shorter=_clean(raw).core.split()[0]),
    )
    memory.record(db, home.household_id, "pinebrook", "out", home.other.id, at=T0)
    row = home.row("PINEBROOK ALDERLY")
    # A shop that only shares a first word nobody would use alone learns nothing.
    memory.record(db, home.household_id, "the", "out", home.another.id, at=T0)
    unrelated = home.row("THE LARKSPUR")

    engine.categorise_rows(db, home.household_id, [row, unrelated])

    assert _provenance(db, row) == (home.other.id, None, "learned", Decimal("0.80"))
    assert _stored(db, row.id)["merchant_key"] == "pinebrook alderly"
    assert _provenance(db, unrelated) == (None, None, None, None)
    assert _stored(db, unrelated.id)["merchant_key"] == "the larkspur"
    # The person's own word about the longer name comes first from then on.
    memory.record(db, home.household_id, "pinebrook alderly", "out", home.another.id, at=T0)
    again = home.row("PINEBROOK ALDERLY")
    engine.categorise_rows(db, home.household_id, [again])
    assert _provenance(db, again)[0] == home.another.id


def test_rows_of_another_household_are_refused(db: Session, home: Home) -> None:
    elsewhere = make_household(db)
    foreign = make_transaction(db, make_account(db, elsewhere.id), raw_payee="CORNER GROCER")
    set_context(db, home.household_id)
    own = home.row("CORNER GROCER 0417")

    with pytest.raises(ValueError, match="must belong to household_id"):
        engine.categorise_rows(db, home.household_id, [own, foreign])

    # Nothing was written for the row that did belong.
    assert _stored(db, own.id)["merchant_key"] is None


# ---------------------------------------------------------------------------
# A person's decision
# ---------------------------------------------------------------------------


def test_person_decided_teaches_and_moving_the_choice_moves_the_count(
    db: Session, home: Home
) -> None:
    row = home.row("MOSSY BOOKSHOP")
    engine.categorise_rows(db, home.household_id, [row])
    assert _memory_rows(db) == []

    row.category_id = home.other.id
    engine.person_decided(db, row, before_category_id=None, before_source=None, at=T1)
    db.flush()

    assert _provenance(db, row) == (home.other.id, None, "person", Decimal(1))
    assert _memory_rows(db) == [("mossy bookshop", "out", home.other.id, 1)]

    # The same person thinks again about the same row: one decision, moved.
    row.category_id = home.another.id
    engine.person_decided(db, row, before_category_id=home.other.id, before_source="person", at=T1)
    db.flush()

    assert _provenance(db, row) == (home.another.id, None, "person", Decimal(1))
    assert _memory_rows(db) == [("mossy bookshop", "out", home.another.id, 1)]


def test_correcting_a_guess_withdraws_nothing_and_drops_the_rule_and_the_suggestion(
    db: Session, home: Home
) -> None:
    rule = make_rule(db, home.household_id)
    set_context(db, home.household_id)
    suggested = home.row("EVERYTHING BARN 22")
    engine.categorise_rows(db, home.household_id, [suggested])
    assert suggested.suggested_category_id == home.shopping.id
    # What the household decided on another row of this merchant.
    memory.record(db, home.household_id, "everything barn", "out", home.other.id, at=T0)
    ruled = home.row(
        "EVERYTHING BARN 23",
        merchant_key="everything barn",
        category=home.other,
        category_source="rule",
        category_confidence=Decimal(1),
        category_rule_id=rule.id,
    )

    for row, before in ((suggested, None), (ruled, home.other.id)):
        before_source = row.category_source
        row.category_id = home.another.id
        engine.person_decided(
            db, row, before_category_id=before, before_source=before_source, at=T1
        )
    db.flush()

    for row in (suggested, ruled):
        stored = _stored(db, row.id)
        assert _provenance(db, row) == (home.another.id, None, "person", Decimal(1))
        assert stored["category_rule_id"] is None
    # A guess and a rule's category are not decisions to take back: the earlier one stays.
    assert sorted(_memory_rows(db), key=lambda found: found[3]) == [
        ("everything barn", "out", home.other.id, 1),
        ("everything barn", "out", home.another.id, 2),
    ]


def test_clearing_a_category_is_a_decision(db: Session, home: Home) -> None:
    row = home.row("CORNER GROCER 0417")
    engine.categorise_rows(db, home.household_id, [row])
    assert row.category_id == home.groceries.id

    row.category_id = None
    engine.person_decided(
        db, row, before_category_id=home.groceries.id, before_source="dictionary", at=T1
    )
    db.flush()

    assert _provenance(db, row) == (None, None, "person", Decimal(1))
    assert _memory_rows(db) == []
    before = _stored(db, row.id)
    # The dictionary still knows the merchant, and it is never asked again.
    summary = engine.reconsider(db, home.household_id, rekey=True)
    assert _stored(db, row.id) == before
    assert (dict(summary.by_source), summary.suggested, summary.changed) == ({}, 0, 0)


def test_a_p2p_row_teaches_nothing(db: Session, home: Home) -> None:
    row = home.row("ZELLE PAYMENT 0042")
    engine.categorise_rows(db, home.household_id, [row])
    # The key is the service, and such a payment is never guessed.
    assert _stored(db, row.id)["merchant_key"] == "zelle"
    assert _provenance(db, row) == (None, None, None, None)

    row.category_id = home.other.id
    engine.person_decided(db, row, before_category_id=None, before_source=None, at=T1)
    db.flush()

    assert _provenance(db, row) == (home.other.id, None, "person", Decimal(1))
    assert _memory_rows(db) == []


def test_a_row_without_a_key_teaches_nothing(db: Session, home: Home) -> None:
    row = home.row("MOSSY BOOKSHOP")

    row.category_id = home.other.id
    engine.person_decided(db, row, before_category_id=None, before_source=None, at=T1)
    db.flush()

    assert _provenance(db, row) == (home.other.id, None, "person", Decimal(1))
    assert _memory_rows(db) == []


def test_a_line_of_one_or_two_letters_gets_no_key_and_teaches_nothing(
    db: Session, home: Home
) -> None:
    first, second = home.row("E"), home.row("XY")

    engine.categorise_rows(db, home.household_id, [first, second])

    for row in (first, second):
        assert _stored(db, row.id)["merchant_key"] is None
        assert _provenance(db, row) == (None, None, None, None)
    first.category_id = home.other.id
    engine.person_decided(db, first, before_category_id=None, before_source=None, at=T1)
    db.flush()
    assert _memory_rows(db) == []
    # Another such line is not answered by that correction.
    later = home.row("E")
    engine.categorise_rows(db, home.household_id, [later])
    assert _provenance(db, later) == (None, None, None, None)


def test_a_rekey_takes_a_short_key_away_and_leaves_what_was_learned_under_it(
    db: Session, home: Home
) -> None:
    # Keyed and taught before keys of one or two letters were withheld.
    mine = home.row(
        "E",
        merchant_key="e",
        category=home.other,
        category_source="person",
        category_confidence=Decimal(1),
        updated_at=T1,
    )
    learned = home.row(
        "E",
        merchant_key="e",
        category=home.other,
        category_source="learned",
        category_confidence=Decimal("0.80"),
    )
    guessed = home.row(
        "E",
        merchant_key="e",
        suggested_category_id=home.other.id,
        category_source="learned",
        category_confidence=Decimal("0.70"),
    )
    memory.record(db, home.household_id, "e", "out", home.other.id, at=T1)

    summary = engine.reconsider(db, home.household_id, rekey=True)

    assert _stored(db, mine.id)["merchant_key"] is None
    assert _provenance(db, mine) == (home.other.id, None, "person", Decimal(1))
    # A learned fact is never guessed again (``GUESS_AGAIN_SOURCES``): it
    # stays, without the key.
    assert _stored(db, learned.id)["merchant_key"] is None
    assert _provenance(db, learned) == (home.other.id, None, "learned", Decimal("0.80"))
    # A suggestion is looked at again, and the memory no longer answers it.
    assert _stored(db, guessed.id)["merchant_key"] is None
    assert _provenance(db, guessed) == (None, None, None, None)
    assert summary.renamed == {"e": frozenset({None})}
    assert engine.key_now("e", summary.renamed) is None
    assert engine.keys_at(home.account.id, "out", "e", summary.renamed_at) == frozenset()
    # No row names the entry any more and it is moved nowhere.
    assert summary.memory_moved == 0
    assert _memory_rows(db) == [("e", "out", home.other.id, 1)]


def test_a_rekey_takes_the_key_off_bare_cash_wording(db: Session, home: Home) -> None:
    # Keyed before bare cash wording was withheld: "WITHDRAWAL" was a merchant.
    mine = home.row(
        "WITHDRAWAL",
        merchant_key="withdrawal",
        category=home.other,
        category_source="person",
        category_confidence=Decimal(1),
        updated_at=T1,
    )
    guessed = home.row(
        "CASH DEPOSIT",
        amount=Decimal("40.00"),
        merchant_key="cash deposit",
        suggested_category_id=home.other.id,
        category_source="learned",
        category_confidence=Decimal("0.70"),
    )
    memory.record(db, home.household_id, "withdrawal", "out", home.other.id, at=T1)

    summary = engine.reconsider(db, home.household_id, rekey=True)

    assert _stored(db, mine.id)["merchant_key"] is None
    assert _provenance(db, mine) == (home.other.id, None, "person", Decimal(1))
    assert _stored(db, guessed.id)["merchant_key"] is None
    assert summary.renamed == {
        "withdrawal": frozenset({None}),
        "cash deposit": frozenset({None}),
    }
    assert summary.memory_moved == 0


# ---------------------------------------------------------------------------
# A second look
# ---------------------------------------------------------------------------


def test_reconsider_touches_only_what_nobody_decided(db: Session, home: Home) -> None:
    decided = {
        source: home.row(
            "CORNER GROCER 0417",
            merchant_key="corner grocer",
            category=home.other,
            category_source=source,
            category_confidence=Decimal("0.95"),
            updated_at=T1,
        )
        for source in ("person", "file", "rule", "payee", "learned", "legacy")
    }
    cleared = home.row(
        "CORNER GROCER 0417", merchant_key="corner grocer", category_source="person", updated_at=T1
    )
    split = home.row("CORNER GROCER 0417", merchant_key="corner grocer", amount="-30.00")
    make_split(db, split, amount="-10.00", category=home.other)
    make_split(db, split, amount="-20.00", category=home.another)
    kept = [*decided.values(), cleared, split]
    undecided = home.row("CORNER GROCER 0090", posted_date=date(2026, 2, 1))
    stale = home.row(
        "CORNER GROCER 0090",
        merchant_key="corner grocer",
        category=home.other,
        category_source="dictionary",
        category_confidence=Decimal("0.90"),
        updated_at=T1,
    )
    suggested = home.row(
        "LAMPLIGHT CINEMA",
        merchant_key="lamplight cinema",
        suggested_category_id=home.other.id,
        category_source="keyword",
        category_confidence=Decimal("0.60"),
    )
    before = {row.id: _stored(db, row.id) for row in [*kept, undecided, stale, suggested]}

    summary = engine.reconsider(db, home.household_id)

    for row in kept:
        assert _stored(db, row.id) == before[row.id]
    fact = (home.groceries.id, None, "dictionary", Decimal("0.90"))
    assert _provenance(db, undecided) == fact
    assert _stored(db, undecided.id)["merchant_key"] == "corner grocer"
    assert _provenance(db, stale) == fact
    assert _provenance(db, suggested) == (home.cinema.id, None, "dictionary", Decimal("0.90"))
    for row in (undecided, stale, suggested):
        assert _stored(db, row.id)["updated_at"] == before[row.id]["updated_at"]
    assert (summary.scanned, summary.changed) == (3, 3)
    assert dict(summary.by_source) == {"dictionary": 3}
    assert summary.changed_accounts == {home.account.id}

    # A second look right after finds nothing to write.
    again = engine.reconsider(db, home.household_id)
    assert (again.scanned, again.changed, again.keyed) == (3, 0, 0)


def test_reconsider_withdraws_a_fact_the_dictionary_no_longer_states(
    db: Session, home: Home, known: dict[str, Merchant]
) -> None:
    gone = home.row("LAMPLIGHT CINEMA")
    now_mixed = home.row("CORNER GROCER 0417")
    engine.categorise_rows(db, home.household_id, [gone, now_mixed])
    assert gone.category_id == home.cinema.id
    assert now_mixed.category_id == home.groceries.id

    # A newer dictionary: one merchant is gone, the other turns out to sell everything.
    del known["lamplight cinema"]
    known["corner grocer"] = Merchant(
        key="corner grocer", name="Corner Grocer", category="food.groceries", mixed=True
    )
    summary = engine.reconsider(db, home.household_id)

    assert _provenance(db, gone) == (None, None, None, None)
    assert _provenance(db, now_mixed) == (None, home.groceries.id, "dictionary", Decimal("0.60"))
    assert (dict(summary.by_source), summary.suggested, summary.changed) == ({}, 1, 2)
    for row in (gone, now_mixed):
        stored = _stored(db, row.id)
        assert stored["updated_at"] == stored["created_at"]
    # The row the caller holds shows the withdrawal.
    assert [gone.category_id, gone.category_source] == [None, None]


def test_rekey_recomputes_keys_of_decided_rows_without_changing_their_category(
    db: Session, home: Home
) -> None:
    person = home.row(
        "CORNER GROCER 0417",
        merchant_key="corner grocer 0417",
        category=home.other,
        category_source="person",
        category_confidence=Decimal(1),
        updated_at=T1,
    )
    legacy = home.row("LAMPLIGHT CINEMA", category=home.other, category_source="legacy")
    before = _stored(db, person.id)

    # Without a re-key a decided row is not even read.
    quiet = engine.reconsider(db, home.household_id)
    assert (quiet.scanned, quiet.keyed) == (0, 0)
    assert _stored(db, person.id) == before

    summary = engine.reconsider(db, home.household_id, rekey=True)

    assert _stored(db, person.id) == {**before, "merchant_key": "corner grocer"}
    stored = _stored(db, legacy.id)
    assert (stored["merchant_key"], stored["category_id"], stored["category_source"]) == (
        "lamplight cinema",
        home.other.id,
        "legacy",
    )
    assert (summary.scanned, summary.keyed, summary.changed) == (2, 2, 0)
    # A key alone is no categorisation: nothing is announced.
    events = db.execute(
        select(Event.id).where(
            Event.household_id == home.household_id, Event.type == "transaction.categorised"
        )
    ).all()
    assert events == []


def _memory(db: Session, home: Home) -> set[tuple[str, str, uuid.UUID, int]]:
    return {
        (row.merchant_key, row.direction, row.category_id, row.decisions)
        for row in db.execute(
            select(MerchantMemory).where(MerchantMemory.household_id == home.household_id)
        ).scalars()
    }


def test_a_key_is_left_alone_unless_a_rekey_of_the_whole_household_is_asked_for(
    db: Session, home: Home
) -> None:
    # Keyed by an older clean-up; the guess itself is what would be made today.
    guessed = home.row(
        "CORNER GROCER 0090",
        merchant_key="corner grocer 0090",
        category=home.groceries,
        category_source="dictionary",
        category_confidence=Decimal("0.90"),
    )
    mine = home.row(
        "CORNER GROCER 0090",
        merchant_key="corner grocer 0090",
        category=home.other,
        category_source="person",
        category_confidence=Decimal(1),
        updated_at=T1,
    )
    memory.record(db, home.household_id, "corner grocer 0090", "out", home.other.id, at=T1)
    before = {row.id: _stored(db, row.id) for row in (guessed, mine)}
    taught = {("corner grocer 0090", "out", home.other.id, 1)}

    # A second look is not a re-key: the row is read, its key stays.
    summary = engine.reconsider(db, home.household_id)

    assert (summary.scanned, summary.keyed, summary.changed) == (1, 0, 0)
    assert _stored(db, guessed.id) == before[guessed.id]
    assert _memory(db, home) == taught

    # A re-key of some rows says nothing about the merchant's other rows:
    # the key of those rows moves, what was learned does not.
    part = engine.reconsider(db, home.household_id, transaction_ids=[mine.id], rekey=True)

    assert (part.keyed, part.memory_moved) == (1, 0)
    assert _stored(db, mine.id) == {**before[mine.id], "merchant_key": "corner grocer"}
    assert _stored(db, guessed.id) == before[guessed.id]
    assert _memory(db, home) == taught

    # Every row of the household: now what was learned goes with them.
    whole = engine.reconsider(db, home.household_id, rekey=True)

    assert (whole.keyed, whole.memory_moved) == (1, 1)
    assert _memory(db, home) == {("corner grocer", "out", home.other.id, 1)}


def test_reconsider_keeps_to_the_rows_it_is_given_and_can_be_cancelled(
    db: Session, home: Home
) -> None:
    first = home.row("CORNER GROCER 0417")
    second = home.row("CORNER GROCER 0090")

    summary = engine.reconsider(db, home.household_id, transaction_ids=[first.id])

    assert (summary.scanned, dict(summary.by_source)) == (1, {"dictionary": 1})
    assert _provenance(db, first) == (home.groceries.id, None, "dictionary", Decimal("0.90"))
    assert _provenance(db, second) == (None, None, None, None)
    assert engine.reconsider(db, home.household_id, transaction_ids=[]).scanned == 0

    class StopRequestedError(Exception):
        pass

    def stop() -> None:
        raise StopRequestedError

    with pytest.raises(StopRequestedError):
        engine.reconsider(db, home.household_id, check_cancelled=stop)
    assert _provenance(db, second) == (None, None, None, None)


def test_the_clauses_count_what_reconsider_would_read(db: Session, home: Home) -> None:
    home.row("MOSSY BOOKSHOP")
    home.row("MOSSY BOOKSHOP", category_source="person")
    home.row("MOSSY BOOKSHOP", category=home.other, category_source="legacy")
    home.row(
        "MOSSY BOOKSHOP",
        category=home.other,
        category_source="keyword",
        category_confidence=Decimal("0.80"),
    )
    home.row(
        "MOSSY BOOKSHOP",
        suggested_category_id=home.other.id,
        category_source="learned",
        category_confidence=Decimal("0.71"),
    )
    split = home.row("MOSSY BOOKSHOP", amount="-30.00")
    make_split(db, split, amount="-30.00", category=home.other)

    def count(clause: Any) -> int:
        return len(
            db.execute(
                select(Transaction.id).where(Transaction.household_id == home.household_id, clause)
            ).all()
        )

    assert count(engine.undecided_clause()) == 1
    assert count(engine.reconsidered_clause()) == 3
