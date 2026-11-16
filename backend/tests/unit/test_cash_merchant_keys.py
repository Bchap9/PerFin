# SPDX-License-Identifier: AGPL-3.0-or-later
"""Bare cash wording names no merchant.

A line that says only that cash went in or out ("WITHDRAWAL", "CASH
DEPOSIT", "TELLER DEPOSIT", an ATM line) names nobody, like the cash lines
clean-up already reads (``ATM WITHDRAWAL``): its merchant key is ``None``
(docs/DATA_MODEL.md, ``transaction.merchant_key``). As a key, every such
line of a household would be one merchant, for what is learned, for a rule
and for a series. Clean-up's core is left as it is, so the keywords still
read it: a withdrawal is still suggested as cash, money out only, and a
cash deposit as cash, money in only. A line that names somebody besides
the cash words keeps its key.

Real clean-up, real dictionary; every descriptor is invented.
"""

from __future__ import annotations

import pytest

from app.domain.categorisation import cleanup, engine
from app.domain.categorisation.memory import Direction
from tests.fixtures.quality import DEFAULT_CATEGORIES, NOW

_KEY_OF = {category_id: key for key, category_id in DEFAULT_CATEGORIES.items()}


@pytest.mark.parametrize(
    "raw",
    [
        "WITHDRAWAL",
        "Withdrawal",
        "WITHDRAWAL 0042",
        "WITHDRAWAL - ATM",
        "CASH DEPOSIT",
        "Cash Deposit",
        "TELLER DEPOSIT",
        "COUNTER WITHDRAWAL",
        "CASH",
        "ATM",
        "ATM CASH",
        "ATM CASH DEPOSIT",
        "ATM 0412 QUARRYVALE",
    ],
)
def test_bare_cash_wording_names_no_merchant(raw: str) -> None:
    identity = engine.identify(raw)

    assert identity.key is None, raw
    assert identity.name is None
    assert identity.also is None
    # Clean-up still read the line: only the key is withheld.
    assert identity.core == cleanup.clean(raw).core != ""


@pytest.mark.parametrize(
    ("raw", "key"),
    [
        ("CASH APP", "cash app"),
        ("WITHDRAWAL QUARRYVALE BAKEHOUSE", "withdrawal quarryvale bakehouse"),
        ("BRANCH", "branch"),
        ("TELLER", "teller"),
    ],
)
def test_a_line_that_names_somebody_keeps_its_key(raw: str, key: str) -> None:
    assert engine.identify(raw).key == key


def _suggested(raw: str, direction: Direction) -> str | None:
    decision = engine.decide(
        engine.identify(raw),
        direction,
        learned=None,
        payee_default=None,
        categories=DEFAULT_CATEGORIES,
        now=NOW,
    )
    assert decision.category_id is None, raw
    found = decision.suggested_category_id
    return _KEY_OF[found] if found is not None else None


def test_cash_wording_is_still_suggested_as_cash_the_way_the_money_went() -> None:
    assert _suggested("WITHDRAWAL", "out") == "transfers.cash"
    assert _suggested("WITHDRAWAL", "in") is None
    assert _suggested("CASH DEPOSIT", "in") == "transfers.cash"
    assert _suggested("CASH DEPOSIT", "out") is None
