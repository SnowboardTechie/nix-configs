#!/bin/bash
# Route the Pi Durable standing page through Tailscale Serve, tailnet-only,
# without taking an existing route. Studio serves routes Nix does not declare,
# so this reads the live Serve state instead of trusting the configuration.
#
# The HTTPS port must be entirely free (no TCP listener, web handler or Funnel
# flag on any hostname), or already be exactly this route: an HTTPS listener and
# one "/" proxy to this target. Anything else is refused: Funnel (public)
# exposure, a TCP forward or plain HTTP listener, stale or extra handlers, or a
# foreground Serve session on the port.
#
# Usage: pi-durable-page-route.sh <tailscale> <jq> <https-port> <target>
set -euo pipefail
tailscale=$1 jq=$2 port=$3 target=$4

if ! status=$("$tailscale" serve status --json); then
  echo "pi-durable: cannot read Tailscale Serve status; port $port not routed" >&2
  exit 1
fi

if ! verdict=$(printf '%s' "$status" | "$jq" -r --arg p "$port" --arg t "$target" '
  def on_port: (. // {}) | to_entries | map(select(.key | endswith(":" + $p)));
  if (.AllowFunnel | on_port | map(select(.value)) | length) > 0 then
    "Funnel (public) exposure is enabled on port \($p)"
  elif [(.Foreground // {})[] | select(((.TCP // {}) | has($p)) or ((.Web | on_port) + (.AllowFunnel | on_port) | length > 0))] | length > 0 then
    "a foreground Serve session holds port \($p)"
  elif (.TCP[$p] == null and (.Web | on_port | length) == 0) then
    "free"
  elif (.TCP[$p] == {"HTTPS": true} and (.Web | on_port | map(.value.Handlers)) == [{"/": {"Proxy": $t}}]) then
    "owned"
  else
    "port \($p) already serves another route"
  end
'); then
  echo "pi-durable: cannot parse Tailscale Serve status; port $port not routed" >&2
  exit 1
fi

case $verdict in
  free | owned) ;;
  *)
    echo "pi-durable: ${verdict:-empty Serve status}; refusing to route the standing page there" >&2
    exit 1
    ;;
esac

"$tailscale" serve --bg --yes --https="$port" "$target"
