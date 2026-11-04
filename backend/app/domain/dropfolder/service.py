# SPDX-License-Identifier: AGPL-3.0-or-later
"""Who a drop-folder file belongs to (INGESTION.md "Drop folder specifics").

**Household.** The drop folder belongs to one household:
``PERFIN_DROPFOLDER_HOUSEHOLD_ID``, else the instance's only household
(:func:`target_household`; with several and none configured the watcher does
nothing and says so in the log).

**Member and account.** ``inbox/<member>/<account>/file`` maps directly;
:func:`attribute` looks, in order, for

1. a ``dropfolder_route`` for ``<member>/<account>``, then for ``<member>``
   (``""`` for files directly in the inbox): routes are what an owner or
   admin *remembered* when answering "Who does this belong to?";
2. a ``<member>`` folder named like a member's username (compared as
   slugs: case, accents and punctuation ignored), and below it an
   ``<account>`` folder named like exactly one account that member may
   edit.

A route or name that no longer holds (the user left, lost ``edit`` on the
account, became a viewer) is ignored. A file with a member but no account
goes through the pipeline as that member's upload (account matching by last
four digits among the accounts they may edit, else ``unknown_account``); a
file without a member is held with an ``unknown_owner`` review item until
someone answers (:func:`resolve_unknown_owner`).

Folder names are compared and stored as slugs only; they are never used to
build a path (SECURITY.md C3).
"""

from __future__ import annotations

import re
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.requests import HTTPConnection

from app.auth.models import HouseholdMember, HouseholdRole, User
from app.auth.principal import Principal
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.logging import get_logger
from app.core.settings import Settings
from app.domain.accounts.models import Account
from app.domain.accounts.visibility import get_visible_account
from app.domain.documents.manage import queue_ingestion
from app.domain.documents.models import Document, ReviewItem
from app.domain.dropfolder.models import FOLDER_MAX, DropfolderRoute
from app.ingestion.match.accounts import importable_account_ids

log = get_logger(__name__)

#: Folder levels below the inbox that carry meaning (``<member>/<account>``).
ROUTE_DEPTH: Final = 2
_SLUG_SEP: Final = re.compile(r"[^0-9a-z]+")

How = Literal["route", "name", "none"]


