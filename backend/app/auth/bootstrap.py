# SPDX-License-Identifier: AGPL-3.0-or-later
"""One-time admin bootstrap (AUTH.md "Admin bootstrap", SECURITY.md A10).

There are no default credentials. While no user exists:

* ``perfin api`` (:func:`announce_on_startup`) and ``perfin bootstrap-url``
  (:func:`issue_setup_token`) draw a random setup token (256 bits, letters
  only so the log scrubber leaves it intact), store **only its SHA-256** in
  the system setting ``bootstrap.setup_token`` (with an expiry of
  :data:`TOKEN_TTL`) and publish ``<PERFIN_PUBLIC_URL>/setup#t=<token>``:
  one WARNING log line (the only log line that carries a secret; it is single
  use and dies with the first user) and a file at
  ``PERFIN_BOOTSTRAP_URL_FILE`` created with mode 0600 for the container
  user. The token sits in the URL fragment, so it never reaches a server
  log or a ``Referer``; the setup page posts it in the body. Issuing a new
  token replaces the previous one.
* ``POST /api/v1/setup`` (:func:`complete_setup`) checks the token in
  constant time, then creates the first user (instance admin, local
  password checked like any new password), a household (the user is its
  owner) and a session. It runs under a transaction-scoped advisory lock and
  re-checks "no user exists and setup not completed" inside it, so of two
  racing requests exactly one succeeds. Completion records the system
  setting ``bootstrap.completed`` and deletes the token; from then on the
  route answers ``404`` for good, even if every user is later deleted
  (``perfin bootstrap-url`` refuses too).

System settings are writable only in an instance-admin RLS context; this
module sets that context only for its own writes.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import secrets
import string
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final

from sqlalchemy import Engine, delete, select, text
from sqlalchemy.orm import Session

from app.auth.models import (
    Credential,
    HouseholdMember,
    HouseholdRole,
    Identity,
    IdentityProvider,
    User,
)
from app.auth.passwords import DEFAULT_PARAMS, hash_password
from app.core import clock
from app.core.ids import uuid7
from app.core.logging import get_logger
from app.core.settings import Settings
from app.db.models import Household, Setting, SettingScope
from app.db.rls import get_context, restore_context, set_context
from app.db.session import make_app_engine, make_sessionmaker

log = get_logger("perfin.bootstrap")

TOKEN_KEY: Final = "bootstrap.setup_token"  # noqa: S105 - a setting key, not a secret
COMPLETED_KEY: Final = "bootstrap.completed"
TOKEN_TTL: Final = timedelta(hours=24)
TOKEN_LENGTH: Final = 45  # 45 * log2(52) > 256 bits
_TOKEN_ALPHABET: Final = string.ascii_letters
_LOCK_SQL: Final = text("SELECT pg_advisory_xact_lock(hashtextextended('perfin:bootstrap', 0))")
SETUP_PATH: Final = "/setup"


class SetupUnavailable(Exception):  # noqa: N818 - an outcome
    """Setup already happened (or a user exists): the route is gone."""


class InvalidSetupToken(Exception):  # noqa: N818 - an outcome
    """The token is wrong, expired or was never issued."""


def new_setup_token() -> str:
    """A random 45-letter token (no digits: the log scrubber keeps it whole)."""
    return "".join(secrets.choice(_TOKEN_ALPHABET) for _ in range(TOKEN_LENGTH))


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def setup_url(settings: Settings, token: str) -> str:
    """``<public url>/setup#t=<token>`` (fragment: never sent to a server)."""
    return f"{settings.public_url.rstrip('/')}{SETUP_PATH}#t={token}"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def lock(db: Session) -> None:
    """Serialise bootstrap decisions (transaction-scoped advisory lock)."""
    db.execute(_LOCK_SQL)


def users_exist(db: Session) -> bool:
    return db.execute(select(User.id).limit(1)).first() is not None


def _system_setting(db: Session, key: str, *, for_update: bool = False) -> Setting | None:
    stmt = select(Setting).where(
        Setting.scope == SettingScope.SYSTEM, Setting.scope_id.is_(None), Setting.key == key
    )
    if for_update:
        stmt = stmt.with_for_update()
    return db.execute(stmt).scalar_one_or_none()


def is_completed(db: Session) -> bool:
    return _system_setting(db, COMPLETED_KEY) is not None


def setup_required(db: Session) -> bool:
    """True while setup has never completed and no user exists."""
    return not is_completed(db) and not users_exist(db)


class _AdminContext:
    """Instance-admin RLS context for system-setting writes, then restore."""

    def __init__(self, db: Session, user_id: uuid.UUID | None = None) -> None:
        self.db = db
        self.user_id = user_id

    def __enter__(self) -> None:
        self.saved = get_context(self.db)
        set_context(self.db, None, self.user_id, instance_admin=True)

    def __exit__(self, *_exc: object) -> None:
        restore_context(self.db, self.saved)


def _put_system_setting(
    db: Session, key: str, value: Any, *, user_id: uuid.UUID | None = None
) -> None:
    with _AdminContext(db, user_id):
        row = _system_setting(db, key, for_update=True)
        now = clock.utcnow()
        if row is None:
            db.add(
                Setting(
                    scope=SettingScope.SYSTEM,
                    scope_id=None,
                    key=key,
                    value=value,
                    created_at=now,
                    updated_at=now,
                )
            )
        else:
            row.value = value
            row.updated_at = now
        db.flush()


def _delete_system_setting(db: Session, key: str, *, user_id: uuid.UUID | None = None) -> None:
    with _AdminContext(db, user_id):
        db.execute(
            delete(Setting).where(
                Setting.scope == SettingScope.SYSTEM,
                Setting.scope_id.is_(None),
                Setting.key == key,
            )
        )


