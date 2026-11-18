# SPDX-License-Identifier: AGPL-3.0-or-later
"""The importer hands its rows to the categorisation engine (design note, section 4).

Files and stored readings go through the real import
(``app.domain.imports.service``), and a person's corrections through the
API. Clean-up is the real one; the dictionary is stubbed with two invented
merchants (a test may add a third), so these tests pin the hand-over and not
the data. Every descriptor, merchant, amount and account number here is
invented; none is a line of a statement.

* A first import: every row gets its merchant key, a known merchant's
  category is a fact that says ``dictionary``, and the run's summary counts
  it (``categorised``, ``suggested``).
* Whatever the engine wrote, the rows are untouched afterwards
  (``updated_at == created_at``), so a later reading of the document may
  still replace them.
* The file's category beats a rule, and a rule beats the dictionary.
* A payee is found behind a processor (``PAYPAL *STREAMBOX``); a line
  between people, or one that names a second payee further along, gets no
  payee and no category from one.
* A store that sells everything arrives as a suggestion, counted as one.
* A second import of the same rows leaves a person's choice alone.
* A re-read removes a row that only ever carried a guess (at import, or
  from a later second look) and keeps, and lists, one whose category a
  person changed.
* One correction teaches: next month's charge from that merchant is
  ``learned``.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.principal import Principal, apply_rls_context
from app.core.crypto import Keyring
from app.core.settings import Settings
from app.db.rls import set_context
from app.domain.accounts.models import Account
from app.domain.categorisation import dictionary, engine
from app.domain.categorisation.dictionary import Hit, Merchant
from app.domain.documents.blobstore import LocalFSBlobStore
from app.domain.documents.models import (
    Document,
    DocumentStatus,
    IngestJob,
    IngestStage,
    ReviewItem,
    ReviewItemState,
)
from app.domain.documents.schemas import INGEST_JOB_TYPE
from app.domain.imports.service import import_extraction
from app.domain.review.layout import stored_result
from app.domain.transactions.models import Category, Transaction
from app.ingestion.intake.upload import receive_upload
from app.ingestion.sandbox.protocol import ExtractResult
from app.workers.handlers import ingest
from tests.fixtures.documents import make_document
from tests.fixtures.factories import AuthedClient, Factories, UserRecord
from tests.fixtures.households import API, Actors
from tests.fixtures.imports import run_jobs
from tests.fixtures.ledger import LedgerFactories, household_context, make_category

pytestmark = pytest.mark.integration

MAX = 10 * 1024 * 1024
PDF = b"%PDF-1.7\n% synthetic placeholder, never parsed by the API\n%%EOF\n"

MARKET = Merchant(key="pennywhistle market", name="Pennywhistle Market", category="food.groceries")
CINEMA = Merchant(key="lamplight cinema", name="Lamplight Cinema", category="leisure.cinema")
#: Not in the dictionary until a test puts it there (a newer dictionary).
TOOL_HIRE = Merchant(key="holloway tool hire", name="Holloway Tool Hire", category="leisure.cinema")
#: A store that sells everything: its category is only ever a suggestion.
BAZAAR = Merchant(
    key="marrowfield bazaar", name="Marrowfield Bazaar", category="food.groceries", mixed=True
)

#: Day of the month, amount, descriptor: six bare lines, two of known merchants.
Entry = tuple[int, str, str]
MARCH: list[Entry] = [
    (2, "-48.10", "PENNYWHISTLE MARKET"),
    (5, "-17.25", "LAMPLIGHT CINEMA"),
    (9, "-63.40", "HOLLOWAY TOOL HIRE"),
    (12, "-21.00", "BRAMBLE AND FINCH"),
    (17, "-9.99", "OSTLER YARD SUPPLY"),
    (24, "-14.50", "TINDERBOX BOOKS"),
]


def _csv(entries: Sequence[Entry], *, month: int = 3) -> bytes:
    lines = ["Date,Description,Amount"]
    lines += [f"2026-{month:02d}-{day:02d},{payee},{amount}" for day, amount, payee in entries]
    return ("\n".join(lines) + "\n").encode()


def _lookup(known: dict[str, Merchant], core: str) -> Hit | None:
    for key, merchant in known.items():
        if core == key:
            return Hit(merchant, "exact")
        if core.startswith(key + " "):
            return Hit(merchant, "prefix")
    return None


@pytest.fixture(autouse=True)
def known(monkeypatch: pytest.MonkeyPatch) -> dict[str, Merchant]:
    """The dictionary holds two merchants and no keyword; a test may add to the mapping."""
    held = {merchant.key: merchant for merchant in (MARKET, CINEMA)}
    monkeypatch.setattr(dictionary, "lookup", lambda core: _lookup(held, core))
    monkeypatch.setattr(dictionary, "keyword", lambda core, via: None)
    monkeypatch.setattr(dictionary, "name_for", lambda key: held[key].name if key in held else None)
    return held


def _keyed(db: Session, household_id: uuid.UUID, key: str) -> Category:
    """The household's category with ``key``, made when it has none."""
    with household_context(db, household_id):
        found = db.execute(
            select(Category).where(
                Category.household_id == household_id, Category.system_key == key
            )
        ).scalar_one_or_none()
    return found or make_category(db, household_id, system_key=key)


