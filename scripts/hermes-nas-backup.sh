#!/usr/bin/env bash
# Nightly backup of Bryan's Hermes state to the NAS (services.hermes-nas-backup).
#
# What it copies: state.db (SQLite online backup, checked with quick_check),
# memories/MEMORY.md and USER.md, cron/jobs.json, skills/ (symlinks kept as
# links) and config.yaml with secret-looking values redacted. Never .env,
# auth.json or anything else.
#
# Safety rules:
# - Everything is built and checked on local disk first. The NAS only ever
#   receives finished files, written to a .partial-<stamp> directory, read back
#   and checksummed, then renamed into place.
# - The destination must sit under MOUNT_POINT, and MOUNT_POINT must be an SMB
#   mount that is really mounted (mount table plus a device-id check), so an
#   unmounted share never turns into a "backup" on the local disk.
# - Retention deletes only directories named exactly like a backup stamp, and
#   only after a new backup is published.
# - A failure exits nonzero and, when NOTIFY_CMD is set, sends a Matrix alert.
#
# Configuration comes from the environment (the Nix module sets all of it).
set -euo pipefail

HERMES_HOME_DIR=${HERMES_HOME_DIR:-"$HOME/.hermes"}
MOUNT_POINT=${MOUNT_POINT:?MOUNT_POINT is required}
DEST_DIR=${DEST_DIR:?DEST_DIR is required}
STAGING_DIR=${STAGING_DIR:-"$HOME/.local/state/hermes-nas-backup"}
KEEP=${KEEP-7}
SQLITE=${SQLITE:-/usr/bin/sqlite3}
ZSTD=${ZSTD:-zstd}
MOUNT_CMD=${MOUNT_CMD:-/sbin/mount}
MOUNT_DEVICE_CHECK=${MOUNT_DEVICE_CHECK:-1}
NOTIFY_CMD=${NOTIFY_CMD:-}
NOTIFY_TARGET=${NOTIFY_TARGET:-matrix}
NOTIFY_MENTION=${NOTIFY_MENTION:-}
ERROR_LOG_HINT=${ERROR_LOG_HINT:-/tmp/hermes-nas-backup.error.log}

STAMP=$(date -u +%Y%m%dT%H%M%SZ)
STAMP_RE='^[0-9]{8}T[0-9]{6}Z$'
STATUS_FILE="$STAGING_DIR/last-run.json"
LOCK_DIR="$STAGING_DIR/lock"
WORK="$STAGING_DIR/work-$STAMP"
PARTIAL="$DEST_DIR/.partial-$STAMP"
FINAL="$DEST_DIR/$STAMP"
stage="starting"
locked=0

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
fail() { stage="$1"; log "ERROR: $1" >&2; exit 1; }

previous_status() {
  [ -f "$STATUS_FILE" ] || return 0
  sed -n 's/.*"status": *"\([a-z]*\)".*/\1/p' "$STATUS_FILE" | head -n 1
}

notify() {
  [ -n "$NOTIFY_CMD" ] || return 0
  local prefix=""
  [ -n "$NOTIFY_MENTION" ] && prefix="$NOTIFY_MENTION "
  "$NOTIFY_CMD" send --to "$NOTIFY_TARGET" --quiet "$prefix$1" || log "WARNING: notification failed" >&2
}

