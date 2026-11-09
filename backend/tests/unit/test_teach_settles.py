# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which review items teaching answers (``app.domain.review.teach._settles``).

A fact applied closes an item only when it is what the item asks for: the
period for a period item, the institution for a new institution; an
institution or an account set without rows imported answers no unknown
account or possible duplicate.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.domain.documents.models import ReviewItem, ReviewKind
from app.domain.review.teach import _settles


def _item(kind: ReviewKind, **payload: Any) -> ReviewItem:
    return ReviewItem(kind=kind, payload=payload)


def _answers(
    item: ReviewItem,
    applied: list[str],
    *,
    rerun: str | None = None,
    reconciled: bool = False,
    remembered: bool = False,
) -> bool:
    return _settles(
        item, reconciled=reconciled, rerun=rerun, applied=applied, remembered=remembered
    )


@pytest.mark.parametrize(
    "kind",
    [
        ReviewKind.UNKNOWN_ACCOUNT,
        ReviewKind.POSSIBLE_DUPLICATE,
        ReviewKind.UNKNOWN_OWNER,
        ReviewKind.RECEIPT_MATCH,
        ReviewKind.UNPARSED,
    ],
)
def test_unrelated_facts_answer_nothing(kind: ReviewKind) -> None:
    item = _item(kind, reason="account_not_found")
    assert not _answers(item, ["institution_id", "account_id", "period_start"])
    assert not _answers(item, ["institution_id"], remembered=True)


def test_a_low_confidence_item_is_answered_by_the_fields_it_names() -> None:
    period = _item(ReviewKind.LOW_CONFIDENCE_FIELD, reason="low_confidence", fields=["period"])
    assert _answers(period, ["period_end"])
    assert not _answers(period, ["institution_id"])
    balance = _item(ReviewKind.LOW_CONFIDENCE_FIELD, reason="balance_mismatch", field="balance")
    assert _answers(balance, ["closing_balance"])
    assert not _answers(balance, ["period_start", "account_id"])
    rows = _item(
        ReviewKind.LOW_CONFIDENCE_FIELD, reason="investment_low_confidence", fields=["price"]
    )
    assert not _answers(rows, ["institution_id", "period_start", "statements"])


def test_a_new_institution_is_answered_by_the_institution_or_the_layout() -> None:
    item = _item(ReviewKind.NEW_INSTITUTION, reason="unknown_layout")
    assert _answers(item, ["institution_id"])
    assert _answers(item, [], remembered=True)
    assert not _answers(item, ["period_start"])


def test_reading_again_answers_what_a_re_run_answers() -> None:
    assert _answers(
        _item(ReviewKind.UNKNOWN_ACCOUNT, reason="account_not_found"), [], rerun="import"
    )
    assert _answers(_item(ReviewKind.UNPARSED, reason="no_records"), [], rerun="import")
    skipped = _item(ReviewKind.LOW_CONFIDENCE_FIELD, reason="lines_skipped")
    assert _answers(skipped, ["statements"], rerun="import")
    duplicate = _item(ReviewKind.POSSIBLE_DUPLICATE, reason="near_match")
    assert not _answers(duplicate, ["statements"], rerun="import")
    # The pipeline reads the whole document again and raises afresh what is still wrong.
    assert _answers(duplicate, [], rerun="pipeline")


def test_a_held_statement_is_answered_only_when_it_reconciles() -> None:
    held = _item(
        ReviewKind.LOW_CONFIDENCE_FIELD, reason="reconciliation_failed", field="reconciliation"
    )
    assert not _answers(held, ["closing_balance"], rerun="pipeline")
    assert _answers(held, ["closing_balance"], reconciled=True)