@dataclass
class Env:
    factories: Factories
    ledger: LedgerFactories
    store: LocalFSBlobStore
    keyring: Keyring
    owner: UserRecord
    actors: Actors
    done: set[uuid.UUID] = field(default_factory=set)

    @property
    def household_id(self) -> uuid.UUID:
        assert self.owner.household_id is not None
        return self.owner.household_id

    def principal(self) -> Principal:
        return Principal(
            user=self.owner.user,
            household_id=self.owner.household_id,
            role=self.owner.role,
            auth_method="session",
        )

    def client(self) -> AuthedClient:
        return self.actors.login(self.owner)

    def keyed(self, key: str) -> Category:
        return self.ledger.run(_keyed, self.household_id, key)

    def ingest(self, data: bytes, account: Account, name: str) -> tuple[uuid.UUID, dict[str, Any]]:
        """Upload a file for ``account`` and run its ingest job; its id and the summary."""
        with self.factories.transaction() as db:
            apply_rls_context(db, self.principal())
            received = receive_upload(
                db,
                self.principal(),
                store=self.store,
                keyring=self.keyring,
                chunks=iter([data]),
                max_bytes=MAX,
                filename=name,
                account_id=account.id,
            )
        assert run_jobs(self.factories, self.household_id, INGEST_JOB_TYPE, done=self.done)
        document_id = received.document_id
        logs = self.query(
            select(IngestJob.log).where(
                IngestJob.document_id == document_id, IngestJob.stage == IngestStage.PARSE
            )
        )
        assert self.document(document_id).status is DocumentStatus.PROCESSED
        assert len(logs) == 1
        return document_id, logs[0]

    def query(self, statement: Any) -> list[Any]:
        with self.factories.transaction() as db:
            set_context(db, self.household_id)
            rows = list(db.execute(statement).scalars())
            db.expunge_all()
            return rows

    def document(self, document_id: uuid.UUID) -> Document:
        found: list[Document] = self.query(select(Document).where(Document.id == document_id))
        return found[0]

    def transactions(self, account: Account) -> dict[str, Transaction]:
        """The account's rows by descriptor (one row per descriptor in these tests)."""
        rows: list[Transaction] = self.query(
            select(Transaction)
            .where(Transaction.account_id == account.id)
            .order_by(Transaction.posted_date, Transaction.id)
        )
        by_payee = {row.raw_payee or "": row for row in rows}
        assert len(by_payee) == len(rows)
        return by_payee

    def kept_items(self, document_id: uuid.UUID) -> list[dict[str, Any]]:
        items: list[ReviewItem] = self.query(
            select(ReviewItem).where(
                ReviewItem.document_id == document_id, ReviewItem.state == ReviewItemState.OPEN
            )
        )
        return [
            item.payload
            for item in items
            if (item.payload or {}).get("reason") == "earlier_rows_kept"
        ]

    def choose(self, row: Transaction, category: Category) -> None:
        """A person sets ``row``'s category in the grid."""
        response = self.client().patch(
            f"{API}/transactions/{row.id}", json={"category_id": str(category.id)}
        )
        assert response.status_code == 200, response.text


