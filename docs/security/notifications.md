# Notifications: what leaves the host

Scope: the household notification channels of Phase 7 (SMTP, webhook, ntfy,
Gotify, Matrix). This page answers one question for an operator or
reviewer: when a channel is configured, which bytes cross a network
boundary, to where, and what stays home. Controls are referenced by their
[`docs/SECURITY.md`](../SECURITY.md) id; their status is in
[`CONTROLS.md`](CONTROLS.md). The instance SMTP relay used for password
reset mail (`PERFIN_SMTP_*`, A11) is a separate thing and is described in
[`docs/AUTH.md`](../AUTH.md).

## Short answer

**By default nothing leaves the host.** A household has no channels until
an owner or admin adds one under *Settings > Notifications*
(`POST /api/v1/settings/notifications/channels`, browser session plus
fresh step-up MFA). When a channel exists, each notification sends **one
fixed title (`PerFin`) and one fixed, generic sentence** to the channel's
own host, for example:

> A recovery code was used to sign in to a household member's account.

No message names a person, a user name, an e-mail or IP address, a
household, an account, an amount, a count, a date or a document. The
sentences are constants in `backend/app/domain/notifications/routing.py`
(`ROUTES`); a unit test fails if one contains a digit, `@`, a URL or a
template placeholder (`backend/tests/unit/test_notification_channels.py::test_routing_table_is_generic`),
and an integration test checks that a delivered webhook names nobody
(`backend/tests/integration/test_notifications.py::test_send_job_delivers_retries_and_records`).

## What triggers a notification

| Kind (subscribable per channel) | Source | Sentence |
| --- | --- | --- |
| `account_locked` | an account's 15-minute lock starts (`user.locked`, A1) | A household member's account was locked after repeated failed sign-in attempts. |
| `recovery_code_used` | a recovery code was accepted (`security.recovery_code_used`, A8) | A recovery code was used to sign in to a household member's account. |
| `mfa_reset` | an instance admin reset someone's second factors (`user.mfa_reset`) | An administrator reset a household member's second factors. |
| `role_changed` | `member.role_changed`, `member.added`, `member.removed` (I2) | A household member's role was changed. / Someone was added to the household. / Someone was removed from the household. |
| `new_device` | a sign-in (local or OIDC) whose `User-Agent` matches none of the user's other session rows, when the user has signed in before | A household member signed in from a new device or browser. |
| `ai_client_created` | an owner created an MCP client (`ai_client.create`) | A new AI assistant connection was created for the household. |
| `review_waiting` | daily job `notify_review_waiting`: the household has open review items | Items are waiting for you in the review queue. |
| `tax_reminder` | `tax.reminder` on the `PERFIN_TAX_REMINDER_DATES` days | Expected tax documents for last year are still missing. |
| `backup_failed` | `perfin notify backup-failed` (see below) | A backup did not complete. Please check the server. |
| (test) | `POST .../channels/{id}/test` | This is a test notification. The channel works. |

A security event about one user (lock, recovery code, new device, MFA reset)
is sent to the channels of **every household the user belongs to**: the
channels belong to the household, not to the user, so the sentence says
"a household member" and the people who read the channel sign in to see
who (the audit log and the household events have the details).

## Per channel

| Channel | Destination | Request | Credentials (encrypted at rest, write-only) |
| --- | --- | --- | --- |
| SMTP | the relay's `host:port` | `EHLO`, `STARTTLS` (required: a server that does not offer it gets no `AUTH` and no message, outcome `tls_failed`) or implicit TLS, optional `AUTH`, one message: `From` (the configured sender), `To` (1 to 10 configured recipients), `Subject: PerFin`, `Message-ID`, a plain-text body with the sentence | user name and password |
| Webhook | the URL's `host:port` | `POST <url>`, `Content-Type: application/json`, body `{"id", "kind", "title", "message", "sent_at"}`; headers `X-PerFin-Delivery` (the id), `X-PerFin-Event` (the kind), `X-PerFin-Timestamp` (Unix seconds), `X-PerFin-Signature: v1=<hex>` | signing secret (made by PerFin, shown once) |
| ntfy | the server's `host:port` (default `https://ntfy.sh`) | `POST <server>/<topic>`, body the sentence, headers `Title: PerFin`, `Priority`, `Tags: perfin`, `Authorization: Bearer` when a token is set | topic (it is the read key of a public topic) and optional access token |
| Gotify | the server's `host:port` | `POST <server>/message`, header `X-Gotify-Key`, JSON `{"title", "message", "priority"}` | application token |
| Matrix | the homeserver's `host:port` | `PUT <homeserver>/_matrix/client/v3/rooms/<room id>/send/m.room.message/<notification id>`, `Authorization: Bearer`, JSON `{"msgtype": "m.notice", "body": "PerFin: <sentence>"}`; the transaction id is the notification id, so a retried delivery is not posted twice | access token (use a dedicated bot account in a private room) |

