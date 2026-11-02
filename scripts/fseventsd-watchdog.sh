#!/bin/sh
# fseventsd watchdog: one check per run, started by launchd every StartInterval.
#
# Reads fseventsd's resident memory and accumulated CPU time, writes them for
# node_exporter's textfile collector, and restarts fseventsd (SIGTERM; launchd
# relaunches it) once it has stayed over a restart limit for SUSTAINED_CHECKS
# checks in a row, at most once per COOLDOWN_SECONDS. CPU is the CPU time used
# between two checks of the same process, as a percentage of one core.
#
# Prints a line only when something changes, so the log stays small.
#
# Environment (all required unless noted):
#   STATE_DIR              root-only state (consecutive checks, restart counters)
#   METRICS_DIR            node_exporter textfile directory; fseventsd.prom is written here
#   RESTART_MEMORY_BYTES   restart limit for resident memory
#   RESTART_CPU_PERCENT    restart limit for CPU, percent of one core
#   SUSTAINED_CHECKS       consecutive checks over a limit before a restart
#   COOLDOWN_SECONDS       minimum time between two restarts
#   PS_CMD, PGREP_CMD, KILL_CMD, SLEEP_CMD   optional; stand-ins for tests
#   NOW                    optional; epoch seconds, for tests
set -u
umask 022

is_uint() {
  case ${1:-} in
    '' | *[!0-9]*) return 1 ;;
  esac
  return 0
}

for name in RESTART_MEMORY_BYTES RESTART_CPU_PERCENT SUSTAINED_CHECKS COOLDOWN_SECONDS; do
  eval "value=\${$name:-}"
  if ! is_uint "$value"; then
    echo "fseventsd-watchdog: $name must be a whole number, got '$value'" >&2
    exit 2
  fi
