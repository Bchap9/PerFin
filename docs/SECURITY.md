# Security model

This document is the threat model and the catalogue of vulnerabilities we
expect in an application of this shape, each paired with the control that
addresses it. It is a living document: every new feature should add a row or
confirm it is covered.

## Assets

1. Financial records (transactions, balances, holdings, loans).
2. Original documents (statements, tax forms, receipts) and the text extracted
   from them.
3. Credentials: user passwords, MFA secrets, sessions, API tokens, IMAP
   credentials, OIDC client secret, integration API keys, the master
   encryption key.
4. Availability of the service (a self-hoster relies on it at tax time).
5. Integrity of the ledger (silently wrong data is worse than no data).

## Attackers

- **Internet opportunist**: scans for exposed self-hosted apps, tries default
  creds, known CVEs, brute force.
- **Targeted remote attacker**: knows the victim, can send email to the
  receipts mailbox, can craft files and hope they get dropped in the folder,
  may control a site the victim visits (CSRF/XSS angle).
- **Malicious document**: a statement or receipt from a compromised merchant or
  a phishing campaign, designed to exploit parsers or inject instructions into
  an AI assistant that reads PerFin data through the MCP server.
- **Compromised dependency**: a package in the supply chain.
- **Another household member**: a legitimate user trying to see accounts they
  were not granted.
- **Someone with the backup**: stolen disk, leaked backup archive.
- **Compromised IdP or IdP admin**: can mint tokens for any email.

Out of scope: a root-level compromise of the host; physical access with the
master key present in memory.

## Vulnerability catalogue and controls

### A. Authentication and sessions

| # | Vulnerability | Control |
| --- | --- | --- |
| A1 | Credential stuffing / brute force | Argon2id, per-account and per-IP rate limits, progressive lockout, MFA, breached-password check |
| A2 | Account enumeration via login/reset responses or timing | Uniform responses, dummy hash on unknown user, uniform reset response |
| A3 | Session fixation | Session id regenerated on login, MFA completion and privilege change |
| A4 | Session theft via XSS or subdomain cookie injection | `HttpOnly`, `__Host-` prefix, strict CSP, no inline scripts |
| A5 | CSRF | Double-submit token bound to session + `SameSite=Lax` + `Sec-Fetch-Site`/`Origin` check |
| A6 | Stolen "remember me" device token | Long sessions still require fresh MFA for step-up; revocable per device |
| A7 | TOTP replay | Last-used counter stored; a code is accepted once |
| A8 | Recovery code brute force | Hashed at rest, rate limited, lockout, notification on use |
| A9 | JWT pitfalls (alg=none, key confusion, long-lived bearer tokens in browser) | No JWTs for browser sessions (ADR-0005); OIDC ID tokens validated with pinned algorithms (`RS256`/`ES256` only) and discarded |
| A10 | Default or bootstrap credentials | None exist; one-time setup token printed to log, route self-disables |
| A11 | Password reset token leakage via Referer | Token in fragment or POST body on a page with `Referrer-Policy: no-referrer`; single use; 15 min |

### B. OIDC integration

| # | Vulnerability | Control |
| --- | --- | --- |
| B1 | Authorization code interception | PKCE S256, confidential client, single-use `state` bound to pre-auth cookie |
| B2 | Token substitution / replay | `nonce` validated, `aud` must equal client id, `iss` must equal configured issuer, `exp`/`iat` checked |
| B3 | Account takeover by email collision (IdP lets a user set any email) | Match on `(iss, sub)` only; email linking requires local re-authentication and `email_verified=true` |
| B4 | Open redirect through `return_to` or `post_logout_redirect_uri` | Relative same-origin paths only; logout URIs from allowlist |
| B5 | SSRF via admin-supplied issuer URL | Discovery and JWKS fetched through an HTTP client that resolves DNS, rejects private/link-local/loopback ranges (unless explicitly allowed for an on-LAN Authentik, with a warning), refuses cross-host redirects, caps response size |
| B6 | Malicious IdP admin creates a user in an admin group | Group-to-role mapping is explicit; instance admin role is **never** granted from a claim, only from the local database |
| B7 | IdP outage locks the owner out | Break-glass local admin restricted to allowlisted CIDR |
| B8 | Stale sessions after IdP revocation | Back-channel logout supported; optional periodic claim refresh with refresh token |
| B9 | JWKS key rotation causing acceptance of unknown keys | Refresh JWKS on unknown `kid` at most once per N minutes; never accept unsigned tokens |

### C. File ingestion

