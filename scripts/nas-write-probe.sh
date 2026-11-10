#!/bin/sh
# NAS write probe: one run per launchd start (services.nas-storage-health).
#
# For each share, checks it is a live SMB mount, then writes a small file into
# the probe directory, reads it back, compares and deletes it. A failed attempt
# is retried once after RETRY_DELAY seconds, so a passing blip is not reported.
# Every step runs under a timeout, because a stuck share can block a file call
# for minutes. The result goes to METRICS_DIR/nas-write-probe.prom for
# node_exporter's textfile collector.
#
# Why a write: a storage pool that has turned itself read-only can still be
# reported writable by the server's own metrics. Only a real write shows it.
#
# Usage: nas-write-probe.sh NAME MOUNT_POINT DIR [NAME MOUNT_POINT DIR ...]
#   NAME          label for the share in the metrics ([A-Za-z0-9_-]+)
#   MOUNT_POINT   where the share is mounted; must be an smbfs mount
#   DIR           directory on the share to write the probe file in; it must
#                 already exist (the probe never creates directories)
#
# Environment:
#   METRICS_DIR    required; directory the .prom file is written to
#   TIMEOUT        seconds allowed per file step (default 60)
#   RETRY_DELAY    seconds before the one retry (default 30)
#   TIMEOUT_CMD    a GNU-compatible timeout (default: timeout on PATH)
#   MOUNT_CMD      prints the mount table (default /sbin/mount); stand-in for tests
#   MOUNT_DEVICE_CHECK  1 (default) also requires MOUNT_POINT on a different
#                  device from its parent; tests set 0
#   NOW            epoch seconds, for tests
set -u
umask 022

fail_usage() {
  echo "nas-write-probe: $*" >&2
  exit 2
}

is_uint() {
  case ${1:-} in
    '' | *[!0-9]*) return 1 ;;
  esac
  return 0
}

