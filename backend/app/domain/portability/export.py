# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writing a full export archive (docs/PORTABILITY.md, VISION.md "Everything exportable").

The archive is a zip with

* ``data/<entity>.json``: every row of every :data:`~app.domain.portability.entities.ENTITIES`
  table of the household (a JSON array, rows in id order, values encoded by
  :mod:`app.domain.portability.codec`);
* ``csv/accounts.csv`` and ``csv/transactions.csv`` for spreadsheets and
  accountants, every text cell neutralised against formula injection
  (``app.core.csv_safety``, SECURITY.md C7);
* ``documents/<document id>/<sanitised file name>``: each document's
  original, decrypted from the blob store and checked against its stored
  SHA-256;
* ``manifest.json`` (written last): format and app version, creation time,
  the household's name and base currency, row counts per entity, and the
  SHA-256 and size of every other member.

The zip is never written to disk in plaintext: it is produced as a stream
(no seeking) into an optional passphrase layer (``app.core.archive_crypto``)
and from there into a new encrypted object of the blob store
(``EncryptingWriter``: per-object key wrapped by the master key).

Rows are read in dependency order within one database transaction; a row
whose required parent was not exported (created between two reads) is left
out and an optional reference to a row that was not exported becomes null,
so every archive is self-consistent and importable.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
import uuid
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

import app
from app.core.archive_crypto import ArchiveEncryptor
from app.core.crypto import DecryptionError, Keyring
from app.core.csv_safety import neutralise_formula
from app.db.models import Household
from app.domain.accounts.balances import balances_as_of
from app.domain.accounts.models import Account
from app.domain.documents import service as documents_service
from app.domain.documents.blobstore import (
    BlobStore,
    BlobStoreError,
    EncryptingWriter,
    SealedBlob,
)
from app.domain.documents.models import Blob, BlobArea, Document
from app.domain.portability import codec
from app.domain.portability.entities import (
    CSV_PREFIX,
    DOCUMENTS_PREFIX,
    ENTITIES,
    LINK_TARGETS,
    MANIFEST,
    EntitySpec,
)
from app.domain.portability.schemas import ARCHIVE_FORMAT, FORMAT_VERSION

_FILENAME_UNSAFE: Final = re.compile(r"[^A-Za-z0-9._ -]+")
FILENAME_MAX: Final = 120
_DOS_EPOCH: Final = (1980, 1, 1, 0, 0, 0)


class ExportFailedError(Exception):
    """A deterministic failure (``slug``), recorded on the archive row."""

    def __init__(self, slug: str) -> None:
        super().__init__(slug)
        self.slug = slug


def member_filename(document: Document) -> str:
    """A safe file name for a document's original inside the archive.

    The stored name is metadata only (C3): it is reduced to letters, digits,
    ``.``, ``_``, ``-`` and spaces, stripped of leading/trailing dots and
    spaces, and cut to :data:`FILENAME_MAX` characters; without a usable
    name it is ``document``.
    """
    base = documents_service.clean_filename(document.original_filename) or ""
    ascii_base = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    safe = _FILENAME_UNSAFE.sub("_", ascii_base).strip(" .")[:FILENAME_MAX].strip(" .")
    return safe or "document"


class _ArchiveSink(io.RawIOBase):
    """An unseekable stream: the passphrase layer (optional), then the blob writer.

    Counts and hashes the bytes that end up in the file a user downloads.
    """

    def __init__(self, writer: EncryptingWriter, encryptor: ArchiveEncryptor | None) -> None:
        super().__init__()
        self._writer = writer
        self._encryptor = encryptor
        self.sha256 = hashlib.sha256()
        self.size = 0
        if encryptor is not None:
            self._emit(encryptor.header)

    def _emit(self, data: bytes) -> None:
        if data:
            self._writer.write(data)
            self.sha256.update(data)
            self.size += len(data)

    def writable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def write(self, data: Any) -> int:
        chunk = bytes(data)
        self._emit(self._encryptor.update(chunk) if self._encryptor is not None else chunk)
        return len(chunk)

    def finish(self) -> None:
        if self._encryptor is not None:
            self._emit(self._encryptor.finish())


