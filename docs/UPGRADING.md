# Upgrading PerFin

This guide covers how PerFin is versioned, how to verify a release before you
run it, and how to upgrade and roll back a Docker Compose or a Helm
(Kubernetes) installation. It covers SECURITY.md row H4 (signed images, SBOMs,
provenance) for the operator's side. Backups are described in
[BACKUP_RESTORE.md](BACKUP_RESTORE.md); read it first if you have never taken
one.

## Versioning policy

PerFin uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). A
release is a git tag `vMAJOR.MINOR.PATCH` (pre-releases: `v1.1.0-rc.1`). The
tag, `backend/pyproject.toml`, `app.__version__`, `frontend/package.json` and
the Helm chart's `version` and `appVersion` always carry the same number; the
release workflow refuses a tag where they differ
(`scripts/release_notes.py check`).

| Change | Meaning for you |
| --- | --- |
| **PATCH** (1.0.0 to 1.0.1) | Bug and security fixes. No new settings are required. Database migrations are possible but only additive. |
| **MINOR** (1.0 to 1.1) | New features. Migrations are possible. New settings have safe defaults; existing `.env` files and Helm values keep working. |
| **MAJOR** (1.x to 2.0) | May need action: a removed or renamed setting, a changed default, a new required secret, or a migration that cannot run online. The release notes have an "Upgrade notes" section with the steps. |

Upgrade one MAJOR version at a time (1.x to 2.x, then 2.x to 3.x). Within a
MAJOR version you can skip releases: the migrations of every skipped release
run in order.

Images are published to `ghcr.io/bchap9/perfin` (and the optional egress
proxy to `ghcr.io/bchap9/perfin-egress`, the optional local-model adapter to
`ghcr.io/bchap9/perfin-ml`) with the tags `1.2.3`, `1.2` and `1`
(and `latest` for the newest non-pre-release). The Helm chart is published to
`oci://ghcr.io/bchap9/charts/perfin`. Pin an exact version, or better a
digest, in production; never run `latest`. The chart refuses
`image.tag=latest`.

## Before every upgrade

1. **Read the release notes** of every release between your version and the
   target, on the GitHub release page. Each one is the `CHANGELOG.md` section
   of that version. Look in particular for:
   - a **Security** section. A fixed vulnerability is described there with
     its advisory (GitHub Security Advisory / CVE) and the versions it
     affects. If you are affected, upgrade promptly and follow any extra
     steps it lists (for example rotating a secret or revoking tokens).
     Advisories are also published under the repository's *Security* tab;
     watch the repository with "Security alerts" to be told about new ones.
   - **Upgrade notes** (MAJOR releases), new required settings, and
     **Changed** entries that alter behaviour you rely on.
