# Deploying PerFin

> [!WARNING]
> **Disclaimer: this project is entirely vibe coded.** PerFin was written by
> AI coding agents directed through prompts, not written line by line by a
> human. A person has not reviewed it line by line, and no external security
> review or penetration test has been done. Its tests and internal reviews
> are evidence, not proof.
>
> It is **not intended for, and not proven safe for, exposure to the open
> internet.** Run it only on a private network: your LAN, a VPN, or behind
> an authenticating gateway such as Authentik's forward-auth outpost. Use it
> at your own risk, keep your own backups, and do not rely on it as your only
> record of anything that matters. The AGPL-3.0 licence's "no warranty"
> terms apply in full.

This guide takes one Linux host from nothing to a running, hardened PerFin
with Docker Compose. It uses the files in `deploy/`: `docker-compose.yml`,
`.env.example`, `Caddyfile.example`, `egress-allowlist.txt` and `db-init/`.
Day-two work is in [OPERATIONS.md](OPERATIONS.md), backups in
[BACKUP_RESTORE.md](BACKUP_RESTORE.md), upgrades in
[UPGRADING.md](UPGRADING.md). For Kubernetes see the Helm section of
UPGRADING.md.

What you get:

```
LAN or VPN -> proxy (Caddy, TLS, the only published ports 80/443)
              |  web network (internal)
              v
            api ----- backend network (internal) ----- db (PostgreSQL 16)
                                                         |
            worker (parses untrusted files) -------------+
            migrate (one-shot, owner role)
            optional: egress (allowlist proxy), clamav, mcp
```

## 1. Prerequisites

| Need | Detail |
| --- | --- |
| Host | 64-bit Linux, 2 CPUs, 4 GB RAM and 20 GB free disk as a starting point (the worker may use up to 2 GB for OCR). |
| Kernel | Landlock enabled (Linux 5.13 or later with the `landlock` LSM, the default on current Debian, Ubuntu, Fedora and Arch). The extraction sandbox refuses to run without it ([ADR-0009](adr/0009-extraction-isolation-under-a-shared-uid.md)). Check: `grep -q landlock /sys/kernel/security/lsm && echo ok`. |
| Docker | Docker Engine 25 or later with the Compose plugin v2.24 or later (`docker compose version`), using Docker's default seccomp profile. Rootless Docker and Podman are not covered here. |
| DNS and ports | A host name (for example `perfin.example.com`) pointing at the host, and inbound TCP 80 and 443 (and UDP 443 for HTTP/3) open. Caddy obtains its certificate from Let's Encrypt over port 80/443; for a LAN-only install use your own certificate (section 6) or a DNS-challenge build of Caddy. |
| Disk | Preferably an encrypted volume or LUKS for `/var/lib/docker` (SECURITY.md G1). |
| Tools | `openssl`, `git`, `curl`. |

Get the files (pin a release tag, never `main`, for a production host):

```sh
sudo mkdir -p /opt/perfin && sudo chown "$USER" /opt/perfin
git clone https://github.com/bchap9/perfin /opt/perfin
cd /opt/perfin && git checkout v1.0.0     # the release you verified (UPGRADING.md)
cd deploy
```

## 2. Generate the secrets

Everything below is run from `/opt/perfin/deploy`. Never commit `.env`.

```sh
cp .env.example .env && chmod 600 .env
```

Set each value with the exact command shown (replace the `CHANGE_ME` line;
`perfin config check` and the database init script refuse a placeholder):

| Variable | Command | Notes |
| --- | --- | --- |
| `PERFIN_MASTER_KEY` | `openssl rand -base64 32` | Exactly 32 random bytes, base64. See section 3. |
| `PERFIN_SECRET_KEY` | `openssl rand -base64 48` | CSRF and session signing. |
| `POSTGRES_PASSWORD` | `openssl rand -hex 32` | Bootstrap superuser, used inside the `db` container only. |
| `POSTGRES_OWNER_PASSWORD` | `openssl rand -hex 32` | `perfin_owner`, used only by the one-shot `migrate` service. |
| `POSTGRES_APP_PASSWORD` | `openssl rand -hex 32` | `perfin_app` (no superuser, no BYPASSRLS), used by `api` and `worker`. |

The three database passwords must be different. Use hex so they are safe
inside the connection URLs. They are read once, when the `pgdata` volume is
first created; changing them later also needs `ALTER ROLE` in the database.

A one-shot way to fill the file:

