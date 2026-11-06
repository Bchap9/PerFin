# SPDX-License-Identifier: AGPL-3.0-or-later
"""Institution fingerprints: deterministic layout rules shipped as data (INGESTION.md "Classify").

A fingerprint names an institution, a document kind, the parser that reads
the layout and a version, and says how to recognise the layout. Shipped
fingerprints are YAML files in ``fingerprints/`` (one per layout); a user's
"remember this layout" becomes a ``parser_fingerprint`` row whose
``match_rules`` hold the same :class:`MatchSpec` as JSON. Both pass the same
validation before they are used:

* YAML is read with ``yaml.safe_load`` after refusing aliases (an alias
  bomb expands exponentially when the result is walked) and files over
  :data:`MAX_YAML_BYTES`; the result must satisfy :class:`FingerprintSpec`
  (strict pydantic, unknown keys refused, bounded lists and strings).
* Patterns compile through :func:`app.ingestion.classify.safe_regex.compile_safe`
  (ReDoS-safe subset, possessive quantifiers) and only see single capped
  lines; account masks through :func:`app.ingestion.classify.accounts.mask_regex`.

Matching (:func:`match_fingerprint`): every rule in ``match.all`` must hold
(a fingerprint has at least one), at least one of ``match.one_of`` when it
lists any (the institution's name on page 1 *or* in the PDF's metadata),
and at least ``min_any`` of ``match.any``.
The confidence is ``0.85`` plus up to ``0.14`` for the share of ``any`` rules
that hold, so a fingerprint with more corroborating evidence wins a tie.

Work is bounded in total, not only per pattern (security review
P34-SR-R2-01). A safe pattern still costs up to (pattern x line^2) per line,
so 64 rules over 4000 lines of 300 characters took a minute. Hence:

* the pattern rules (``regex``, ``account_mask``) of **household**
  fingerprints run under a :class:`MatchBudget`, a wall-clock deadline
  checked before every line searched (one search is at most a few
  milliseconds, so the deadline is overrun by at most that).
  :func:`best_match` shares one budget of :data:`HOUSEHOLD_MATCH_SECONDS`
  between all household fingerprints of a document; a household
  fingerprint evaluated without a budget gets its own. A fingerprint whose
  evaluation runs out of budget **does not match** (fail closed: the
  document goes on to the classifier and review). Shipped fingerprints are
  reviewed code and are not budgeted;
* a household has at most :data:`MAX_HOUSEHOLD_FINGERPRINTS` layouts (one
  per institution, kind and parser); only the latest version of each is
  used, and :func:`remember_layout` refuses a new layout beyond the cap
  (:class:`FingerprintLimitError`).

Rules (exactly one key each):

``header``   case-insensitive substring of the header (first page's top lines)
``text``     case-insensitive substring of any line
``regex``    safe pattern searched in each line (``region: header|text``)
``account_mask``  a mask such as ``XXXX-XXXX-XXXX-####`` found in a line
``columns``  a table whose header row starts with these cells (normalised)
``metadata`` case-insensitive substring of the document's title or author
             (PDF metadata; an institution whose name is only in its logo)
"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from app.domain.accounts.models import Institution
from app.domain.documents.models import DocumentKind, ParserFingerprint
from app.ingestion.classify.accounts import MaskError, mask_regex
from app.ingestion.classify.dates import DateOrder
from app.ingestion.classify.safe_regex import UnsafePatternError, compile_safe, search_line
from app.ingestion.classify.text import DocumentView, normalise, normalise_cell

SCHEMA_VERSION: Final = 1
MAX_YAML_BYTES: Final = 64 * 1024
MAX_RULES: Final = 32
MAX_STRING: Final = 200
BASE_CONFIDENCE: Final = 0.85
ANY_BONUS: Final = 0.14
MAX_VERSION: Final = 10_000
#: Layouts (institution, kind, parser) a household may remember; only the
#: latest version of each is matched (security review P34-SR-R2-01).
MAX_HOUSEHOLD_FINGERPRINTS: Final = 100
#: Wall-clock budget for the pattern rules of all household fingerprints on
#: one document (P34-SR-R2-01).
HOUSEHOLD_MATCH_SECONDS: Final = 0.25

_LOCK_LAYOUTS: Final = text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 7307))")

Key = Annotated[
    str, Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9-]{1,62}[a-z0-9]$")
]
ParserName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z][a-z0-9_]{0,63}$")]
Needle = Annotated[str, Field(min_length=2, max_length=MAX_STRING)]
Cell = Annotated[str, Field(min_length=1, max_length=64)]
FingerprintKind = Literal[
    "statement", "receipt", "invoice", "tax_form", "payslip", "insurance", "other"
]


class FingerprintError(ValueError):
    """A fingerprint that does not satisfy the schema (shipped file or stored row)."""


class FingerprintLimitError(FingerprintError):
    """The household already has :data:`MAX_HOUSEHOLD_FINGERPRINTS` layouts (or versions)."""


class MatchBudget:
    """A wall-clock deadline shared by pattern-rule evaluations (P34-SR-R2-01).

    :meth:`spent` is checked before each line a pattern rule searches; once
    the deadline has passed it stays spent. :meth:`child` gives a budget of
    at most ``seconds`` that also ends with this one.
    """

    __slots__ = ("_clock", "_deadline", "exhausted")

    def __init__(
        self,
        seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        deadline: float | None = None,
    ) -> None:
        self._clock = clock
        own = clock() + seconds
        self._deadline = own if deadline is None else min(own, deadline)
        self.exhausted = False

    def spent(self) -> bool:
        if not self.exhausted and self._clock() >= self._deadline:
            self.exhausted = True
        return self.exhausted

    def child(self, seconds: float) -> MatchBudget:
        return MatchBudget(seconds, clock=self._clock, deadline=self._deadline)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Rule(_Strict):
    header: Needle | None = None
    text: Needle | None = None
    regex: Annotated[str, Field(min_length=1, max_length=256)] | None = None
    ignore_case: bool = True
    region: Literal["header", "text"] = "text"
    account_mask: Annotated[str, Field(min_length=4, max_length=40)] | None = None
    columns: list[Cell] | None = Field(default=None, min_length=2, max_length=20)
    metadata: Needle | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Rule:
        set_keys = [
            k
            for k in ("header", "text", "regex", "account_mask", "columns", "metadata")
            if getattr(self, k) is not None
        ]
        if len(set_keys) != 1:
            raise ValueError(
                "a rule has exactly one of header, text, regex, account_mask, columns, metadata"
            )
        if self.regex is not None:
            try:
                compile_safe(self.regex, ignore_case=self.ignore_case)
            except UnsafePatternError as exc:
                raise ValueError(f"unsafe regex: {exc}") from exc
        if self.account_mask is not None:
            try:
                mask_regex(self.account_mask)
            except MaskError as exc:
                raise ValueError(str(exc)) from exc
        return self


class MatchSpec(_Strict):
    all: list[Rule] = Field(min_length=1, max_length=MAX_RULES)
    #: At least one of these must hold (when any are listed).
    one_of: list[Rule] = Field(default_factory=list, max_length=MAX_RULES)
    any: list[Rule] = Field(default_factory=list, max_length=MAX_RULES)
    min_any: int = Field(default=0, ge=0, le=MAX_RULES)

    @model_validator(mode="after")
    def _min_any(self) -> MatchSpec:
        if self.min_any > len(self.any):
            raise ValueError("min_any is larger than the number of any rules")
        return self


class FingerprintSpec(_Strict):
    schema_version: Literal[1] = Field(alias="schema")
    key: Key
    institution: Annotated[str, Field(min_length=2, max_length=MAX_STRING)]
    country: Annotated[str, Field(pattern=r"^[A-Z]{2}$")] | None = None
    kind: FingerprintKind
    parser: ParserName
    version: int = Field(ge=1, le=10_000)
    date_order: DateOrder | None = None
    match: MatchSpec

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, populate_by_name=True)

    @field_validator("institution")
    @classmethod
    def _printable(cls, value: str) -> str:
        if not value.isprintable() or value != value.strip():
            raise ValueError("institution must be printable and trimmed")
        return value


@dataclass(frozen=True)
class _CompiledRule:
    rule: Rule
    pattern: re.Pattern[str] | None = None
    needle: str = ""
    columns: tuple[str, ...] = ()

    def holds(self, view: DocumentView, budget: MatchBudget | None = None) -> bool:
        rule = self.rule
        if rule.header is not None:
            return any(self.needle in normalise(line) for line in view.header)
        if rule.text is not None:
            return any(self.needle in line for line in view.normalised)
        if rule.metadata is not None:
            return any(self.needle in value for value in view.metadata)
        if self.pattern is not None:
            lines = view.header if rule.region == "header" else view.lines
            if budget is None:
                return any(search_line(self.pattern, line) for line in lines)
            for line in lines:
                if budget.spent():
                    return False
                if search_line(self.pattern, line):
                    return True
            return False
        if self.columns:
            width = len(self.columns)
            return any(tuple(h[:width]) == self.columns for h in view.table_headers)
        return False


@dataclass(frozen=True)
class Fingerprint:
    """A validated, compiled fingerprint (shipped or the household's own)."""

    spec: FingerprintSpec
    all_rules: tuple[_CompiledRule, ...]
    any_rules: tuple[_CompiledRule, ...]
    masks: tuple[re.Pattern[str], ...]
    origin: Literal["builtin", "household"] = "builtin"
    fingerprint_id: uuid.UUID | None = None
    institution_id: uuid.UUID | None = None
    one_of_rules: tuple[_CompiledRule, ...] = ()

    @property
    def key(self) -> str:
        return self.spec.key


@dataclass(frozen=True)
class FingerprintMatch:
    fingerprint: Fingerprint
    confidence: float
    any_held: int


def _compile_rule(rule: Rule) -> _CompiledRule:
    if rule.regex is not None:
        return _CompiledRule(rule, pattern=compile_safe(rule.regex, ignore_case=rule.ignore_case))
    if rule.account_mask is not None:
        return _CompiledRule(rule, pattern=mask_regex(rule.account_mask))
    if rule.columns is not None:
        return _CompiledRule(rule, columns=tuple(normalise_cell(c) for c in rule.columns))
    return _CompiledRule(rule, needle=normalise(rule.header or rule.text or rule.metadata or ""))


def compile_fingerprint(
    spec: FingerprintSpec,
    *,
    origin: Literal["builtin", "household"] = "builtin",
    fingerprint_id: uuid.UUID | None = None,
    institution_id: uuid.UUID | None = None,
) -> Fingerprint:
    rules = [*spec.match.all, *spec.match.one_of, *spec.match.any]
    masks = tuple(mask_regex(r.account_mask) for r in rules if r.account_mask is not None)
    return Fingerprint(
        spec=spec,
        all_rules=tuple(_compile_rule(r) for r in spec.match.all),
        any_rules=tuple(_compile_rule(r) for r in spec.match.any),
        one_of_rules=tuple(_compile_rule(r) for r in spec.match.one_of),
        masks=masks,
        origin=origin,
        fingerprint_id=fingerprint_id,
        institution_id=institution_id,
    )


def _refuse_aliases(text: str) -> None:
    for event in yaml.parse(text, Loader=yaml.SafeLoader):
        if isinstance(event, yaml.AliasEvent):
            raise FingerprintError("YAML aliases are not allowed in fingerprints")
        if isinstance(event, yaml.NodeEvent) and event.anchor is not None:
            raise FingerprintError("YAML anchors are not allowed in fingerprints")


def parse_fingerprint_yaml(text: str | bytes) -> FingerprintSpec:
    """Validate one fingerprint document (raises :class:`FingerprintError`)."""
    if isinstance(text, bytes):
        if len(text) > MAX_YAML_BYTES:
            raise FingerprintError("fingerprint file too large")
        try:
            text = text.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise FingerprintError("fingerprint file is not UTF-8") from exc
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise FingerprintError("fingerprint file too large")
    try:
        _refuse_aliases(text)
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise FingerprintError(f"invalid YAML: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise FingerprintError("a fingerprint is a mapping")
    try:
        return FingerprintSpec.model_validate(data)
    except ValidationError as exc:
        raise FingerprintError(_summary(exc)) from exc


def _summary(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:5]:
        where = ".".join(str(p) for p in err["loc"])
        parts.append(f"{where}: {err['msg']}")
    return "; ".join(parts)


def parse_match_rules(data: Any) -> MatchSpec:
    """Validate stored ``parser_fingerprint.match_rules`` (raises :class:`FingerprintError`)."""
    try:
        return MatchSpec.model_validate(data)
    except ValidationError as exc:
        raise FingerprintError(_summary(exc)) from exc


@cache
def builtin_fingerprints() -> tuple[Fingerprint, ...]:
    """The shipped catalogue (``fingerprints/*.yaml``), validated once per process."""
    folder = resources.files("app.ingestion.classify").joinpath("fingerprints")
    out: list[Fingerprint] = []
    seen: set[str] = set()
    for entry in sorted(folder.iterdir(), key=lambda e: e.name):
        if not entry.name.endswith(".yaml"):
            continue
        try:
            spec = parse_fingerprint_yaml(entry.read_bytes())
        except FingerprintError as exc:
            raise FingerprintError(f"{entry.name}: {exc}") from exc
        if spec.key in seen:
            raise FingerprintError(f"{entry.name}: duplicate key {spec.key}")
        seen.add(spec.key)
        out.append(compile_fingerprint(spec))
    return tuple(out)


def household_fingerprints(db: Session, household_id: uuid.UUID) -> list[Fingerprint]:
    """The household's own fingerprints; a stored row that fails validation is skipped.

    Only the latest version of each layout (institution, kind, parser) is
    used, and at most :data:`MAX_HOUSEHOLD_FINGERPRINTS` layouts (the most
    recently learned), so the work of matching a document is bounded
    (P34-SR-R2-01).
    """
    latest = (
        select(ParserFingerprint.id)
        .where(ParserFingerprint.household_id == household_id)
        .ext(
            distinct_on(
                ParserFingerprint.institution_id,
                ParserFingerprint.kind,
                ParserFingerprint.parser_name,
            )
        )
        .order_by(
            ParserFingerprint.institution_id,
            ParserFingerprint.kind,
            ParserFingerprint.parser_name,
            ParserFingerprint.version.desc(),
            ParserFingerprint.created_at.desc(),
            ParserFingerprint.id.desc(),
        )
        .subquery()
    )
    newest = (
        select(ParserFingerprint.id)
        .where(ParserFingerprint.id.in_(select(latest.c.id)))
        .order_by(ParserFingerprint.created_at.desc(), ParserFingerprint.id.desc())
        .limit(MAX_HOUSEHOLD_FINGERPRINTS)
        .subquery()
    )
    rows = db.execute(
        select(ParserFingerprint, Institution.name, Institution.country)
        .join(
            Institution,
            (Institution.household_id == ParserFingerprint.household_id)
            & (Institution.id == ParserFingerprint.institution_id),
        )
        .where(
            ParserFingerprint.household_id == household_id,
            ParserFingerprint.id.in_(select(newest.c.id)),
        )
        .order_by(ParserFingerprint.created_at, ParserFingerprint.id)
    ).all()
    out: list[Fingerprint] = []
    for row, name, country in rows:
        if row.kind is DocumentKind.UNKNOWN:
            continue
        try:
            match = parse_match_rules(row.match_rules)
            spec = FingerprintSpec.model_validate(
                {
                    "schema": SCHEMA_VERSION,
                    "key": f"household-{row.id.hex[:16]}",
                    "institution": name,
                    "country": country,
                    "kind": row.kind.value,
                    "parser": row.parser_name,
                    "version": row.version,
                    "match": match,
                }
            )
        except (FingerprintError, ValidationError):
            continue
        out.append(
            compile_fingerprint(
                spec, origin="household", fingerprint_id=row.id, institution_id=row.institution_id
            )
        )
    return out


def remember_layout(
    db: Session,
    household_id: uuid.UUID,
    *,
    institution_id: uuid.UUID,
    kind: DocumentKind,
    match_rules: Any,
    parser_name: str,
    document_id: uuid.UUID | None = None,
    template: dict[str, Any] | None = None,
) -> ParserFingerprint:
    """Store a user's fingerprint ("remember this layout"); the rules are validated first.

    The caller (the review API) checks that the institution and document are
    the household's and that the user may edit them; the database refuses
    cross-household references anyway (ADR-0008). A new layout beyond
    :data:`MAX_HOUSEHOLD_FINGERPRINTS`, or a version beyond
    :data:`MAX_VERSION`, raises :class:`FingerprintLimitError`; the check
    holds a per-household transaction lock so concurrent calls cannot pass it
    together. ``template`` is a validated learned layout
    (``app.ingestion.parsers.template.LayoutTemplate``, "Teach PerFin").
    """
    if kind is DocumentKind.UNKNOWN:
        raise FingerprintError("a fingerprint names a known kind")
    spec = parse_match_rules(match_rules)
    try:
        FingerprintSpec.model_validate(
            {
                "schema": SCHEMA_VERSION,
                "key": "household-check",
                "institution": "check",
                "kind": kind.value,
                "parser": parser_name,
                "version": 1,
                "match": spec,
            }
        )
    except ValidationError as exc:
        raise FingerprintError(_summary(exc)) from exc
    db.execute(_LOCK_LAYOUTS, {"k": f"perfin:fingerprints:{household_id}"})
    existing = db.execute(
        select(ParserFingerprint.version)
        .where(
            ParserFingerprint.household_id == household_id,
            ParserFingerprint.institution_id == institution_id,
            ParserFingerprint.kind == kind,
            ParserFingerprint.parser_name == parser_name,
        )
        .order_by(ParserFingerprint.version.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is None:
        known = (
            select(
                ParserFingerprint.institution_id,
                ParserFingerprint.kind,
                ParserFingerprint.parser_name,
            )
            .where(ParserFingerprint.household_id == household_id)
            .distinct()
            .subquery()
        )
        layouts = db.execute(select(func.count()).select_from(known)).scalar_one()
        if layouts >= MAX_HOUSEHOLD_FINGERPRINTS:
            raise FingerprintLimitError(
                f"the household already has {MAX_HOUSEHOLD_FINGERPRINTS} remembered layouts"
            )
    elif existing >= MAX_VERSION:
        raise FingerprintLimitError(f"this layout already has {MAX_VERSION} versions")
    row = ParserFingerprint(
        household_id=household_id,
        institution_id=institution_id,
        kind=kind,
        match_rules=spec.model_dump(mode="json", exclude_defaults=True),
        parser_name=parser_name,
        version=(existing or 0) + 1,
        learned_from_document_id=document_id,
        template=template,
    )
    db.add(row)
    db.flush()
    return row


def match_fingerprint(
    fingerprint: Fingerprint, view: DocumentView, budget: MatchBudget | None = None
) -> FingerprintMatch | None:
    """Match one fingerprint; a household one runs under ``budget`` (else its own).

    An evaluation that runs out of budget is not a match.
    """
    if fingerprint.origin == "household":
        if budget is None:
            budget = MatchBudget(HOUSEHOLD_MATCH_SECONDS)
    else:
        budget = None
    if budget is not None and budget.spent():
        return None
    if not all(rule.holds(view, budget) for rule in fingerprint.all_rules):
        return None
    if fingerprint.one_of_rules and not any(
        rule.holds(view, budget) for rule in fingerprint.one_of_rules
    ):
        return None
    held = sum(1 for rule in fingerprint.any_rules if rule.holds(view, budget))
    if budget is not None and budget.exhausted:
        return None
    if held < fingerprint.spec.match.min_any:
        return None
    share = held / len(fingerprint.any_rules) if fingerprint.any_rules else 0.0
    return FingerprintMatch(fingerprint, round(BASE_CONFIDENCE + ANY_BONUS * share, 4), held)


def best_match(
    fingerprints: Iterable[Fingerprint],
    view: DocumentView,
    budget: MatchBudget | None = None,
) -> tuple[FingerprintMatch | None, Sequence[FingerprintMatch]]:
    """The strongest matching fingerprint and every match (household ones win ties).

    Two different institutions matching with the same confidence is not a
    match: the document goes on to the classifier and review. All household
    fingerprints share one budget of :data:`HOUSEHOLD_MATCH_SECONDS` (within
    ``budget`` when given).
    """
    shared = (
        MatchBudget(HOUSEHOLD_MATCH_SECONDS)
        if budget is None
        else budget.child(HOUSEHOLD_MATCH_SECONDS)
    )
    matches = [m for fp in fingerprints if (m := match_fingerprint(fp, view, shared)) is not None]
    if not matches:
        return None, ()
    matches.sort(
        key=lambda m: (
            -m.confidence,
            -len(m.fingerprint.all_rules),
            m.fingerprint.origin != "household",
            m.fingerprint.key,
        )
    )
    top = matches[0]
    rivals = [
        m
        for m in matches[1:]
        if m.confidence == top.confidence
        and len(m.fingerprint.all_rules) == len(top.fingerprint.all_rules)
        and normalise(m.fingerprint.spec.institution) != normalise(top.fingerprint.spec.institution)
    ]
    if rivals:
        return None, matches
    return top, matches
