# Host configuration: studio (Home Server and Personal Desktop Mac)
#
# Features: fonts, nix-settings, zsh, homebrew, editors, git, cli-tools, personal desktop
# Services: Hermes (with a nightly NAS backup), ollama, open-webui, monitoring, fseventsd
# watchdog, smb-mount, syncthing, iCloud backup, Dashy, Artifact Shelf, a Forgejo Actions
# runner (durable-pi-workspace's gate)
# (Pi Durable SGG pre-review enabled for commissioning 2026-10-16; now commissioned.)
# (oMLX imported for an isolated GLM-5.3-Flash trial; disabled until commissioning.)
# (Obsidian Headless Sync and the vault Git backup were retired 2026-09-16. Since 2026-10-31
# ~/second-brain is the personal second brain again, synced only by agents committing to
# Forgejo; neither service is re-enabled.)
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
      personal-desktop
      element
      # Service modules
      ollama
      open-webui
      monitoring
      fseventsd-watchdog
      smb-mount
      syncthing
      icloud-backup
      hermes
      hermes-nas-backup
      dashy
      artifact-shelf
      summit-point-gallery
      pi-durable
      forgejo-runner
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
      # Desktop attaches to this gateway (`hermes gateway ensure`) for Bot Mode.
      desktop.enable = true;
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

    # Nightly copy of Bryan's Hermes state (state.db, memories, cron jobs, skills,
    # redacted config) to his NAS share, an hour before the 04:00 Hermes update.
    # Nix doesn't mount this share (smb-mount handles only the media share); an
    # unmounted share fails the run with a Matrix alert instead of writing to
    # the local disk.
    services.hermes-nas-backup = {
      enable = true;
      mountPoint = "/Volumes/Bryan";
      destDir = "/Volumes/Bryan/Backups/hermes";
      keep = 7;
      hour = 3;
      notifications = {
        enable = true;
        target = "matrix";
        mention = "@bryan:snowboardtechie.com";
      };
    };

    services.dashy =
      let
        monitoring = config.services.monitoring;
        tailnetUrl = port: "https://${monitoring.tailscaleServe.hostName}:${toString port}";
      in
      {
        enable = true;
        # Loopback only; Tailscale Serve publishes it tailnet-only over HTTPS on
        # the same port number (it listened on the tailnet address over plain
        # HTTP until 2026-11-03).
        host = "127.0.0.1";
        tailscaleHttpsPort = 8088;
        # Derived so the portal links follow the authoritative monitoring ports,
        # which Tailscale Serve publishes tailnet-only over HTTPS.
        grafanaBaseUrl = tailnetUrl monitoring.grafana.port;
        prometheusUrl = tailnetUrl monitoring.prometheus.port;
        alloyUrl = tailnetUrl monitoring.alloy.port;
        # Each Pi Durable workspace page, from its own Serve port. The status
        # names are the Hermes watchdog's probes (check-studio-services.py).
        piDurableLinks = [
          {
            title = "SGG pre-review";
            description = "Pre-reviews, dependencies and audit fixes";
            url = "${tailnetUrl config.services.pi-durable.page.tailscaleHttpsPort}/";
            icon = "🔎";
            statusName = "SGG pre-review";
          }
          {
            title = "Claude usage";
            description = "Subscription use by workspace and week";
            url = "${tailnetUrl config.services.pi-durable.sampler.tailscaleHttpsPort}/";
            icon = "📈";
            statusName = "Claude usage";
          }
          {
            title = "Planning";
            description = "Planning conversations and runs";
            url = "${tailnetUrl config.services.pi-durable.planning.tailscaleHttpsPort}/";
            icon = "🧭";
            statusName = "Planning";
          }
        ];
        dailyLinks = [
          {
            title = "Artifact Shelf";
            description = "Pages and visuals Hermes made for you";
            url = "${tailnetUrl config.services.artifact-shelf.tailscaleHttpsPort}/";
            icon = "🗂️";
            statusName = "Artifact Shelf";
          }
        ];
      };

    # Artifact Shelf: one page holding every visual artifact Hermes prepares,
    # each at a stable URL (publish with `artifact-shelf publish`). Loopback
    # Caddy on 8092, published tailnet-only over HTTPS on the same port. On
    # 2026-11-06 Serve held 443, 8088, 8790, 8791, 9090, 9120, 10443-10445
    # (previews), 11434, 12345 and 33000; nothing listened on 8092.
    services.artifact-shelf = {
      enable = true;
      tailscaleHttpsPort = 8092;
      baseUrl = "https://${config.services.monitoring.tailscaleServe.hostName}:${toString config.services.artifact-shelf.tailscaleHttpsPort}";
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
      # Model-provider proxy (durable-pi-workspace#268; the application's
      # workspaces/proxies/OPERATIONS.md) on the gateway at the default 8892,
      # which must equal `listen.port` in ~/.config/pi-durable/model-proxy.json.
      # It exits 1 until that file exists; launchd retries.
      modelProxy.enable = true;
      # GitHub write proxy (durable-pi-workspace#268; OPERATIONS.md §7): its
      # API route on a Unix socket, and (slice 3) case VMs' pushes and
      # signature requests on the gateway at the default 8893, which must
      # equal `push.listen.port` in ~/.config/pi-durable/github-proxy.json.
      # Without that `push` block it opens no port. It exits 1 until the file
      # exists; launchd retries.
      githubProxy.enable = true;
      # Replay proxy (durable-pi-workspace#21) for the evaluation's replay VMs,
      # on the gateway at the default 8889, which must equal `sandbox.proxy`'s
      # port in ~/.config/pi-durable/replay.json. Its allowlist is the package
      # hosts only: GitHub is denied by design.
      replayProxy.enable = true;
      # Forge broker (durable-pi-workspace#48; OPERATIONS.md §8) on both
      # gateways at the default 8894, which must equal `listen.port` in
      # ~/.config/pi-durable/forge-broker.json (its `listen.hosts` both
      # gateways). It exits 1 until the file exists; launchd retries.
      forgeBroker.enable = true;
      # VM broker (durable-pi-workspace#48; OPERATIONS.md §9) on the planning
      # gateway alone at the default 8895, which must equal `listen.port` in
      # ~/.config/pi-durable/vm-broker.json. The sandbox anchor never passes it.
      vmBroker.enable = true;
      # Planning service in its VM (durable-pi-workspace#48; the planning
      # runbook §4.1): `container network create --internal --subnet
      # 192.168.129.0/24 pi-durable-planning`, its gateway and subnets. Like
      # the sandbox's, the gateway exists only while a VM is attached. pf
      # confines the network to the two brokers. The relay's loopback port is
      # the default 8792 (planning-relay.json's default); free tailnet HTTPS
      # port 8792 (2026-11-10 Serve held 443, 8088, 8092, 8790, 8791, 9090,
      # 9120, 10443, 12345 and 33000; nothing listened on 8792).
      planning = {
        enable = true;
        listenAddress = "192.168.129.1";
        allowedClients = [ "192.168.129.0/24" ];
        ipv6Subnet = "fdd2:4a0c:729f:fb6::/64";
        tailscaleHttpsPort = 8792;
      };
    };

    # Shared Claude usage sampler (#236; the application's
    # workspaces/sampler/OPERATIONS.md). Runs from the same deployment
    # checkout and refuses to start until the coordinator has written
    # ~/.config/pi-durable/sampler.json, whose page.port must be 8791.
    services.pi-durable.sampler = {
      enable = true;
      port = 8791;
      # Free tailnet HTTPS port (2026-11-01 Serve held 443, 8790, 9090, 9120,
      # 10443, 11434, 12345 and 33000; nothing listened on 8791).
      tailscaleHttpsPort = 8791;
    };

    # Forgejo Actions runner for durable-pi-workspace's canonical gate
    # (durable-pi-workspace#357, its docs/ci.md), as the hidden account
    # `_forgejo-runner` (uid/gid 450, free on 2026-11-07). It waits until
    # Bryan registers it (modules/services/AGENTS.md, "forgejo-runner").
    services.forgejo-runner = {
      enable = true;
      labels = [ "studio-gate:host" ];
      # The Nix-installed Hermes's interpreter (the env whose bin/hermes is on
      # the system path), which imports Hermes's tools.send_message_tool.
      hermesPython = "${config.services.hermes.package.passthru.hermesVenv}/bin/python3";
      # The approved Chrome copy, its record and disposition (#47). The
      # coordinator places them; Bryan-owned, not writable by the runner.
      browserApproval = "/Users/Shared/durable-pi-ci/browser/browser-approval.json";
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

    # fseventsd grew to 12 GB and a full core over 32 days of uptime
    # (2026-11-02), freezing Claude Code and other file watchers. The watchdog
    # exports its memory and CPU, alerts through the pipeline above (email and
    # the Studio Alerts Matrix room), adds an "fseventsd" Grafana dashboard and
    # restarts it after 15 minutes over a limit, at most hourly. Defaults: restart
    # above 2 GiB or 90% of a core, warn above 1 GiB or 80%.
    services.fseventsd-watchdog.enable = true;

    # === Host-specific Homebrew Configuration ===

    homebrew = {
      taps = [
        "steipete/tap"
      ];
      brews = [
        "cloudflared"
        "grafana"
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
        "jellyfin"
        "zen"
      ];
    };
  };
}
