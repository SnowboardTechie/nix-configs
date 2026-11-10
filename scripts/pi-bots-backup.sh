#!/usr/bin/env bash
# Nightly encrypted backup of the bot service's state to the NAS
# (services.pi-bots-backup).
#
# What it takes: the service's state directory (each bot's Matrix key store,
# bridge journal, sync position and Pi session; SQLite databases through
# SQLite's online backup, each quick_check'd) and its secrets directory
# (recovery keys, access tokens, the owner file and the application-service
# registration). Not taken: sockets, lock files, SQLite -wal/-shm/-journal
# files (the online backup already holds their contents) and the metrics
# directory. Bot directories are in Git and are not taken.
#
# Everything goes into one tar, compressed with zstd and encrypted with age to
# AGE_RECIPIENTS (public keys only) on local disk, so nothing readable leaves
# the Studio. The age identity that decrypts it never goes to the NAS.
#
# Safety rules, as in hermes-nas-backup.sh:
# - The destination must sit under MOUNT_POINT, and MOUNT_POINT must be a live
#   SMB mount (mount table plus a device-id check), so an unmounted share never
#   turns into a "backup" on the local disk.
# - The NAS only receives a finished archive, written to .partial-<stamp>,
#   read back and checksummed, then renamed to <stamp>.
# - Retention deletes only directories named exactly like a stamp, and only
#   after a new backup is published.
# - Every step on the share runs under a timeout: a stuck share fails the run
#   instead of hanging it.
# - With AGE_IDENTITY set, the archive is decrypted locally and listed before
#   it is published, so a backup that cannot be opened is a failed run.
#
# Each run writes METRICS_DIR/pi-bots-backup.prom for node_exporter: success
# of the last run, when it ran, when a backup last succeeded and its size.
# Prometheus alerts on a failed or overdue backup.
#
# Configuration comes from the environment (the Nix module sets all of it).
set -euo pipefail
umask 077

STATE_DIR=${STATE_DIR:?STATE_DIR is required}
SECRETS_DIR=${SECRETS_DIR:?SECRETS_DIR is required}
MOUNT_POINT=${MOUNT_POINT:?MOUNT_POINT is required}
DEST_DIR=${DEST_DIR:?DEST_DIR is required}
STAGING_DIR=${STAGING_DIR:?STAGING_DIR is required}
METRICS_DIR=${METRICS_DIR:?METRICS_DIR is required}
AGE_RECIPIENTS=${AGE_RECIPIENTS:?AGE_RECIPIENTS is required}
AGE_IDENTITY=${AGE_IDENTITY:-}
KEEP=${KEEP-14}
TIMEOUT=${TIMEOUT:-300}
SQLITE=${SQLITE:-/usr/bin/sqlite3}
ZSTD=${ZSTD:-zstd}
AGE=${AGE:-age}
TIMEOUT_CMD=${TIMEOUT_CMD:-timeout}
MOUNT_CMD=${MOUNT_CMD:-/sbin/mount}
MOUNT_DEVICE_CHECK=${MOUNT_DEVICE_CHECK:-1}

STAMP=${STAMP:-$(date -u +%Y%m%dT%H%M%SZ)}
STAMP_RE='^[0-9]{8}T[0-9]{6}Z$'
ARCHIVE=pi-bots-state.tar.zst.age
METRICS_FILE="$METRICS_DIR/pi-bots-backup.prom"
LOCK_DIR="$STAGING_DIR/lock"
WORK="$STAGING_DIR/work-$STAMP"
PARTIAL="$DEST_DIR/.partial-$STAMP"
FINAL="$DEST_DIR/$STAMP"
stage="starting"
locked=0
published=0
partial_made=0
archive_bytes=0

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { stage="$1"; log "ERROR: $1" >&2; exit 1; }
run() { "$TIMEOUT_CMD" -k 5 "$TIMEOUT" "$@"; }

# The last successful backup's time survives failed runs: read it back from
# the previous metrics file.
previous_success() {
  [ -f "$METRICS_FILE" ] || return 0
  sed -n 's/^pi_bots_backup_last_success_timestamp_seconds \([0-9][0-9]*\)$/\1/p' "$METRICS_FILE" | head -n 1
}

