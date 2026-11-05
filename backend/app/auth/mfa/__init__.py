# SPDX-License-Identifier: AGPL-3.0-or-later
"""Second factors (AUTH.md "Multi-factor", SECURITY.md A3/A6/A7/A8).

* :mod:`app.auth.mfa.models`: ``mfa_challenge``, short-lived server-side
  state bound to one session (WebAuthn challenges, TOTP enrolments awaiting
  their first code).
* :mod:`app.auth.mfa.challenges`: issue and single-use consumption of those.
* :mod:`app.auth.mfa.totp`: RFC 6238 enrolment and verification with replay
  protection (A7).
* :mod:`app.auth.mfa.webauthn`: WebAuthn / passkey registration and
  assertions (py_webauthn).
* :mod:`app.auth.mfa.recovery`: ten single-use recovery codes (A8).
* :mod:`app.auth.mfa.stepup`: verifying any factor for the login's second
  step and for step-up, with rate limits, lockout and audit.
* :mod:`app.auth.mfa.policy`: what the login flows call: the stage a new
  session starts in, and OIDC ``amr`` claims as satisfied MFA.

The HTTP surface is ``app/api/routers/auth_mfa.py``.
"""
