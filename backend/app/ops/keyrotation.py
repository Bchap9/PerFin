# SPDX-License-Identifier: AGPL-3.0-or-later
"""Master-key rotation: rewrap every data key under a new key (SECURITY.md G4).

PerFin encrypts every secret column and every document with its own data key
(DEK, ADR-0003); the master key only wraps DEKs. Rotation therefore rewraps
DEKs and never re-encrypts a payload:

* **Blobs** (``blob.wrapped_dek``/``wrap_nonce``/``key_version``): the DEK is
  unwrapped with the old key and wrapped with the new one; the file on disk
  and ``blob.nonce`` are not touched (:meth:`Keyring.rewrap_data_key`).
* **Encrypted columns** (:data:`ENVELOPE_COLUMNS`): each value is a
  :class:`app.core.crypto.Envelope`; :meth:`Keyring.rewrap` replaces its
  header and wrap and keeps the payload nonce and ciphertext byte for byte.

The key version every DEK was wrapped with is stored next to it (the blob's
``key_version`` column, bytes 5-8 of an envelope), so the rotation is
**resumable**: each pass selects only the DEKs not yet at the new version, in
batches of ``batch_size`` committed one by one; an interrupted run is simply
started again. **Verification** then unwraps every DEK (and authenticates
every envelope payload) with a keyring holding *only* the new key. Only a
verified run appends the ``instance.master_key_rotated`` audit row; the
operator then switches ``PERFIN_MASTER_KEY(_FILE)`` and
``PERFIN_MASTER_KEY_VERSION`` to the new key (docs/BACKUP_RESTORE.md).

The command connects as the application role and works household by
household under RLS (``perfin_household_ids()``); the identity tables
(MFA, OIDC sessions) are not household-scoped. A session advisory lock keeps
two rotations from running at once. API and worker must be stopped: a value
they write with the old key during the run is picked up by the next run, but
they cannot read values the run has already rewrapped.
"""

from __future__ import annotations

import struct
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from sqlalchemy import Connection, Engine, Table, func, select, text, update

from app.core.audit import audit
from app.core.crypto import (
    DecryptionError,
    Envelope,
    Keyring,
    WrappedKey,
    column_ad,
)
from app.db.base import Base, household_scoped_tables
from app.db.registry import import_all_models
from app.db.rls import set_context
from app.ops.defaults import DEFAULT_BATCH_SIZE

#: Advisory lock key of a running rotation (any constant unique to PerFin).
LOCK_KEY: Final = 0x5045_5246_524B_0001  # "PERF" "RK" 1


@dataclass(frozen=True)
class EncryptedColumn:
    """A column holding :class:`Envelope` bytes with ``column_ad(table, column, id)``."""

    table: str
    column: str

    @property
    def name(self) -> str:
        return f"{self.table}.{self.column}"


#: Every column whose value is an envelope under the master key. A new
#: encrypted column must be listed here (or in :data:`NOT_ENVELOPES` with a
#: reason): ``tests/unit/test_keyrotation_units.py`` fails otherwise.
ENVELOPE_COLUMNS: Final[tuple[EncryptedColumn, ...]] = (
    EncryptedColumn("mfa_method", "secret_encrypted"),
    EncryptedColumn("mfa_challenge", "secret_encrypted"),
    EncryptedColumn("oidc_session", "id_token_encrypted"),
    EncryptedColumn("oidc_session", "refresh_token_encrypted"),
    EncryptedColumn("notification_channel", "config_encrypted"),
    EncryptedColumn("mail_intake", "username_encrypted"),
    EncryptedColumn("mail_intake", "password_encrypted"),
    EncryptedColumn("mail_intake", "alias_encrypted"),
    EncryptedColumn("portability_archive", "passphrase_key"),
)

#: Binary columns that are not envelopes, and why rotation leaves them alone.
NOT_ENVELOPES: Final[dict[tuple[str, str], str]] = {
    ("blob", "wrapped_dek"): "the blob's wrapped DEK: rewrapped by the blob pass",
    ("blob", "wrap_nonce"): "nonce of the blob's DEK wrap: replaced by the blob pass",
    ("blob", "nonce"): "secretstream header of the file payload: never changes",
    ("mfa_method", "public_key"): "WebAuthn public key: not secret, not encrypted",
    ("mfa_method", "credential_id"): "WebAuthn credential id: not secret, not encrypted",
    ("mfa_challenge", "challenge"): "WebAuthn challenge bytes: random, not encrypted",
}