@pytest.fixture
def env(
    factories: Factories,
    ledger: LedgerFactories,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Env]:
    local = settings.model_copy(
        update={"documents_dir": tmp_path / "documents", "quarantine_dir": tmp_path / "quarantine"}
    )
    monkeypatch.setattr(ingest, "get_settings", lambda: local)
    ingest.default_deps.cache_clear()
    deps = ingest.default_deps()
    assert isinstance(deps.store, LocalFSBlobStore)
    with Actors.open(local, factories) as actors:
        yield Env(
            factories=factories,
            ledger=ledger,
            store=deps.store,
            keyring=deps.keyring,
            owner=factories.user(role="owner", with_totp=True),
            actors=actors,
        )
    ingest.default_deps.cache_clear()


def _month(last4: str, entries: Sequence[tuple[Any, ...]]) -> dict[str, Any]:
    """March 2026 as a document parser stores it; a fourth field is the file's category."""
    return {
        "source_format": "pdf",
        "account": {"last4": last4, "currency": "USD"},
        "period_start": date(2026, 3, 1).isoformat(),
        "period_end": date(2026, 3, 30).isoformat(),
        "transactions": [
            {
                "posted": date(2026, 3, entry[0]).isoformat(),
                "amount": entry[1],
                "payee": entry[2],
                **({"category": entry[3]} if len(entry) > 3 else {}),
            }
            for entry in entries
        ],
        "confidence": {"period": 1.0},
    }


def _read(env: Env, document_id: uuid.UUID, statement: dict[str, Any]) -> dict[str, Any]:
    """Store ``statement`` as the document's reading and import it; the run's summary."""
    read = ExtractResult.model_validate({"kind": "pdf", "ok": True, "statements": [statement]})
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        document = db.get(Document, document_id)
        assert document is not None
        document.extraction = read.model_dump(mode="json")
        db.flush()
        result = stored_result(document.extraction)
        assert result is not None
        outcome = import_extraction(db, document, result)
        document.status = outcome.status
        return outcome.summary


def _stored(env: Env, statement: dict[str, Any]) -> tuple[uuid.UUID, dict[str, Any]]:
    """A document whose first reading is ``statement``, imported; its id and summary."""
    with env.factories.transaction() as db:
        document = make_document(
            db,
            env.store,
            env.keyring,
            env.household_id,
            data=PDF + uuid.uuid4().hex.encode(),
            mime="application/pdf",
            filename="statement.pdf",
            uploaded_by=env.owner.user.id,
            status=DocumentStatus.NEEDS_REVIEW,
        )
        document_id = document.id
    return document_id, _read(env, document_id, statement)


def _decided(row: Transaction) -> tuple[uuid.UUID | None, str | None]:
    return row.category_id, row.category_source


# ---------------------------------------------------------------------------
# A first import
# ---------------------------------------------------------------------------


def test_a_first_import_is_keyed_and_guessed(env: Env) -> None:
    groceries, cinema = env.keyed("food.groceries"), env.keyed("leisure.cinema")
    account = env.ledger.account(env.household_id, currency="USD")

    _, summary = env.ingest(_csv(MARCH), account, "march.csv")

    rows = env.transactions(account)
    assert len(rows) == len(MARCH)
    assert [row.merchant_key is not None for row in rows.values()] == [True] * len(MARCH)
    assert rows["PENNYWHISTLE MARKET"].merchant_key == MARKET.key
    assert rows["TINDERBOX BOOKS"].merchant_key == "tinderbox books"
    assert _decided(rows["PENNYWHISTLE MARKET"]) == (groceries.id, "dictionary")
    assert _decided(rows["LAMPLIGHT CINEMA"]) == (cinema.id, "dictionary")
    assert rows["LAMPLIGHT CINEMA"].category_confidence == Decimal("0.90")
    known_lines = {"PENNYWHISTLE MARKET", "LAMPLIGHT CINEMA"}
    others = [row for payee, row in rows.items() if payee not in known_lines]
    assert [_decided(row) for row in others] == [(None, None)] * 4
    assert summary["imported"] == len(MARCH)
    assert summary["categorised"] == {"dictionary": 2}
    assert summary["suggested"] == 0
    assert (summary["rules_changed"], summary["payee_categorised"]) == (0, 0)


def test_every_imported_row_is_untouched_after_the_engine_ran(env: Env) -> None:
    env.keyed("food.groceries")
    env.keyed("leisure.cinema")
    account = env.ledger.account(env.household_id, currency="USD")

    env.ingest(_csv(MARCH), account, "march.csv")

    rows = env.transactions(account)
    assert rows["PENNYWHISTLE MARKET"].category_source == "dictionary"
    assert [row.updated_at == row.created_at for row in rows.values()] == [True] * len(MARCH)


