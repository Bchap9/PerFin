#!/bin/sh
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Entrypoint for the optional `clamav` service (profile `av`, SECURITY.md C9)
# with a read-only root filesystem (H1).
#
# The image's own /init cannot run read-only: it re-links /var/lock and edits
# /etc/clamav/*.conf in place (`set -e`, so the container restarts forever;
# found by the v1.0.0 verification run, docs/RELEASE_CHECKLIST.md). This
# script does the same job without writing outside the tmpfs mounts and the
# signature volume:
#
#   * FRESHCLAM_CONF_<Option>=<value> and CLAMD_CONF_<Option>=<value>
#     (the image's documented convention, e.g. FRESHCLAM_CONF_HTTPProxyServer=
#     egress and FRESHCLAM_CONF_HTTPProxyPort=3128 for the egress profile) are
#     applied to copies of the configuration files in /tmp;
#   * without any signature database yet, freshclam downloads one first; if
#     that fails and no database is present, the container exits (clamd
#     cannot start without signatures) and Compose retries;
#   * freshclam then runs as a daemon (unless CLAMAV_NO_FRESHCLAMD=true) and
#     clamd runs in the foreground as PID 1's child, as user clamav.
set -eu

conf_copy() { # conf_copy SRC DEST PREFIX
    cp "$1" "$2"
    env | grep "^$3" | while IFS="=" read -r key value; do
        option="${key#"$3"}"
        sed -i "/^#\{0,1\}$option /d" "$2"
        printf '%s %s\n' "$option" "$value" >>"$2"
    done
}

chown -R clamav:clamav /var/lib/clamav
conf_copy /etc/clamav/freshclam.conf /tmp/freshclam.conf FRESHCLAM_CONF_
conf_copy /etc/clamav/clamd.conf /tmp/clamd.conf CLAMD_CONF_
# The image logs to /var/log/clamav (a tmpfs in docker-compose.yml) and
# notifies clamd through its own configuration file: point it at the copy.
sed -i 's|^NotifyClamd .*|NotifyClamd /tmp/clamd.conf|' /tmp/freshclam.conf
chown clamav:clamav /tmp/freshclam.conf /tmp/clamd.conf

has_db() {
    for f in /var/lib/clamav/*.cvd /var/lib/clamav/*.cld /var/lib/clamav/*.hdb \
        /var/lib/clamav/*.hsb /var/lib/clamav/*.ndb /var/lib/clamav/*.ldb; do
        [ -f "$f" ] && return 0
    done
    return 1
}

if ! has_db; then
    echo "perfin clamav: no signature database yet, running freshclam"
    sed -e 's|^NotifyClamd .*||' -e '$a TestDatabases no' /tmp/freshclam.conf >/tmp/freshclam-initial.conf
    freshclam --foreground --stdout --config-file=/tmp/freshclam-initial.conf || true
    rm -f /tmp/freshclam-initial.conf
    has_db || {
        echo "perfin clamav: no signature database could be downloaded" >&2
        exit 1
    }
fi

if [ "${CLAMAV_NO_FRESHCLAMD:-false}" != "true" ]; then
    freshclam --config-file=/tmp/freshclam.conf --checks="${FRESHCLAM_CHECKS:-1}" \
        --daemon --foreground --stdout --user=clamav &
fi

exec clamd --foreground --config-file=/tmp/clamd.conf
