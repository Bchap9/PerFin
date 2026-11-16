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

**`edge`: the newest release, pre-releases included.** Every image also
carries `edge`, moved by the release workflow to each new release, release
candidates too, once all three images of that release passed their scan
(`scripts/move_edge.sh`). It moves for the three images together or not at
all, and by digest, so `edge` is the very image that was scanned and
signed: `cosign verify ghcr.io/bchap9/perfin:edge ...` checks the same
signature. A Compose deployment that uses `:edge` for `perfin`,
`perfin-egress` and `perfin-ml` gets each release by re-pulling
(`docker compose pull && docker compose up -d`), with no tag to edit.
What that costs: a pull is an upgrade, and migrations only move forward,
so **back up before every pull**, exactly as for a pinned upgrade
(below); and `docker compose up` after `pull` may start a release you
have not read the notes of. To go back, restore the backup and pin the
previous version's exact tag. The manual workflow `edge`
(Actions → edge → Run workflow, with a version) points `edge` at any
existing release: to start it, or to move it back after a bad release.

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

During an upgrade the old release keeps serving for a short while against
the migrated schema. With Helm, the old pods run until the rollout replaces
them. With Compose, the old `api`, `worker` and `mcp` containers keep running
while `migrate` runs and until `docker compose up -d` recreates them, usually
seconds. A release whose migration makes that overlap unsafe says so in its
notes. For such a release, or if you want no overlap at all, stop the old
release first: with Compose, run `docker compose stop api worker mcp` before
`docker compose up -d`; with Helm, scale the api, worker and MCP deployments
to zero before `helm upgrade`.

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

### 1.0.0 (not released yet)

- **QIF files imported before are not changed by the upgrade.** A QIF
  file's own `Opening Balance` record (or `Balance brought forward` and the
  like) was imported as a transaction, and a record made only of column
  names (`Description`, `Reference`, `Debit`) was posted quietly. 1.0.0
  reads both differently (extractor `qif/2`), but only when a file is read
  again: nothing is backfilled. To find them, open Inbox › Documents and
  look for file names ending in `.qif`; open each one and choose "Process
  again". The opening balance record then becomes the statement's opening
  balance and its transaction is removed, unless somebody changed that
  row: then it is kept and listed under "Rows from an earlier reading",
  where it can be removed. A record of column names keeps its date and
  amount and waits in the Inbox for review. A closing balance record is
  left out and counted in the document's warnings.
- **Some merchant keys clear themselves.** A line that leaves fewer than
  three letters and digits (`E`, `XY #0042`) and bare cash wording
  (`WITHDRAWAL`, `CASH DEPOSIT`, `TELLER DEPOSIT`, a line that starts with
  `ATM`) no longer have a merchant key, nor a cheque written with the
  `CHEQUE` or `CHQ` spelling (`CHEQUE #0042`, `CHQ 0042`, a bare `CHEQUE`
  or `CHECK`), which is now read as a cheque like `CHECK 0042`. Rows keyed before lose it in the
  refresh the hourly `categorise.dispatch` job queues after the upgrade
  (clean-up version 3, merchant list version 10); nothing needs to be run
  by hand, and an admin's "Check again" (Settings, What PerFin learned)
  runs it at once. Categories on those rows stay; what was learned under such a key
  no longer answers, and the cash suggestion of a withdrawal or a cash
  deposit stays. A merchant rule ("Always use…") on such a key stops
  applying, since no row carries the key any more: look in Settings ›
  Rules for a merchant rule of a withdrawal, a cash deposit, a cheque or a
  name of one or two letters, and replace it with a rule on the payee text
  if it is still wanted. A recurring series of such rows loses its key too, and a
  dismissed one stays dismissed as long as detection can tell it is the
  same: by its name or payee and an amount in its range (a dismissed
  series that was renamed may come back once as a suggestion). The same refresh suggests rent, water, sewer and tuition
  for more lines, and the next daily detection then shows a recurring
  series of such rows as a bill instead of "other", unless somebody set
  its kind.