def test_a_file_category_beats_a_rule_and_a_rule_beats_the_dictionary(env: Env) -> None:
    groceries, cinema = env.keyed("food.groceries"), env.keyed("leisure.cinema")
    from_file = env.ledger.category(env.household_id, name="Larder (from the file)")
    from_rule = env.ledger.category(env.household_id, name="Larder (from a rule)")
    env.ledger.rule(
        env.household_id,
        conditions={"type": "payee", "op": "contains", "value": "pennywhistle"},
        actions=[{"type": "set_category", "category_id": str(from_rule.id)}],
    )
    account = env.ledger.account(env.household_id, number_last4="5512", currency="USD")
    entries: list[tuple[Any, ...]] = [
        (2, "-48.10", "PENNYWHISTLE MARKET WEST", from_file.name),
        (9, "-31.75", "PENNYWHISTLE MARKET"),
        (16, "-17.25", "LAMPLIGHT CINEMA"),
    ]

    _, summary = _stored(env, _month("5512", entries))

    rows = env.transactions(account)
    assert _decided(rows["PENNYWHISTLE MARKET WEST"]) == (from_file.id, "file")
    assert rows["PENNYWHISTLE MARKET WEST"].category_confidence == Decimal("0.95")
    assert _decided(rows["PENNYWHISTLE MARKET"]) == (from_rule.id, "rule")
    assert _decided(rows["LAMPLIGHT CINEMA"]) == (cinema.id, "dictionary")
    assert groceries.id not in {row.category_id for row in rows.values()}
    assert summary["categorised"] == {"dictionary": 1, "file": 1, "rule": 1}
    assert (summary["rules_changed"], summary["suggested"]) == (1, 0)
    assert [row.updated_at == row.created_at for row in rows.values()] == [True] * 3


def test_an_issuers_category_words_are_read_as_a_default_category(env: Env) -> None:
    fuel, cinema = env.keyed("transport.fuel"), env.keyed("leisure.cinema")
    account = env.ledger.account(env.household_id, number_last4="5513", currency="USD")
    entries: list[tuple[Any, ...]] = [
        (3, "-41.20", "HARBOURSIDE 0042", "Transportation-Fuel"),
        # The dictionary knows the cinema: the issuer's word does not change it.
        (5, "-17.25", "LAMPLIGHT CINEMA", "Restaurant-Restaurant"),
        # Words that say nothing about what was bought.
        (7, "-9.99", "QUIET HARBOUR 17", "Other-Miscellaneous"),
    ]

    _, summary = _stored(env, _month("5513", entries))

    rows = env.transactions(account)
    assert _decided(rows["HARBOURSIDE 0042"]) == (fuel.id, "file")
    assert rows["HARBOURSIDE 0042"].category_confidence == Decimal("0.85")
    assert _decided(rows["LAMPLIGHT CINEMA"]) == (cinema.id, "dictionary")
    assert _decided(rows["QUIET HARBOUR 17"]) == (None, None)
    assert summary["categorised"] == {"dictionary": 1, "file": 1}
    assert [row.updated_at == row.created_at for row in rows.values()] == [True] * 3


def test_a_row_takes_its_payees_default_before_the_dictionary_answers(env: Env) -> None:
    env.keyed("food.groceries")
    weekly = env.ledger.category(env.household_id, name="Weekly shop")
    payee = env.ledger.payee(env.household_id, name="Pennywhistle Market", default_category=weekly)
    account = env.ledger.account(env.household_id, currency="USD")

    _, summary = env.ingest(_csv(MARCH[:1]), account, "one.csv")

    row = env.transactions(account)["PENNYWHISTLE MARKET"]
    assert (row.payee_id, row.category_id, row.category_source) == (payee.id, weekly.id, "payee")
    assert summary["categorised"] == {"payee": 1}
    assert summary["payee_categorised"] == 1
    assert row.updated_at == row.created_at


