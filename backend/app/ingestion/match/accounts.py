# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which account a statement belongs to (INGESTION.md "Classify").

A statement names its account by the **last four digits** (OFX ``ACCTID``;
the full number never leaves the sandbox) and, in OFX, its institution
(``FI/ORG``). :func:`match_account` picks the household's account with those
last four digits; when several share them, the one whose institution has the
same name (compared like payees: case, accents and punctuation ignored).
Anything else (none, or still several) is not a match: the importer raises an
``unknown_account`` review item naming the candidates.

A statement that prints **no digits** at all (many card and savings
statements mask the whole number) is matched by its institution instead: the
one open account of the household at ``institution_id``, on the statement's
side of the balance sheet when the caller knows it (``liability``: a card
statement never lands on the checking account of the same bank). Several
such accounts are candidates and none is chosen. Nor is a sole account of a
kind that cannot hold the statement (:data:`INSTITUTION_MATCH_TYPES`): a
bank statement is put on a checking or savings account and a card statement
on a credit card, never on a brokerage, retirement, loan, mortgage, line of
credit or manual account that happens to be the only one there; with the
side unknown only a checking or savings account is chosen. Such an account
is still named as a candidate, for a person to choose. Digits that are printed and
match no account never fall back to the institution: a number the household
does not have is a new account, not the old one. :attr:`AccountMatch.by` says
which of the two a match went by.

Several statements of one document may carry the **same** last four digits
(a checking and a savings account printed under one masked number). The
digits then say nothing about which is which: :func:`match_shared_number`
puts such a statement on the one account with those digits that has the
type its own summary names, and on none when it names no type, when no
account or several of that type carry the digits and the name does not tell
them apart, or when another statement of the document names the same type.

