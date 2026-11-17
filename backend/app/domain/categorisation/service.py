# SPDX-License-Identifier: AGPL-3.0-or-later
"""The refresh, and what ``/categorisation`` answers: the summary, what was
learned, forgetting, asking for a refresh.

**The refresh.** Each household records the versions of the clean-up, the
bundled dictionary and the default categories its rows were last evaluated
with, and whether its memory was seeded (:class:`State`, one household-scoped
``setting`` row, :data:`STATE_KEY`, validated again on every read). When that
is behind what this version ships (:func:`current_versions`),
:func:`refresh`:

1. adds the default categories introduced since
   (``categories.add_new_defaults``);
2. looks again at the rows nobody decided (``engine.reconsider``),
   recomputing every merchant key first when the clean-up or the dictionary
   moved; what the household taught about a merchant goes with its rows to
   the new key, so a correction is not lost to a new name, and so do the
   merchant conditions of its rules and the merchant of its recurring
   series (:func:`_follow_keys`);
3. once per household, counts the categories it set before provenance
   existed as decisions (``memory.seed_from_legacy``);
   and looks again when step 2 moved or step 3 added something learned;
4. stores the state and writes one ``categorise.refresh`` audit row.

It never runs the rules and never touches a row whose source is ``person``,
``file``, ``rule``, ``payee``, ``learned`` or ``legacy``, nor a split row;
every write is the engine's pinned write, so no ``updated_at`` moves. An
hourly job queues it for the households that are behind
(``app.workers.handlers.categorise``); an admin may ask for one that looks
again whatever the versions say (:func:`request_refresh`).

**Who sees what.** Reading needs any household role, forgetting a learned
merchant a member, asking for a refresh an admin. The summary counts the
transactions of the accounts the caller may see. The memory belongs to the
household and not to an account, so a caller who does not see every account
is shown (and counted) only what was learned about merchants that appear on
a row they may see: a merchant of somebody else's account is not revealed.
A learned entry of another household is ``404 memory-not-found``.

Audit rows carry ids, versions and counts only: never a descriptor, a
merchant key or a name. Nothing here leaves the host.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Final, cast

from pydantic import BaseModel, ConfigDict
from sqlalchemy import ColumnElement, case, exists, func, null, select, tuple_
from sqlalchemy.orm import Session

from app.auth.authz import forbidden, role_at_least
from app.auth.models import HouseholdRole
from app.auth.principal import Principal
from app.core import clock
from app.core.audit import audit
from app.core.errors import ProblemError
from app.core.events import emit
from app.db.models import Setting, SettingScope
from app.db.queue import enqueue
from app.domain.accounts.visibility import sees_all_accounts, visible_account_filter
from app.domain.categorisation import cleanup, dictionary, engine, memory
from app.domain.categorisation.engine import Summary
from app.domain.categorisation.memory import Direction, Learned
from app.domain.categorisation.models import MerchantMemory
from app.domain.categorisation.schemas import (
    CategorisationSummary,
    LearnedCategory,
    LearnedCategoryPage,
    SourceCount,
)
from app.domain.rules import engine as rules_engine
from app.domain.rules import repo as rules_repo
from app.domain.rules.schemas import JobAccepted
from app.domain.transactions import categories
from app.domain.transactions.models import (
    Rule,
    ScheduledTransaction,
    Transaction,
    TransactionSplit,
)
from app.domain.transactions.schemas import CategorySource

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

#: The household setting that holds :class:`State`.
STATE_KEY: Final = "categorisation.state"
#: The job that runs :func:`refresh` for one household.
REFRESH_JOB_TYPE: Final = "categorise.refresh"
#: Decisions, all for one category, from which a learned merchant is offered as a rule.
OFFER_RULE_MIN: Final = 3
#: The default categories every household had before a state was recorded.
FIRST_TAXONOMY: Final = 1

_VERSION_MAX: Final = 1_000_000


class RefreshJob(BaseModel):
    """Payload of :data:`REFRESH_JOB_TYPE`.

    ``force``: look again although the household's versions are current (a
    person asked, or a rule was reverted). ``requested_by``: the admin who
    asked, for the audit row; ``None`` for the hourly dispatcher.
    """

    model_config = ConfigDict(extra="forbid")

    force: bool = False
    requested_by: uuid.UUID | None = None


@dataclass(frozen=True)
class State:
    """What a household's rows were last evaluated with (module doc).

    The defaults are a household that never had a refresh: no clean-up and
    no dictionary yet, the first edition of the default categories.
    """

    cleaner: int = 0
    dictionary: int = 0
    taxonomy: int = FIRST_TAXONOMY
    memory_seeded: bool = False

    def versions(self) -> dict[str, int]:
        return {"cleaner": self.cleaner, "dictionary": self.dictionary, "taxonomy": self.taxonomy}


@dataclass(frozen=True)
class RefreshResult:
    """What one :func:`refresh` did (versions and counts only)."""

    #: False: the household was up to date and nothing was read or written.
    ran: bool
    before: State
    after: State
    summary: Summary = field(default_factory=Summary)
    #: Default categories added.
    categories_added: int = 0
    #: Memory rows seeded from ``legacy`` categories.
    seeded: int = 0


def memory_not_found() -> ProblemError:
    return ProblemError(404, "memory-not-found", "Not found", "No such learned category.")


def _household(principal: Principal, minimum: HouseholdRole) -> uuid.UUID:
    if principal.household_id is None or principal.role is None:
        raise forbidden("You are not a member of a household.")
    if not role_at_least(principal.role, minimum):
        raise forbidden()
    return principal.household_id


# ---------------------------------------------------------------------------
# The state and the refresh
# ---------------------------------------------------------------------------


def current_versions() -> dict[str, int]:
    """The clean-up, dictionary and default-category versions this build ships."""
    return {
        "cleaner": cleanup.CLEANER_VERSION,
        "dictionary": dictionary.version(),
        "taxonomy": categories.TAXONOMY_VERSION,
    }


def _version(value: Any) -> int | None:
    # ``bool`` is an ``int`` in Python and never a version.
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= _VERSION_MAX else None


def _parse_state(value: Any) -> State:
    """A stored state, checked again; anything this version would not have
    written counts as no state, so the household is simply evaluated again."""
    if not isinstance(value, dict):
        return State()
    cleaner, dictionary_version, taxonomy = (
        _version(value.get(name)) for name in ("cleaner", "dictionary", "taxonomy")
    )
    seeded = value.get("memory_seeded")
    if cleaner is None or dictionary_version is None or taxonomy is None:
        return State()
    if taxonomy < FIRST_TAXONOMY or not isinstance(seeded, bool):
        return State()
    return State(
        cleaner=cleaner, dictionary=dictionary_version, taxonomy=taxonomy, memory_seeded=seeded
    )


def _state_row(db: Session, household_id: uuid.UUID, *, lock: bool = False) -> Setting | None:
    statement = select(Setting).where(
        Setting.scope == SettingScope.HOUSEHOLD,
        Setting.scope_id == household_id,
        Setting.key == STATE_KEY,
    )
    if lock:
        statement = statement.with_for_update()
    return db.execute(statement).scalar_one_or_none()


def read_state(db: Session, household_id: uuid.UUID) -> State:
    """The household's state; :class:`State`'s defaults when it has none."""
    row = _state_row(db, household_id)
    return _parse_state(row.value if row is not None else None)


def _up_to_date(state: State) -> bool:
    return state.memory_seeded and state.versions() == current_versions()


def is_behind(db: Session, household_id: uuid.UUID) -> bool:
    """Whether a refresh has something to do for the household."""
    return not _up_to_date(read_state(db, household_id))


def _store_state(db: Session, household_id: uuid.UUID, row: Setting | None, state: State) -> None:
    value = asdict(state)
    now = clock.utcnow()
    if row is None:
        db.add(
            Setting(
                scope=SettingScope.HOUSEHOLD,
                scope_id=household_id,
                key=STATE_KEY,
                value=value,
                created_at=now,
                updated_at=now,
            )
        )
    else:
        row.value = value
        row.updated_at = now
    db.flush()


def _add(total: Summary, part: Summary) -> None:
    total.scanned += part.scanned
    total.by_source.update(part.by_source)
    total.suggested += part.suggested
    total.keyed += part.keyed
    total.changed += part.changed
    total.changed_accounts |= part.changed_accounts
    total.memory_moved += part.memory_moved
    total.renamed.update(part.renamed)
    total.renamed_at.update(part.renamed_at)


def _renamed_conditions(
    node: Any, renamed: Mapping[str, frozenset[str | None]]
) -> tuple[Any, bool]:
    """``node`` (a rule's condition tree) with every merchant leaf under its key of now."""
    if isinstance(node, list):
        parts = [_renamed_conditions(child, renamed) for child in node]
        return [part for part, _ in parts], any(moved for _, moved in parts)
    if not isinstance(node, dict):
        return node, False
    key = node.get("key")
    if node.get("type") == "merchant" and isinstance(key, str):
        new = engine.key_now(key, renamed)
        return ({**node, "key": new}, True) if new is not None else (node, False)
    out = dict(node)
    moved = False
    for name in ("conditions", "condition"):
        if name in node:
            out[name], changed = _renamed_conditions(node[name], renamed)
            moved = moved or changed
    return out, moved


def _keys_of_series(
    account_id: uuid.UUID, direction: Direction, key: str, summary: Summary
) -> frozenset[str]:
    """The keys a series' rows carried as ``key`` carry now; ``{key}`` when none did."""
    return engine.keys_at(account_id, direction, key, summary.renamed_at) or frozenset({key})


def _names_nobody_now(
    account_id: uuid.UUID, direction: Direction, key: str, summary: Summary
) -> bool:
    """Whether rows of ``account_id`` and ``direction`` carried ``key``, none naming anybody now."""
    now = summary.renamed_at.get((account_id, direction, key), frozenset())
    return bool(now) and all(new is None for new in now)


def _follow_keys(db: Session, household_id: uuid.UUID, summary: Summary) -> tuple[int, int]:
    """Carry merchant rules and recurring series over to their merchants' new keys.

    After a pass that computed every merchant key again: a rule's
    ``merchant`` condition names the merchant as the household's rows are
    keyed now (``Summary.renamed``, :func:`engine.key_now`), so "always
    categorise" keeps firing. A merchant rule is left as it is when the
    household already has one for the new key (the earlier of two would win).

    A series lives on one account and is read for one direction: it follows
    the rows of that account and direction that carried its key
    (``Summary.renamed_at``, :func:`engine.keys_at`), whatever a row of the
    same key elsewhere reads as now, so a person's decision about it stays
    where the next detection looks for it. When those rows went to one key,
    the series takes it; when they went to several, it keeps its key and
    ``split_keys`` holds the others, where detection finds a person's
    "dismissed" or "ended" too (``recurring.service``). When none of them
    names anybody now (bare cash wording, a line of one or two letters),
    the series has no key either: detection groups such rows by their text
    and finds a series without a key by its name or payee, so a person's
    "dismissed" still stands. Keys already in
    ``split_keys`` follow their rows the same way. Nothing else of the rule
    or the series is written. Returns ``(rules, series)`` that follow a new
    key (a series that loses its key counts too); a series that only gains
    ``split_keys`` is not counted.
    """
    renamed = summary.renamed
    rules = list(
        db.execute(
            select(Rule)
            .where(Rule.household_id == household_id)
            .order_by(Rule.id)
            .with_for_update()
        ).scalars()
    )
    held = {
        document[0]
        for rule in rules
        if (document := rules_engine.merchant_document(rule.conditions, rule.actions)) is not None
    }
    rules_moved = 0
    for rule in rules:
        conditions, moved = _renamed_conditions(rule.conditions, renamed)
        if not moved:
            continue
        before = rules_engine.merchant_document(rule.conditions, rule.actions)
        after = rules_engine.merchant_document(conditions, rule.actions)
        if before is not None and after is not None:
            if after[0] in held:
                continue
            held.add(after[0])
        rule.conditions = conditions
        rules_moved += 1
    series_moved = 0
    for series in db.execute(
        select(ScheduledTransaction)
        .where(
            ScheduledTransaction.household_id == household_id,
            ScheduledTransaction.merchant_key.is_not(None),
        )
        .order_by(ScheduledTransaction.id)
        .with_for_update()
    ).scalars():
        old = cast(str, series.merchant_key)
        direction: Direction = "in" if series.amount_min >= 0 else "out"
        keys = _keys_of_series(series.account_id, direction, old, summary)
        if _names_nobody_now(series.account_id, direction, old, summary):
            series.merchant_key = None
            series_moved += 1
        elif len(keys) == 1 and old not in keys:
            [series.merchant_key] = keys
            series_moved += 1
        held_now = {
            key
            for held_key in series.split_keys
            for key in _keys_of_series(series.account_id, direction, held_key, summary)
        }
        split = sorted((keys | held_now) - {series.merchant_key})
        if split != list(series.split_keys):
            series.split_keys = split
    db.flush()
    return rules_moved, series_moved


def refresh(
    db: Session,
    household_id: uuid.UUID,
    *,
    force: bool = False,
    check_cancelled: Callable[[], None] | None = None,
    user_id: uuid.UUID | None = None,
) -> RefreshResult:
    """Bring the household's undecided rows up to date (module doc).

    Returns at once, having written nothing, when the household's state is
    current and ``force`` is not set. In the caller's transaction and the
    household's RLS context. ``check_cancelled`` is called between batches
    and stops the pass by raising; nothing is stored then. ``user_id`` (who
    asked) goes into the audit row.
    """
    row = _state_row(db, household_id, lock=True)
    before = _parse_state(row.value if row is not None else None)
    if _up_to_date(before) and not force:
        return RefreshResult(ran=False, before=before, after=before)
    current = current_versions()
    after = State(
        cleaner=current["cleaner"],
        dictionary=current["dictionary"],
        taxonomy=current["taxonomy"],
        memory_seeded=True,
    )

    added = 0
    if before.taxonomy < after.taxonomy:
        added = categories.add_new_defaults(db, household_id, since=before.taxonomy)
    # A new clean-up or dictionary may name a merchant differently: every key
    # is computed again, also on rows somebody decided (their category stays).
    rekey = before.cleaner != after.cleaner or before.dictionary != after.dictionary
    total = Summary()
    _add(
        total,
        engine.reconsider(db, household_id, rekey=rekey, check_cancelled=check_cancelled),
    )
    rules_moved = series_moved = 0
    if rekey:
        rules_moved, series_moved = _follow_keys(db, household_id, total)
    seeded = 0
    if not before.memory_seeded:
        # After the keys: the seed counts ``legacy`` rows by their merchant key.
        seeded = memory.seed_from_legacy(db, household_id)
    if seeded or total.memory_moved:
        # The first look did not know yet what is now learned under these keys.
        _add(total, engine.reconsider(db, household_id, check_cancelled=check_cancelled))

    _store_state(db, household_id, row, after)
    audit(
        db,
        action="categorise.refresh",
        target_type="household",
        target_id=household_id,
        before=asdict(before),
        after={
            "versions": after.versions(),
            "forced": force,
            "scanned": total.scanned,
            "keyed": total.keyed,
            "changed": total.changed,
            "by_source": dict(sorted(total.by_source.items())),
            "suggested": total.suggested,
            "categories_added": added,
            "memory_seeded": seeded,
            "memory_moved": total.memory_moved,
            "rules_moved": rules_moved,
            "series_moved": series_moved,
        },
        household_id=household_id,
        user_id=user_id,
    )
    return RefreshResult(
        ran=True, before=before, after=after, summary=total, categories_added=added, seeded=seeded
    )


def request_refresh(
    db: Session, principal: Principal, *, request: HTTPConnection | None
) -> JobAccepted:
    """Queue a refresh of the household's undecided rows (admin); it always looks again.

    The job's own ``categorise.refresh`` audit row names who asked.
    """
    del request
    household_id = _household(principal, HouseholdRole.ADMIN)
    job_id = enqueue(
        db,
        REFRESH_JOB_TYPE,
        RefreshJob(force=True, requested_by=principal.user_id),
        household_id=household_id,
    )
    emit(db, household_id, "job.queued", {"id": str(job_id), "type": REFRESH_JOB_TYPE})
    return JobAccepted(job_id=job_id, state="queued")


# ---------------------------------------------------------------------------
# The summary
# ---------------------------------------------------------------------------


def _direction() -> ColumnElement[str]:
    """``memory.direction_of`` in SQL."""
    return case((Transaction.amount > 0, "in"), else_="out")


def _memory_scope(principal: Principal, household_id: uuid.UUID) -> list[ColumnElement[bool]]:
    """The memory rows the caller is shown (module doc, "Who sees what")."""
    clauses: list[ColumnElement[bool]] = [MerchantMemory.household_id == household_id]
    if not sees_all_accounts(principal):
        clauses.append(
            exists(
                select(Transaction.id).where(
                    Transaction.household_id == household_id,
                    Transaction.merchant_key == MerchantMemory.merchant_key,
                    visible_account_filter(principal, Transaction.account_id),
                )
            )
        )
    return clauses


def summary(db: Session, principal: Principal) -> CategorisationSummary:
    """Where categorisation stands, counted over the accounts the caller may see.

    ``by_source`` counts each visible transaction once, by how its category
    was decided. A row that carries only a suggestion has no category yet and
    is counted with those nobody decided (``null``); a row a person cleared
    is the person's.
    """
    household_id = _household(principal, HouseholdRole.VIEWER)
    no_category = Transaction.category_id.is_(None) & ~exists(
        select(TransactionSplit.id).where(
            TransactionSplit.household_id == Transaction.household_id,
            TransactionSplit.transaction_id == Transaction.id,
        )
    )
    suggested = Transaction.suggested_category_id.is_not(None)
    source = case((suggested, null()), else_=Transaction.category_source)
    counted = db.execute(
        select(
            source,
            func.count(),
            func.count().filter(no_category),
            func.count().filter(no_category & suggested),
        )
        .where(
            Transaction.household_id == household_id,
            visible_account_filter(principal, Transaction.account_id),
        )
        .group_by(source)
    ).all()
    by_source = [
        SourceCount(source=cast("CategorySource | None", found), transactions=int(rows))
        for found, rows, _, _ in sorted(counted, key=lambda item: (item[0] is None, item[0] or ""))
    ]
    entries = (
        select(MerchantMemory.merchant_key, MerchantMemory.direction)
        .where(*_memory_scope(principal, household_id))
        .distinct()
        .subquery()
    )
    learned = int(db.execute(select(func.count()).select_from(entries)).scalar_one())
    return CategorisationSummary(
        dictionary_version=dictionary.version(),
        dictionary_merchants=len(dictionary.merchants()),
        cleaner_version=cleanup.CLEANER_VERSION,
        up_to_date=_up_to_date(read_state(db, household_id)),
        by_source=by_source,
        uncategorised=sum(int(item[2]) for item in counted),
        suggestions=sum(int(item[3]) for item in counted),
        learned_merchants=learned,
    )


# ---------------------------------------------------------------------------
# What was learned
# ---------------------------------------------------------------------------


def _merchant_rules(db: Session, household_id: uuid.UUID) -> dict[str, uuid.UUID]:
    """Merchant key to its "always" rule: what ``rules.merchant.merchant_rule``
    answers for each key (the first enabled merchant rule in rule order), read once."""
    found: dict[str, uuid.UUID] = {}
    for rule in rules_repo.list_rules(db, household_id, enabled_only=True):
        document = rules_engine.merchant_document(rule.conditions, rule.actions)
        if document is not None:
            found.setdefault(document[0], rule.id)
    return found


def _examples(
    db: Session,
    principal: Principal,
    household_id: uuid.UUID,
    entries: Mapping[tuple[str, Direction], Learned],
) -> dict[tuple[str, str], uuid.UUID]:
    """For each entry, the newest row of that merchant and direction the caller may see."""
    keys = sorted({key for key, _ in entries})
    if not keys:
        return {}
    direction = _direction().label("direction")
    newest = (
        select(
            Transaction.merchant_key.label("merchant_key"),
            direction,
            Transaction.id.label("id"),
            func.row_number()
            .over(
                partition_by=(Transaction.merchant_key, direction),
                order_by=(Transaction.posted_date.desc(), Transaction.id.desc()),
            )
            .label("place"),
        )
        .where(
            Transaction.household_id == household_id,
            Transaction.merchant_key.in_(keys),
            visible_account_filter(principal, Transaction.account_id),
        )
        .subquery()
    )
    rows = db.execute(
        select(newest.c.merchant_key, newest.c.direction, newest.c.id).where(newest.c.place == 1)
    ).all()
    return {(str(key), str(way)): row_id for key, way, row_id in rows}


def list_learned(
    db: Session, principal: Principal, *, cursor: uuid.UUID | None, limit: int
) -> LearnedCategoryPage:
    """One page of what the household's corrections taught, in merchant-key order.

    One item per merchant and direction: the category with the most
    decisions. ``cursor`` is the ``next_cursor`` of the page before (the id
    of its last item); one that no longer names an entry of the household
    ends the listing instead of starting it over.
    """
    household_id = _household(principal, HouseholdRole.VIEWER)
    position = tuple_(MerchantMemory.merchant_key, MerchantMemory.direction)
    statement = (
        select(MerchantMemory.merchant_key, MerchantMemory.direction)
        .where(*_memory_scope(principal, household_id))
        .distinct()
        .order_by(MerchantMemory.merchant_key, MerchantMemory.direction)
        .limit(max(limit, 0) + 1)
    )
    if cursor is not None:
        last = db.execute(
            select(MerchantMemory.merchant_key, MerchantMemory.direction).where(
                MerchantMemory.household_id == household_id, MerchantMemory.id == cursor
            )
        ).first()
        if last is None:
            return LearnedCategoryPage(items=[], next_cursor=None)
        statement = statement.where(position > tuple_(last.merchant_key, last.direction))
    found = [(str(key), cast("Direction", way)) for key, way in db.execute(statement)]
    page = found[:limit]
    if not page:
        return LearnedCategoryPage(items=[], next_cursor=None)
    entries = memory.lookup(db, household_id, page)
    rules = _merchant_rules(db, household_id)
    examples = _examples(db, principal, household_id, entries)
    items: list[LearnedCategory] = []
    for position_key in page:
        entry = entries.get(position_key)
        if entry is None:
            continue
        key, direction = position_key
        rule_id = rules.get(key)
        items.append(
            LearnedCategory(
                id=entry.memory_id,
                merchant_key=key,
                merchant=engine.display_name(key) or key,
                direction=direction,
                category_id=entry.category_id,
                decisions=entry.decisions,
                total=entry.total,
                share=format(entry.share, ".2f"),
                last_decided_at=entry.last_decided_at,
                rule_id=rule_id,
                offer_rule=(
                    rule_id is None
                    and direction == "out"
                    and entry.decisions >= OFFER_RULE_MIN
                    and entry.decisions == entry.total
                ),
                example_transaction_id=examples.get((key, direction)),
            )
        )
    more = len(found) > limit
    return LearnedCategoryPage(items=items, next_cursor=items[-1].id if more and items else None)


def require_memory(db: Session, principal: Principal, memory_id: uuid.UUID) -> None:
    """``404 memory-not-found`` unless the entry is one of the caller's household."""
    household_id = _household(principal, HouseholdRole.MEMBER)
    found = db.execute(
        select(MerchantMemory.id).where(
            MerchantMemory.household_id == household_id, MerchantMemory.id == memory_id
        )
    ).first()
    if found is None:
        raise memory_not_found()


def forget(
    db: Session, principal: Principal, memory_id: uuid.UUID, *, request: HTTPConnection | None
) -> None:
    """Forget what was learned about one merchant (every category of the entry).

    Categories already written from it stay as they are; the next row of the
    merchant is guessed without it.
    """
    household_id = _household(principal, HouseholdRole.MEMBER)
    removed = memory.forget(db, household_id, memory_id)
    if not removed:
        raise memory_not_found()
    audit(
        db,
        action="memory.forget",
        target_type="merchant_memory",
        target_id=memory_id,
        after={"removed": removed},
        household_id=household_id,
        user_id=principal.user_id,
        request=request,
    )