def slugify(name: str) -> str:
    """``"Alex's  Checking"`` -> ``"alex-s-checking"`` (accents dropped, at most 64 chars)."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return _SLUG_SEP.sub("-", folded.casefold()).strip("-")[:64]


def folder_slug(parts: Sequence[str]) -> str:
    """The route key of a folder: its first two levels as slugs joined by ``/``."""
    return "/".join(slugify(p) or "-" for p in parts[:ROUTE_DEPTH])[:FOLDER_MAX]


@dataclass(frozen=True)
class Attribution:
    """Who a file belongs to; ``user_id`` ``None`` means "ask" (``unknown_owner``)."""

    folder: str
    user_id: uuid.UUID | None
    account_id: uuid.UUID | None
    how: How


# ---------------------------------------------------------------------------
# Household
# ---------------------------------------------------------------------------


def household_ids(db: Session) -> list[uuid.UUID]:
    """Every household id (``perfin_household_ids()``; ``household`` is under RLS)."""
    rows = db.execute(text("SELECT * FROM perfin_household_ids()")).scalars()
    return [r if isinstance(r, uuid.UUID) else uuid.UUID(str(r)) for r in rows]


def target_household(db: Session, settings: Settings) -> uuid.UUID | None:
    """The household drop-folder files belong to (module doc), or ``None``."""
    ids = household_ids(db)
    if settings.dropfolder_household_id is not None:
        if settings.dropfolder_household_id in ids:
            return settings.dropfolder_household_id
        log.warning("PERFIN_DROPFOLDER_HOUSEHOLD_ID names no existing household")
        return None
    if len(ids) > 1:
        log.warning(
            "drop folder idle: several households exist; set PERFIN_DROPFOLDER_HOUSEHOLD_ID"
        )
        return None
    return ids[0] if ids else None


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------


def _member_role(db: Session, household_id: uuid.UUID, user_id: uuid.UUID) -> HouseholdRole | None:
    return db.execute(
        select(HouseholdMember.role).where(
            HouseholdMember.household_id == household_id, HouseholdMember.user_id == user_id
        )
    ).scalar_one_or_none()


def _may_own(db: Session, household_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    """Members, admins and owners may own drop-folder files; viewers may not."""
    role = _member_role(db, household_id, user_id)
    return role in (HouseholdRole.MEMBER, HouseholdRole.ADMIN, HouseholdRole.OWNER)


def _may_import_into(
    db: Session, household_id: uuid.UUID, user_id: uuid.UUID, account_id: uuid.UUID
) -> bool:
    exists = db.execute(
        select(Account.id).where(Account.household_id == household_id, Account.id == account_id)
    ).scalar_one_or_none()
    if exists is None:
        return False
    allowed = importable_account_ids(db, household_id, user_id)
    return allowed is None or account_id in allowed


def _route(db: Session, household_id: uuid.UUID, folder: str) -> DropfolderRoute | None:
    return db.execute(
        select(DropfolderRoute).where(
            DropfolderRoute.household_id == household_id, DropfolderRoute.folder == folder
        )
    ).scalar_one_or_none()


def _member_by_name(db: Session, household_id: uuid.UUID, name: str) -> uuid.UUID | None:
    slug = slugify(name)
    if not slug:
        return None
    rows = db.execute(
        select(User.id, User.username)
        .join(HouseholdMember, HouseholdMember.user_id == User.id)
        .where(HouseholdMember.household_id == household_id)
    ).all()
    matches = [row.id for row in rows if slugify(row.username) == slug]
    return matches[0] if len(matches) == 1 else None


def _account_by_name(
    db: Session, household_id: uuid.UUID, user_id: uuid.UUID, name: str
) -> uuid.UUID | None:
    slug = slugify(name)
    if not slug:
        return None
    allowed = importable_account_ids(db, household_id, user_id)
    rows = db.execute(
        select(Account.id, Account.name).where(Account.household_id == household_id)
    ).all()
    matches = [
        row.id
        for row in rows
        if slugify(row.name) == slug and (allowed is None or row.id in allowed)
    ]
    return matches[0] if len(matches) == 1 else None


def _valid_route(
    db: Session, household_id: uuid.UUID, route: DropfolderRoute
) -> tuple[uuid.UUID, uuid.UUID | None] | None:
    if not _may_own(db, household_id, route.user_id):
        return None
    account_id = route.account_id
    if account_id is not None and not _may_import_into(db, household_id, route.user_id, account_id):
        account_id = None
    return route.user_id, account_id


def attribute(db: Session, household_id: uuid.UUID, parts: Sequence[str]) -> Attribution:
    """Member and account for a file in the folder ``parts`` below the inbox (module doc)."""
    folder = folder_slug(parts)
    # Deepest first; the inbox's own route ("") is for files directly in it.
    stop = 0 if parts else -1
    levels = [folder_slug(parts[:n]) for n in range(min(len(parts), ROUTE_DEPTH), stop, -1)]
    for level in levels:
        route = _route(db, household_id, level)
        if route is None:
            continue
        valid = _valid_route(db, household_id, route)
        if valid is None:
            continue
        user_id, account_id = valid
        if account_id is None and level != folder and len(parts) >= ROUTE_DEPTH:
            account_id = _account_by_name(db, household_id, user_id, parts[1])
        return Attribution(folder, user_id, account_id, "route")
    if parts:
        named = _member_by_name(db, household_id, parts[0])
        if named is not None and _may_own(db, household_id, named):
            named_account = (
                _account_by_name(db, household_id, named, parts[1])
                if len(parts) >= ROUTE_DEPTH
                else None
            )
            return Attribution(folder, named, named_account, "name")
    return Attribution(folder, None, None, "none")


# ---------------------------------------------------------------------------
# "Who does this belong to?"
# ---------------------------------------------------------------------------


def remember_route(
    db: Session,
    household_id: uuid.UUID,
    folder: str,
    user_id: uuid.UUID,
    account_id: uuid.UUID | None,
) -> DropfolderRoute:
    """Create or replace the route of ``folder`` (a slug from :func:`folder_slug`)."""
    route = _route(db, household_id, folder)
    if route is None:
        route = DropfolderRoute(household_id=household_id, folder=folder, user_id=user_id)
        db.add(route)
    route.user_id = user_id
    route.account_id = account_id
    db.flush()
    return route


def _invalid(detail: str) -> ProblemError:
    return ProblemError(422, "invalid-resolution", "Invalid resolution", detail)


def resolve_unknown_owner(
    db: Session,
    principal: Principal,
    item: ReviewItem,
    document: Document,
    *,
    user_id: uuid.UUID,
    account_id: uuid.UUID | None,
    remember: bool,
    request: HTTPConnection | None,
) -> dict[str, Any]:
    """Assign a held drop-folder document to a member (and account) and ingest it.

    ``user_id`` must be a member, admin or owner of the household; an
    ``account_id`` must be one the caller may edit and the chosen member may
    import into. With ``remember`` the item's folder gets a route so the
    next file there is attributed without asking.
    """
    household_id = item.household_id
    if not _may_own(db, household_id, user_id):
        raise _invalid("Choose a member, admin or owner of this household.")
    if account_id is not None:
        get_visible_account(db, principal, account_id, need="edit")
        if not _may_import_into(db, household_id, user_id, account_id):
            raise _invalid("That member may not import into this account.")
    payload = item.payload or {}
    folder = payload.get("folder")
    remembered = False
    if remember:
        if not isinstance(folder, str) or len(folder) > FOLDER_MAX:
            raise _invalid("This item names no drop-folder subfolder to remember.")
        remember_route(db, household_id, folder, user_id, account_id)
        remembered = True
        audit(
            db,
            action="dropfolder.route_remember",
            target_type="dropfolder_route",
            target_id=None,
            after={"folder": folder, "user_id": user_id, "account_id": account_id},
            household_id=household_id,
            user_id=principal.user_id,
            request=request,
        )
    document.uploaded_by = user_id
    document.account_id = account_id
    db.flush()
    job_id = queue_ingestion(
        db,
        document,
        principal=principal,
        action="document.assign_owner",
        request=request,
        facts={"uploaded_by": str(user_id), "account_id": account_id},
    )
    return {
        "user_id": str(user_id),
        "account_id": str(account_id) if account_id is not None else None,
        "remembered": remembered,
        "rerun": "pipeline",
        "job_id": str(job_id),
    }
