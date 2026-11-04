# SPDX-License-Identifier: AGPL-3.0-or-later
"""Categories, payees and tags (API.md ``/categories``, ``/payees``, ``/tags``).

These are household-wide: every role reads them. Members and above create
and change them (they categorise transactions); deleting and merging rewrite
references on every account of the household, including accounts the caller
cannot see, so they need owner or admin.

Rules:

* **Category tree**: at most :data:`MAX_CATEGORY_DEPTH` levels, no cycles
  (``422 category-cycle``, ``422 category-too-deep``). A child has its
  parent's ``kind`` (``422 category-kind-mismatch`` when a different one is
  given); moving a category under another parent gives its subtree that
  parent's kind, and changing a top-level category's kind changes its
  descendants. Sibling names are unique ignoring case (``409
  category-name-taken``). A category with children cannot be deleted (``409
  category-has-children``: move or merge them first); deleting one clears it
  from transactions, splits, schedules and payee defaults and removes its
  budget lines.
* **Category merge** (``source`` into ``into``): both of the same kind,
  ``into`` not ``source`` nor one of its descendants. Transactions, splits,
  schedules and payee defaults move to ``into``; budget lines move or are
  added to ``into``'s line in the same budget; ``source``'s children move
  under ``into``; then ``source`` is deleted.
* **Payees**: ``normalized_name`` is
  ``app.domain.transactions.import_hash.normalize_payee(name)``, unique per
  household (``409 payee-exists``). Aliases (statement spellings) are kept
  as entered, deduplicated by their normalised form, and no normalised alias
  may be another payee's name or alias (``409 payee-alias-taken``), so a
  statement payee maps to at most one payee. Merging moves transactions and
  schedules to ``into``, adds ``source``'s name and aliases to ``into``'s
  aliases and keeps ``into``'s default category (``source``'s when ``into``
  has none).
* **Tags**: names unique ignoring case (``409 tag-exists``); deleting a tag
  removes it from its transactions.

Every write is audited and emits ``category.changed``, ``payee.changed`` or
``tag.changed`` (payload: ids only).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.domain.taxonomy import repo
from app.domain.taxonomy.schemas import (
    CategoryCreate,
    CategoryOut,
    CategoryPage,
    CategoryUpdate,
    MergeInto,
    PayeeCreate,
    PayeeOut,
    PayeePage,
    PayeeUpdate,
    TagCreate,
    TagOut,
    TagPage,
    TagUpdate,
)
from app.domain.transactions.import_hash import normalize_payee
from app.domain.transactions.models import ALIASES_MAX, Category, CategoryKind, Payee, Tag

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: Levels of the category tree (a top-level category is level 1).
MAX_CATEGORY_DEPTH: Final = 3


# ---------------------------------------------------------------------------
# Problems and helpers
# ---------------------------------------------------------------------------


def category_not_found() -> ProblemError:
    return ProblemError(404, "category-not-found", "Category not found")


def payee_not_found() -> ProblemError:
    return ProblemError(404, "payee-not-found", "Payee not found")


def tag_not_found() -> ProblemError:
    return ProblemError(404, "tag-not-found", "Tag not found")


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


def _record(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    *,
    action: str,
    target_type: str,
    target_id: uuid.UUID,
    event: str,
    payload: dict[str, Any],
    before: Any = None,
    after: Any = None,
    request: HTTPConnection | None,
) -> None:
    audit(
        db,
        action=action,
        target_type=target_type,
        target_id=target_id,
        before=before,
        after=after,
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
    emit(db, household_id, event, payload)


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


def category_out(category: Category) -> CategoryOut:
    return CategoryOut(
        id=category.id,
        parent_id=category.parent_id,
        name=category.name,
        kind=category.kind,
        tax_treatment=category.tax_treatment,
        system_key=category.system_key,
        created_at=category.created_at,
        updated_at=category.updated_at,
    )


def _category_state(category: Category) -> dict[str, Any]:
    return {
        "name": category.name,
        "parent_id": str(category.parent_id) if category.parent_id else None,
        "kind": category.kind.value,
        "tax_treatment": category.tax_treatment.value if category.tax_treatment else None,
    }


def _kind_mismatch(parent_kind: CategoryKind) -> ProblemError:
    return ProblemError(
        422,
        "category-kind-mismatch",
        "Category kind mismatch",
        f"Categories under this parent are of kind {parent_kind.value}.",
        extra={"kind": parent_kind.value},
    )


def _name_taken() -> ProblemError:
    return ProblemError(
        409,
        "category-name-taken",
        "Category name taken",
        "A category with this name already exists at that place.",
    )


def _too_deep() -> ProblemError:
    return ProblemError(
        422,
        "category-too-deep",
        "Category tree too deep",
        f"Categories nest at most {MAX_CATEGORY_DEPTH} levels.",
        extra={"max_depth": MAX_CATEGORY_DEPTH},
    )


def _cycle() -> ProblemError:
    return ProblemError(
        422,
        "category-cycle",
        "Invalid category move",
        "A category cannot be placed under itself or one of its descendants.",
    )


def _depth(tree: dict[uuid.UUID, repo.CategoryNode], category_id: uuid.UUID) -> int:
    """Level of ``category_id`` (1 for a top-level category)."""
    level, current = 0, tree.get(category_id)
    seen: set[uuid.UUID] = set()
    while current is not None and current.id not in seen:
        seen.add(current.id)
        level += 1
        current = tree.get(current.parent_id) if current.parent_id else None
    return level


def _descendants(tree: dict[uuid.UUID, repo.CategoryNode], root: uuid.UUID) -> list[uuid.UUID]:
    children: dict[uuid.UUID, list[uuid.UUID]] = {}
    for node in tree.values():
        if node.parent_id is not None:
            children.setdefault(node.parent_id, []).append(node.id)
    out: list[uuid.UUID] = []
    stack = list(children.get(root, ()))
    while stack:
        node_id = stack.pop()
        if node_id in out:
            continue
        out.append(node_id)
        stack.extend(children.get(node_id, ()))
    return out


def _height(tree: dict[uuid.UUID, repo.CategoryNode], root: uuid.UUID) -> int:
    """Levels in the subtree of ``root`` (1 for a leaf)."""
    base = _depth(tree, root)
    deepest = max((_depth(tree, d) for d in _descendants(tree, root)), default=base)
    return deepest - base + 1


def _get_category(
    db: Session, household_id: uuid.UUID, category_id: uuid.UUID, *, lock: bool = False
) -> Category:
    category = repo.get_category(db, household_id, category_id, lock=lock)
    if category is None:
        raise category_not_found()
    return category


def _check_sibling_name(
    db: Session,
    household_id: uuid.UUID,
    parent_id: uuid.UUID | None,
    name: str,
    *,
    exclude: uuid.UUID | None = None,
) -> None:
    taken = repo.sibling_names(
        db, household_id, parent_id, exclude=() if exclude is None else (exclude,)
    )
    if name.lower() in taken:
        raise _name_taken()


def list_categories(
    db: Session,
    principal: Principal,
    *,
    cursor: uuid.UUID | None,
    limit: int,
    kind: CategoryKind | None = None,
) -> CategoryPage:
    household_id = _household(principal, HouseholdRole.VIEWER)
    rows = repo.list_categories(db, household_id, after=cursor, limit=limit + 1, kind=kind)
    page = rows[:limit]
    more = len(rows) > limit
    return CategoryPage(
        items=[category_out(c) for c in page], next_cursor=page[-1].id if more and page else None
    )


def get_category(db: Session, principal: Principal, category_id: uuid.UUID) -> CategoryOut:
    household_id = _household(principal, HouseholdRole.VIEWER)
    return category_out(_get_category(db, household_id, category_id))


def create_category(
    db: Session, principal: Principal, data: CategoryCreate, *, request: HTTPConnection | None
) -> CategoryOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    kind = data.kind or CategoryKind.EXPENSE
    if data.parent_id is not None:
        parent = _get_category(db, household_id, data.parent_id)
        if data.kind is not None and data.kind is not parent.kind:
            raise _kind_mismatch(parent.kind)
        kind = parent.kind
        if _depth(repo.category_tree(db, household_id), parent.id) + 1 > MAX_CATEGORY_DEPTH:
            raise _too_deep()
    _check_sibling_name(db, household_id, data.parent_id, data.name)
    category = Category(
        household_id=household_id,
        parent_id=data.parent_id,
        name=data.name,
        kind=kind,
        tax_treatment=data.tax_treatment,
    )
    db.add(category)
    db.flush()
    _record(
        db,
        principal,
        household_id,
        action="category.create",
        target_type="category",
        target_id=category.id,
        after=_category_state(category),
        event="category.changed",
        payload={"category_id": str(category.id)},
        request=request,
    )
    return category_out(category)


def update_category(
    db: Session,
    principal: Principal,
    category_id: uuid.UUID,
    data: CategoryUpdate,
    *,
    request: HTTPConnection | None,
) -> CategoryOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    category = _get_category(db, household_id, category_id, lock=True)
    before = _category_state(category)
    given = data.model_fields_set
    tree = repo.category_tree(db, household_id)
    parent_id = data.parent_id if "parent_id" in given else category.parent_id
    moved = parent_id != category.parent_id
    parent: Category | None = None
    if parent_id is not None:
        if parent_id == category.id or parent_id in _descendants(tree, category.id):
            raise _cycle()
        parent = _get_category(db, household_id, parent_id)
        if moved and _depth(tree, parent.id) + _height(tree, category.id) > MAX_CATEGORY_DEPTH:
            raise _too_deep()
    name = data.name if "name" in given and data.name is not None else category.name
    if moved or name.lower() != category.name.lower():
        _check_sibling_name(db, household_id, parent_id, name, exclude=category.id)
    if parent is not None:
        if "kind" in given and data.kind is not parent.kind:
            raise _kind_mismatch(parent.kind)
        kind = parent.kind
    else:
        kind = data.kind if "kind" in given and data.kind is not None else category.kind
    category.name = name
    category.parent_id = parent_id
    if "tax_treatment" in given:
        category.tax_treatment = data.tax_treatment
    if kind is not category.kind:
        category.kind = kind
        repo.set_kind(db, household_id, _descendants(tree, category.id), kind)
    after = _category_state(category)
    changed = {k: v for k, v in after.items() if before[k] != v}
    if changed:
        category.updated_at = clock.utcnow()
        db.flush()
        _record(
            db,
            principal,
            household_id,
            action="category.update",
            target_type="category",
            target_id=category.id,
            before={k: before[k] for k in changed},
            after=changed,
            event="category.changed",
            payload={"category_id": str(category.id)},
            request=request,
        )
    return category_out(category)


def delete_category(
    db: Session, principal: Principal, category_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    household_id = _household(principal, HouseholdRole.ADMIN)
    category = _get_category(db, household_id, category_id, lock=True)
    if repo.children_of(db, household_id, category.id):
        raise ProblemError(
            409,
            "category-has-children",
            "Category has children",
            "Move, merge or delete its subcategories first.",
        )
    state = _category_state(category)
    repo.delete_category(db, household_id, category.id)
    _record(
        db,
        principal,
        household_id,
        action="category.delete",
        target_type="category",
        target_id=category.id,
        before=state,
        event="category.changed",
        payload={"category_id": str(category.id)},
        request=request,
    )


def merge_category(
    db: Session,
    principal: Principal,
    category_id: uuid.UUID,
    data: MergeInto,
    *,
    request: HTTPConnection | None,
) -> CategoryOut:
    """Merge ``category_id`` into ``data.into`` (see the module doc)."""
    household_id = _household(principal, HouseholdRole.ADMIN)
    source = _get_category(db, household_id, category_id, lock=True)
    target = _get_category(db, household_id, data.into, lock=True)
    tree = repo.category_tree(db, household_id)
    if target.id == source.id or target.id in _descendants(tree, source.id):
        raise ProblemError(
            422,
            "category-merge-invalid",
            "Invalid merge",
            "A category cannot be merged into itself or one of its subcategories.",
        )
    if target.kind is not source.kind:
        raise _kind_mismatch(target.kind)
    children = repo.children_of(db, household_id, source.id)
    if children:
        taken = repo.sibling_names(db, household_id, target.id)
        if any(child.name.lower() in taken for child in children):
            raise _name_taken()
        target_depth = _depth(tree, target.id)
        if any(target_depth + _height(tree, child.id) > MAX_CATEGORY_DEPTH for child in children):
            raise _too_deep()
    state, source_id = _category_state(source), source.id
    counts = repo.reassign_category(db, household_id, source_id, target.id)
    counts["children"] = repo.reparent_children(db, household_id, source_id, target.id)
    db.flush()
    db.expunge(source)
    repo.delete_category(db, household_id, source_id)
    target.updated_at = clock.utcnow()
    db.flush()
    _record(
        db,
        principal,
        household_id,
        action="category.merge",
        target_type="category",
        target_id=source_id,
        before=state,
        after={"into": str(target.id), "moved": counts},
        event="category.changed",
        payload={"category_id": str(source_id), "into": str(target.id)},
        request=request,
    )
    return category_out(target)


# ---------------------------------------------------------------------------
# Payees
# ---------------------------------------------------------------------------


def payee_out(payee: Payee) -> PayeeOut:
    return PayeeOut(
        id=payee.id,
        name=payee.name,
        normalized_name=payee.normalized_name,
        aliases=list(payee.aliases or ()),
        default_category_id=payee.default_category_id,
        created_at=payee.created_at,
        updated_at=payee.updated_at,
    )


def _payee_state(payee: Payee) -> dict[str, Any]:
    return {
        "name": payee.name,
        "aliases": list(payee.aliases or ()),
        "default_category_id": (
            str(payee.default_category_id) if payee.default_category_id else None
        ),
    }


def _payee_key(name: str) -> str:
    key = normalize_payee(name)
    if not key:
        raise ProblemError(
            422,
            "invalid-payee-name",
            "Invalid payee name",
            "A payee name needs at least one letter or digit.",
        )
    return key


def _clean_aliases(name_key: str, aliases: list[str]) -> list[str]:
    """Aliases deduplicated by normalised form, without the payee's own name."""
    seen = {name_key}
    out: list[str] = []
    for alias in aliases:
        key = normalize_payee(alias)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(alias)
    if len(out) > ALIASES_MAX:
        raise ProblemError(
            422,
            "too-many-aliases",
            "Too many aliases",
            f"A payee has at most {ALIASES_MAX} aliases.",
            extra={"max_aliases": ALIASES_MAX},
        )
    return out