```sh
sed -i "s|^PERFIN_MASTER_KEY=.*|PERFIN_MASTER_KEY=$(openssl rand -base64 32)|" .env
sed -i "s|^PERFIN_SECRET_KEY=.*|PERFIN_SECRET_KEY=$(openssl rand -base64 48)|" .env
for v in POSTGRES_PASSWORD POSTGRES_OWNER_PASSWORD POSTGRES_APP_PASSWORD; do
  sed -i "s|^$v=.*|$v=$(openssl rand -hex 32)|" .env
done
```

Then set the public address (it must be the exact `https://` origin users
type, no trailing path; it is the CSRF origin, the cookie scope and the base
of the setup link):

```sh
sed -i 's|^PERFIN_PUBLIC_URL=.*|PERFIN_PUBLIC_URL=https://perfin.example.com|' .env
sed -i 's|^PERFIN_VERSION=.*|PERFIN_VERSION=1.0.0|' .env      # pin the image
```

Leave `PERFIN_TRUSTED_PROXIES=172.30.80.10/32` as shipped: the compose file
pins the proxy to that address on the private `web` network, and the API
trusts `X-Forwarded-*` from it alone.

## 3. The master key, and backing it up separately

`PERFIN_MASTER_KEY` wraps the data key of every document and every
encrypted column (ADR-0003). It is **never** part of a backup and never
stored in the database or the documents volume. If you lose it, every
document is unreadable and no support request can fix that; with it and a
backup, a fresh host restores everything.