json_escape() { printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\n'; }

write_status() {
  local status="$1" detail="$2"
  mkdir -p "$STAGING_DIR"
  printf '{"status": "%s", "stamp": "%s", "finished_at": "%s", "dest": "%s", "detail": "%s"}\n' \
    "$status" "$STAMP" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$(json_escape "$DEST_DIR")" \
    "$(json_escape "$detail")" > "$STATUS_FILE.tmp"
  mv -f "$STATUS_FILE.tmp" "$STATUS_FILE"
}

on_exit() {
  local code=$?
  rm -rf "$WORK"
  if [ "$code" -ne 0 ]; then
    # A partial directory is never published; remove it if the share is still there.
    [ -d "$PARTIAL" ] && rm -rf "$PARTIAL" 2>/dev/null || true
    if [ "$locked" -eq 1 ]; then
      write_status failed "$stage" || true
      notify "Hermes NAS backup FAILED (exit $code) at: $stage. Nothing new was published to $DEST_DIR. See $ERROR_LOG_HINT." || true
    fi
  fi
  if [ "$locked" -eq 1 ]; then
    rm -f "$LOCK_DIR/pid"
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  exit "$code"
}
trap on_exit EXIT
trap 'exit 130' HUP INT TERM

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
was=$(previous_status)

log "=== Hermes NAS backup $STAMP ==="

# --- preflight: the share must really be mounted ---------------------------
case "$KEEP" in '' | *[!0-9]* | 0) fail "KEEP must be a positive integer, got '$KEEP'" ;; esac
case "$DEST_DIR" in
  "$MOUNT_POINT"/?*) ;;
  *) fail "destination $DEST_DIR is not inside the mount point $MOUNT_POINT" ;;
esac
case "$DEST_DIR" in *'/../'* | */.. | *'/./'* | */.) fail "destination $DEST_DIR must be a plain path" ;; esac

stage="checking the share is mounted"
mount_table=$("$MOUNT_CMD") || fail "could not read the mount table"
if ! grep -qF " on $MOUNT_POINT (smbfs," <<<"$mount_table"; then
  fail "$MOUNT_POINT is not mounted (no smbfs entry in the mount table)"
fi
if [ "$MOUNT_DEVICE_CHECK" = 1 ]; then
  mp_dev=$(stat -f %d "$MOUNT_POINT" 2>/dev/null) || fail "cannot stat $MOUNT_POINT"
  parent_dev=$(stat -f %d "$(dirname "$MOUNT_POINT")")
  [ "$mp_dev" != "$parent_dev" ] || fail "$MOUNT_POINT is on the same device as its parent, so it is not a live mount"
fi
[ -f "$HERMES_HOME_DIR/state.db" ] || fail "no state.db in $HERMES_HOME_DIR"

mkdir -p "$WORK"
chmod 700 "$WORK"

# --- state.db --------------------------------------------------------------
stage="copying state.db"
"$SQLITE" "$HERMES_HOME_DIR/state.db" ".timeout 120000" ".backup '$WORK/state.db'" \
  || fail "SQLite online backup of state.db failed"
stage="checking the state.db copy"
check=$("$SQLITE" "$WORK/state.db" "PRAGMA quick_check;" 2>&1 || true)
[ "$check" = "ok" ] || fail "quick_check on the state.db copy returned: $(printf '%s' "$check" | head -n 3)"
sessions=$("$SQLITE" "$WORK/state.db" "SELECT count(*) FROM sessions;")
messages=$("$SQLITE" "$WORK/state.db" "SELECT count(*) FROM messages;")
db_bytes=$(stat -f %z "$WORK/state.db")
stage="compressing state.db"
"$ZSTD" -q -T0 --rm "$WORK/state.db" -o "$WORK/state.db.zst" || fail "zstd failed on state.db"

# --- small files -----------------------------------------------------------
stage="collecting memories, cron jobs, skills and config"
mkdir -p "$WORK/files"
if [ -f "$HERMES_HOME_DIR/config.yaml" ]; then
  # Redact values of keys whose name ends in secret/token/password/api key,
  # plain or env-style (FOO_TOKEN=...), including YAML block scalars.
  /usr/bin/perl -ne '
    if (defined $skip) {
      if (/^\s*$/ || (/^(\s*)/ && length($1) > $skip)) { next } else { undef $skip }
    }
    if (/^(\s*(?:-\s+)?["\x27]?[\w.-]*(?:secret|token|password|passwd|api_?key|apikey)["\x27]?\s*:\s*)(\S.*)$/i) {
      my ($k, $v) = ($1, $2);
      if ($v =~ /^[|>]/) { /^(\s*)/; $skip = length($1); }
      print "$k\"<redacted>\"\n"; next;
    }
    s/\b([A-Z0-9_]*(?:SECRET|TOKEN|PASSWORD|PASSWD|API_?KEY|APIKEY))=\S+/$1=<redacted>/g;
    print;
  ' "$HERMES_HOME_DIR/config.yaml" > "$WORK/files/config.yaml.redacted" \
    || fail "redacting config.yaml failed"
