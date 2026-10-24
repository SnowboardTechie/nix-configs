# Host configuration: studio (Home Server and Personal Desktop Mac)
#
# Features: fonts, nix-settings, zsh, homebrew, editors, git, cli-tools, personal desktop
# Services: Hermes, ollama, open-webui, monitoring, smb-mount, syncthing, iCloud backup
# (Pi Durable SGG pre-review enabled for commissioning 2026-10-16; now commissioned.)
# (oMLX imported for an isolated GLM-5.3-Flash trial; disabled until commissioning.)
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
      cinny
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
      pi-durable
      omlx
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
        # The guarded updater follows this install's channel and preserves only
        # the reviewed classic /status and #170 matrix-send patches; unrelated
        # drift still refuses.
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
        # Bryan's agents repair this instance without a sudo password.
        operator = "bryan";
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

    services.dashy =
      let
        monitoring = config.services.monitoring;
        tailnetUrl = port: "https://${monitoring.tailscaleServe.hostName}:${toString port}";
      in
      {
        enable = true;
        host = "100.121.238.48";
        # Derived so the portal links follow the authoritative monitoring ports,
        # which Tailscale Serve publishes tailnet-only over HTTPS.
        grafanaBaseUrl = tailnetUrl monitoring.grafana.port;
        prometheusUrl = tailnetUrl monitoring.prometheus.port;
        alloyUrl = tailnetUrl monitoring.alloy.port;
      };

    # Loopback-only static review service for the Summit Point website
    # concept preview. Only summitpoint.thompson.codes reaches it, through the
    # existing remotely managed Cloudflare Tunnel; other hosts get a 404.
    # Content is published by the site repo's scripts/publish-review.sh into
    # an immutable releases/<sha>/ directory, never from a working tree.
    services.summit-point-gallery.enable = true;

    # Pi Durable SGG pre-review (dedicated ~/.local/share/pi-durable/sgg-deployment checkout). Enabled
    # 2026-10-16 for commissioning, after Apple `container` 1.5.0 was approved
    # and installed from Apple's signed installer (pinned; upgrades are
    # deliberate). The agent starts paused and holds every side effect until
    # its own gates are recorded; see modules/services/AGENTS.md.
    services.pi-durable = {
      enable = true;
      # `container network create --internal pi-durable-sgg`: its gateway and
      # subnets. The gateway exists only while a VM is attached; tinyproxy
      # retries until the first one, then keeps its socket across VM churn.
      # pf confines both subnets to the proxy.
      proxy.listenAddress = "192.168.128.1";
      proxy.allowedClients = [ "192.168.128.0/24" ];
      proxy.ipv6Subnet = "fd85:1c07:74fb:2dc7::/64";
      # Free tailnet HTTPS port (Serve held 443, 9120, 10443 and 11434).
      page.tailscaleHttpsPort = 8790;
    };

    # oMLX trial server (docs/omlx-glm53-trial.md): an on-demand, loopback-only
    # second inference runtime for GLM-5.3-Flash with SSD expert offload.
    # Disabled until the reviewed runtime and checkpoint are approved,
    # installed and verified; Ollama, its routes and every client stay as they
    # are. Port 8000 had no listener on 2026-10-16.
    services.omlx = {
      enable = false;
      port = 8000;
      stateDir = "/Users/${config.system.primaryUser}/.local/share/omlx-trial";
      memoryGuard = "safe";
      ssdCacheGB = 20;
    };

    # === Service Health & UNRAID NAS Monitoring ===

    # Alert delivery: Prometheus evaluates rules → Alertmanager (real, on
    # localhost:9093) dedups/routes → email via smtp2go. Requires one secret:
    #   ~/.secrets/grafana-smtp-password   (smtp2go password, shared with Grafana)
    services.monitoring.alertEmail = "bryan@snowboardtechie.com";

    # Every notification also goes to Hermes's loopback `studio-alerts` webhook
    # route (deliver-only, no agent run), which posts it into the encrypted
    # "Studio Alerts" Matrix room (!BY2yuFPzB-GFM6e1MCD33kHk-YH2c1H3Ga-R59PCTUg).
    # Hermes owns the route, the room and the token; its route config lives in
    # ~/.hermes/config.yaml, not here.
    services.monitoring.alertWebhook = {
      url = "http://127.0.0.1:8644/webhooks/studio-alerts";
      tokenHeader = "X-Gitlab-Token";
      tokenFile = "/Users/bryan/.secrets/hermes-alertmanager-webhook-token";
    };

    # Studio host policy: keep Grafana clear of the 3000/3001 development-server
    # range. The monitoring module's reusable default stays 3000.
    services.monitoring.grafana.port = 33000;

    # Every monitoring listener is loopback-only. Grafana (33000), Prometheus
    # (9090) and the Alloy UI (12345) are published tailnet-only over HTTPS on
    # the same port numbers (Serve also holds 443, 8790, 9120, 10443 and TCP
    # 11434).
    services.monitoring.tailscaleServe = {
      enable = true;
      hostName = "bryans-mac-studio.tail5ba690.ts.net";
    };

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
        "jellyfin"
        "zen"
      ];
    };
  };
}
