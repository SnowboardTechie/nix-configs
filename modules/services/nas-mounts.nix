# NAS share mounts owned by Nix (beyond smb-mount's media share).
#
# A user LaunchAgent runs scripts/nas-mount.sh at login, every `interval`
# seconds and on network changes. For each share it leaves a live SMB mount
# alone and otherwise mounts it with `mount_smbfs -N`, which reads the password
# from the login keychain: no password in the store, the plist or a file.
# Mounts are soft (a dead server fails calls instead of hanging them) and
# nobrowse. The mount point must sit somewhere the user can create it (not
# /Volumes, which is root's).
{ inputs, ... }:
{
  flake.modules.darwin.nas-mounts = { pkgs, config, lib, ... }:
    let
      cfg = config.services.nas-mounts;
      script = pkgs.writeScript "nas-mount" (builtins.readFile ../../scripts/nas-mount.sh);
      shareType = lib.types.submodule {
        options = {
          url = lib.mkOption {
            type = lib.types.strMatching "//[A-Za-z0-9._-]+@[A-Za-z0-9.-]+/[A-Za-z0-9._-]+";
            example = "//user@nas.local/Share";
            description = "The share, as //user@server/share. Its password must be in the login keychain (connect once from Finder and save it).";
          };
          mountPoint = lib.mkOption {
            type = lib.types.strMatching "/[^[:space:]]+";
            description = "Where to mount it; created if missing, never mounted over a non-empty directory.";
          };
        };
      };
    in
    {
      options.services.nas-mounts = {
        enable = lib.mkEnableOption "Nix-owned NAS share mounts";

        shares = lib.mkOption {
          type = lib.types.attrsOf shareType;
          default = { };
          description = "Shares to keep mounted, by name.";
        };

        interval = lib.mkOption {
          type = lib.types.ints.between 60 3600;
          default = 300;
          description = "Seconds between checks; a dropped mount is remounted at the next one.";
        };

        timeout = lib.mkOption {
          type = lib.types.ints.between 5 300;
          default = 60;
          description = "Seconds allowed for each mount attempt.";
        };
      };

      config = lib.mkIf (cfg.enable && cfg.shares != { }) {
        assertions = [
          {
            assertion = lib.all
              (s: !(lib.hasInfix "/../" "${s.mountPoint}/") && !(lib.hasInfix "/./" "${s.mountPoint}/")
                && !(lib.hasInfix "//" s.mountPoint) && !(lib.hasPrefix "/Volumes/" s.mountPoint))
              (lib.attrValues cfg.shares);
            message = "services.nas-mounts.shares: each mountPoint must be a plain path outside /Volumes.";
          }
          {
            assertion = lib.allUnique (map (s: s.mountPoint) (lib.attrValues cfg.shares));
            message = "services.nas-mounts.shares: two shares share a mountPoint.";
          }
        ];

        launchd.user.agents.nas-mounts.serviceConfig = {
          Label = "org.nixos.nas-mounts";
          ProgramArguments = [ "${script}" ]
            ++ lib.concatLists (lib.mapAttrsToList (_: s: [ s.url s.mountPoint ]) cfg.shares);
          RunAtLoad = true;
          KeepAlive = false;
          StartInterval = cfg.interval;
          # Re-run at once on network changes (wake, reconnect), as smb-mount does.
          WatchPaths = [ "/Library/Preferences/SystemConfiguration" ];
          EnvironmentVariables = {
            PATH = "/usr/bin:/bin:/usr/sbin:/sbin";
            TIMEOUT = toString cfg.timeout;
            TIMEOUT_CMD = "${pkgs.coreutils}/bin/timeout";
          };
          ProcessType = "Background";
          StandardOutPath = "/tmp/nas-mounts.log";
          StandardErrorPath = "/tmp/nas-mounts.error.log";
        };
      };
    };

  # NixOS aspect - not implemented (no NixOS host mounts the NAS's shares).
  flake.modules.nixos.nas-mounts = { ... }: {
    # TODO: fileSystems.<name> with fsType = "cifs" if a NixOS host ever needs one.
  };
}
