#!/usr/bin/env bash
#
# TimeNest - Samba entrypoint
#
# Renders /etc/samba/smb.conf from the template, seeds the passdb and the
# shares directory if this is a fresh install, then starts smbd in the
# foreground so tini can reap it.

set -euo pipefail

# shellcheck source=scripts/accounts.sh
source /usr/local/lib/timenest/accounts.sh

log() { printf '[samba] %s\n' "$*"; }
die() { printf '[samba] ERROR: %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------
mkdir -p /etc/samba /etc/timenest/shares.d /var/lib/samba/private /var/log/samba /backup

log "backing up to /backup (host BACKUP_PATH)"
log "advertising as '${SERVER_NAME:-TimeNest}' / model '${DEVICE_MODEL:-TimeCapsule8,119}'"

# ---------------------------------------------------------------------------
# Seed passdb on first boot so `net` commands do not complain.
# ---------------------------------------------------------------------------
if [[ ! -s /var/lib/samba/passdb.tdb ]]; then
    log "initializing fresh passdb"
    touch /var/lib/samba/smbpasswd
fi

# Unix accounts live in the container layer; recreate them from the
# persisted registry so tdbsam users can log in again.
host_group_ensure > /dev/null
accounts_restore
log "restored POSIX accounts: $(cut -d: -f1 "$ACCOUNTS_FILE" 2>/dev/null | xargs)"

# Render smb.conf from the template plus every share fragment. The script
# validates with testparm before publishing, so a bad fragment fails fast
# here with a readable error instead of looping the container.
# smbd is not up yet, so there is nothing to signal.
/usr/local/bin/render-smb-conf.sh --no-reload \
    || die "smb.conf failed to render; refusing to start"

# ---------------------------------------------------------------------------
# Handle SIGTERM cleanly - smbd's default is graceful on SIGTERM.
# ---------------------------------------------------------------------------
shutdown() {
    log "received shutdown signal, stopping smbd"
    kill -TERM "${SMBD_PID:-0}" 2>/dev/null || true
    wait "${SMBD_PID:-0}" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

log "starting smbd (foreground)"
smbd --foreground --debug-stdout --no-process-group --configfile=/etc/samba/smb.conf &
SMBD_PID=$!

wait "$SMBD_PID"
