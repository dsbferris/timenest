#!/usr/bin/env bash
#
# Container healthcheck for the Samba service.
#
# Three checks, cheapest and most diagnostic first. Each on its own so the
# failure message in `docker inspect` says which layer broke:
#
#   1. smb.conf still parses. render-smb-conf.sh validates before it
#      publishes, but a hand-edited fragment or a half-written mount can
#      still leave a config smbd would refuse on its next reload.
#   2. Something accepts connections on 445. smbstatus alone is not enough:
#      it only reads tdb files and keeps succeeding after smbd is gone.
#   3. smbstatus runs. Catches a listener that is up but whose tdb state is
#      unreadable, which is what a wedged or half-restored volume looks like.

set -uo pipefail

SMB_PORT="${SMB_PORT:-445}"

fail() { printf '[healthcheck] %s\n' "$*" >&2; exit 1; }

testparm -s /etc/samba/smb.conf >/dev/null 2>&1 \
    || fail "smb.conf does not parse"

# `interfaces` in the template always includes lo, so loopback is reachable
# even when SMB_INTERFACES pins smbd to one LAN interface.
timeout 3 bash -c "</dev/tcp/127.0.0.1/${SMB_PORT}" 2>/dev/null \
    || fail "nothing listening on ${SMB_PORT}"

timeout 5 smbstatus -b >/dev/null 2>&1 \
    || fail "smbstatus failed"

exit 0