def test_a_payee_is_found_behind_a_processor(env: Env) -> None:
    films = env.ledger.category(env.household_id, name="Films at home")
    env.ledger.payee(env.household_id, name="PayPal")
    streambox = env.ledger.payee(env.household_id, name="Streambox", default_category=films)
    account = env.ledger.account(env.household_id, currency="USD")
    entries: list[Entry] = [
        (3, "-11.99", "PAYPAL *STREAMBOX"),
        (4, "-11.99", "PAYPAL *STREAMBOX 4029357733 CA"),
    ]

    _, summary = env.ingest(_csv(entries), account, "behind.csv")

    rows = env.transactions(account)
    for line in ("PAYPAL *STREAMBOX", "PAYPAL *STREAMBOX 4029357733 CA"):
        row = rows[line]
        assert (row.payee_id, row.category_id, row.category_source) == (
            streambox.id,
            films.id,
            "payee",
        )
        assert row.category_confidence == Decimal("0.95")
        assert row.updated_at == row.created_at
    assert summary["categorised"] == {"payee": 2}
    assert summary["payee_categorised"] == 2


def test_a_line_naming_two_payees_gets_neither_whatever_its_core_says(env: Env) -> None:
    fees = env.ledger.category(env.household_id, name="App fees")
    oil = env.ledger.category(env.household_id, name="Heating oil")
    env.ledger.payee(env.household_id, name="Venmo", default_category=fees)
    env.ledger.payee(env.household_id, name="Marta Lindqvist")
    env.ledger.payee(env.household_id, name="Lantern Fuel", default_category=oil)
    env.ledger.payee(env.household_id, name="Oakfield Rentals")
    env.ledger.payee(env.household_id, name="PayPal")
    account = env.ledger.account(env.household_id, currency="USD")
    entries: list[Entry] = [
        # Between people: the core is the app, which is not who was paid.
        (3, "-40.00", "VENMO PAYMENT 1027 MARTA LINDQVIST"),
        # The second payee stands past the sixth word, where the core ends.
        (5, "-212.00", "Lantern Fuel weekly delivery charge for Oakfield Rentals"),
        (6, "-212.00", "PAYPAL *LANTERN FUEL WEEKLY DELIVERY CHARGE FOR OAKFIELD RENTALS"),
        (8, "-212.00", "Lantern Fuel at Oakfield Rentals"),
    ]

    _, summary = env.ingest(_csv(entries), account, "two-payees.csv")

    rows = env.transactions(account)
    assert len(rows) == len(entries)
    found = [(row.payee_id, row.category_id, row.category_source) for row in rows.values()]
    assert found == [(None, None, None)] * len(entries)
    assert summary["categorised"] == {}
    assert (summary["payee_categorised"], summary["suggested"]) == (0, 0)


def test_a_store_that_sells_everything_arrives_as_a_suggestion(
    env: Env, known: dict[str, Merchant]
) -> None:
    groceries = env.keyed("food.groceries")
    known[BAZAAR.key] = BAZAAR
    account = env.ledger.account(env.household_id, currency="USD")
    entries: list[Entry] = [
        (2, "-48.10", "PENNYWHISTLE MARKET"),
        (7, "-36.20", "MARROWFIELD BAZAAR"),
    ]

    _, summary = env.ingest(_csv(entries), account, "mixed.csv")

    rows = env.transactions(account)
    offered = rows["MARROWFIELD BAZAAR"]
    assert offered.merchant_key == BAZAAR.key
    assert (offered.category_id, offered.suggested_category_id) == (None, groceries.id)
    assert (offered.category_source, offered.category_confidence) == ("dictionary", Decimal("0.60"))
    assert offered.updated_at == offered.created_at
    assert _decided(rows["PENNYWHISTLE MARKET"]) == (groceries.id, "dictionary")
    # A suggestion is counted as one and never among the facts.
    assert summary["suggested"] == 1
    assert summary["categorised"] == {"dictionary": 1}


# ---------------------------------------------------------------------------
# What a person chose
# ---------------------------------------------------------------------------


def test_a_second_import_keeps_what_a_person_chose(env: Env) -> None:
    env.keyed("food.groceries")
    env.keyed("leisure.cinema")
    own = env.ledger.category(env.household_id, name="Film nights")
    account = env.ledger.account(env.household_id, currency="USD")
    env.ingest(_csv(MARCH), account, "march.csv")
    chosen = env.transactions(account)["LAMPLIGHT CINEMA"]
    env.choose(chosen, own)

    # The same six lines again, in a later export that has one more.
    later: Entry = (27, "-5.25", "QUARRYSIDE KIOSK")
    _, summary = env.ingest(_csv([*MARCH, later]), account, "march-again.csv")

    rows = env.transactions(account)
    assert (summary["imported"], summary["duplicates"]) == (1, len(MARCH))
    assert summary["categorised"] == {}
    assert len(rows) == len(MARCH) + 1
    kept = rows["LAMPLIGHT CINEMA"]
    assert kept.id == chosen.id
    assert _decided(kept) == (own.id, "person")
    assert kept.category_confidence == Decimal("1.00")
    assert kept.suggested_category_id is None


