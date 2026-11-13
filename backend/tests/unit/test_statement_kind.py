# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a page-1 summary names, on hand-built lines.

:mod:`app.ingestion.parsers.statement_kind` holds pure functions on the
normalised short lines of a statement's first page, so they are tested here
without a PDF: every line is an invented label with an invented figure.
"""

from __future__ import annotations

import pytest

from app.ingestion.parsers.statement_kind import card_summary, loan_bill
from app.ingestion.parsers.text import normalise


def _lines(*printed: str) -> list[str]:
    """The lines as the reader hands them over: normalised."""
    return [normalise(line) for line in printed]


CARD = ("Previous Balance $412.07", "Payments and Credits -$150.00", "Purchases $88.31")


@pytest.mark.parametrize(
    "payments",
    [
        "Payments and Credits -$150.00",
        "Payments & Credits 150.00",
        "Payment/Credits 150.00",
        "Total Payments and Credits 150.00",
    ],
)
@pytest.mark.parametrize(
    "charges",
    [
        "Purchases $88.31",
        "Total Purchases 88.31",
        "New Charges 88.31",
        "Minimum Payment Due $25.00",
    ],
)
def test_card_equation_is_a_card(payments: str, charges: str) -> None:
    lines = _lines("Previous Balance $412.07", payments, charges, "New Balance $350.38")
    assert card_summary(lines)


def test_new_balance_alone_is_no_card() -> None:
    assert not card_summary(_lines("New Balance $350.38"))
    # Two of the three lines are not the equation either.
    assert not card_summary(_lines("New Balance $350.38", "Payments and Credits 150.00"))
    assert not card_summary(_lines("New Balance $350.38", "Purchases 88.31"))
    assert not card_summary(_lines("Payments and Credits 150.00", "Purchases 88.31"))
    assert not card_summary([])


def test_a_label_inside_a_line_is_no_card_line() -> None:
    # The label starts its line; a sentence that mentions it names nothing.
    lines = _lines(
        "Your new balance is shown below",
        "See payments and credits overleaf",
        "All purchases this period",
    )
    assert not card_summary(lines)
    # A longer word that starts with a label is another word.
    assert not card_summary(_lines(*CARD[1:], "New Balances 350.38"))


@pytest.mark.parametrize(
    "deposit",
    [
        "Beginning balance 10.00",
        "Ending Balance 96.24",
        "Opening Balance 10.00",
        "Closing Balance 96.24",
        "Deposits and Additions 200.00",
        "Withdrawals 113.76",
    ],
)
def test_a_deposit_summary_is_no_card(deposit: str) -> None:
    assert card_summary(_lines(*CARD, "New Balance $350.38"))
    assert not card_summary(_lines(*CARD, "New Balance $350.38", deposit))


LOAN = ("Current Principal Balance $18,204.55", "Total Amount Due $342.10")


@pytest.mark.parametrize(
    "balance", ["Current Principal Balance $18,204.55", "Payoff Amount 18,311.02"]
)
@pytest.mark.parametrize("due", ["Total Amount Due $342.10", "Payment Due Date 12/05/2026"])
def test_loan_bill(balance: str, due: str) -> None:
    assert loan_bill(_lines("Dunmere Auto Lending", balance, due))


def test_amount_due_alone_is_no_loan_bill() -> None:
    # A card prints a payment that is due and no principal.
    assert not loan_bill(_lines("New Balance $350.38", "Minimum Payment Due $25.00"))
    assert not loan_bill(_lines("Total Amount Due $342.10"))
    assert not loan_bill(_lines("Current Principal Balance $18,204.55"))
    assert not loan_bill(_lines("Principal balances are reviewed yearly", "Amount due 1.00"))
    assert not loan_bill([])


def test_a_member_statement_is_no_loan_bill() -> None:
    # A credit union prints a member's loan beside the share accounts.
    assert loan_bill(_lines(*LOAN))
    assert not loan_bill(_lines(*LOAN, "Beginning Balance 1,250.00"))
    assert not loan_bill(_lines(*LOAN, "Deposits 400.00"))
    # The deposit phrase starts its line: a note that mentions one vetoes nothing.
    assert loan_bill(_lines(*LOAN, "AutoPay withdrawals post on the due date"))


def test_a_card_line_that_mentions_withdrawals_is_still_a_card() -> None:
    # The card's veto is by line start: a card prints its cash withdrawals.
    assert card_summary(_lines(*CARD, "New Balance $350.38", "Cash withdrawals 0.00"))
    assert card_summary(_lines(*CARD, "New Balance $350.38", "Total Deposits 0.00"))


@pytest.mark.parametrize(
    "deposit",
    [
        # A total, a share's name or a count comes before the label.
        "Total Deposits 400.00",
        "Total Withdrawals 113.76",
        "Share Savings Beginning Balance 1,250.00",
        "Share Savings Ending Balance 1,536.24",
        "2 Deposits 400.00",
        "1 Withdrawals 10.00",
        "Deposits and Credits 400.00",
        # A checking summary that prints neither word.
        "Starting Balance 1,250.00",
        "Balance Forward 1,250.00",
    ],
)
def test_a_deposit_label_behind_a_prefix_is_no_loan_bill(deposit: str) -> None:
    assert loan_bill(_lines(*LOAN))
    assert not loan_bill(_lines(*LOAN, deposit))


def test_a_checking_summary_with_a_credit_line_is_no_loan_bill() -> None:
    # An overdraft line of credit printed under a checking summary.
    lines = _lines(
        "Starting Balance 1,250.00",
        "Total Credits 400.00",
        "Total Debits 113.76",
        "Current Balance 1,536.24",
        "Principal Balance 600.00",
        "Minimum Payment Due 25.00",
    )
    assert not loan_bill(lines)


@pytest.mark.parametrize(
    "note",
    [
        # A sentence that mentions a deposit word labels nothing.
        "AutoPay withdrawals post on the due date",
        "Payments are not deposits and post in two days",
        "Your ending balance is shown on the coupon below",
        "See the balance forward notice overleaf",
        # A loan's escrow account is the loan's, whatever it prints.
        "Escrow Deposits 212.40",
        "Escrow Beginning Balance 1,804.11",
        "Escrow Ending Balance 2,016.51",
    ],
)
def test_a_note_or_an_escrow_line_leaves_a_loan_bill(note: str) -> None:
    assert loan_bill(_lines(*LOAN, note))


def test_a_card_with_a_plan_principal_is_no_loan_bill() -> None:
    # A card prints an instalment plan's principal beside its payment due date.
    plan = "Plan Principal Balance 600.00"
    assert not loan_bill(
        _lines("New Balance $350.38", "Minimum Payment Due 25.00", "Payment Due Date 12/05", plan)
    )
    assert not loan_bill(_lines(*CARD, "New Balance $350.38", "Payment Due Date 12/05", plan))
    # One card label alone does not make a card of a loan bill.
    assert loan_bill(_lines(*LOAN, "New Balance $18,204.55"))
    assert loan_bill(_lines(*LOAN, "Minimum Payment Due 342.10"))
    assert loan_bill(_lines(*LOAN, "Your new balance and purchases are overleaf"))
