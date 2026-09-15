#!/usr/bin/env bash
#
# TimeNest - Avahi entrypoint
#
# Renders avahi-daemon.conf and the Bonjour service XML from env vars and
# launches avahi-daemon in the foreground. Because this runs in host
# network mode, advertisement reaches the LAN directly without NAT.
#
# The _adisk record must name the actual shares: TimeNest gives every user
# their own share, so one dkN record is rendered per fragment in
# /etc/timenest/shares.d. That directory is written by the samba container
# when users come and go, so we watch it and re-render on change.

set -euo pipefail

log() { printf '[avahi] %s\n' "$*"; }
die() { printf '[avahi] ERROR: %s\n' "$*" >&2; exit 1; }

: "${SERVER_NAME:=TimeNest}"
: "${DEVICE_MODEL:=TimeCapsule8,119}"
: "${AVAHI_INTERFACES:=}"
export SERVER_NAME DEVICE_MODEL AVAHI_INTERFACES

SHARES_DIR=/etc/timenest/shares.d
SERVICE_FILE=/etc/avahi/services/timenest.service

mkdir -p /etc/avahi/services

# shellcheck disable=SC2016  # single quotes intentional; envsubst reads literal ${VAR} list
envsubst '${AVAHI_INTERFACES}' \
    < /usr/local/share/timenest/avahi-daemon.conf.template \
    > /etc/avahi/avahi-daemon.conf

if [[ -n "$AVAHI_INTERFACES" ]]; then
    log "advertising on interfaces: ${AVAHI_INTERFACES}"
else
    log "advertising on all interfaces (set AVAHI_INTERFACES to restrict)"
fi

# One dk<N> record per share. adVN must be the share name, adVF=0x82 marks
# it as a Time Machine target. See `man 5 avahi.service` and Apple TN2154.
render_service() {
    local n=0 share records=""
    for conf in "$SHARES_DIR"/*.conf; do
        [[ -e "$conf" ]] || continue
        share="$(basename "$conf" .conf)"
        records+="    <txt-record>dk${n}=adVN=${share},adVF=0x82</txt-record>"$'\n'
        n=$((n + 1))
    done
    if (( n == 0 )); then
        log "no shares in ${SHARES_DIR} yet; advertising none"
        records="    <!-- no shares configured -->"$'\n'
    fi
    # shellcheck disable=SC2016  # single quotes intentional; envsubst reads literal ${VAR} list
    ADISK_VOLUMES="${records%$'\n'}" \
    envsubst '${SERVER_NAME} ${DEVICE_MODEL} ${ADISK_VOLUMES}' \
        < /etc/timenest/timenest.service.template \
        > "$SERVICE_FILE"
    log "rendered ${SERVICE_FILE} with ${n} share(s)"
}

shares_fingerprint() {
    # shellcheck disable=SC2012  # share names are validated [a-z0-9_-] by create-user.sh
    ls -1 "$SHARES_DIR" 2>/dev/null | sort | md5sum
}

render_service

# Strip out any pre-existing stock services so we don't double-advertise.
rm -f /etc/avahi/services/*.service.dpkg-* 2>/dev/null || true
for f in /etc/avahi/services/*.service; do
    case "$(basename "$f")" in
        timenest.service) : ;;
        *) rm -f "$f" ;;
    esac
done

log "advertising '${SERVER_NAME}' as model '${DEVICE_MODEL}'"

shutdown() {
    log "received shutdown signal, stopping avahi-daemon"
    kill -TERM "${WATCH_PID:-0}" 2>/dev/null || true
    kill -TERM "${AVAHI_PID:-0}" 2>/dev/null || true
    wait "${AVAHI_PID:-0}" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

# --no-drop-root is required when running as pid 1 inside a minimal
# container. --no-rlimits because tini+container already bounds us.
avahi-daemon \
    --no-drop-root \
    --no-rlimits \
    --file=/etc/avahi/avahi-daemon.conf &
AVAHI_PID=$!

# Re-render and reload when the samba container adds or removes a user.
# avahi-daemon re-reads /etc/avahi/services on SIGHUP.
(
    last="$(shares_fingerprint)"
    while sleep 30; do
        current="$(shares_fingerprint)"
        if [[ "$current" != "$last" ]]; then
            last="$current"
            render_service
            log "shares changed, reloading avahi-daemon"
            kill -HUP "$AVAHI_PID" 2>/dev/null || true
        fi
    done
) &
WATCH_PID=$!

wait "$AVAHI_PID"