| # | Vulnerability | Control |
| --- | --- | --- |
| C1 | Parser RCE (PDF/image library CVEs) | Extraction runs in a sandboxed subprocess: rlimits, seccomp, read-only FS, no network, non-root, timeouts; worker container itself has no inbound ports and denied egress by default; libraries pinned and scanned |
| C2 | Zip bomb / decompression bomb (PDF streams, images, zips) | Size caps at every layer: upload, expanded zip (count and total bytes), rendered image pixel caps, OCR time caps |
| C3 | Path traversal via filenames (upload or drop folder) | Original names are metadata only; blobs are stored under random UUIDs; drop folder never processed in place; symlinks not followed |
| C4 | Polyglot files (PDF that is also HTML/JS) served back to the browser | Magic-byte detection on intake; downloads served with sniffed type, `Content-Disposition: attachment`, `X-Content-Type-Options: nosniff`, and from a separate origin path covered by CSP `sandbox` |
| C5 | SVG/HTML with scripts as "image" | SVG not accepted; previews are server-rendered PNGs |
| C6 | XXE in OFX/XML/XLSX | XML parsers configured with external entities and DTD loading disabled (defusedxml) |
| C7 | CSV formula injection on later export | Dangerous leading characters neutralised on import and on export |
| C8 | PDF JavaScript / launch actions / remote links | Rendering library configured to ignore; never opened by a browser engine server-side |
| C9 | Malware passing through the document store to another device | Optional ClamAV scan; downloads always as attachment; the store is not a file share |
| C10 | Resource exhaustion by many or huge files (drop folder, mailbox flood) | Per-run and per-household caps, queue concurrency limits, job timeouts, rejected-email digest instead of per-message items |
| C11 | Drop folder race: file read while still being written | Size-stability settle time, optional `.ready` sidecar, copy-then-process |
| C12 | Data poisoning: a crafted statement inserts fake transactions | Reconciliation check (opening + sum = closing), confidence thresholds, review queue, provenance recorded on every row, audit log |

### D. Email intake

| # | Vulnerability | Control |
| --- | --- | --- |
| D1 | Sender spoofing to inject documents | Secret alias token in recipient address (rotatable) is the primary gate; sender allowlist; SPF/DKIM pass required when headers exist |
| D2 | Mailbox credential theft from DB | Column-level encryption with master key; least-privilege mailbox (dedicated account) |
| D3 | HTML email with tracking pixels / remote content | Converted to text; no remote fetches; no HTML rendering |
| D4 | Attachment bombs | Same intake caps as uploads; nested depth 1 |
| D5 | IMAP server impersonation | IMAPS with certificate verification; no STARTTLS downgrade |

### E. AI access through the MCP server (optional role)

PerFin never calls a hosted model. The optional local model (ADR-0010,
[ML.md](ML.md)) runs on the host's own network only (`PERFIN_ML_URL` must be
local, checked at start-up and before every call), receives only a document's
extracted text, and its answers are untrusted suggestions a person confirms.
The MCP server is the only AI-facing surface for assistants; its threat table lives in [AI_ACCESS.md](AI_ACCESS.md)
(rows M1 to M8). Summary of the controls:

| # | Vulnerability | Control |
| --- | --- | --- |
| E1 | Identity data (address, DOB, SSN, account numbers, names, contacts) or document text reaches a model | Allowlist serialisers for the `ai:read` scope; denylist classes have no serialiser; free-text scrubber in API and MCP; CI corpus must show zero leaks; CI fails on unclassified columns |
| E2 | Prompt injection via payee/memo strings | MCP has no write tools, so there is nothing to act on inside PerFin |
| E3 | Over-exposure by misconfiguration | Exposure preview with real data before a client is created; step-up MFA; Aggregates mode and rounding/hiding options; kill switch |
| E4 | Stolen MCP token or compromised MCP process | Read-only scoped token, hashed, expiring, rate limited, revocable; process has no DB, key or document access; full audit of every call and response |

### F. Web application

| # | Vulnerability | Control |
| --- | --- | --- |
| F1 | Stored XSS via payee names, memos, document text | React escaping by default, no `dangerouslySetInnerHTML`, strict CSP with nonces/hashes, `Trusted Types` where supported |
| F2 | SQL injection | SQLAlchemy Core/ORM with bound parameters only; no string-built SQL; RLS as backstop |
| F3 | IDOR / horizontal privilege escalation | Household scoping in every query plus Postgres RLS; object-level grants on accounts; tests that assert cross-household access returns 404 |
| F4 | Mass assignment | Separate Pydantic input schemas; no model-to-ORM passthrough |
| F5 | Verbose errors leaking internals | Problem-details responses; stack traces only in logs; `DEBUG` cannot be enabled in production mode |
| F6 | Clickjacking | `frame-ancestors 'none'`, `X-Frame-Options: DENY` |
| F7 | Missing security headers | HSTS, CSP, `Referrer-Policy: no-referrer`, `Permissions-Policy` minimal, `Cross-Origin-Opener-Policy: same-origin`, `Cross-Origin-Resource-Policy: same-origin` |
| F8 | Unbounded queries / DoS | Cursor pagination with max limits, statement timeouts in Postgres, request timeouts |
| F9 | Information leakage through caching | `Cache-Control: no-store` on all API responses |
| F10 | Dependency confusion / typosquatting | Lockfiles with hashes (`uv.lock`, `package-lock.json`), private index not used, CI verifies lockfile integrity |

