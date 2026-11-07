#!/bin/sh
# forgejo-runner's launcher: nix-configs modules/services/forgejo-runner.nix.
#
# Usage: forgejo-runner-launcher.sh RUNNER STATE_DIR TEMPLATE POLL_SECONDS
#
# Waits, sleeping (never exiting, so launchd never loops it), until the
# registration files STATE_DIR/uuid and STATE_DIR/token exist (Bryan writes
# them: modules/services/AGENTS.md, "forgejo-runner"). Refuses, and keeps
# waiting, while the uuid is not one canonical lowercase UUID or the token is
# readable by anyone but its owner. Then writes the effective config, TEMPLATE
# with the UUID filled in, to STATE_DIR/config.yaml at 0600 and execs
# `RUNNER daemon --config` on it, under umask 022: the 077 below covers only
# the launcher's own writes, and every job inherits the runner's umask (a gate
# expects a login shell's 022).
#
# It never reads, copies or prints the token: the runner loads it itself
# through the config's `token_url`.
set -eu
umask 077

runner=$1
state=$2
template=$3
poll=$4

uuid_file=$state/uuid
token_file=$state/token
config=$state/config.yaml
placeholder=@FORGEJO_RUNNER_UUID@

# 8-4-4-4-12 lowercase hex digits and nothing else: no whitespace, no newline,
# nothing that could end the YAML string the UUID is written into.
h='[0-9a-f]'
h4=$h$h$h$h
canonical="$h4$h4-$h4-$h4-$h4-$h4$h4$h4"

# Each state is reported once, not on every poll.
said=
say() {
  if [ "$said" != "$1" ]; then
    echo "forgejo-runner-launcher: $1" >&2
    said=$1
  fi
}

while :; do
  if [ ! -s "$uuid_file" ] || [ ! -s "$token_file" ]; then
    say "waiting for $uuid_file and $token_file (registration: nix-configs modules/services/AGENTS.md)"
  elif [ -n "$(/usr/bin/find "$token_file" -prune -perm +077)" ]; then
    say "refusing $token_file: readable by others; it must be mode 0600, owned by the runner's account"
  else
    uuid=$(/bin/cat "$uuid_file")
    # The pattern is unquoted on purpose: it is a pattern, not a string.
    # shellcheck disable=SC2254
    case $uuid in
      $canonical) break ;;
    esac
    say "refusing $uuid_file: not one canonical lowercase UUID (8-4-4-4-12 hex digits); waiting for a corrected file"
  fi
  /bin/sleep "$poll"
done

if ! /usr/bin/grep -q -F "$placeholder" "$template"; then
  echo "forgejo-runner-launcher: $template has no $placeholder" >&2
  exit 78
fi

tmp=$(/usr/bin/mktemp "$state/.config.yaml.XXXXXX")
/usr/bin/sed "s/$placeholder/$uuid/g" "$template" >"$tmp"
/bin/chmod 0600 "$tmp"
/bin/mv -f "$tmp" "$config"

echo "forgejo-runner-launcher: registration found; starting the runner" >&2
cd "$state"
umask 022
exec "$runner" daemon --config "$config"