BLOB_TARGET: Final = "blob.wrapped_dek"


def version_bytes(version: int) -> bytes:
    """Bytes 5-8 of an envelope written with master-key ``version``."""
    return struct.pack(">I", version)


def _table(name: str) -> Table:
    import_all_models()
    return Base.metadata.tables[name]


def _is_household_table(name: str) -> bool:
    import_all_models()
    return name in household_scoped_tables()


def targets() -> list[str]:
    """Names of everything rotation rewraps, in processing order."""
    return [BLOB_TARGET, *(c.name for c in ENVELOPE_COLUMNS)]


# ---------------------------------------------------------------------------
# Scopes: household contexts and the context-free identity tables
# ---------------------------------------------------------------------------


def household_ids(engine: Engine) -> list[uuid.UUID]:
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT * FROM perfin_household_ids()")).scalars()
        return [v if isinstance(v, uuid.UUID) else uuid.UUID(str(v)) for v in rows]


def _scopes(engine: Engine, table: str) -> list[uuid.UUID | None]:
    """RLS contexts to visit for ``table``: each household, or none at all."""
    return list(household_ids(engine)) if _is_household_table(table) else [None]


@contextmanager
def _scoped(engine: Engine, household_id: uuid.UUID | None) -> Iterator[Connection]:
    with engine.begin() as conn:
        set_context(conn, household_id)
        yield conn


# ---------------------------------------------------------------------------
# Status: DEKs per key version
# ---------------------------------------------------------------------------


def versions_by_target(engine: Engine) -> dict[str, dict[int, int]]:
    """For every target, how many DEKs are wrapped under each key version."""
    out: dict[str, dict[int, int]] = {}
    blob = _table("blob")
    counts: Counter[int] = Counter()
    for scope in _scopes(engine, "blob"):
        with _scoped(engine, scope) as conn:
            for version, n in conn.execute(
                select(blob.c.key_version, func.count())
                .where(blob.c.wrapped_dek.is_not(None))
                .group_by(blob.c.key_version)
            ):
                counts[int(version)] += int(n)
    out[BLOB_TARGET] = dict(sorted(counts.items()))
    for target in ENVELOPE_COLUMNS:
        table = _table(target.table)
        col = table.c[target.column]
        counts = Counter()
        for scope in _scopes(engine, target.table):
            with _scoped(engine, scope) as conn:
                marker = func.substring(col, 5, 4).label("version_marker")
                for raw, n in conn.execute(
                    select(marker, func.count()).where(col.is_not(None)).group_by(marker)
                ):
                    data = bytes(raw)
                    version = struct.unpack(">I", data)[0] if len(data) == 4 else 0
                    counts[version] += int(n)
        out[target.name] = dict(sorted(counts.items()))
    return out


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------


@dataclass
class TargetResult:
    rewrapped: int = 0
    failed: int = 0
    failed_ids: list[str] = field(default_factory=list)


