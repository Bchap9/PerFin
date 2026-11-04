# SPDX-License-Identifier: AGPL-3.0-or-later
"""Operator commands: ``rotate-key``, ``backup-encrypt``, ``backup-decrypt``, ``backup-info``,
``restore-check``.

Registered on the CLI parser by :func:`register` (``app.cli.build_parser``).
None of these serves traffic, so the serving-role startup gate does not run;
each loads ``Settings`` itself and reports invalid configuration without
echoing values. docs/BACKUP_RESTORE.md is the operator guide.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from pydantic import ValidationError

if TYPE_CHECKING:
    from app.core.settings import Settings

EXIT_OK: Final = 0
EXIT_FAIL: Final = 1

NEW_KEY_ENV: Final = "PERFIN_NEW_MASTER_KEY"
NEW_KEY_FILE_ENV: Final = "PERFIN_NEW_MASTER_KEY_FILE"


def _settings(prog: str) -> Settings | None:
    from app.core.settings import get_settings

    try:
        return get_settings()
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_url=False):
            field = ".".join(str(part) for part in error["loc"])
            print(f"{prog}: ERROR {field}: {error['msg']}", file=sys.stderr)
        return None


def _err(prog: str, message: str) -> int:
    print(f"{prog}: {message}", file=sys.stderr)
    return EXIT_FAIL


# ---------------------------------------------------------------------------
# rotate-key
# ---------------------------------------------------------------------------


class _KeyError(ValueError):
    pass


def _decode_key(raw: str) -> bytes:
    from app.core.settings import MASTER_KEY_BYTES

    try:
        key = base64.b64decode(raw.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise _KeyError("the new master key is not valid base64") from exc
    if len(key) != MASTER_KEY_BYTES:
        raise _KeyError(f"the new master key must decode to {MASTER_KEY_BYTES} bytes")
    return key


def _new_key(args: argparse.Namespace) -> bytes:
    """From ``--new-key-file``, ``PERFIN_NEW_MASTER_KEY_FILE`` or ``PERFIN_NEW_MASTER_KEY``."""
    path = args.new_key_file or os.environ.get(NEW_KEY_FILE_ENV)
    if path:
        try:
            return _decode_key(Path(path).read_text(encoding="ascii"))
        except (OSError, UnicodeDecodeError) as exc:
            raise _KeyError("the new master key file is not readable") from exc
    value = os.environ.get(NEW_KEY_ENV)
    if value:
        return _decode_key(value)
    raise _KeyError(f"no new key: pass --new-key-file or set {NEW_KEY_FILE_ENV} / {NEW_KEY_ENV}")


def cmd_rotate_key(args: argparse.Namespace) -> int:
    """Rewrap every data key under a new master key (SECURITY.md G4).

    ``--status`` prints the configured key's fingerprint and version and how
    many data keys each key version wraps; it exits 1 when any data key is
    not under the configured version (for example after switching the key
    without finishing a rotation). Without ``--status`` it rotates: the
    current key comes from ``PERFIN_MASTER_KEY(_FILE)`` and
    ``PERFIN_MASTER_KEY_VERSION``, the new one from ``--new-key-file`` (or
    ``PERFIN_NEW_MASTER_KEY_FILE`` / ``PERFIN_NEW_MASTER_KEY``), never from
    the command line. Progress goes to stderr, the JSON report to stdout.
    """
    from sqlalchemy import pool
    from sqlalchemy.exc import SQLAlchemyError

    from app.core.crypto import MAX_KEY_VERSION, Keyring
    from app.core.settings import MasterKeyError
    from app.db.session import make_app_engine
    from app.ops import keyrotation

    prog = "perfin rotate-key"
    settings = _settings(prog)
    if settings is None:
        return EXIT_FAIL
    try:
        old_key = settings.load_master_key()
    except MasterKeyError as exc:
        return _err(prog, str(exc))
    old_version = settings.master_key_version
    current = Keyring.from_master_key(old_key, version=old_version)
    engine = make_app_engine(settings, poolclass=pool.NullPool)
    try:
        if args.status:
            counts = keyrotation.versions_by_target(engine)
            stray = {
                name: {str(v): n for v, n in c.items() if v != old_version}
                for name, c in counts.items()
                if any(v != old_version for v in c)
            }
            print(
                json.dumps(
                    {
                        "ok": not stray,
                        "key_id": current.key_id(old_version),
                        "key_version": old_version,
                        "data_keys": {
                            name: {str(v): n for v, n in c.items()} for name, c in counts.items()
                        },
                        "not_under_configured_version": stray,
                    },
                    sort_keys=True,
                )
            )
            if stray:
                print(
                    f"{prog}: some data keys are not under the configured key version "
                    f"{old_version}; finish the rotation (docs/BACKUP_RESTORE.md)",
                    file=sys.stderr,
                )
            return EXIT_OK if not stray else EXIT_FAIL
        try:
            new_key = _new_key(args)
        except _KeyError as exc:
            return _err(prog, str(exc))
        new_version = args.new_version if args.new_version is not None else old_version + 1
        if not 1 <= new_version <= MAX_KEY_VERSION:
            return _err(prog, "--new-version is out of range")
        if args.dry_run:
            counts = keyrotation.versions_by_target(engine)
            pending = {
                name: sum(n for v, n in c.items() if v != new_version) for name, c in counts.items()
            }
            print(json.dumps({"dry_run": True, "to_version": new_version, "pending": pending}))
            return EXIT_OK

        def progress(message: str) -> None:
            print(f"{prog}: {message}", file=sys.stderr, flush=True)

        try:
            report = keyrotation.rotate(
                engine,
                old_key=old_key,
                old_version=old_version,
                new_key=new_key,
                new_version=new_version,
                batch_size=args.batch_size,
                progress=progress,
            )
        except (ValueError, keyrotation.RotationLockedError) as exc:
            return _err(prog, str(exc))
    except SQLAlchemyError as exc:
        return _err(prog, f"database error ({type(exc).__name__}); run it again to resume")
    finally:
        engine.dispose()
    print(json.dumps(report.as_dict(), sort_keys=True))
    if not report.ok:
        return _err(
            prog,
            "rotation incomplete: some data keys did not rewrap or verify; keep the current "
            "key configured, fix the listed rows and run the command again",
        )
    print(
        f"{prog}: every data key is wrapped by key {report.to_key_id} (version {new_version}).\n"
        f"  1. Back the NEW key up separately from the data (password manager, offline copy).\n"
        f"  2. Set PERFIN_MASTER_KEY (or PERFIN_MASTER_KEY_FILE) to the new key and\n"
        f"     PERFIN_MASTER_KEY_VERSION={new_version}, then start api and worker.\n"
        f"  3. Run 'perfin rotate-key --status' (exit 0) and take a fresh backup.\n"
        f"  4. Keep the old key as long as you keep backups made before the rotation.",
        file=sys.stderr,
    )
    return EXIT_OK


# ---------------------------------------------------------------------------
# backup-encrypt / backup-decrypt / backup-info / restore-check
# ---------------------------------------------------------------------------


def cmd_backup_encrypt(args: argparse.Namespace) -> int:
    """Encrypt stdin to stdout with the backup passphrase (archive_crypto format)."""
    from app.core.archive_crypto import ArchiveCryptoError
    from app.ops.backup import PassphraseError, encrypt_to, read_passphrase

    prog = "perfin backup-encrypt"
    try:
        passphrase = read_passphrase(args.passphrase_file, for_encryption=True)
        encrypt_to(sys.stdin.buffer, sys.stdout.buffer, passphrase)
    except (PassphraseError, ArchiveCryptoError) as exc:
        return _err(prog, str(exc))
    return EXIT_OK


def cmd_backup_decrypt(args: argparse.Namespace) -> int:
    """Authenticate and decrypt stdin to stdout; exit 1 means discard the output."""
    from app.core.archive_crypto import ArchiveCryptoError
    from app.ops.backup import PassphraseError, decrypt_to, read_passphrase

    prog = "perfin backup-decrypt"
    try:
        passphrase = read_passphrase(args.passphrase_file, for_encryption=False)
        decrypt_to(sys.stdin.buffer, sys.stdout.buffer, passphrase)
    except PassphraseError as exc:
        return _err(prog, str(exc))
    except ArchiveCryptoError as exc:
        return _err(prog, f"{exc}; the output is not usable")
    return EXIT_OK


def cmd_backup_info(_args: argparse.Namespace) -> int:
    """Print the PerFin version and the master key's fingerprint and version (JSON)."""
    from app.core.crypto import Keyring
    from app.core.settings import MasterKeyError
    from app.ops.backup import backup_info

    prog = "perfin backup-info"
    settings = _settings(prog)
    if settings is None:
        return EXIT_FAIL
    try:
        keyring = Keyring.from_settings(settings)
    except MasterKeyError as exc:
        return _err(prog, str(exc))
    print(json.dumps(backup_info(keyring), sort_keys=True))
    return EXIT_OK


