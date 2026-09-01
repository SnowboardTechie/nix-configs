#!/bin/sh
set -eu

owner_group=${1-}
[ -n "$owner_group" ] || {
  echo "migrate-monitoring-storage: missing owner:group argument" >&2
  exit 2
}
shift

fail() {
  echo "migrate-monitoring-storage: $*" >&2
  exit 1
}

for storage_path in "$@"; do
  case "$storage_path" in
    /*) ;;
    *) fail "storage path must be absolute: $storage_path" ;;
  esac

  storage_parent=${storage_path%/*}
  storage_name=${storage_path##*/}
  [ -n "$storage_parent" ] || storage_parent=/
  case "$storage_name" in
    ""|.|..) fail "invalid storage path: $storage_path" ;;
  esac

  if [ ! -e "$storage_path" ] && [ ! -L "$storage_path" ]; then
    continue
  fi
  [ ! -L "$storage_path" ] \
    || fail "refusing symlinked storage path: $storage_path"
  [ -d "$storage_path" ] \
    || fail "storage path exists but is not a directory: $storage_path"

  (
    cd -P "$storage_parent" \
      || fail "cannot enter storage parent: $storage_parent"
    physical_parent=$(/bin/pwd -P)
    [ "$physical_parent" = "$storage_parent" ] \
      || fail "refusing symlinked parent for $storage_path (resolved to $physical_parent)"
    [ ! -L "$storage_name" ] \
      || fail "refusing symlinked storage entry: $storage_path"
    [ -d "$storage_name" ] \
      || fail "storage entry changed before migration: $storage_path"

    # The physical working directory pins the parent inode. `-h` prevents a
    # final-component swap from redirecting this privileged ownership repair.
    /usr/sbin/chown -h "$owner_group" "./$storage_name"
  )
done