def _check_payee_keys(
    db: Session,
    household_id: uuid.UUID,
    name_key: str,
    aliases: list[str],
    *,
    exclude: set[uuid.UUID],
) -> None:
    alias_keys = {normalize_payee(a) for a in aliases}
    for other in repo.payee_keys(db, household_id):
        if other.id in exclude:
            continue
        other_keys = {normalize_payee(a) for a in other.aliases}
        if other.normalized_name == name_key:
            raise ProblemError(
                409,
                "payee-exists",
                "Payee exists",
                "Another payee already has this name.",
                extra={"payee_id": str(other.id)},
            )
        if name_key in other_keys or alias_keys & ({other.normalized_name} | other_keys):
            raise ProblemError(
                409,
                "payee-alias-taken",
                "Alias taken",
                "Another payee already uses this name or alias.",
                extra={"payee_id": str(other.id)},
            )


def _get_payee(
    db: Session, household_id: uuid.UUID, payee_id: uuid.UUID, *, lock: bool = False
) -> Payee:
    payee = repo.get_payee(db, household_id, payee_id, lock=lock)
    if payee is None:
        raise payee_not_found()
    return payee


def list_payees(
    db: Session,
    principal: Principal,
    *,
    cursor: uuid.UUID | None,
    limit: int,
    q: str | None = None,
) -> PayeePage:
    household_id = _household(principal, HouseholdRole.VIEWER)
    search = normalize_payee(q) if q else None
    rows = repo.list_payees(db, household_id, after=cursor, limit=limit + 1, search=search)
    page = rows[:limit]
    more = len(rows) > limit
    return PayeePage(
        items=[payee_out(p) for p in page], next_cursor=page[-1].id if more and page else None
    )


