# SPDX-License-Identifier: AGPL-3.0-or-later
"""User administration (instance admin) and user preferences.

* :func:`create_local_user`: a local account (username, display name,
  optional email, password under the normal policy including the breached
  list). Never an instance admin, never break-glass: those flags are only
  set by explicit instance-admin actions on the local database (B6/B7).
  Used by ``POST /admin/users`` and by inviting a new member.
* :func:`update_user`: activate/deactivate, the break-glass flag and the
  email-verified flag. Deactivation revokes every session and personal access
  token of the user. Nobody changes their own account here (no self-lockout).
* :func:`set_breakglass`: the break-glass flag itself, shared with the
  ``perfin breakglass`` command (which has no acting user). Changing the flag
  ends every session of the account: sessions that predate the flag were not
  created as break-glass logins, so the per-request network check (B7) would
  not apply to them. It also revokes the account's personal access tokens:
  a token carries no network check at all, and break-glass accounts may not
  hold one (``app.auth.tokens``).
* :func:`set_email_verified`: an instance admin vouches for (or withdraws)
  the email address of a local account. Linking an OIDC identity by verified
  email (B3) requires it; nothing else marks a local address verified.
* :func:`reset_mfa`: deletes every factor (TOTP, WebAuthn, recovery codes)
  of another user and revokes their sessions, so the next login enrols anew.
  The router requires step-up MFA from the instance admin.
* :func:`get_preferences` / :func:`update_preferences`: display name (on
  ``app_user``) plus locale, theme and time zone (the ``user.preferences``
  user-scoped setting, readable and writable only in that user's RLS
  context).

Every change is audited at instance level (``household_id`` NULL) without
passwords or factor material.
"""

from __future__ import annotations

import contextlib
import uuid
from typing import TYPE_CHECKING, Any, Final, Literal

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.local import set_password, system_actor, validate_new_password, weak_password
from app.auth.mfa.stepup import notify_households
from app.auth.models import Identity, IdentityProvider, User
from app.auth.passwords import PasswordPolicyError
from app.auth.principal import Principal, memberships
from app.auth.sessions import revoke_all_sessions
from app.core import clock
from app.core.audit import RequestMeta, audit
from app.core.errors import ProblemError
from app.core.settings import Settings
from app.db.rls import get_context, restore_context, set_context
from app.domain.households import repo
from app.domain.households.schemas import (
    DEFAULT_LOCALE,
    DEFAULT_THEME,
    DEFAULT_TIMEZONE,
    AdminUserOut,
    AdminUserPage,
    AdminUserUpdate,
    DashboardEntry,
    NewLocalUser,
    UserPreferencesOut,
    UserPreferencesUpdate,
    clean_locale,
    clean_timezone,
    unique_widgets,
)

if TYPE_CHECKING:
    from starlette.requests import HTTPConnection

PREFERENCES_KEY: Final = "user.preferences"
THEMES: Final = ("system", "light", "dark")
MAX_PAGE: Final = 500


def user_not_found() -> ProblemError:
    return ProblemError(404, "user-not-found", "User not found")


def _own_account() -> ProblemError:
    return ProblemError(
        409,
        "own-account",
        "Cannot change your own account here",
        "Another instance admin has to do this; use your own settings for your factors.",
    )


def _meta(request: HTTPConnection | None) -> RequestMeta:
    return RequestMeta.from_request(request) if request is not None else RequestMeta()


