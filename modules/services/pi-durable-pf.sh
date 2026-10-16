#!/bin/bash
# Load the Pi Durable sandbox's pf anchor and keep pf enabled. Runs as root at
# activation and, through a LaunchDaemon, at every boot: pf rules do not
# survive a restart and nix-darwin does not re-run activation at boot.
#
# The anchor sits under "com.apple/", the one anchor point the stock
# /etc/pf.conf evaluates, so neither that file nor any other anchor is
# touched. pf is enabled through our own reference, so it stays on when
# another enabler (vmnet's NAT) drops theirs. Exits nonzero unless pf is
# enabled, the main ruleset evaluates the anchor and the anchor holds exactly
# these rules.
#
# Usage: pi-durable-pf.sh <pfctl> <anchor> <rules> <token-file>
set -euo pipefail
pfctl=$1 anchor=$2 rules=$3 token_file=$4
# com.apple.pfctl loads /etc/pf.conf at boot, possibly after this daemon starts.
wait_seconds=${PI_DURABLE_PF_WAIT:-60}

fail() {
  echo "pi-durable: $*" >&2
  exit 1
}

expected=$("$pfctl" -nvf "$rules" 2>/dev/null) || fail "pfctl cannot parse $rules"
# Replaces this anchor's rules in one step and nothing else.
if ! out=$("$pfctl" -a "$anchor" -f "$rules" 2>&1); then
  fail "pfctl refused $rules for anchor $anchor: $out"
fi

token=$(cat "$token_file" 2>/dev/null || true)
if [ -z "$token" ] || ! "$pfctl" -s References 2>/dev/null | grep -qw -- "$token"; then
  token=$("$pfctl" -E 2>&1 | sed -n 's/^Token *: *\([0-9][0-9]*\).*/\1/p')
  [ -n "$token" ] || fail "pfctl -E returned no enable token"
  printf '%s\n' "$token" >"$token_file"
fi
"$pfctl" -s info 2>/dev/null | grep -q '^Status: Enabled' || fail "pf is not enabled"

waited=0
until "$pfctl" -s rules 2>/dev/null | grep -qF 'anchor "com.apple/*"'; do
  [ "$waited" -lt "$wait_seconds" ] || fail "the main ruleset has no anchor \"com.apple/*\", so $anchor is never evaluated"
  sleep 1
  waited=$((waited + 1))
done

loaded=$("$pfctl" -a "$anchor" -s rules 2>/dev/null || true)
if [ "$loaded" != "$expected" ]; then
  fail "anchor $anchor does not hold exactly $rules:
$(diff <(printf '%s\n' "$expected") <(printf '%s\n' "$loaded") || true)"
fi