@dataclass
class _Members:
    """SHA-256 and size of every member written so far."""

    entries: dict[str, dict[str, Any]] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportResult:
    sealed: SealedBlob
    size: int
    sha256: str
    counts: dict[str, int]


@contextmanager
def _member(
    archive: zipfile.ZipFile, members: _Members, name: str, created: datetime
) -> Iterator[Callable[[bytes], None]]:
    info = zipfile.ZipInfo(name, date_time=max(created.timetuple()[:6], _DOS_EPOCH))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o600 << 16
    digest = hashlib.sha256()
    size = 0
    with archive.open(info, "w", force_zip64=True) as handle:

        def write(data: bytes) -> None:
            nonlocal size
            if data:
                handle.write(data)
                digest.update(data)
                size += len(data)

        yield write
    members.entries[name] = {"sha256": digest.hexdigest(), "size": size}


def _rows(session: Session, spec: EntitySpec, household_id: uuid.UUID) -> Iterator[Any]:
    table = spec.table
    result = session.execute(
        select(table)
        .where(table.c.household_id == household_id)
        .order_by(table.c.id)
        .execution_options(yield_per=1000)
    )
    yield from result.mappings()


def _keep(
    spec: EntitySpec, row: dict[str, Any], exported: dict[str, set[uuid.UUID]]
) -> dict[str, Any] | None:
    """``row`` with references to rows that were not exported resolved (module doc)."""
    for column, target in spec.refs.items():
        value = row.get(column)
        if value is None or value in exported[target]:
            continue
        if not spec.table.c[column].nullable:
            return None
        row[column] = None
    if spec.name == "document_links":
        link_target = LINK_TARGETS.get(str(row["target_type"]))
        if link_target is None or row["target_id"] not in exported[link_target]:
            return None
    return row