fi
members=()
for rel in memories/MEMORY.md memories/USER.md cron/jobs.json skills; do
  [ -e "$HERMES_HOME_DIR/$rel" ] && members+=("$rel")
done
tar -C "$HERMES_HOME_DIR" -cf "$WORK/files.tar" \
  --exclude 'skills/.hub' --exclude '*.lock' --exclude '.DS_Store' \
  "${members[@]}" || fail "tar of the Hermes files failed"
if [ -f "$WORK/files/config.yaml.redacted" ]; then
  tar -C "$WORK/files" -rf "$WORK/files.tar" config.yaml.redacted || fail "adding the redacted config failed"
fi
"$ZSTD" -q -T0 --rm "$WORK/files.tar" -o "$WORK/files.tar.zst" || fail "zstd failed on files.tar"
rm -rf "$WORK/files"

# --- manifest --------------------------------------------------------------
db_sha=$(shasum -a 256 "$WORK/state.db.zst" | awk '{print $1}')
files_sha=$(shasum -a 256 "$WORK/files.tar.zst" | awk '{print $1}')
cat > "$WORK/MANIFEST.json" <<EOF
{
  "stamp": "$STAMP",
  "host": "$(json_escape "$(hostname -s)")",
  "source": "$(json_escape "$HERMES_HOME_DIR")",
  "state_db": {"file": "state.db.zst", "sha256": "$db_sha", "uncompressed_bytes": $db_bytes, "quick_check": "ok", "sessions": $sessions, "messages": $messages},
  "files": {"file": "files.tar.zst", "sha256": "$files_sha", "members": "$(json_escape "${members[*]} config.yaml.redacted")"}
}
EOF

# --- publish ---------------------------------------------------------------
stage="writing to the NAS"
mkdir -p "$DEST_DIR"
mkdir "$PARTIAL"
cp "$WORK/state.db.zst" "$WORK/files.tar.zst" "$WORK/MANIFEST.json" "$PARTIAL/" \
  || fail "copying to $PARTIAL failed"
stage="reading the NAS copy back"
for f in state.db.zst files.tar.zst MANIFEST.json; do
  want=$(shasum -a 256 "$WORK/$f" | awk '{print $1}')
  got=$(shasum -a 256 "$PARTIAL/$f" | awk '{print $1}')
  [ "$want" = "$got" ] || fail "checksum mismatch on the NAS copy of $f"
done
stage="publishing"
mv "$PARTIAL" "$FINAL" || fail "could not rename $PARTIAL to $FINAL"
log "Published $FINAL ($sessions sessions, $messages messages)."

# --- retention (only after a successful publish) ---------------------------
stage="pruning old backups"
backups=()
for path in "$DEST_DIR"/*; do  # glob expansion is sorted, so oldest stamps come first
  name=${path##*/}
  [[ $name =~ $STAMP_RE ]] && backups+=("$name")
done
excess=$(( ${#backups[@]} - KEEP ))
i=0
while [ "$i" -lt "$excess" ]; do
  old="${backups[$i]}"
  if [ -d "$DEST_DIR/$old" ] && [ ! -L "$DEST_DIR/$old" ]; then
    rm -rf "${DEST_DIR:?}/$old"
    log "Removed old backup $old."
  fi
  i=$((i + 1))
done
# Leftovers from runs that died before publishing.
for path in "$DEST_DIR"/.partial-*; do
  name=${path##*/}
  suffix=${name#.partial-}
  if [[ $suffix =~ $STAMP_RE ]] && [ -d "$path" ] && [ ! -L "$path" ]; then
    rm -rf "${DEST_DIR:?}/$name"
    log "Removed unfinished $name."
  fi
done

stage="done"
write_status ok "$sessions sessions, $messages messages"
if [ "$was" = "failed" ]; then
  notify "Hermes NAS backup recovered: $STAMP published to $DEST_DIR."
fi
log "=== done ==="