def test_a_reprocess_removes_a_guessed_row_the_new_reading_dropped(
    env: Env, known: dict[str, Merchant]
) -> None:
    groceries, cinema = env.keyed("food.groceries"), env.keyed("leisure.cinema")
    account = env.ledger.account(env.household_id, number_last4="5513", currency="USD")
    document_id, summary = _stored(env, _month("5513", MARCH))
    assert summary["categorised"] == {"dictionary": 2}
    # A newer dictionary knows one more merchant, and a second look applies it.
    known[TOOL_HIRE.key] = TOOL_HIRE
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        again = engine.reconsider(db, env.household_id, rekey=True)
    assert again.by_source == {"dictionary": 1}
    before = env.transactions(account)
    assert _decided(before["PENNYWHISTLE MARKET"]) == (groceries.id, "dictionary")
    assert _decided(before["HOLLOWAY TOOL HIRE"]) == (cinema.id, "dictionary")

    # The new reading no longer holds the two guessed lines.
    dropped = {"PENNYWHISTLE MARKET", "HOLLOWAY TOOL HIRE"}
    summary = _read(env, document_id, _month("5513", [e for e in MARCH if e[2] not in dropped]))

    after = env.transactions(account)
    assert set(after) == set(before) - dropped
    assert (summary["replaced_rows"], summary["kept_rows"], summary["imported"]) == (2, 0, 0)
    assert env.kept_items(document_id) == []
    assert _decided(after["LAMPLIGHT CINEMA"]) == (cinema.id, "dictionary")


def test_a_reprocess_keeps_a_row_whose_category_a_person_changed(env: Env) -> None:
    env.keyed("food.groceries")
    env.keyed("leisure.cinema")
    own = env.ledger.category(env.household_id, name="Corner shop runs")
    account = env.ledger.account(env.household_id, number_last4="5514", currency="USD")
    document_id, _ = _stored(env, _month("5514", MARCH))
    guessed = env.transactions(account)["PENNYWHISTLE MARKET"]
    assert guessed.category_source == "dictionary"
    env.choose(guessed, own)

    dropped = {"PENNYWHISTLE MARKET", "LAMPLIGHT CINEMA"}
    summary = _read(env, document_id, _month("5514", [e for e in MARCH if e[2] not in dropped]))

    rows = env.transactions(account)
    # The row only the engine wrote is gone; the person's is kept and listed.
    assert "LAMPLIGHT CINEMA" not in rows
    assert rows["PENNYWHISTLE MARKET"].id == guessed.id
    assert _decided(rows["PENNYWHISTLE MARKET"]) == (own.id, "person")
    assert (summary["replaced_rows"], summary["kept_rows"]) == (1, 1)
    assert env.kept_items(document_id) == [
        {
            "reason": "earlier_rows_kept",
            "account_id": str(account.id),
            "count": 1,
            "transactions": [{"transaction_id": str(guessed.id)}],
        }
    ]


def test_learned_applies_on_the_next_import(env: Env) -> None:
    tools = env.ledger.category(env.household_id, name="Garden and shed")
    account = env.ledger.account(env.household_id, currency="USD")
    env.ingest(_csv(MARCH), account, "march.csv")
    corrected = env.transactions(account)["HOLLOWAY TOOL HIRE"]
    assert _decided(corrected) == (None, None)
    env.choose(corrected, tools)

    april: list[Entry] = [
        (8, "-71.15", "HOLLOWAY TOOL HIRE 0042"),
        (20, "-12.00", "WICKFORD FERRY"),
    ]
    _, summary = env.ingest(_csv(april, month=4), account, "april.csv")

    rows = env.transactions(account)
    learned = rows["HOLLOWAY TOOL HIRE 0042"]
    assert learned.merchant_key == corrected.merchant_key == "holloway tool hire"
    assert _decided(learned) == (tools.id, "learned")
    assert learned.category_confidence == Decimal("0.80")
    assert learned.updated_at == learned.created_at
    assert _decided(rows["WICKFORD FERRY"]) == (None, None)
    assert summary["categorised"] == {"learned": 1}