**There is exactly one master key per instance, and it never changes after
first boot except through `perfin rotate-key`**
([BACKUP_RESTORE.md](BACKUP_RESTORE.md#key-rotation)). Generating a new key
and putting it in place of the old one (in `.env` or in a key file) makes
every document and encrypted column already written unreadable, and the
key you backed up would no longer match the instance. The key you keep
offline must always be the key the instance is configured with.

**Optional, recommended: move the key into a file.** To keep the key out of
the process environment, put **the key already in `.env`** (do not generate
a new one) in a file, mount it into `api` and `worker`, and remove it from
`.env`. Do this before you back the key up, so the value you store offline
is the one in the file:

```sh
umask 077 && mkdir -p secrets
grep '^PERFIN_MASTER_KEY=' .env | cut -d= -f2- > secrets/master.key   # the existing key, not a new one
test "$(wc -c < secrets/master.key)" -gt 40 || echo "secrets/master.key is empty: stop"
sudo chown 10001:10001 secrets/master.key && sudo chmod 0400 secrets/master.key
sed -i '/^PERFIN_MASTER_KEY=/d' .env    # set only one of the two
# add to docker-compose.override.yml:
#   services:
#     api:    { volumes: ["./secrets/master.key:/run/secrets/perfin_master_key:ro"],
#               environment: { PERFIN_MASTER_KEY_FILE: /run/secrets/perfin_master_key } }
#     worker: { volumes: [...same, plus the documents/quarantine/inbox mounts below...] }
```

(A service's `volumes` list in an override file is merged with the base
file's; repeat only the new entry.) A world- or group-readable key file is
refused or warned about by `perfin config check`, and setting both
`PERFIN_MASTER_KEY` and `PERFIN_MASTER_KEY_FILE` is refused.

**Back the key up separately:**

1. Copy the value of the key, from `secrets/master.key` if you moved it
   there (`sudo cat secrets/master.key`) or else from the
   `PERFIN_MASTER_KEY` line of `.env`, into a password manager or print it
   and store it offline, in a place that is **not** the host and **not**
   next to your backup archives.
2. Prove you can read it back from there, and that it is identical to the
   configured value.
3. Only then tell PerFin you did it:

   ```sh
   sed -i 's|^PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED=.*|PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED=true|' .env
   ```

Until the acknowledgement is `true`, every start of `api`, `worker` and
`mcp`, and every `perfin config check`, logs the warning
`master_key.backup_unacknowledged` (SECURITY.md G4). It is a warning, not an
error: the stack runs, but do not enter real data before you have done this.

If you move the key into a file after first boot, follow the same recipe:
it copies the configured key, so nothing becomes unreadable. Key rotation
(the only way to change the key) is in
[BACKUP_RESTORE.md](BACKUP_RESTORE.md#key-rotation).

## 4. First boot and the admin account

```sh
cp Caddyfile.example Caddyfile
sed -i 's/perfin\.example\.com/your.host.name/' Caddyfile
mkdir -p inbox processed && sudo chown 10001:10001 processed
docker compose run --rm --no-deps api config check
docker compose up -d
docker compose ps
```

`config check` prints every problem (`ERROR` lines refuse startup) and
`perfin config check: OK (N warning(s))` when the configuration is
acceptable. `docker compose up -d` starts, in order: `db` (healthy), the
one-shot `migrate` (runs as `perfin_owner`, exits 0), then `api` and `worker`,
then `proxy`. The first start pulls images and the proxy requests its
certificate, so allow a few minutes. `docker compose ps` should show `db`,
`api`, `worker` and `proxy` as healthy/running and `migrate` as exited (0).

**Where the setup URL appears.** While no user exists, `api` draws a
one-time token and prints the setup link once, as a WARNING log line:

```sh
docker compose logs api | grep -o 'https://[^" ]*/setup#t=[A-Za-z]*'
```

It is also written (mode 0600) to
`/var/lib/perfin/documents/.perfin-setup/setup-url` inside the container:

```sh
docker compose exec api cat /var/lib/perfin/documents/.perfin-setup/setup-url
```

The link expires after 24 hours; get a fresh one (replacing the old token)
with `docker compose exec api perfin bootstrap-url`. The token sits in the URL
fragment, which browsers never send to a server. Open the link, choose a
username and a strong password, name your household and base currency; this
creates the first instance admin and owner, and the page is gone for good
(`404`) afterwards. With `PERFIN_REQUIRE_MFA=true` (the default) the first
session may only enrol a second factor (TOTP or a WebAuthn key); do that now
and save the recovery codes.

Smoke test:

```sh
curl -sI https://perfin.example.com/ | head -5                  # 200 + HSTS
curl -s -o /dev/null -w '%{http_code}\n' https://perfin.example.com/healthz   # 404: not served to the internet
docker compose exec api perfin healthcheck && echo healthy
docker compose run --rm --no-deps migrate audit verify         # audit-log hash chain
```

## 5. Sign-in options

- **Local accounts** (default): Argon2id passwords plus MFA.
- **OIDC / Authentik**: set `PERFIN_OIDC_ISSUER`, `PERFIN_OIDC_CLIENT_ID`,
  `PERFIN_OIDC_CLIENT_SECRET` and the group variables in `.env`, add the
  issuer's host to `egress-allowlist.txt`, enable the `egress` profile
  (section 9) and follow [AUTHENTIK.md](AUTHENTIK.md). To go OIDC-only set
  `PERFIN_LOCAL_LOGIN_ENABLED=false` **and** `PERFIN_BREAKGLASS_ADMIN_USERNAME`;
  the stack refuses to start without a named break-glass admin.
- **Break-glass**: reachable only from `PERFIN_BREAKGLASS_CIDRS`. In Compose
  a client on the Docker host itself arrives from the edge gateway
  `172.30.79.1`, not `127.0.0.1`; set
  `PERFIN_BREAKGLASS_CIDRS=172.30.79.1/32` deliberately (read the IPv6
  caveat in AUTHENTIK.md) and check with `perfin breakglass status`.
- **Password reset by email**: set `PERFIN_SMTP_HOST` and `PERFIN_SMTP_FROM`
  (TLS is mandatory; see [security/email.md](security/email.md)).

## 6. Reverse proxy

The shipped `Caddyfile` is the reference. The API sets CSP, COOP/CORP,
Permissions-Policy and `Cache-Control: no-store` itself; the proxy must
provide TLS, the body limits, slow-client bounds, correct `X-Forwarded-*`
handling and unbuffered server-sent events, and must **not** serve
`/healthz`, `/readyz` or `/metrics`.

Requirements any proxy must meet:

| Requirement | Value |
| --- | --- |
| TLS | 1.2 and 1.3 only; HTTP redirects to HTTPS; HSTS `max-age=63072000; includeSubDomains; preload`. |
| Headers | `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`; strip `Server` and `Via`. |
| Body size | `POST /api/v1/documents`: `PERFIN_UPLOAD_MAX_MB` + 2 MB (52 MB by default). `POST /api/v1/import`: `PERFIN_IMPORT_MAX_MB` + 2 MB (514 MB). Everything else: 1 MB. |
| Slow clients | Header read 10 s, body read 60 s, idle 2 minutes, header size 16 KB. |
| SSE | `GET /api/v1/events` is never buffered or compressed, with no short read/write timeout (streams last up to `PERFIN_SSE_MAX_STREAM_SECONDS`, 600 by default, then the browser reconnects). |
| Forwarded headers | The proxy sets `X-Forwarded-For` and `X-Forwarded-Proto` from what it saw, discarding client-supplied values, and its address on the `web` network is listed in `PERFIN_TRUSTED_PROXIES`. |
| Internal paths | `/healthz*`, `/readyz*` and `/metrics*` answer 404 (prefix, case-insensitive, decoded path). |
| Logs | No query strings, cookies, `Authorization` or CSRF headers (H8). |

### Caddy (shipped)

`Caddyfile.example` implements all of the above (it is validated and
format-checked in CI). Replace the host name, `cp Caddyfile.example
Caddyfile`, and keep the body limits in step with `PERFIN_UPLOAD_MAX_MB` and
`PERFIN_IMPORT_MAX_MB`. After editing, `docker compose restart proxy` (the
admin API is off by design). Caddy core has no rate limiting; either build it
with the `caddy-ratelimit` module (example in the file) or ban repeated
401/429 responses with fail2ban or CrowdSec reading the proxy log. To use your
own certificate instead of ACME, replace the `tls { ... }` block with
`tls /path/fullchain.pem /path/privkey.pem` and mount the files read-only.

### Another proxy: Traefik or nginx

Run it **instead of** Caddy, as the `proxy` service, so it keeps the fixed
address on the `web` network (`172.30.80.10`, `PROXY_WEB_IP`), the only
published ports and the dependency on a healthy `api`. Put the override in
`deploy/docker-compose.override.yml` (Compose reads it automatically). Both
snippets below were run against a stub API and checked for: 404 on
`/healthz`, `/HEALTHZ`, `/healthz%2F`, `/readyz` and `/metrics`; 413 for a
3 MB POST to an ordinary route; success for a 3 MB POST to the upload and
import routes; the headers above.

**nginx** (`docker-compose.override.yml`, then the three files below):

```yaml
services:
  proxy:
    image: docker.io/library/nginx:1.27-alpine
    user: "101:101"
    ports: !override
      - "${PROXY_HTTP_PORT:-80}:8080"
      - "${PROXY_HTTPS_PORT:-443}:8443"
    volumes: !override
      - ./nginx/nginx.conf:/etc/nginx/nginx.conf:ro
      - ./nginx/snippets:/etc/nginx/snippets:ro
      - ./nginx/tls:/etc/nginx/tls:ro      # fullchain.pem, privkey.pem
    tmpfs:
      - /tmp:size=64m,mode=1777,nosuid,nodev
```

`nginx/nginx.conf`:

```nginx
worker_processes auto;
pid /tmp/nginx.pid;
error_log /dev/stderr warn;
events { worker_connections 1024; }

http {
    include       /etc/nginx/mime.types;
    default_type  application/octet-stream;
    server_tokens off;
    client_body_temp_path /tmp/client_body;
    proxy_temp_path       /tmp/proxy;
    fastcgi_temp_path     /tmp/fastcgi;
    uwsgi_temp_path       /tmp/uwsgi;
    scgi_temp_path        /tmp/scgi;

    # $uri has no query string; no cookies or credentials are logged (H8).
    log_format perfin escape=json '{"ts":"$time_iso8601","remote":"$remote_addr",'
        '"method":"$request_method","path":"$uri","status":$status,'
        '"bytes":$body_bytes_sent,"ms":"$request_time"}';
    access_log /dev/stdout perfin;

    client_header_timeout 10s;
    client_body_timeout 60s;
    keepalive_timeout 2m;
    large_client_header_buffers 4 16k;

    gzip on;
    gzip_types text/css application/javascript application/json image/svg+xml;
    # text/event-stream is deliberately absent: never compress SSE.

    server {
        listen 8080;
        return 301 https://$host$request_uri;
    }

    server {
        listen 8443 ssl;
        http2 on;
        server_name perfin.example.com;
        ssl_certificate     /etc/nginx/tls/fullchain.pem;
        ssl_certificate_key /etc/nginx/tls/privkey.pem;
        ssl_protocols       TLSv1.2 TLSv1.3;

        location ~* ^/(healthz|readyz|metrics) { return 404; }

        location ~ ^/api/v1/documents/?$ {          # PERFIN_UPLOAD_MAX_MB + 2
            client_max_body_size 52m;
            proxy_request_buffering off;
            include snippets/perfin-headers.conf;
            include snippets/perfin-proxy.conf;
            proxy_read_timeout 300s;
        }
        location ~ ^/api/v1/import/?$ {             # PERFIN_IMPORT_MAX_MB + 2
            client_max_body_size 514m;
            proxy_request_buffering off;
            include snippets/perfin-headers.conf;
            include snippets/perfin-proxy.conf;
            proxy_read_timeout 300s;
        }
        location /api/v1/events {                   # SSE: never buffer
            client_max_body_size 1m;
            proxy_buffering off;
            proxy_cache off;
            gzip off;
            include snippets/perfin-headers.conf;
            include snippets/perfin-proxy.conf;
            proxy_read_timeout 700s;                # > PERFIN_SSE_MAX_STREAM_SECONDS
        }
        location / {
            client_max_body_size 1m;
            include snippets/perfin-headers.conf;
            include snippets/perfin-proxy.conf;
            proxy_read_timeout 60s;
        }
    }
}
```

`nginx/snippets/perfin-headers.conf` (every location includes it, because
`add_header` in a location replaces inherited ones):

```nginx
add_header Strict-Transport-Security "max-age=63072000; includeSubDomains; preload" always;
add_header X-Content-Type-Options "nosniff" always;
add_header Referrer-Policy "no-referrer" always;
add_header X-Frame-Options "DENY" always;
```

`nginx/snippets/perfin-proxy.conf`:

```nginx
proxy_pass http://api:8000;
proxy_http_version 1.1;
proxy_set_header Connection "";
proxy_set_header Host $host;
# Overwrite, never append: this proxy is the edge.
proxy_set_header X-Forwarded-For $remote_addr;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_connect_timeout 5s;
proxy_hide_header Server;
proxy_hide_header X-Powered-By;
```

**Traefik v3** (`docker-compose.override.yml`, then two files):

```yaml
services:
  proxy:
    image: docker.io/library/traefik:v3.3
    ports: !override
      - "${PROXY_HTTP_PORT:-80}:8080"
      - "${PROXY_HTTPS_PORT:-443}:8443"
    volumes: !override
      - ./traefik/traefik.yml:/etc/traefik/traefik.yml:ro
      - ./traefik/dynamic.yml:/etc/traefik/dynamic.yml:ro
      - ./traefik/tls:/etc/traefik/tls:ro      # fullchain.pem, privkey.pem
```

Traefik can obtain certificates itself with a `certificatesResolvers`
entry in `traefik.yml` and a writable volume for `acme.json`; the static
certificate below keeps the example self-contained.

`traefik/traefik.yml`:

```yaml
entryPoints:
  web:
    address: ":8080"
    http:
      redirections:
        entryPoint: { to: websecure, scheme: https }
  websecure:
    address: ":8443"
    # No forwardedHeaders.trustedIPs: Traefik discards any X-Forwarded-*
    # the client sent and sets its own (what PERFIN_TRUSTED_PROXIES expects).
    transport:
      respondingTimeouts:
        readTimeout: 60s      # whole request including body; raise it for imports over a slow link
        writeTimeout: 0s      # SSE streams are long-lived
        idleTimeout: 2m
providers:
  file: { filename: /etc/traefik/dynamic.yml }
log: { level: WARN }
accessLog:
  format: json
  fields:
    defaultMode: keep
    headers: { defaultMode: drop }     # no cookies, no Authorization
```

`traefik/dynamic.yml`:

```yaml
tls:
  certificates:
    - { certFile: /etc/traefik/tls/fullchain.pem, keyFile: /etc/traefik/tls/privkey.pem }
  options:
    default: { minVersion: VersionTLS12 }

http:
  middlewares:
    perfin-headers:
      headers:
        stsSeconds: 63072000
        stsIncludeSubdomains: true
        stsPreload: true
        forceSTSHeader: true
        contentTypeNosniff: true
        referrerPolicy: no-referrer
        frameDeny: true
    perfin-limit-default:
      buffering: { maxRequestBodyBytes: 1048576 }
    perfin-limit-upload:                 # PERFIN_UPLOAD_MAX_MB + 2 MB
      buffering: { maxRequestBodyBytes: 54525952, memRequestBodyBytes: 1048576 }
    perfin-limit-import:                 # PERFIN_IMPORT_MAX_MB + 2 MB
      buffering: { maxRequestBodyBytes: 539754496, memRequestBodyBytes: 1048576 }
    perfin-compress:
      compress: { excludedContentTypes: [text/event-stream] }

  services:
    perfin-api:
      loadBalancer:
        servers: [{ url: "http://api:8000" }]
        responseForwarding: { flushInterval: -1ms }   # flush SSE frames at once

  routers:
    perfin-internal:       # never forwarded; noop@internal answers without calling the API
      rule: Host(`perfin.example.com`) && PathRegexp(`(?i)^/(healthz|readyz|metrics)`)
      entryPoints: [websecure]
      priority: 1000
      service: noop@internal
      tls: {}
    perfin-upload:
      rule: Host(`perfin.example.com`) && (Path(`/api/v1/documents`) || Path(`/api/v1/documents/`))
      entryPoints: [websecure]
      priority: 300
      middlewares: [perfin-headers, perfin-limit-upload]
      service: perfin-api
      tls: {}
    perfin-import:
      rule: Host(`perfin.example.com`) && (Path(`/api/v1/import`) || Path(`/api/v1/import/`))
      entryPoints: [websecure]
      priority: 300
      middlewares: [perfin-headers, perfin-limit-import]
      service: perfin-api
      tls: {}
    perfin-events:
      rule: Host(`perfin.example.com`) && PathPrefix(`/api/v1/events`)
      entryPoints: [websecure]
      priority: 200
      middlewares: [perfin-headers, perfin-limit-default]     # no compress
      service: perfin-api
      tls: {}
    perfin:
      rule: Host(`perfin.example.com`)
      entryPoints: [websecure]
      priority: 100
      middlewares: [perfin-headers, perfin-limit-default, perfin-compress]
      service: perfin-api
      tls: {}
```

(Traefik answers `noop@internal` with status 418; any non-2xx works, the
point is that the request never reaches the API.) With either proxy, the
backend `PERFIN_TRUSTED_PROXIES` must contain the proxy container's address.
If your proxy runs somewhere else, or a CDN or second proxy sits in front of
Caddy, list those addresses under `trusted_proxies` in the Caddyfile as
described there, and never let the API be reachable except through the proxy.

## 7. The drop folder

The drop folder is a host directory the worker watches (inotify, with a
polling fallback). It is local filesystem access only; files are copied into
encrypted quarantine before anything parses them.

```sh
cd /opt/perfin/deploy
mkdir -p inbox processed
sudo chown 10001:10001 processed        # writable sibling, UID 10001 = the app user
sudo chmod 0755 inbox                    # the worker only reads it
```

How it is wired (already in `docker-compose.yml` and `.env.example`):

| Host path (`.env`) | In the worker | Mode | Purpose |
| --- | --- | --- | --- |
| `DROPFOLDER_HOST_INBOX=./inbox` | `/var/lib/perfin/inbox` (`PERFIN_DROPFOLDER_INBOX`) | read-only | where you (or a scanner, NAS or sync client) put files |
| `DROPFOLDER_HOST_PROCESSED=./processed` | `/var/lib/perfin/processed` (`PERFIN_DROPFOLDER_PROCESSED`) | read-write, owned by UID 10001 | refused files and move targets |

`PERFIN_DROPFOLDER_ENABLED=true` is the default. The worker runs as UID
10001, so the inbox files must be readable by that UID (mode `0644` or group
or ACL access) and `processed/` must be writable by it. Layout:
`inbox/<member username>/<account name>/statement.pdf` attributes a file to
that member and account; a file anywhere else asks "Who does this belong
to?" in the review queue. With several households set
`PERFIN_DROPFOLDER_HOUSEHOLD_ID`. A file is picked up when unchanged for
`PERFIN_DROPFOLDER_SETTLE_SECONDS` (10); if your writer is slow, set
`PERFIN_DROPFOLDER_READY_SIDECAR=true` and create `<name>.ready` after the
file is complete.

With the read-only inbox only `PERFIN_DROPFOLDER_AFTER=keep` works: taken
files stay where they are and are never taken twice. `move` or `delete`
need the inbox mounted read-write (drop `:ro` from its mount in
`docker-compose.yml`), which lets the worker change that directory; the
hardening checklist prefers `keep`. `perfin config check` (worker role)
warns about each mismatch, and refuses `processed/` inside the inbox.

## 8. Antivirus (optional)

`docker compose --profile av up -d` starts ClamAV; set
`PERFIN_ANTIVIRUS=clamav` in `.env`. Signature updates need
`database.clamav.net:443` in the egress allowlist (section 9). The default
fail mode is `closed`: if the scanner is down, documents wait and are
retried rather than skipped.

## 9. Egress profile: allowlisted outbound access

By default `api` and `worker` have no route to the internet. Anything that
must go out (email intake, OIDC, price feeds, notification channels,
ClamAV updates, HIBP) goes through the `egress` profile, a CONNECT-only
proxy that reaches only the hosts in `deploy/egress-allowlist.txt` and never
a private address.

1. Edit `deploy/egress-allowlist.txt`: one `host` (ports 443 and 993),
   `host:port` or `*.domain` per line.
2. In `.env`, uncomment
   `HTTPS_PROXY=http://egress:3128` and
   `NO_PROXY=localhost,127.0.0.1,db,api,clamav`. For a service on your LAN
   also set `EGRESS_ALLOW_PRIVATE_CIDRS=192.168.1.0/24` (and the matching
   `PERFIN_*_ALLOW_PRIVATE_*` switch of that feature).
3. `docker compose --profile egress up -d` (restart `egress` after every
   allowlist edit: `docker compose restart egress`).

What each integration sends is in [security/what-leaves-the-host.md](security/what-leaves-the-host.md).

## 10. Email intake

Receipts and statements can arrive by email, from a dedicated mailbox the
worker polls over IMAPS every 5 minutes.

1. Create a **dedicated** mailbox (not your personal one) with IMAP access
   and an app password. Put the IMAP host in the egress allowlist, for
   example `imap.example.com:993`, enable the egress profile (section 9) and
   set `HTTPS_PROXY` in `.env`. Without the proxy, a private (LAN) mail host
   is refused unless `PERFIN_EMAIL_INTAKE_ALLOW_PRIVATE_HOSTS=true`.
2. In `.env`: `PERFIN_EMAIL_INTAKE_ENABLED=true`; then
   `docker compose --profile egress up -d` to recreate `worker` and `api`.
3. As household owner or admin open **Settings > Intake**: enter host, port
   993, username and app password (stored column-encrypted, write-only),
   folder names, and the allowed senders. PerFin shows a secret **alias
   token**; mail must be addressed to it (as the local part or a `+tag` of
   the receiving address). Messages from other senders, without the token,
   or failing the SPF/DKIM requirement are moved to `PerFin/Rejected` and
   summarised in one daily review item. Rotate the token if it leaks.
4. Watch `docker compose logs worker` for the first poll. Certificate or TLS
   errors: [OPERATIONS.md](OPERATIONS.md).

Details and limits: [INGESTION.md](INGESTION.md) "Email intake specifics" and
[security/email.md](security/email.md).

## 11. The MCP role (optional AI access)

PerFin never calls a hosted model (the optional local model of
[ML.md](ML.md) runs on your own network and only suggests). The optional `mcp` container lets an assistant
you run pull read-only, scrubbed data. It is off by default.

1. As household owner create an AI client in **Settings > AI access**
   (step-up required), review the exposure preview, choose an expiry, and
   copy the `ai:read` token, which is shown once
   ([MCP_CLIENTS.md](MCP_CLIENTS.md)).
2. In `.env` set `PERFIN_MCP_ENABLED=true` and `MCP_API_TOKEN=<token>`. For
   the HTTP transport also set `MCP_HTTP_TOKEN=$(openssl rand -hex 32)` and
   `PERFIN_MCP_HTTP_ALLOWED_ORIGINS=https://perfin.example.com`.
3. `docker compose --profile mcp up -d`. The container receives only those
   variables (it does not read `.env`, so it never sees the database URL or
   keys, and refuses to start if it does).
4. HTTP only: uncomment the `handle /mcp*` block in the `Caddyfile` and
   `docker compose restart proxy`. Prefer stdio, which opens no listener:
   `docker compose run --rm -T -e PERFIN_MCP_TRANSPORT=stdio mcp`.
5. Token from a file instead of the environment (stdio on a workstation):
   `PERFIN_MCP_TOKEN_FILE=$HOME/.config/perfin-mcp-token` (mode 0600), as in
   MCP_CLIENTS.md.

Check with `PERFIN_ROLE=mcp perfin config check`. Disable by setting
`PERFIN_MCP_ENABLED=false` and `docker compose stop mcp`, or revoke the
client in Settings.

## 12. Updating

Pin the new version, read the release notes, back up, then pull and restart.
The complete procedure, including verifying signed images and rolling back,
is in [UPGRADING.md](UPGRADING.md). In short:

```sh
cd /opt/perfin && git fetch --tags && git checkout v1.0.1 && cd deploy
sed -i 's|^PERFIN_VERSION=.*|PERFIN_VERSION=1.0.1|' .env
scripts=../scripts; $scripts/backup.sh --compose-dir . --output-dir /srv/backups/perfin
docker compose pull && docker compose up -d
docker compose logs migrate | tail -3
```

## 13. Hardening checklist walk-through

Work through this list after the first boot and after every change to the
topology. Each item names the control in [SECURITY.md](SECURITY.md) and how to
check it.

1. **TLS, HSTS, redirect (F7, H7).** `curl -sI http://perfin.example.com/`
   answers a redirect to HTTPS; `curl -sI https://perfin.example.com/` shows
   `strict-transport-security`. Check the certificate chain and protocols
   with an external scanner; TLS 1.0/1.1 must be refused.
2. **Only the proxy publishes ports (H2).** `docker compose ps --format
   '{{.Service}} {{.Ports}}'` lists published ports for `proxy` only;
   `ss -tlnp` on the host shows nothing from `api`, `worker`, `db`, `mcp` or
   `egress`. Metrics stay on the container's loopback
   (`PERFIN_METRICS_BIND_HOST=127.0.0.1`).
3. **Master key separate (G2, G4).** The key is stored off the host and
   `PERFIN_MASTER_KEY_BACKUP_ACKNOWLEDGED=true`; `docker compose run --rm
   --no-deps api config check` prints no `master_key.*` warning. Prefer the
   key file of section 3, made from the existing key (never a newly
   generated one). The key never appears in a backup archive.
4. **MFA (A6, A7).** `PERFIN_REQUIRE_MFA=true` (default); every user has
   enrolled TOTP or a security key and holds recovery codes. If you use OIDC,
   enforce MFA at the IdP and only then set `PERFIN_OIDC_TRUST_IDP_MFA=true`.
5. **Drop folder read-only (C11).** `PERFIN_DROPFOLDER_AFTER=keep` with the
   inbox mounted `:ro`, `processed/` owned by UID 10001 and nothing else
   writable. Verify: `docker compose exec worker touch
   /var/lib/perfin/inbox/x` fails with "Read-only file system".
6. **Receipts mailbox dedicated (D1, D2, D5).** A mailbox used for nothing else,
   alias token set, allowed senders limited to household members,
   `require_auth_results` left on. Rotate the token after any suspicion.
7. **MCP off unless in use (E3, E4, M7).** `PERFIN_MCP_ENABLED=false` and no
   `mcp` container (`docker compose ps mcp` empty) unless you use it; review
   the exposure preview of each client; prefer an on-prem model if
   merchant-level detail must not leave the site; no `/mcp` block in the
   Caddyfile unless required.
8. **Backups encrypted and restore tested (G3).** A scheduled
   `scripts/backup.sh` run to storage on another machine, the passphrase
   stored apart from the archives, and a practised restore into a fresh stack
   with `perfin restore-check` ([BACKUP_RESTORE.md](BACKUP_RESTORE.md)).
   Failures reach you through a notification channel
   (`perfin notify`).
9. **Updates (H4).** Image tag pinned (`PERFIN_VERSION`), signature and
   provenance verified before each upgrade, a maintenance window, release
   notes read for advisories ([UPGRADING.md](UPGRADING.md)). Keep the host OS
   and Docker patched.
10. **Brute-force banning or no exposure (A1).** If the instance is reachable
    from the internet, add rate limiting at the proxy or fail2ban/CrowdSec on
    repeated 401/429; better, expose it only through a VPN or an Authentik
    forward-auth outpost. The API rate-limits logins by itself regardless.
11. **Container hardening (H1).** `docker compose config | grep -E
    'read_only|cap_drop|no-new-privileges'` shows the settings on every
    service; `docker compose exec api id` prints `uid=10001`. Do not run the
    stack with `--privileged` or extra capabilities.
12. **Database (H3).** No published port; `docker compose exec db psql -U
    postgres -c 'SHOW password_encryption'` returns `scram-sha-256`; the app
    role cannot bypass row-level security.
13. **Configuration gate (H6).** `docker compose run --rm --no-deps api
    config check` ends with `OK`; never set `PERFIN_DEV_MODE=true` or
    `PERFIN_LOG_LEVEL=DEBUG` on a real host.
14. **Antivirus and sandbox (C1, C9).** The worker starts without
    `landlock` complaints; with `PERFIN_ANTIVIRUS=clamav` the `clamav`
    container is healthy and `PERFIN_ANTIVIRUS_FAIL_MODE=closed`.
15. **Audit log (I4).** `docker compose run --rm --no-deps migrate audit
    verify` reports an intact chain; run it from cron or after restores.
16. **Encrypted disk (G1).** The volume holding `/var/lib/docker` (or the
    named volumes) sits on an encrypted device.
17. **What leaves the host.** Review
    [security/what-leaves-the-host.md](security/what-leaves-the-host.md)
    against your `.env`: every enabled integration should be one you meant to
    enable, with its host in the egress allowlist and nothing else in it.