write_metrics() {
  local success="$1" now last_success
  now=$(date +%s)
  last_success=$(previous_success)
  [ "$success" = 1 ] && last_success=$now
  mkdir -p "$METRICS_DIR"
  chmod 755 "$METRICS_DIR"
  {
    echo "# HELP pi_bots_backup_success Whether the last bot service backup run published an archive to the NAS."
    echo "# TYPE pi_bots_backup_success gauge"
    echo "pi_bots_backup_success $success"
    echo "# HELP pi_bots_backup_last_run_timestamp_seconds When the bot service backup last finished, successful or not."
    echo "# TYPE pi_bots_backup_last_run_timestamp_seconds gauge"
    echo "pi_bots_backup_last_run_timestamp_seconds $now"
    if [ -n "$last_success" ]; then
      echo "# HELP pi_bots_backup_last_success_timestamp_seconds When a bot service backup was last published to the NAS."
      echo "# TYPE pi_bots_backup_last_success_timestamp_seconds gauge"
      echo "pi_bots_backup_last_success_timestamp_seconds $last_success"
    fi
    if [ "$success" = 1 ]; then
      echo "# HELP pi_bots_backup_archive_bytes Size of the last published archive."
      echo "# TYPE pi_bots_backup_archive_bytes gauge"
      echo "pi_bots_backup_archive_bytes $archive_bytes"
    fi
  } > "$METRICS_DIR/.pi-bots-backup.prom.$$"
  chmod 644 "$METRICS_DIR/.pi-bots-backup.prom.$$"
  mv -f "$METRICS_DIR/.pi-bots-backup.prom.$$" "$METRICS_FILE"
}

on_exit() {
  local code=$?
  trap - EXIT
  rm -rf "$WORK"
  if [ "$code" -ne 0 ] && [ "$published" -eq 0 ] && [ "$partial_made" -eq 1 ]; then
    run rm -rf "$PARTIAL" 2>/dev/null || true
  fi
  if [ "$locked" -eq 1 ]; then
    if [ "$code" -eq 0 ]; then write_metrics 1 || code=1; else write_metrics 0 || true; fi
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  [ "$code" -ne 0 ] && log "Backup failed at: $stage" >&2
  exit "$code"
}
trap on_exit EXIT
trap 'exit 143' TERM
trap 'exit 130' HUP INT

# --- lock ------------------------------------------------------------------
mkdir -p "$STAGING_DIR"
chmod 700 "$STAGING_DIR"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  holder=$(cat "$LOCK_DIR/pid" 2>/dev/null || true)
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    log "Another backup (pid $holder) is running; skipping."
    exit 0
  fi
  rm -f "$LOCK_DIR/pid"
  rmdir "$LOCK_DIR" 2>/dev/null || fail "could not recover a stale lock at $LOCK_DIR"
  mkdir "$LOCK_DIR" || fail "could not take the lock at $LOCK_DIR"
  log "Recovered a stale lock."
fi
echo $$ > "$LOCK_DIR/pid"
locked=1
# A run killed outright (power loss, SIGKILL) leaves its working directory,
# which can hold a plaintext copy of the secrets; remove any before going on.
for stale in "$STAGING_DIR"/work-*; do
  [ -d "$stale" ] && [ ! -L "$stale" ] || continue
  rm -rf "$stale" || fail "could not remove the stale working directory $stale"
  log "Removed a stale working directory ${stale##*/}."
done

log "=== bot service backup $STAMP ==="

# --- preflight -------------------------------------------------------------
stage="checking the configuration"
[[ $STAMP =~ $STAMP_RE ]] || fail "STAMP must look like 20260101T000000Z, got '$STAMP'"
case "$KEEP" in '' | *[!0-9]* | 0) fail "KEEP must be a positive integer, got '$KEEP'" ;; esac
case "$TIMEOUT" in '' | *[!0-9]* | 0) fail "TIMEOUT must be a positive integer, got '$TIMEOUT'" ;; esac
case "$DEST_DIR" in
  "$MOUNT_POINT"/?*) ;;
  *) fail "destination $DEST_DIR is not inside the mount point $MOUNT_POINT" ;;
