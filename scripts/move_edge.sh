#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Point the moving `edge` tag of every PerFin image at one release
# (docs/UPGRADING.md "Image tags").
#
#   scripts/move_edge.sh 1.0.0-rc.9
#
# `edge` is the newest release, pre-releases included: a deployment that
# follows it gets each release by re-pulling, with no tag to edit. It moves
# for all images together or not at all:
#
#   1. every image must already have the version tag (the release workflow
#      adds it only after the Trivy scan passed); one missing, nothing moves;
#   2. each image is copied by digest (skopeo --preserve-digests), so `edge`
#      is the very manifest list that was scanned, signed and attested, and
#      `cosign verify ...:edge` checks the same signature;
#   3. the digest `edge` resolves to afterwards is compared with the source.
#
# Needs skopeo logged in to ghcr.io with packages:write (the release
# workflow's `edge` job, or the manual `edge` workflow). IMAGE_NAMESPACE
# overrides ghcr.io/bchap9 for a test registry.
set -euo pipefail

usage() { echo "usage: scripts/move_edge.sh <version>, e.g. 1.0.0-rc.9" >&2; exit 64; }
[ "$#" -eq 1 ] || usage
version="$1"
[[ "$version" =~ ^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[0-9A-Za-z]+(\.[0-9A-Za-z]+)*)?$ ]] || usage

namespace="${IMAGE_NAMESPACE:-ghcr.io/bchap9}"
images=(perfin perfin-egress perfin-ml)

# The digest of what a reference resolves to: sha256 of the raw manifest
# bytes, piped straight from skopeo (a command substitution would strip a
# trailing newline and change the hash).
manifest_digest() {
    local sum
    sum=$(skopeo inspect --raw "docker://$1" | sha256sum) || return 1
    printf 'sha256:%s\n' "${sum%% *}"
}

declare -A digest=()
for name in "${images[@]}"; do
    if ! digest[$name]=$(manifest_digest "${namespace}/${name}:${version}"); then
        echo "move_edge: ${namespace}/${name}:${version} not found; edge not moved" >&2
        exit 1
    fi
done

for name in "${images[@]}"; do
    skopeo copy --all --preserve-digests --quiet \
        "docker://${namespace}/${name}@${digest[$name]}" "docker://${namespace}/${name}:edge"
    moved=$(manifest_digest "${namespace}/${name}:edge")
    if [ "$moved" != "${digest[$name]}" ]; then
        echo "move_edge: ${name}:edge resolves to ${moved}, expected ${digest[$name]}" >&2
        exit 1
    fi
    echo "${namespace}/${name}:edge -> ${version} (${digest[$name]})"
done
