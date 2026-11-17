# SPDX-License-Identifier: AGPL-3.0-or-later
"""A dismissed cash series is still found once its rows lose their key.

An older clean-up keyed bare cash wording: every "ATM 0412 <town>" line was
the merchant ``atm``, and a detected series was named after the key ("Atm").
The refresh takes the key off the series and its rows; detection then groups
the rows by their text ("atm quarryvale oh"), which is not the series' name.
The series still matches (rank 2) when its name is the key the line once had:
the first words of its core. Real clean-up; every line is invented.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.domain.categorisation import engine
from app.domain.recurring import detection
from app.domain.recurring.detection import Candidate, Observed
from app.domain.recurring.service import _match_rank
from app.domain.transactions.models import ScheduledTransaction

ACCOUNT = uuid.uuid4()


def _candidate(raw: str, amount: str = "-200.00") -> Candidate:
    identity = engine.identify(raw)
    ledger = [
        Observed(
            id=uuid.uuid4(),
            account_id=ACCOUNT,
            posted_date=date(2026, month, 6),
            amount=Decimal(amount),
            merchant_key=identity.key,
            text_key=detection.normalize_payee(raw) or None,
            kind=identity.kind,
            category_key=None,
            category_kind=None,
            series_hint=None,
            payee_id=None,
            category_id=None,
            label=raw,
        )
        for month in range(1, 7)
    ]
    [found] = detection.detect(ledger, {ACCOUNT: date(2026, 6, 6)})
    return found


def _dismissed(name: str, amount: str = "-200.00") -> ScheduledTransaction:
    return ScheduledTransaction(
        account_id=ACCOUNT,
        name=name,
        merchant_key=None,
        payee_id=None,
        amount_min=Decimal(amount),
        amount_max=Decimal(amount),
        status="dismissed",
    )


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        ("ATM 0412 QUARRYVALE OH", "Atm"),
        ("ATM CASH 0042 ELKTON PARK", "Atm Cash 0042 Elkton"),
        ("WITHDRAWAL MADE IN A BRANCH/STORE", "Withdrawal Made In A"),
    ],
)
def test_a_series_named_after_the_old_cash_key_is_the_candidate_s(raw: str, name: str) -> None:
    candidate = _candidate(raw)
    assert candidate.merchant_key is None
    assert candidate.text_key != detection.normalize_payee(name)
    assert _match_rank(_dismissed(name), candidate) == 2


@pytest.mark.parametrize(
    ("raw", "name"),
    [
        # Another cash line's old key: "WITHDRAWAL" was its own merchant.
        ("WITHDRAWAL MADE IN A BRANCH/STORE", "Withdrawal"),
        ("ATM 0412 QUARRYVALE OH", "Atm Cash"),
        # A name that is no cash wording is matched as it is, never by its start.
        ("ATM 0412 QUARRYVALE OH", "Atmosphere"),
    ],
)
def test_another_name_is_not_the_candidate_s(raw: str, name: str) -> None:
    assert _match_rank(_dismissed(name), _candidate(raw)) is None


def test_the_amounts_must_still_overlap() -> None:
    assert _match_rank(_dismissed("Atm", "-60.00"), _candidate("ATM 0412 QUARRYVALE OH")) is None
