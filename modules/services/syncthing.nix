# Syncthing file synchronization service
#
# Provides peer-to-peer file sync across devices. On macOS, requires homebrew
# `syncthing` package. On NixOS, uses the native services.syncthing module.
# Default: Web UI on 127.0.0.1:8384, sync protocol on 22000.
{ inputs, ... }:
{
  # Darwin aspect - launchd user agent
  flake.modules.darwin.syncthing = { config, lib, ... }: {
    options.services.syncthing = {
      enable = lib.mkEnableOption "Syncthing file synchronization";

      guiAddress = lib.mkOption {
        type = lib.types.str;
        default = "127.0.0.1:8384";
        description = "Address and port for the Web UI";
      };

      dataDir = lib.mkOption {
        type = lib.types.str;
        default = "/Users/${config.system.primaryUser}";
        description = "Default data directory for Syncthing";
      };

      logFile = lib.mkOption {
        type = lib.types.str;
        default = "/tmp/syncthing.log";
        description = "Path to log file";
      };
    };

    config = let
      cfg = config.services.syncthing;
    in lib.mkIf cfg.enable {
      # Syncthing launchd service
      launchd.user.agents.syncthing = {
        serviceConfig = {
          ProgramArguments = [
            "/opt/homebrew/bin/syncthing"
            "serve"
            "--no-browser"
            "--no-restart"
            "--gui-address=${cfg.guiAddress}"
          ];
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = cfg.logFile;
          StandardErrorPath = "/tmp/syncthing.error.log";
          EnvironmentVariables = {
            HOME = cfg.dataDir;
          };
        };
      };

      # Firewall rules + setup-info echo, folded into extraActivation because
      # nix-darwin's system.activationScripts only composes a fixed set of
      # named phases (custom names like `syncthing-firewall` are silently
      # dropped). See services/AGENTS.md.
      system.activationScripts.extraActivation.text = lib.mkAfter ''
        # === syncthing firewall ===
        /usr/libexec/ApplicationFirewall/socketfilterfw --add /opt/homebrew/bin/syncthing >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --unblock /opt/homebrew/bin/syncthing >/dev/null 2>&1 || true

        # === syncthing setup info ===
        echo ""
        echo "=== Syncthing Setup ==="
        echo "Web UI: http://${cfg.guiAddress}"
        echo "Log file: ${cfg.logFile}"
        echo ""
      '';
    };
  };

  # NixOS aspect - not needed, NixOS has native services.syncthing
  # Configure directly in host files using native options
  flake.modules.nixos.syncthing = { ... }: {
    # NixOS has native services.syncthing module
    # No wrapper needed - use native module directly in host config
  };
}
