# iCloud backup service for notes vault
#
# Performs nightly rsync backup of ~/notes to iCloud Drive. Darwin-only.
# Default: runs at 2am daily, mirrors content with --delete flag.
{ inputs, ... }:
{
  # Darwin aspect - launchd scheduled backup
  flake.modules.darwin.icloud-backup = { pkgs, config, lib, ... }:
  let
    cfg = config.services.icloud-backup;
    homeDir = "/Users/${config.system.primaryUser}";

    backupScript = pkgs.writeShellScript "icloud-backup" ''
      exec 1>/tmp/icloud-backup.log 2>/tmp/icloud-backup.error.log

      SOURCE_DIR="${cfg.sourceDir}"
      DEST_DIR="${cfg.destDir}"

      echo "=== iCloud Backup Started at $(date) ==="

      # Ensure source exists
      if [ ! -d "$SOURCE_DIR" ]; then
        echo "ERROR: Source directory not found: $SOURCE_DIR"
        exit 1
      fi

      # Ensure destination parent exists
      DEST_PARENT=$(dirname "$DEST_DIR")
      if [ ! -d "$DEST_PARENT" ]; then
        echo "ERROR: iCloud Drive not accessible: $DEST_PARENT"
        echo "Make sure iCloud Drive is enabled and synced"
        exit 1
      fi

      # Create destination if needed
      mkdir -p "$DEST_DIR"

      # Perform backup with rsync
      echo "Backing up $SOURCE_DIR -> $DEST_DIR"
      if /usr/bin/rsync -av --delete \
        --exclude='.stversions' \
        --exclude='.stfolder' \
        --exclude='.syncthing*' \
        --exclude='.DS_Store' \
        "$SOURCE_DIR/" "$DEST_DIR/"; then
        echo "Backup completed successfully at $(date)"
        exit 0
      else
        echo "Backup failed at $(date)"
        exit 1
      fi
    '';
  in
  {
    options.services.icloud-backup = {
      enable = lib.mkEnableOption "iCloud backup for notes vault";

      sourceDir = lib.mkOption {
        type = lib.types.str;
        default = "${homeDir}/notes";
        description = "Source directory to backup";
      };

      destDir = lib.mkOption {
        type = lib.types.str;
        default = "${homeDir}/Library/Mobile Documents/com~apple~CloudDocs/notes-backup";
        description = "Destination directory in iCloud Drive";
      };

      hour = lib.mkOption {
        type = lib.types.int;
        default = 2;
        description = "Hour of day to run backup (0-23)";
      };

      minute = lib.mkOption {
        type = lib.types.int;
        default = 0;
        description = "Minute to run backup (0-59)";
      };
    };

    config = lib.mkIf cfg.enable {
      # Scheduled backup service
      launchd.user.agents.icloud-backup = {
        serviceConfig = {
          ProgramArguments = [ "${backupScript}" ];
          StartCalendarInterval = [
            {
              Hour = cfg.hour;
              Minute = cfg.minute;
            }
          ];
          StandardOutPath = "/tmp/icloud-backup.log";
          StandardErrorPath = "/tmp/icloud-backup.error.log";
        };
      };

      # Setup-info echo, folded into extraActivation because nix-darwin's
      # system.activationScripts only composes a fixed set of named phases
      # (custom names like `icloud-backup-setup` are silently dropped).
      # See services/AGENTS.md.
      system.activationScripts.extraActivation.text = lib.mkAfter ''
        # === icloud-backup info ===
        echo "icloud-backup scheduled daily at ${toString cfg.hour}:${if cfg.minute < 10 then "0" else ""}${toString cfg.minute} (${cfg.sourceDir} -> iCloud)"
      '';
    };
  };

  # NixOS aspect - not applicable (iCloud is macOS only)
  flake.modules.nixos.icloud-backup = { config, lib, ... }: {
    # iCloud backup is darwin-only, no NixOS implementation
  };
}
