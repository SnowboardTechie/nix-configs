#!/bin/sh
# One Pi Durable VM network's anchor (durable-pi-workspace#418): keep one idle
# VM attached to the network, so its vmnet bridge, and with it the gateway the
# proxies and brokers listen on, never goes down while the host runs.
#
# Apple `container` 1.5.0 gives a network a host bridge only while a VM is
# attached, and can give two networks the same bridge unit; the second to come
# up then destroys the first's gateway. An idle VM attached to each network
# before anything else uses it prevented that under every ordering probed
# (durable-pi-workspace vault/2026-11-10-vm-network-bridges-plan.md). The
# protection is that a network's bridge never goes down once its helper has
# started, so this agent never stops a running anchor.
#
# Usage: pi-durable-net-anchor.sh <network> <image> <metrics-dir> <gateway> [<earlier-gateway>...]
#
# <gateway> is this network's gateway address; each <earlier-gateway> is the
# gateway of a network whose anchor comes before this one (the sandbox
# network's, then the planning network's).
#
# The anchor VM, `net-anchor-<network>`, is detached and outlives this agent.
# This agent only starts it when it isn't running and watches it:
#   - running at start: adopted (nothing deleted, nothing started, no run
#     counted, no wait);
#   - otherwise, before starting one, it waits until every earlier network's
#     gateway is on a host interface, however long that takes. Anchors thus
#     make their networks' first attaches one at a time, in a fixed order: two
#     networks' first attaches at the same moment (as both agents would make
#     at boot) were never probed, and if they were given the same bridge unit
#     the result would be the outage this exists to prevent;
#   - present but not running: re-read after a short wait (a VM that is still
#     starting may be listed by `--all` before it is running), and only if it
#     is still present and still not running is that leftover (only that
#     name) removed; then started;
#   - absent: started with `container run --detach` (no --rm).
# If `container list` fails at start, it runs `container system start` (safe
# to repeat; SGG runs it at each start) and lists again, so the anchors don't
# depend on SGG to bring the runtime up after a boot.
#
# Then it polls every PI_DURABLE_NET_ANCHOR_POLL_SECONDS (default 15). When the
# VM is no longer running it writes up 0 and exits 1, so launchd's KeepAlive
# starts the agent again and it starts a fresh VM. A poll whose `container
# list` fails is not "the VM is gone": after PI_DURABLE_NET_ANCHOR_LIST_FAILURES
# (default 4) failures in a row it writes up 0 and exits 1 without touching
# the VM. SIGTERM, SIGINT and SIGHUP end the agent and leave the VM running.
#
# Every `container` call is bounded: one still running after
# PI_DURABLE_NET_ANCHOR_CALL_SECONDS (default 120) is killed and counts as a
# failure of that call, so a hang can't leave the agent stuck while its
# metrics read healthy.
#
# It checks its own gateway after a start or an adoption and on every poll,
# and exports whether it is on a host interface. It never deletes or restarts
# anything because the gateway is missing: an attached anchor without a
# gateway is the collision (or a killed InternetSharing), whose recovery needs
# a person (durable-pi-workspace workspaces/proxies/OPERATIONS.md §9).
#
# A changed image therefore takes effect only once the running anchor is gone
# (removed by hand, `container delete --force net-anchor-<network>`, which takes
# the network's bridge down, or a restart of the host).
#
# Metrics, in <metrics-dir>/net-anchor-<network>.prom for node_exporter's
# textfile collector, each labelled network="<network>":
#   pi_durable_net_anchor_up                          1 while the watched VM is running (adopted or started)
#   pi_durable_net_anchor_gateway_present             1 while <gateway> is on a host interface
#   pi_durable_net_anchor_runs_since_boot             anchor VMs started since the host booted (adoptions
#                                                     not counted); above 1, the bridge went down in between
#   pi_durable_net_anchor_last_run_timestamp_seconds  when the latest anchor VM was started
#   pi_durable_net_anchor_last_check_timestamp_seconds when the agent last wrote this file (every poll and
#                                                     every wait); stale, the agent is hung or gone
# The file is first written, with up 0, before any `container` call.
#
# It never restarts a network helper (com.apple.container.container-network-
# vmnet.*): on 2026-11-10 that took the network's bridge down and hung
# `container exec` machine-wide.
set -u

