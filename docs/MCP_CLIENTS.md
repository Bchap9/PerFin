# Connecting an assistant to PerFin (MCP)

<!-- SPDX-License-Identifier: AGPL-3.0-or-later -->

PerFin contains **no language-model client** (ADR-0007). It offers a
read-only [MCP](https://modelcontextprotocol.io) server, `perfin mcp`, and an
assistant host that you run or choose connects to it. The host talks to the
model; PerFin never does. What the server may return, and what it never
returns, is specified in [AI_ACCESS.md](AI_ACCESS.md); what crosses a network
boundary is in [security/ai.md](security/ai.md).

> **Verification status (v0.7).** Checked against the shipped code: every
> environment variable and command below (`app/mcp/config.py`,
> `deploy/docker-compose.yml`, `deploy/.env.example`,
> `deploy/Caddyfile.example`) and the web page labels
> (`frontend/src/features/settings/ai/`). Run for real: the local
> `perfin mcp` with a token file over stdio against a live API from an `mcp`
> SDK client session (`backend/tests/security/mcp/test_mcp_live.py`, every
> tool), and **Claude Code 2.1** as the host: both `claude mcp add` stdio
> forms of section 4 (installed `perfin` and `uv run --project`) and the
> streamable-HTTP form with its bearer header reported *Connected* in
> `claude mcp list`, and a wrong bearer was refused with 401. **Not run
> here:** Claude Desktop (the config is the same `mcpServers` shape Claude
> Code accepted), the `docker exec` / `docker compose run` forms against the
> real image (the image build needs deb.debian.org, blocked in the build
> sandbox; the environment those forms produce is checked by
> `test_stdio_override_of_the_compose_service_needs_nothing_else`), and
> `mcphost` with Ollama (its flags were checked against its README).

## Overview

1. An **owner** creates an *AI client* in the web app (Settings > AI access).
   This yields a token with the single scope `ai:read`, shown once.
2. You give that token to the `perfin mcp` process (never to the model, never
   to the assistant host).
3. The assistant host starts or reaches `perfin mcp` over **stdio** (default,
   same machine or over SSH) or **streamable HTTP** (through the reverse
   proxy, with TLS, a bearer secret and an origin allowlist).

`perfin mcp` holds only that token. It has no database access, no master key
and no documents. Every answer is scrubbed twice and audited (AI_ACCESS.md).
There are 19 read-only tools, listed by the host once connected;
`explain_data_policy` tells the assistant (and you) the client's mode and
options.

## 1. Create the client and review the exposure preview

Requires the household **owner**, a browser session and a fresh step-up
(re-authentication) for create and revoke.

1. Open **Settings > AI access** and, in the **AI clients** card of the
   **Clients** tab, choose **New client**. A three-step wizard opens
   (*Basics*, *Options and preview*, *Review*); the API behind it is
   `POST /settings/ai/clients` and `POST /settings/ai/preview`.
2. *Basics*: name the client (for example "Claude Desktop, laptop"), choose
   the transport (stdio or http) and an **expiry** (mandatory).
3. Choose the **mode**:
   - *Detail* (default): transaction-level financial facts.
   - *Aggregates*: totals and trends only, no transactions, payees or
     nicknames. A good first step.
4. Choose options: round amounts to the nearest 1, 10 or 100; limit the date
   range the client may query; hide account nicknames ("Checking A"); hide
   merchant location. Optionally add custom denylist terms (an ex-employer, a
   child's school) and member aliases.
5. *Options and preview*: review the **exposure preview** (it refreshes
   as you change options). It runs every tool on your real data with
   the chosen options, before anything is saved, and highlights each
   placeholder the scrubber inserted (`[ACCT]`, `[MEMBER]`, `[ADDRESS]` and so
   on). Change options until it shows what you are willing to share. The
   **Scrubbed vs original** tab lets you try denylist terms on your own text.
   Nothing is stored by the preview.
6. *Review*, then **Create client** (step-up prompt). The token is displayed
   **once**, with a copy button and a setup snippet that never contains the
   token. Copy it to a file only you can read, for example:

   ```sh
   umask 077
   printf '%s' 'PASTE_TOKEN_HERE' > ~/.config/perfin-mcp-token
   ```

   PerFin keeps only a hash; a lost token means revoking the client and
   creating another.

While any client exists the web app shows a persistent banner (M5).

## 2. Environment of `perfin mcp`

| Variable | Meaning |
|---|---|
| `PERFIN_MCP_ENABLED` | must be `true`, otherwise `perfin mcp` refuses to start |
| `PERFIN_MCP_API_URL` | API base URL (`http://api:8000` in compose) |
| `PERFIN_MCP_TOKEN` or `PERFIN_MCP_TOKEN_FILE` | the client's `ai:read` token (exactly one; a file should be mode 0600) |
| `PERFIN_MCP_TRANSPORT` | `stdio` (default) or `http` |
| `PERFIN_MCP_HTTP_HOST`, `PERFIN_MCP_HTTP_PORT` | HTTP listener (`127.0.0.1:8001` by default) |
| `PERFIN_MCP_HTTP_ALLOWED_ORIGINS` | explicit `https` origins; required for `http` |
| `PERFIN_MCP_HTTP_TOKEN` or `_FILE` | bearer secret the assistant host sends; 32+ characters, different from the API token; required for `http` |
| `PERFIN_MCP_RATE_PER_MINUTE` | tool calls per minute (default 30) |

In `deploy/.env` the compose service reads `PERFIN_MCP_ENABLED`,
`MCP_API_TOKEN` (becomes `PERFIN_MCP_TOKEN`), `MCP_HTTP_TOKEN`,
`PERFIN_MCP_HTTP_ALLOWED_ORIGINS` and `MCP_RATE_PER_MINUTE`. The process
**refuses to start** if its environment holds a database URL, the master key,
the secret key or another credential (M7): do not run it from a shell that
sourced the full `.env`. Check an environment with
`PERFIN_ROLE=mcp perfin config check`.

Enable the container with `PERFIN_MCP_ENABLED=true`, the token in `.env`, and
`docker compose --profile mcp up -d`. Generate the HTTP secret with
`openssl rand -hex 32`.

## 3. Claude Desktop (stdio)

Claude Desktop starts the server itself from `claude_desktop_config.json`
(Settings > Developer > Edit Config). Both options keep the token on the
machine that runs PerFin or in a file only you read.

**Option A: into the running stack, with `docker exec -i`** (PerFin on the
same machine or reached by `ssh host docker exec ...`). The container already
holds the token and the enable flag from `.env`; the exec overrides the
transport:

```json
{
  "mcpServers": {
    "perfin": {
      "command": "docker",
      "args": [
        "exec", "-i",
        "-e", "PERFIN_MCP_TRANSPORT=stdio",
        "perfin-mcp-1",
        "perfin", "mcp"
      ]
    }
  }
}
```

`perfin-mcp-1` is the compose container name for project `perfin`
(`name: perfin` in `deploy/docker-compose.yml`), service `mcp` (check with
`docker ps`). Over SSH, use `"command": "ssh"` with
`"args": ["you@perfin-host", "docker", "exec", "-i", "-e",
"PERFIN_MCP_TRANSPORT=stdio", "perfin-mcp-1", "perfin", "mcp"]`.
The exec needs no further variables: with `PERFIN_MCP_TRANSPORT=stdio` the
HTTP settings of the container are ignored, and the exec'd process has the
container's token and API URL. The container itself only runs while its
HTTP transport is fully configured (`MCP_HTTP_TOKEN` and
`PERFIN_MCP_HTTP_ALLOWED_ORIGINS` in `.env`), because the compose service
always serves HTTP to the proxy. If you want stdio only, start a one-off
container per session instead, which needs only `PERFIN_MCP_ENABLED=true`
and `MCP_API_TOKEN` in `.env`:

```json
"args": ["compose", "--project-directory", "/path/to/PerFin/deploy",
         "--profile", "mcp", "run", "--rm", "-T", "--no-deps",
         "-e", "PERFIN_MCP_TRANSPORT=stdio", "mcp"]
```

(with `"command": "docker"`; `-T` keeps stdio a plain pipe, `--no-deps`
assumes the api is already running).

**Option B: a local `perfin mcp` with a token file** (PerFin's backend
installed on the machine, for example with `uv`; API reachable at a URL you
trust). Keep the environment clean; do not export database variables:

```json
{
  "mcpServers": {
    "perfin": {
      "command": "uv",
      "args": ["run", "--project", "/path/to/PerFin/backend", "perfin", "mcp"],
      "env": {
        "PERFIN_MCP_ENABLED": "true",
        "PERFIN_MCP_API_URL": "https://perfin.example.com",
        "PERFIN_MCP_TOKEN_FILE": "/home/you/.config/perfin-mcp-token",
        "PERFIN_MCP_TRANSPORT": "stdio"
      }
    }
  }
}
```

The `uv run` form needs the backend's environment synced once
(`uv sync --project /path/to/PerFin/backend`); the installed console script
(`/path/to/PerFin/backend/.venv/bin/perfin`) works as the command too.

Restart Claude Desktop. The 19 PerFin tools appear in the tools menu; ask
"call explain_data_policy" to see the client's mode and options.

## 4. Claude Code

**stdio:**

```sh
claude mcp add perfin -- docker exec -i -e PERFIN_MCP_TRANSPORT=stdio perfin-mcp-1 perfin mcp
```

or with a local install and a token file:

```sh
claude mcp add perfin \
  -e PERFIN_MCP_ENABLED=true \
  -e PERFIN_MCP_API_URL=https://perfin.example.com \
  -e PERFIN_MCP_TOKEN_FILE=$HOME/.config/perfin-mcp-token \
  -e PERFIN_MCP_TRANSPORT=stdio \
  -- uv run --project /path/to/PerFin/backend perfin mcp
```

**Streamable HTTP through the proxy.** Prepare once:

1. In `.env`: `PERFIN_MCP_TRANSPORT` is `http` for the container (compose
   already sets it), `MCP_HTTP_TOKEN=$(openssl rand -hex 32)`,
   `PERFIN_MCP_HTTP_ALLOWED_ORIGINS=https://perfin.example.com` (explicit
   `https` origins; no `*`). Requests with no `Origin` header (command-line
   hosts such as Claude Code) are accepted if the bearer is right; a request
   with an `Origin` outside the list is refused with 403.
2. In the Caddyfile, uncomment the `handle /mcp*` block that proxies to
   `mcp:8001` (`deploy/Caddyfile.example`), then reload the proxy.
3. Add the server:

```sh
claude mcp add --transport http perfin https://perfin.example.com/mcp \
  --header "Authorization: Bearer $(cat ~/.config/perfin-mcp-http-token)"
```

The bearer is `MCP_HTTP_TOKEN` (the secret between the host and
`perfin mcp`); it is not the `ai:read` token, which stays inside the
container. Verify with `claude mcp list`: each PerFin entry should read *Connected*;
a wrong bearer reads *Failed to connect* with HTTP 401. Prefer stdio when the assistant
is on a machine you can reach by SSH; HTTP puts one more listener on the
network.

## 5. An on-prem model through an MCP host (Ollama)

Ollama serves models; it does not speak MCP. To use a local model you run an
**MCP host or bridge**: a program that connects to MCP servers, offers their
tools to the model through the model's tool-calling interface, and runs the
loop. PerFin sees only the host's MCP connection, exactly as for Claude
Desktop. **PerFin does not talk to the model, does not know it exists, and
has no setting for it.** Any host that supports MCP stdio or streamable HTTP
and a tool-capable local model works.

One concrete open-source example is
[`mcphost`](https://github.com/mark3labs/mcphost), a command-line host with
Ollama support. Its repository is archived (its authors point to a
successor, Kit); any other MCP-capable local host is equivalent. The flags
and keys below follow its README (checked 2026-11-04; not run here, there
is no Ollama in the build sandbox). Its config (`~/.mcphost.yml`) registers
the server as a local stdio command:

```yaml
mcpServers:
  perfin:
    type: local
    command: ["docker", "exec", "-i", "-e", "PERFIN_MCP_TRANSPORT=stdio", "perfin-mcp-1", "perfin", "mcp"]
```

and the model is chosen on the host's side (`provider/model`):

```sh
ollama pull qwen2.5:14b          # any tool-capable model
mcphost -m ollama/qwen2.5:14b --config ~/.mcphost.yml
```

Data path: PerFin API -> `perfin mcp` -> host -> Ollama on your network. Keep
Ollama bound to a private address. Smaller local models call tools less
reliably than hosted ones; if answers look wrong, ask the host to show tool
calls, and use `explain_data_policy` to confirm what the model can see.

## 6. Audit, revocation and the kill switch

- **Audit.** Settings > AI access, **Calls** tab, lists every call per client: tool,
  parameters, status, size, and the response exactly as sent. Search the
  stored responses for a string (a surname, the last 4 of a card) to confirm
  it never left (`GET /settings/ai/calls?client_id&contains`). Every call that
  reaches an AI view is recorded.
- **Revoke.** Settings > AI access, **Clients** tab, **Revoke** on the
  client's row (step-up) ends the client and its
  token immediately; the next call answers 401/403. Do this for a lost
  laptop or a leaked config file. Tokens also expire on their own.
- **Kill switch.** Two levels, both answer `403 ai-access-disabled` on every
  AI view and refuse new clients, without revoking existing ones:
  the *household* switch (owner, step-up, `PATCH /settings/ai`) and the
  *instance* switch (instance admin, step-up, `PATCH /settings/system/ai`).
  Both are in the **Kill switch** card at the top of Settings > AI access
  ("Switch off for this household"; "Engage the instance kill switch", shown
  to instance administrators only).
  Operators can additionally stop the container
  (`docker compose --profile mcp stop mcp`) or set `PERFIN_MCP_ENABLED=false`.
- **Rate limits.** 60 calls a minute and 300 per ten minutes per client at the
  API; `PERFIN_MCP_RATE_PER_MINUTE` at the process.

## 7. Hosted or on-prem

The same denylist applies to every client; the choice is about *how much
financial detail* leaves your host.

- **Hosted assistant** (Claude and others): what the tools return is sent to
  the provider as part of the conversation. Identity never is, but merchant
  names, amounts and dates are themselves revealing. Start with Aggregates
  mode, or Detail with rounding and hidden nicknames; widen deliberately. The
  provider's retention and training terms apply to what you send; PerFin
  cannot see or change them.
- **On-prem model:** no further restriction is needed, and nothing about your
  finances leaves your network. This is the private option.

## 8. What leaves the host

PerFin's own code sends nothing to a model or an AI service. Data leaves only
when **you** connect an assistant, and then only what the `ai:read` views
return after the policy and both scrubs, to the host you configured.
Documents, extracted text, OCR, previews, credentials and all identity
(names, addresses, DOB, government ids, any account or card number including
last 4) are not reachable. See [security/ai.md](security/ai.md) for the
complete list and threat notes.
