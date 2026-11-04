# SPDX-License-Identifier: AGPL-3.0-or-later
"""Which account a statement belongs to (INGESTION.md "Classify").

A statement names its account by the **last four digits** (OFX ``ACCTID``;
the full number never leaves the sandbox) and, in OFX, its institution
(``FI/ORG``). :func:`match_account` picks the household's account with those
last four digits; when several share them, the one whose institution has the
same name (compared like payees: case, accents and punctuation ignored).
Anything else (none, or still several) is not a match: the importer raises an
``unknown_account`` review item naming the candidates.

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

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.models import AccountGrant, GrantLevel, HouseholdMember, HouseholdRole
from app.domain.accounts.models import Account, Institution
from app.domain.transactions.import_hash import normalize_payee


@dataclass(frozen=True)
class AccountMatch:
    account_id: uuid.UUID | None
    candidates: tuple[uuid.UUID, ...] = ()


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
) -> AccountMatch:
    """The account with ``last4`` (and ``institution`` to break ties), if exactly one."""
    if not last4:
        return AccountMatch(None)
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
        return AccountMatch(rows[0][0])
    candidates = tuple(account_id for account_id, _ in rows)
    wanted = normalize_payee(institution)
    if wanted and rows:
        same = [account_id for account_id, name in rows if normalize_payee(name) == wanted]
        if len(same) == 1:
            return AccountMatch(same[0], candidates)
    return AccountMatch(None, candidates)
