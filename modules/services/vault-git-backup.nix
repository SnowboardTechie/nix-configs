# Conservative nightly Git snapshot service for a vault
{ inputs, ... }:
{
  flake.modules.darwin.vault-git-backup = { config, lib, pkgs, ... }:
    let
      cfg = config.services.vault-git-backup;
      backup = pkgs.writeShellApplication {
        name = "vault-git-backup";
        runtimeInputs = [ pkgs.git pkgs.git-lfs ];
        text = builtins.readFile ../../scripts/vault-git-backup.sh;
      };
    in
    {
      options.services.vault-git-backup = {
        enable = lib.mkEnableOption "conservative scheduled Git snapshots for a vault";

        user = lib.mkOption {
          type = lib.types.str;
          default = config.system.primaryUser;
          description = "User whose Git identity and SSH credentials are used";
        };

        homeDirectory = lib.mkOption {
          type = lib.types.str;
          default = "/Users/${cfg.user}";
          description = "Home directory containing machine-local Git and SSH configuration";
        };

        vaultPath = lib.mkOption {
          type = lib.types.str;
          description = "Absolute path to the Git-backed vault";
        };

        remote = lib.mkOption {
          type = lib.types.str;
          default = "origin";
          description = "Existing Git remote to fetch from and push to";
        };

        branch = lib.mkOption {
          type = lib.types.str;
          default = "main";
          description = "Branch that must match its remote before a snapshot is created";
        };

        hour = lib.mkOption {
          type = lib.types.ints.between 0 23;
          default = 3;
          description = "Local hour for the nightly snapshot";
        };

        minute = lib.mkOption {
          type = lib.types.ints.between 0 59;
          default = 0;
          description = "Local minute for the nightly snapshot";
        };

        logPath = lib.mkOption {
          type = lib.types.str;
          default = "/tmp/vault-git-backup.log";
          description = "Path for standard output";
        };

        errorLogPath = lib.mkOption {
          type = lib.types.str;
          default = "/tmp/vault-git-backup.error.log";
          description = "Path for standard error";
        };
      };

      config = lib.mkIf cfg.enable {
        assertions = [{
          assertion = lib.hasPrefix "/" cfg.homeDirectory && lib.hasPrefix "/" cfg.vaultPath;
          message = "services.vault-git-backup homeDirectory and vaultPath must be absolute paths";
        }];

        launchd.user.agents.vault-git-backup = {
          serviceConfig = {
            Label = "com.snowboardtechie.vault-git-backup";
            ProgramArguments = [
              "${backup}/bin/vault-git-backup"
              cfg.vaultPath
              cfg.remote
              cfg.branch
            ];
            StartCalendarInterval = [{
              Hour = cfg.hour;
              Minute = cfg.minute;
            }];
            WorkingDirectory = cfg.vaultPath;
            EnvironmentVariables = {
              HOME = cfg.homeDirectory;
            };
            StandardOutPath = cfg.logPath;
            StandardErrorPath = cfg.errorLogPath;
            ProcessType = "Background";
          };
        };
      };
    };

  flake.modules.nixos.vault-git-backup = { ... }: {
    # TODO: Implement a systemd timer when a NixOS host needs this.
  };
}
