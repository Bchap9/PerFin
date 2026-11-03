# ADR-0005: Server-side sessions, not JWTs, for browser authentication

Status: Accepted. Date: 2026-11-03.

## Context
SPAs are often paired with JWT bearer tokens stored in local storage. That
pattern makes tokens readable by any XSS, impossible to revoke before expiry,
and prone to library misconfiguration (`alg=none`, key confusion).

## Decision
Browser authentication uses an opaque, random session identifier in a
`__Host-` prefixed `HttpOnly; Secure; SameSite=Lax` cookie, backed by a
`session` table. CSRF is handled with a double-submit token plus
`Sec-Fetch-Site`/`Origin` checks. Sessions are revocable, listable, rotated on
privilege change and expire on idle and absolute timers.

Programmatic access uses opaque personal access tokens (hashed at rest, scoped,
expiring), also not JWTs.

OIDC ID tokens from Authentik are validated and then discarded; they are never
used as PerFin's session credential.

## Alternatives considered
- **JWT in localStorage.** Rejected: XSS exposure, no revocation.
- **JWT in HttpOnly cookie.** Revocation still requires a server-side
  denylist, at which point it is a session store with extra parsing risk.
- **Using Authentik's forward-auth as the only auth.** Excellent as an
  additional layer and documented as an option, but PerFin must have its own
  identity model for per-user grants and local-login deployments.

## Consequences
- One database round trip per request for session lookup (cached briefly).
- Horizontal scaling needs a shared database, which it has.
