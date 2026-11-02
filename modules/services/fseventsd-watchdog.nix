# fseventsd watchdog: keep macOS's file-change daemon from wedging the Mac.
#
# fseventsd records file changes and delivers them to everything that watches
# files: Spotlight, Time Machine, Finder, Syncthing, editors, dev servers and
# Claude Code. On Studio (2026-11-02, 32 days up) it had grown to 12 GB and a
# full core. Starting a new watch then waited on it, which froze Claude Code's
# UI for over a minute at a time. Restarting it fixed it; launchd relaunches
# fseventsd on its own.
#
# A root LaunchDaemon (signalling fseventsd needs root) runs
# scripts/fseventsd-watchdog.sh every `interval` seconds. It writes fseventsd's
# memory and CPU for node_exporter's textfile collector and restarts fseventsd
# once it has stayed over a restart limit for `sustainedChecks` checks in a row,
# at most once per `cooldownSeconds`.
#
# With the monitoring module on the same host, Prometheus alerts (email, plus
# the Studio Alerts Matrix room through Hermes) when fseventsd's memory or CPU
# is high, on every restart, when fseventsd is down, and when the watchdog stops
# reporting. Grafana gets an "fseventsd" dashboard.
{ inputs, ... }:
{
  flake.modules.darwin.fseventsd-watchdog = { pkgs, config, lib, options, ... }:
    let
      cfg = config.services.fseventsd-watchdog;
      script = pkgs.writeScript "fseventsd-watchdog"
        (builtins.readFile ../../scripts/fseventsd-watchdog.sh);
      logFile = "/var/log/fseventsd-watchdog.log";
      label = config.launchd.daemons.fseventsd-watchdog.serviceConfig.Label;

      mib = bytes: "${toString (bytes / 1048576)} MiB";
      sustainedMinutes = toString ((cfg.sustainedChecks * cfg.interval + 59) / 60);
      silentAfter = cfg.interval * 5;

      alertRules = [
        {
          alert = "FseventsdHighMemory";
          expr = "fseventsd_resident_memory_bytes > ${toString cfg.alertMemoryBytes}";
          "for" = "10m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "fseventsd is using {{ $value | humanize1024 }}B of memory on {{ $labels.instance }}";
            description = "macOS's file-change daemon normally uses tens of MB. While it is bloated, apps that watch files (Claude Code, editors, dev servers) can freeze waiting on it. The watchdog restarts it after it stays over ${mib cfg.restartMemoryBytes} for about ${sustainedMinutes} minutes.";
          };
        }
        {
          alert = "FseventsdHighCpu";
          expr = "rate(fseventsd_cpu_seconds_total[5m]) * 100 > ${toString cfg.alertCpuPercent}";
          "for" = "10m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "fseventsd has used {{ $value | printf \"%.0f\" }}% CPU for 10 minutes on {{ $labels.instance }}";
            description = "macOS's file-change daemon is busy enough that apps that watch files (Claude Code, editors, dev servers) can freeze waiting on it. The watchdog restarts it after it stays at ${toString cfg.restartCpuPercent}% or more of one core for about ${sustainedMinutes} minutes.";
          };
        }
        {
          # The watchdog writes both counters at 0 from its first check, so
          # increase() sees the first restart.
          alert = "FseventsdRestartedByWatchdog";
          expr = "increase(fseventsd_watchdog_restarts_total[10m]) > 0";
          labels = { severity = "warning"; };
          annotations = {
            summary = "The watchdog restarted fseventsd ({{ $labels.reason }}) on {{ $labels.instance }}";
            description = "fseventsd stayed over its {{ $labels.reason }} limit for about ${sustainedMinutes} minutes, so the watchdog restarted it and launchd relaunched it. Apps that watch files may need a restart to see changes again. If this keeps happening, something is flooding fseventsd; see ${logFile} and the fseventsd dashboard in Grafana.";
          };
        }
        {
          alert = "FseventsdDown";
          expr = "fseventsd_up == 0";
          "for" = "5m";
          labels = { severity = "critical"; };
          annotations = {
            summary = "fseventsd is not running on {{ $labels.instance }}";
            description = "launchd normally relaunches fseventsd within seconds. Without it, Spotlight, Time Machine and every app that watches files miss changes.";
          };
        }
        {
          alert = "FseventsdWatchdogSilent";
          expr = "time() - fseventsd_watchdog_last_check_timestamp_seconds > ${toString silentAfter} or absent(fseventsd_watchdog_last_check_timestamp_seconds)";
          "for" = "10m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "The fseventsd watchdog has stopped reporting";
            description = "No fseventsd check in over ${toString (silentAfter / 60)} minutes, so fseventsd is neither watched nor restarted. Check ${logFile} and `sudo launchctl print system/${label}`.";
          };
        }
      ];

      # Grafana dashboard, generated so its threshold lines follow the options.
      datasource = { type = "prometheus"; uid = "Prometheus"; };
      target = expr: legendFormat: { inherit datasource expr legendFormat; refId = "A"; };
      steps = warn: crit: [
        { color = "green"; value = null; }
        { color = "orange"; value = warn; }
        { color = "red"; value = crit; }
      ];
      stat = { id, title, x, expr, unit, thresholds, decimals ? null }: {
        inherit id title datasource;
        type = "stat";
        gridPos = { h = 4; w = 6; inherit x; y = 0; };
        fieldConfig = {
          defaults = {
            inherit unit;
            color = { mode = "thresholds"; };
            thresholds = { mode = "absolute"; steps = thresholds; };
          } // lib.optionalAttrs (decimals != null) { inherit decimals; };
          overrides = [ ];
        };
        options = {
          reduceOptions = { values = false; calcs = [ "lastNotNull" ]; fields = ""; };
          orientation = "auto";
          textMode = "auto";
          colorMode = "background";
          graphMode = "none";
          justifyMode = "auto";
        };
        targets = [ (target expr title) ];
      };
      timeseries = { id, title, gridPos, targets, unit, thresholds ? null, drawStyle ? "line", decimals ? null }: {
        inherit id title gridPos datasource targets;
        type = "timeseries";
        fieldConfig = {
          defaults = {
            inherit unit;
            min = 0;
            color = { mode = "palette-classic"; };
            custom = {
              inherit drawStyle;
              lineWidth = 2;
              fillOpacity = 20;
              showPoints = "never";
              spanNulls = false;
              thresholdsStyle = { mode = if thresholds == null then "off" else "dashed"; };
            };
            thresholds = {
              mode = "absolute";
              steps = if thresholds == null then [{ color = "green"; value = null; }] else thresholds;
            };
          } // lib.optionalAttrs (decimals != null) { inherit decimals; };
          overrides = [ ];
        };
        options = {
          legend = { calcs = [ "max" ]; displayMode = "list"; placement = "bottom"; showLegend = true; };
          tooltip = { mode = "multi"; sort = "desc"; };
        };
      };
      cpuExpr = "rate(fseventsd_cpu_seconds_total[5m]) * 100";
      dashboard = pkgs.writeText "fseventsd.json" (builtins.toJSON {
        id = null;
        uid = "fseventsd";
        title = "fseventsd";
        description = "macOS's file-change daemon: memory, CPU and watchdog restarts (services.fseventsd-watchdog)";
        tags = [ "macos" "fseventsd" "watchdog" ];
        timezone = "browser";
        editable = false;
        graphTooltip = 1;
        refresh = "1m";
        schemaVersion = 39;
        version = 1;
        time = { from = "now-7d"; to = "now"; };
        annotations.list = [{
          name = "Watchdog restarts";
          inherit datasource;
          enable = true;
          iconColor = "red";
          expr = "increase(fseventsd_watchdog_restarts_total[2m]) > 0";
          step = "1m";
          titleFormat = "Watchdog restarted fseventsd";
          textFormat = "over its {{reason}} limit";
        }];
        panels = [
          (stat {
            id = 1;
            title = "Memory";
            x = 0;
            unit = "bytes";
            expr = "fseventsd_resident_memory_bytes";
            thresholds = steps cfg.alertMemoryBytes cfg.restartMemoryBytes;
          })
          (stat {
            id = 2;
            title = "CPU (5m)";
            x = 6;
            unit = "percent";
            decimals = 0;
            expr = cpuExpr;
            thresholds = steps cfg.alertCpuPercent cfg.restartCpuPercent;
          })
          (stat {
            id = 3;
            title = "Watchdog restarts in range";
            x = 12;
            unit = "none";
            decimals = 0;
            expr = "sum(round(increase(fseventsd_watchdog_restarts_total[$__range])))";
            thresholds = steps 1 3;
          })
          (stat {
            id = 4;
            title = "Since last watchdog check";
            x = 18;
            unit = "s";
            decimals = 0;
            expr = "time() - fseventsd_watchdog_last_check_timestamp_seconds";
            thresholds = steps (cfg.interval * 3) silentAfter;
          })
          (timeseries {
            id = 5;
            title = "Memory";
            unit = "bytes";
            gridPos = { h = 8; w = 12; x = 0; y = 4; };
            targets = [ (target "fseventsd_resident_memory_bytes" "fseventsd") ];
            thresholds = steps cfg.alertMemoryBytes cfg.restartMemoryBytes;
          })
          (timeseries {
            id = 6;
            title = "CPU (% of one core, 5m rate)";
            unit = "percent";
            gridPos = { h = 8; w = 12; x = 12; y = 4; };
            targets = [ (target cpuExpr "fseventsd") ];
            thresholds = steps cfg.alertCpuPercent cfg.restartCpuPercent;
          })
          (timeseries {
            id = 7;
            title = "Consecutive checks over a restart limit";
            unit = "none";
            decimals = 0;
            gridPos = { h = 8; w = 12; x = 0; y = 12; };
            targets = [ (target "fseventsd_watchdog_over_limit_checks" "checks") ];
            thresholds = steps (cfg.sustainedChecks / 2) cfg.sustainedChecks;
          })
          (timeseries {
            id = 8;
            title = "Watchdog restarts per hour";
            unit = "none";
            decimals = 0;
            drawStyle = "bars";
            gridPos = { h = 8; w = 12; x = 12; y = 12; };
            targets = [ (target "round(increase(fseventsd_watchdog_restarts_total[1h]))" "{{reason}}") ];
          })
        ];
      });
    in
    {
      options.services.fseventsd-watchdog = {
        enable = lib.mkEnableOption "the fseventsd watchdog (metrics, alerts and automatic restarts)";

        interval = lib.mkOption {
          type = lib.types.ints.between 10 3600;
          default = 60;
          description = "Seconds between checks.";
        };

        sustainedChecks = lib.mkOption {
          type = lib.types.ints.positive;
          default = 15;
          description = "Consecutive checks over a restart limit before the watchdog restarts fseventsd (15 × 60 s = 15 minutes).";
        };

        cooldownSeconds = lib.mkOption {
          type = lib.types.ints.unsigned;
          default = 3600;
          description = "Minimum time between two restarts. While it runs, alerts keep firing but nothing is restarted.";
        };

        restartMemoryBytes = lib.mkOption {
          type = lib.types.ints.positive;
          default = 2 * 1024 * 1024 * 1024;
          description = "Restart limit for fseventsd's resident memory. A fresh fseventsd uses about 10 MB.";
        };

        restartCpuPercent = lib.mkOption {
          type = lib.types.ints.positive;
          default = 90;
          description = "Restart limit for fseventsd's CPU, as a percentage of one core between two checks.";
        };

        alertMemoryBytes = lib.mkOption {
          type = lib.types.ints.positive;
          default = 1024 * 1024 * 1024;
          description = "FseventsdHighMemory fires when resident memory stays above this for 10 minutes.";
        };

        alertCpuPercent = lib.mkOption {
          type = lib.types.ints.positive;
          default = 80;
          description = "FseventsdHighCpu fires when the 5-minute CPU rate stays above this percentage of one core for 10 minutes.";
        };

        stateDir = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "/var/db/fseventsd-watchdog";
          description = "Root-owned directory for the watchdog's state (consecutive checks, restart counters).";
        };

        metricsDir = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "/var/db/fseventsd-watchdog/metrics";
          description = "Root-owned, world-readable directory the watchdog writes fseventsd.prom into for node_exporter's textfile collector.";
        };
      };

      config = lib.mkIf cfg.enable (lib.mkMerge [
        {
          assertions = [
            {
              assertion = cfg.alertMemoryBytes <= cfg.restartMemoryBytes
                && cfg.alertCpuPercent <= cfg.restartCpuPercent;
              message = "services.fseventsd-watchdog: alert limits must not exceed restart limits, or a restart can come before its warning.";
            }
          ];

          launchd.daemons.fseventsd-watchdog.serviceConfig = {
            # A boot-time system daemon must not exec /nix/store directly: wait
            # for the Nix volume first (see services/AGENTS.md).
            ProgramArguments = [
              "/bin/sh"
              "-c"
              ''
                while [ ! -x "$1" ]; do
                  /bin/sleep 1
                done
                exec "$@"
              ''
              "wait-for-nix-store"
              "${script}"
            ];
            RunAtLoad = true;
            StartInterval = cfg.interval;
            EnvironmentVariables = {
              PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
              STATE_DIR = cfg.stateDir;
              METRICS_DIR = cfg.metricsDir;
              RESTART_MEMORY_BYTES = toString cfg.restartMemoryBytes;
              RESTART_CPU_PERCENT = toString cfg.restartCpuPercent;
              SUSTAINED_CHECKS = toString cfg.sustainedChecks;
              COOLDOWN_SECONDS = toString cfg.cooldownSeconds;
            };
            # Root daemons log under /var/log, never /tmp (services/AGENTS.md).
            StandardOutPath = logFile;
            StandardErrorPath = logFile;
          };

          system.activationScripts.extraActivation.text = lib.mkAfter ''
            # === fseventsd-watchdog: state and metrics directories ===
            /usr/bin/install -d -o root -g wheel -m 0755 ${lib.escapeShellArg cfg.stateDir} ${lib.escapeShellArg cfg.metricsDir}
          '';
        }

        # Only hosts that import the monitoring module have these options.
        (lib.optionalAttrs (options.services ? monitoring) {
          services.monitoring = {
            nodeExporter.textfileDirectories = [ cfg.metricsDir ];
            extraAlertRules = alertRules;
            extraDashboards."fseventsd.json" = dashboard;
          };
        })
      ]);
    };

  # NixOS aspect - fseventsd is macOS-only; Linux has no equivalent daemon to watch.
  flake.modules.nixos.fseventsd-watchdog = { ... }: {
    # TODO: Nothing to implement; kept for the both-platforms convention.
  };
}