if [ "$#" -lt 4 ]; then
  echo "usage: $0 <network> <image> <metrics-dir> <gateway> [<earlier-gateway>...]" >&2
  exit 64
fi
network=$1
image=$2
metrics_dir=$3
gateway=$4
shift 4
earlier="$*"
name="net-anchor-$network"
metrics="$metrics_dir/net-anchor-$network.prom"
poll=${PI_DURABLE_NET_ANCHOR_POLL_SECONDS:-15}
max_failures=${PI_DURABLE_NET_ANCHOR_LIST_FAILURES:-4}
call_limit=${PI_DURABLE_NET_ANCHOR_CALL_SECONDS:-120}
settle=${PI_DURABLE_NET_ANCHOR_SETTLE_SECONDS:-5}

log() {
  printf '%s %s: %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$name" "$*"
}

work=$(mktemp -d "${TMPDIR:-/tmp}/pi-durable-net-anchor.XXXXXX") || exit 1
sleeper=
watchdog=
cleanup() {
  [ -n "$sleeper" ] && kill "$sleeper" 2>/dev/null
  [ -n "$watchdog" ] && kill "$watchdog" 2>/dev/null
  rm -rf "$work"
}
# The agent ends on a stop and leaves the VM running, on purpose.
on_signal() {
  log "stopping the agent; the anchor VM is left running"
  cleanup
  exit 0
}
trap on_signal TERM INT HUP

# Wait <seconds> interruptibly, so a signal is handled at once. The sleep
# holds none of the agent's output: if a signal lands before $sleeper is set,
# the agent still exits and nothing waits on the orphaned sleep.
pause() {
  sleep "$1" >/dev/null 2>&1 &
  sleeper=$!
  wait "$sleeper"
  sleeper=
}

# Run one `container` call, its stdout in $output, killed after $call_limit
# seconds. Its status is the call's, or non-zero when it was killed.
output=
call() {
  container "$@" >"$work/out" </dev/null &
  pid=$!
  (
    s=
    trap '[ -n "$s" ] && kill "$s" 2>/dev/null; exit 0' TERM
    sleep "$call_limit" &
    s=$!
    wait "$s"
    kill -TERM "$pid" 2>/dev/null
    sleep 2 &
    s=$!
    wait "$s"
    kill -KILL "$pid" 2>/dev/null
  ) </dev/null >/dev/null 2>&1 &
  watchdog=$!
  wait "$pid"
  status=$?
  kill "$watchdog" 2>/dev/null
  wait "$watchdog" 2>/dev/null
  watchdog=
  output=$(cat "$work/out" 2>/dev/null)
  if [ "$status" -ne 0 ] && [ "$status" -gt 128 ]; then
    log "container $1 did not finish within ${call_limit}s (status $status)"
  fi
  return "$status"
}

# Whether an address is on a host interface.
on_host() {
  ifconfig 2>/dev/null | grep -Fq "inet $1 "
}

# A value from the last file written, or empty.
previous() {
  sed -n "s/^$1{network=\"[^\"]*\"} \\([0-9][0-9]*\\)\$/\\1/p" "$metrics" 2>/dev/null | head -n 1
}

# VM starts counted this boot: the last file's count, unless its latest start
# was before the host booted.
boot=$(sysctl -n kern.boottime | sed -n 's/^{ sec = \([0-9][0-9]*\),.*/\1/p')
runs=$(previous pi_durable_net_anchor_runs_since_boot)
last_run=$(previous pi_durable_net_anchor_last_run_timestamp_seconds)
if [ -z "$boot" ] || [ -z "$runs" ] || [ -z "$last_run" ] || [ "$last_run" -lt "$boot" ]; then
  runs=0
  last_run=0
fi

