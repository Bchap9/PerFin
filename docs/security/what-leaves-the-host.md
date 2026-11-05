# What leaves the host

PerFin runs on hardware you control, has no telemetry and calls no hosted
model. The optional local model (ADR-0010) must run on your own network:
`PERFIN_ML_URL` is refused unless it is a local address, and its runtime
reaches the internet only through the egress proxy, to download a model. **With the default configuration, no connection leaves the
host at all**: the `api` and `worker` containers have no route to the internet
(SECURITY.md H2). Every outbound path below is opt-in, goes through the
allowlisting `egress` proxy in the Compose deployment, and is documented in
detail on its own page.

| Feature | Setting that enables it | Destination | What is sent | Page |
| --- | --- | --- | --- | --- |
| Document ingestion (upload, drop folder) | always on | none | nothing; files stay local, encrypted | [ingestion.md](ingestion.md) |
| Email intake | `PERFIN_EMAIL_INTAKE_ENABLED=true` and mailbox settings | your IMAPS host, port 993 | mailbox login, IMAP commands | [ingestion.md](ingestion.md), [email.md](email.md) |
| Password-reset mail | `PERFIN_SMTP_HOST` | your SMTP relay | a reset link to the account's own address | [email.md](email.md) |
| Notification channels | an owner adds a channel | the channel's host (SMTP, webhook, ntfy, Gotify, Matrix) | a fixed title and one generic sentence | [notifications.md](notifications.md) |
| Prices | `PERFIN_PRICE_PROVIDER=http_json` | `PERFIN_PRICE_PROVIDER_URL` host | one ticker symbol per request (and the optional API key header) | [integrations.md](integrations.md) |
| Exchange rates | `PERFIN_FX_PROVIDER=ecb` | `www.ecb.europa.eu` | a fixed URL; nothing household-specific | [integrations.md](integrations.md) |
| OIDC sign-in | `PERFIN_OIDC_ISSUER` | your identity provider | protocol messages (discovery, JWKS, token exchange) | [oidc.md](oidc.md) |
| Breached-password check | `PERFIN_HIBP_CHECK_ENABLED=true` | `api.pwnedpasswords.com` | the first 5 hex characters of a SHA-1 hash (k-anonymity) | [ingestion.md](ingestion.md#other-outbound-connections-not-ingestion) |
| Antivirus signatures | `av` profile | `database.clamav.net` | a download request | [ingestion.md](ingestion.md#other-outbound-connections-not-ingestion) |
| AI assistant access | `PERFIN_MCP_ENABLED=true` and a client | nothing outbound: an assistant host pulls | scrubbed financial facts on request | [ai.md](ai.md) |
| Updates | you run `docker compose pull` | the image registry | a pull request | [UPGRADING.md](../UPGRADING.md) |

Things PerFin never sends: passwords, session tokens, the master key,
document contents or text, account or government identifiers, names,
addresses and dates of birth. Logs are scrubbed before they are written
(SECURITY.md H8) and are not shipped anywhere by PerFin.

To audit your instance: list the hosts in `deploy/egress-allowlist.txt` (that
file is the complete set of external destinations the application can
reach), the integrations enabled in `.env`, and the notification channels in
Settings. Anything else leaving the host is outside PerFin (your proxy, your
DNS, your backup tooling, an AI assistant you connected).