esac
case "$DEST_DIR/" in *'/../'* | *'/./'* | *'//'*) fail "destination $DEST_DIR must be a plain path" ;; esac
recipients=()
for r in $AGE_RECIPIENTS; do
  [[ $r =~ ^age1[0-9a-z]{58}$ ]] || fail "AGE_RECIPIENTS must hold age public keys (age1...), got '$r'"
  recipients+=(-r "$r")
done
[ ${#recipients[@]} -gt 0 ] || fail "AGE_RECIPIENTS is empty"
[ -d "$STATE_DIR" ] || fail "no state directory at $STATE_DIR"
[ -d "$SECRETS_DIR" ] || fail "no secrets directory at $SECRETS_DIR"

stage="checking the share is mounted"
mount_table=$("$MOUNT_CMD") || fail "could not read the mount table"
grep -qF " on $MOUNT_POINT (smbfs," <<<"$mount_table" \
  || fail "$MOUNT_POINT is not mounted (no smbfs entry in the mount table)"
if [ "$MOUNT_DEVICE_CHECK" = 1 ]; then
  mp_dev=$(run stat -f %d "$MOUNT_POINT" 2>/dev/null) || fail "cannot stat $MOUNT_POINT"
  parent_dev=$(stat -f %d "$(dirname "$MOUNT_POINT")")
  [ "$mp_dev" != "$parent_dev" ] || fail "$MOUNT_POINT is on the same device as its parent, so it is not a live mount"
fi

mkdir -p "$WORK/tree/state" "$WORK/tree/secrets"

# --- state: plain files, then SQLite databases through the online backup ---
stage="copying the state directory"
excludes=(--exclude '*.sock' --exclude 'lock' --exclude '*.lock' --exclude '*-journal'
  --exclude '*-wal' --exclude '*-shm' --exclude '.DS_Store' --exclude './metrics')
databases=()
while IFS= read -r -d '' f; do
  case "$f" in */lock | *.lock) continue ;; esac
  [ "$(head -c 15 "$f" 2>/dev/null)" = "SQLite format 3" ] && databases+=("${f#"$STATE_DIR"/}")
done < <(find "$STATE_DIR" -type f ! -name '*-wal' ! -name '*-shm' ! -name '*-journal' -print0)
db_excludes=()
for rel in "${databases[@]}"; do db_excludes+=(--exclude "./$rel"); done
tar -C "$STATE_DIR" -cf - "${excludes[@]}" "${db_excludes[@]}" . | tar -C "$WORK/tree/state" -xf - \
  || fail "copying the state directory failed"

stage="copying the SQLite databases"
for rel in "${databases[@]}"; do
  mkdir -p "$WORK/tree/state/$(dirname "$rel")"
  "$SQLITE" "$STATE_DIR/$rel" ".timeout 60000" ".backup '$WORK/tree/state/$rel'" \
    || fail "SQLite online backup of $rel failed"
  # The copy can keep the source's WAL mode, and Apple's SQLite can leave
  # -wal and -shm files beside it; switch it to rollback mode so the one file
  # holds everything and opens on its own.
  "$SQLITE" "$WORK/tree/state/$rel" "PRAGMA journal_mode=DELETE;" >/dev/null \
    || fail "could not take the copy of $rel out of WAL mode"
  check=$("$SQLITE" "$WORK/tree/state/$rel" "PRAGMA quick_check;" 2>&1 || true)
  [ "$check" = "ok" ] || fail "quick_check on the copy of $rel returned: $(printf '%s' "$check" | head -n 3)"
  [ ! -s "$WORK/tree/state/$rel-wal" ] || fail "the copy of $rel still has a WAL file"
  rm -f "$WORK/tree/state/$rel-wal" "$WORK/tree/state/$rel-shm"
done

stage="copying the secrets directory"
tar -C "$SECRETS_DIR" -cf - --exclude '.DS_Store' . | tar -C "$WORK/tree/secrets" -xf - \
  || fail "copying the secrets directory failed"
# Listed by tar, as the check below lists the decrypted archive, so both
# sides escape names (non-ASCII ones in the C locale) the same way.
tar -C "$WORK/tree" -cf - state secrets | tar -tf - | grep -v '/$' | LC_ALL=C sort > "$WORK/members" \
  || fail "listing the archive's members failed"
files=$(wc -l < "$WORK/members" | tr -d ' ')

# --- archive and encrypt on local disk -------------------------------------
stage="encrypting the archive"
tar -C "$WORK/tree" -cf - state secrets | "$ZSTD" -q -T0 -c | "$AGE" -e "${recipients[@]}" -o "$WORK/$ARCHIVE" \
  || fail "building the encrypted archive failed"
rm -rf "$WORK/tree"
if [ -n "$AGE_IDENTITY" ]; then
  stage="checking the archive opens"
  "$AGE" -d -i "$AGE_IDENTITY" "$WORK/$ARCHIVE" | "$ZSTD" -q -d -c | tar -tf - | grep -v '/$' \
    | LC_ALL=C sort > "$WORK/listed" || true
  cmp -s "$WORK/members" "$WORK/listed" \
    || fail "the archive does not open to the $files files put in (got $(wc -l < "$WORK/listed" | tr -d ' '))"
fi
sha=$(shasum -a 256 "$WORK/$ARCHIVE" | awk '{print $1}')
archive_bytes=$(stat -f %z "$WORK/$ARCHIVE")
cat > "$WORK/MANIFEST.json" <<EOF
{"stamp": "$STAMP", "archive": "$ARCHIVE", "sha256": "$sha", "bytes": $archive_bytes, "files": $files, "databases": ${#databases[@]}, "encryption": "age", "format": "tar of state/ and secrets/, zstd, age"}
EOF

# --- publish ---------------------------------------------------------------
stage="writing to the NAS"
run mkdir -p "$DEST_DIR" || fail "could not create $DEST_DIR"
run mkdir "$PARTIAL" || fail "could not create $PARTIAL"
partial_made=1
run cp "$WORK/$ARCHIVE" "$WORK/MANIFEST.json" "$PARTIAL/" || fail "copying to $PARTIAL failed"
stage="reading the NAS copy back"
for f in "$ARCHIVE" MANIFEST.json; do
  want=$(shasum -a 256 "$WORK/$f" | awk '{print $1}')
  got=$(run shasum -a 256 "$PARTIAL/$f" | awk '{print $1}') || fail "could not read back $f"
  [ "$want" = "$got" ] || fail "checksum mismatch on the NAS copy of $f"
done
stage="publishing"
run mv "$PARTIAL" "$FINAL" || fail "could not rename $PARTIAL to $FINAL"
published=1
log "Published $FINAL ($files files, ${#databases[@]} databases, $archive_bytes bytes)."

# --- retention (only after a successful publish) ---------------------------
stage="pruning old backups"
# One timed listing of the share: directories (not links) named like a stamp,
# oldest first, and unfinished .partial-<stamp> directories.
listing=$(run find "$DEST_DIR" -mindepth 1 -maxdepth 1 -type d -print) || fail "could not list $DEST_DIR"
backups=()
partials=()
while IFS= read -r path; do
  name=${path##*/}
  if [[ $name =~ $STAMP_RE ]]; then
    backups+=("$name")
  elif [[ $name == .partial-* && ${name#.partial-} =~ $STAMP_RE ]]; then
    partials+=("$name")
  fi
done < <(printf '%s\n' "$listing" | LC_ALL=C sort)
excess=$(( ${#backups[@]} - KEEP ))
i=0
while [ "$i" -lt "$excess" ]; do
  run rm -rf "${DEST_DIR:?}/${backups[$i]}" || fail "could not remove old backup ${backups[$i]}"
  log "Removed old backup ${backups[$i]}."
  i=$((i + 1))
done
for name in ${partials[@]+"${partials[@]}"}; do
  run rm -rf "${DEST_DIR:?}/$name" || fail "could not remove unfinished $name"
  log "Removed unfinished $name."
done

stage="done"
log "=== done ==="
