# Host configuration: studio (Home Server and Personal Desktop Mac)
#
# Features: fonts, nix-settings, zsh, homebrew, editors, git, cli-tools, personal desktop
# Services: Hermes, ollama, open-webui, monitoring, smb-mount, syncthing, iCloud backup
# (Obsidian Headless Sync and the vault Git backup were retired 2026-09-16: the personal
# second brain moved to Apple Notes; ~/second-brain is a frozen archive.)
# Host-specific: Media server tools (cloudflared, etc.)
{ inputs, ... }:
{
  flake.modules.darwin.studio = { config, ... }: {
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
      personal-desktop
      # Service modules
      ollama
      open-webui
      monitoring
      smb-mount
      syncthing
      icloud-backup
      hermes
      dashy
      summit-point-gallery
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

    services.ollama = {
      enable = true;
      contextLength = 65536;
      numParallel = 1;
      tailscaleServe = true;
    };
    services.open-webui.enable = true;
    services.monitoring.enable = true;
    services.smb-mount.enable = true;
    services.syncthing.enable = true;
    services.tailscale.enable = true;
    services.icloud-backup.enable = true;
    services.hermes = {
      enable = true;
      secureHome = true;
      gateway.enable = true;
      autoUpdate = {
        enable = true;
        calendar = { Hour = 4; Minute = 0; };
        notifications = {
          enable = true;
          target = "matrix";
          mention = "@bryan:snowboardtechie.com";
        };
      };
      dashboard = {
        enable = true;
        host = "100.121.238.48";
        port = 9119;
        tailscale = {
          enable = true;
          httpsPort = 443;
          proxyPort = 9122;
        };
      };
      headlessInstances.traci = {
        user = "traci";
        homeDirectory = "/Users/traci";
        gateway.enable = true;
        autoUpdate = {
          enable = true;
          calendar = { Weekday = 0; Hour = 5; Minute = 0; };
          notifications = {
            enable = true;
            target = "matrix";
            mention = "@bryan:snowboardtechie.com";
          };
        };
        serve = {
          enable = true;
          host = "127.0.0.1";
          port = 9120;
          tailscale = {
            enable = true;
            httpsPort = 9120;
            proxyPort = 9121;
          };
        };
      };
    };

    services.dashy = {
      enable = true;
      host = "100.121.238.48";
      # Derived so the portal links follow the one authoritative Grafana port.
      grafanaBaseUrl = "http://${config.services.dashy.host}:${toString config.services.monitoring.grafana.port}";
    };

    # Loopback-only static review service for the Summit Point website
    # concept preview. Only summitpoint.thompson.codes reaches it, through the
    # existing remotely managed Cloudflare Tunnel; other hosts get a 404.
    # Content is published by the site repo's scripts/publish-review.sh into
    # an immutable releases/<sha>/ directory, never from a working tree.
    services.summit-point-gallery.enable = true;

    # === Service Health & UNRAID NAS Monitoring ===

    # Alert delivery: Prometheus evaluates rules → Alertmanager (real, on
    # localhost:9093) dedups/routes → email via smtp2go. Requires one secret:
    #   ~/.secrets/grafana-smtp-password   (smtp2go password, shared with Grafana)
    services.monitoring.alertEmail = "bryan@snowboardtechie.com";

    # Studio host policy: keep Grafana clear of the 3000/3001 development-server
    # range. The monitoring module's reusable default stays 3000.
    services.monitoring.grafana.port = 33000;

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
        "libomp" # Colibri Metal/OpenMP build dependency for isolated GLM-5.2 retests
        "loki"
        "node_exporter"
        "ollama"

        "grafana-alloy"
        "python@3.11" # Kept for legacy use; open-webui now lives in a uv tool venv
        "steipete/tap/remindctl"
        "uv" # Isolated venvs + lockfiles for python tools (open-webui)
        "syncthing"
      ];
      casks = [
        "codex"
        "zen"
      ];
    };
  };
}
