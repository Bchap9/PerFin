# Email: what leaves the host

Scope: the two ways PerFin uses email. Both are off by default. Control ids
refer to [SECURITY.md](../SECURITY.md) (D1 to D5, A11); status is in
[CONTROLS.md](CONTROLS.md).

## Short answer

**PerFin sends mail only for password reset (if you configure an SMTP relay)
and for an SMTP notification channel (if a household adds one), and reads mail
only from the one mailbox you configure for intake.** No message contains an
amount, a name, an account or a document. Email intake reads; it does not
send.

## 1. Password-reset mail (`PERFIN_SMTP_*`)

| Property | Behaviour |
| --- | --- |
| Enabled by | `PERFIN_SMTP_HOST` and `PERFIN_SMTP_FROM` set, and local login enabled. Otherwise there is no reset flow and no SMTP connection. |
| Sent by | `api`, after the HTTP response has been sent (timing does not reveal whether an account exists). |
| Destination | The relay `PERFIN_SMTP_HOST:PERFIN_SMTP_PORT` and nothing else. |
| Transport | `starttls` (the server must offer it, or nothing, not even `AUTH`, is sent) or `implicit` TLS; certificates and host names verified; TLS 1.2+. The credentials (`PERFIN_SMTP_USERNAME`, `PERFIN_SMTP_PASSWORD`) never leave in clear text. |
| Content | One message to the account's own verified address: a link `<PERFIN_PUBLIC_URL>/reset-password#token=...` (the token is in the fragment, so it never reaches a server log or a `Referer`) and fixed text. Break-glass, inactive and address-less accounts are never sent one. |
| Egress in Compose | Add `smtp-host:587` (or 465) to `deploy/egress-allowlist.txt` and enable the `egress` profile; the connection is a `CONNECT` tunnel. |

## 2. Notification channels (SMTP and others)

An owner or admin may add an SMTP channel under Settings > Notifications.
Each notification is one fixed title (`PerFin`) and one fixed generic
sentence, to 1 to 10 configured recipients. Everything about it, including
why it names nobody, is in [notifications.md](notifications.md).

## 3. Email intake (receiving receipts and statements)

`PERFIN_EMAIL_INTAKE_ENABLED=true` plus a per-household configuration under
Settings > Intake. Full behaviour: [INGESTION.md](../INGESTION.md) "Email
intake specifics" and [ingestion.md](ingestion.md).

| Property | Behaviour |
| --- | --- |
| Destination | The configured mailbox `host:port` (993) and nothing else. No autodiscovery, no MX lookups. Remote images, links and trackers in mail bodies are never fetched (D3). |
| Transport | Implicit TLS only (`IMAP4_SSL`), TLS 1.2+, certificate and host name verified against the system store (a private CA via `SSL_CERT_FILE`). No `STARTTLS`, no plaintext fallback (D5). |
| Path | In Compose, a `CONNECT host:993` tunnel through the `egress` proxy; the host must be in `deploy/egress-allowlist.txt`. Without a proxy, private addresses are refused unless `PERFIN_EMAIL_INTAKE_ALLOW_PRIVATE_HOSTS=true`. |
| What is sent | The mailbox login (an app password, stored column-encrypted, D2) and IMAP commands to list, fetch and move messages between the folders you named. Nothing from PerFin's data is uploaded. |
| What is read | Header blocks (at most 64 KiB) to apply the acceptance policy; only accepted messages are fetched whole and parsed in the sandbox. A rejected message's body is never fetched. |
| Acceptance policy | The secret alias token must be in a recipient address; the single `From` address must be an allowed sender; `spf=pass` and `dkim=pass` from the topmost `Authentication-Results` header when required (D1). |
| Side effect on the mailbox | Accepted messages are moved to `PerFin/Processed`, rejected ones to `PerFin/Rejected` (names configurable). Use a dedicated mailbox: PerFin moves mail there. |

## Your controls

Leave both `PERFIN_SMTP_HOST` and `PERFIN_EMAIL_INTAKE_ENABLED` unset to keep
email entirely off. Use an app password with access to one dedicated mailbox,
rotate the alias token after any suspicion, keep the allowed-sender list to
people in the household, and keep the egress allowlist to the mail hosts you
use.

## Verifying it

- `backend/tests/security/test_imaps_tls.py`: implicit TLS only, plaintext
  servers never spoken to, untrusted or wrong-name certificates refused
  before login, the `CONNECT` tunnel, loopback only with permission.
- `backend/tests/unit/test_email_policy.py`: the acceptance policy and the
  TLS context.
- `backend/tests/unit/test_smtp.py`, `backend/tests/integration/test_password_reset.py`:
  TLS mandatory, no mail for unknown or break-glass accounts, fragment token.