def cmd_restore_check(args: argparse.Namespace) -> int:
    """Decrypt stored documents with the master key and compare their SHA-256."""
    from sqlalchemy import pool
    from sqlalchemy.exc import SQLAlchemyError

    from app.core.crypto import Keyring
    from app.core.settings import MasterKeyError
    from app.db.session import make_app_engine
    from app.domain.documents.blobstore import LocalFSBlobStore
    from app.domain.documents.models import Blob
    from app.domain.documents.service import open_plaintext
    from app.ops.backup import restore_check

    prog = "perfin restore-check"
    settings = _settings(prog)
    if settings is None:
        return EXIT_FAIL
    try:
        keyring = Keyring.from_settings(settings)
    except MasterKeyError as exc:
        return _err(prog, str(exc))
    store = LocalFSBlobStore.from_settings(settings)

    def plaintext(blob: Blob) -> Any:
        return open_plaintext(store, keyring, blob)

    engine = make_app_engine(settings, poolclass=pool.NullPool)
    try:
        report = restore_check(engine, plaintext, sample=None if args.all else args.sample)
    except SQLAlchemyError as exc:
        return _err(prog, f"database error ({type(exc).__name__})")
    finally:
        engine.dispose()
    print(json.dumps(report.as_dict(), sort_keys=True))
    if not report.ok:
        return _err(prog, "some documents did not decrypt or did not match their SHA-256")
    return EXIT_OK


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def register(sub: Any) -> None:
    """Add the operator commands to the ``perfin`` sub-parser collection."""
    from app.ops.defaults import DEFAULT_BATCH_SIZE, DEFAULT_SAMPLE

    rotate = sub.add_parser("rotate-key", help="rewrap every data key under a new master key (G4)")
    rotate.add_argument(
        "--status",
        action="store_true",
        help="show the configured key and data keys per key version; exit 1 on a mismatch",
    )
    rotate.add_argument(
        "--new-key-file",
        help=f"file holding the new base64 key (or set {NEW_KEY_FILE_ENV} / {NEW_KEY_ENV})",
    )
    rotate.add_argument(
        "--new-version",
        type=_positive,
        help="key version of the new key (default: PERFIN_MASTER_KEY_VERSION + 1)",
    )
    rotate.add_argument(
        "--batch-size",
        type=_positive,
        default=DEFAULT_BATCH_SIZE,
        help=f"data keys per committed batch (default {DEFAULT_BATCH_SIZE})",
    )
    rotate.add_argument("--dry-run", action="store_true", help="only count what would change")
    rotate.set_defaults(func=cmd_rotate_key)

    for name, func, text in (
        ("backup-encrypt", cmd_backup_encrypt, "encrypt stdin to stdout with a passphrase"),
        ("backup-decrypt", cmd_backup_decrypt, "authenticate and decrypt stdin to stdout"),
    ):
        command = sub.add_parser(name, help=text)
        command.add_argument(
            "--passphrase-file",
            help="file holding the passphrase (default: env PERFIN_BACKUP_PASSPHRASE)",
        )
        command.set_defaults(func=func)
    sub.add_parser(
        "backup-info", help="print the version and the master key fingerprint (JSON)"
    ).set_defaults(func=cmd_backup_info)
    check = sub.add_parser(
        "restore-check", help="decrypt stored documents and compare their SHA-256"
    )
    check.add_argument(
        "--sample",
        type=_positive,
        default=DEFAULT_SAMPLE,
        help=f"random blobs to check (default {DEFAULT_SAMPLE})",
    )
    check.add_argument("--all", action="store_true", help="check every blob")
    check.set_defaults(func=cmd_restore_check)
