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
(a fingerprint has at least one), and at least ``min_any`` of ``match.any``.
The confidence is ``0.85`` plus up to ``0.14`` for the share of ``any`` rules
that hold, so a fingerprint with more corroborating evidence wins a tie.

Rules (exactly one key each):

``header``   case-insensitive substring of the header (first page's top lines)
``text``     case-insensitive substring of any line
``regex``    safe pattern searched in each line (``region: header|text``)
``account_mask``  a mask such as ``XXXX-XXXX-XXXX-####`` found in a line
``columns``  a table whose header row starts with these cells (normalised)
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import cache
from importlib import resources
from typing import Annotated, Any, Final, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
from sqlalchemy import select
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

    @model_validator(mode="after")
    def _one_kind(self) -> Rule:
        set_keys = [
            k
            for k in ("header", "text", "regex", "account_mask", "columns")
            if getattr(self, k) is not None
        ]
        if len(set_keys) != 1:
            raise ValueError("a rule has exactly one of header, text, regex, account_mask, columns")
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

    def holds(self, view: DocumentView) -> bool:
        rule = self.rule
        if rule.header is not None:
            return any(self.needle in normalise(line) for line in view.header)
        if rule.text is not None:
            return any(self.needle in line for line in view.normalised)
        if self.pattern is not None:
            lines = view.header if rule.region == "header" else view.lines
            return any(search_line(self.pattern, line) for line in lines)
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
    return _CompiledRule(rule, needle=normalise(rule.header or rule.text or ""))


def compile_fingerprint(
    spec: FingerprintSpec,
    *,
    origin: Literal["builtin", "household"] = "builtin",
    fingerprint_id: uuid.UUID | None = None,
    institution_id: uuid.UUID | None = None,
) -> Fingerprint:
    rules = [*spec.match.all, *spec.match.any]
    masks = tuple(mask_regex(r.account_mask) for r in rules if r.account_mask is not None)
    return Fingerprint(
        spec=spec,
        all_rules=tuple(_compile_rule(r) for r in spec.match.all),
        any_rules=tuple(_compile_rule(r) for r in spec.match.any),
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
    """The household's own fingerprints; a stored row that fails validation is skipped."""
    rows = db.execute(
        select(ParserFingerprint, Institution.name, Institution.country)
        .join(
            Institution,
            (Institution.household_id == ParserFingerprint.household_id)
            & (Institution.id == ParserFingerprint.institution_id),
        )
        .where(ParserFingerprint.household_id == household_id)
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
) -> ParserFingerprint:
    """Store a user's fingerprint ("remember this layout"); the rules are validated first.

    The caller (the review API) checks that the institution and document are
    the household's and that the user may edit them; the database refuses
    cross-household references anyway (ADR-0008).
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
    row = ParserFingerprint(
        household_id=household_id,
        institution_id=institution_id,
        kind=kind,
        match_rules=spec.model_dump(mode="json", exclude_defaults=True),
        parser_name=parser_name,
        version=(existing or 0) + 1,
        learned_from_document_id=document_id,
    )
    db.add(row)
    db.flush()
    return row


def match_fingerprint(fingerprint: Fingerprint, view: DocumentView) -> FingerprintMatch | None:
    if not all(rule.holds(view) for rule in fingerprint.all_rules):
        return None
    held = sum(1 for rule in fingerprint.any_rules if rule.holds(view))
    if held < fingerprint.spec.match.min_any:
        return None
    share = held / len(fingerprint.any_rules) if fingerprint.any_rules else 0.0
    return FingerprintMatch(fingerprint, round(BASE_CONFIDENCE + ANY_BONUS * share, 4), held)


def best_match(
    fingerprints: Iterable[Fingerprint], view: DocumentView
) -> tuple[FingerprintMatch | None, Sequence[FingerprintMatch]]:
    """The strongest matching fingerprint and every match (household ones win ties).

    Two different institutions matching with the same confidence is not a
    match: the document goes on to the classifier and review.
    """
    matches = [m for fp in fingerprints if (m := match_fingerprint(fp, view)) is not None]
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
