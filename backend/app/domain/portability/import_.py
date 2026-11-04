# SPDX-License-Identifier: AGPL-3.0-or-later
"""Validating and applying a full export archive (docs/PORTABILITY.md "Import").

:func:`read_archive` checks everything it can before the database is
touched: the zip itself (:mod:`app.domain.portability.zipsafe`, C2/C3), the
manifest (format name; an unknown ``format_version`` is refused), that the
members are exactly those the manifest lists with matching SHA-256 and size,
that every member has a known place in the layout, and every row of every
``data/*.json`` member (exact column set, values decoded strictly by
:mod:`app.domain.portability.codec`, unique ids, counts as in the manifest).

:func:`apply_archive` then writes into the **current** household inside the
caller's transaction, which the caller makes all-or-nothing (a savepoint and
``SET CONSTRAINTS ALL IMMEDIATE``, so even the deferred household foreign
keys are checked before it decides):

* every row gets a new id; references are re-mapped through the ids of the
  archive and a reference to a row that is not in the archive refuses the
  import (``archive-reference-unknown``);
* institutions (by built-in key, then name), categories (by system key, then
  parent and name), payees (by normalised name) and tags (by name) are
  **merged** into existing ones of the household; an existing import mapping
  of the same name or an FX rate for the same pair and day is kept and the
  archive's is skipped; names that would clash are suffixed ``(imported)``;
* everything else (accounts, transactions, splits, tags on transactions,
  balance snapshots, schedules, rules, budgets, goals, documents and their
  links) is created; rules are appended after the household's own;
* ``import_hash`` embeds the account id, so it is recomputed for the new
  account (its running index is recovered by trying the indexes until the
  stored hash matches); re-importing the same statement into the new
  household is therefore still recognised as a duplicate. A hash that cannot
  be recovered is dropped (counted as ``import_hash_unrecoverable``);
* document originals are checked against the manifest and the document's
  own SHA-256, sniffed again (the archive's ``mime_detected`` is not
  trusted; a type intake would refuse is ``archive-document-rejected``) and
  encrypted into new ``permanent`` blobs; documents still being processed
  when exported arrive as ``needs_review``;
* rules, import mapping specs, split sums and the category depth are
  validated as the services that normally write them would.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from pydantic import ValidationError
from sqlalchemy import insert, select, text
from sqlalchemy.orm import Session

from app.core.crypto import Keyring
from app.core.ids import uuid7
from app.domain.accounts.models import Institution
from app.domain.documents.blobstore import BlobStore, EncryptingWriter, SealedBlob
from app.domain.documents.models import (
    TERMINAL_STATUSES,
    Blob,
    BlobArea,
    DocumentStatus,
)
from app.domain.fx.models import FxRate
from app.domain.imports.models import ImportMapping
from app.domain.imports.schemas import MappingSpec
from app.domain.portability.codec import ArchiveInvalidError, decode
from app.domain.portability.entities import (
    BY_NAME,
    CSV_PREFIX,
    DOCUMENTS_PREFIX,
    ENTITIES,
    LINK_TARGETS,
    MANIFEST,
)
from app.domain.portability.schemas import (
    ARCHIVE_FORMAT,
    SUPPORTED_FORMAT_VERSIONS,
    Manifest,
)
from app.domain.portability.zipsafe import SafeZip, ZipLimits
from app.domain.rules import engine
from app.domain.rules import repo as rules_repo
from app.domain.rules.schemas import RULES_MAX, RuleBody
from app.domain.taxonomy.service import MAX_CATEGORY_DEPTH
from app.domain.transactions.import_hash import import_hash, normalize_payee
from app.domain.transactions.models import Category, Payee, Tag
from app.ingestion.intake.sniff import SNIFF_BYTES, Rejected, sniff

MANIFEST_MAX_BYTES: Final = 16 * 1024 * 1024
#: Largest ``data/*.json`` or ``csv/*.csv`` member read into memory.
DATA_MEMBER_MAX_BYTES: Final = 512 * 1024 * 1024
#: How many running indexes are tried to recover an import hash.
HASH_INDEX_MAX: Final = 64
CSV_MEMBERS: Final = frozenset({f"{CSV_PREFIX}accounts.csv", f"{CSV_PREFIX}transactions.csv"})
_RULE_KINDS: Final = {
    "category": "categories",
    "payee": "payees",
    "tag": "tags",
    "account": "accounts",
}


def _fail(slug: str, detail: str) -> ArchiveInvalidError:
    return ArchiveInvalidError(slug, detail)


@dataclass
class ParsedArchive:
    """An archive that passed :func:`read_archive`; ``rows`` hold decoded values."""

    manifest: Manifest
    rows: dict[str, list[dict[str, Any]]]
    documents: dict[uuid.UUID, str]
    zip: SafeZip


def _manifest(archive: SafeZip) -> Manifest:
    if MANIFEST not in archive.names:
        raise _fail("archive-manifest-invalid", "the archive has no manifest.json")
    try:
        raw = json.loads(archive.read(MANIFEST, limit=MANIFEST_MAX_BYTES))
    except (ValueError, RecursionError):
        raise _fail("archive-manifest-invalid", "manifest.json is not valid JSON") from None
    if not isinstance(raw, dict) or raw.get("format") != ARCHIVE_FORMAT:
        raise _fail("archive-manifest-invalid", "this is not a PerFin export")
    version = raw.get("format_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise _fail("archive-manifest-invalid", "the manifest has no format version")
    if version not in SUPPORTED_FORMAT_VERSIONS:
        raise _fail(
            "archive-version-unsupported",
            f"format version {version} is not supported by this PerFin "
            f"(supported: {', '.join(map(str, sorted(SUPPORTED_FORMAT_VERSIONS)))})",
        )
    try:
        return Manifest.model_validate(raw)
    except ValidationError:
        raise _fail("archive-manifest-invalid", "manifest.json does not validate") from None


def _check_member(archive: SafeZip, manifest: Manifest, name: str, data: bytes) -> None:
    entry = manifest.members[name]
    if len(data) != entry.size or hashlib.sha256(data).hexdigest() != entry.sha256:
        raise _fail("archive-hash-mismatch", f"{name} does not match its manifest hash")


def _decode_rows(entity: str, data: bytes) -> list[dict[str, Any]]:
    spec = BY_NAME[entity]
    try:
        raw = json.loads(data)
    except (ValueError, RecursionError):
        raise _fail("archive-invalid-value", f"{spec.member} is not valid JSON") from None
    if not isinstance(raw, list):
        raise _fail("archive-invalid-value", f"{spec.member} is not a JSON array")
    columns = {c.name: c for c in spec.columns()}
    expected = set(columns)
    rows: list[dict[str, Any]] = []
    seen: set[uuid.UUID] = set()
    for item in raw:
        if not isinstance(item, dict) or set(item) != expected:
            raise _fail("archive-invalid-value", f"{spec.member}: a row has unexpected fields")
        row = {name: decode(entity, column, item[name]) for name, column in columns.items()}
        if row["id"] in seen:
            raise _fail("archive-invalid-value", f"{spec.member}: duplicate id")
        seen.add(row["id"])
        rows.append(row)
    return rows


def read_archive(data: bytes, *, limits: ZipLimits | None = None) -> ParsedArchive:
    """Check an archive completely before anything is written (module doc)."""
    archive = SafeZip(data, limits)
    manifest = _manifest(archive)
    members = set(archive.names) - {MANIFEST}
    if members != set(manifest.members):
        raise _fail(
            "archive-members-mismatch", "the archive's members differ from those in its manifest"
        )
    rows: dict[str, list[dict[str, Any]]] = {}
    for spec in ENTITIES:
        if spec.member not in members:
            raise _fail("archive-members-mismatch", f"the archive has no {spec.member}")
        content = archive.read(spec.member, limit=DATA_MEMBER_MAX_BYTES)
        _check_member(archive, manifest, spec.member, content)
        rows[spec.name] = _decode_rows(spec.name, content)
        if manifest.counts.get(spec.name) != len(rows[spec.name]):
            raise _fail("archive-members-mismatch", f"the row count of {spec.member} differs")
    for name in CSV_MEMBERS & members:
        _check_member(archive, manifest, name, archive.read(name, limit=DATA_MEMBER_MAX_BYTES))
    known = {spec.member for spec in ENTITIES} | CSV_MEMBERS
    document_ids = {row["id"] for row in rows["documents"]}
    documents: dict[uuid.UUID, str] = {}
    for name in sorted(members - known):
        parts = name.split("/")
        if len(parts) != 3 or f"{parts[0]}/" != DOCUMENTS_PREFIX:
            raise _fail(
                "archive-unexpected-member", "the archive has a member PerFin does not know"
            )
        try:
            document_id = uuid.UUID(parts[1])
        except ValueError:
            raise _fail("archive-unexpected-member", "a document folder is not an id") from None
        if document_id not in document_ids or document_id in documents:
            raise _fail("archive-unexpected-member", "a document file has no single document row")
        documents[document_id] = name
    if set(documents) != document_ids:
        raise _fail("archive-members-mismatch", "a document has no original in the archive")
    return ParsedArchive(manifest=manifest, rows=rows, documents=documents, zip=archive)


def blob_row(household_id: uuid.UUID, sealed: SealedBlob) -> dict[str, Any]:
    """The ``blob`` row for an object written by ``EncryptingWriter``."""
    return {
        "id": sealed.blob_id,
        "household_id": household_id,
        "store": sealed.store,
        "key": sealed.key,
        "area": sealed.area,
        "size": sealed.size,
        "stored_size": sealed.stored_size,
        "enc_alg": sealed.enc_alg,
        "key_version": sealed.wrapped.key_version,
        "wrap_nonce": sealed.wrapped.wrap_nonce,
        "wrapped_dek": sealed.wrapped.wrapped_dek,
        "nonce": sealed.header,
    }


def _free_name(name: str, taken: set[str], limit: int) -> str:
    if name not in taken:
        return name
    for n in range(1, 1000):
        suffix = " (imported)" if n == 1 else f" (imported {n})"
        candidate = name[: limit - len(suffix)] + suffix
        if candidate not in taken:
            return candidate
    raise _fail("archive-limit", "too many clashing names")


def recover_import_hash(
    stored: str, old_account: uuid.UUID, new_account: uuid.UUID, row: dict[str, Any]
) -> str | None:
    """``stored`` recomputed for ``new_account`` (module doc), or None."""
    for index in range(HASH_INDEX_MAX):
        args = (row["posted_date"], row["amount"], row["raw_payee"], index)
        if import_hash(old_account, *args) == stored:
            return import_hash(new_account, *args)
    return None


class _Importer:
    def __init__(
        self,
        session: Session,
        household_id: uuid.UUID,
        parsed: ParsedArchive,
        *,
        store: BlobStore,
        keyring: Keyring,
        check_cancelled: Callable[[], None],
        written_keys: list[str],
    ) -> None:
        self.session = session
        self.household_id = household_id
        self.parsed = parsed
        self.store = store
        self.keyring = keyring
        self.check_cancelled = check_cancelled
        self.ids: dict[str, dict[uuid.UUID, uuid.UUID]] = defaultdict(dict)
        self.created: dict[str, int] = defaultdict(int)
        self.merged: dict[str, int] = defaultdict(int)
        self.skipped: dict[str, int] = defaultdict(int)
        self.unrecoverable_hashes = 0
        self.written_keys = written_keys

    # ---- helpers ---------------------------------------------------------
    def rows(self, entity: str) -> list[dict[str, Any]]:
        return [dict(row) for row in self.parsed.rows[entity]]

    def map_refs(self, entity: str, row: dict[str, Any]) -> dict[str, Any]:
        for column, target in BY_NAME[entity].refs.items():
            value = row.get(column)
            if value is None:
                continue
            mapped = self.ids[target].get(value)
            if mapped is None:
                raise _fail(
                    "archive-reference-unknown",
                    f"{entity}.{column} names a {target} row that is not in the archive",
                )
            row[column] = mapped
        return row

    def insert(
        self,
        entity: str,
        rows: Iterable[dict[str, Any]] | None = None,
        transform: Callable[[dict[str, Any], uuid.UUID], dict[str, Any]] | None = None,
    ) -> None:
        spec = BY_NAME[entity]
        out: list[dict[str, Any]] = []
        for row in self.rows(entity) if rows is None else rows:
            old = row.pop("id")
            row = self.map_refs(entity, row)
            if transform is not None:
                row = transform(row, old)
            new = uuid7()
            self.ids[entity][old] = new
            row["id"] = new
            row["household_id"] = self.household_id
            out.append(row)
        self.write(spec.table, out)
        self.created[entity] += len(out)
        self.check_cancelled()

    def write(self, table: Any, rows: list[dict[str, Any]]) -> None:
        for start in range(0, len(rows), 1000):
            self.session.execute(insert(table), rows[start : start + 1000])

    # ---- merged lookups --------------------------------------------------
    def institutions(self) -> None:
        existing = self.session.execute(
            select(Institution.id, Institution.name, Institution.builtin_key).where(
                Institution.household_id == self.household_id
            )
        ).all()
        by_key = {key: iid for iid, _, key in existing if key}
        by_name = {name: iid for iid, name, _ in existing}
        names = set(by_name)
        claimed: set[uuid.UUID] = set()
        new_rows: list[dict[str, Any]] = []
        for row in self.rows("institutions"):
            match = by_key.get(row["builtin_key"]) if row["builtin_key"] else None
            match = match or by_name.get(row["name"])
            if match is not None and match not in claimed:
                claimed.add(match)
                self.ids["institutions"][row["id"]] = match
                self.merged["institutions"] += 1
                continue
            row["name"] = _free_name(row["name"], names, 200)
            if row["builtin_key"] in by_key:
                row["builtin_key"] = None
            names.add(row["name"])
            new_rows.append(row)
        self.insert("institutions", new_rows)

    def categories(self) -> None:
        existing = self.session.execute(
            select(Category.id, Category.parent_id, Category.name, Category.system_key).where(
                Category.household_id == self.household_id
            )
        ).all()
        parent_of = {cid: parent for cid, parent, _, _ in existing}
        by_key = {key: cid for cid, _, _, key in existing if key}
        by_place = {(parent, name): cid for cid, parent, name, _ in existing}
        siblings: dict[uuid.UUID | None, set[str]] = defaultdict(set)
        for _, parent, name, _ in existing:
            siblings[parent].add(name)
        claimed: set[uuid.UUID] = set()

        def depth(cid: uuid.UUID | None) -> int:
            level = 0
            seen: set[uuid.UUID] = set()
            while cid is not None and cid not in seen:
                seen.add(cid)
                level += 1
                cid = parent_of.get(cid)
            return level

        pending = self.rows("categories")
        ordered: list[dict[str, Any]] = []
        while pending:
            progressed = False
            waiting: list[dict[str, Any]] = []
            for row in pending:
                old_parent = row["parent_id"]
                if old_parent is not None and old_parent not in self.ids["categories"]:
                    waiting.append(row)
                    continue
                progressed = True
                parent = self.ids["categories"].get(old_parent) if old_parent else None
                key = row["system_key"]
                match = by_key.get(key) if key else None
                if match is None or match in claimed:
                    match = by_place.get((parent, row["name"]))
                if match is not None and match not in claimed:
                    claimed.add(match)
                    self.ids["categories"][row["id"]] = match
                    self.merged["categories"] += 1
                    continue
                new = uuid7()
                if depth(parent) + 1 > MAX_CATEGORY_DEPTH:
                    raise _fail(
                        "archive-limit", f"categories nest at most {MAX_CATEGORY_DEPTH} levels"
                    )
                name = _free_name(row["name"], siblings[parent], 200)
                siblings[parent].add(name)
                if key and key in by_key:
                    key = None
                if key:
                    by_key[key] = new
                parent_of[new] = parent
                claimed.add(new)
                self.ids["categories"][row["id"]] = new
                ordered.append(
                    {**row, "id": new, "parent_id": parent, "name": name, "system_key": key}
                )
            if not progressed:
                raise _fail("archive-reference-unknown", "the category tree has a cycle or orphan")
            pending = waiting
        for row in ordered:
            row["household_id"] = self.household_id
        # Parents precede their children in ``ordered`` (inserted in order).
        self.write(Category.__table__, ordered)
        self.created["categories"] += len(ordered)

    def payees(self) -> None:
        existing = self.session.execute(
            select(Payee.id, Payee.normalized_name).where(Payee.household_id == self.household_id)
        ).all()
        by_norm = {norm: pid for pid, norm in existing}
        first_of: dict[str, uuid.UUID] = {}
        same_as: dict[uuid.UUID, uuid.UUID] = {}
        new_rows: list[dict[str, Any]] = []
        for row in self.rows("payees"):
            norm = normalize_payee(row["name"])
            match = by_norm.get(norm)
            if match is not None:
                self.ids["payees"][row["id"]] = match
                self.merged["payees"] += 1
                continue
            if norm in first_of:
                # Two archive payees that normalise alike become one.
                same_as[row["id"]] = first_of[norm]
                self.merged["payees"] += 1
                continue
            first_of[norm] = row["id"]
            row["normalized_name"] = norm
            new_rows.append(row)
        self.insert("payees", new_rows)
        for old, first in same_as.items():
            self.ids["payees"][old] = self.ids["payees"][first]

    def tags(self) -> None:
        existing: dict[str, uuid.UUID] = dict(
            self.session.execute(
                select(Tag.name, Tag.id).where(Tag.household_id == self.household_id)
            ).all()
        )
        new_rows: list[dict[str, Any]] = []
        for row in self.rows("tags"):
            match = existing.get(row["name"])
            if match is not None:
                self.ids["tags"][row["id"]] = match
                self.merged["tags"] += 1
                continue
            new_rows.append(row)
        self.insert("tags", new_rows)

    def fx_rates(self) -> None:
        existing = {
            (base, quote, day)
            for base, quote, day in self.session.execute(
                select(FxRate.base, FxRate.quote, FxRate.rate_date).where(
                    FxRate.household_id == self.household_id
                )
            )
        }
        new_rows = []
        for row in self.rows("fx_rates"):
            if (row["base"], row["quote"], row["rate_date"]) in existing:
                self.skipped["fx_rates"] += 1
                continue
            new_rows.append(row)
        self.insert("fx_rates", new_rows)

    def import_mappings(self) -> None:
        taken = set(
            self.session.execute(
                select(ImportMapping.name).where(ImportMapping.household_id == self.household_id)
            ).scalars()
        )
        new_rows = []
        for row in self.rows("import_mappings"):
            try:
                MappingSpec.model_validate(row["spec"])
            except ValidationError:
                raise _fail(
                    "archive-invalid-value", "an import mapping does not validate"
                ) from None
            if row["name"] in taken:
                self.skipped["import_mappings"] += 1
                continue
            row["created_by"] = None
            new_rows.append(row)
        self.insert("import_mappings", new_rows)

    # ---- created entities ------------------------------------------------
    def documents(self) -> None:
        blobs: list[dict[str, Any]] = []
        documents: list[dict[str, Any]] = []
        for row in self.rows("documents"):
            old = row["id"]
            sealed, mime = self.store_original(old, row)
            row = self.map_refs("documents", row)
            new = uuid7()
            self.ids["documents"][old] = new
            if row["status"] not in TERMINAL_STATUSES:
                row["status"] = DocumentStatus.NEEDS_REVIEW
            blobs.append(blob_row(self.household_id, sealed))
            documents.append(
                {
                    **row,
                    "id": new,
                    "household_id": self.household_id,
                    "blob_id": sealed.blob_id,
                    "mime_detected": mime,
                    "uploaded_by": None,
                    "extracted_text": None,
                }
            )
            self.check_cancelled()
        self.write(Blob.__table__, blobs)
        self.write(BY_NAME["documents"].table, documents)
        self.created["documents"] += len(documents)

    def store_original(self, document_id: uuid.UUID, row: dict[str, Any]) -> tuple[SealedBlob, str]:
        name = self.parsed.documents[document_id]
        entry = self.parsed.manifest.members[name]
        digest = hashlib.sha256()
        head = bytearray()
        writer = EncryptingWriter(self.store, self.keyring, uuid7(), area=BlobArea.PERMANENT)
        try:
            for chunk in self.parsed.zip.chunks(name):
                digest.update(chunk)
                if len(head) < SNIFF_BYTES:
                    head += chunk[: SNIFF_BYTES - len(head)]
                writer.write(chunk)
            if writer.size != entry.size or digest.hexdigest() != entry.sha256:
                raise _fail("archive-hash-mismatch", f"{name} does not match its manifest hash")
            if writer.size != row["size"] or digest.hexdigest() != row["sha256"]:
                raise _fail("archive-hash-mismatch", f"{name} does not match its document row")
            detected = sniff(bytes(head))
            if isinstance(detected, Rejected):
                raise _fail(
                    "archive-document-rejected",
                    f"a document is of a type PerFin does not accept ({detected.reason})",
                )
            mime = str(detected.value)
            sealed = writer.finish()
        except BaseException:
            writer.abort()
            raise
        self.written_keys.append(sealed.key)
        return sealed, mime

    def transactions(self) -> None:
        groups: dict[uuid.UUID, uuid.UUID] = {}
        old_accounts = {row["id"]: row["account_id"] for row in self.parsed.rows["transactions"]}

        def transform(row: dict[str, Any], old: uuid.UUID) -> dict[str, Any]:
            group = row["transfer_group_id"]
            if group is not None:
                row["transfer_group_id"] = groups.setdefault(group, uuid7())
            stored = row["import_hash"]
            if stored is not None:
                recovered = recover_import_hash(stored, old_accounts[old], row["account_id"], row)
                if recovered is None:
                    self.unrecoverable_hashes += 1
                row["import_hash"] = recovered
            return row

        self.insert("transactions", transform=transform)

    def check_splits(self) -> None:
        amounts = {row["id"]: row["amount"] for row in self.parsed.rows["transactions"]}
        totals: dict[uuid.UUID, Decimal] = defaultdict(Decimal)
        for split in self.parsed.rows["transaction_splits"]:
            totals[split["transaction_id"]] += split["amount"]
        for txn_id, total in totals.items():
            if txn_id in amounts and total != amounts[txn_id]:
                raise _fail(
                    "archive-invalid-value", "the splits of a transaction do not add up to it"
                )

    def rules(self) -> None:
        rows = sorted(self.rows("rules"), key=lambda r: (r["sort_order"], r["id"]))
        if rules_repo.count_rules(self.session, self.household_id) + len(rows) > RULES_MAX:
            raise _fail("archive-limit", f"a household has at most {RULES_MAX} rules")
        start = rules_repo.next_sort_order(self.session, self.household_id)
        out: list[dict[str, Any]] = []
        for offset, row in enumerate(rows):
            documents = [row["conditions"], row["actions"]]
            mapping: dict[str, dict[uuid.UUID, uuid.UUID]] = {}
            for kind, ids in engine.references(documents).items():
                table = self.ids[_RULE_KINDS[kind]]
                # An id the archive does not hold was already dangling at
                # export; it stays dangling (a fresh id never names a row).
                mapping[kind] = {i: table.get(i) or uuid7() for i in ids}
            try:
                body = RuleBody.model_validate(
                    {
                        "conditions": engine.remap(row["conditions"], mapping),
                        "actions": engine.remap(row["actions"], mapping),
                        "stop_processing": row["stop_processing"],
                    }
                )
            except (ValidationError, ValueError, TypeError):
                raise _fail("archive-invalid-value", "a rule does not validate") from None
            new = uuid7()
            self.ids["rules"][row["id"]] = new
            out.append(
                {
                    **row,
                    "id": new,
                    "household_id": self.household_id,
                    "sort_order": start + offset,
                    "conditions": body.conditions.model_dump(mode="json"),
                    "actions": [action.model_dump(mode="json") for action in body.actions],
                }
            )
        self.write(BY_NAME["rules"].table, out)
        self.created["rules"] += len(out)

    def document_links(self) -> None:
        def transform(row: dict[str, Any], _: uuid.UUID) -> dict[str, Any]:
            target = LINK_TARGETS.get(str(row["target_type"]))
            mapped = self.ids[target].get(row["target_id"]) if target else None
            if mapped is None:
                raise _fail(
                    "archive-reference-unknown", "a document link names a row not in the archive"
                )
            row["target_id"] = mapped
            return row

        self.insert("document_links", transform=transform)

    def run(self) -> None:
        self.check_splits()
        self.institutions()
        self.categories()
        self.payees()
        self.tags()
        self.insert("accounts")
        self.documents()
        self.transactions()
        self.insert("transaction_splits")
        self.insert("transaction_tags")
        self.insert("balance_snapshots")
        self.insert("scheduled_transactions")
        self.rules()
        self.insert("budgets")
        self.insert("budget_lines")
        self.insert("goals")
        self.insert("goal_accounts")
        self.insert("goal_contributions")
        self.fx_rates()
        self.import_mappings()
        self.document_links()
        # Deferred household foreign keys are checked now, inside the
        # caller's savepoint, instead of at the job's commit.
        self.session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))


def apply_archive(
    session: Session,
    household_id: uuid.UUID,
    parsed: ParsedArchive,
    *,
    store: BlobStore,
    keyring: Keyring,
    written_keys: list[str],
    check_cancelled: Callable[[], None] = lambda: None,
) -> dict[str, Any]:
    """Write ``parsed`` into ``household_id`` (module doc); returns the summary.

    The blob keys of document originals written so far are appended to
    ``written_keys`` (the caller deletes them if the import is rolled back).
    """
    importer = _Importer(
        session,
        household_id,
        parsed,
        store=store,
        keyring=keyring,
        check_cancelled=check_cancelled,
        written_keys=written_keys,
    )
    importer.run()
    return {
        "created": dict(sorted(importer.created.items())),
        "merged": dict(sorted(importer.merged.items())),
        "skipped": dict(sorted(importer.skipped.items())),
        "import_hash_unrecoverable": importer.unrecoverable_hashes,
        "archive_created_at": parsed.manifest.created_at.isoformat(),
        "archive_base_currency": parsed.manifest.household.base_currency,
    }