@dataclass
class RotationReport:
    from_version: int
    to_version: int
    from_key_id: str
    to_key_id: str
    results: dict[str, TargetResult]
    verified: int = 0
    verify_failed: int = 0
    remaining: dict[str, dict[int, int]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return (
            all(r.failed == 0 for r in self.results.values())
            and self.verify_failed == 0
            and not any(v != self.to_version for counts in self.remaining.values() for v in counts)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "from_key_id": self.from_key_id,
            "to_key_id": self.to_key_id,
            "rewrapped": {k: r.rewrapped for k, r in self.results.items()},
            "failed": {k: r.failed for k, r in self.results.items() if r.failed},
            "failed_ids": {k: r.failed_ids[:20] for k, r in self.results.items() if r.failed},
            "verified": self.verified,
            "verify_failed": self.verify_failed,
            "remaining": {
                k: {str(v): n for v, n in counts.items()} for k, counts in self.remaining.items()
            },
        }


Progress = Callable[[str], None]


class RotationLockedError(RuntimeError):
    """Another ``perfin rotate-key`` holds the rotation lock."""


@contextmanager
def rotation_lock(engine: Engine) -> Iterator[None]:
    """Session-level advisory lock held on a dedicated connection."""
    with engine.connect() as conn:
        got = conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": LOCK_KEY}).scalar()
        conn.commit()
        if not got:
            raise RotationLockedError("another key rotation is running")
        try:
            yield
        finally:
            conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": LOCK_KEY})
            conn.commit()


def _rewrap_blobs(
    engine: Engine,
    keyring: Keyring,
    to_version: int,
    batch_size: int,
    result: TargetResult,
    progress: Progress,
) -> None:
    blob = _table("blob")
    for scope in _scopes(engine, "blob"):
        after = uuid.UUID(int=0)
        while True:
            with _scoped(engine, scope) as conn:
                rows = conn.execute(
                    select(blob.c.id, blob.c.key_version, blob.c.wrap_nonce, blob.c.wrapped_dek)
                    .where(
                        blob.c.wrapped_dek.is_not(None),
                        blob.c.key_version != to_version,
                        blob.c.id > after,
                    )
                    .order_by(blob.c.id)
                    .limit(batch_size)
                    .with_for_update()
                ).all()
                if not rows:
                    break
                done = 0
                for row in rows:
                    after = row.id
                    wrapped = WrappedKey(
                        int(row.key_version), bytes(row.wrap_nonce), bytes(row.wrapped_dek)
                    )
                    try:
                        new = keyring.rewrap_data_key(
                            wrapped, ad=column_ad("blob", "data", row.id), to_version=to_version
                        )
                    except DecryptionError:
                        result.failed += 1
                        result.failed_ids.append(str(row.id))
                        continue
                    changed = conn.execute(
                        update(blob)
                        .where(blob.c.id == row.id, blob.c.key_version == row.key_version)
                        .values(
                            key_version=new.key_version,
                            wrap_nonce=new.wrap_nonce,
                            wrapped_dek=new.wrapped_dek,
                        )
                    ).rowcount
                    done += changed
            result.rewrapped += done
            progress(f"{BLOB_TARGET}: {result.rewrapped} rewrapped")


def _rewrap_column(
    engine: Engine,
    keyring: Keyring,
    target: EncryptedColumn,
    to_version: int,
    batch_size: int,
    result: TargetResult,
    progress: Progress,
) -> None:
    table = _table(target.table)
    col = table.c[target.column]
    marker = version_bytes(to_version)
    for scope in _scopes(engine, target.table):
        after = uuid.UUID(int=0)
        while True:
            with _scoped(engine, scope) as conn:
                rows = conn.execute(
                    select(table.c.id, col)
                    .where(
                        col.is_not(None),
                        func.substring(col, 5, 4) != marker,
                        table.c.id > after,
                    )
                    .order_by(table.c.id)
                    .limit(batch_size)
                    .with_for_update()
                ).all()
                if not rows:
                    break
                done = 0
                for row_id, value in rows:
                    after = row_id
                    old = bytes(value)
                    try:
                        envelope = Envelope.from_bytes(old)
                        new = keyring.rewrap(
                            envelope,
                            ad=column_ad(target.table, target.column, row_id),
                            to_version=to_version,
                        )
                    except DecryptionError:
                        result.failed += 1
                        result.failed_ids.append(str(row_id))
                        continue
                    done += conn.execute(
                        update(table)
                        .where(table.c.id == row_id, col == old)
                        .values({target.column: bytes(new)})
                    ).rowcount
            result.rewrapped += done
            progress(f"{target.name}: {result.rewrapped} rewrapped")


def verify(engine: Engine, keyring: Keyring, progress: Progress) -> tuple[int, int, list[str]]:
    """Unwrap every DEK (and decrypt every envelope) with ``keyring``.

    Returns ``(verified, failed, sample of failing "target:id")``.
    """
    verified = failed = 0
    bad: list[str] = []
    blob = _table("blob")
    for scope in _scopes(engine, "blob"):
        with _scoped(engine, scope) as conn:
            result = conn.execution_options(yield_per=1000).execute(
                select(blob.c.id, blob.c.key_version, blob.c.wrap_nonce, blob.c.wrapped_dek)
                .where(blob.c.wrapped_dek.is_not(None))
                .order_by(blob.c.id)
            )
            for row in result:
                try:
                    keyring.unwrap_data_key(
                        WrappedKey(
                            int(row.key_version), bytes(row.wrap_nonce), bytes(row.wrapped_dek)
                        ),
                        ad=column_ad("blob", "data", row.id),
                    )
                    verified += 1
                except DecryptionError:
                    failed += 1
                    bad.append(f"{BLOB_TARGET}:{row.id}")
    progress(f"verify: {BLOB_TARGET} done ({verified} ok, {failed} failed)")
    for target in ENVELOPE_COLUMNS:
        table = _table(target.table)
        col = table.c[target.column]
        for scope in _scopes(engine, target.table):
            with _scoped(engine, scope) as conn:
                values = conn.execution_options(yield_per=1000).execute(
                    select(table.c.id, col).where(col.is_not(None)).order_by(table.c.id)
                )
                for row_id, value in values:
                    try:
                        keyring.decrypt_bytes(
                            bytes(value), ad=column_ad(target.table, target.column, row_id)
                        )
                        verified += 1
                    except DecryptionError:
                        failed += 1
                        bad.append(f"{target.name}:{row_id}")
        progress(f"verify: {target.name} done")
    return verified, failed, bad[:20]


def rotate(
    engine: Engine,
    *,
    old_key: bytes,
    old_version: int,
    new_key: bytes,
    new_version: int,
    batch_size: int = DEFAULT_BATCH_SIZE,
    progress: Progress | None = None,
) -> RotationReport:
    """Rewrap every DEK from ``old_version`` to ``new_version``, then verify.

    Appends ``instance.master_key_rotated`` only when every DEK unwraps with
    the new key alone. Safe to run again after an interruption.
    """
    if old_version == new_version:
        raise ValueError("the new key needs a different version than the current key")
    if old_key == new_key:
        raise ValueError("the new key is the same as the current key")
    if batch_size < 1:
        raise ValueError("batch size must be at least 1")
    say: Progress = progress or (lambda _msg: None)
    both = Keyring({old_version: old_key, new_version: new_key}, current=new_version)
    new_only = Keyring.from_master_key(new_key, version=new_version)
    report = RotationReport(
        from_version=old_version,
        to_version=new_version,
        from_key_id=both.key_id(old_version),
        to_key_id=both.key_id(new_version),
        results={name: TargetResult() for name in targets()},
    )
    with rotation_lock(engine):
        before = versions_by_target(engine)
        pending = sum(
            n for counts in before.values() for v, n in counts.items() if v != new_version
        )
        say(f"{pending} data key(s) to rewrap from key {report.from_key_id} to {report.to_key_id}")
        _rewrap_blobs(engine, both, new_version, batch_size, report.results[BLOB_TARGET], say)
        for target in ENVELOPE_COLUMNS:
            _rewrap_column(
                engine, both, target, new_version, batch_size, report.results[target.name], say
            )
        report.verified, report.verify_failed, bad = verify(engine, new_only, say)
        for item in bad:
            say(f"verify failed: {item}")
        report.remaining = {
            name: {v: n for v, n in counts.items() if n}
            for name, counts in versions_by_target(engine).items()
        }
        if report.ok:
            with engine.begin() as conn:
                set_context(conn, None, system_actor=True)
                audit(
                    conn,
                    action="instance.master_key_rotated",
                    target_type="master_key",
                    target_id=report.to_key_id,
                    before={"key_version": old_version, "key_id": report.from_key_id},
                    after={
                        "key_version": new_version,
                        "key_id": report.to_key_id,
                        # Totals only: per-column names such as
                        # "blob.wrapped_dek" are redacted keys in audit state.
                        "rewrapped_total": sum(r.rewrapped for r in report.results.values()),
                        "verified": report.verified,
                    },
                )
    return report
