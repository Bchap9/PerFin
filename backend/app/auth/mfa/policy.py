# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the login flows ask of the MFA layer (AUTH.md "Multi-factor", "OIDC").

The local login (``POST /auth/login``) and the OIDC callback call these after
the first factor succeeded:

1. ``idp_mfa = idp_asserted_mfa(id_token_amr, trust_idp_mfa=settings.oidc_trust_idp_mfa)``
   (OIDC only; local logins pass ``False``).
2. ``stage = login_stage(db, user.id, idp_mfa=idp_mfa)``:
   ``mfa_pending`` when the user has a second factor (TOTP or WebAuthn) that
   the login has not yet satisfied, else ``authenticated``.
3. ``issued = app.auth.sessions.create_session(..., stage=stage)``.
4. OIDC only: ``mark_mfa_from_idp(db, issued.row, amr, auth_time=...,
   step_up_from=..., settings=settings)`` stamps ``mfa_verified_at`` on a
   fresh *authenticated* session when the IdP asserted MFA and the admin
   trusts it. The IdP's MFA is a fresh **step-up** only when the login was
   started with ``reauth=true`` (``prompt=login&max_age=0``) and the ID
   token's ``auth_time`` is not older than that start (``step_up_from``,
   the start time minus the clock skew): the stamp is then
   ``min(now, auth_time)``. Every other trusted-MFA login (a plain login, a
   silent SSO re-login, an IdP that ignored the reauth request, an ID token
   without a usable ``auth_time``) satisfies MFA for the login but is dated
   at least just outside the step-up window, so PerFin never relies on the
   IdP dating ``auth_time`` correctly on an SSO answer (A6, reviews
   P1-SR2-01 and P1-SR3-01).

A pending session is completed with ``POST /auth/mfa/verify``
(``app.auth.mfa.stepup.verify_second_factor`` +
``app.auth.sessions.complete_mfa``, which rotates the id, A3).

With ``PERFIN_REQUIRE_MFA`` a user who has no second factor and whose login
did not satisfy MFA through the IdP gets an ``authenticated`` session that
``app.auth.principal`` marks *restricted*: only MFA enrolment works until a
factor is added (``app.auth.authz.require_mfa_if_enforced``).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session

from app.auth.models import SessionStage, UserSession
from app.auth.principal import has_primary_mfa
from app.core import clock
from app.core.settings import Settings

#: RFC 8176 ``amr`` values by factor category.
KNOWLEDGE: Final = frozenset({"pwd", "pin", "kba"})
POSSESSION: Final = frozenset({"hwk", "swk", "otp", "sms", "tel", "sc"})
INHERENCE: Final = frozenset({"fpt", "face", "iris", "retina", "vbm"})
#: ``mfa`` is the IdP's own statement that several factors were used.
MFA_VALUE: Final = "mfa"
#: How far past ``app.auth.authz.STEP_UP_WINDOW`` an IdP MFA that is no
#: step-up is dated: it satisfies MFA for the login but never a step-up.
NO_STEP_UP_MARGIN: Final = timedelta(microseconds=1)


def amr_indicates_mfa(amr: Iterable[str] | None) -> bool:
    """True when ``amr`` says ``mfa`` or names factors of two categories."""
    if amr is None or isinstance(amr, str | bytes):
        return False
    values = {v.strip().lower() for v in amr if isinstance(v, str)}
    if MFA_VALUE in values:
        return True
    categories = sum(1 for group in (KNOWLEDGE, POSSESSION, INHERENCE) if values & group)
    return categories >= 2


def idp_asserted_mfa(amr: Iterable[str] | None, *, trust_idp_mfa: bool) -> bool:
    """The IdP's ``amr`` counts as satisfied MFA (only if the admin trusts it)."""
    return trust_idp_mfa and amr_indicates_mfa(amr)


def login_stage(db: Session, user_id: uuid.UUID, *, idp_mfa: bool = False) -> SessionStage:
    """The stage a new session starts in after the first factor succeeded."""
    if idp_mfa:
        return SessionStage.AUTHENTICATED
    if has_primary_mfa(db, user_id):
        return SessionStage.MFA_PENDING
    return SessionStage.AUTHENTICATED


def idp_mfa_verified_at(
    auth_time: datetime | None, now: datetime, *, step_up_from: datetime | None = None
) -> datetime:
    """The ``mfa_verified_at`` an IdP MFA earns.

    ``step_up_from`` is set only for a login started with ``reauth=true``:
    the time the login started, minus the allowed clock skew. When the ID
    token's ``auth_time`` is at or after it, the IdP authenticated the user
    during this very login, and the stamp is ``min(now, auth_time)`` (an
    ``auth_time`` in the future never makes it fresher than now), a fresh
    step-up. In every other case the stamp is ``min(auth_time, now -
    STEP_UP_WINDOW - 1 µs)`` (or the latter without ``auth_time``): MFA for
    the login, never a step-up, whatever ``auth_time`` the IdP put on an SSO
    answer (review P1-SR3-01).
    """
    # Imported here: app.auth.authz -> deps -> local -> this module.
    from app.auth.authz import STEP_UP_WINDOW

    outside = now - STEP_UP_WINDOW - NO_STEP_UP_MARGIN
    if auth_time is None:
        return outside
    if step_up_from is not None and auth_time >= step_up_from:
        return min(now, auth_time)
    return min(outside, auth_time)


def mark_mfa_from_idp(
    db: Session,
    row: UserSession,
    amr: Iterable[str] | None,
    *,
    auth_time: datetime | None,
    settings: Settings,
    step_up_from: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """Stamp ``mfa_verified_at`` on a fresh OIDC session when the IdP did MFA.

    Call it right after ``create_session`` (which drew a fresh id, so no
    rotation is needed) and only for an ``authenticated`` session. Returns
    whether the session now counts as MFA-verified. Whatever the IdP says,
    ``PERFIN_OIDC_TRUST_IDP_MFA=false`` (the default) makes this a no-op.
    The stamp is :func:`idp_mfa_verified_at` of the ID token's ``auth_time``
    and ``step_up_from`` (``None``, the default, for any login not started
    with ``reauth=true``): only a reauth login whose ``auth_time`` is not
    older than its start opens the step-up window
    (``app.auth.authz.require_step_up``), and the window then runs from the
    IdP's authentication.
    """
    if row.stage is not SessionStage.AUTHENTICATED:
        raise ValueError("create the session as authenticated (see login_stage) first")
    if not idp_asserted_mfa(amr, trust_idp_mfa=settings.oidc_trust_idp_mfa):
        return False
    now = now or clock.utcnow()
    row.mfa_verified_at = idp_mfa_verified_at(auth_time, now, step_up_from=step_up_from)
    row.updated_at = now
    db.flush()
    return True