### G. Data at rest and backups

| # | Vulnerability | Control |
| --- | --- | --- |
| G1 | Stolen disk or volume snapshot | Documents envelope-encrypted per object; sensitive columns encrypted; Postgres on an encrypted volume is recommended in docs |
| G2 | Master key in the same place as the data | Key supplied via env/file/KMS at runtime, never written to the data volume; docs explain separating the key from backups |
| G3 | Backup archive leaked | Export/backup archives are encrypted with age/libsodium and a user passphrase; restore script verifies integrity |
| G4 | Key loss = data loss | Startup check warns if no key backup acknowledgement recorded; documented key rotation procedure rewraps DEKs without re-encrypting blobs |
| G5 | Deleted data recoverable from media | Deletion destroys wrapped DEKs (crypto-shredding) |

### H. Deployment and supply chain

| # | Vulnerability | Control |
| --- | --- | --- |
| H1 | Container runs as root, writable rootfs, broad capabilities | Non-root UID, `read_only: true`, `cap_drop: [ALL]`, `no-new-privileges`, tmpfs for scratch, seccomp default profile |
| H2 | Worker reachable from the network | No published ports; internal network only; egress denied unless an integration needs it, then via allowlisting proxy |
| H3 | Database exposed | No published port; strong generated password; `scram-sha-256`; app role has no superuser, separate migration role |
| H4 | Vulnerable base image or dependency | Weekly automated rebuilds, Dependabot/Renovate, Trivy/Grype scan in CI, SBOM published with each release, images signed (cosign) |
| H5 | Secrets in images or git | `.gitignore` for env files, gitleaks pre-commit and CI, secrets only via env/files |
| H6 | Insecure configuration accepted silently | `perfin config check` refuses to start with insecure settings outside `DEV_MODE` |
| H7 | Metrics/health endpoints leak info | Metrics on internal port only; health endpoints minimal and proxy-restricted |
| H8 | Logs contain PII | Structured logging with scrubbing filters; document text and account numbers never logged |

### I. Multi-user and authorisation

| # | Vulnerability | Control |
| --- | --- | --- |
| I1 | Viewer downloads original documents | Document download requires `member` or explicit grant; viewers see extracted summaries only by default |
| I2 | Member escalates to admin | Role changes require owner/admin plus step-up MFA; audit logged; notifications on role change |
| I3 | API token over-privilege | Scoped tokens, inherit at most the user's grants, expiry mandatory, last-used tracked, listing and revocation |
| I4 | Audit log tampering | Append-only table with no `UPDATE`/`DELETE` grants for the app role; optional hash chain |

## Deployment hardening checklist (for the docs site)

- [ ] TLS via the proxy; HSTS enabled; HTTP redirects to HTTPS.
- [ ] Only the proxy publishes ports.
- [ ] `PERFIN_MASTER_KEY` supplied from a secret file or KMS, backed up
      separately from data.
- [ ] MFA required for all users; OIDC with MFA at the IdP if used.
- [ ] Drop folder mounted read-only; processed files moved to a sibling.
- [ ] Receipts mailbox is dedicated; alias token set; sender allowlist set.
- [ ] MCP role disabled unless in use; review the exposure preview for each
      AI client; prefer an on-prem model if merchant-level detail should not
      leave the site.
- [ ] Backups encrypted, restore tested with the provided script.
- [ ] Automatic image updates with a maintenance window; release notes read
      for security advisories.
- [ ] Fail2ban or proxy-level banning on repeated 401/429 if internet-exposed;
      better: expose only through a VPN or Authentik's forward-auth outpost.

## Security engineering practices

- Threat-model review for every feature touching intake, auth or export.
- Security tests in CI: authz matrix tests, CSRF tests, upload fuzz corpus
  (malformed PDFs, zip bombs, polyglots), OIDC negative tests (bad nonce, bad
  aud, expired, unknown kid).
- Static analysis: Ruff + Bandit + Semgrep for Python, ESLint security rules
  for TypeScript, `pip-audit`/`npm audit` in CI.
- Dependency pinning with hashes; renovate bot; a documented upgrade cadence.
- Release signing and a `SECURITY.md` disclosure process (present).
- A `docs/security/` page per integration explaining exactly what data leaves
  the host.