def get_payee(db: Session, principal: Principal, payee_id: uuid.UUID) -> PayeeOut:
    household_id = _household(principal, HouseholdRole.VIEWER)
    return payee_out(_get_payee(db, household_id, payee_id))


def create_payee(
    db: Session, principal: Principal, data: PayeeCreate, *, request: HTTPConnection | None
) -> PayeeOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    key = _payee_key(data.name)
    aliases = _clean_aliases(key, data.aliases)
    if data.default_category_id is not None:
        _get_category(db, household_id, data.default_category_id)
    _check_payee_keys(db, household_id, key, aliases, exclude=set())
    payee = Payee(
        household_id=household_id,
        name=data.name,
        normalized_name=key,
        aliases=aliases,
        default_category_id=data.default_category_id,
    )
    db.add(payee)
    db.flush()
    _record(
        db,
        principal,
        household_id,
        action="payee.create",
        target_type="payee",
        target_id=payee.id,
        after=_payee_state(payee),
        event="payee.changed",
        payload={"payee_id": str(payee.id)},
        request=request,
    )
    return payee_out(payee)


def update_payee(
    db: Session,
    principal: Principal,
    payee_id: uuid.UUID,
    data: PayeeUpdate,
    *,
    request: HTTPConnection | None,
) -> PayeeOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    payee = _get_payee(db, household_id, payee_id, lock=True)
    before = _payee_state(payee)
    given = data.model_fields_set
    name = data.name if "name" in given and data.name is not None else payee.name
    key = _payee_key(name)
    raw_aliases = (
        data.aliases if "aliases" in given and data.aliases is not None else list(payee.aliases)
    )
    aliases = _clean_aliases(key, raw_aliases)
    if "default_category_id" in given and data.default_category_id is not None:
        _get_category(db, household_id, data.default_category_id)
    _check_payee_keys(db, household_id, key, aliases, exclude={payee.id})
    payee.name = name
    payee.normalized_name = key
    payee.aliases = aliases
    if "default_category_id" in given:
        payee.default_category_id = data.default_category_id
    after = _payee_state(payee)
    changed = {k: v for k, v in after.items() if before[k] != v}
    if changed:
        payee.updated_at = clock.utcnow()
        db.flush()
        _record(
            db,
            principal,
            household_id,
            action="payee.update",
            target_type="payee",
            target_id=payee.id,
            before={k: before[k] for k in changed},
            after=changed,
            event="payee.changed",
            payload={"payee_id": str(payee.id)},
            request=request,
        )
    return payee_out(payee)


