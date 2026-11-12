# Host configuration: studio (Home Server and Personal Desktop Mac)
#
# Features: fonts, nix-settings, zsh, homebrew, editors, git, cli-tools, personal desktop
# Services: Hermes (with a nightly NAS backup), the bot service's encrypted NAS
# backup, NAS share mounts, ollama, open-webui, monitoring, fseventsd
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
  flake.modules.darwin.studio = { config, lib, ... }: {
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
      nas-storage-health
      nas-mounts
      smb-mount
      syncthing
      icloud-backup
      hermes
      hermes-nas-backup
      pi-bots
      pi-bots-backup
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
    services.open-webui = {
      enable = true;
      # Through the Cloudflare Tunnel, as people reach it.
      publicUrl = "https://ai.thompson.codes";
    };
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

    # Bryan's NAS share, mounted by Nix for the backups and the write probe
    # (smb-mount owns only the media share). The password comes from the login
    # keychain.
    services.nas-mounts = {
      enable = true;
      shares.Bryan = {
        url = "//bryan@192.168.1.3/Bryan";
        mountPoint = "/Users/bryan/NAS/Bryan";
      };
    };

    # The bot service (bryan/pi-bots), run from its checkout. Its health file
    # feeds node_exporter and the PiBots* alert rules.
    services.pi-bots = {
      enable = true;
      homeserver = "https://matrix.snowboardtechie.com";
      # The SGG preview watchdog runs as one of its jobs here.
      alerts.sggPreviewWatchdog = true;
    };

    # Nightly encrypted (age) archive of the bot service's state and secrets on
    # the NAS. Only the public key is here; the identity stays in ~/.secrets on
    # the Studio, never on the NAS.
    services.pi-bots-backup = {
      enable = true;
      ageRecipients = [ "age1cw3x6qq5lnhr2wkxu5z6u5wcr8kcvmz3weprxp9934pzh7jvndlsxrrh4s" ];
      ageIdentity = "/Users/bryan/.secrets/pi-bots-backup/age-identity.txt";
      mountPoint = "/Users/bryan/NAS/Bryan";
      destDir = "/Users/bryan/NAS/Bryan/Backups/pi-bots";
    };

    # Nightly copy of Bryan's Hermes state (state.db, memories, cron jobs, skills,
    # redacted config) to his NAS share, an hour before the 04:00 Hermes update.
    # Kept until Hermes's final archive is taken; the bot service's backup above
    # replaces it. An unmounted share fails the run with a Matrix alert instead
    # of writing to the local disk.
    services.hermes-nas-backup = {
      enable = true;
      mountPoint = "/Users/bryan/NAS/Bryan";
      destDir = "/Users/bryan/NAS/Bryan/Backups/hermes";
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
        piDurable = config.services.pi-durable;
        tailnetUrl = port: "https://${monitoring.tailscaleServe.hostName}:${toString port}";
        # A Pi Durable page's link exists only while its service runs and Serve
        # publishes it; that service is also what declares the link's probe.
        servedLink = enabled: port: link:
          lib.optional (enabled && port != null) (link // { url = "${tailnetUrl port}/"; });
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
        # Each Pi Durable workspace page, from its own Serve port. Each status
        # name is the page's content probe (services.monitoring.blackbox).
        piDurableLinks =
          servedLink piDurable.enable piDurable.page.tailscaleHttpsPort
            {
              title = "SGG pre-review";
              description = "Pre-reviews, dependencies and audit fixes";
              icon = "🔎";
              statusName = "SGG pre-review";
            }
          ++ servedLink piDurable.sampler.enable piDurable.sampler.tailscaleHttpsPort {
            title = "Claude usage";
            description = "Subscription use by workspace and week";
            icon = "📈";
            statusName = "Claude usage";
          }
          ++ servedLink piDurable.planning.enable piDurable.planning.tailscaleHttpsPort {
            title = "Planning";
            description = "Planning conversations and runs";
            icon = "🧭";
            statusName = "Planning";
          };
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
      proxy.network = "pi-durable-sgg";
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
      # The VM networks' anchors (durable-pi-workspace#418): with
      # `proxy.network` and `planning.network` set, one agent per network
      # keeps an idle, detached VM (`net-anchor-<network>`) attached, so
      # neither bridge goes down and the two never share a bridge unit. The
      # sandbox network's anchor comes first: the planning anchor starts a VM
      # only once the sandbox gateway is on the host. Each reports its
      # gateway, and an attached anchor without one raises an alert for a
      # person (never an automatic restart). The VM outlives its agent:
      # restarting or disabling the agent leaves it running, and `container delete --force net-anchor-<network>` removes
      # it, taking that network's bridge down. The image must already be in
      # Studio's local `container` image store (`container image list`); the
      # anchors never build or pull it. It is the planning agent image the
      # deployment builds. Changing it does not replace a running anchor: the
      # new image is used only once that VM is gone (removed by hand, or a
      # restart of Studio). Before the image is removed from the store, set
      # this to an image that is present and run update-system, so no anchor
      # is left needing an image that is gone.
      netAnchor.image = "pi-durable-planning-agent:30bade4";
      # Passwordless `sudo /usr/bin/killall InternetSharing` only, the VM networks' recovery (durable-pi-workspace#418).
      netAnchor.recoverySudo.enable = true;
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
        network = "pi-durable-planning";
        listenAddress = "192.168.129.1";
        allowedClients = [ "192.168.129.0/24" ];
        ipv6Subnet = "fdd2:4a0c:729f:fb6::/64";
        tailscaleHttpsPort = 8792;
        # The relay leg's probe port for durable-pi-workspace#421.
        workspaceRelayPorts = [ 8896 ];
        # The planning network reaches the model proxy (8892) on its gateway,
        # for the sampler's VM (durable-pi-workspace#443). The proxy binds it
        # once model-proxy.json's `listen` names 192.168.129.1.
        modelProxy = true;
      };
      # The workspace supervisor (durable-pi-workspace#443; the proxies
      # runbook §11): starts the sampler's VM from its definition and relays
      # its page. It exits 1 until ~/.config/pi-durable/supervisor.json exists;
      # launchd retries.
      supervisor.enable = true;
    };

    # Shared Claude usage sampler (#236; the application's
    # workspaces/sampler/OPERATIONS.md). Runs from the same deployment
    # checkout and refuses to start until the coordinator has written
    # ~/.config/pi-durable/sampler.json, whose page.port must be 8791.
    # The host agent stays on until the sampler's VM has been checked live
    # (durable-pi-workspace#443 commissioning): turning it off
    # (`hostAgent = false`) is a later, separate change at cut-over. The
    # route, probe, scrape and alerts then stay for the supervisor's relay
    # on the same port.
    services.pi-durable.sampler = {
      enable = true;
      hostAgent = true;
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

    # Every notification also goes to the bot service's loopback
    # `studio-alerts` webhook (pi-bots `webhooks/studio-alerts.json`): Steve
    # posts it into the encrypted "Studio Alerts" Matrix room, with no model
    # turn. pi-bots owns the route, the room key and the token
    # (`pi-bots webhook token studio-alerts`); nothing of theirs is here.
    services.monitoring.alertWebhook = {
      url = "http://127.0.0.1:8645/webhooks/studio-alerts";
      tokenHeader = "X-Pi-Bots-Token";
      tokenFile = "/Users/bryan/.secrets/pi-bots/webhooks/studio-alerts.token";
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

    # Status-only probes. Every service with a Studio Home tile has a content
    # probe instead, which its dot reads.
    services.monitoring.blackbox.targets = [
      "http://192.168.1.3/login" # UNRAID Web UI (avoids / → /Main → /login redirects)
    ];

    # Probes that must also see the right content. Each service's module adds
    # its own (Dashy, Artifact Shelf, the Pi Durable pages, Hermes, Ollama,
    # Open WebUI, Syncthing and the monitoring stack); Jellyfin and Plex are
    # Studio-only casks, so theirs are here.
    services.monitoring.blackbox.contentProbes = {
      Jellyfin = {
        url = "http://127.0.0.1:8096/health";
        bodyMatches = "^Healthy\\s*$";
      };
      # On the tailnet address Studio Home links to; /web/index.html avoids
      # the /web redirect.
      Plex = {
        url = "http://100.121.238.48:32400/web/index.html";
        bodyMatches = "[Pp]lex";
      };
    };

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

    # NAS storage health: alert on new btrfs device errors from the NAS's
    # node_exporter (the unraid-node job above) and when the backup share stops
    # taking writes, checked by writing a small file every 10 minutes. It probes
    # the directory pi-bots-backup is configured to publish to (its destDir).
    services.nas-storage-health = {
      enable = true;
      shares.Bryan = {
        mountPoint = "/Users/bryan/NAS/Bryan";
        directory = config.services.pi-bots-backup.destDir;
      };
    };

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