Visibility (SECURITY.md F3): a document uploaded by a member may only land
on accounts that member can **edit** (:func:`importable_account_ids`), so a
crafted file cannot write into an account hidden from its uploader. Owners
and admins, and intake without an uploading user (drop folder, email; their
attribution is Phase 3), may match any account of the household.
"""

from __future__ import annotations

import uuid
from collections.abc import Collection
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import AccountGrant, GrantLevel, HouseholdMember, HouseholdRole
from app.domain.accounts.models import LIABILITY_TYPES, Account, AccountType, Institution
from app.ingestion.match.institutions import pick

#: The account types a statement without digits may be put on by its
#: institution alone, by the ``liability`` side the caller names (module doc).
INSTITUTION_MATCH_TYPES: dict[bool | None, frozenset[AccountType]] = {
    True: frozenset({AccountType.CREDIT_CARD}),
    False: frozenset({AccountType.CHECKING, AccountType.SAVINGS}),
    None: frozenset({AccountType.CHECKING, AccountType.SAVINGS}),
}


@dataclass(frozen=True)
class AccountMatch:
    account_id: uuid.UUID | None
    candidates: tuple[uuid.UUID, ...] = ()
    #: What a match went by; ``None`` when there is no match.
    by: Literal["digits", "institution"] | None = None


def importable_account_ids(
    db: Session, household_id: uuid.UUID, user_id: uuid.UUID | None
) -> frozenset[uuid.UUID] | None:
    """Accounts a document of ``user_id`` may be imported into (``None``: all of them).

    Owner/admin uploaders and documents without an uploader: every account.
    Members: the accounts granted ``edit``. Viewers, and users who are no
    longer members: none.
    """
    if user_id is None:
        return None
    role = db.execute(
        select(HouseholdMember.role).where(
            HouseholdMember.household_id == household_id, HouseholdMember.user_id == user_id
        )
    ).scalar_one_or_none()
    if role in (HouseholdRole.OWNER, HouseholdRole.ADMIN):
        return None
    if role is not HouseholdRole.MEMBER:
        return frozenset()
    return frozenset(
        db.execute(
            select(AccountGrant.account_id).where(
                AccountGrant.household_id == household_id,
                AccountGrant.user_id == user_id,
                AccountGrant.level == GrantLevel.EDIT,
            )
        ).scalars()
    )


def match_account(
    db: Session,
    household_id: uuid.UUID,
    *,
    last4: str | None,
    institution: str | None = None,
    allowed: Collection[uuid.UUID] | None = None,
    institution_id: uuid.UUID | None = None,
    liability: bool | None = None,
) -> AccountMatch:
    """The account with ``last4`` (and ``institution`` to break ties), if exactly one.

    Without ``last4``: the one open account at ``institution_id``, on the
    ``liability`` side when that is given, if it is of a kind that holds
    such a statement (module doc).
    """
    if not last4:
        return _sole_account_of(db, household_id, institution_id, liability, allowed)
    query = (
        select(Account.id, Institution.name)
        .outerjoin(
            Institution,
            (Institution.household_id == Account.household_id)
            & (Institution.id == Account.institution_id),
        )
        .where(Account.household_id == household_id, Account.number_last4 == last4)
        .order_by(Account.id)
    )
    rows = [
        (account_id, name)
        for account_id, name in db.execute(query)
        if allowed is None or account_id in allowed
    ]
    if len(rows) == 1:
        return AccountMatch(rows[0][0], by="digits")
    candidates = tuple(account_id for account_id, _ in rows)
    if institution and rows:
        # Exact name first, else the one account whose institution means the
        # same (``Chase`` / ``JPMorgan Chase Bank, N.A.``; match.institutions).
        chosen = pick(institution, rows)
        if chosen is not None:
            return AccountMatch(chosen, candidates, by="digits")
    return AccountMatch(None, candidates)


def _sole_account_of(
    db: Session,
    household_id: uuid.UUID,
    institution_id: uuid.UUID | None,
    liability: bool | None,
    allowed: Collection[uuid.UUID] | None,
) -> AccountMatch:
    """The match of a statement without digits: the one open account of the
    household at ``institution_id`` (on the ``liability`` side, by type, when
    given) among ``allowed``; several are candidates only, and so is one of
    a type outside :data:`INSTITUTION_MATCH_TYPES` for that side."""
    if institution_id is None:
        return AccountMatch(None)
    query = (
        select(Account.id, Account.type)
        .where(
            Account.household_id == household_id,
            Account.institution_id == institution_id,
            Account.closed_on.is_(None),
        )
        .order_by(Account.id)
    )
    found = [
        (account_id, account_type)
        for account_id, account_type in db.execute(query)
        if (allowed is None or account_id in allowed)
        and (liability is None or (account_type in LIABILITY_TYPES) is liability)
    ]
    candidates = tuple(account_id for account_id, _ in found)
    if len(found) == 1 and found[0][1] in INSTITUTION_MATCH_TYPES[liability]:
        return AccountMatch(candidates[0], candidates, by="institution")
    return AccountMatch(None, candidates)


def _same_type(word: str, account_type: object) -> bool:
    """A statement's own word for its type is the account's (``creditcard``
    is ``credit_card``)."""
    return word.replace("_", "").lower() == str(account_type).replace("_", "").lower()


def _words(text: str | None) -> frozenset[str]:
    return frozenset("".join(c if c.isalnum() else " " for c in (text or "").lower()).split())


def shared_numbers(accounts: Collection[tuple[str | None, str | None]]) -> frozenset[str]:
    """The last four digits more than one of a document's statements carry;
    ``accounts`` is ``(last4, account type)`` per statement."""
    seen: dict[str, int] = {}
    for last4, _ in accounts:
        if last4:
            seen[last4] = seen.get(last4, 0) + 1
    return frozenset(last4 for last4, count in seen.items() if count > 1)


def match_shared_number(
    db: Session,
    household_id: uuid.UUID,
    *,
    last4: str,
    account_type: str | None,
    name: str | None = None,
    told_apart: bool = True,
    allowed: Collection[uuid.UUID] | None = None,
) -> AccountMatch:
    """The account of a statement of a document split at its accounts'
    summaries, whose ``last4`` other statements of it may carry too
    (module doc).

    Exactly one account with the digits and the type ``account_type``: that
    one. Several of the type: the one whose name holds every word of
    ``name`` or is held by it, if exactly one. ``told_apart`` false (another
    statement of the document names the same type) or no type named: none.
    The candidates are every account with the digits.
    """
    query = (
        select(Account.id, Account.type, Account.name)
        .where(Account.household_id == household_id, Account.number_last4 == last4)
        .order_by(Account.id)
    )
    rows = [row for row in db.execute(query) if allowed is None or row[0] in allowed]
    candidates = tuple(account_id for account_id, _, _ in rows)
    if not account_type or not told_apart:
        return AccountMatch(None, candidates)
    typed = [row for row in rows if _same_type(account_type, row[1])]
    if len(typed) > 1 and (wanted := _words(name)):
        typed = [row for row in typed if wanted <= _words(row[2]) or _words(row[2]) <= wanted]
    if len(typed) == 1:
        return AccountMatch(typed[0][0], candidates, by="digits")
    return AccountMatch(None, candidates)
