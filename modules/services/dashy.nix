# Dashy static service portal for the Studio tailnet.
{ inputs, ... }:
{
  flake.modules.darwin.dashy =
    { config
    , lib
    , pkgs
    , ...
    }:
    let
      cfg = config.services.dashy;
      statusState = "/Users/${config.system.primaryUser}/.local/state/hermes/studio-service-watchdog.json";
      grafanaBase = "http://100.121.238.48:3000";
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
                  title = "Hindsight";
                  description = "Shared agent memory control plane";
                  url = "https://bryans-mac-studio.tail5ba690.ts.net:9444";
                  icon = "🧠";
                  statusName = "Hindsight Control Plane";
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
              ];
            }
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
                  url = "http://100.121.238.48:9090";
                  icon = "🔥";
                })
                (item {
                  title = "Grafana Alloy";
                  description = "Studio telemetry collector";
                  url = "http://100.121.238.48:12345";
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
                  title = "Hindsight API";
                  description = "Agent-memory data plane";
                  url = "https://bryans-mac-studio.tail5ba690.ts.net:9444";
                  icon = "💾";
                })
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
        statusPort = lib.mkOption {
          type = lib.types.port;
          default = 8089;
          description = "Loopback port for the read-only watchdog status adapter.";
        };
      };

      config = lib.mkIf cfg.enable {
        assertions = [
          {
            assertion = cfg.port != cfg.statusPort;
            message = "services.dashy portal and status ports must differ";
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

        system.activationScripts.extraActivation.text = lib.mkAfter ''
          # === Dashy firewall ===
          /usr/libexec/ApplicationFirewall/socketfilterfw --add ${pkgs.caddy}/bin/caddy >/dev/null 2>&1 || true
          /usr/libexec/ApplicationFirewall/socketfilterfw --unblock ${pkgs.caddy}/bin/caddy >/dev/null 2>&1 || true
        '';
      };
    };

  flake.modules.nixos.dashy = { };
}
