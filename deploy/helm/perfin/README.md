# PerFin Helm chart

Runs PerFin on Kubernetes with the same roles and hardening as
`deploy/docker-compose.yml`: the `api` and `worker` Deployments, an optional
read-only `mcp` Deployment, a `migrate` Job (Helm hook) and, optionally, a
single-instance PostgreSQL StatefulSet. Every option is documented in
[`values.yaml`](values.yaml); upgrades, rollback and release verification are
in [docs/UPGRADING.md](../../../docs/UPGRADING.md).

## Install

1. A PostgreSQL 16 database with the PerFin roles. Either create them on your
   server as `deploy/db-init/10-perfin-roles.sh` does (owner role
   `perfin_owner`, app role `perfin_app`, no superuser, no `BYPASSRLS`), or set
   `postgresql.enabled=true` and create its Secret:

   ```sh
   kubectl create namespace perfin
   kubectl -n perfin create secret generic perfin-db \
     --from-literal=POSTGRES_PASSWORD="$(openssl rand -hex 32)" \
     --from-literal=POSTGRES_OWNER_PASSWORD="$(openssl rand -hex 32)" \
     --from-literal=POSTGRES_APP_PASSWORD="$(openssl rand -hex 32)"
   ```

2. The application Secret. Each key is the environment variable name; each
   role receives only the keys it needs (`values.yaml`, "Secrets"). With the
   bundled database the host is `<release>-perfin-db` (`perfin-db` for a
   release named `perfin`):

   ```sh
   kubectl -n perfin create secret generic perfin-secrets \
     --from-literal=PERFIN_DATABASE_URL="postgresql+psycopg://perfin_app:<app password>@perfin-db:5432/perfin" \
     --from-literal=PERFIN_MIGRATION_DATABASE_URL="postgresql+psycopg://perfin_owner:<owner password>@perfin-db:5432/perfin" \
     --from-literal=PERFIN_MASTER_KEY="$(openssl rand -base64 32)" \
     --from-literal=PERFIN_SECRET_KEY="$(openssl rand -base64 48)"
   ```

   Back up `PERFIN_MASTER_KEY` separately: without it no document can be
   decrypted.

3. A values file, at least:

   ```yaml
   secrets:
     existingSecret: perfin-secrets
   config:
     PERFIN_PUBLIC_URL: https://perfin.example.com
     PERFIN_TRUSTED_PROXIES: 10.42.0.0/16   # your ingress controller's pod CIDR
   image:
     digest: sha256:...                      # verified with `cosign verify`
   postgresql:
     enabled: true
     existingSecret: perfin-db
   ingress:
     enabled: true
     className: nginx
     host: perfin.example.com
     tlsSecretName: perfin-tls
   ```

4. Install:

   ```sh
   helm install perfin oci://ghcr.io/bchap9/charts/perfin --version 1.0.0 \
     -n perfin -f my-values.yaml --wait --timeout 15m
   ```

## What the chart enforces

- Pods run as UID 10001 (PostgreSQL as 70) with `runAsNonRoot`, a read-only
  root filesystem, every capability dropped, `allowPrivilegeEscalation:
  false`, the `RuntimeDefault` seccomp profile and no service-account token.
- The chart never renders a Secret. A `config` key that looks like a secret,
  `image.tag=latest`, a non-https public URL, `0.0.0.0/0` as trusted proxy or
  an ingress without TLS fails the render.
- NetworkPolicies: default deny for every pod of the release; the worker has
  no ingress and reaches only DNS and the database; the mcp pod reaches only
  the api; the bundled database accepts only the api, worker and migrate pods.
  Extra destinations go in `networkPolicy.egressAllowlist`.

`backend/tests/unit/test_helm_chart.py` renders the chart and checks each of
these; CI also runs `helm lint --strict` and `kubeconform` on
`ci/*.yaml`.
