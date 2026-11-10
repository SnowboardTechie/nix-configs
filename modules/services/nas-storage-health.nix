# NAS storage health: alert when the NAS's storage stops taking writes.
#
# Two signals, because the server's own read-only flag can't be trusted: a
# btrfs pool that has turned itself read-only after an error can still be
# reported writable by node_exporter's filesystem metrics.
#
# - btrfs error counters. The NAS's node_exporter (already scraped, job
#   `nodeJob`) exports each btrfs device's error counters. They are kept on
#   disk and only ever grow, so any increase is a new fault: a device dropping
#   off its controller shows here within a scrape or two.
# - A write probe. A user LaunchAgent runs scripts/nas-write-probe.sh every
#   `interval` seconds: for each share it checks the mount, writes a small
#   file, reads it back and deletes it, retrying once. The result goes to a
#   textfile for node_exporter.
#
# Rules (with the monitoring module on the same host): NasBtrfsDeviceErrors,
# NasBtrfsMetricsMissing, NasWriteProbeFailed, NasShareNotMounted and
# NasWriteProbeSilent, delivered like every other alert.
{ inputs, ... }:
{
  flake.modules.darwin.nas-storage-health = { pkgs, config, lib, options, ... }:
    let
      cfg = config.services.nas-storage-health;
      homeDir = "/Users/${config.system.primaryUser}";
      script = pkgs.writeScript "nas-write-probe"
        (builtins.readFile ../../scripts/nas-write-probe.sh);
      logFile = "/tmp/nas-write-probe.log";
      job = ''job="${cfg.nodeJob}"'';
      probeMinutes = toString ((cfg.interval + 59) / 60);
      # Two probes in a row (each already retried once) before alerting.
      failedFor = "${toString ((cfg.interval * 3 / 2 + 59) / 60)}m";
      silentAfter = cfg.interval * 3;

      btrfsRules = [
        {
          # The counters are kept on the devices and never reset by themselves,
          # so increase() over a window sees each new error; 30 minutes keeps
          # one fault one notification instead of flapping per scrape. A device
          # that comes back under a new name is a new series and starts quiet.
          alert = "NasBtrfsDeviceErrors";
          expr = "increase(node_btrfs_device_errors_total{${job}}[30m]) > 0";
          labels = { severity = "critical"; };
          annotations = {
            # Device-independent, so alerts grouped into one notification
            # keep a common summary; the device is in the description.
            summary = "New btrfs device errors on the NAS ({{ $labels.instance }})";
            description = "{{ $value | printf \"%.0f\" }} new {{ $labels.type }} errors on device {{ $labels.device }} of btrfs filesystem {{ $labels.uuid }} in the last 30 minutes. A pool with write or flush errors may have turned itself read-only even though its filesystem still reports writable; check the device's connection and `btrfs device stats` on the NAS.";
          };
        }
        {
          # Without btrfs metrics the rule above can't fire. A whole exporter
          # outage is PrometheusTargetDown's; this covers the collector failing
          # and a pool seen in the last day disappearing.
          alert = "NasBtrfsMetricsMissing";
          expr = "node_scrape_collector_success{${job},collector=\"btrfs\"} == 0 or ((max_over_time(node_btrfs_info{${job}}[1d]) unless on (instance, uuid) node_btrfs_info{${job}}) and on (instance) up{${job}} == 1)";
          "for" = "15m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "NAS btrfs metrics are missing on {{ $labels.instance }}";
            description = "node_exporter on the NAS has stopped reporting btrfs metrics (the collector failed, or a filesystem seen in the last day is gone), so btrfs errors would go unnoticed. If a pool was removed on purpose, this clears within a day.";
          };
        }
      ];

      probeRules = [
        {
          alert = "NasWriteProbeFailed";
          expr = "nas_write_probe_success == 0 and nas_write_probe_mounted == 1";
          "for" = failedFor;
          labels = { severity = "critical"; };
          annotations = {
            summary = "The NAS share {{ $labels.share }} is not taking writes";
            description = "The write probe could not write, read back and delete a file on the mounted share {{ $labels.share }} in two runs in a row (each retried once). Anything written there, backups included, is failing. See ${logFile}.";
          };
        }
        {
          alert = "NasShareNotMounted";
          expr = "nas_write_probe_mounted == 0";
          "for" = failedFor;
          labels = { severity = "warning"; };
          annotations = {
            summary = "The NAS share {{ $labels.share }} is not mounted";
            description = "The write probe found no live SMB mount for {{ $labels.share }} in two runs in a row, so nothing can be written to it and its storage health is not being checked.";
          };
        }
        {
          alert = "NasWriteProbeSilent";
          expr = "time() - nas_write_probe_last_run_timestamp_seconds > ${toString silentAfter} or absent(nas_write_probe_last_run_timestamp_seconds)";
          "for" = "10m";
          labels = { severity = "warning"; };
          annotations = {
            summary = "The NAS write probe has stopped reporting";
            description = "No NAS write probe has finished in over ${toString (silentAfter / 60)} minutes (it runs every ${probeMinutes}), so the shares' writability is not being checked. See ${logFile} and `launchctl print gui/$(id -u)/org.nixos.nas-write-probe`.";
          };
        }
      ];

      shareType = lib.types.submodule {
        options = {
          mountPoint = lib.mkOption {
            type = lib.types.strMatching "/[^[:space:]]+";
            description = "Where the share is mounted. The probe fails unless it is a live smbfs mount.";
          };
          directory = lib.mkOption {
            type = lib.types.strMatching "/[^[:space:]]+";
            description = "Existing directory inside mountPoint that the probe writes its file in. The probe never creates it.";
          };
        };
      };
    in
    {
      options.services.nas-storage-health = {
        enable = lib.mkEnableOption "NAS storage health alerts (btrfs error counters and a share write probe)";

        nodeJob = lib.mkOption {
          type = lib.types.strMatching "[A-Za-z0-9_-]+";
          default = "unraid-node";
          description = "Prometheus job that scrapes the NAS's node_exporter.";
        };

        shares = lib.mkOption {
          type = lib.types.attrsOf shareType;
          default = { };
          example = lib.literalExpression ''{ backups = { mountPoint = "/Volumes/Share"; directory = "/Volumes/Share/Backups"; }; }'';
          description = "Shares the write probe checks, by name (the `share` label). Empty means no probe, only the btrfs rules.";
        };

        interval = lib.mkOption {
          type = lib.types.ints.between 60 86400;
          default = 600;
          description = "Seconds between write probes.";
        };

        timeout = lib.mkOption {
          type = lib.types.ints.between 1 600;
          default = 60;
          description = "Seconds allowed for each file step of a probe; a stuck share fails the step instead of hanging the probe.";
        };

        metricsDir = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "${homeDir}/.local/state/nas-storage-health/metrics";
          description = "Directory the probe writes nas-write-probe.prom into for node_exporter's textfile collector.";
        };
      };

      config = lib.mkIf cfg.enable (lib.mkMerge [
        {
          assertions = [
            {
              assertion = lib.all (n: builtins.match "[A-Za-z0-9_-]+" n != null) (lib.attrNames cfg.shares);
              message = "services.nas-storage-health.shares: names must match [A-Za-z0-9_-]+.";
            }
            {
              assertion = lib.all
                (s: lib.hasPrefix "${s.mountPoint}/" "${s.directory}/"
                  && !(lib.hasInfix "/../" "${s.directory}/")
                  && !(lib.hasInfix "/./" "${s.directory}/")
                  && !(lib.hasInfix "//" s.directory))
                (lib.attrValues cfg.shares);
              message = "services.nas-storage-health.shares: each directory must be a plain path inside (or equal to) its mountPoint.";
            }
            {
              # The probe's two tries, each up to four timed steps, must end
              # before the next run starts.
              assertion = 2 * 4 * (cfg.timeout + 5) + 30 < cfg.interval;
              message = "services.nas-storage-health: interval must exceed two tries of the probe (8 × (timeout + 5) + 30 seconds).";
            }
          ];
        }

        (lib.mkIf (cfg.shares != { }) {
          launchd.user.agents.nas-write-probe.serviceConfig = {
            Label = "org.nixos.nas-write-probe";
            ProgramArguments = [ "${script}" ]
              ++ lib.concatLists (lib.mapAttrsToList (name: s: [ name s.mountPoint s.directory ]) cfg.shares);
            RunAtLoad = true;
            StartInterval = cfg.interval;
            EnvironmentVariables = {
              PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
              METRICS_DIR = cfg.metricsDir;
              TIMEOUT = toString cfg.timeout;
              TIMEOUT_CMD = "${pkgs.coreutils}/bin/timeout";
            };
            ProcessType = "Background";
            # A stop waits out one timed step (timeout + 5 s), so the probe's
            # trap removes its file before launchd's SIGKILL.
            ExitTimeOut = cfg.timeout + 15;
            StandardOutPath = logFile;
            StandardErrorPath = "/tmp/nas-write-probe.error.log";
          };
        })

        # Only hosts that import the monitoring module have these options.
        (lib.optionalAttrs (options.services ? monitoring) {
          services.monitoring = {
            nodeExporter.textfileDirectories = lib.mkIf (cfg.shares != { }) [ cfg.metricsDir ];
            extraAlertRules = btrfsRules ++ lib.optionals (cfg.shares != { }) probeRules;
          };
        })
      ]);
    };

  # NixOS aspect - the probe and rules are written for the macOS monitoring host.
  flake.modules.nixos.nas-storage-health = { ... }: {
    # TODO: Implement NixOS equivalent if a NixOS host ever runs the monitoring stack.
  };
}