2. **Verify the release** you are about to run (next section).
3. **Back up**, with the version you are running now:
   `scripts/backup.sh --compose-dir deploy --output-dir /srv/backups/perfin`
   (Compose; see [BACKUP_RESTORE.md](BACKUP_RESTORE.md) for the bare-metal
   mode and passphrase handling). Keep the master key's backup at hand: a
   rollback restores this archive, and it needs the same key.
   On Kubernetes, see [Backups on Kubernetes](#backups-on-kubernetes).

## Verifying a release

Every image is signed with [cosign](https://github.com/sigstore/cosign)
keyless signing: the signature is bound to the GitHub Actions workflow
`.github/workflows/release.yml` of `Bchap9/PerFin` at the release tag, through
Sigstore's OIDC certificate authority, and recorded in the public Rekor
transparency log. There is no long-lived signing key to steal. The image also
carries an SBOM (CycloneDX, as a signed cosign attestation and as a GitHub
SBOM attestation) and SLSA build provenance (GitHub attestation). The image is
tagged only after it passed a Trivy scan with no fixable HIGH or CRITICAL
vulnerability.

Verify the image signature (cosign 2.x or 3.x):

```sh
VERSION=1.0.0
cosign verify "ghcr.io/bchap9/perfin:${VERSION}" \
  --certificate-identity "https://github.com/Bchap9/PerFin/.github/workflows/release.yml@refs/tags/v${VERSION}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

The output names the verified digest (`sha256:...`). Deploy that digest, not
the tag: `APP_IMAGE`/`PERFIN_VERSION` cannot express a digest in Compose, so
on Compose either set `PERFIN_VERSION` to the version and check that
`docker compose images` shows the verified digest, or pin it in a
`docker-compose.override.yml` (`image: ghcr.io/bchap9/perfin@sha256:...`). In
Helm, set `image.digest`.

Verify the SBOM attestation and the build provenance:

```sh
cosign verify-attestation --type cyclonedx "ghcr.io/bchap9/perfin:${VERSION}" \
  --certificate-identity "https://github.com/Bchap9/PerFin/.github/workflows/release.yml@refs/tags/v${VERSION}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com > /dev/null

# GitHub CLI: SLSA provenance and the SBOM attestation
gh attestation verify "oci://ghcr.io/bchap9/perfin:${VERSION}" --repo Bchap9/PerFin
gh attestation verify "oci://ghcr.io/bchap9/perfin:${VERSION}" --repo Bchap9/PerFin \
  --predicate-type https://spdx.dev/Document/v2.3
```

The release page also carries, for each image and platform
(`linux-amd64`, `linux-arm64`), the SBOM files
`perfin-<version>-linux-<arch>.cdx.json` (CycloneDX) and `.spdx.json` (SPDX),
each with a Sigstore bundle `<file>.sigstore.json`, the packaged chart
`perfin-<version>.tgz` with its bundle, and `SHA256SUMS`. Verify a downloaded
file with:

```sh
sha256sum --check --ignore-missing SHA256SUMS
cosign verify-blob perfin-1.0.0-linux-amd64.cdx.json \
  --bundle perfin-1.0.0-linux-amd64.cdx.json.sigstore.json \
  --certificate-identity "https://github.com/Bchap9/PerFin/.github/workflows/release.yml@refs/tags/v1.0.0" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

You can feed the SBOM to your own scanner (for example
`trivy sbom perfin-1.0.0-linux-amd64.cdx.json` or `grype sbom:...`) to check
it against advisories published after the release.

The Helm chart is signed the same way:

```sh
cosign verify "ghcr.io/bchap9/charts/perfin:${VERSION}" \
  --certificate-identity "https://github.com/Bchap9/PerFin/.github/workflows/release.yml@refs/tags/v${VERSION}" \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

If any verification fails, do not deploy the image; open a security report as
described in [SECURITY.md](https://github.com/bchap9/perfin/blob/main/SECURITY.md).

## Database migrations

Schema changes ship as Alembic revisions inside the image. They are applied
by `perfin migrate`, which runs as the owner role (`perfin_owner`, with
`PERFIN_MIGRATION_DATABASE_URL`) and is the only step that can change the
schema; the api and worker connect as `perfin_app`, which cannot.

- **Compose:** the one-shot `migrate` service runs on every
  `docker compose up -d`; `api` and `worker` start only after it exited
  successfully (`service_completed_successfully`).
- **Helm:** the `<release>-migrate` Job is a `pre-upgrade` hook, so the
  schema is migrated before the new api and worker pods roll out. On a first
  install it is `pre-install` (external database) or `post-install` (bundled
  PostgreSQL).
- Until the database is at the migration head, the api answers `/readyz`
  with 503 and is not sent traffic.

During a Helm rolling upgrade the old pods keep serving for a short while
against the migrated schema. A release whose migration makes that unsafe says
so in its notes; for such a release, or if you want no overlap at all, scale
the api and worker to zero before `helm upgrade` (Compose always stops the old
containers before it starts the new ones).

**Downgrades are not supported.** `perfin migrate` only moves forward, and an
older api reports not ready against a newer schema (`/readyz` stays at 503). To go
back, restore the backup you took before upgrading (see [Rollback](#rollback)).

## Upgrading a Docker Compose installation

From the repository checkout that holds `deploy/`:

```sh
cd /opt/perfin
git fetch --tags
git checkout v1.0.1                  # the deploy files of the new release
cd deploy
# Variables in the new .env.example that your .env does not set yet:
comm -13 <(grep -o '^[A-Z_]*=' .env | sort) <(grep -o '^[A-Z_]*=' .env.example | sort)
```

Add any new variable from `.env.example` that the release notes call
required, then set the version and pull:

```sh
sed -i 's/^PERFIN_VERSION=.*/PERFIN_VERSION=1.0.1/' .env
docker compose pull                  # api, worker, migrate, mcp share the image
docker compose up -d                 # migrate runs first, then api and worker
docker compose ps                    # api healthy, migrate exited 0
docker compose logs migrate          # "database is at the migration head"
```

`docker compose up -d` recreates only the containers whose image or
configuration changed. Profiles you use must be named again
(`docker compose --profile egress --profile mcp up -d`). If you build the
image locally instead of pulling, run `docker compose build --pull` first.

Check after the upgrade:

```sh
docker compose exec api perfin config check
docker compose exec api perfin healthcheck
```

## Upgrading a Helm installation

Check what will change, then upgrade:

```sh
VERSION=1.0.1
helm show values oci://ghcr.io/bchap9/charts/perfin --version "$VERSION" > values-new.yaml
diff -u <(helm get values perfin -n perfin --all) values-new.yaml   # new or renamed values

# Pin the digest you verified with `cosign verify` above.
helm upgrade perfin oci://ghcr.io/bchap9/charts/perfin --version "$VERSION" \
  -n perfin -f my-values.yaml \
  --set image.digest=sha256:<verified digest> \
  --wait --timeout 15m
```

Without a registry you can install from a checkout of the release tag:
`helm upgrade perfin deploy/helm/perfin -n perfin -f my-values.yaml ...`.

`helm upgrade` first runs the migrate Job (pre-upgrade hook). If it fails,
the upgrade stops before any Deployment changes and the old pods keep
serving; read its log with
`kubectl -n perfin logs job/perfin-migrate` (the Job is kept on failure and
deleted after it succeeded). Then the api and worker roll out; `--wait`
returns when the new api pods are ready.

Notes on the chart:

- Secrets are never part of the chart or of `helm get values`: they live in
  the Secrets named by `secrets.existingSecret`, `postgresql.existingSecret`
  and `mcp.existingSecret`. A release that needs a new secret key says so;
  add it to the Secret before upgrading.
- The documents and quarantine PersistentVolumeClaims carry
  `helm.sh/resource-policy: keep`, so `helm uninstall` does not delete the
  encrypted documents.
- The NetworkPolicies need a CNI that enforces them. After an upgrade that
  adds an integration, add its destination to
  `networkPolicy.egressAllowlist.<role>`; nothing else has a route out.
  Each rule needs a non-empty `to` list, and `networkPolicy.ingressFrom`,
  `networkPolicy.dns.peers` and `externalDatabase.networkPeers` must not be
  empty: an empty peer list matches every pod or address, so the chart
  refuses to render it.

### Backups on Kubernetes

`scripts/backup.sh` has a Compose and a bare-metal mode, not a Kubernetes
one. Before a Helm upgrade, take:

1. a database backup with your database service's own mechanism (managed
   PostgreSQL snapshot or point-in-time recovery), or a `pg_dump -Fc` as a
   superuser or `BYPASSRLS` role (the PerFin tables use
   `FORCE ROW LEVEL SECURITY`, so `perfin_owner` cannot dump them); with the
   bundled PostgreSQL:
   `kubectl -n perfin exec perfin-db-0 -- sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dump --no-password -Fc -U postgres perfin' > perfin.dump`
   (the superuser can log in over the pod's local socket only, as in
   Compose);
2. a snapshot of the `perfin-documents` and `perfin-quarantine`
   PersistentVolumeClaims (CSI `VolumeSnapshot`) taken right after the dump.

Keep the master key from `secrets.existingSecret` backed up separately, as for
every installation.

## Rollback

Because migrations only move forward, a rollback is a restore:

- **The upgrade failed in `migrate`** (Compose: `migrate` exited non-zero; Helm:
  the hook Job failed). The new schema was not committed: each revision runs
  in its own transaction, so the database is at the last revision that
  succeeded. If that is still the old head, set `PERFIN_VERSION` back (Compose)
  or `helm rollback perfin -n perfin` (Helm) and you are done. If some
  revisions of the new release were applied, restore as below.
- **The new version runs but misbehaves.** Restore the backup taken before
  the upgrade into a fresh stack running the **old** version:
  - Compose: set `PERFIN_VERSION` to the old version in `.env`, then follow
    [BACKUP_RESTORE.md, "Restoring"](BACKUP_RESTORE.md#restoring)
    (`scripts/restore.sh --compose-dir deploy <archive>`; it refuses a
    non-empty target unless you pass `--force`).
  - Helm: `helm rollback perfin <previous revision> -n perfin`, then restore
    the database dump and the volume snapshots taken before the upgrade
    while the api and worker are scaled to zero
    (`kubectl -n perfin scale deploy/perfin-api deploy/perfin-worker --replicas=0`).

Data entered after the upgrade is lost by a restore; export it first if you
need it ([PORTABILITY.md](PORTABILITY.md)).

## Notes for specific releases

### Unreleased

- Migration `0010` adds the `statement_record` table (the statement-level
  duplicate guard) and `0011` the nullable `parser_fingerprint.template`
  column (layouts remembered by "Teach PerFin"). Both are additive and run
  on their own during the upgrade. Statements imported before `0010` are not
  backfilled: they are protected by the row-level duplicate checks only, so
  uploading one of them again is deduplicated row by row, not held as
  `statement_overlap`.
- Migration `0012` lets `security.symbol` be NULL (securities a statement
  prints by their name alone) and turns the `(household, symbol, exchange)`
  unique constraint into a unique index over securities that have a symbol.
  No data changes. Downgrading below `0012` needs every security to have a
  symbol again. API clients must accept `"symbol": null` in securities and
  holdings.
- No new settings. Several parser versions change (see the changelog);
  re-running the import of an existing document may read it differently,
  usually more rows and more accounts.

### 1.0.0-rc.4

- No migration and no new settings. `DELETE /documents/{id}` keeps its
  behaviour by default (imported rows stay, unlinked); `?with_data=true` is
  new and opt-in.

### 1.0.0-rc.3

- Migration `0009` adds the nullable `account.subtype` column. It is
  additive and runs on its own during the upgrade, like every migration.
- New optional settings `PERFIN_ML_URL`, `PERFIN_ML_TOKEN`,
  `PERFIN_ML_TIMEOUT_SECONDS` and `PERFIN_ML_MAX_CHARS` default to off;
  existing `.env` files keep working. `PERFIN_ML_URL`, when set, must be a
  local address or the application refuses to start.
- The `perfin-ml` image and the Ollama runtime only start with the `ml` or
  `ml-cuda` Compose profile ([ML.md](ML.md)). The worker joins a new internal
  `ml` network either way; `docker compose up -d` creates it.

## Reporting problems

Upgrade bugs: open an issue with the from and to versions and the `migrate`
log. Security problems: follow [SECURITY.md](https://github.com/bchap9/perfin/blob/main/SECURITY.md), never a public
issue.
