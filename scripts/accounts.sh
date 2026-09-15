# shellcheck shell=bash
#
# POSIX account registry for TimeNest users.
#
# Samba's tdbsam passdb only stores password hashes; smbd still needs a
# matching unix user (getpwnam) to log someone in and for `force user`.
# /etc/passwd lives in the container layer and is lost on every recreate,
# so we keep `name:id` pairs in the persisted /var/lib/samba volume and
# replay them on start. Using a fixed id keeps ownership of /backup/<name>
# stable across recreates. Ids start at 10000 to stay clear of host users.
#
# Sourced by entrypoint-samba.sh, create-user.sh and delete-user.sh.

ACCOUNTS_FILE="${ACCOUNTS_FILE:-/var/lib/samba/timenest-accounts}"
ACCOUNTS_MIN_ID=10000
HOST_GROUP_NAME=tnhost

account_id() {
    awk -F: -v u="$1" '$1 == u { print $2 }' "$ACCOUNTS_FILE" 2>/dev/null
}

# Create the unix user and group for <name>, allocating an id if needed.
account_ensure() {
    local name="$1" num
    touch "$ACCOUNTS_FILE"
    num="$(account_id "$name")"
    if [[ -z "$num" ]]; then
        num="$(awk -F: -v m=$((ACCOUNTS_MIN_ID - 1)) '$2 > m { m = $2 } END { print m + 1 }' "$ACCOUNTS_FILE")"
        printf '%s:%s\n' "$name" "$num" >> "$ACCOUNTS_FILE"
    fi
    getent group "$name" >/dev/null || groupadd --gid "$num" "$name"
    id -u "$name" &>/dev/null || useradd --uid "$num" --gid "$num" \
        --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin "$name"
}

account_remove() {
    local name="$1"
    id -u "$name" &>/dev/null && userdel "$name"
    getent group "$name" >/dev/null && groupdel "$name"
    [[ -f "$ACCOUNTS_FILE" ]] && sed -i "/^${name}:/d" "$ACCOUNTS_FILE"
    return 0
}

# Group that owns the share contents, so a host user with this gid can read
# the backups. Falls back to the per-user group when HOST_READ_GID is unset.
# Echoes the group name, or nothing.
host_group_ensure() {
    local existing
    [[ -n "${HOST_READ_GID:-}" ]] || return 0
    existing="$(getent group "$HOST_READ_GID" | cut -d: -f1)"
    if [[ -z "$existing" ]]; then
        groupadd --gid "$HOST_READ_GID" "$HOST_GROUP_NAME"
        existing="$HOST_GROUP_NAME"
    fi
    printf '%s\n' "$existing"
}

# Recreate every registered account. Called once at container start.
accounts_restore() {
    local name
    [[ -f "$ACCOUNTS_FILE" ]] || return 0
    while IFS=: read -r name _; do
        [[ -n "$name" ]] && account_ensure "$name"
    done < "$ACCOUNTS_FILE"
}
