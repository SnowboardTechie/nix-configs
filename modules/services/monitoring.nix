# Monitoring stack service
#   Prometheus + Alertmanager + Grafana + node_exporter + Loki + Alloy + blackbox_exporter
#
# Provides metrics collection, log aggregation, visualization, and alerting. Grafana,
# node_exporter, Loki, and Alloy use Homebrew. Prometheus, Alertmanager, and
# blackbox_exporter use nixpkgs derivations (Homebrew Prometheus builds with netgo which
# breaks multi-interface routing on macOS; Alertmanager isn't in Homebrew). All services
# bind to 0.0.0.0 for network access. Configuration is fully declarative.
#
# Alerting pipeline:
#   Prometheus (rule eval) → Alertmanager (dedup/group/route) → smtp2go → email
# Grafana stays as pure visualization (Alertmanager added as datasource for alert-state
# view in UI). Set services.monitoring.alertEmail in the host config to enable.
{ inputs, ... }:
{
  # Darwin aspect - full configuration from modules/darwin/services/monitoring.nix
  flake.modules.darwin.monitoring = { pkgs, config, lib, ... }:
  let
    cfg = config.services.monitoring;
    homeDir = "/Users/${config.system.primaryUser}";
    alertingEnabled = cfg.alertEmail != null;

    # Grafana custom config — overrides Homebrew defaults.ini
    # Only contains settings not handled by GF_* environment variables
    grafanaCustomIni = pkgs.writeText "grafana-custom.ini" ''
      [smtp]
      user = snowboardtechie.com
      password = $__file{${homeDir}/.secrets/grafana-smtp-password}
    '';

    # Prometheus alerting rules
    alertRulesFile = pkgs.writeText "alert-rules.yml" (builtins.toJSON {
      groups = [{
        name = "service-health";
        rules = [
          {
            alert = "ServiceDown";
            expr = "probe_success == 0";
            "for" = "5m";
            labels = { severity = "critical"; };
            annotations = {
              summary = "Service {{ $labels.instance }} is down";
              description = "{{ $labels.instance }} has been unreachable for more than 5 minutes.";
            };
          }
          {
            alert = "HighCpuUsage";
            expr = "100 - (avg by(instance) (rate(node_cpu_seconds_total{mode=\"idle\"}[5m])) * 100) > 85";
            "for" = "10m";
            labels = { severity = "warning"; };
            annotations = {
              summary = "High CPU usage on {{ $labels.instance }}";
              description = "CPU usage has been above 85% for more than 10 minutes.";
            };
          }
          {
            alert = "HighMemoryUsage";
            expr = "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100 > 90";
            "for" = "10m";
            labels = { severity = "warning"; };
            annotations = {
              summary = "High memory usage on {{ $labels.instance }}";
              description = "Memory usage has been above 90% for more than 10 minutes.";
            };
          }
          {
            alert = "HighDiskUsage";
            expr = "(1 - node_filesystem_avail_bytes{fstype!~\"tmpfs|devtmpfs|overlay\"} / node_filesystem_size_bytes{fstype!~\"tmpfs|devtmpfs|overlay\"}) * 100 > 85";
            "for" = "15m";
            labels = { severity = "warning"; };
            annotations = {
              summary = "Disk usage above 85% on {{ $labels.instance }}";
              description = "{{ $labels.mountpoint }} is {{ $value | printf \"%.1f\" }}% full.";
            };
          }
          {
            alert = "PrometheusTargetDown";
            expr = "up == 0";
            "for" = "5m";
            labels = { severity = "critical"; };
            annotations = {
              summary = "Prometheus target {{ $labels.job }} is down";
              description = "{{ $labels.instance }} (job {{ $labels.job }}) has been down for 5 minutes.";
            };
          }
        ];
      }];
    });

    # Declarative prometheus.yml generated from Nix attrset
    # Alerting block sends to Grafana's embedded Alertmanager when alertEmail is set.
    prometheusConfigFile = pkgs.writeText "prometheus.yml" (builtins.toJSON ({
      global = {
        scrape_interval = "15s";
        evaluation_interval = "15s";
      };
      rule_files = [ "${alertRulesFile}" ];
      scrape_configs = [
        {
          job_name = "prometheus";
          static_configs = [{ targets = [ "localhost:${toString cfg.prometheus.port}" ]; }];
        }
        {
          job_name = "grafana";
          static_configs = [{ targets = [ "localhost:${toString cfg.grafana.port}" ]; }];
        }
        {
          job_name = "node_exporter";
          static_configs = [{ targets = [ "localhost:${toString cfg.nodeExporter.port}" ]; }];
        }
        {
          job_name = "loki";
          static_configs = [{ targets = [ "localhost:${toString cfg.loki.port}" ]; }];
        }
        {
          job_name = "blackbox";
          metrics_path = "/probe";
          params = { module = ["http_2xx"]; };
          static_configs = [{ targets = cfg.blackbox.targets; }];
          relabel_configs = [
            {
              source_labels = ["__address__"];
              target_label = "__param_target";
            }
            {
              source_labels = ["__param_target"];
              target_label = "instance";
            }
            {
              target_label = "__address__";
              replacement = "127.0.0.1:${toString cfg.blackbox.port}";
            }
          ];
        }
        {
          job_name = "blackbox_exporter";
          static_configs = [{ targets = [ "127.0.0.1:${toString cfg.blackbox.port}" ]; }];
        }
      ] ++ cfg.extraScrapeConfigs;
    } // lib.optionalAttrs alertingEnabled {
      alerting = {
        alertmanagers = [{
          # Real Prometheus Alertmanager running on localhost. We do NOT use
          # Grafana's embedded AM — as of Grafana 12.4 its /api/v2/alerts
          # endpoint expects a non-spec wrapped payload ({alerts: [...]}) while
          # Prometheus's client sends the standard bare-array AM v2 format,
          # causing 400 "cannot unmarshal array into PostableAlerts" on every
          # real alert. Real Alertmanager speaks canonical AM v2.
          static_configs = [{ targets = [ "localhost:${toString cfg.alertmanager.port}" ]; }];
        }];
      };
    }));

    # Grafana dashboard JSON directory (separate to avoid circular ref)
    dashboardJsonDir = pkgs.runCommand "grafana-dashboards" {} ''
      mkdir -p $out
      cp ${./grafana/dashboards/node-exporter-macos.json} $out/node-exporter-macos.json
      cp ${./grafana/dashboards/service-health.json} $out/service-health.json
      cp ${./grafana/dashboards/unraid.json} $out/unraid.json
      cp ${./grafana/dashboards/studio-logs.json} $out/studio-logs.json
    '';

    # Grafana datasource config (Prometheus + Loki + Alertmanager when alerting is on)
    datasourceConfig = pkgs.writeText "datasources.yml" (builtins.toJSON {
      apiVersion = 1;
      deleteDatasources = [
        { name = "Prometheus"; orgId = 1; }
        { name = "Loki"; orgId = 1; }
        { name = "Alertmanager"; orgId = 1; }
      ];
      datasources = [
        {
          name = "Prometheus";
          uid = "Prometheus";
          type = "prometheus";
          access = "proxy";
          url = "http://localhost:${toString cfg.prometheus.port}";
          isDefault = true;
          editable = false;
        }
        {
          name = "Loki";
          uid = "Loki";
          type = "loki";
          access = "proxy";
          url = "http://localhost:${toString cfg.loki.port}";
          editable = false;
        }
      ] ++ lib.optional alertingEnabled {
        name = "Alertmanager";
        uid = "Alertmanager";
        type = "alertmanager";
        access = "proxy";
        url = "http://localhost:${toString cfg.alertmanager.port}";
        jsonData = {
          implementation = "prometheus";
          handleGrafanaManagedAlerts = false;
        };
        editable = false;
      };
    });

    # Grafana dashboard provider config
    dashboardProviderConfig = pkgs.writeText "dashboard-provider.yml" (builtins.toJSON {
      apiVersion = 1;
      providers = [{
        name = "default";
        orgId = 1;
        folder = "";
        type = "file";
        disableDeletion = true;
        editable = false;
        options = {
          path = "${dashboardJsonDir}";
          foldersFromFilesStructure = false;
        };
      }];
    });

    # Alertmanager config — SMTP via smtp2go (reuses the same password file
    # Grafana uses for its own SMTP integration).
    alertmanagerConfigFile = pkgs.writeText "alertmanager.yml" (builtins.toJSON {
      global = {
        smtp_smarthost = "mail.smtp2go.com:2525";
        smtp_from = "grafana@snowboardtechie.com";
        smtp_auth_username = "snowboardtechie.com";
        smtp_auth_password_file = "${homeDir}/.secrets/grafana-smtp-password";
        smtp_require_tls = true;
      };
      route = {
        receiver = "email-primary";
        group_by = [ "alertname" "instance" ];
        group_wait = "30s";
        group_interval = "5m";
        repeat_interval = "4h";
      };
      receivers = [{
        name = "email-primary";
        email_configs = [{
          to = cfg.alertEmail;
          send_resolved = true;
        }];
      }];
    });

    # Loki configuration
    lokiConfigFile = pkgs.writeText "loki-local-config.yaml" (builtins.toJSON {
      auth_enabled = false;
      server = {
        http_listen_port = cfg.loki.port;
      };
      common = {
        path_prefix = cfg.loki.storagePath;
        storage = {
          filesystem = {
            chunks_directory = "${cfg.loki.storagePath}/chunks";
            rules_directory = "${cfg.loki.storagePath}/rules";
          };
        };
        replication_factor = 1;
        ring = {
          instance_addr = "127.0.0.1";
          kvstore.store = "inmemory";
        };
      };
      schema_config = {
        configs = [{
          from = "2024-01-01";
          store = "tsdb";
          object_store = "filesystem";
          schema = "v13";
          index = {
            prefix = "index_";
            period = "24h";
          };
        }];
      };
      limits_config = {
        retention_period = "720h";
      };
      compactor = {
        working_directory = "${cfg.loki.storagePath}/compactor";
        delete_request_store = "filesystem";
        retention_enabled = true;
      };
    });

    # Grafana Alloy configuration (replaces promtail — EOL March 2026)
    #
    # Pipeline:
    #   local.file_match → loki.source.file → loki.process (regex+labels) → loki.write
    #
    # The process stage extracts `service_name` from the basename of the log file
    # (e.g. /tmp/open-webui.error.log → service_name=open-webui) so LogQL queries
    # can filter by app instead of full filename.
    alloyConfigFile = pkgs.writeText "alloy-config.alloy" ''
      // Discover service log files
      local.file_match "service_logs" {
        path_targets = [{"__path__" = "/tmp/*.log", "host" = "studio"}]
      }

      // Read discovered log files and forward to processing
      loki.source.file "service_logs" {
        targets    = local.file_match.service_logs.targets
        forward_to = [loki.process.service_logs.receiver]
      }

      // Extract service_name label from the filename basename.
      // /tmp/ollama.log               -> service_name=ollama
      // /tmp/open-webui.error.log     -> service_name=open-webui
      // /tmp/open-webui.updater.log   -> service_name=open-webui  (strips first segment)
      loki.process "service_logs" {
        forward_to = [loki.write.default.receiver]

        stage.regex {
          expression = "/tmp/(?P<service>[^./]+)(?:\\.[^/]*)?\\.log$"
          source     = "filename"
        }

        stage.labels {
          values = { service_name = "service" }
        }
      }

      // Receive syslog from unraid
      loki.source.syslog "unraid" {
        listener {
          address       = "0.0.0.0:1514"
          protocol      = "udp"
          syslog_format = "rfc3164"
          labels        = { host = "unraid", service_name = "unraid" }
        }
        forward_to = [loki.relabel.syslog.receiver]
      }

      // Relabel syslog hostname
      loki.relabel "syslog" {
        rule {
          source_labels = ["__syslog_message_hostname"]
          target_label  = "hostname"
        }
        forward_to = [loki.write.default.receiver]
      }

      // Push logs to Loki
      loki.write "default" {
        endpoint {
          url = "http://localhost:${toString cfg.loki.port}/loki/api/v1/push"
        }
      }
    '';

    # Blackbox exporter configuration
    blackboxConfigFile = pkgs.writeText "blackbox.yml" (builtins.toJSON {
      modules = {
        http_2xx = {
          prober = "http";
          timeout = "5s";
          http = {
            preferred_ip_protocol = "ip4";
            valid_status_codes = [];
          };
        };
      };
    });

    # Grafana provisioning directory (datasources + dashboards only).
    # Alerting lives in Alertmanager, not Grafana.
    grafanaProvisioning = pkgs.runCommand "grafana-provisioning" {} ''
      mkdir -p $out/datasources $out/dashboards
      cp ${datasourceConfig} $out/datasources/prometheus.yml
      cp ${dashboardProviderConfig} $out/dashboards/default.yml
    '';
  in
  {
    options.services.monitoring = {
      enable = lib.mkEnableOption "Prometheus and Grafana monitoring stack";

      alertEmail = lib.mkOption {
        type = lib.types.nullOr lib.types.str;
        default = null;
        example = "bryan@snowboardtechie.com";
        description = ''
          Email address to deliver alert notifications to. Null disables
          Alertmanager entirely (Prometheus still evaluates rules but has no
          AM to send to).

          Wiring: Prometheus (rule eval) → Alertmanager (dedup/route/group) →
          smtp2go → this address. Grafana is kept as pure visualization; it
          gets Alertmanager added as a datasource so you can view alert state
          in its UI.

          Requires ~/.secrets/grafana-smtp-password (already used for
          Grafana's own SMTP integration — Alertmanager reuses it via
          smtp_auth_password_file).
        '';
      };

      prometheus = {
        port = lib.mkOption {
          type = lib.types.port;
          default = 9090;
          description = "Port for Prometheus web interface";
        };

        storagePath = lib.mkOption {
          type = lib.types.str;
          default = "${homeDir}/.prometheus/data";
          description = "Path for Prometheus TSDB storage";
        };
      };

      grafana = {
        port = lib.mkOption {
          type = lib.types.port;
          default = 3000;
          description = "Port for Grafana web interface";
        };

        configFile = lib.mkOption {
          type = lib.types.str;
          default = "/opt/homebrew/etc/grafana/grafana.ini";
          description = "Path to Grafana configuration file";
        };

        dataPath = lib.mkOption {
          type = lib.types.str;
          default = "${homeDir}/.grafana/data";
          description = "Path for Grafana data (DB, plugins). Kept outside Homebrew Cellar to survive brew upgrades.";
        };
      };

      nodeExporter = {
        port = lib.mkOption {
          type = lib.types.port;
          default = 9100;
          description = "Port for node_exporter metrics endpoint";
        };
      };

      loki = {
        port = lib.mkOption {
          type = lib.types.port;
          default = 3100;
          description = "Port for Loki HTTP API";
        };
        storagePath = lib.mkOption {
          type = lib.types.str;
          default = "${homeDir}/.loki/data";
          description = "Path for Loki data storage";
        };
      };

      alloy = {
        port = lib.mkOption {
          type = lib.types.port;
          default = 12345;
          description = "Port for Alloy HTTP server";
        };
      };

      blackbox = {
        port = lib.mkOption {
          type = lib.types.port;
          default = 9115;
          description = "Port for blackbox_exporter";
        };
        targets = lib.mkOption {
          type = lib.types.listOf lib.types.str;
          default = [
            "http://localhost:11434/api/tags"   # Ollama
            "http://localhost:8080/health"       # Open-WebUI
            "http://localhost:32400/web/index.html"  # Plex
          ];
          description = "HTTP endpoints to probe for service health";
        };
      };

      alertmanager = {
        port = lib.mkOption {
          type = lib.types.port;
          default = 9093;
          description = "Port for Alertmanager HTTP API / Web UI";
        };
        storagePath = lib.mkOption {
          type = lib.types.str;
          default = "${homeDir}/.alertmanager/data";
          description = "Path for Alertmanager data (silences, notification log)";
        };
      };

      extraScrapeConfigs = lib.mkOption {
        type = lib.types.listOf lib.types.attrs;
        default = [];
        description = "Additional Prometheus scrape configurations for remote targets";
      };
    };

    config = lib.mkIf cfg.enable {
      # Ensure storage directories exist + run firewall rules, via the only
      # nix-darwin activation extension point that actually executes:
      # `extraActivation`. (nix-darwin's system.activationScripts only composes
      # a hard-coded set of named phases into the final activate script; custom
      # names like `monitoring-setup` are silently ignored. See services/AGENTS.md.)
      system.activationScripts.extraActivation.text = lib.mkAfter (''
        # === monitoring: ensure storage directories exist ===
        mkdir -p "${cfg.prometheus.storagePath}"
        mkdir -p "${cfg.grafana.dataPath}"
        mkdir -p "${cfg.loki.storagePath}"
        mkdir -p "${homeDir}/.alloy/data"

        # === monitoring: firewall rules ===
        /usr/libexec/ApplicationFirewall/socketfilterfw --add ${pkgs.prometheus}/bin/prometheus >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --unblock ${pkgs.prometheus}/bin/prometheus >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --add /opt/homebrew/opt/grafana/bin/grafana >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --unblock /opt/homebrew/opt/grafana/bin/grafana >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --add /opt/homebrew/opt/node_exporter/bin/node_exporter >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --unblock /opt/homebrew/opt/node_exporter/bin/node_exporter >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --add /opt/homebrew/opt/loki/bin/loki >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --unblock /opt/homebrew/opt/loki/bin/loki >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --add /opt/homebrew/opt/grafana-alloy/bin/alloy >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --unblock /opt/homebrew/opt/grafana-alloy/bin/alloy >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --add ${pkgs.prometheus-blackbox-exporter}/bin/blackbox_exporter >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --unblock ${pkgs.prometheus-blackbox-exporter}/bin/blackbox_exporter >/dev/null 2>&1 || true
      '' + lib.optionalString alertingEnabled ''

        # === alertmanager: storage + firewall + preflight ===
        mkdir -p "${cfg.alertmanager.storagePath}"
        /usr/libexec/ApplicationFirewall/socketfilterfw --add ${pkgs.prometheus-alertmanager}/bin/alertmanager >/dev/null 2>&1 || true
        /usr/libexec/ApplicationFirewall/socketfilterfw --unblock ${pkgs.prometheus-alertmanager}/bin/alertmanager >/dev/null 2>&1 || true

        if [ ! -s "${homeDir}/.secrets/grafana-smtp-password" ]; then
          echo "WARNING: services.monitoring.alertEmail is set but ${homeDir}/.secrets/grafana-smtp-password" >&2
          echo "         is missing. Alertmanager will start but email delivery will fail." >&2
        fi
      '');

      # Prometheus service configuration
      launchd.user.agents.prometheus = {
        serviceConfig = {
          ProgramArguments = [
            "${pkgs.prometheus}/bin/prometheus"
            "--config.file=${prometheusConfigFile}"
            "--web.listen-address=0.0.0.0:${toString cfg.prometheus.port}"
            "--storage.tsdb.path=${cfg.prometheus.storagePath}"
            "--storage.tsdb.retention.time=30d"
          ];
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "/tmp/prometheus.log";
          StandardErrorPath = "/tmp/prometheus.error.log";
        };
      };

      # Grafana service configuration
      # SMTP password: read via $__file{} in grafanaCustomIni from ~/.secrets/grafana-smtp-password
      # Admin password (for alerting): ~/.secrets/grafana-admin-password via GF_SECURITY_ADMIN_PASSWORD__FILE
      # Create the secrets files:
      #   echo '<smtp-password>' > ~/.secrets/grafana-smtp-password && chmod 600 $_
      #   echo '<admin-password>' > ~/.secrets/grafana-admin-password && chmod 600 $_
      launchd.user.agents.grafana = {
        serviceConfig = {
          ProgramArguments = [
            "/opt/homebrew/opt/grafana/bin/grafana"
            "server"
            "--homepath=/opt/homebrew/opt/grafana/share/grafana"
            "--config=${grafanaCustomIni}"
          ];
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "/tmp/grafana.log";
          StandardErrorPath = "/tmp/grafana.error.log";
          EnvironmentVariables = {
            GF_SERVER_HTTP_ADDR = "0.0.0.0";
            GF_SERVER_HTTP_PORT = toString cfg.grafana.port;
            GF_PATHS_PROVISIONING = "${grafanaProvisioning}";
            GF_PATHS_DATA = cfg.grafana.dataPath;
            GF_SMTP_ENABLED = "true";
            GF_SMTP_HOST = "mail.smtp2go.com:2525";
            GF_SMTP_FROM_ADDRESS = "grafana@snowboardtechie.com";
            GF_SMTP_FROM_NAME = "Studio Grafana";
          };
          # Note: we deliberately do NOT set GF_SECURITY_ADMIN_PASSWORD__FILE.
          # Prometheus authenticates to Grafana's embedded AM with a dedicated
          # service account token, so the personal admin login stays untouched.
        };
      };

      # node_exporter service configuration
      launchd.user.agents.node-exporter = {
        serviceConfig = {
          ProgramArguments = [
            "/opt/homebrew/opt/node_exporter/bin/node_exporter"
            "--web.listen-address=0.0.0.0:${toString cfg.nodeExporter.port}"
            "--no-collector.thermal"
          ];
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "/tmp/node_exporter.log";
          StandardErrorPath = "/tmp/node_exporter.error.log";
        };
      };

      # Loki service configuration
      launchd.user.agents.loki = {
        serviceConfig = {
          ProgramArguments = [
            "/opt/homebrew/opt/loki/bin/loki"
            "-config.file=${lokiConfigFile}"
          ];
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "/tmp/loki.log";
          StandardErrorPath = "/tmp/loki.error.log";
        };
      };

      # Grafana Alloy service configuration (log shipper → Loki)
      launchd.user.agents.alloy = {
        serviceConfig = {
          ProgramArguments = [
            "/opt/homebrew/opt/grafana-alloy/bin/alloy"
            "run"
            "${alloyConfigFile}"
            "--storage.path=${homeDir}/.alloy/data"
            "--server.http.listen-addr=0.0.0.0:${toString cfg.alloy.port}"
          ];
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "/tmp/alloy.log";
          StandardErrorPath = "/tmp/alloy.error.log";
        };
      };

      # blackbox_exporter service configuration
      launchd.user.agents.blackbox-exporter = {
        serviceConfig = {
          ProgramArguments = [
            "${pkgs.prometheus-blackbox-exporter}/bin/blackbox_exporter"
            "--config.file=${blackboxConfigFile}"
            "--web.listen-address=0.0.0.0:${toString cfg.blackbox.port}"
          ];
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "/tmp/blackbox_exporter.log";
          StandardErrorPath = "/tmp/blackbox_exporter.error.log";
        };
      };

      # Alertmanager — receives alerts from Prometheus, dedups/groups/routes
      # and delivers via SMTP. Only started when alertEmail is configured.
      # `--cluster.listen-address=` intentionally empty: single-node, skip
      # gossip/HA which would otherwise bind on UDP:9094.
      launchd.user.agents.alertmanager = lib.mkIf alertingEnabled {
        serviceConfig = {
          ProgramArguments = [
            "${pkgs.prometheus-alertmanager}/bin/alertmanager"
            "--config.file=${alertmanagerConfigFile}"
            "--storage.path=${cfg.alertmanager.storagePath}"
            "--web.listen-address=0.0.0.0:${toString cfg.alertmanager.port}"
            "--cluster.listen-address="
          ];
          RunAtLoad = true;
          KeepAlive = true;
          StandardOutPath = "/tmp/alertmanager.log";
          StandardErrorPath = "/tmp/alertmanager.error.log";
        };
      };
    };
  };

  # NixOS aspect - stub for future implementation
  flake.modules.nixos.monitoring = { config, lib, ... }: {
    # TODO: Implement NixOS equivalent using systemd services
    # NixOS has native prometheus and grafana service modules
  };
}
