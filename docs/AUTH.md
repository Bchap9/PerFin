# Authentication and authorisation

Two login methods are first-class: a local username/password account and an
OpenID Connect identity from Authentik (or any compliant provider). An instance
can run with both, or be locked to OIDC only.

## Local accounts

### Passwords
- Hashed with **Argon2id** (memory 64 MiB, iterations 3, parallelism 4 as the
  baseline; parameters stored alongside the hash so they can be raised later
  and rehashed on next successful login).
- Minimum length 12, maximum 128, no composition rules. Checked against a
  local copy of a breached-password list (k-anonymity range check against
  HIBP is opt-in because it is an off-host call).
- Constant-time comparison; a login attempt against an unknown username runs
  a dummy hash so timing does not reveal account existence.

### Multi-factor
- **WebAuthn / passkeys** are the recommended second factor (and can be the
  only factor for passwordless login later). Resident keys supported.
- **TOTP** (RFC 6238) supported with encrypted secret storage and replay
  protection (a code is accepted once).
- **Recovery codes**: ten single-use codes, hashed at rest, shown once.
- MFA can be required instance-wide by the admin.
- Step-up: sensitive actions (change password, add MFA method, create API
  token, change OIDC settings, export everything, purge) require a fresh MFA
  verification within the last 10 minutes.

### Sessions
- Server-side sessions. The cookie carries only a random 256-bit session id;
  the database stores its SHA-256. No JWTs for browser sessions (ADR-0005).
- Cookie flags: `HttpOnly; Secure; SameSite=Lax; Path=/`, with the `__Host-`
  prefix so it is bound to the origin and cannot be set by subdomains.
- Idle timeout 30 minutes (configurable), absolute lifetime 12 hours
  (configurable), "remember this device" extends absolute lifetime to 30 days
  but still requires MFA re-verification for step-up actions.
- Session id rotates on login, on privilege change and on MFA completion
  (prevents fixation).
- Users can list and revoke their sessions. Password change revokes all other
  sessions.

### CSRF
Double-submit token bound to the session, sent in an `X-CSRF-Token` header for
every state-changing request. `SameSite=Lax` is defence in depth, not the
control. The `Origin`/`Sec-Fetch-Site` headers are additionally checked.

### Brute force and enumeration
- Per-account and per-IP sliding-window rate limits on login, MFA, password
  reset and OIDC callback.
- Progressive delay then temporary lock after N failures; lock notifies the
  user via configured channel.
- Identical responses for "no such user" and "wrong password".
- Password reset is only available if an SMTP channel is configured; tokens
  are single-use, 15-minute, hashed at rest, and the response never confirms
  whether the email exists.

## OIDC (Authentik)

### Flow
Authorization Code with **PKCE (S256)**. No implicit, no hybrid, no resource
owner password flow. Discovery via `.well-known/openid-configuration`; the
issuer URL is admin-configured and the document's `issuer` must match it
exactly.

1. User clicks "Sign in with Authentik". API generates `state`, `nonce` and
   PKCE verifier, stores them in a short-lived pre-auth session cookie (5 min),
   redirects to the authorization endpoint with `scope=openid profile email
   groups`.
2. Authentik redirects back to `/api/v1/auth/oidc/callback` with `code` and
   `state`.
3. API verifies `state`, exchanges the code (back-channel, client secret or
   private_key_jwt), validates the ID token: signature against cached JWKS
   (with key rotation handling), `iss`, `aud`, `exp`, `iat` skew, `nonce`,
   and `at_hash` if present.
4. User lookup by `(issuer, sub)`. If none:
   - If a local user exists with the same **verified** email and the admin has
     enabled "link by verified email", prompt the user to confirm linking by
     authenticating locally (prevents account takeover through an IdP that
     allows email changes).
   - Else, if just-in-time provisioning is enabled, create the user; else
     reject with a friendly message.
5. Map claims: `groups` -> household role via admin-configured mapping
   (e.g. `perfin-admins` -> admin, `perfin-users` -> member, `perfin-viewers`
   -> viewer). A user in no mapped group is denied if "require group" is on.
6. Create a normal server-side session. The ID/access tokens are **not** kept
   beyond the request except the refresh token if back-channel logout or
   claim refresh is enabled (stored encrypted).

### Logout
RP-initiated logout to Authentik's `end_session_endpoint` with
`id_token_hint` and `post_logout_redirect_uri` from an allowlist. Back-channel
logout (`logout_token`) supported so a session killed in Authentik dies in
PerFin too.

### Hardening specifics
- Redirect URI is fixed in configuration; never taken from the request.
- The post-login "return to" URL is validated as a same-origin relative path
  (no open redirect).
- Discovery and JWKS fetches go through an egress allowlist containing only
  the issuer host; responses are cached with a maximum lifetime; the HTTP
  client refuses redirects to other hosts and private address ranges (SSRF).
- Clock skew tolerance 60 s.
- OIDC-only mode disables the local password form and the local password
  reset, but keeps a **break-glass local admin** that is only usable from an
  allowlisted CIDR (default: loopback) so an IdP outage does not lock the
  owner out.
- Authentik-specific docs will show the provider setup, the group mapping, and
  how to enforce MFA at the IdP so PerFin can treat `amr` claims as satisfied
  MFA.

## Authorisation model

```
instance admin  -> manages users, OIDC config, system settings, backups
household owner -> everything in the household, can delete the household
household admin -> everything except delete and member removal of the owner
member          -> read/write on accounts granted to them, own documents, review queue
viewer          -> read-only on granted accounts; no documents download by default
```

- Every query is scoped by `household_id` in the application **and** by a
  Postgres RLS policy keyed on a session variable set per request. A bug that
  forgets a `WHERE` clause returns nothing rather than someone else's data.
- Object-level checks use the `(household, account_grant)` pair; IDs are
  UUIDv7 so they are unguessable but authorisation never relies on that.
- API tokens carry explicit scopes (`documents:write`, `transactions:read`,
  ...) and inherit the user's household grants, never more.

## Admin bootstrap

First boot with no users: the API prints a one-time setup URL with a random
token to the log (and to a file in the data volume readable only by the
container user). Visiting it creates the first instance admin. The route
disables itself permanently afterwards. No default credentials, ever.