done
for name in STATE_DIR METRICS_DIR; do
  eval "value=\${$name:-}"
  case $value in
    /*) ;;
    *)
      echo "fseventsd-watchdog: $name must be an absolute path, got '$value'" >&2
      exit 2
      ;;
  esac
done

PS_CMD=${PS_CMD:-/bin/ps}
PGREP_CMD=${PGREP_CMD:-/usr/bin/pgrep}
KILL_CMD=${KILL_CMD:-/bin/kill}
SLEEP_CMD=${SLEEP_CMD:-/bin/sleep}
NOW=${NOW:-$(/bin/date +%s)}
if ! is_uint "$NOW"; then
  echo "fseventsd-watchdog: NOW must be epoch seconds, got '$NOW'" >&2
  exit 2
fi

log() {
  printf '%s %s\n' "$(/bin/date -u -r "$NOW" +%Y-%m-%dT%H:%M:%SZ)" "$*"
}

mib() {
  echo $(($1 / 1048576))
}

/bin/mkdir -p "$STATE_DIR" "$METRICS_DIR" || exit 1
state_file=$STATE_DIR/state

# --- previous state ---------------------------------------------------------
prev_pid=
prev_cpu_centis=
prev_checked_at=
over=0
restarts_memory=0
restarts_cpu=0
last_restart=0
if [ -f "$state_file" ]; then
  while IFS='=' read -r key value; do
    is_uint "$value" || continue
    case $key in
      pid) prev_pid=$value ;;
      cpu_centis) prev_cpu_centis=$value ;;
      checked_at) prev_checked_at=$value ;;
      over) over=$value ;;
      restarts_memory) restarts_memory=$value ;;
      restarts_cpu) restarts_cpu=$value ;;
      last_restart) last_restart=$value ;;
    esac
  done <"$state_file"
fi

# --- this check -------------------------------------------------------------
pid=$("$PGREP_CMD" -x fseventsd 2>/dev/null | /usr/bin/head -n 1)
rss_kb=
cpu_time=
if is_uint "$pid"; then
  # shellcheck disable=SC2046 # word splitting is the point: "rss time"
  set -- $("$PS_CMD" -o rss= -o time= -p "$pid" 2>/dev/null)
  rss_kb=${1:-}
  cpu_time=${2:-}
fi

# ps prints CPU time as [[dd-]hh:]mm:ss.ss; minutes are not capped at 59.
cpu_centis=$(printf '%s\n' "$cpu_time" | /usr/bin/awk '
  /^([0-9]+-)?[0-9]+(:[0-9]+)+(\.[0-9]+)?$/ {
    days = 0
    t = $0
    dash = index(t, "-")
    if (dash) { days = substr(t, 1, dash - 1); t = substr(t, dash + 1) }
    n = split(t, part, ":")
    seconds = 0
    for (i = 1; i <= n; i++) seconds = seconds * 60 + part[i]
    printf "%d\n", int((days * 86400 + seconds) * 100 + 0.5)
  }')

running=0
if is_uint "$pid" && is_uint "$rss_kb" && is_uint "$cpu_centis"; then
  running=1
fi

restarted=0
if [ "$running" -eq 0 ]; then
  if [ -n "$prev_pid" ]; then
    log "fseventsd is not running (last seen as pid $prev_pid)"
  fi
  pid=
  cpu_centis=
  over=0
else
  rss_bytes=$((rss_kb * 1024))

  cpu_percent=
  if [ "$pid" != "$prev_pid" ]; then
    # A new process (restarted by us, by launchd or by hand): fresh baseline.
    over=0
  elif is_uint "$prev_cpu_centis" && is_uint "$prev_checked_at" \
    && [ "$NOW" -gt "$prev_checked_at" ] && [ "$cpu_centis" -ge "$prev_cpu_centis" ]; then
    # Centiseconds of CPU per wall second is the percentage of one core.
    cpu_percent=$(((cpu_centis - prev_cpu_centis) / (NOW - prev_checked_at)))
  fi

  reason=
  if [ "$rss_bytes" -gt "$RESTART_MEMORY_BYTES" ]; then
    reason=memory
  elif [ -n "$cpu_percent" ] && [ "$cpu_percent" -ge "$RESTART_CPU_PERCENT" ]; then
    reason=cpu
  fi
  usage="memory $(mib "$rss_bytes") MiB, cpu ${cpu_percent:-?}%"

  if [ -n "$reason" ]; then
    over=$((over + 1))
    if [ "$over" -eq 1 ]; then
      log "fseventsd (pid $pid) is over its $reason limit: $usage"
    fi
  else
    if [ "$over" -gt 0 ]; then
      log "fseventsd (pid $pid) is back under its limits after $over checks: $usage"
    fi
    over=0
  fi

  if [ "$over" -ge "$SUSTAINED_CHECKS" ]; then
    since=$((NOW - last_restart))
    if [ "$last_restart" -gt 0 ] && [ "$since" -lt "$COOLDOWN_SECONDS" ]; then
      if [ "$over" -eq "$SUSTAINED_CHECKS" ]; then
        log "fseventsd (pid $pid) has been over its $reason limit for $over checks, but it was restarted ${since}s ago; waiting for the ${COOLDOWN_SECONDS}s cooldown"
      fi
    else
      log "restarting fseventsd (pid $pid): over its $reason limit for $over checks: $usage"
      if "$KILL_CMD" -TERM "$pid"; then
        waited=0
        while "$KILL_CMD" -0 "$pid" 2>/dev/null; do
          if [ "$waited" -ge 10 ]; then
            log "fseventsd (pid $pid) ignored SIGTERM for ${waited}s; sending SIGKILL"
            "$KILL_CMD" -KILL "$pid"
            break
          fi
          "$SLEEP_CMD" 1
          waited=$((waited + 1))
        done
        if [ "$reason" = memory ]; then
          restarts_memory=$((restarts_memory + 1))
        else
          restarts_cpu=$((restarts_cpu + 1))
        fi
        last_restart=$NOW
        over=0
        restarted=1
      else
        log "could not signal fseventsd (pid $pid); will try again next check"
      fi
    fi
  fi
fi

# --- metrics ----------------------------------------------------------------
metrics_tmp=$METRICS_DIR/.fseventsd.prom.$$
{
  echo "# HELP fseventsd_up Whether the watchdog found fseventsd running at its last check."
  echo "# TYPE fseventsd_up gauge"
  echo "fseventsd_up $running"
  if [ "$running" -eq 1 ] && [ "$restarted" -eq 0 ]; then
    echo "# HELP fseventsd_resident_memory_bytes Resident memory of fseventsd."
    echo "# TYPE fseventsd_resident_memory_bytes gauge"
    echo "fseventsd_resident_memory_bytes $rss_bytes"
    echo "# HELP fseventsd_cpu_seconds_total CPU time used by the current fseventsd process; resets when it restarts."
    echo "# TYPE fseventsd_cpu_seconds_total counter"
    printf 'fseventsd_cpu_seconds_total %d.%02d\n' $((cpu_centis / 100)) $((cpu_centis % 100))
  fi
  echo "# HELP fseventsd_watchdog_over_limit_checks Consecutive checks with fseventsd over a restart limit."
  echo "# TYPE fseventsd_watchdog_over_limit_checks gauge"
  echo "fseventsd_watchdog_over_limit_checks $over"
  echo "# HELP fseventsd_watchdog_restarts_total Restarts of fseventsd by the watchdog, by the limit it was over."
  echo "# TYPE fseventsd_watchdog_restarts_total counter"
  echo "fseventsd_watchdog_restarts_total{reason=\"memory\"} $restarts_memory"
  echo "fseventsd_watchdog_restarts_total{reason=\"cpu\"} $restarts_cpu"
  echo "# HELP fseventsd_watchdog_last_restart_timestamp_seconds When the watchdog last restarted fseventsd; 0 if never."
  echo "# TYPE fseventsd_watchdog_last_restart_timestamp_seconds gauge"
  echo "fseventsd_watchdog_last_restart_timestamp_seconds $last_restart"
  echo "# HELP fseventsd_watchdog_last_check_timestamp_seconds When the watchdog last checked fseventsd."
  echo "# TYPE fseventsd_watchdog_last_check_timestamp_seconds gauge"
  echo "fseventsd_watchdog_last_check_timestamp_seconds $NOW"
  echo "# HELP fseventsd_watchdog_restart_memory_bytes The watchdog's memory restart limit."
  echo "# TYPE fseventsd_watchdog_restart_memory_bytes gauge"
  echo "fseventsd_watchdog_restart_memory_bytes $RESTART_MEMORY_BYTES"
  echo "# HELP fseventsd_watchdog_restart_cpu_percent The watchdog's CPU restart limit, percent of one core."
  echo "# TYPE fseventsd_watchdog_restart_cpu_percent gauge"
  echo "fseventsd_watchdog_restart_cpu_percent $RESTART_CPU_PERCENT"
} >"$metrics_tmp" && /bin/mv -f "$metrics_tmp" "$METRICS_DIR/fseventsd.prom"

# --- state ------------------------------------------------------------------
# The CPU baseline is skipped after a restart: the next check sees a new pid.
state_tmp=$STATE_DIR/.state.$$
{
  echo "pid=$pid"
  echo "cpu_centis=$cpu_centis"
  echo "checked_at=$NOW"
  echo "over=$over"
  echo "restarts_memory=$restarts_memory"
  echo "restarts_cpu=$restarts_cpu"
  echo "last_restart=$last_restart"
} >"$state_tmp" && /bin/chmod 0600 "$state_tmp" && /bin/mv -f "$state_tmp" "$state_file"
