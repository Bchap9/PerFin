# SPDX-License-Identifier: AGPL-3.0-or-later
"""Brokerage positions exports end to end (INGESTION.md "Brokerage positions exports").

upload -> worker ``ingest_document`` -> real sandbox (delimited extractor) ->
structured importer -> positions table parser
(``app.ingestion.parsers.positions_table``) ->
``app.domain.investments.statement_import`` as a positions snapshot. The
file is the synthetic export of ``tests/fixtures/synth_positions_csv.py``
(the real header, invented accounts, funds and amounts).

* Each account's holdings land on the account with its last four digits,
  with the cost basis the export prints, a price per position and the
  account's value (holdings plus the core position's cash).
* A later export brings the holdings to what it lists: more units enter at
  the cost that makes the basis the printed one, a position that shrank is
  reset to the printed units and basis, one no longer listed leaves; the
  same holdings again add nothing.
* A position with lots of its own (bought through an activity export) keeps
  them; only its units follow the export.
* Exports and activity out of order are held, not applied.
* A file whose figures do not add up (a value that is not quantity x price)
  waits for "import as read"; accounts the file names without digits are
  created from its proposals.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.auth.principal import apply_rls_context
from app.db.rls import set_context
from app.domain.accounts.models import Account, AccountType, BalanceSnapshot, BalanceSource
from app.domain.accounts.schemas import AccountCreate
from app.domain.documents.models import DocumentStatus, ReviewItem
from app.domain.imports import proposals
from app.domain.imports.schemas import (
    APPLY_MAPPING_JOB_TYPE,
    AccountChoice,
    DocumentAccounts,
    DocumentAccountsApply,
)
from app.domain.investments.models import (
    CostMethod,
    Holding,
    InvestmentAccount,
    InvestmentTxn,
    Lot,
    Price,
    Security,
)
from app.domain.investments.statement_import import (
    SNAPSHOT_IN_MEMO,
    SNAPSHOT_OUT_MEMO,
    STATEMENT_PRICE_SOURCE,
)
from app.domain.review import service as review_service
from app.domain.review.schemas import ResolveLowConfidence
from tests.fixtures.imports import run_jobs
from tests.fixtures.synth_activity_csv import ActivityAccount, ActivityRow, activity_csv_bytes
from tests.fixtures.synth_positions_csv import (
    BOND,
    GROWTH,
    HEADER,
    VALUE,
    PositionsAccount,
    positions_csv_bytes,
    two_accounts,
)
from tests.fixtures.synth_positions_csv import (
    Holding as Held,
)
from tests.integration.test_activity_table_import import Env, _items, env

pytestmark = pytest.mark.integration
__all__ = ["env"]

DAY = date(2026, 11, 11)
LATER = date(2026, 12, 11)
CENT = Decimal("0.01")
Position = tuple[str, Decimal, Decimal]


def _account(env: Env, synthetic: PositionsAccount, kind: AccountType) -> Account:
    return env.ledger.account(
        env.household_id,
        type=kind,
        number_last4=synthetic.last4,
        currency="USD",
        name=synthetic.name,
    )


def _held(env: Env, account: Account) -> list[Position]:
    """``(symbol, units, cost basis to the cent)`` of what the account holds."""
    names = {s.id: s.symbol or s.name for s in env.query(select(Security))}
    rows: list[Holding] = env.query(select(Holding).where(Holding.account_id == account.id))
    return sorted(
        (names[h.security_id], h.quantity, h.cost_basis.quantize(CENT))
        for h in rows
        if h.quantity != 0
    )


def _printed(synthetic: PositionsAccount) -> list[Position]:
    return sorted((h.symbol, h.quantity, h.cost or Decimal(0)) for h in synthetic.holdings)


def _rows(env: Env, account: Account) -> list[tuple[date, str, str, Decimal | None, str | None]]:
    names = {s.id: s.symbol or s.name for s in env.query(select(Security))}
    txns: list[InvestmentTxn] = env.query(
        select(InvestmentTxn)
        .where(InvestmentTxn.account_id == account.id)
        .order_by(InvestmentTxn.trade_date, InvestmentTxn.id)
    )
    return [
        (t.trade_date, str(t.type), names[t.security_id], t.quantity, t.memo)
        for t in txns
        if t.security_id is not None
    ]


def _snapshots(env: Env, account: Account) -> list[tuple[date, Decimal, BalanceSource]]:
    rows: list[BalanceSnapshot] = env.query(
        select(BalanceSnapshot)
        .where(BalanceSnapshot.account_id == account.id)
        .order_by(BalanceSnapshot.as_of_date)
    )
    return [(s.as_of_date, s.balance.quantize(CENT), s.source) for s in rows]


def _reasons(env: Env, document_id: uuid.UUID) -> list[str]:
    return sorted(str(payload.get("reason")) for _, payload in _items(env, document_id))


# ---------------------------------------------------------------------------
# Through the worker
# ---------------------------------------------------------------------------


def test_positions_export_lands_as_holdings_with_cost_basis_on_each_account(env: Env) -> None:
    synthetic = two_accounts()
    taxable = _account(env, synthetic[0], AccountType.BROKERAGE)
    roth = _account(env, synthetic[1], AccountType.RETIREMENT)
    document_id = env.ingest(positions_csv_bytes(synthetic), "Portfolio_Positions_Nov-11-2026.csv")

    document = env.document(document_id)
    assert document.status is DocumentStatus.PROCESSED, _items(env, document_id)
    assert document.account_id is None  # two accounts: linked to neither
    assert (document.period_start, document.period_end) == (DAY, DAY)
    assert document.parser_version == "brokerage_positions_table/1"
    for account, mine in ((taxable, synthetic[0]), (roth, synthetic[1])):
        assert _held(env, account) == _printed(mine), mine.name
        # The account's value: its holdings and the core position's cash.
        assert _snapshots(env, account) == [(DAY, mine.total, BalanceSource.STATEMENT)]
    # One opening per position, on the day of the download; the cash row is no holding.
    assert sorted(_rows(env, taxable)) == sorted(
        (DAY, "transfer_in", h.symbol, h.quantity, SNAPSHOT_IN_MEMO) for h in (GROWTH, BOND)
    )
    prices: list[Price] = env.query(select(Price))
    by_symbol = {s.id: s.symbol for s in env.query(select(Security))}
    assert sorted((by_symbol[p.security_id], p.close, p.price_date, p.source) for p in prices) == [
        (h.symbol, h.price, DAY, STATEMENT_PRICE_SOURCE) for h in (BOND, GROWTH, VALUE)
    ]
    assert env.mappings() == []  # not a cash ledger: no column mapping was made
    (log,) = env.parse_logs(document_id)
    assert log["positions_table"]["statements"] == 2
    assert log["positions_table"]["positions"] == 3
    assert log["positions_table"]["cash_rows"] == 2
    assert log["positions_table"]["as_of"] == "2026-11-11"
    assert log["investments"]["opening_positions"] == 3
    assert log["investments"]["snapshots"] == 2
    assert "brokerage_table" not in log
    # Account numbers never reach the trail, not even the last four digits.
    trail = env.trail()
    for account_ in synthetic:
        assert account_.number not in trail
        assert account_.last4 not in trail

    # Re-running the import from the stored extraction adds nothing.
    before = (_rows(env, taxable), _rows(env, roth))
    env.rerun(document_id)
    assert (_rows(env, taxable), _rows(env, roth)) == before
    assert env.parse_logs(document_id)[-1]["investments"]["opening_positions"] == 0
    assert env.document(document_id).status is DocumentStatus.PROCESSED


def test_a_later_export_brings_the_holdings_to_what_it_lists(env: Env) -> None:
    first = PositionsAccount("Z40001111", "Individual", (GROWTH, BOND, VALUE), Decimal("1000.75"))
    account = _account(env, first, AccountType.BROKERAGE)
    env.ingest(positions_csv_bytes([first]))
    assert _held(env, account) == _printed(first)

    # A month on: more of the growth fund (bought for 500.00 in all), part of
    # the bond fund sold, the value fund gone.
    more = Held(GROWTH.symbol, GROWTH.name, Decimal("62.069"), Decimal("47.10"), Decimal("2737.68"))
    less = Held(BOND.symbol, BOND.name, Decimal("15"), Decimal("10.40"), Decimal("148.20"))
    second = PositionsAccount("Z40001111", "Individual", (more, less), Decimal("1650.00"))
    document_id = env.ingest(positions_csv_bytes([second], downloaded=LATER), "december.csv")
    assert env.document(document_id).status is DocumentStatus.PROCESSED, _items(env, document_id)
    assert _held(env, account) == _printed(second)
    added = [r for r in _rows(env, account) if r[0] == LATER]
    assert sorted(added) == sorted(
        [
            # Ten more units, costed so the position's basis is the printed one.
            (LATER, "transfer_in", GROWTH.symbol, Decimal("10"), SNAPSHOT_IN_MEMO),
            # Known only from exports: reset to the printed units and basis.
            (LATER, "transfer_out", BOND.symbol, Decimal("40"), SNAPSHOT_OUT_MEMO),
            (LATER, "transfer_in", BOND.symbol, Decimal("15"), SNAPSHOT_IN_MEMO),
            # No longer listed: every unit leaves.
            (LATER, "transfer_out", VALUE.symbol, VALUE.quantity, SNAPSHOT_OUT_MEMO),
        ]
    )
    bought: list[InvestmentTxn] = env.query(
        select(InvestmentTxn).where(
            InvestmentTxn.account_id == account.id,
            InvestmentTxn.trade_date == LATER,
            InvestmentTxn.quantity == Decimal("10"),
        )
    )
    assert bought[0].price is not None
    # 500.00 of new basis over ten units.
    assert bought[0].price.quantize(Decimal("0.000001")) == Decimal("50")
    # The earlier lot of the growth fund keeps its day.
    lots: list[Lot] = env.query(
        select(Lot).where(Lot.account_id == account.id, Lot.remaining_quantity > 0)
    )
    assert sorted((lot.open_date, lot.remaining_quantity) for lot in lots) == [
        (DAY, GROWTH.quantity),
        (LATER, Decimal("10")),
        (LATER, Decimal("15")),
    ]
    assert _snapshots(env, account) == [
        (DAY, first.total, BalanceSource.STATEMENT),
        (LATER, second.total, BalanceSource.STATEMENT),
    ]

    # The same holdings a day later: nothing moves, the value is recorded.
    before = _rows(env, account)
    again = env.ingest(positions_csv_bytes([second], downloaded=date(2026, 12, 12)), "again.csv")
    assert env.document(again).status is DocumentStatus.PROCESSED, _items(env, again)
    assert _rows(env, account) == before
    assert env.parse_logs(again)[-1]["investments"]["opening_positions"] == 0
    assert len(_snapshots(env, account)) == 3


def test_an_older_export_is_not_applied_over_a_newer_one(env: Env) -> None:
    newer = PositionsAccount("Z40001111", "Individual", (GROWTH,), Decimal("10.00"))
    older = PositionsAccount("Z40001111", "Individual", (GROWTH, BOND), Decimal("10.00"))
    account = _account(env, newer, AccountType.BROKERAGE)
    env.ingest(positions_csv_bytes([newer], downloaded=LATER), "december.csv")
    document_id = env.ingest(positions_csv_bytes([older], downloaded=DAY), "november.csv")
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert _reasons(env, document_id) == ["investment_out_of_order"]
    assert _held(env, account) == _printed(newer)
    assert [day for day, _, _ in _snapshots(env, account)] == [LATER]

    # An older export that lists what the account holds anyway changes nothing and is fine.
    same = env.ingest(positions_csv_bytes([newer], downloaded=date(2026, 11, 30)), "same.csv")
    assert env.document(same).status is DocumentStatus.PROCESSED, _items(env, same)
    assert _held(env, account) == _printed(newer)


def _bought(account: ActivityAccount, day: date, quantity: str, amount: str) -> ActivityRow:
    return ActivityRow(
        run_date=day,
        account=account,
        action=f"YOU BOUGHT {GROWTH.name} ({GROWTH.symbol}) (Cash)",
        symbol=GROWTH.symbol,
        description=GROWTH.name,
        price=Decimal("40.00"),
        quantity=Decimal(quantity),
        amount=Decimal(amount),
        settlement=day,
    )


def test_a_position_with_lots_of_its_own_keeps_them(env: Env) -> None:
    """Bought through an activity export: the positions export only trues up the units."""
    number = "Z40001111"
    account = _account(env, PositionsAccount(number, "Individual"), AccountType.BROKERAGE)
    activity = ActivityAccount("Individual", number)
    env.ingest(
        activity_csv_bytes(
            [
                _bought(activity, date(2026, 10, 5), "10", "-400.00"),
                _bought(activity, date(2026, 10, 20), "5", "-210.00"),
            ]
        )
    )
    assert _held(env, account) == [(GROWTH.symbol, Decimal("15"), Decimal("610.00"))]

    # The broker's own basis differs (it would be reset, were it not for the
    # purchases on record) and there are two more units.
    listed = Held(GROWTH.symbol, GROWTH.name, Decimal("17"), Decimal("45.00"), Decimal("600.00"))
    export = PositionsAccount(number, "Individual", (listed,), Decimal("5.00"))
    document_id = env.ingest(positions_csv_bytes([export]), "positions.csv")
    assert env.document(document_id).status is DocumentStatus.PROCESSED, _items(env, document_id)
    rows = _rows(env, account)
    assert [(r[1], r[3]) for r in rows] == [
        ("buy", Decimal("10")),
        ("buy", Decimal("5")),
        ("transfer_in", Decimal("2")),
    ]
    # The two units enter at the price (their basis is not the export's to give).
    assert _held(env, account) == [(GROWTH.symbol, Decimal("17"), Decimal("700.00"))]

    # Fewer units next time: they leave first in, first out; the rest stays.
    fewer = Held(GROWTH.symbol, GROWTH.name, Decimal("12"), Decimal("45.00"), Decimal("480.00"))
    env.ingest(
        positions_csv_bytes(
            [PositionsAccount(number, "Individual", (fewer,), Decimal("5.00"))], downloaded=LATER
        ),
        "december.csv",
    )
    assert _rows(env, account)[-1][1:4] == ("transfer_out", GROWTH.symbol, Decimal("5"))
    assert _held(env, account) == [(GROWTH.symbol, Decimal("12"), Decimal("500.00"))]


def test_activity_from_before_a_positions_export_is_held(env: Env) -> None:
    """The export already counts what those rows bought: adding them would double units."""
    number = "Z40001111"
    export = PositionsAccount(number, "Individual", (GROWTH,), Decimal("5.00"))
    account = _account(env, export, AccountType.BROKERAGE)
    env.ingest(positions_csv_bytes([export]))
    document_id = env.ingest(
        activity_csv_bytes(
            [
                _bought(ActivityAccount("Individual", number), date(2026, 10, 20), "10", "-400.00"),
                _bought(ActivityAccount("Individual", number), date(2026, 10, 28), "5", "-210.00"),
            ]
        ),
        "activity.csv",
    )
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW, env.document(
        document_id
    ).error
    assert _reasons(env, document_id) == ["investment_out_of_order"]
    assert _held(env, account) == _printed(export)


def test_an_average_cost_account_is_never_reset(env: Env) -> None:
    first = PositionsAccount("Z40001111", "Individual", (BOND,), Decimal("0.00"))
    account = _account(env, first, AccountType.BROKERAGE)
    with env.factories.transaction() as db:
        set_context(db, env.household_id)
        db.add(
            InvestmentAccount(
                household_id=env.household_id, account_id=account.id, cost_method=CostMethod.AVERAGE
            )
        )
    env.ingest(positions_csv_bytes([first]))
    less = Held(BOND.symbol, BOND.name, Decimal("15"), Decimal("10.40"), Decimal("148.20"))
    env.ingest(
        positions_csv_bytes(
            [PositionsAccount("Z40001111", "Individual", (less,), Decimal("0.00"))],
            downloaded=LATER,
        ),
        "december.csv",
    )
    assert _rows(env, account)[-1][1:4] == ("transfer_out", BOND.symbol, Decimal("25"))
    # Fifteen of forty units at the average cost of 10.00.
    assert _held(env, account) == [(BOND.symbol, Decimal("15"), Decimal("150.00"))]


def test_a_position_without_a_printed_basis_asks_for_it(env: Env) -> None:
    unknown = Held(VALUE.symbol, VALUE.name, VALUE.quantity, VALUE.price, None)
    export = PositionsAccount("Z40001111", "Individual", (GROWTH, unknown), Decimal("5.00"))
    account = _account(env, export, AccountType.BROKERAGE)
    document_id = env.ingest(positions_csv_bytes([export]))
    assert _reasons(env, document_id) == ["investment_opening_basis_unknown"]
    # Opened at the price until a person says what it cost.
    assert _held(env, account) == sorted(
        [
            (GROWTH.symbol, GROWTH.quantity, GROWTH.cost or Decimal(0)),
            (VALUE.symbol, VALUE.quantity, VALUE.value),
        ]
    )


# ---------------------------------------------------------------------------
# A sample whose numbers were edited by hand; accounts named without digits
# ---------------------------------------------------------------------------


def _sample() -> bytes:
    """Masked numbers and symbols, one value that is not quantity x price."""

    def row(name: str, description: str, *cells: str) -> str:
        quantity, price, value, percent, cost = cells
        return ",".join(
            [
                "XXX",
                name,
                "XXX",
                description,
                quantity,
                f'"{price}"' if price else "",
                "",
                f'"{value}"',
                "",
                "",
                "",
                "",
                percent,
                f'"{cost}"' if cost else "",
                "",
                "Cash",
            ]
        )

    lines = [
        ",".join(cell.capitalize() for cell in HEADER),
        row(
            "Individual",
            "SYNTHETIC LARGE CAP GROWTH INDEX FUND",
            "52.069",
            "$45.78 ",
            "$1,000.71 ",
            "40.32%",
            "$2,237.68 ",
        ),
        row(
            "Cash Management (Individual)",
            "HELD IN MONEY MARKET",
            "",
            "",
            "$1,000.75 ",
            "100.00%",
            "",
        ),
        "",
        '"Date downloaded Nov-11-2026 7:35 p.m ET"',
    ]
    return ("\r\n".join(lines) + "\r\n").encode()


def _detect(env: Env, document_id: uuid.UUID) -> DocumentAccounts:
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        return proposals.detect_accounts(db, env.principal(), document_id)


def _create_all(env: Env, document_id: uuid.UUID) -> list[uuid.UUID]:
    found = _detect(env, document_id)
    choices = []
    for detected in found.accounts:
        proposal = detected.proposal
        assert proposal is not None
        choices.append(
            AccountChoice(
                target=detected.target,
                index=detected.index,
                create=AccountCreate(
                    type=proposal.type,
                    subtype=proposal.subtype,
                    name=proposal.name,
                    currency=proposal.currency or "USD",
                ),
                institution_name=proposal.institution_name,
            )
        )
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        applied = proposals.apply_accounts(
            db, env.principal(), document_id, DocumentAccountsApply(items=choices), request=None
        )
    env.run_apply()
    return applied.created


def _import_as_read(env: Env, item: ReviewItem) -> None:
    with env.factories.transaction() as db:
        apply_rls_context(db, env.principal())
        review_service.resolve(
            db,
            env.principal(),
            item.id,
            ResolveLowConfidence(kind="low_confidence_field", import_as_read=True),
            request=None,
        )
    assert run_jobs(env.factories, env.household_id, APPLY_MAPPING_JOB_TYPE, done=env.done)


def test_an_edited_sample_creates_its_accounts_and_waits_for_import_as_read(env: Env) -> None:
    document_id = env.ingest(_sample(), "Demo_Portfolio_Positions_Nov-11-2026.csv")
    assert env.document(document_id).status is DocumentStatus.NEEDS_REVIEW
    assert _reasons(env, document_id) == ["account_not_found", "account_not_found"]

    found = _detect(env, document_id)
    assert [(a.status, a.name, a.last4) for a in found.accounts] == [
        ("new", "Individual", None),
        ("new", "Cash Management (Individual)", None),
    ]
    proposals_ = [a.proposal for a in found.accounts]
    assert [p.name for p in proposals_ if p is not None] == [
        "Fidelity Individual",
        "Fidelity Cash Management (Individual)",
    ]
    assert all(p is not None and p.type is AccountType.BROKERAGE for p in proposals_)
    fund_id, cash_id = _create_all(env, document_id)
    accounts = {a.id: a for a in env.query(select(Account))}

    # The cash account is whole: its value is recorded.
    assert _snapshots(env, accounts[cash_id]) == [
        (DAY, Decimal("1000.75"), BalanceSource.STATEMENT)
    ]
    assert _held(env, accounts[cash_id]) == []
    # The fund's value is not quantity x price: it waits, nothing of it is written.
    (item,) = env.open_reviews(document_id)
    assert item.payload["reason"] == "investment_reconciliation_failed"
    assert item.payload["failed"] == ["positions"]
    (difference,) = item.payload["differences"]
    assert (difference["printed"], difference["computed"]) == ("1000.710000", "2383.718820")
    assert _held(env, accounts[fund_id]) == []

    _import_as_read(env, item)
    assert env.document(document_id).status is DocumentStatus.PROCESSED, _items(env, document_id)
    assert _held(env, accounts[fund_id]) == [("XXX", Decimal("52.069"), Decimal("2237.68"))]
    # Figures that did not add up are never the account's balance.
    assert _snapshots(env, accounts[fund_id]) == []
    prices: list[Any] = env.query(select(Price))
    assert [(p.close, p.price_date) for p in prices] == [(Decimal("45.78"), DAY)]
