# Dashy static service portal for the Studio tailnet.
{ inputs, ... }:
{
  flake.modules.darwin.dashy =
    { config
    , lib
    , options
    , pkgs
    , ...
    }:
    let
      cfg = config.services.dashy;
      statusState = "/Users/${config.system.primaryUser}/.local/state/hermes/studio-service-watchdog.json";
      grafanaBase = cfg.grafanaBaseUrl;
      grafanaHealth = "${grafanaBase}/d/service-health/service-health";
      grafanaLogs = "${grafanaBase}/d/studio-logs/studio-service-logs";
      statusTarget = name: "watchdog:///${lib.replaceStrings [ " " ] [ "%20" ] name}";
      item =
        { title
        , description
        , url
        , icon
        , statusName ? title
        ,
        }:
        {
          inherit
            title
            description
            url
            icon
            ;
          statusCheck = true;
          statusCheckUrl = statusTarget statusName;
        };
      # A link without a watchdog name gets no status dot.
      piDurableItem = link:
        if link.statusName == null then
          { inherit (link) title description url icon; statusCheck = false; }
        else
          item link;
      loopback = cfg.host == "127.0.0.1";
      # One portal link, shown with piDurableItem.
      linkType = lib.types.submodule {
        options = {
          title = lib.mkOption { type = lib.types.str; };
          description = lib.mkOption { type = lib.types.str; };
          url = lib.mkOption { type = lib.types.str; };
          icon = lib.mkOption { type = lib.types.str; };
          statusName = lib.mkOption {
            type = lib.types.nullOr lib.types.str;
            default = null;
            description = ''
              The Hermes watchdog's name for this service, which drives the
              status dot. Null shows the link without one.
            '';
          };
        };
      };
      dashyPackage = pkgs.dashy-ui.override {
        settings = {
          pageInfo = {
            title = "Studio Home";
            description = "Bryan's private service portal";
            navLinks = [
              {
                title = "Service Health";
                path = grafanaHealth;
              }
              {
                title = "Studio Logs";
                path = grafanaLogs;
              }
            ];
          };
          appConfig = {
            theme = "nord-frost";
            defaultOpeningMethod = "newtab";
            statusCheck = true;
            statusCheckInterval = 300;
            statusCheckAccessibility = true;
            disableConfiguration = true;
            preventWriteToDisk = true;
            preventLocalSave = true;
            disableSmartSort = true;
            disableUpdateChecks = true;
            enableErrorReporting = false;
            enableServiceWorker = false;
            enableFontAwesome = false;
            webSearch.disableWebSearch = true;
          };
          sections = [
            {
              name = "Daily Services";
              icon = "🧭";
              displayData.itemSize = "large";
              items = [
                (item {
                  title = "Hermes";
                  description = "Agent administration and sessions";
                  url = "https://bryans-mac-studio.tail5ba690.ts.net";
                  icon = "🤖";
                  statusName = "Hermes Dashboard";
                })

                (item {
                  title = "Open WebUI";
                  description = "Local AI chat and model access";
                  url = "https://ai.thompson.codes";
                  icon = "✨";
                })
                (item {
                  title = "Grafana";
                  description = "Studio metrics and service health";
                  url = grafanaBase;
                  icon = "📊";
                })
              ] ++ map piDurableItem cfg.dailyLinks;
            }
          ] ++ lib.optional (cfg.piDurableLinks != [ ]) {
            name = "Pi Durable";
            icon = "🧩";
            displayData.itemSize = "large";
            items = map piDurableItem cfg.piDurableLinks;
          } ++ [
            {
              name = "Media";
              icon = "🎬";
              displayData.itemSize = "large";
              items = [
                (item {
                  title = "Plex";
                  description = "Primary media library";
                  url = "http://100.121.238.48:32400/web/index.html";
                  icon = "▶️";
                })
                (item {
                  title = "Jellyfin";
                  description = "Open media library";
                  url = "http://100.121.238.48:8096";
                  icon = "🎞️";
                })
              ];
            }
            {
              name = "Operations";
              icon = "🛠️";
              displayData.itemSize = "medium";
              items = [
                (item {
                  title = "Prometheus";
                  description = "Metrics and alert rules";
                  url = cfg.prometheusUrl;
                  icon = "🔥";
                })
                (item {
                  title = "Grafana Alloy";
                  description = "Studio telemetry collector";
                  url = cfg.alloyUrl;
                  icon = "🧪";
                })
                {
                  title = "Tailscale";
                  description = "Tailnet devices and access controls";
                  url = "https://login.tailscale.com/admin/machines";
                  icon = "🔐";
                  statusCheck = false;
                }

                (item {
                  title = "Alertmanager";
                  description = "Alert routing; inspect through Grafana";
                  url = grafanaHealth;
                  icon = "🚨";
                })
                (item {
                  title = "Loki";
                  description = "Service logs; inspect through Grafana";
                  url = grafanaLogs;
                  icon = "📝";
                })
                (item {
                  title = "Ollama";
                  description = "Local inference backend";
                  url = "https://ai.thompson.codes";
                  icon = "🦙";
                })
                (item {
                  title = "Syncthing";
                  description = "File synchronization; inspect health in Grafana";
                  url = grafanaHealth;
                  icon = "🔄";
                })
              ];
            }
          ];
        };
      };
      statusApi = pkgs.writeText "dashy-status-api.py" (builtins.readFile ./dashy-status-api.py);
      caddyConfig = pkgs.writeText "dashy-caddy.json" (builtins.toJSON {
        admin.disabled = true;
        apps.http.servers.dashy = {
          listen = [ "${cfg.host}:${toString cfg.port}" ];
          automatic_https.disable = true;
          routes = [
            {
              match = [{ path = [ "/status-check" "/status-check/" ]; }];
              handle = [
                {
                  handler = "reverse_proxy";
                  upstreams = [{ dial = "127.0.0.1:${toString cfg.statusPort}"; }];
                }
              ];
            }
            {
              handle = [
                {
                  handler = "file_server";
                  root = "${dashyPackage}";
                }
              ];
            }
          ];
        };
      });
    in
    {
      options.services.dashy = {
        enable = lib.mkEnableOption "tailnet-only Dashy service portal";
        host = lib.mkOption {
          type = lib.types.str;
          default = "127.0.0.1";
          description = "Address for the Dashy portal listener.";
        };
        port = lib.mkOption {
          type = lib.types.port;
          default = 8088;
          description = "Port for the Dashy portal.";
        };
        grafanaBaseUrl = lib.mkOption {
          type = lib.types.str;
          default = "http://127.0.0.1:3000";
          description = ''
            Base URL every Grafana-backed portal link is built from. Hosts
            should derive this from services.monitoring.grafana.port so the
            listener and the portal links cannot drift apart.
          '';
        };
        prometheusUrl = lib.mkOption {
          type = lib.types.str;
          default = "http://127.0.0.1:9090";
          description = "Prometheus portal link. Hosts should derive it from services.monitoring, like grafanaBaseUrl.";
        };
        alloyUrl = lib.mkOption {
          type = lib.types.str;
          default = "http://127.0.0.1:12345";
          description = "Grafana Alloy portal link. Hosts should derive it from services.monitoring, like grafanaBaseUrl.";
        };
        statusPort = lib.mkOption {
          type = lib.types.port;
          default = 8089;
          description = "Loopback port for the read-only watchdog status adapter.";
        };
        tailscaleHttpsPort = lib.mkOption {
          type = lib.types.nullOr lib.types.port;
          default = null;
          example = 8088;
          description = ''
            Tailnet-only HTTPS port that Tailscale Serve forwards to the portal.
            Requires the portal on 127.0.0.1, so Serve is its only way in.
          '';
        };
        piDurableLinks = lib.mkOption {
          type = lib.types.listOf linkType;
          default = [ ];
          description = ''
            Pi Durable workspace pages, shown in their own section in this
            order. Hosts should derive each URL from the page's Serve port.
          '';
        };
        dailyLinks = lib.mkOption {
          type = lib.types.listOf linkType;
          default = [ ];
          description = ''
            Extra links appended to the Daily Services section, in this order.
            Hosts should derive each URL from the service's own options.
          '';
        };
      };

      config = lib.mkIf cfg.enable (lib.mkMerge [{
        assertions = [
          {
            assertion = cfg.port != cfg.statusPort;
            message = "services.dashy portal and status ports must differ";
          }
          {
            assertion = cfg.tailscaleHttpsPort == null || (config.services.tailscale.enable && loopback);
            message = "services.dashy.tailscaleHttpsPort requires services.tailscale and services.dashy.host = \"127.0.0.1\"";
          }
        ];

        launchd.user.agents.dashy-status = {
          serviceConfig = {
            ProgramArguments = [
              "${pkgs.python3Minimal}/bin/python3"
              "${statusApi}"
              "--host"
              "127.0.0.1"
              "--port"
              (toString cfg.statusPort)
              "--state"
              statusState
            ];
            RunAtLoad = true;
            KeepAlive = true;
            StandardOutPath = "/tmp/dashy-status.log";
            StandardErrorPath = "/tmp/dashy-status.error.log";
          };
        };

        launchd.user.agents.dashy = {
          serviceConfig = {
            ProgramArguments = [
              "${pkgs.caddy}/bin/caddy"
              "run"
              "--config"
              "${caddyConfig}"
            ];
            RunAtLoad = true;
            KeepAlive = true;
            StandardOutPath = "/tmp/dashy.log";
            StandardErrorPath = "/tmp/dashy.error.log";
          };
        };

        # A loopback portal needs no firewall rule: loopback isn't filtered.
        system.activationScripts.extraActivation.text = lib.mkAfter (lib.optionalString (!loopback) ''
          # === Dashy firewall ===
          /usr/libexec/ApplicationFirewall/socketfilterfw --add ${pkgs.caddy}/bin/caddy >/dev/null 2>&1 || true
          /usr/libexec/ApplicationFirewall/socketfilterfw --unblock ${pkgs.caddy}/bin/caddy >/dev/null 2>&1 || true
        '' + lib.optionalString (cfg.tailscaleHttpsPort != null) ''

          # === dashy: portal via Tailscale Serve ===
          if ! ${pkgs.coreutils}/bin/timeout --foreground 30s \
            ${config.services.tailscale.package}/bin/tailscale serve \
              --bg \
              --yes \
              --https=${toString cfg.tailscaleHttpsPort} \
              http://127.0.0.1:${toString cfg.port}; then
            echo "Failed to configure Tailscale Serve for Dashy on port ${toString cfg.tailscaleHttpsPort}" >&2
            exit 1
          fi
        '');
      }

        # Only hosts that import the monitoring module have this option.
        (lib.optionalAttrs (options.services ? monitoring) {
          services.monitoring.blackbox.contentProbes.Dashy = {
            url = "http://${cfg.host}:${toString cfg.port}/";
            bodyMatches = "<title>Dashy</title>";
          };
        })]);
    };

  flake.modules.nixos.dashy = { };
}
