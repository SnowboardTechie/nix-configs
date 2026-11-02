# Nightly backup of the primary user's Hermes state to an SMB share on the NAS.
#
# Runs scripts/hermes-nas-backup.sh from a user LaunchAgent. It copies state.db
# (SQLite online backup, quick_check'd), memories, cron jobs, skills and a
# redacted config.yaml; never .env or auth.json. The script refuses to write
# unless mountPoint is a live SMB mount, publishes each backup atomically,
# keeps the newest `keep` backups and sends a Hermes alert on failure.
# This module never mounts anything: the share's mount is owned elsewhere.
{ inputs, ... }:
{
  flake.modules.darwin.hermes-nas-backup = { pkgs, config, lib, ... }:
    let
      cfg = config.services.hermes-nas-backup;
      homeDir = "/Users/${config.system.primaryUser}";
      script = pkgs.writeShellScript "hermes-nas-backup"
        (builtins.readFile ../../scripts/hermes-nas-backup.sh);
    in
    {
      options.services.hermes-nas-backup = {
        enable = lib.mkEnableOption "nightly Hermes state backup to the NAS";

        hermesHome = lib.mkOption {
          type = lib.types.str;
          default = "${homeDir}/.hermes";
          description = "Hermes home to back up.";
        };

        mountPoint = lib.mkOption {
          type = lib.types.str;
          description = "Where the SMB share is mounted. The backup refuses to run unless it is a live smbfs mount.";
        };

        destDir = lib.mkOption {
          type = lib.types.str;
          description = "Backup directory on the share; must be inside mountPoint.";
        };

        stagingDir = lib.mkOption {
          type = lib.types.str;
          default = "${homeDir}/.local/state/hermes-nas-backup";
          description = "Local working directory and last-run receipt (last-run.json).";
        };

        keep = lib.mkOption {
          type = lib.types.ints.positive;
          default = 7;
          description = "Number of backups to keep on the share.";
        };

        hour = lib.mkOption {
          type = lib.types.ints.between 0 23;
          default = 3;
          description = "Hour of day to run.";
        };

        minute = lib.mkOption {
          type = lib.types.ints.between 0 59;
          default = 0;
          description = "Minute to run.";
        };

        notifications = {
          enable = lib.mkOption {
            type = lib.types.bool;
            default = false;
            description = "Send failures (and the first success after one) through `hermes send`.";
          };
          command = lib.mkOption {
            type = lib.types.str;
            default = "${homeDir}/.local/bin/hermes";
            description = "Hermes launcher used for notifications.";
          };
          target = lib.mkOption {
            type = lib.types.str;
            default = "matrix";
            description = "`hermes send --to` target.";
          };
          mention = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = "Optional mention prepended to notifications.";
          };
        };
      };

      config = lib.mkIf cfg.enable {
        assertions = [
          {
            assertion = lib.hasPrefix "${cfg.mountPoint}/" cfg.destDir
              && !(lib.hasInfix "/../" "${cfg.destDir}/")
              && !(lib.hasInfix "/./" "${cfg.destDir}/");
            message = "services.hermes-nas-backup.destDir must be a plain path inside mountPoint.";
          }
        ];

        launchd.user.agents.hermes-nas-backup = {
          serviceConfig = {
            ProgramArguments = [ "${script}" ];
            StartCalendarInterval = [{ Hour = cfg.hour; Minute = cfg.minute; }];
            EnvironmentVariables = {
              HOME = homeDir;
              PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
              HERMES_HOME_DIR = cfg.hermesHome;
              MOUNT_POINT = cfg.mountPoint;
              DEST_DIR = cfg.destDir;
              STAGING_DIR = cfg.stagingDir;
              KEEP = toString cfg.keep;
              SQLITE = "/usr/bin/sqlite3";
              ZSTD = "${pkgs.zstd}/bin/zstd";
              MOUNT_CMD = "/sbin/mount";
              ERROR_LOG_HINT = "/tmp/hermes-nas-backup.error.log";
            } // lib.optionalAttrs cfg.notifications.enable ({
              NOTIFY_CMD = cfg.notifications.command;
              NOTIFY_TARGET = cfg.notifications.target;
            } // lib.optionalAttrs (cfg.notifications.mention != null) {
              NOTIFY_MENTION = cfg.notifications.mention;
            });
            # Low priority: the job compresses a multi-GB database at night.
            ProcessType = "Background";
            LowPriorityIO = true;
            Nice = 10;
            StandardOutPath = "/tmp/hermes-nas-backup.log";
            StandardErrorPath = "/tmp/hermes-nas-backup.error.log";
          };
        };

        # Setup-info echo, folded into extraActivation (see services/AGENTS.md).
        system.activationScripts.extraActivation.text = lib.mkAfter ''
          # === hermes-nas-backup info ===
          echo "hermes-nas-backup scheduled daily at ${toString cfg.hour}:${lib.fixedWidthNumber 2 cfg.minute} (${cfg.hermesHome} -> ${cfg.destDir}, keep ${toString cfg.keep})"
        '';
      };
    };

  # NixOS aspect - not implemented (Hermes runs on Studio only).
  flake.modules.nixos.hermes-nas-backup = { ... }: {
    # TODO: systemd timer equivalent if a NixOS host ever runs Hermes.
  };
}
