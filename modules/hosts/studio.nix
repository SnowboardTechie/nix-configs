# Host configuration: studio (Media Server Mac)
#
# Features: fonts, nix-settings, zsh, homebrew, editors, git, cli-tools
# Services: ollama, open-webui, monitoring, smb-mount, syncthing, icloud-backup
# Host-specific: Media server tools (cloudflared, etc.)
{ inputs, ... }:
{
  flake.modules.darwin.studio = { ... }: {
    imports = with inputs.self.modules.darwin; [
      # Base features
      fonts
      nix-settings
      zsh
      homebrew
      editors
      git
      cli-tools
      activation
      # Service modules
      ollama
      open-webui
      monitoring
      smb-mount
      syncthing
      icloud-backup
      hermes
    ];

    # === Core System Settings ===

    # Set primary user for homebrew and other user-specific options
    system.primaryUser = "bryan";

    # Platform
    nixpkgs.hostPlatform = "aarch64-darwin";

    # System state version
    system.stateVersion = 4;

    # Allow unfree packages
    nixpkgs.config.allowUnfree = true;

    # Add Homebrew to system PATH
    environment.systemPath = [ "/opt/homebrew/bin" ];

    # === Enable Services ===

    services.ollama.enable = true;
    services.open-webui.enable = true;
    services.monitoring.enable = true;
    services.smb-mount.enable = true;
    services.syncthing.enable = true;
    services.tailscale.enable = true;
    services.icloud-backup.enable = true;
    services.hermes = {
      enable = true;
      gateway.enable = true;
      serve = {
        enable = true;
        host = "100.121.238.48";
        port = 9119;
      };
    };

    # === Service Health & UNRAID NAS Monitoring ===

    # Alert delivery: Prometheus evaluates rules → Alertmanager (real, on
    # localhost:9093) dedups/routes → email via smtp2go. Requires one secret:
    #   ~/.secrets/grafana-smtp-password   (smtp2go password, shared with Grafana)
    services.monitoring.alertEmail = "bryan@snowboardtechie.com";

    services.monitoring.blackbox.targets = [
      "http://localhost:11434/api/tags" # Ollama
      "http://localhost:8080/health" # Open-WebUI
      "http://localhost:32400/web/index.html" # Plex (avoids /web → /web/index.html redirect)
      "http://localhost:8384/rest/noauth/health" # Syncthing
      "http://192.168.1.3/login" # UNRAID Web UI (avoids / → /Main → /login redirects)
    ];

    services.monitoring.extraScrapeConfigs = [
      {
        job_name = "unraid-node";
        static_configs = [{
          targets = [ "192.168.1.3:9100" ];
          labels = { host = "unraid"; };
        }];
      }
      {
        job_name = "unraid-cadvisor";
        static_configs = [{
          targets = [ "192.168.1.3:6666" ];
          labels = { host = "unraid"; };
        }];
      }
    ];

    # === Host-specific Homebrew Configuration ===

    homebrew = {
      taps = [
        "steipete/tap"
      ];
      brews = [
        "cloudflared"
        "grafana"
        "loki"
        "node" # Includes npm and npx for MCP extensions
        "node_exporter"
        "ollama"

        "grafana-alloy"
        "python@3.11" # Kept for legacy use; open-webui now lives in a uv tool venv
        "steipete/tap/remindctl"
        "uv" # Isolated venvs + lockfiles for python tools (open-webui)
        "syncthing"
      ];
      casks = [
        "zen"
      ];
    };
  };
}