def _audit(
    db: Session,
    *,
    action: str,
    actor_id: uuid.UUID | None,
    request: HTTPConnection | None,
    target_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> None:
    """An instance-level audit row (``household_id`` NULL, system-actor context).

    ``actor_id`` is ``None`` for the local operator (``perfin`` commands).
    """
    with system_actor(db, actor_id):
        audit(
            db,
            action=action,
            target_type="user",
            target_id=target_id if target_id is not None else actor_id,
            user_id=actor_id,
            before=before,
            after=after,
            meta=_meta(request),
        )


# ---------------------------------------------------------------------------
# Creating users
# ---------------------------------------------------------------------------


def create_local_user(
    db: Session,
    settings: Settings,
    data: NewLocalUser,
    *,
    actor_id: uuid.UUID,
    request: HTTPConnection | None,
) -> User:
    """A new local account (see module doc). ``409 username-taken`` if it exists."""
    if repo.get_user_by_username(db, data.username) is not None:
        raise _username_taken()
    try:
        validate_new_password(data.password, settings, username=data.username)
    except PasswordPolicyError as exc:
        raise weak_password(exc) from exc
    now = clock.utcnow()
    user = User(
        username=data.username,
        display_name=data.display_name,
        email=data.email,
        email_verified=False,
        is_instance_admin=False,
        is_breakglass=False,
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    try:
        with db.begin_nested():
            db.add(user)
            db.flush()
    except IntegrityError as exc:  # a concurrent request took the name
        raise _username_taken() from exc
    set_password(db, user.id, data.password, now=now)
    db.add(
        Identity(
            user_id=user.id, provider=IdentityProvider.LOCAL, issuer="local", subject=str(user.id)
        )
    )
    db.flush()
    _audit(
        db,
        action="user.create",
        actor_id=actor_id,
        request=request,
        target_id=user.id,
        after={"username": user.username, "has_email": user.email is not None},
    )
    return user


def _username_taken() -> ProblemError:
    return ProblemError(409, "username-taken", "Username taken", "Choose another username.")


# ---------------------------------------------------------------------------
# Listing and changing users
# ---------------------------------------------------------------------------


def admin_view(db: Session, users: list[User]) -> list[AdminUserOut]:
    ids = [u.id for u in users]
    with_password = repo.users_with_password(db, ids)
    factors = repo.mfa_types_by_user(db, ids)
    out: list[AdminUserOut] = []
    for user in users:
        methods: Any = factors.get(user.id, [])
        out.append(
            AdminUserOut(
                id=user.id,
                username=user.username,
                display_name=user.display_name,
                email=user.email,
                email_verified=user.email_verified,
                is_active=user.is_active,
                is_instance_admin=user.is_instance_admin,
                is_breakglass=user.is_breakglass,
                has_password=user.id in with_password,
                mfa_methods=methods,
                locked_until=user.locked_until,
                last_login_at=user.last_login_at,
                created_at=user.created_at,
            )
        )
    return out


def list_users_page(db: Session, *, cursor: uuid.UUID | None, limit: int) -> AdminUserPage:
    """Users ordered by id (creation order, UUIDv7), ``limit`` per page."""
    limit = max(1, min(limit, MAX_PAGE))
    rows = repo.list_users(db, after=cursor, limit=limit + 1)
    page, more = rows[:limit], len(rows) > limit
    return AdminUserPage(items=admin_view(db, page), next_cursor=page[-1].id if more else None)


def get_user_view(db: Session, user_id: uuid.UUID) -> AdminUserOut:
    user = repo.get_user(db, user_id)
    if user is None:
        raise user_not_found()
    return admin_view(db, [user])[0]


def _revoke_all_tokens(db: Session, user_id: uuid.UUID) -> int:
    """Revoke the user's tokens in every household (each in its own context)."""
    saved = get_context(db)
    now = clock.utcnow()
    count = 0
    try:
        for member in memberships(db, user_id):
            set_context(db, member.household_id, user_id)
            count += repo.revoke_household_tokens(db, user_id, member.household_id, now=now)
    finally:
        restore_context(db, saved)
    return count


def update_user(
    db: Session,
    principal: Principal,
    user_id: uuid.UUID,
    data: AdminUserUpdate,
    *,
    request: HTTPConnection | None,
) -> AdminUserOut:
    """Activation, break-glass and email-verified flags of another user (instance admin)."""
    user = repo.get_user(db, user_id, lock=True)
    if user is None:
        raise user_not_found()
    if user.id == principal.user_id:
        raise _own_account()
    if data.is_breakglass is not None and data.is_breakglass != user.is_breakglass:
        set_breakglass(
            db, user, data.is_breakglass, actor_id=principal.user_id, request=request, via="api"
        )
    if data.email_verified is not None and data.email_verified != user.email_verified:
        set_email_verified(
            db, user, data.email_verified, actor_id=principal.user_id, request=request
        )
    if data.is_active is not None and data.is_active != user.is_active:
        user.is_active = data.is_active
        after: dict[str, Any] = {"is_active": data.is_active}
        if not data.is_active:
            after["sessions_revoked"] = revoke_all_sessions(db, user.id, reason="user_deactivated")
            after["pats_revoked"] = _revoke_all_tokens(db, user.id)
        _audit(
            db,
            action="user.activate" if data.is_active else "user.deactivate",
            actor_id=principal.user_id,
            request=request,
            target_id=user.id,
            after=after,
        )
    user.updated_at = clock.utcnow()
    db.flush()
    return admin_view(db, [user])[0]


def set_breakglass(
    db: Session,
    user: User,
    enabled: bool,
    *,
    actor_id: uuid.UUID | None,
    request: HTTPConnection | None,
    via: Literal["api", "cli"],
) -> int:
    """Set or clear the break-glass flag (B7); returns the sessions it ended.

    The caller holds the row lock on ``user``. ``409 no-local-password`` when
    enabling for an account without a local password. Every session and
    personal access token of the account ends, so neither a pre-existing
    session nor a token escapes the break-glass network check, and the change
    is audited (``user.breakglass_set``).
    """
    if enabled and not repo.users_with_password(db, [user.id]):
        raise ProblemError(
            409,
            "no-local-password",
            "No local password",
            "A break-glass account needs a local password.",
        )
    before = user.is_breakglass
    user.is_breakglass = enabled
    user.updated_at = clock.utcnow()
    revoked = revoke_all_sessions(db, user.id, reason="breakglass_changed")
    tokens = _revoke_all_tokens(db, user.id)
    _audit(
        db,
        action="user.breakglass_set",
        actor_id=actor_id,
        request=request,
        target_id=user.id,
        before={"is_breakglass": before},
        after={
            "is_breakglass": enabled,
            "via": via,
            "sessions_revoked": revoked,
            "pats_revoked": tokens,
        },
    )
    db.flush()
    return revoked


def set_email_verified(
    db: Session,
    user: User,
    verified: bool,
    *,
    actor_id: uuid.UUID,
    request: HTTPConnection | None,
) -> None:
    """An instance admin marks a local account's email verified (or not); audited.

    ``409 no-email`` when the account has no email address to vouch for.
    """
    if verified and not (user.email or "").strip():
        raise ProblemError(409, "no-email", "No email address", "The account has no email.")
    user.email_verified = verified
    _audit(
        db,
        action="user.email_verified_set",
        actor_id=actor_id,
        request=request,
        target_id=user.id,
        before={"email_verified": not verified},
        after={"email_verified": verified},
    )


def reset_mfa(
    db: Session, principal: Principal, user_id: uuid.UUID, *, request: HTTPConnection | None
) -> int:
    """Remove every factor of another user and end their sessions; returns factors removed."""
    user = repo.get_user(db, user_id, lock=True)
    if user is None:
        raise user_not_found()
    if user.id == principal.user_id:
        raise _own_account()
    removed = repo.delete_mfa_methods(db, user.id)
    sessions = revoke_all_sessions(db, user.id, reason="mfa_reset")
    _audit(
        db,
        action="user.mfa_reset",
        actor_id=principal.user_id,
        request=request,
        target_id=user.id,
        after={"factors_removed": removed, "sessions_revoked": sessions},
    )
    notify_households(
        db, user.id, "user.mfa_reset", {"user_id": str(user.id), "by": str(principal.user_id)}
    )
    return removed


# ---------------------------------------------------------------------------
# Preferences (``/settings/user``)
# ---------------------------------------------------------------------------


def _stored_preferences(db: Session, user_id: uuid.UUID) -> dict[str, Any]:
    """Stored values that are still valid; anything else falls back to the default."""
    raw = repo.get_user_setting(db, user_id, PREFERENCES_KEY)
    stored: dict[str, Any] = {}
    if not isinstance(raw, dict):
        return stored
    layout = _clean_layout(raw.get("dashboard_layout"))
    if layout is not None:
        stored["dashboard_layout"] = layout
    checks = {"locale": clean_locale, "timezone": clean_timezone}
    for key, check in checks.items():
        value = raw.get(key)
        if isinstance(value, str):
            with contextlib.suppress(ValueError):
                stored[key] = check(value)
    theme = raw.get("theme")
    if isinstance(theme, str) and theme in THEMES:
        stored["theme"] = theme
    return stored


def _clean_layout(raw: object) -> list[dict[str, str]] | None:
    """A stored layout that still validates; anything else counts as never set."""
    if not isinstance(raw, list):
        return None
    try:
        entries = [DashboardEntry.model_validate(item) for item in raw]
        unique_widgets(entries)
    except (ValueError, TypeError):
        return None
    return [{"widget": e.widget, "size": e.size} for e in entries]


def get_preferences(db: Session, principal: Principal) -> UserPreferencesOut:
    """The caller's preferences (``perfin.user_id`` context, set by the principal)."""
    stored = _stored_preferences(db, principal.user_id)
    theme: Any = stored.get("theme", DEFAULT_THEME)
    return UserPreferencesOut(
        display_name=principal.user.display_name,
        locale=stored.get("locale", DEFAULT_LOCALE),
        theme=theme,
        timezone=stored.get("timezone", DEFAULT_TIMEZONE),
        dashboard_layout=stored.get("dashboard_layout"),
    )


def update_preferences(
    db: Session,
    principal: Principal,
    data: UserPreferencesUpdate,
    *,
    request: HTTPConnection | None,
) -> UserPreferencesOut:
    now = clock.utcnow()
    changed: dict[str, Any] = {}
    user = principal.user
    if data.display_name is not None and data.display_name != user.display_name:
        user.display_name = data.display_name
        user.updated_at = now
        changed["display_name"] = data.display_name
        db.flush()
    stored = _stored_preferences(db, principal.user_id)
    updated = dict(stored)
    for key in ("locale", "theme", "timezone"):
        value = getattr(data, key)
        if value is not None and stored.get(key) != value:
            updated[key] = value
            changed[key] = value
    if data.dashboard_layout is not None:
        layout = [{"widget": e.widget, "size": e.size} for e in data.dashboard_layout]
        if stored.get("dashboard_layout") != layout:
            updated["dashboard_layout"] = layout
            changed["dashboard_layout"] = layout
    if updated != stored:
        repo.put_user_setting(db, principal.user_id, PREFERENCES_KEY, updated, now=now)
    if changed:
        _audit(
            db,
            action="user.preferences_update",
            actor_id=principal.user_id,
            request=request,
            after=changed,
        )
    return get_preferences(db, principal)
