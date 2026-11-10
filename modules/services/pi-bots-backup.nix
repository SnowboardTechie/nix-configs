# Nightly encrypted backup of the bot service's state (bryan/pi-bots) to an SMB
# share on the NAS.
#
# Runs scripts/pi-bots-backup.sh from a user LaunchAgent. It takes the state
# directory (key stores, bridge journals, sync positions, Pi sessions; SQLite
# through the online backup) and the secrets directory (recovery keys, access
# tokens, the registration file), packs them into one tar, compresses it and
# encrypts it with age to `ageRecipients` before anything leaves the Studio.
# Only public keys are configured here; the identity that decrypts the
# archive stays off the NAS (`ageIdentity` is read locally, only to check that
# each new archive opens).
#
# Like hermes-nas-backup it refuses to write unless the share is a live SMB
# mount, publishes atomically and keeps the newest `keep` backups. Failures
# reach Prometheus through a textfile metric (PiBotsBackupFailed,
# PiBotsBackupOverdue, PiBotsBackupMissing) on hosts with the monitoring module.
# The mount itself is owned by services.nas-mounts.
{ inputs, ... }:
{
  flake.modules.darwin.pi-bots-backup = { pkgs, config, lib, options, ... }:
    let
      cfg = config.services.pi-bots-backup;
      homeDir = "/Users/${config.system.primaryUser}";
      script = pkgs.writeShellScript "pi-bots-backup"
        (builtins.readFile ../../scripts/pi-bots-backup.sh);
      logFile = "/tmp/pi-bots-backup.log";
      time = "${toString cfg.hour}:${lib.fixedWidthNumber 2 cfg.minute}";

      rules = [
        {
          alert = "PiBotsBackupFailed";
          expr = "pi_bots_backup_success == 0";
          labels = { severity = "warning"; };
          annotations = {
            summary = "The bot service backup failed";
            description = "The last nightly backup of the bot service's state did not reach the NAS; the previous archives are kept. See ${logFile} and /tmp/pi-bots-backup.error.log.";
          };
        }
        {
          alert = "PiBotsBackupOverdue";
          expr = "time() - pi_bots_backup_last_success_timestamp_seconds > ${toString (26 * 3600)}";
          "for" = "1h";
          labels = { severity = "warning"; };
          annotations = {
            summary = "No bot service backup in over a day";
            description = "The last bot service backup published to the NAS is {{ $value | humanizeDuration }} old (it runs daily at ${time}).";
          };
        }
        {
          alert = "PiBotsBackupMissing";
          expr = "absent(pi_bots_backup_last_run_timestamp_seconds)";
          "for" = "26h";
          labels = { severity = "warning"; };
          annotations = {
            summary = "The bot service backup has never reported";
            description = "No bot service backup run has written its metrics in over a day (it runs daily at ${time}). Check `launchctl print gui/$(id -u)/org.nixos.pi-bots-backup`.";
          };
        }
      ];
    in
    {
      options.services.pi-bots-backup = {
        enable = lib.mkEnableOption "nightly encrypted backup of the bot service's state to the NAS";

        stateDir = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "${homeDir}/.local/state/pi-bots";
          description = "The bot service's state directory.";
        };

        secretsDir = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "${homeDir}/.secrets/pi-bots";
          description = "The bot service's secrets directory (recovery keys, tokens, registration).";
        };

        ageRecipients = lib.mkOption {
          type = lib.types.nonEmptyListOf (lib.types.strMatching "age1[0-9a-z]{58}");
          description = "age public keys the archive is encrypted to. Never put an identity (private key) here.";
        };

        ageIdentity = lib.mkOption {
          type = lib.types.nullOr (lib.types.strMatching "/[^[:space:]]+");
          default = null;
          description = "Local age identity file used only to check each archive opens before it is published. Must not be on the share.";
        };

        mountPoint = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          description = "Where the SMB share is mounted. The backup refuses to run unless it is a live smbfs mount.";
        };

        destDir = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          description = "Backup directory on the share; must be inside mountPoint.";
        };

        stagingDir = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "${homeDir}/.local/state/pi-bots-backup";
          description = "Local working directory (0700).";
        };

        metricsDir = lib.mkOption {
          type = lib.types.strMatching "/[^[:space:]]+";
          default = "${homeDir}/.local/state/pi-bots-backup/metrics";
          description = "Directory the run writes pi-bots-backup.prom into for node_exporter's textfile collector.";
        };

        keep = lib.mkOption {
          type = lib.types.ints.positive;
          default = 14;
          description = "Number of backups to keep on the share.";
        };

        hour = lib.mkOption {
          type = lib.types.ints.between 0 23;
          default = 3;
          description = "Hour of day to run.";
        };

        minute = lib.mkOption {
          type = lib.types.ints.between 0 59;
          default = 30;
          description = "Minute to run.";
        };

        timeout = lib.mkOption {
          type = lib.types.ints.between 10 3600;
          default = 300;
          description = "Seconds allowed for each step on the share.";
        };
      };

      config = lib.mkIf cfg.enable (lib.mkMerge [
        {
          assertions = [
            {
              assertion = lib.hasPrefix "${cfg.mountPoint}/" cfg.destDir
                && !(lib.hasInfix "/../" "${cfg.destDir}/")
                && !(lib.hasInfix "/./" "${cfg.destDir}/")
                && !(lib.hasInfix "//" cfg.destDir);
              message = "services.pi-bots-backup.destDir must be a plain path inside mountPoint.";
            }
            {
              assertion = cfg.ageIdentity == null
                || !(lib.hasPrefix "${cfg.mountPoint}/" cfg.ageIdentity);
              message = "services.pi-bots-backup.ageIdentity must not be on the share.";
            }
            {
              assertion = !(lib.hasPrefix "${cfg.mountPoint}/" cfg.stagingDir)
                && !(lib.hasPrefix "${cfg.mountPoint}/" cfg.metricsDir);
              message = "services.pi-bots-backup: stagingDir and metricsDir must be on local disk, not the share.";
            }
          ];

          launchd.user.agents.pi-bots-backup.serviceConfig = {
            Label = "org.nixos.pi-bots-backup";
            ProgramArguments = [ "${script}" ];
            StartCalendarInterval = [{ Hour = cfg.hour; Minute = cfg.minute; }];
            EnvironmentVariables = {
              HOME = homeDir;
              PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
              STATE_DIR = cfg.stateDir;
              SECRETS_DIR = cfg.secretsDir;
              AGE_RECIPIENTS = lib.concatStringsSep " " cfg.ageRecipients;
              MOUNT_POINT = cfg.mountPoint;
              DEST_DIR = cfg.destDir;
              STAGING_DIR = cfg.stagingDir;
              METRICS_DIR = cfg.metricsDir;
              KEEP = toString cfg.keep;
              TIMEOUT = toString cfg.timeout;
              SQLITE = "/usr/bin/sqlite3";
              ZSTD = "${pkgs.zstd}/bin/zstd";
              AGE = "${pkgs.age}/bin/age";
              TIMEOUT_CMD = "${pkgs.coreutils}/bin/timeout";
              MOUNT_CMD = "/sbin/mount";
            } // lib.optionalAttrs (cfg.ageIdentity != null) {
              AGE_IDENTITY = cfg.ageIdentity;
            };
            ProcessType = "Background";
            # A stop waits out one timed share step, so the run's trap removes
            # its working directory and unfinished copy before launchd's SIGKILL.
            ExitTimeOut = cfg.timeout + 15;
            LowPriorityIO = true;
            Nice = 10;
            StandardOutPath = logFile;
            StandardErrorPath = "/tmp/pi-bots-backup.error.log";
          };
        }

        # Only hosts that import the monitoring module have these options.
        (lib.optionalAttrs (options.services ? monitoring) {
          services.monitoring = {
            nodeExporter.textfileDirectories = [ cfg.metricsDir ];
            extraAlertRules = rules;
          };
        })
      ]);
    };

  # NixOS aspect - not implemented (the bot service runs on the Studio only).
  flake.modules.nixos.pi-bots-backup = { ... }: {
    # TODO: systemd timer equivalent if a NixOS host ever runs the bot service.
  };
}
