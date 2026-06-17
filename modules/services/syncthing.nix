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
            "${config.homebrew.prefix}/bin/syncthing"
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

      # Firewall rules for Syncthing
      system.activationScripts.syncthing-firewall.text = ''
        /usr/libexec/ApplicationFirewall/socketfilterfw --add ${config.homebrew.prefix}/bin/syncthing >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --unblock ${config.homebrew.prefix}/bin/syncthing >/dev/null 2>&1 || true
      '';

      # Setup instructions
      system.activationScripts.syncthing-setup.text = ''
        echo ""
        echo "=== Syncthing Setup ==="
        echo "Web UI: http://${cfg.guiAddress}"
        echo "Log file: ${cfg.logFile}"
        echo ""
        echo "First-time setup:"
        echo "  1. Open http://${cfg.guiAddress} in browser"
        echo "  2. Add remote devices by exchanging Device IDs"
        echo "  3. Create shared folders pointing to ~/notes"
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