Every HTTPS request also carries `Host`, `User-Agent: PerFin`, `Accept: */*`,
`Accept-Encoding: identity`, `Connection` and `Content-Length`. No cookies,
nothing else. Credentials are only put in
headers, never in a URL (the ntfy topic is a path segment by design).

### Verifying a webhook

```text
expected = "v1=" + hex(HMAC-SHA256(secret, X-PerFin-Timestamp + "." + raw_body))
```

Compare `expected` with `X-PerFin-Signature` in constant time, refuse
timestamps more than a few minutes old (replay), and use `X-PerFin-Delivery`
to drop a delivery you already processed (a retry keeps the id).
`PATCH .../channels/{id}` with `{"rotate_secret": true}` replaces the secret
(step-up; the new one is shown once). Changing a webhook's `url` to another
host or port also replaces the secret and returns the new one once, so a
body signed for one receiver never verifies at the other and cannot be
replayed there; another path on the same `host:port` keeps the secret.

## Transport rules (B5, H2)

* **https only** for webhook, ntfy, Gotify and Matrix (`http://` URLs are
  refused when saved and when sent); **TLS always** for SMTP. Certificates
  and host names are verified against the system trust store, TLS 1.2 or
  later (`app.integrations.notifications.base.default_ssl_context`). A
  private CA is trusted only through `SSL_CERT_FILE`, as for the OIDC
  issuer.
* **One host per channel, addresses vetted, connection pinned**:
  `app.core.http.SafeHttpClient` (HTTPS) and
  `app.ingestion.intake.email.open_socket` (SMTP) resolve the name, refuse
  it if any address is loopback, link-local (including the cloud metadata
  address), multicast, reserved or private, and connect to the checked
  address. **No redirects** (a `3xx` answer is `blocked`), 64 KiB response
  cap, `PERFIN_NOTIFICATIONS_TIMEOUT_SECONDS` (default 10 s).
* **Instance allowlist**: with `PERFIN_NOTIFICATIONS_ALLOWED_HOSTS` set
  (`host` or `host:port`, comma separated), a channel can only be saved,
  tested or used for a listed host (`422 notification-host-not-allowed`,
  outcome `blocked`). A household admin therefore cannot point PerFin at an
  arbitrary host. `PERFIN_NOTIFICATIONS_ALLOW_PRIVATE_HOSTS=true` allows
  private (RFC 1918, ULA) addresses **only for listed hosts** (a Gotify or
  ntfy on the LAN); `perfin config check` warns when it is set without a
  list.
* **Egress proxy**: with `HTTPS_PROXY` set (the Compose `egress` profile),
  HTTPS requests and SMTP connections are tunnelled through the CONNECT
  allowlist proxy, which applies its own allowlist and address rules: add
  each channel host (with its port, e.g. `smtp.example.com:587`) to
  `deploy/egress-allowlist.txt`, and a LAN address to
  `EGRESS_ALLOW_PRIVATE_CIDRS`.
* Both the api (test sends) and the worker (all other sends) open these
  connections.

## Delivery, retries and failure

Routing happens in the transaction that records the event
(`app.domain.notifications.routing.notify`): for every **enabled** channel
of the household subscribed to the kind, one `send_notification` job is
queued (dedupe key `notify:<channel>:<source>`). Its payload is the channel
id, the routed event type and a delivery id, nothing else; the sentence is
looked up when the job runs. A rolled-back change sends nothing.

The job delivers through the channel. `connect_failed`, `timeout` and
`server_error` (5xx, 408, 425, 429, SMTP 4xx) are retried with the queue's
exponential backoff, up to 6 attempts; `blocked`, `tls_failed`,
`auth_failed`, `rejected` and `protocol_error` are not. The final outcome
code and time are stored on the channel (`last_outcome`, `last_sent_at`,
shown in the UI). A channel disabled, deleted or unsubscribed before the
job runs sends nothing. Errors and logs contain outcome codes and the
channel type, never a URL path, a credential or a service's reply.

## Storage

`notification_channel` (household-scoped, forced RLS): name, type,
subscribed kinds, enabled flag, last outcome, and `config_encrypted`, one
envelope under the master key bound to its row and column (D2) holding the
whole configuration, destination included. The API returns only the
non-secret fields plus `secrets_set`; moving a channel to another
destination requires its credentials to be typed again (`422
notification-secret-required`), so a stored token or password is never sent
to a host it was not entered for, and a webhook moved to another
`host:port` gets a new signing secret. Every change is audited
(`notification_channel.create|update|delete`, changed field names only).
Channels are not part of the full export (`NOT_EXPORTED`): set them up again
after an import. The column classification denies the whole table to AI
views.

## Backups

PerFin does not schedule backups itself. A backup job (cron, systemd timer,
the host's backup tool) that fails can tell every household's subscribed
channels with:

```sh
docker compose exec -T worker perfin notify backup-failed
```

It queues one `backup.failed` notification per household with a channel
subscribed to `backup_failed`; the worker sends them.