# ---------------------------------------------------------------------------
# Issuing the token
# ---------------------------------------------------------------------------


def issue_setup_token(db: Session, *, now: datetime | None = None) -> str | None:
    """Store a fresh token's hash and return the token; ``None`` if setup is done.

    Runs in the caller's transaction (commit to make the token valid).
    """
    now = now or clock.utcnow()
    lock(db)
    if not setup_required(db):
        return None
    token = new_setup_token()
    _put_system_setting(
        db,
        TOKEN_KEY,
        {
            "hash": hash_token(token),
            "created_at": now.isoformat(),
            "expires_at": (now + TOKEN_TTL).isoformat(),
        },
    )
    return token


def write_url_file(path: Path, url: str) -> bool:
    """Write ``url`` to ``path`` with mode 0600 (directory 0700); ``False`` on failure.

    Refuses to follow a symlink at ``path``. Failures are logged without the URL.
    """
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, (url + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except OSError as exc:
        log.warning(
            "could not write the setup URL file",
            extra={"path": str(path), "error": errno.errorcode.get(exc.errno or 0, "OSError")},
        )
        return False
    return True


def remove_url_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        log.warning(
            "could not remove the setup URL file",
            extra={"path": str(path), "error": errno.errorcode.get(exc.errno or 0, "OSError")},
        )


def publish(settings: Settings, token: str) -> str:
    """Log the setup URL at WARNING and write it to the URL file; return the URL."""
    url = setup_url(settings, token)
    expires = (clock.utcnow() + TOKEN_TTL).strftime("%Y-%m-%d %H:%M UTC")
    # The one log line that carries a secret (SECURITY.md A10): single use,
    # dead as soon as the first user exists.
    log.warning(
        "No user exists yet. Open this one-time setup URL to create the first administrator "
        "(valid until %s; `perfin bootstrap-url` prints a new one): %s",
        expires,
        url,
    )
    write_url_file(settings.bootstrap_url_file, url)
    return url


def announce_with_engine(settings: Settings, engine: Engine) -> str | None:
    """Issue and publish a token if setup is still required; return the URL."""
    factory = make_sessionmaker(engine)
    with factory() as db, db.begin():
        token = issue_setup_token(db)
    if token is None:
        remove_url_file(settings.bootstrap_url_file)
        return None
    return publish(settings, token)


def announce_on_startup(settings: Settings) -> str | None:
    """Called by ``perfin api`` before serving."""
    engine = make_app_engine(settings)
    try:
        return announce_with_engine(settings, engine)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Completing setup
# ---------------------------------------------------------------------------


def _token_valid(row: Setting | None, token: str, now: datetime) -> bool:
    candidate = hash_token(token)
    value = row.value if row is not None and isinstance(row.value, dict) else {}
    stored = value.get("hash")
    expires_raw = value.get("expires_at")
    # Compare even when nothing is stored, so timing does not reveal that.
    expected = stored if isinstance(stored, str) and len(stored) == 64 else "0" * 64
    matches = hmac.compare_digest(candidate.encode("ascii"), expected.encode("ascii"))
    if not matches or stored is None or not isinstance(expires_raw, str):
        return False
    try:
        expires_at = datetime.fromisoformat(expires_raw)
    except ValueError:
        return False
    return now < expires_at


@dataclass(frozen=True)
class SetupResult:
    user: User
    household: Household


def check_token(db: Session, token: str, *, now: datetime | None = None) -> None:
    """Lock, then raise unless setup is open and ``token`` is the current one."""
    now = now or clock.utcnow()
    lock(db)
    if not setup_required(db):
        raise SetupUnavailable
    if not _token_valid(_system_setting(db, TOKEN_KEY), token, now):
        raise InvalidSetupToken


def complete_setup(
    db: Session,
    *,
    token: str,
    username: str,
    password: str,
    display_name: str,
    household_name: str,
    base_currency: str,
    now: datetime | None = None,
) -> SetupResult:
    """Create the first instance admin, their household and membership.

    The caller has validated the password policy; this re-checks the token
    under the lock (race-safe) and marks setup completed. Runs in the
    caller's transaction.
    """
    now = now or clock.utcnow()
    check_token(db, token, now=now)
    user = User(
        username=username,
        display_name=display_name,
        is_instance_admin=True,
        created_at=now,
        updated_at=now,
    )
    db.add(user)
    db.flush()
    hashed = hash_password(password, DEFAULT_PARAMS)
    db.add(
        Credential(
            user_id=user.id,
            password_hash=hashed.encoded,
            hash_params=hashed.params_dict(),
            rotated_at=now,
        )
    )
    db.add(
        Identity(
            user_id=user.id,
            provider=IdentityProvider.LOCAL,
            issuer="local",
            subject=str(user.id),
        )
    )
    db.flush()
    household_id = uuid7()
    saved = get_context(db)
    # A household is inserted in the context of its own new id.
    set_context(db, household_id, user.id)
    household = Household(id=household_id, name=household_name, base_currency=base_currency)
    db.add(household)
    db.flush()
    db.add(HouseholdMember(household_id=household_id, user_id=user.id, role=HouseholdRole.OWNER))
    db.flush()
    restore_context(db, saved)
    _put_system_setting(
        db,
        COMPLETED_KEY,
        {"completed_at": now.isoformat(), "user_id": str(user.id)},
        user_id=user.id,
    )
    _delete_system_setting(db, TOKEN_KEY, user_id=user.id)
    return SetupResult(user=user, household=household)
