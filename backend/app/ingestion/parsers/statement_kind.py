# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the summary on a statement's first page names: a card, a loan bill.

Pure functions on the short summary lines of page 1, each already
normalised (:func:`app.ingestion.parsers.text.normalise`: lower case, no
punctuation, single spaces), used by the ``detect`` of the generic
statement parsers in :mod:`app.ingestion.parsers.table_statement` and
testable on plain strings without a PDF. The caller chooses the lines
(outside the transaction rows, of few words); nothing here reads a figure.

* :func:`card_summary`: the summary is a card's balance equation
  (``Previous Balance``, ``Payments and Credits``, ``Purchases``, ``New
  Balance``). A card statement that prints no card word (no ``credit
  card``, ``card ending``, ``credit limit``) is known by it: read as a bank
  statement its purchases would be income, and its figures would still add
  up.
* :func:`loan_bill`: the summary is a loan's bill (a principal balance or a
  payoff amount, and an amount or a payment that is due). Its activity
  table is no bank statement's.

Both stand down when a line starts with a deposit account's summary label
(:data:`DEPOSIT_LABELS`): a checking statement that says ``New Balance``
is no card, and a member statement that lists a loan beside the share
accounts is no loan bill.

A label **starts** its line where the functions say "starts with" (a label
with its value: ``new balance 350 38``); a sentence that mentions the label
names nothing. Labels are compared by whole words (``new balances`` is not
``new balance``).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Final

#: The line that closes a card's equation.
CARD_BALANCE_LABELS: Final = ("new balance",)
#: What came off the balance, as normalised (``Payments & Credits``,
#: ``Payment/Credits``).
CARD_PAYMENT_LABELS: Final = (
    "payments and credits",
    "payments credits",
    "payment credits",
    "total payments and credits",
)
#: What a card adds or asks for: no deposit account prints these as labels.
CARD_CHARGE_LABELS: Final = ("purchases", "total purchases", "new charges", "minimum payment")
#: Summary labels of a deposit account; one at the start of a line and the
#: summary is neither a card's nor a loan bill's.
DEPOSIT_LABELS: Final = (
    "beginning balance",
    "ending balance",
    "opening balance",
    "closing balance",
    "deposits",
    "withdrawals",
)
#: What a loan owes...
LOAN_BALANCE_PHRASES: Final = ("principal balance", "payoff amount")
#: ...and what its bill asks for.
LOAN_DUE_PHRASES: Final = ("amount due", "payment due")


def _starts(lines: Sequence[str], labels: Sequence[str]) -> bool:
    """One of ``lines`` starts with one of ``labels``, as whole words."""
    return any(f"{line} ".startswith(f"{label} ") for line in lines for label in labels)


def _holds(lines: Sequence[str], phrases: Sequence[str]) -> bool:
    """One of ``lines`` holds one of ``phrases``, as whole words."""
    return any(f" {phrase} " in f" {line} " for line in lines for phrase in phrases)


def card_summary(lines: Sequence[str]) -> bool:
    """The lines are a card's summary: one starts with ``new balance``, one
    with a payments-and-credits label (:data:`CARD_PAYMENT_LABELS`) and one
    with a charge label (:data:`CARD_CHARGE_LABELS`), and none starts with a
    deposit account's label (:data:`DEPOSIT_LABELS`). ``New Balance`` alone
    names nothing: a checking summary prints it too."""
    if _starts(lines, DEPOSIT_LABELS):
        return False
    return (
        _starts(lines, CARD_BALANCE_LABELS)
        and _starts(lines, CARD_PAYMENT_LABELS)
        and _starts(lines, CARD_CHARGE_LABELS)
    )


def loan_bill(lines: Sequence[str]) -> bool:
    """The lines are a loan bill's summary: one holds ``principal balance``
    or ``payoff amount``, one holds ``amount due`` or ``payment due``, and
    none starts with a deposit account's label (:data:`DEPOSIT_LABELS`). An
    amount due alone is a card's (``minimum payment due``)."""
    if _starts(lines, DEPOSIT_LABELS):
        return False
    return _holds(lines, LOAN_BALANCE_PHRASES) and _holds(lines, LOAN_DUE_PHRASES)
