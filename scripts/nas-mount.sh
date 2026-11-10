#!/bin/sh
# Mount NAS shares for services.nas-mounts: one run per launchd start.
#
# For each URL and mount point: leave a live SMB mount alone; otherwise create
# the (empty) mount point and mount the share with mount_smbfs -N, which takes
# the password from the login keychain, so none is on a command line or in a
# file. Shares are mounted soft (a dead server fails calls instead of hanging
# them) and nobrowse (kept out of Finder's sidebar).
#
# Usage: nas-mount.sh URL MOUNT_POINT [URL MOUNT_POINT ...]
#   URL           //user@server/share
#   MOUNT_POINT   absolute path; created if missing, never mounted over files
#
# Environment:
#   TIMEOUT        seconds allowed for each mount or stat (default 60)
#   TIMEOUT_CMD    a GNU-compatible timeout (default: timeout on PATH)
#   MOUNT_CMD      prints the mount table (default /sbin/mount); stand-in for tests
#   MOUNT_SMBFS    the mounter (default /sbin/mount_smbfs); stand-in for tests
#   MOUNT_DEVICE_CHECK  1 (default) also requires the mount point on a different
#                  device from its parent; tests set 0
# Exit status: 0 when every share is mounted, 1 when any is not, 2 on bad usage.
set -u
umask 077

fail_usage() {
  echo "nas-mount: $*" >&2
  exit 2
}

TIMEOUT=${TIMEOUT:-60}
case $TIMEOUT in '' | *[!0-9]* | 0) fail_usage "TIMEOUT must be a positive whole number, got '$TIMEOUT'" ;; esac
TIMEOUT_CMD=${TIMEOUT_CMD:-timeout}
MOUNT_CMD=${MOUNT_CMD:-/sbin/mount}
MOUNT_SMBFS=${MOUNT_SMBFS:-/sbin/mount_smbfs}
MOUNT_DEVICE_CHECK=${MOUNT_DEVICE_CHECK:-1}
[ $# -gt 0 ] && [ $(($# % 2)) -eq 0 ] || fail_usage "expected URL MOUNT_POINT pairs"

# Validate every pair before mounting anything.
n=0
for arg in "$@"; do
  if [ $((n % 2)) -eq 0 ]; then
    printf '%s\n' "$arg" | /usr/bin/grep -Eqx '//[A-Za-z0-9._-]+@[A-Za-z0-9.-]+/[A-Za-z0-9._-]+' \
      || fail_usage "URL must look like //user@server/share, got '$arg'"
  else
    case $arg in /?*) ;; *) fail_usage "mount point must be an absolute path, got '$arg'" ;; esac
    case "$arg/" in *'/../'* | *'/./'* | *'//'*) fail_usage "'$arg' must be a plain path" ;; esac
  fi
  n=$((n + 1))
done

log() {
  printf '%s %s\n' "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

run() {
  "$TIMEOUT_CMD" -k 5 "$TIMEOUT" "$@"
}

# mounted MOUNT_POINT: 0 when it is a live smbfs mount.
mounted() {
  table=$("$MOUNT_CMD" 2>/dev/null) || return 1
  printf '%s\n' "$table" | /usr/bin/grep -qF " on $1 (smbfs," || return 1
  if [ "$MOUNT_DEVICE_CHECK" = 1 ]; then
    mp_dev=$(run /usr/bin/stat -f %d "$1" 2>/dev/null) || return 1
    parent_dev=$(/usr/bin/stat -f %d "$(/usr/bin/dirname "$1")" 2>/dev/null) || return 1
    [ "$mp_dev" != "$parent_dev" ] || return 1
  fi
  return 0
}

status=0
while [ $# -gt 0 ]; do
  url=$1 mount_point=$2
  shift 2
  if mounted "$mount_point"; then
    continue
  fi
  if "$MOUNT_CMD" 2>/dev/null | /usr/bin/grep -qF " on $mount_point ("; then
    log "ERROR: $mount_point holds a mount that is not a live SMB mount of $url; leaving it alone"
    status=1
    continue
  fi
  if ! /bin/mkdir -p "$mount_point" 2>/dev/null || [ ! -d "$mount_point" ] || [ -L "$mount_point" ]; then
    log "ERROR: cannot use $mount_point as a mount point (not a plain directory)"
    status=1
    continue
  fi
  if [ -n "$(/bin/ls -A "$mount_point" 2>/dev/null)" ]; then
    log "ERROR: $mount_point is not empty; refusing to mount $url over it"
    status=1
    continue
  fi
  if out=$(run "$MOUNT_SMBFS" -N -o soft,nobrowse "$url" "$mount_point" </dev/null 2>&1); then
    log "Mounted $url on $mount_point"
  else
    log "ERROR: could not mount $url on $mount_point: $(printf '%s' "$out" | /usr/bin/head -n 3 | /usr/bin/tr '\n' ' ')"
    status=1
  fi
done
exit "$status"