METRICS_DIR=${METRICS_DIR:-}
case $METRICS_DIR in /*) ;; *) fail_usage "METRICS_DIR must be an absolute path, got '$METRICS_DIR'" ;; esac
TIMEOUT=${TIMEOUT:-60}
RETRY_DELAY=${RETRY_DELAY:-30}
is_uint "$TIMEOUT" && [ "$TIMEOUT" -gt 0 ] || fail_usage "TIMEOUT must be a positive whole number, got '$TIMEOUT'"
is_uint "$RETRY_DELAY" || fail_usage "RETRY_DELAY must be a whole number, got '$RETRY_DELAY'"
TIMEOUT_CMD=${TIMEOUT_CMD:-timeout}
MOUNT_CMD=${MOUNT_CMD:-/sbin/mount}
MOUNT_DEVICE_CHECK=${MOUNT_DEVICE_CHECK:-1}
[ $# -gt 0 ] && [ $(($# % 3)) -eq 0 ] || fail_usage "expected NAME MOUNT_POINT DIR triples"

# Validate every triple before probing anything.
n=0
for arg in "$@"; do
  case $((n % 3)) in
    0)
      case $arg in
        '' | *[!A-Za-z0-9_-]*) fail_usage "share name must match [A-Za-z0-9_-]+, got '$arg'" ;;
      esac
      ;;
    1 | 2)
      case $arg in
        /?*) ;;
        *) fail_usage "mount point and directory must be absolute paths, got '$arg'" ;;
      esac
      case "$arg/" in *'/../'* | *'/./'* | *'//'*) fail_usage "'$arg' must be a plain path" ;; esac
      ;;
  esac
  n=$((n + 1))
done
n=0
for arg in "$@"; do
  case $((n % 3)) in
    1) mp=$arg ;;
    2)
      case "$arg/" in
        "$mp"/*) ;;
        *) fail_usage "directory '$arg' must be inside its mount point '$mp'" ;;
      esac
      ;;
  esac
  n=$((n + 1))
done

log() {
  printf '%s %s\n' "$(/bin/date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

# run SECONDS CMD...: the command under the timeout; SIGKILL 5 s after SIGTERM.
run() {
  "$TIMEOUT_CMD" -k 5 "$TIMEOUT" "$@"
}

# mounted MOUNT_POINT: 0 when MOUNT_POINT is a live smbfs mount.
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

# attempt DIR: write, read back, compare and remove one probe file. On failure
# prints the failed step on stdout and returns 1.
attempt() {
  file="$1/.nas-write-probe.$$"
  token="nas-write-probe $(/bin/date -u +%Y-%m-%dT%H:%M:%SZ) $$ $(/usr/bin/od -An -N8 -tx1 /dev/urandom | /usr/bin/tr -d ' \n')"
  if ! run /bin/sh -c 'printf "%s\n" "$2" > "$1"' probe "$file" "$token" 2>/dev/null; then
    run /bin/rm -f "$file" >/dev/null 2>&1
    echo "write"
    return 1
  fi
  readback=$(run /bin/cat "$file" 2>/dev/null)
  status=$?
  if [ "$status" -ne 0 ] || [ "$readback" != "$token" ]; then
    run /bin/rm -f "$file" >/dev/null 2>&1
    if [ "$status" -ne 0 ]; then echo "read back"; else echo "compare"; fi
    return 1
  fi
  if ! run /bin/rm -f "$file" 2>/dev/null; then
    echo "remove"
    return 1
  fi
  return 0
}

/bin/mkdir -p "$METRICS_DIR" || exit 1
metrics_tmp=$METRICS_DIR/.nas-write-probe.prom.$$
probe_file=
# A run stopped part-way (launchd unloading it, a logout) removes what it left.
cleanup() {
  /bin/rm -f "$metrics_tmp"
  [ -n "$probe_file" ] && run /bin/rm -f "$probe_file" >/dev/null 2>&1
}
trap cleanup EXIT
trap 'exit 143' TERM
trap 'exit 130' INT HUP
results=

while [ $# -gt 0 ]; do
  name=$1 mount_point=$2 dir=$3
  shift 3
  is_mounted=0
  success=0
  started=$(/bin/date +%s)
  try=1
  while :; do
    if mounted "$mount_point"; then
      is_mounted=1
      if ! run /bin/test -d "$dir" 2>/dev/null; then
        step="find the directory $dir"
      else
        # attempt cleans up after itself; the trap covers a run stopped inside it.
        probe_file="$dir/.nas-write-probe.$$"
        step=$(attempt "$dir")
        ok=$?
        probe_file=
        if [ "$ok" -eq 0 ]; then
          success=1
          break
        fi
      fi
    else
      is_mounted=0
      step="find $mount_point mounted (smbfs)"
    fi
    [ "$try" -ge 2 ] && break
    try=2
    /bin/sleep "$RETRY_DELAY"
  done
  duration=$(($(/bin/date +%s) - started))
  if [ "$success" -eq 1 ]; then
    [ "$try" -eq 2 ] && log "$name: write probe passed on the retry"
  else
    log "$name: write probe failed twice; could not $step"
  fi
  results="$results$name $is_mounted $success $duration
"
done

NOW=${NOW:-$(/bin/date +%s)}
{
  echo "# HELP nas_write_probe_success Whether the last write probe wrote, read back and removed a file on the share."
  echo "# TYPE nas_write_probe_success gauge"
  printf '%s' "$results" | while read -r name is_mounted success duration; do
    echo "nas_write_probe_success{share=\"$name\"} $success"
  done
  echo "# HELP nas_write_probe_mounted Whether the share was a live SMB mount at the last probe."
  echo "# TYPE nas_write_probe_mounted gauge"
  printf '%s' "$results" | while read -r name is_mounted success duration; do
    echo "nas_write_probe_mounted{share=\"$name\"} $is_mounted"
  done
  echo "# HELP nas_write_probe_duration_seconds How long the last probe of the share took, retry included."
  echo "# TYPE nas_write_probe_duration_seconds gauge"
  printf '%s' "$results" | while read -r name is_mounted success duration; do
    echo "nas_write_probe_duration_seconds{share=\"$name\"} $duration"
  done
  echo "# HELP nas_write_probe_last_run_timestamp_seconds When the write probe last finished."
  echo "# TYPE nas_write_probe_last_run_timestamp_seconds gauge"
  echo "nas_write_probe_last_run_timestamp_seconds $NOW"
} >"$metrics_tmp" && /bin/mv -f "$metrics_tmp" "$METRICS_DIR/nas-write-probe.prom"
