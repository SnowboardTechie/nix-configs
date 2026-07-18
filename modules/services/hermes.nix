# Hermes Agent client packages and macOS service supervision.
#
# The upstream Nix package makes the CLI/Desktop reproducible on all client
# hosts. On macOS, Matrix is intentionally excluded from that package upstream,
# so the primary Studio gateway/dashboard agents continue to use Hermes's
# managed venv in ~/.hermes while nix-darwin owns their launchd definitions
# and lifecycle. Additional headless users run the Nix-built package directly.
# Desktop remote URL/token state remains in its per-user settings. Do not inject
# only HERMES_DESKTOP_REMOTE_URL: that env path requires a paired token and
# bypasses the client's already-saved authentication.
{ inputs, ... }:
{
  flake.modules.darwin.hermes =
    { config
    , lib
    , pkgs
    , ...
    }:
    let
      cfg = config.services.hermes;
      system = pkgs.stdenv.hostPlatform.system;
      homeDirectory =
        if cfg.homeDirectory != null then cfg.homeDirectory else "/Users/${cfg.user}";
      hermesHome = "${homeDirectory}/.hermes";
      runtimeVenv = "${hermesHome}/hermes-agent/venv";
      runtimePython =
        if cfg.runtimePython != null then cfg.runtimePython else "${runtimeVenv}/bin/python";
      upstreamDesktop = inputs.hermes-agent.packages.${system}.desktop;
      headlessInstanceModule =
        { name
        , config
        , ...
        }:
        {
          options = {
            user = lib.mkOption {
              type = lib.types.str;
              default = name;
              description = "macOS user that owns and runs this Hermes instance.";
            };

            homeDirectory = lib.mkOption {
              type = lib.types.str;
              default = "/Users/${config.user}";
              description = "Home directory for the instance user.";
            };

            secureHome = lib.mkOption {
              type = lib.types.bool;
              default = true;
              description = "Restrict the user's home directory to mode 0700.";
            };

            gateway.enable = lib.mkOption {
              type = lib.types.bool;
              default = false;
              description = "Run this user's messaging gateway as a boot-time LaunchDaemon.";
            };

            serve = {
              enable = lib.mkOption {
                type = lib.types.bool;
                default = false;
                description = "Run this user's Desktop backend as a boot-time LaunchDaemon.";
              };
              host = lib.mkOption {
                type = lib.types.str;
                default = "127.0.0.1";
                description = "Address passed to hermes serve. Non-loopback addresses require Hermes authentication.";
              };
              port = lib.mkOption {
                type = lib.types.port;
                default = 9119;
                description = "Port passed to hermes serve.";
              };
              tailscale = {
                enable = lib.mkOption {
                  type = lib.types.bool;
                  default = false;
                  description = "Expose a loopback-bound backend through Tailscale Serve HTTPS.";
                };
                httpsPort = lib.mkOption {
                  type = lib.types.port;
                  default = config.serve.port;
                  description = "Tailnet-only HTTPS port exposed by Tailscale Serve.";
                };
                proxyPort = lib.mkOption {
                  type = lib.types.port;
                  default = config.serve.port + 1;
                  description = "Loopback reverse-proxy port used to normalize the Host header for Hermes.";
                };
              };
            };
          };
        };
      enabledHeadlessInstances = lib.filterAttrs
        (_: instance: instance.gateway.enable || instance.serve.enable)
        cfg.headlessInstances;
      enabledHeadlessServeInstances = lib.filterAttrs
        (_: instance: instance.serve.enable)
        enabledHeadlessInstances;
      enabledHeadlessGatewayInstances = lib.filterAttrs
        (_: instance: instance.gateway.enable)
        enabledHeadlessInstances;
      enabledTailscaleServeInstances = lib.filterAttrs
        (_: instance: instance.serve.tailscale.enable)
        enabledHeadlessServeInstances;
      localServePorts = lib.optional cfg.dashboard.enable cfg.dashboard.port
        ++ lib.mapAttrsToList (_: instance: instance.serve.port) enabledHeadlessServeInstances;
      localProxyPorts = lib.mapAttrsToList
        (_: instance: instance.serve.tailscale.proxyPort)
        enabledTailscaleServeInstances;
      tailscaleHttpsPorts = lib.mapAttrsToList
        (_: instance: instance.serve.tailscale.httpsPort)
        enabledTailscaleServeInstances;
      headlessEnvironment = instance: {
        HOME = instance.homeDirectory;
        HERMES_HOME = "${instance.homeDirectory}/.hermes";
        PATH = lib.concatStringsSep ":" [
          "${instance.homeDirectory}/.local/bin"
          "${cfg.package}/bin"
          "/run/current-system/sw/bin"
          "${config.homebrew.prefix}/bin"
          "/usr/bin"
          "/bin"
          "/usr/sbin"
          "/sbin"
        ];
      };
      headlessServeDaemons = lib.mapAttrs'
        (name: instance:
          lib.nameValuePair "hermes-${name}-serve" {
            serviceConfig = {
              Label = "ai.hermes.serve-${name}";
              UserName = instance.user;
              ProgramArguments = [
                "${cfg.package}/bin/hermes"
                "serve"
                "--host"
                instance.serve.host
                "--port"
                (toString instance.serve.port)
                "--skip-build"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = instance.homeDirectory;
              EnvironmentVariables = headlessEnvironment instance;
              StandardOutPath = "${instance.homeDirectory}/.hermes/logs/serve.log";
              StandardErrorPath = "${instance.homeDirectory}/.hermes/logs/serve.error.log";
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          })
        enabledHeadlessServeInstances;
      headlessGatewayDaemons = lib.mapAttrs'
        (name: instance:
          lib.nameValuePair "hermes-${name}-gateway" {
            serviceConfig = {
              Label = "ai.hermes.gateway-${name}";
              UserName = instance.user;
              ProgramArguments = [
                "${cfg.package}/bin/hermes"
                "gateway"
                "run"
                "--replace"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = instance.homeDirectory;
              EnvironmentVariables = headlessEnvironment instance;
              StandardOutPath = "${instance.homeDirectory}/.hermes/logs/gateway.log";
              StandardErrorPath = "${instance.homeDirectory}/.hermes/logs/gateway.error.log";
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          })
        enabledHeadlessGatewayInstances;
      headlessTailscaleProxyDaemons = lib.mapAttrs'
        (name: instance:
          let
            caddyConfig = pkgs.writeText "hermes-${name}-tailscale-proxy.json" (builtins.toJSON {
              admin.disabled = true;
              apps.http.servers.hermes = {
                listen = [ "127.0.0.1:${toString instance.serve.tailscale.proxyPort}" ];
                automatic_https.disable = true;
                routes = [
                  {
                    handle = [
                      {
                        handler = "reverse_proxy";
                        headers.request.set.Host = [ "127.0.0.1:${toString instance.serve.port}" ];
                        upstreams = [{ dial = "127.0.0.1:${toString instance.serve.port}"; }];
                      }
                    ];
                  }
                ];
              };
            });
          in
          lib.nameValuePair "hermes-${name}-tailscale-proxy" {
            serviceConfig = {
              Label = "ai.hermes.serve-${name}-tailscale-proxy";
              UserName = instance.user;
              ProgramArguments = [
                "${pkgs.caddy}/bin/caddy"
                "run"
                "--config"
                "${caddyConfig}"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = instance.homeDirectory;
              EnvironmentVariables = headlessEnvironment instance;
              StandardOutPath = "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.log";
              StandardErrorPath = "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.error.log";
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          })
        enabledTailscaleServeInstances;
      serviceEnvironment = {
        HOME = homeDirectory;
        HERMES_HOME = hermesHome;
        VIRTUAL_ENV = runtimeVenv;
        PATH = lib.concatStringsSep ":" [
          "${runtimeVenv}/bin"
          "${homeDirectory}/.local/bin"
          "${cfg.package}/bin"
          "/run/current-system/sw/bin"
          "${config.homebrew.prefix}/bin"
          "/usr/bin"
          "/bin"
          "/usr/sbin"
          "/sbin"
        ];
      };
    in
    {
      options.services.hermes = {
        enable = lib.mkEnableOption "Hermes Agent client packages";

        package = lib.mkOption {
          type = lib.types.package;
          default = inputs.hermes-agent.packages.${system}.default;
          description = "Nix-built Hermes CLI package installed for interactive client use.";
        };

        user = lib.mkOption {
          type = lib.types.str;
          default = config.system.primaryUser;
          description = "User whose Hermes state and launchd agents are managed.";
        };

        homeDirectory = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = "Hermes user's home directory; defaults to /Users/<user>.";
        };

        secureHome = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Restrict the primary Hermes user's home directory to mode 0700.";
        };

        headlessInstances = lib.mkOption {
          type = lib.types.attrsOf (lib.types.submodule headlessInstanceModule);
          default = { };
          description = ''
            Additional per-user Hermes instances supervised as LaunchDaemons.
            These start at boot without a GUI login and use the Nix-built Hermes
            package, so Matrix support is unavailable unless added upstream.
          '';
        };

        desktop.enable = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Install the Hermes Desktop client.";
        };

        clientOnly = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Remove legacy auto-start gateway/serve agents so this host cannot compete with the primary gateway.";
        };

        runtimePython = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          description = ''
            Python executable from Hermes's managed macOS installation. The
            upstream Darwin Nix package excludes Matrix, so gateway services use
            this mutable venv while Nix manages launchd supervision.
          '';
        };

        gateway.enable = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Run the canonical Hermes messaging gateway at login.";
        };

        dashboard = {
          enable = lib.mkOption {
            type = lib.types.bool;
            default = false;
            description = "Expose the authenticated Hermes browser dashboard and remote backend through launchd.";
          };
          host = lib.mkOption {
            type = lib.types.str;
            default = "127.0.0.1";
            description = "Address passed to hermes dashboard; use a Tailscale address for remote clients.";
          };
          port = lib.mkOption {
            type = lib.types.port;
            default = 9119;
            description = "Port passed to hermes dashboard.";
          };
        };
      };

      config = lib.mkIf cfg.enable (lib.mkMerge [
        {
          environment.systemPackages = [ cfg.package ]
            ++ lib.optional cfg.desktop.enable upstreamDesktop;

          assertions = [
            {
              assertion = builtins.length (localServePorts ++ localProxyPorts)
                == builtins.length (lib.unique (localServePorts ++ localProxyPorts));
              message = "services.hermes dashboard/serve/proxy ports must be unique";
            }
            {
              assertion = builtins.length tailscaleHttpsPorts
                == builtins.length (lib.unique tailscaleHttpsPorts);
              message = "services.hermes Tailscale Serve HTTPS ports must be unique";
            }
          ] ++ lib.mapAttrsToList
            (name: instance: {
              assertion = instance.gateway.enable || instance.serve.enable;
              message = "services.hermes.headlessInstances.${name} must enable gateway or serve";
            })
            cfg.headlessInstances
          ++ lib.mapAttrsToList
            (name: instance: {
              assertion = !instance.serve.tailscale.enable
                || (instance.serve.enable
                && instance.serve.host == "127.0.0.1"
                && config.services.tailscale.enable);
              message = "services.hermes.headlessInstances.${name}.serve.tailscale requires a loopback serve host and services.tailscale";
            })
            cfg.headlessInstances;

          launchd.daemons = headlessServeDaemons
            // headlessGatewayDaemons
            // headlessTailscaleProxyDaemons;

          system.activationScripts.extraActivation.text = lib.mkAfter (
            lib.optionalString cfg.secureHome ''
              # === Hermes primary-user home isolation ===
              /bin/chmod 0700 ${lib.escapeShellArg homeDirectory}
            ''
            + lib.concatMapStrings
              (instance: ''
                # === Hermes headless instance: ${instance.user} ===
                ${lib.optionalString instance.secureHome "/bin/chmod 0700 ${lib.escapeShellArg instance.homeDirectory}"}
                /bin/mkdir -p ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs"}
                /usr/bin/touch \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/serve.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/serve.error.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/gateway.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/gateway.error.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.error.log"}
                /usr/sbin/chown ${lib.escapeShellArg instance.user}:staff \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/serve.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/serve.error.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/gateway.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/gateway.error.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.log"} \
                  ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/tailscale-proxy.error.log"}
                /bin/chmod 0700 ${lib.escapeShellArg "${instance.homeDirectory}/.hermes"}
                /bin/chmod 0600 ${lib.escapeShellArg "${instance.homeDirectory}/.hermes/logs/"}*.log
              '')
              (lib.attrValues enabledHeadlessInstances)
            + lib.concatMapStrings
              (instance: lib.optionalString instance.serve.tailscale.enable ''
                # === Hermes headless backend via Tailscale Serve ===
                if ! ${pkgs.coreutils}/bin/timeout --foreground 30s \
                  ${config.services.tailscale.package}/bin/tailscale serve \
                    --bg \
                    --yes \
                    --https=${toString instance.serve.tailscale.httpsPort} \
                    http://127.0.0.1:${toString instance.serve.tailscale.proxyPort}; then
                  echo "Failed to configure Tailscale Serve for Hermes on port ${toString instance.serve.tailscale.httpsPort}" >&2
                  exit 1
                fi
              '')
              (lib.attrValues enabledHeadlessServeInstances)
          );
        }

        (lib.mkIf (cfg.gateway.enable || cfg.dashboard.enable) {
          assertions = [
            {
              assertion = cfg.user == config.system.primaryUser;
              message = "services.hermes launchd agents must run as system.primaryUser";
            }
          ];
        })

        (lib.mkIf cfg.clientOnly {
          assertions = [
            {
              assertion = !cfg.gateway.enable && !cfg.dashboard.enable;
              message = "services.hermes.clientOnly conflicts with gateway.enable or dashboard.enable";
            }
            {
              assertion = enabledHeadlessInstances == { };
              message = "services.hermes.clientOnly conflicts with headlessInstances";
            }
          ];

          system.activationScripts.extraActivation.text = lib.mkAfter ''
            # === Hermes client-only role ===
            # Remove gateway definitions left by a previous manual migration.
            hermes_uid=$(/usr/bin/id -u ${lib.escapeShellArg cfg.user} 2>/dev/null || true)
            if [ -n "$hermes_uid" ]; then
              /bin/launchctl bootout "user/$hermes_uid/ai.hermes.gateway" >/dev/null 2>&1 || true
              /bin/launchctl bootout "gui/$hermes_uid/ai.hermes.gateway" >/dev/null 2>&1 || true
              /bin/launchctl bootout "user/$hermes_uid/ai.hermes.serve" >/dev/null 2>&1 || true
              /bin/launchctl bootout "gui/$hermes_uid/ai.hermes.serve" >/dev/null 2>&1 || true
              /bin/launchctl bootout "user/$hermes_uid/ai.hermes.dashboard" >/dev/null 2>&1 || true
              /bin/launchctl bootout "gui/$hermes_uid/ai.hermes.dashboard" >/dev/null 2>&1 || true
            fi
            /bin/rm -f \
              ${lib.escapeShellArg "${homeDirectory}/Library/LaunchAgents/ai.hermes.gateway.plist"} \
              ${lib.escapeShellArg "${homeDirectory}/Library/LaunchAgents/ai.hermes.serve.plist"} \
              ${lib.escapeShellArg "${homeDirectory}/Library/LaunchAgents/ai.hermes.dashboard.plist"}
          '';
        })

        (lib.mkIf cfg.gateway.enable {
          launchd.user.agents.hermes-gateway = {
            serviceConfig = {
              # Match the label and plist name used by `hermes gateway install`
              # so nix-darwin adopts the existing service instead of creating a
              # second canonical gateway.
              Label = "ai.hermes.gateway";
              ProgramArguments = [
                runtimePython
                "-m"
                "hermes_cli.main"
                "gateway"
                "run"
                "--replace"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = homeDirectory;
              EnvironmentVariables = serviceEnvironment;
              StandardOutPath = "${hermesHome}/logs/gateway.log";
              StandardErrorPath = "${hermesHome}/logs/gateway.error.log";
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          };
        })

        (lib.mkIf cfg.dashboard.enable {
          launchd.user.agents.hermes-dashboard = {
            serviceConfig = {
              Label = "ai.hermes.dashboard";
              ProgramArguments = [
                runtimePython
                "-m"
                "hermes_cli.main"
                "dashboard"
                "--host"
                cfg.dashboard.host
                "--port"
                (toString cfg.dashboard.port)
                "--no-open"
              ];
              RunAtLoad = true;
              KeepAlive = true;
              WorkingDirectory = homeDirectory;
              EnvironmentVariables = serviceEnvironment;
              StandardOutPath = "${hermesHome}/logs/dashboard.log";
              StandardErrorPath = "${hermesHome}/logs/dashboard.error.log";
              ProcessType = "Background";
              ThrottleInterval = 10;
            };
          };
        })
      ]);
    };

  flake.modules.nixos.hermes =
    { config
    , lib
    , pkgs
    , ...
    }:
    let
      cfg = config.services.hermes;
      system = pkgs.stdenv.hostPlatform.system;
      upstreamDesktop = inputs.hermes-agent.packages.${system}.desktop;
      desktopEntry = pkgs.makeDesktopItem {
        name = "hermes-desktop";
        desktopName = "Hermes";
        comment = "Nous Research Hermes Agent desktop client";
        exec = "hermes-desktop";
        icon = ./assets/hermes-icon.png;
        terminal = false;
        categories = [
          "Network"
          "Utility"
        ];
      };
    in
    {
      options.services.hermes = {
        enable = lib.mkEnableOption "Hermes Agent client packages";

        package = lib.mkOption {
          type = lib.types.package;
          default = inputs.hermes-agent.packages.${system}.default;
          description = "Nix-built Hermes CLI package installed for client use.";
        };

        desktop.enable = lib.mkOption {
          type = lib.types.bool;
          default = false;
          description = "Install the Hermes Desktop client and application launcher.";
        };
      };

      config = lib.mkIf cfg.enable {
        environment.systemPackages = [ cfg.package ]
          ++ lib.optionals cfg.desktop.enable [
          upstreamDesktop
          desktopEntry
        ];
      };
    };
}