- **An investment account's chart changes.** The balance history of a
  brokerage or retirement account (`GET /accounts/{id}/balances`, the
  account page's chart) is now cash plus holdings on each day, as the
  account's balance and net worth already were; the response shape is the
  same.

### 1.0.0-rc.11

The redesign: six sections and Settings, one Inbox, a drawer per
transaction, light and dark ([USING.md](USING.md)). No migration, no new
setting, no new container, no new outbound connection. One new read-only
endpoint, `GET /inbox/summary`, gives the Inbox count (API.md); nothing
was removed from the API and no response changed.

- **Every old address still works.** It forwards to its one new home and
  keeps what came after `?` and `#`, so bookmarks, links in old
  notifications and links in your own notes keep working. In short (the
  full table is in USING.md, "Where the old pages went"):

  | Old address | Now |
  | --- | --- |
  | `/review`, `/documents` | `/inbox` |
  | `/documents/library`, `/documents/<id>` | `/inbox/documents`, `/inbox/documents/<id>` |
  | `/documents/learned` | `/settings/learned` |
  | `/investments` | `/accounts/investments` |
  | `/loans`, `/assets` | `/accounts?group=loans`, `/accounts?group=property` |
  | `/loans/<id>`, `/assets/<id>` | the account's page, Schedule or Valuations tab |
  | `/subscriptions`, `/budgets`, `/goals` | `/plan/subscriptions`, `/plan/budgets`, `/plan/goals` |
  | `/calendar` | `/plan/upcoming` |
  | `/net-worth`, `/tax` | `/reports/net-worth`, `/reports/tax` |
  | `/tax/mapping` | `/settings/categories?tab=tax` |
  | `/rules` | `/settings/rules` |

  A bookmarked `/review?document=<id>` still opens that document's card.
  `/loans/<id>` and `/assets/<id>` look the object up first; if it was
  deleted, the page says so and links to Accounts.
- **The dashboard layout you customised is no longer used.** Home is one
  fixed page, and its customise dialog is gone. Nothing was deleted from
  the database: the `dashboard_layout` preference is still stored and still
  accepted by the API, and is simply not read. Budgets, goals, holdings,
  loans and missing tax documents, which could be dashboard widgets, are
  one click away in Plan, Accounts and Reports.
- **Fonts are served by PerFin itself** (Figtree and Bricolage Grotesque,
  OFL-1.1, bundled under `/static/`). The CSP keeps `font-src 'self'` and
  `style-src 'self'`; a strict proxy or egress policy needs nothing new,
  and the browser still loads nothing from another site
  ([what-leaves-the-host.md](security/what-leaves-the-host.md)).
- **One reload picks up the new look.** A tab left open across the upgrade
  keeps the old bundle until it is reloaded. The service worker caches only
  content-hashed files under `/static/` and never the page itself, so the
  reload fetches the new page and its new files; nothing needs to be
  cleared by hand. PerFin installed as an app on a phone or a desktop
  needs the same: reload it, or close it and open it again.
- **Habits that changed.** The category of a transaction is no longer a
  select in the list: open the row (its drawer) or select several rows and
  use the bar. "Check again" moved from Documents to Settings, What PerFin
  learned. Upload is at the top of the Inbox.
- **Rollback.** This release adds no revision, so the database stays at
  `0014`, the head of rc.10. Going back is the first case under
  "Rollback": set `PERFIN_VERSION` back (Compose) or `helm rollback`
  (Helm); no restore is needed. Take the backup before the upgrade all the
  same. In rc.10 the new addresses (`/inbox`, `/plan/...`) do not exist,
  so a bookmark made in rc.11 does not open its page there.

### 1.0.0-rc.10

Categorisation and subscriptions ([CATEGORISATION.md](CATEGORISATION.md)).
No new setting, no new container, no new outbound connection.

- Migration `0014` adds six columns to `transaction`, twelve to
  `scheduled_transaction`, the tables `merchant_memory` and
  `subscription_event`, and a unique constraint each on `rule` and
  `scheduled_transaction`. It is applied by `perfin migrate` like every
  revision ("Database migrations"). It writes to every transaction that
  has a category or a split and to every recurring item (one pass each, in
  plain SQL, which does not move `updated_at`), so on a large ledger it
  takes longer than the earlier revisions; take the backup first, as
  always. The new columns are nullable or have defaults, but two new checks
  refuse some writes of rc.9 and earlier: one ties
  `scheduled_transaction.active` to the new `status` column, the other
  allows a suggested category only on a row without a category.
- **Rolling upgrade from rc.9 or earlier: stop, then migrate.** While pods
  of rc.9 or earlier still run against the migrated database, switching a
  recurring item on or off, adding one switched off, or setting the
  category of a transaction that the new release has already given a
  suggestion fails with an error (nothing is half-written; the person
  tries again once the new pods serve). Adding transactions, categories
  and recurring items that are on, and categorising a row without a
  suggestion, keep working. With Compose, the old `api`, `worker` and
  `mcp` containers keep running while `migrate` runs and until
  `docker compose up -d` recreates them, usually seconds; for no window at
  all, run `docker compose stop api worker mcp` before
  `docker compose up -d`. With Helm, scale the api, worker and MCP
  deployments to zero before `helm upgrade` (see "Database migrations");
  otherwise the window lasts until the old api pods fail `/readyz` (it
  answers 503 once the database is past their head) and the rollout
  replaces them. An upgrade from rc.10 or later adds no revision and has
  no such window.
- **Your existing categories are kept, and never guessed over.** Every
  transaction that already had a category or a split is marked as
  categorised "before this version" (`legacy`). No merchant list, keyword
  or learned answer ever replaces such a category; a rule you re-apply may
  still change it, as before.
- **A background pass runs within an hour of the upgrade** (the hourly
  `categorise.dispatch` job queues one `categorise.refresh` per
  household; an owner or admin can start it at once with "Check again" on
  Documents, "What PerFin has learned"). Once, per household, it:
  - adds 13 default categories under the groups you still have (Fast
    food, Alcohol and bars, Food delivery, General merchandise, Home and
    garden, Games, News and books, Software and apps, Memberships, Other
    utilities, Home improvement, Financial and legal services, Cash and
    ATM). Nothing of yours is renamed or moved; a category of your own
    with the same name under the same group takes the key instead of
    getting a twin; a default you deleted earlier stays deleted. New
    households see "Streaming" where yours says "Streaming and
    subscriptions"; yours keeps its name;
  - works out the merchant of every transaction from the bank's wording
    (the wording itself is not changed);
  - counts the categories your transactions already have as what you
    taught, so the next charge from a merchant you always filed one way is
    filed that way from the first day;
  - guesses for the transactions that have **no** category, and only for
    those. A sure guess becomes the category and says where it came from;
    an unsure one is a suggestion you can accept.

  Until it has run, "How transactions got their category" says "Not up to
  date yet". It is audited as `categorise.refresh` with counts. If the
  number of uncategorised transactions is what you budget or report on,
  expect it to drop after the pass: reports count a guessed category like
  any other, and never a suggestion.
- **Recurring items keep working.** An item you entered by hand is
  "Confirmed"; one PerFin found and that is active is "Found for you"; one
  you switched off is "Paused". Until the next detection looks at them,
  money in is "income" and everything else "other"; the daily detection
  then says which are subscriptions and which are bills. The calendar and
  the budget forecast read the same items as before.
- **A deleted found item no longer returns.** Deleting a recurring item
  PerFin found now marks it "Not recurring" (dismissed) instead of
  removing it, so the next night's detection does not bring it back. Items
  you deleted **before** this release left no trace and can be found once
  more; dismiss them again and they stay away. `DELETE /recurring/{id}`
  still answers `204`; a script that relied on the row being gone should
  filter on `status`. A deleted hand-entered item is deleted, as before.
- **The first detection is quiet.** It records what it finds without a
  "new subscription" for everything you have been paying for years; only
  series that start from now on are announced. Detection is also stricter
  than before: weekly groceries, a coffee habit, fuel and transfers are no
  longer listed as recurring, and a found item with thin evidence is only
  suggested. An item the old detector created that the new one would not
  find stays as it is while its charges keep coming (dismiss it with "Not
  recurring" if it is a habit); one whose charges stopped long ago is
  marked ended without an alert.
- **Notifications.** Four kinds are new (a new subscription, a price
  change, a missed or stopped charge, a renewal within a week). A channel
  you already have keeps the kinds it was subscribed to, so it sends none
  of these until you tick them in Settings, Notifications. A new channel
  starts with all kinds.
- **"Always categorise like this" rules made before keep working.** They
  are ordinary rules and stay in your rules list, in their place. The old
  dialog is gone; its replacement, "Always categorise *merchant* as...",
  is in the same menu, is now offered to members too, and makes a rule on
  the merchant instead of on words of the description. Some rules made
  with the old dialog never matched anything (the reason it was replaced):
  the rules page shows how often each rule matched ("Matches"), and one
  at zero can be deleted.
- **Re-applying rules no longer overrides you.** `POST /rules/apply` (and
  "Apply rules") leaves the category of a transaction alone when a person
  chose it from this release on. Categories from before the upgrade are
  not known to be a person's and can still be changed by a rule, as
  before.
- **Archives are format 3** (PORTABILITY.md). An export made by this
  release cannot be imported into rc.9 or earlier
  (`archive-version-unsupported`); keep that in mind before a rollback.
  Format 1 and 2 archives still import, with their categories marked
  `legacy`. One old case does not, and did not before this release
  either: a format 2 archive written before migration `0009` (a release
  before 1.0.0-rc.3) lacks `account.subtype` and is refused whole with
  `archive-invalid-value`, importing nothing. Export it again from an
  instance at `0009` or later: restore the backup of that time, upgrade,
  export.
- **Rollback** is a restore of the pre-upgrade backup, as for every
  release ("Rollback"). What was recorded since (sources, suggestions,
  what was learned, decisions about subscriptions) goes with it.
- API: nothing was removed. Twelve routes, four transaction filters, seven
  transaction fields, four schedule fields and one bulk action are new
  (CHANGELOG.md, API.md). Changed behaviour a client may notice: `DELETE
  /recurring/{id}` on a detected series (above); `PATCH /recurring/{id}`
  with `active` sets the status as a person's decision; the import run
  summary's `rules_changed` counts rows a rule categorised, no longer rows
  a rule changed in any way; recurring patterns gain `two_monthly` and
  `half_yearly`. MCP: still 19 tools; `get_recurring` rows gain `kind` and
  `status`.

### 1.0.0-rc.9

- Migration `0013` adds the column `statement_record.verification` and the
  table `import_tombstone`. It is applied by `perfin migrate` like every
  revision ("Database migrations"), takes no setting and rewrites no
  ledger row; the column has a default and the table is new, so the pods
  of the previous release keep working during a rolling upgrade. Neither
  is part of a full export (PORTABILITY.md).
- **How earlier statements are marked.** `verification` of the statements
  imported before this release is filled in from what those imports left
  behind: a bank or card statement read from a PDF or image whose account
  has a statement balance from the same document is `reconciled`; an
  imported brokerage statement read from a PDF or image is `reconciled`;
  one a person imported as read, or whose "does not add up" finding was
  answered, is `accepted`; everything else is `unverified`. So a PDF or
  image statement imported before this release shows "Not checked against
  a balance" unless a balance recorded from it proves otherwise. Nothing
  about its rows changes. To check one, open the document and choose
  "Check against the statement". CSV, OFX, QFX and QIF imports are
  `unverified` and show no marker: they carry nothing to check.
- **Statements that print no balance no longer wait.** A PDF or image
  statement that reads cleanly and that nothing contradicts is imported
  as posted and marked "Not checked against a balance" (CHANGELOG.md,
  INGESTION.md "Commit"). Documents already waiting in Review for this
  reason alone stay there until they are read again: "Process again"
  imports them under the new rule. No balance is recorded from such a
  statement.
- **"Process again" now replaces rows.** Reading a document again removes
  the rows it produced earlier that the new reading no longer holds and
  that nobody touched, and keeps every row a person changed, listing
  them as "Rows from an earlier reading". Rows imported before this
  release that a rule changed during their import count as changed: they
  are kept and listed, and the reconcile view removes them in one step.
  Parser versions moved (`statement_generic/10`,
  `card_statement_generic/10`, the institution parsers `/8`,
  `layout_template/6`); a stored document keeps what it read until it is
  processed again.
- **A deleted imported row stays deleted** from this release on. Rows
  deleted before it left no trace, so a statement read again can still
  bring those back once; delete them again and they stay out.
- **A second upload of the same bytes is skipped** with a notice instead
  of waiting as a possible duplicate. Copies already waiting in Review
  keep their question. Scripts that upload through `POST /documents`
  should expect `status: "rejected"` for such a file (it was
  `needs_review`), with `duplicate_of` and `job_id: null` as before.
- API: nothing was removed. New endpoints and fields are listed in
  CHANGELOG.md ("Added", API). A client that lists the review queue can
  keep using `GET /review`; `GET /review/cards` groups the same items by
  document. No new settings.

### 1.0.0-rc.8

- No migration. One new setting, `PERFIN_PRICE_REFRESH_MINUTES` (how often
  the worker fetches prices and exchange rates): its default, 360, is the
  six hours the job ran at before, so nothing changes unless you set it.
- A broker's positions export (Fidelity's *Portfolio Positions* CSV and
  layouts like it) is now imported as what each account holds
  (INGESTION.md "Brokerage positions exports"). One uploaded before this
  release waits in the review queue as unparsed: use "Process again" on it.
  With both of a broker's exports, import the activity first, then the
  positions; the other order is held for review so that units are not
  counted twice.
- A statement or export whose account you chose by hand (its file prints no
  account digits) is no longer asked about again when its document is
  imported again. Choices made before this release are not remembered: the
  next re-run of such a document asks once more.
- `GET /investments/holdings` gained fields (`average_cost`,
  `price.source`, `price.updated_at`, `price_change`,
  `accounts[].positions_as_of`, `price_feed`); nothing was removed. New
  endpoint: `POST /investments/prices/refresh` (`investments:write`, member
  or above; `409 price-feed-disabled` without a price feed). Households get
  a `price.updated` event when fetched prices change.
- Loans: `POST /loans/{id}/payments` also takes `{"paid_on", "amount"}`
  and works out the split; `POST /loans` also takes `account` (`{"name",
  "type"}`) to open the loan's account with it, for which a token needs
  `accounts:write` too. `account_id` and a payment's full split are
  accepted as before.
- Loans: balances of a loan with a payment on its origination date were
  recorded as too low. They are rebuilt the next time the loan or one of
  its payments changes; nothing else is needed.

### 1.0.0-rc.7

- No migration and no new settings.
- Statement parsers changed (`brokerage_statement_generic` 8,
  `fidelity_investments_statement` 7, `placeholder_brokerage` 6,
  `statement_generic` and `card_statement_generic` 6, the institution
  layouts 4). Documents already imported are not touched. A document that is
  processed again (a held statement in the review queue, "Process again")
  is read by the new parsers and may be held differently: a brokerage
  statement that was held whole is now imported except for the lines or the
  security in doubt, and a bank statement that does not add up keeps only
  the rows its running balance does not vouch for as pending.
- Review items raised by an earlier release stay as they are. To get the
  new per-line items for a statement that is still held, use "Process
  again" on it.
- A brokerage account's first statement now also records the value the
  statement opens with, as a balance for the day before its period.
  Accounts that already have statements imported are not changed.
- A statement with a short position no longer imports that security's
  trades as ordinary purchases and sales. If an earlier import left a
  holding for a security you only hold short, remove it by hand.
- The local model (`ml` profile): the worker and the `perfin-ml` adapter
  must be the same release, since the adapter's `/v1/vocabulary` answer
  gained a `columns` list. A long statement now makes up to six calls to
  the model instead of one. `perfin ml eval` measures a model before you
  rely on its suggestions.
- New endpoint: `GET /documents/{id}/summary` (`documents:write`).
  `POST /review/{id}/resolve` accepts `lines` with `import_as_read`; the
  wording list may hold the kind `column`. No endpoint changed shape.

### 1.0.0-rc.6

- No migration and no new settings.
- Imports now give a row the payee whose name or alias its description
  contains, and apply a payee's default category to rows that neither the
  file nor a rule categorised. Until now a default category set under
  "Categories, payees and tags" had no effect, so check the ones you have
  set before the next import. Rows already imported are not changed.
- Several parser versions change (see the changelog); re-running the import
  of an existing document may read it differently: the account's last four
  digits (a number printed in groups now ends in its last group), the
  currency of a statement printed in pounds or euros, and one statement per
  share for a credit union statement that numbers none. A statement that
  matched an account by the wrong four digits before will ask for its
  account again.
- A CSV stored as "format not supported" by an earlier release because of
  lines above its header is read by "Process again" in the review queue.
- New endpoints: `GET /imports/layouts` and `DELETE /imports/layouts/{id}`
  (`documents:read` / `documents:write`). No endpoint changed shape.

### 1.0.0-rc.5

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