def delete_payee(
    db: Session, principal: Principal, payee_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    household_id = _household(principal, HouseholdRole.ADMIN)
    payee = _get_payee(db, household_id, payee_id, lock=True)
    state = _payee_state(payee)
    repo.delete_payee(db, household_id, payee.id)
    _record(
        db,
        principal,
        household_id,
        action="payee.delete",
        target_type="payee",
        target_id=payee.id,
        before=state,
        event="payee.changed",
        payload={"payee_id": str(payee.id)},
        request=request,
    )


def merge_payee(
    db: Session,
    principal: Principal,
    payee_id: uuid.UUID,
    data: MergeInto,
    *,
    request: HTTPConnection | None,
) -> PayeeOut:
    """Merge ``payee_id`` into ``data.into`` (see the module doc)."""
    household_id = _household(principal, HouseholdRole.ADMIN)
    source = _get_payee(db, household_id, payee_id, lock=True)
    target = _get_payee(db, household_id, data.into, lock=True)
    if source.id == target.id:
        raise ProblemError(
            422, "payee-merge-invalid", "Invalid merge", "A payee cannot be merged into itself."
        )
    state, source_id = _payee_state(source), source.id
    aliases = _clean_aliases(
        target.normalized_name, [*target.aliases, source.name, *source.aliases]
    )
    counts = repo.reassign_payee(db, household_id, source_id, target.id)
    if target.default_category_id is None:
        target.default_category_id = source.default_category_id
    db.expunge(source)
    repo.delete_payee(db, household_id, source_id)
    target.aliases = aliases
    target.updated_at = clock.utcnow()
    db.flush()
    _record(
        db,
        principal,
        household_id,
        action="payee.merge",
        target_type="payee",
        target_id=source_id,
        before=state,
        after={"into": str(target.id), "moved": counts},
        event="payee.changed",
        payload={"payee_id": str(source_id), "into": str(target.id)},
        request=request,
    )
    return payee_out(target)


# ---------------------------------------------------------------------------
# Tags
# ---------------------------------------------------------------------------


def tag_out(tag: Tag) -> TagOut:
    return TagOut(id=tag.id, name=tag.name, created_at=tag.created_at, updated_at=tag.updated_at)


def _tag_exists() -> ProblemError:
    return ProblemError(409, "tag-exists", "Tag exists", "A tag with this name already exists.")


def _get_tag(db: Session, household_id: uuid.UUID, tag_id: uuid.UUID, *, lock: bool = False) -> Tag:
    tag = repo.get_tag(db, household_id, tag_id, lock=lock)
    if tag is None:
        raise tag_not_found()
    return tag


def list_tags(
    db: Session, principal: Principal, *, cursor: uuid.UUID | None, limit: int
) -> TagPage:
    household_id = _household(principal, HouseholdRole.VIEWER)
    rows = repo.list_tags(db, household_id, after=cursor, limit=limit + 1)
    page = rows[:limit]
    more = len(rows) > limit
    return TagPage(
        items=[tag_out(t) for t in page], next_cursor=page[-1].id if more and page else None
    )


def get_tag(db: Session, principal: Principal, tag_id: uuid.UUID) -> TagOut:
    household_id = _household(principal, HouseholdRole.VIEWER)
    return tag_out(_get_tag(db, household_id, tag_id))


def create_tag(
    db: Session, principal: Principal, data: TagCreate, *, request: HTTPConnection | None
) -> TagOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    if repo.tag_name_taken(db, household_id, data.name):
        raise _tag_exists()
    tag = Tag(household_id=household_id, name=data.name)
    db.add(tag)
    db.flush()
    _record(
        db,
        principal,
        household_id,
        action="tag.create",
        target_type="tag",
        target_id=tag.id,
        after={"name": tag.name},
        event="tag.changed",
        payload={"tag_id": str(tag.id)},
        request=request,
    )
    return tag_out(tag)


def update_tag(
    db: Session,
    principal: Principal,
    tag_id: uuid.UUID,
    data: TagUpdate,
    *,
    request: HTTPConnection | None,
) -> TagOut:
    household_id = _household(principal, HouseholdRole.MEMBER)
    tag = _get_tag(db, household_id, tag_id, lock=True)
    if data.name == tag.name:
        return tag_out(tag)
    if repo.tag_name_taken(db, household_id, data.name, exclude=tag.id):
        raise _tag_exists()
    before = tag.name
    tag.name = data.name
    tag.updated_at = clock.utcnow()
    db.flush()
    _record(
        db,
        principal,
        household_id,
        action="tag.update",
        target_type="tag",
        target_id=tag.id,
        before={"name": before},
        after={"name": tag.name},
        event="tag.changed",
        payload={"tag_id": str(tag.id)},
        request=request,
    )
    return tag_out(tag)


def delete_tag(
    db: Session, principal: Principal, tag_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    household_id = _household(principal, HouseholdRole.ADMIN)
    tag = _get_tag(db, household_id, tag_id, lock=True)
    name = tag.name
    repo.delete_tag(db, household_id, tag.id)
    _record(
        db,
        principal,
        household_id,
        action="tag.delete",
        target_type="tag",
        target_id=tag.id,
        before={"name": name},
        event="tag.changed",
        payload={"tag_id": str(tag.id)},
        request=request,
    )
