# PerFin documentation

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
