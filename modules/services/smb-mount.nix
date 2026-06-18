# SMB network storage mount service
#
# Mounts an SMB share on boot/wake with retry logic. Uses credentials from
# ~/.smb-credentials file. Periodic health check ensures the mount is always
# present. Mount check uses `mount | grep` (no I/O to the share), so it's
# safe during active streams.
{ inputs, ... }:
{
  # Darwin aspect - full configuration from modules/darwin/services/smb-mount.nix
  flake.modules.darwin.smb-mount = { pkgs, config, lib, ... }: 
  let
    cfg = config.services.smb-mount;
    homeDir = "/Users/${config.system.primaryUser}";

    mountScript = pkgs.writeShellScript "mount-smb-media" ''
      exec 1>/tmp/smb-mount.log 2>/tmp/smb-mount.error.log

      MOUNT_POINT="${cfg.mountPoint}"
      CREDENTIALS_FILE="${cfg.credentialsFile}"
      MAX_RETRIES=${toString cfg.maxRetries}
      RETRY_DELAY=${toString cfg.retryDelay}

      echo "=== SMB Mount Script Started at $(date) ==="

      # Load credentials
      if [ ! -f "$CREDENTIALS_FILE" ]; then
        echo "ERROR: Credentials file not found at $CREDENTIALS_FILE"
        echo "Please create it with the following format:"
        echo "USERNAME=your_username"
        echo "PASSWORD=your_password"
        echo "SERVER=192.168.1.x"
        echo "SHARE=media"
        exit 1
      fi

      source "$CREDENTIALS_FILE"

      if [ -z "$USERNAME" ] || [ -z "$PASSWORD" ] || [ -z "$SERVER" ] || [ -z "$SHARE" ]; then
        echo "ERROR: Missing required credentials in $CREDENTIALS_FILE"
        echo "Required: USERNAME, PASSWORD, SERVER, SHARE"
        exit 1
      fi

      # Check if the share is already mounted at our mount point
      if mount | grep -q " on $MOUNT_POINT "; then
        echo "Already mounted at $MOUNT_POINT, skipping"
        exit 0
      fi

      # Check if macOS already mounted the share elsewhere (Finder/Bonjour → /Volumes/*)
      EXISTING_MOUNT=$(mount | grep -i "/$SHARE " | grep smbfs | awk '{print $3}')
      if [ -n "$EXISTING_MOUNT" ]; then
        # Symlink our mount point to the existing Bonjour mount
        if [ -L "$MOUNT_POINT" ] && [ "$(readlink "$MOUNT_POINT")" = "$EXISTING_MOUNT" ]; then
          echo "Symlink already correct: $MOUNT_POINT -> $EXISTING_MOUNT"
          exit 0
        fi
        rm -f "$MOUNT_POINT" 2>/dev/null
        ln -s "$EXISTING_MOUNT" "$MOUNT_POINT"
        echo "Symlinked $MOUNT_POINT -> $EXISTING_MOUNT"
        exit 0
      fi

      # No mount exists anywhere — mount it ourselves
      # Remove stale symlink if Bonjour mount went away
      [ -L "$MOUNT_POINT" ] && rm -f "$MOUNT_POINT"
      mkdir -p "$MOUNT_POINT"

      # Wait for network to be ready
      echo "Checking network connectivity to $SERVER..."
      for attempt in $(seq 1 $MAX_RETRIES); do
        if /sbin/ping -c 1 -t 2 "$SERVER" >/dev/null 2>&1; then
          echo "Network ready (attempt $attempt)"
          break
        fi

        if [ $attempt -eq $MAX_RETRIES ]; then
          echo "ERROR: Server $SERVER unreachable after $MAX_RETRIES attempts"
          exit 1
        fi

        echo "Network not ready, waiting... (attempt $attempt of $MAX_RETRIES)"
        sleep $RETRY_DELAY
      done

      # Mount with soft option to prevent hanging on temporary network issues
      echo "Mounting //$USERNAME@$SERVER/$SHARE to $MOUNT_POINT..."
      if /sbin/mount_smbfs -o soft "//$USERNAME:$PASSWORD@$SERVER/$SHARE" "$MOUNT_POINT" 2>&1; then
        echo "Mount successful at $(date)"
        exit 0
      else
        echo "Mount failed at $(date)"
        echo "Check credentials in $CREDENTIALS_FILE and network connectivity"
        exit 1
      fi
    '';
  in
  {
    options.services.smb-mount = {
      enable = lib.mkEnableOption "SMB network storage mount";

      mountPoint = lib.mkOption {
        type = lib.types.str;
        default = "${homeDir}/Media";
        description = "Local path to mount the SMB share";
      };

      credentialsFile = lib.mkOption {
        type = lib.types.str;
        default = "${homeDir}/.smb-credentials";
        description = "Path to credentials file containing USERNAME, PASSWORD, SERVER, SHARE";
      };

      maxRetries = lib.mkOption {
        type = lib.types.int;
        default = 30;
        description = "Maximum number of network connectivity retries";
      };

      retryDelay = lib.mkOption {
        type = lib.types.int;
        default = 2;
        description = "Delay in seconds between retry attempts";
      };

      watchPaths = lib.mkOption {
        type = lib.types.listOf lib.types.str;
        default = [ "/Library/Preferences/SystemConfiguration" ];
        description = "Paths to watch for changes that trigger remount (e.g., network config)";
      };
    };

    config = lib.mkIf cfg.enable {
      # SMB mount service
      launchd.user.agents.smb-mount = {
        serviceConfig = {
          ProgramArguments = [ "${mountScript}" ];
          RunAtLoad = true;
          KeepAlive = false;
          # Periodic health check — re-mounts if the share dropped.
          # Safe because the script checks `mount | grep`, not I/O on the share.
          StartInterval = 300;
          # Also re-run immediately on network config changes (wake, reconnect)
          WatchPaths = cfg.watchPaths;
          StandardOutPath = "/tmp/smb-mount.log";
          StandardErrorPath = "/tmp/smb-mount.error.log";
        };
      };

      # Setup-info echo, folded into extraActivation because nix-darwin's
      # system.activationScripts only composes a fixed set of named phases
      # (custom names like `smb-mount-setup` are silently dropped).
      # See services/AGENTS.md.
      system.activationScripts.extraActivation.text = lib.mkAfter ''
        # === smb-mount setup info ===
        if [ ! -f "${cfg.credentialsFile}" ]; then
          echo ""
          echo "=== SMB Mount Setup ==="
          echo "Create ${cfg.credentialsFile} with:"
          echo "  USERNAME=username"
          echo "  PASSWORD=your_password"
          echo "  SERVER=192.168.1.x"
          echo "  SHARE=media"
          echo ""
        fi
      '';
    };
  };

  # NixOS aspect - stub for future implementation
  flake.modules.nixos.smb-mount = { config, lib, ... }: {
    # TODO: Implement NixOS equivalent using systemd mount units
    # Consider using fileSystems.<name> with fsType = "cifs"
  };
}