class _Writer:
    def __init__(
        self,
        session: Session,
        household_id: uuid.UUID,
        archive: zipfile.ZipFile,
        *,
        store: BlobStore,
        keyring: Keyring,
        created: datetime,
        check_cancelled: Callable[[], None],
    ) -> None:
        self.session = session
        self.household_id = household_id
        self.archive = archive
        self.store = store
        self.keyring = keyring
        self.created = created
        self.check_cancelled = check_cancelled
        self.members = _Members()
        self.exported: dict[str, set[uuid.UUID]] = defaultdict(set)
        self.counts: dict[str, int] = {}
        # Small lookups for the CSV files.
        self.names: dict[str, dict[uuid.UUID, str]] = defaultdict(dict)
        self.category_parent: dict[uuid.UUID, uuid.UUID | None] = {}

    def member(self, name: str) -> Any:
        return _member(self.archive, self.members, name, self.created)

    def entity(self, spec: EntitySpec) -> None:
        columns = spec.columns()
        count = 0
        with self.member(spec.member) as write:
            write(b"[")
            for mapping in _rows(self.session, spec, self.household_id):
                row = _keep(spec, dict(mapping), self.exported)
                if row is None:
                    continue
                self._remember(spec, row)
                encoded = {c.name: codec.encode(c, row[c.name]) for c in columns}
                write((b",\n" if count else b"\n") + json.dumps(encoded, sort_keys=True).encode())
                self.exported[spec.name].add(row["id"])
                count += 1
                if count % 1000 == 0:
                    self.check_cancelled()
            write(b"\n]\n")
        self.counts[spec.name] = count

    def _remember(self, spec: EntitySpec, row: dict[str, Any]) -> None:
        if spec.name in ("accounts", "payees", "tags", "institutions"):
            self.names[spec.name][row["id"]] = str(row["name"])
        elif spec.name == "categories":
            self.names["categories"][row["id"]] = str(row["name"])
            self.category_parent[row["id"]] = row["parent_id"]

    def category_path(self, category_id: uuid.UUID | None) -> str:
        names: list[str] = []
        current = category_id
        seen: set[uuid.UUID] = set()
        while current is not None and current not in seen and len(names) < 10:
            seen.add(current)
            names.append(self.names["categories"].get(current, ""))
            current = self.category_parent.get(current)
        return " / ".join(reversed(names))

    def csv_member(self, name: str, header: list[str], rows: Iterable[list[Any]]) -> None:
        with self.member(f"{CSV_PREFIX}{name}") as write:
            for cells in [header, *rows]:
                buffer = io.StringIO()
                csv.writer(buffer, lineterminator="\r\n").writerow(
                    [neutralise_formula(_cell(cell)) for cell in cells]
                )
                write(buffer.getvalue().encode("utf-8"))

    def accounts_csv(self, today: date) -> None:
        accounts = list(
            self.session.execute(
                select(Account)
                .where(
                    Account.household_id == self.household_id,
                    Account.id.in_(self.exported["accounts"]),
                )
                .order_by(Account.id)
            ).scalars()
        )
        balances = balances_as_of(self.session, accounts, today) if accounts else {}
        header = [
            "id",
            "name",
            "nickname",
            "type",
            "asset_kind",
            "institution",
            "currency",
            "number_last4",
            "is_asset",
            "include_in_net_worth",
            "opened_on",
            "closed_on",
            "balance",
            "balance_as_of",
        ]
        self.csv_member(
            "accounts.csv",
            header,
            (
                [
                    a.id,
                    a.name,
                    a.nickname,
                    a.type,
                    a.asset_kind,
                    self.names["institutions"].get(a.institution_id) if a.institution_id else "",
                    a.currency,
                    a.number_last4,
                    a.is_asset,
                    a.include_in_net_worth,
                    a.opened_on,
                    a.closed_on,
                    balances.get(a.id, Decimal(0)),
                    today,
                ]
                for a in accounts
            ),
        )

    def transactions_csv(
        self,
        splits: dict[uuid.UUID, list[tuple[uuid.UUID | None, Decimal]]],
        tags: dict[uuid.UUID, list[str]],
    ) -> None:
        header = [
            "id",
            "posted_date",
            "effective_date",
            "account",
            "account_id",
            "amount",
            "currency",
            "payee",
            "raw_payee",
            "category",
            "splits",
            "tags",
            "memo",
            "notes",
            "status",
            "review_state",
            "transfer_group_id",
            "source_document_id",
        ]
        spec = next(e for e in ENTITIES if e.name == "transactions")

        def rows() -> Iterator[list[Any]]:
            for mapping in _rows(self.session, spec, self.household_id):
                if mapping["id"] not in self.exported["transactions"]:
                    continue
                txn_splits = splits.get(mapping["id"], [])
                yield [
                    mapping["id"],
                    mapping["posted_date"],
                    mapping["effective_date"],
                    self.names["accounts"].get(mapping["account_id"], ""),
                    mapping["account_id"],
                    mapping["amount"],
                    mapping["currency"],
                    self.names["payees"].get(mapping["payee_id"], "")
                    if mapping["payee_id"]
                    else "",
                    mapping["raw_payee"],
                    "" if txn_splits else self.category_path(mapping["category_id"]),
                    "; ".join(
                        f"{self.category_path(cat) or '(uncategorised)'}: {_cell(amount)}"
                        for cat, amount in txn_splits
                    ),
                    ", ".join(sorted(tags.get(mapping["id"], []))),
                    mapping["memo"],
                    mapping["notes"],
                    mapping["status"],
                    mapping["review_state"],
                    mapping["transfer_group_id"],
                    mapping["source_document_id"]
                    if mapping["source_document_id"] in self.exported["documents"]
                    else "",
                ]

        self.csv_member("transactions.csv", header, rows())

    def documents(self) -> int:
        count = 0
        query = (
            select(Document, Blob)
            .join(
                Blob,
                (Blob.id == Document.blob_id) & (Blob.household_id == Document.household_id),
            )
            .where(
                Document.household_id == self.household_id,
                Document.id.in_(self.exported["documents"]),
            )
            .order_by(Document.id)
        )
        for document, blob in self.session.execute(query).all():
            self.check_cancelled()
            name = f"{DOCUMENTS_PREFIX}{document.id}/{member_filename(document)}"
            digest = hashlib.sha256()
            size = 0
            try:
                with self.member(name) as write:
                    for chunk in documents_service.open_plaintext(self.store, self.keyring, blob):
                        digest.update(chunk)
                        size += len(chunk)
                        write(chunk)
            except (DecryptionError, BlobStoreError, OSError):
                raise ExportFailedError("document-unreadable") from None
            if digest.hexdigest() != document.sha256 or size != document.size:
                raise ExportFailedError("document-unreadable")
            count += 1
        return count


