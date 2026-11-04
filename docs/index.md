# PerFin documentation

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

PerFin is a self-hosted, security-first personal finance application: ledger,
budgets, investments, loans, net worth and a tax vault, fed by uploads, a
watched drop folder and a receipts mailbox. No telemetry and no model calls
inside the app. This site is built from the `docs/` directory of the
repository and contains nothing that is not in git.

## Run it

1. [Deploying PerFin](DEPLOYMENT.md): one Linux host, Docker Compose, secrets,
   the master key, reverse proxy, drop folder, email intake, the MCP role and
   the hardening checklist.
2. [Operating PerFin](OPERATIONS.md): logs, health, metrics, jobs and dead
   letters, and the fixes for the usual failures.
3. [Upgrading](UPGRADING.md) and [Backup and restore](BACKUP_RESTORE.md).

## Trust it

- [Threat model and controls](SECURITY.md) and the live
  [control status](security/CONTROLS.md).
- [What leaves the host](security/what-leaves-the-host.md): one page per
  integration, starting from "nothing, by default".

## Understand it

[Vision](VISION.md), [architecture](ARCHITECTURE.md), [data model](DATA_MODEL.md),
[authentication](AUTH.md), [ingestion](INGESTION.md), [AI access](AI_ACCESS.md),
[API](API.md) and the [decision records](adr/README.md).

## Licence

AGPL-3.0-or-later. Reporting a vulnerability: see `SECURITY.md` in the
repository root.
