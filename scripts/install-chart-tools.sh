#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Install pinned Helm and kubeconform binaries for the chart checks
# (deploy/helm/perfin; ci.yml job `helm`, backend/tests/unit/test_helm_chart.py)
# and the release workflow. Usage:
#
#   scripts/install-chart-tools.sh <bin-dir>
#
# Both tools are built from their source modules, fetched through the Go
# module proxy, and each module's content hash is checked twice: by the Go
# checksum database (sum.golang.org, on by default) and against the hash
# pinned below (`go mod download -json <module>@<version>` -> "Sum"). A
# changed or re-tagged upstream release therefore fails the install instead
# of producing a different binary. Requires Go and python3; GOTOOLCHAIN=auto
# fetches the toolchain a go.mod asks for, itself checksum-verified.
set -euo pipefail

HELM_VERSION="v3.19.0"
HELM_MODULE_SUM="h1:krVyCGa8fa/wzTZgqw0DUiXuRT5BPdeqE/sQXujQ22k="
KUBECONFORM_VERSION="v0.7.0"
KUBECONFORM_MODULE_SUM="h1:ZFfniR8VChrWQxaxTUGnNrxw8RIDkjVBrjdhXSamwjw="

bin_dir="${1:?usage: install-chart-tools.sh <bin-dir>}"
mkdir -p "$bin_dir"
bin_dir="$(cd "$bin_dir" && pwd)"

if [[ -n "${GONOSUMDB:-}${GONOSUMCHECK:-}${GOFLAGS:-}${GOINSECURE:-}" || "${GOSUMDB:-}" == "off" ]]; then
  echo "install-chart-tools: refusing to run with GONOSUMDB, GONOSUMCHECK, GOFLAGS or GOINSECURE set, or GOSUMDB=off" >&2
  exit 1
fi

verify_module() { # <module>@<version> <expected h1 sum>
  local sum
  sum="$(go mod download -json "$1" | python3 -I -c 'import json, sys; print(json.load(sys.stdin)["Sum"])')"
  if [[ "$sum" != "$2" ]]; then
    echo "install-chart-tools: module hash mismatch for $1: got $sum, expected $2" >&2
    exit 1
  fi
}

verify_module "helm.sh/helm/v3@${HELM_VERSION}" "$HELM_MODULE_SUM"
verify_module "github.com/yannh/kubeconform@${KUBECONFORM_VERSION}" "$KUBECONFORM_MODULE_SUM"

GOBIN="$bin_dir" CGO_ENABLED=0 go install -trimpath \
  -ldflags "-s -w -X helm.sh/helm/v3/internal/version.version=${HELM_VERSION}" \
  "helm.sh/helm/v3/cmd/helm@${HELM_VERSION}"
GOBIN="$bin_dir" CGO_ENABLED=0 go install -trimpath -ldflags "-s -w" \
  "github.com/yannh/kubeconform/cmd/kubeconform@${KUBECONFORM_VERSION}"

"$bin_dir/helm" version --short
"$bin_dir/kubeconform" -v