def _cell(value: Any) -> str:
    """A CSV cell: plain decimals, ISO dates, ``true``/``false``, enum values."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)


def _split_and_tag_lookups(
    writer: _Writer,
) -> tuple[dict[uuid.UUID, list[tuple[uuid.UUID | None, Decimal]]], dict[uuid.UUID, list[str]]]:
    splits: dict[uuid.UUID, list[tuple[uuid.UUID | None, Decimal]]] = defaultdict(list)
    tags: dict[uuid.UUID, list[str]] = defaultdict(list)
    by_name = {e.name: e for e in ENTITIES}
    for mapping in _rows(writer.session, by_name["transaction_splits"], writer.household_id):
        if mapping["id"] in writer.exported["transaction_splits"]:
            splits[mapping["transaction_id"]].append((mapping["category_id"], mapping["amount"]))
    for mapping in _rows(writer.session, by_name["transaction_tags"], writer.household_id):
        if mapping["id"] in writer.exported["transaction_tags"]:
            tags[mapping["transaction_id"]].append(writer.names["tags"].get(mapping["tag_id"], ""))
    return splits, tags


def write_export(
    session: Session,
    household_id: uuid.UUID,
    *,
    store: BlobStore,
    keyring: Keyring,
    blob_id: uuid.UUID,
    created: datetime,
    encryptor: ArchiveEncryptor | None = None,
    check_cancelled: Callable[[], None] = lambda: None,
) -> ExportResult:
    """Write the household's export into a new ``permanent`` object of ``store``.

    Raises :class:`ExportFailedError` for a document whose original cannot be
    read back intact; the object is removed on any failure.
    """
    household = session.get(Household, household_id)
    if household is None:
        raise ExportFailedError("household-missing")
    writer_blob = EncryptingWriter(store, keyring, blob_id, area=BlobArea.PERMANENT)
    try:
        sink = _ArchiveSink(writer_blob, encryptor)
        with zipfile.ZipFile(
            sink, mode="w", compression=zipfile.ZIP_DEFLATED, allowZip64=True
        ) as archive:
            writer = _Writer(
                session,
                household_id,
                archive,
                store=store,
                keyring=keyring,
                created=created,
                check_cancelled=check_cancelled,
            )
            for spec in ENTITIES:
                writer.entity(spec)
                check_cancelled()
            splits, tags = _split_and_tag_lookups(writer)
            writer.accounts_csv(created.date())
            writer.transactions_csv(splits, tags)
            writer.counts["document_files"] = writer.documents()
            manifest = {
                "format": ARCHIVE_FORMAT,
                "format_version": FORMAT_VERSION,
                "app_version": app.__version__,
                "created_at": created.isoformat(),
                "household": {
                    "name": household.name,
                    "base_currency": household.base_currency,
                },
                "counts": writer.counts,
                "members": writer.members.entries,
            }
            info = zipfile.ZipInfo(MANIFEST, date_time=max(created.timetuple()[:6], _DOS_EPOCH))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            archive.writestr(info, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        sink.finish()
        sealed = writer_blob.finish()
    except BaseException:
        writer_blob.abort()
        raise
    return ExportResult(
        sealed=sealed, size=sink.size, sha256=sink.sha256.hexdigest(), counts=dict(writer.counts)
    )