# Atomic and world-readable, so node_exporter never reads half a file. Each
# write checks the gateway and stamps the check time.
write_metrics() {
  if on_host "$gateway"; then present_now=1; else present_now=0; fi
  mkdir -p "$metrics_dir" || return 1
  tmp="$metrics_dir/.net-anchor-$network.prom.$$"
  {
    echo "# HELP pi_durable_net_anchor_up 1 while the VM network's anchor VM is running (adopted or started), else 0."
    echo "# TYPE pi_durable_net_anchor_up gauge"
    echo "pi_durable_net_anchor_up{network=\"$network\"} $1"
    echo "# HELP pi_durable_net_anchor_gateway_present 1 while the VM network's gateway ($gateway) is on a host interface, else 0."
    echo "# TYPE pi_durable_net_anchor_gateway_present gauge"
    echo "pi_durable_net_anchor_gateway_present{network=\"$network\"} $present_now"
    echo "# HELP pi_durable_net_anchor_runs_since_boot Anchor VMs started since the host booted (adoptions not counted); above 1, the network's bridge went down in between."
    echo "# TYPE pi_durable_net_anchor_runs_since_boot gauge"
    echo "pi_durable_net_anchor_runs_since_boot{network=\"$network\"} $runs"
    echo "# HELP pi_durable_net_anchor_last_run_timestamp_seconds When the latest anchor VM was started (0: none this boot)."
    echo "# TYPE pi_durable_net_anchor_last_run_timestamp_seconds gauge"
    echo "pi_durable_net_anchor_last_run_timestamp_seconds{network=\"$network\"} $last_run"
    echo "# HELP pi_durable_net_anchor_last_check_timestamp_seconds When the anchor agent last checked its VM and gateway; stale, the agent is hung or gone."
    echo "# TYPE pi_durable_net_anchor_last_check_timestamp_seconds gauge"
    echo "pi_durable_net_anchor_last_check_timestamp_seconds{network=\"$network\"} $(date +%s)"
  } >"$tmp" && chmod 644 "$tmp" && mv -f "$tmp" "$metrics"
}

record() {
  write_metrics "$1" || log "could not write $metrics"
}

give_up() {
  log "$1; launchd retries"
  record 0
  cleanup
  exit 1
}

# `container list --quiet` prints the running containers' IDs and `--all`
# adds the stopped ones; `--name` sets the ID.
listed() {
  printf '%s\n' "$1" | grep -Fqx -- "$name"
}

# $running and $present, from two bounded calls.
read_lists() {
  call list --quiet || return 1
  running=$output
  call list --all --quiet || return 1
  present=$output
}

# Before any `container` call: a file left from before a reboot, or a call
# that hangs, never reads as up.
record 0

if ! read_lists; then
  log "container list failed; running container system start"
  call system start || log "container system start failed"
  read_lists || give_up "container list failed after container system start"
fi

if listed "$running"; then
  log "adopting the running anchor VM $name (network $network)"
else
  # One network's first attach at a time: wait for every earlier network's
  # gateway before attaching this one.
  waited=0
  while :; do
    missing=
    for address in $earlier; do
      on_host "$address" || missing="$missing $address"
    done
    [ -z "$missing" ] && break
    [ "$waited" -eq 0 ] && log "waiting for the earlier networks' gateways:$missing"
    waited=1
    record 0
    pause "$poll"
  done
  [ "$waited" -eq 1 ] && log "the earlier networks' gateways are up"
  read_lists || give_up "container list failed"
  if listed "$running"; then
    log "adopting the running anchor VM $name (network $network)"
  else
    if listed "$present"; then
      # Possibly still starting: look again before removing it.
      pause "$settle"
      read_lists || give_up "container list failed"
    fi
    if listed "$running"; then
      log "adopting the anchor VM $name, which was still starting"
    else
      if listed "$present"; then
        log "removing the stopped leftover $name"
        call delete --force "$name" || give_up "could not remove the stopped leftover $name"
      fi
      call run --detach --name "$name" --network "$network" --cpus 1 --memory 200M "$image" sleep infinity ||
        give_up "could not start the anchor VM $name"
      runs=$((runs + 1))
      last_run=$(date +%s)
      log "started anchor VM $name: run $runs this boot, network $network, image $image"
    fi
  fi
fi
record 1
on_host "$gateway" || log "the anchor VM is attached but gateway $gateway is on no host interface; see the proxies runbook §9"

failures=0
while :; do
  pause "$poll"
  if ! call list --quiet; then
    failures=$((failures + 1))
    log "container list failed ($failures in a row); still watching"
    if [ "$failures" -ge "$max_failures" ]; then
      give_up "container list failed $failures times in a row; exiting without touching the anchor VM"
    fi
    record 1
    continue
  fi
  failures=0
  if ! listed "$output"; then
    give_up "the anchor VM $name is no longer running; its bridge is down until it starts again"
  fi
  record 1
done
